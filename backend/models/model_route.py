"""The model-routing table — one row per upstream model endpoint.

``model_routes`` is the registry a downstream intranet DSH reads to decide which
model endpoints exist, what they are for and how to authenticate against them.
It is **the** source of truth: the table replaced the former
``config/model_routes.json`` document, which could only be edited by one process
at a time and had to live on a writable (or read-only, depending on which
container mounted it) file.  A shipped route set now arrives as the
``config/model_routes.seed.sql`` script; nothing is imported from JSON.

This module is the single source of truth for the schema *and* for the two
vocabularies that are closed sets, which is what lets the generated CHECK
constraints below stay in step with the services that validate against them:

* :data:`PROVIDERS` — the **wire format** (``openai`` / ``mineru`` /
  ``anthropic``).  It decides the request shape and the default endpoint path.
* :data:`KINDS` — the **purpose** (``chat`` / ``completion`` / ``embedding`` /
  ``rerank`` / ``ocr`` / ``asr`` / ``tts``).  It is what a downstream client
  consults to decide whether a route may back an LLM provider, an embedding
  index or a document-parse step.

A route is one endpoint, so it carries exactly one kind; a server that answers
both ``/v1/chat/completions`` and ``/v1/embeddings`` is two rows.

Two representation choices worth stating, because both are deliberate:

``api_key``
    A **sealed** credential, not a plaintext one.  The row *is* the registry,
    and the whole point of the table is that a downstream client can
    authenticate from it without a human copying a key around — which used to
    mean the table held live plaintext credentials, and a database dump, a
    backup or a replicated volume leaked every upstream key at once.  So the
    key is sealed with Fernet under :attr:`config.keys.KeysConfig.model_route_key`
    and stored as an envelope carrying :data:`API_KEY_PREFIX`; the plaintext
    exists only in the process that is about to send the request.  There is
    deliberately no "read it from this environment variable instead" column:
    that made the table non-self-describing (one row's behaviour depended on the
    deploying process) without protecting anything end-to-end, since
    ``GET /api/v1/models/resolved`` hands the plaintext to a client either way.
    A value **without** the prefix is a row written before sealing existed; the
    read path still serves it and reports ``api_key_source: "plaintext"`` so the
    operator can see the outstanding work, and
    :mod:`models.model_route_migrate` is how it is re-sealed.

``aliases`` / ``health``
    TEXT holding JSON, not SQLAlchemy's ``JSON`` type: the same DDL then works
    on SQLite and PostgreSQL with no dialect branch, which is the same trade
    ``models.agent_hub`` makes for ``RepoIssue.labels`` and ``AgentTask.payload``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped

from .base import Base, in_check, utcnow

# ── Vocabularies the schema refuses to store anything else ───────────
# Both tuples are closed sets: the panel offers them as a select, the API
# publishes them as an enum, and a downstream client can switch on them without
# guessing free-text tags.  The CHECK constraints below are generated from them,
# so a typo is an IntegrityError at write time rather than a row that only one
# query accidentally matches.

#: Wire formats a route may speak.  The values are the canonical spellings.
PROVIDERS: tuple[str, ...] = ("openai", "mineru", "anthropic")

#: What a route is *for*.  Orthogonal to :data:`PROVIDERS`.
KINDS: tuple[str, ...] = (
    "chat",        # 对话
    "completion",  # 文本补全（/v1/completions 一类的续写端点）
    "embedding",   # 向量化
    "rerank",      # 重排序
    "ocr",         # 文档解析 / OCR
    "asr",         # 语音转文字
    "tts",         # 文字转语音
)

#: ``name`` / ``description`` ceilings — generous, but bounded so one edit
#: cannot balloon a row.  They are column lengths too, so the database rejects
#: an over-long value even when it arrives from hand-written SQL.
MAX_NAME = 128
MAX_DESCRIPTION = 500

#: What a sealed ``api_key`` starts with — ``<prefix><fernet token>``.
#:
#: The prefix, not the token, is what makes "sealed" decidable: a Fernet token
#: is only recognisable by attempting to open it, so without a marker a reader
#: could not tell a legacy plaintext row from an envelope without already
#: holding the master key.  It is versioned so a future cipher change can be
#: rolled out per row instead of as a flag day.
API_KEY_PREFIX = "enc:v1:"


class ModelRoute(Base):
    """One upstream model endpoint.

    Rows are ordered by ``id``, which is insertion order: the first **enabled**
    row is the default a run picks when it names no route, so "move a route to
    the top" and "enable it" are not the same edit.
    """

    __tablename__ = "model_routes"
    __table_args__ = (
        CheckConstraint(in_check("provider", PROVIDERS), name="ck_model_routes_provider"),
        CheckConstraint(in_check("kind", KINDS), name="ck_model_routes_kind"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    #: The addressable name, and the conflict target the seed script upserts on.
    name: Mapped[str] = Column(String(MAX_NAME), nullable=False, unique=True, index=True)
    provider: Mapped[str] = Column(String(32), nullable=False, default="openai")
    kind: Mapped[str] = Column(String(32), nullable=False, default="chat")
    base_url: Mapped[str] = Column(String(1024), nullable=False)
    #: Endpoint below ``base_url``.  The empty string means "the protocol
    #: default", which is resolved on read so a protocol-wide change reaches
    #: every row that never overrode it.
    path: Mapped[str] = Column(String(256), nullable=False, default="")
    #: Upstream model id (``deepseek-flash``), not the route name.
    model: Mapped[str] = Column(String(256), nullable=False, default="")
    #: Upstream key, sealed — an :data:`API_KEY_PREFIX` envelope, or ``""`` when
    #: the route needs no credential.  Never a plaintext value: see the module
    #: docstring, and ``services.model_routes.normalize_api_key`` for the
    #: header-safety rule the plaintext must satisfy *before* it is sealed (a
    #: CR/LF here would be request splitting).
    api_key: Mapped[str] = Column(String(1024), nullable=False, default="")
    #: JSON array of local aliases; ``default`` marks the intranet default model.
    aliases: Mapped[str] = Column(Text, nullable=False, default="[]")
    enabled: Mapped[bool] = Column(Boolean, nullable=False, default=True)
    description: Mapped[str] = Column(String(MAX_DESCRIPTION), nullable=False, default="")

    # ── Last connectivity probe ─────────────────────────────────────
    # In this table rather than beside it: a probe belongs to exactly one route,
    # so a row carries its own answer, a rename keeps it and a delete takes it
    # away.  It stays an opaque JSON document because it is display-only — the
    # published payload shows it under ``health`` and nothing queries into it.
    health: Mapped[str | None] = Column(Text, nullable=True)

    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    # ── JSON accessors ───────────────────────────────────────────────

    def alias_list(self) -> list[str]:
        """The aliases as a list; a corrupt value degrades to ``[]``."""
        try:
            parsed = json.loads(self.aliases or "[]")
        except ValueError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    def set_aliases(self, values: list[str]) -> None:
        """Store the aliases as a JSON array."""
        self.aliases = json.dumps([str(value) for value in values], ensure_ascii=False)

    def health_dict(self) -> dict[str, Any] | None:
        """The remembered probe result, or ``None`` when one never ran."""
        if not self.health:
            return None
        try:
            parsed = json.loads(self.health)
        except ValueError:
            return None
        return dict(parsed) if isinstance(parsed, dict) else None

    def set_health(self, health: Mapping[str, Any] | None) -> None:
        """Store the remembered probe result; ``None`` clears it."""
        self.health = (
            None if health is None else json.dumps(dict(health), ensure_ascii=False)
        )


__all__ = [
    "API_KEY_PREFIX",
    "KINDS",
    "MAX_DESCRIPTION",
    "MAX_NAME",
    "PROVIDERS",
    "ModelRoute",
]
