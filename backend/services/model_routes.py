"""The model-routing registry — read, edit and probe the routes DSH talks to.

The registry lives in the ``model_routes`` table (see
:mod:`models.model_route`); it replaced a single JSON document that had to sit
on a writable — or, in the sandbox runner, deliberately read-only — file, so
that exactly one process could ever edit it and a hand-edit was silently
tolerated.  This module owns the whole lifecycle of a route:

* **Read** — :func:`load` returns the API-shaped payload (never the raw API
  keys) the SPA renders; :func:`resolve` returns the machine view, keys
  included, that a downstream intranet DSH consumes; :func:`raw_route` returns
  one stored route for a probe.
* **Write** — :func:`create`, :func:`update` and :func:`delete` validate one
  route and commit it.  The **table is the source of truth**; a shipped route
  set arrives as ``config/model_routes.seed.sql``, and nothing is imported from
  JSON.
* **Probe** — :func:`probe` asks the endpoint for its model list through the
  OpenAI client and :func:`record_health` remembers the answer **on that route's
  row**, so a rename keeps its probe result and a delete takes it away.

Every outbound request a route provokes is made by that client, never by a
hand-rolled HTTP call: :class:`openai.OpenAI` speaks the ``openai`` wire format
natively and reaches every other route through the same call (see
:func:`probe_url`).  One client means the format the table stores, the library
that speaks it and the headers :func:`request_headers` builds are one behaviour
instead of three that have to be kept in step.

Every route is classified on two independent axes:

* ``provider`` — the **wire format** (``openai`` / ``anthropic``).  It decides
  the request shape and the default endpoint ``path``.  A MinerU-style
  document-parse endpoint is an ``openai`` route with ``kind`` ``ocr``, not a
  protocol of its own: it answers the OpenAI format, so it is spoken to the same
  way as any other OpenAI-compatible endpoint.
* ``kind`` — the **purpose** (``chat`` / ``completion`` / ``embedding`` /
  ``rerank`` / ``ocr`` / ``asr`` / ``tts``).  It is what a downstream client
  consults to decide whether a route may back an LLM provider, an embedding
  index or a document-parse step.

A route is one endpoint, so it carries exactly one kind; a server that answers
both ``/v1/chat/completions`` and ``/v1/embeddings`` is two routes.  Both axes
default from each other where that is obvious (see :data:`DEFAULT_KINDS`), so a
row whose ``kind`` was never filled in still classifies correctly.

Alongside those, a route carries the ``base_url``, an optional ``api_key``, the
endpoint ``path``, an optional ``model`` id and display ``aliases``.  A route's
``name`` and ``description`` are mandatory; the API key may be empty.

The key is **sealed at rest**: :func:`seal_api_key` wraps the plaintext in a
Fernet envelope (prefix :data:`models.model_route.API_KEY_PREFIX`) under
``MODEL_ROUTE_KEY`` before anything is stored, so the table, its backups and any
replica carry no usable credential.  The plaintext exists only in the process
about to send a request — see :func:`effective_api_key`, which is the one place
that decides what a route authenticates with, and :func:`migrate_plaintext_keys`,
which re-seals the rows that predate this.

Validation lives here rather than in the route layer because there are three
callers — the panel, the seed path and the runner — and a rule that only two of
them apply is a rule that will drift.

The key is also a **header value** the moment it is used, so its character set is
an allowlist (:func:`is_header_safe`) rather than a CR/LF blocklist, and the
allowlist is enforced both when a key is accepted and again when one is read back
out — a rule checked only on the write path says nothing about the rows that are
already there.
"""

from __future__ import annotations

import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import openai
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from config.keys import KeysConfig
from models.model_route import (
    KINDS,
    MAX_DESCRIPTION,
    MAX_NAME,
    PROVIDERS,
    API_KEY_PREFIX,
    ModelRoute,
)
from services.format import utc_now_iso
from services.headers import (
    UnsafeHeaderError,
    ValuePolicy,
    checked_headers,
    is_header_value_safe,
)
from services.sealing import SealingKeyMissing, SecretSealer
from services.urlsafety import UnsafeUrlError, check_outbound_url


#: What ``source`` reports in the API payload.  The field predates the move to
#: the database, when it named the file being read; it is now a stable label the
#: panel can print instead of a path that no longer exists.
SOURCE = "model_routes"

#: The payload's own version, kept at the value the file-era document used so a
#: downstream client that checks it keeps working.
PAYLOAD_VERSION = 1

#: The kind a route falls back to when it does not name one, keyed by protocol.
#: Both formats describe a chat model unless the route says otherwise — a
#: document-parse endpoint (``kind: ocr``) is an OpenAI-format route like any
#: other — so a row whose kind was never filled in still classifies the same way.
DEFAULT_KINDS: dict[str, str] = {
    "openai": "chat",
    "anthropic": "chat",
}

#: Spellings seen in hand-written input that map onto a canonical provider.
PROVIDER_ALIASES: dict[str, str] = {
    "openai": "openai",
    "openai-compatible": "openai",
    "openai_compatible": "openai",
    "openai_compat": "openai",
    "anthropic": "anthropic",
    "claude": "anthropic",
}

#: Endpoint used when a route does not name one itself.
DEFAULT_PATHS: dict[str, str] = {
    "openai": "/v1/chat/completions",
    "anthropic": "/v1/messages",
}

#: Route names that would collide with the probe endpoint's own path.
RESERVED_NAMES: frozenset[str] = frozenset({"probe"})

#: A probe labels itself, so its traffic is recognisable in an upstream's logs.
#: It overrides the client's own ``User-Agent`` rather than adding to it.
_PROBE_USER_AGENT = "cpypiserver-model-probe/1.0"

#: The Anthropic Messages API version a probe declares.  Anthropic's endpoint
#: refuses a request that names no version, so even a route with no key sends it.
ANTHROPIC_VERSION = "2023-06-01"

def is_header_safe(value: Any) -> bool:
    """Whether *value* may be written into an HTTP header as one printable line.

    The rule itself lives in :func:`services.headers.is_header_value_safe` — this
    is a thin, model-route-shaped alias so that both ends of a route's key
    (accepted by :func:`normalize_api_key`, read back by
    :func:`effective_api_key`) and the send site (:func:`request_headers`) name
    the same predicate.  One rule, three callers: that is what keeps the
    write-side rule and the send-side rule from drifting apart.
    """
    return is_header_value_safe(value, policy=ValuePolicy.TOKEN)

#: Environment variable holding the master key every route's ``api_key`` is
#: sealed under.  Derived from :class:`config.keys.KeysConfig`, which owns the
#: variable, so the name an operator is told to set cannot drift from the name
#: that is read.
MODEL_ROUTE_KEY_ENV = KeysConfig.env_name("model_route_key")

#: What an operator sees when the master key is missing.  Deliberately
#: actionable, and deliberately fail-closed: storing a route key is refused
#: rather than silently falling back to plaintext.
MODEL_ROUTE_KEY_HELP = (
    f"{MODEL_ROUTE_KEY_ENV} 未配置：模型路由的上游密钥必须加密存储，不会以明文降级。"
    "生成并注入 backend 容器：python -c "
    "\"import secrets; print(secrets.token_urlsafe(48))\""
)

#: What ``api_key_source`` reports.  Each value names a distinct operator
#: action, which is the test a state has to pass to be worth publishing:
#:
#: ``stored``     a sealed key, opened successfully — nothing to do;
#: ``plaintext``  a row written before sealing existed — run ``model-route seal``;
#: ``unreadable`` sealed, but not under the configured key — wrong or missing
#:                ``MODEL_ROUTE_KEY``, or a corrupt row;
#: ``none``       the route needs no credential — configure one if it does.
KEY_SOURCES: tuple[str, ...] = ("stored", "plaintext", "unreadable", "none")


def api_key_sealer() -> SecretSealer:
    """The sealer for :attr:`models.model_route.ModelRoute.api_key`.

    Built per call from the settings object rather than cached in a module
    global: construction is a SHA-256 of a short string, and reading the key at
    the call site is what lets the CLI and the gates point it at their own
    deployment instead of whichever one happened to be imported first.
    """
    return SecretSealer(
        settings.keys.model_route_key,
        prefix=API_KEY_PREFIX,
        help_text=MODEL_ROUTE_KEY_HELP,
    )


class ModelRouteError(Exception):
    """Base class for the registry's domain errors."""


class RouteNotFoundError(ModelRouteError):
    """No row in the table carries the requested name."""


class DuplicateRouteError(ModelRouteError):
    """A route with that name already exists."""


# ── Value normalisation ──────────────────────────────────────────────

def canonical_provider(value: Any, default: str = "openai") -> str:
    """Best-effort canonical provider for display; never raises."""
    text = str(value or "").strip()
    if not text:
        return default
    return PROVIDER_ALIASES.get(text.lower(), text.lower())


def normalize_provider(value: Any) -> str:
    """Validate a provider for storage, raising :class:`ValueError`.

    A blank value means "the default", which is ``openai``.
    """
    provider = canonical_provider(value, default="openai")
    if provider not in PROVIDERS:
        choices = " / ".join(PROVIDERS)
        raise ValueError(f"不支持的格式 {str(value or '').strip()}，可选：{choices}")
    return provider


def default_kind(provider: Any) -> str:
    """The kind a route of this protocol gets when it does not name one."""
    return DEFAULT_KINDS.get(canonical_provider(provider), DEFAULT_KINDS["openai"])


def normalize_kind(value: Any, provider: Any) -> str:
    """Validate a route's kind for storage, raising :class:`ValueError`.

    A blank value means "the default for this protocol" (``chat``), which is what
    keeps rows written before this field existed working.
    """
    text = str(value or "").strip().lower()
    if not text:
        return default_kind(provider)
    if text not in KINDS:
        choices = " / ".join(KINDS)
        raise ValueError(f"不支持的功能 {str(value or '').strip()}，可选：{choices}")
    return text


def effective_kind(route: Mapping[str, Any]) -> str:
    """A route's kind **for publication**; never raises.

    Unlike :func:`normalize_kind` this is the read path, so a value that is
    missing or unrecognised (a row written by hand-written SQL) degrades to the
    protocol default instead of failing the whole table.
    """
    text = str(route.get("kind") or "").strip().lower()
    if text in KINDS:
        return text
    return default_kind(route.get("provider"))


def normalize_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    if len(name) > MAX_NAME:
        raise ValueError(f"名称不能超过 {MAX_NAME} 个字符")
    if name.lower() in RESERVED_NAMES:
        raise ValueError(f"名称 {name} 为系统保留，请换一个")
    if "/" in name or "\\" in name or any(ord(ch) < 32 for ch in name):
        raise ValueError("名称不能包含斜杠或控制字符")
    return name


def normalize_description(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("描述不能为空")
    if len(text) > MAX_DESCRIPTION:
        raise ValueError(f"描述不能超过 {MAX_DESCRIPTION} 个字符")
    return text


def normalize_base_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url:
        raise ValueError("URL 不能为空")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("URL 必须是 http(s)://主机[:端口][/路径] 形式")
    return url


def normalize_path(value: Any, provider: str) -> str:
    path = str(value or "").strip()
    if not path:
        return DEFAULT_PATHS.get(provider, DEFAULT_PATHS["openai"])
    return path if path.startswith("/") else "/" + path


def normalize_aliases(value: Any) -> list[str]:
    """Accept a list or a comma-separated string; drop blanks and duplicates."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        raise ValueError("别名必须是字符串或字符串数组")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def normalize_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def normalize_api_key(value: Any) -> str:
    """Validate a route's API key as one printable, space-free line.

    The key is sent as an HTTP header (``Authorization: Bearer …`` /
    ``x-api-key``), and a value carrying CR/LF would otherwise be a request-
    splitting primitive aimed at the upstream.  Header characters are therefore
    restricted here rather than at the send site, so every path that stores or
    resolves a key validates it once (``routes/hub.py`` even approves a key on
    the device-auth page without ever reading it back).

    This validates the *plaintext*, before :func:`seal_api_key` wraps it: the
    envelope is what is stored, but the header is built from what came out — and
    ``normalize_api_key`` never sees the plaintext again, which is why
    :func:`effective_api_key` re-checks it on the way out.

    The allowlist itself lives in :func:`is_header_safe`, so the rule that runs
    here and the rule that runs at the send site are one rule.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if not is_header_safe(text):
        raise ValueError("api_key 不能包含空格、制表符、控制字符或非 ASCII 字符")
    return text


def seal_api_key(value: Any) -> str:
    """Validate a plaintext key and seal it for storage.

    ``""`` (or ``None``) means "no key" and stays ``""`` — the one case that
    needs no master key, so a route without a credential can always be saved.
    Anything else fails closed: :class:`services.sealing.SealingKeyMissing` when
    no ``MODEL_ROUTE_KEY`` is configured, never a plaintext row.
    """
    text = normalize_api_key(value)
    if not text:
        return ""
    return api_key_sealer().seal(text)


def resolve_api_key(payload: Mapping[str, Any], existing: Mapping[str, Any]) -> str:
    """Apply the API key's three-state edit rule, sealing what it stores.

    An absent or ``null`` ``api_key`` keeps the stored value **verbatim** (so a
    GET that never returned the secret can still round-trip through an edit —
    and the envelope is not re-encrypted, which keeps an edit from churning a
    secret it did not change); an empty string clears it; anything else is
    validated and sealed.
    """
    if "api_key" not in payload or payload.get("api_key") is None:
        return str(existing.get("api_key") or "")
    return seal_api_key(payload.get("api_key"))


def effective_api_key(route: Mapping[str, Any]) -> tuple[str, str]:
    """The key a route authenticates with, and where it came from.

    Returns ``(plaintext_key, source)`` where *source* is one of
    :data:`KEY_SOURCES`.  The key is empty for every source except ``stored``
    and ``plaintext``.

    Reading a sealed row needs ``MODEL_ROUTE_KEY``; a missing or wrong key is
    reported as ``unreadable`` rather than raising, because one unopenable row
    must not take the whole routing table down — and reporting it is how the
    console shows "this deployment cannot decrypt the stored key" instead of
    quietly probing unauthenticated.  The legacy ``plaintext`` branch is what
    lets a row written before sealing existed keep working until
    :func:`migrate_plaintext_keys` has run, and what lets
    :func:`probe_target` authenticate an unsaved draft, whose key arrives from
    the request body and was never stored at all.

    The plaintext is re-checked with :func:`is_header_safe` on the way out, and
    an unsafe value is treated as **no key at all**.  Writing one is already
    impossible through :func:`normalize_api_key`, but *reading* one is a
    different crossing of the same boundary: this value is about to become an
    ``Authorization`` / ``x-api-key`` header, and the row it came from may
    predate the validation, or have been written straight into the database, or
    carry characters that a future writer accepted.  Dropping it here means the
    char that reaches a header is always from the allowlist, whatever the row
    holds — a rule enforced only on the write path is not enforced on the copy
    that already exists.
    """
    raw = str(route.get("api_key") or "").strip()
    if not raw:
        return "", "none"
    sealer = api_key_sealer()
    if not sealer.is_sealed(raw):
        return _safe_plaintext(raw, route, "plaintext")
    try:
        return _safe_plaintext(sealer.unseal(raw), route, "stored")
    except Exception as exc:  # noqa: BLE001
        return "", "unreadable"


def _safe_plaintext(
    plaintext: Any,
    route: Mapping[str, Any],
    source: str,
) -> tuple[str, str]:
    """*plaintext* when it may become a header value, else ``("", source)``.

    A stored-but-unsendable key is reported the same way an unopenable envelope
    is: as a route that authenticates with nothing, plus a warning naming the
    route so an operator can see which row to re-enter.
    """
    text = str(plaintext or "").strip()
    if not text:
        return "", source
    if not is_header_safe(text):
        return "", source
    return text, source


def migrate_plaintext_keys(session: Session) -> list[str]:
    """Re-seal every legacy plaintext ``api_key``; returns the names changed.

    The data half of retiring ``api_key_env``: rows written before sealing
    existed hold a bare key, and this is what closes the window in which a
    database dump still leaks them.  Idempotent — a sealed or empty value is
    left alone — so it is safe to run from a deploy step, and it is an explicit
    operator action rather than something a boot does, because it both needs the
    master key and writes secrets.
    """
    sealer = api_key_sealer()
    if not sealer.available:
        raise SealingKeyMissing(MODEL_ROUTE_KEY_HELP)
    resealed: list[str] = []
    for row in _rows(session):
        stored = str(row.api_key or "")
        if not stored or sealer.is_sealed(stored):
            continue
        row.api_key = sealer.seal(normalize_api_key(stored))
        resealed.append(row.name)
    if resealed:
        session.commit()
    return resealed


def mask_api_key(api_key: str) -> str | None:
    """A non-secret hint that a key is set — the last four characters."""
    if not api_key:
        return None
    if len(api_key) <= 4:
        return "•" * len(api_key)
    return "••••" + api_key[-4:]


def build_route(
    payload: Mapping[str, Any],
    *,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one request body into a storable route entry."""
    if not isinstance(payload, Mapping):
        raise ValueError("请求体必须是 JSON 对象")
    existing = existing or {}

    provider = normalize_provider(payload.get("provider", existing.get("provider")))
    route: dict[str, Any] = {
        "name": normalize_name(payload["name"] if "name" in payload else existing.get("name")),
        "provider": provider,
        "kind": normalize_kind(payload.get("kind", existing.get("kind")), provider),
        "base_url": normalize_base_url(payload.get("base_url", existing.get("base_url"))),
        "api_key": resolve_api_key(payload, existing),
        "model": str(payload.get("model", existing.get("model") or "") or "").strip(),
        "aliases": normalize_aliases(payload.get("aliases", existing.get("aliases"))),
        "path": normalize_path(payload.get("path", existing.get("path")), provider),
        "enabled": normalize_enabled(payload.get("enabled", existing.get("enabled", True))),
        "description": normalize_description(
            payload.get("description", existing.get("description"))
        ),
    }
    return route


def public_route(
    item: Mapping[str, Any],
    *,
    health: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One route shaped for the API — the raw API key is never included.

    ``api_key_source`` says *how* the route is authenticated without ever
    revealing what with: a ``plaintext`` row is one still awaiting
    :func:`migrate_plaintext_keys`, and an ``unreadable`` one means this
    deployment cannot open its own envelope (no ``MODEL_ROUTE_KEY``, or the
    wrong one) — both worth showing rather than rendering as a normal route.
    """
    api_key, key_source = effective_api_key(item)
    return {
        "name": item.get("name") or item.get("model") or "unnamed",
        "provider": canonical_provider(item.get("provider")),
        "kind": effective_kind(item),
        "base_url": item.get("base_url") or item.get("baseUrl") or "",
        "api_key": None,
        "has_api_key": bool(api_key),
        "api_key_hint": mask_api_key(api_key),
        "api_key_source": key_source,
        "model": item.get("model") or "",
        "aliases": normalize_aliases(item.get("aliases")),
        "path": item.get("path") or normalize_path("", canonical_provider(item.get("provider"))),
        "enabled": item.get("enabled", True) is not False,
        "description": item.get("description"),
        "health": dict(health) if health else None,
    }


# ── Row ⟷ entry ──────────────────────────────────────────────────────
# The stored entry is the mapping shape the validators above speak.  It is built
# in exactly one place so no caller can accidentally hand a half-populated row
# to ``public_route`` (which would report a key-bearing route as unconfigured).

def _entry(row: ModelRoute) -> dict[str, Any]:
    """One row as the stored-entry mapping — **including** the sealed key."""
    return {
        "name": row.name,
        "provider": row.provider,
        "kind": row.kind,
        "base_url": row.base_url,
        "api_key": row.api_key,
        "model": row.model,
        "aliases": row.alias_list(),
        "path": row.path,
        "enabled": bool(row.enabled),
        "description": row.description,
    }


def _apply(row: ModelRoute, entry: Mapping[str, Any]) -> None:
    """Copy a validated entry onto a row."""
    row.name = str(entry["name"])
    row.provider = str(entry["provider"])
    row.kind = str(entry["kind"])
    row.base_url = str(entry["base_url"])
    row.path = str(entry["path"])
    row.model = str(entry["model"])
    row.api_key = str(entry["api_key"])
    row.set_aliases(entry["aliases"])
    row.enabled = bool(entry["enabled"])
    row.description = str(entry["description"])


def _rows(session: Session) -> list[ModelRoute]:
    """Every route, in insertion order — the order the table displays."""
    return list(session.scalars(select(ModelRoute).order_by(ModelRoute.id)))


def _find(session: Session, name: str) -> ModelRoute | None:
    return session.scalars(select(ModelRoute).where(ModelRoute.name == name)).first()


def _payload(*, routes: list[dict[str, Any]]) -> dict[str, Any]:
    """The envelope both read views share."""
    return {
        "source": SOURCE,
        "exists": True,
        "error": None,
        "version": PAYLOAD_VERSION,
        "providers": list(PROVIDERS),
        "kinds": list(KINDS),
        "default_paths": dict(DEFAULT_PATHS),
        "routes": routes,
    }


# ── Read / create / update / delete ──────────────────────────────────

def load(session: Session) -> dict[str, Any]:
    """The API payload for the route table, with every secret masked."""
    return _payload(routes=[
        public_route(_entry(row), health=row.health_dict()) for row in _rows(session)
    ])


def raw_route(session: Session, name: str) -> dict[str, Any]:
    """One stored route **including** its sealed API key — for probing, not the API."""
    row = _find(session, name)
    if row is None:
        raise RouteNotFoundError(name)
    return _entry(row)


def resolve(session: Session) -> dict[str, Any]:
    """The route table **with** each route's API key — for a downstream client.

    :func:`load` is the browsing view: it masks every secret, which is right for
    the SPA but useless to a client that has to actually authenticate against a
    route's upstream.  This is that machine view, and it exists because the DSH
    ``enterprise-intranet`` plugin has to configure a provider from the table
    without a human copying keys around.

    The exposure is deliberate and bounded: the endpoint that serves this is
    guarded by ``model:resolve`` (seeded to the ``authenticated`` role), so an
    anonymous caller and a docs-only role can never reach it.  Each route
    carries the **decrypted** ``api_key`` (or an empty string when the route
    needs no key, or when its envelope cannot be opened — ``api_key_source``
    says which), plus the derived ``endpoint_url`` so a client does not have to
    join ``base_url`` and ``path`` itself.
    """
    routes: list[dict[str, Any]] = []
    for row in _rows(session):
        entry = _entry(row)
        api_key, key_source = effective_api_key(entry)
        route = dict(entry)
        route["provider"] = canonical_provider(entry["provider"])
        route["kind"] = effective_kind(entry)
        route["api_key"] = api_key
        route["api_key_source"] = key_source
        route["has_api_key"] = bool(api_key)
        route["aliases"] = normalize_aliases(entry["aliases"])
        route["path"] = entry["path"] or normalize_path("", route["provider"])
        route["health"] = row.health_dict()
        try:
            route["endpoint_url"] = endpoint_url(route)
        except ValueError:
            route["endpoint_url"] = ""
        routes.append(route)
    return _payload(routes=routes)


def create(session: Session, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one route and insert it, returning the stored entry."""
    entry = build_route(payload)
    if _find(session, entry["name"]) is not None:
        raise DuplicateRouteError(entry["name"])
    row = ModelRoute(name=entry["name"])
    _apply(row, entry)
    session.add(row)
    _commit_new(session, entry["name"])
    return _entry(row)


def update(
    session: Session,
    name: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace one route, addressed by its current name (so a body renames it)."""
    row = _find(session, name)
    if row is None:
        raise RouteNotFoundError(name)
    entry = build_route(payload, existing=_entry(row))
    if entry["name"] != name and _find(session, entry["name"]) is not None:
        raise DuplicateRouteError(entry["name"])
    _apply(row, entry)
    _commit_new(session, entry["name"], renamed_from=name)
    return _entry(row)


def delete(session: Session, name: str) -> dict[str, Any]:
    """Remove one route — and, with the row, its remembered probe result."""
    row = _find(session, name)
    if row is None:
        raise RouteNotFoundError(name)
    entry = _entry(row)
    session.delete(row)
    session.commit()
    return entry


def _commit_new(session: Session, name: str, *, renamed_from: str | None = None) -> None:
    """Commit an insert/update, translating a lost race into a domain error.

    The name is unique in the table, so the database — not only the
    check-then-write above — is what makes a duplicate impossible.  Which
    constraint a driver reports losing is dialect-specific, so rather than parse
    the message this re-reads the row: if the name is taken now, it was a
    duplicate; anything else re-raises untouched.
    """
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        taken = _find(session, name) is not None and name != renamed_from
        if taken:
            raise DuplicateRouteError(name) from exc
        raise


# ── Health (the last connectivity probe, stored on the route's row) ──

def record_health(session: Session, name: str, health: Mapping[str, Any]) -> None:
    """Remember one probe result.  A route that no longer exists is ignored."""
    row = _find(session, name)
    if row is None:
        return
    row.set_health(health)
    session.commit()


# ── Connectivity probe ───────────────────────────────────────────────

def endpoint_url(route: Mapping[str, Any]) -> str:
    """``base_url`` + ``path`` as one URL — the *inference* endpoint.

    This is what a downstream client posts a conversation to, and what
    ``/api/v1/models/resolved`` publishes as ``endpoint_url``.  It is deliberately
    **not** what the probe requests: a probe lists models, and no route serves a
    model listing at its inference path (see :func:`probe_url`).
    """
    base = normalize_base_url(route.get("base_url"))
    path = str(route.get("path") or "").strip()
    if not path:
        return base
    return base.rstrip("/") + (path if path.startswith("/") else "/" + path)


def probe_url(route: Mapping[str, Any]) -> str:
    """The URL a probe asks for a model list — ``{base_url}/models``.

    Listing models is the cheapest call an endpoint answers and the only one that
    both proves reachability and exercises the credential, so it is what a probe
    sends.  The OpenAI client appends ``/models`` to the ``base_url`` it is given,
    which is why a route's ``base_url`` must be the API root: a gateway that only
    serves ``/v1/models`` is registered as ``https://host/v1``.

    ``anthropic`` is the one format whose listing is not served relative to that
    root — it is ``GET {root}/v1/models`` — and its base URL is published both
    with and without the trailing ``/v1``, so that single segment is normalised
    here.  Only this listing URL is adjusted; the inference endpoint above is
    whatever the administrator configured.
    """
    base = normalize_base_url(route.get("base_url")).rstrip("/")
    if canonical_provider(route.get("provider")) == "anthropic":
        root = base[: -len("/v1")] if base.endswith("/v1") else base
        return f"{root}/v1/models"
    return f"{base}/models"


def request_headers(route: Mapping[str, Any]) -> dict[str, str]:
    """Every header a probe sends — the route's auth plus its probe label.

    Uses :func:`effective_api_key`, so a route whose key is sealed probes
    authenticated too — otherwise a perfectly good route would report ``auth``
    after every check.  ``anthropic`` authenticates with ``x-api-key`` and the API
    version; every other format with ``Authorization: Bearer``.

    Every value this returns is checked against :func:`is_header_safe`, and a key
    that fails is dropped rather than sent: an API key is an opaque secret that
    ends up in an outbound header, so the only safe shape is "a short line of
    printable ASCII" and anything else must be refused rather than passed on.  The
    dropping happens inside :func:`effective_api_key`, and the final mapping goes
    through :func:`services.headers.checked_headers`, so the mapping that leaves
    this function is known-safe by construction — this is the send site, and the
    check here is the one that actually hands the value to the HTTP client.

    The mapping is passed to the client as ``default_headers`` (see
    :func:`probe_client`) rather than letting the client build its own
    ``Authorization``, so the credential is assembled in exactly one place even
    though the request itself is the SDK's.
    """
    headers = {"User-Agent": _PROBE_USER_AGENT, "Accept": "*/*"}
    api_key, _source = effective_api_key(route)
    if api_key and is_header_safe(api_key):
        if canonical_provider(route.get("provider")) == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    return checked_headers(headers, context="model route probe")


def probe_client(
    route: Mapping[str, Any],
    *,
    url: str,
    timeout: float,
) -> tuple[openai.OpenAI, dict[str, Any]]:
    """The client one probe speaks through, plus its per-request header overrides.

    Returns ``(client, extra_headers)``.  *url* is the listing URL
    :func:`probe_url` derived, and the client's ``base_url`` is that URL minus its
    ``models`` segment because the SDK joins the two itself — so the URL the
    health payload reports is by construction the URL that was requested.

    Three deliberate settings:

    * the credential is **not** handed to the SDK as ``api_key``.  ``Authorization``
      travels as a per-request override instead: :func:`request_headers` still
      builds it (the one place a route becomes headers), and a route with no key
      passes ``Omit`` — which is what makes the SDK send no ``Authorization`` at
      all.  ``anthropic`` needs exactly that, since it authenticates with
      ``x-api-key`` and its ``Bearer`` would be an unknown header beside it.  An
      empty ``api_key`` also keeps the SDK from adding a header of its own, and
      ``_enforce_credentials=False`` is required because it otherwise refuses to
      build a credential-less client — a legitimate route here;
    * ``http_client`` keeps ``follow_redirects`` off.  A redirect would move the
      probe to a host :func:`services.urlsafety.check_outbound_url` never saw;
    * ``max_retries=0``: a probe is one attempt with the timeout the panel asked
      for, and the SDK's retry budget would multiply it and report a latency
      nobody requested.
    """
    headers = request_headers(route)
    extra_headers = {"Authorization": headers.pop("Authorization", openai.Omit())}
    client = openai.OpenAI(
        api_key="",
        _enforce_credentials=False,
        base_url=url[: -len("models")],
        default_headers=headers,
        timeout=timeout,
        max_retries=0,
        http_client=openai.DefaultHttpxClient(follow_redirects=False),
    )
    return client, extra_headers


def _classify(status_code: int) -> str:
    if 200 <= status_code < 400:
        return "ok"
    if status_code in (401, 403):
        return "auth"
    if status_code == 404:
        return "not_found"
    if status_code == 405:
        return "method"
    if 400 <= status_code < 500:
        return "client_error"
    return "server_error"


def _checked_at() -> str:
    return utc_now_iso()


def _unreachable(url: str, error: str, latency_ms: int | None = None) -> dict[str, Any]:
    return {
        "reachable": False,
        "ok": False,
        "status": "unreachable",
        "http_status": None,
        "latency_ms": latency_ms,
        "url": url,
        "error": error,
        "checked_at": _checked_at(),
    }


def _reachable(url: str, status_code: int, latency_ms: int) -> dict[str, Any]:
    """One HTTP answer, classified — the payload both probe outcomes share."""
    status = _classify(status_code)
    return {
        "reachable": True,
        "ok": status == "ok",
        "status": status,
        "http_status": status_code,
        "latency_ms": latency_ms,
        "url": url,
        "error": None,
        "checked_at": _checked_at(),
    }


def probe(route: Mapping[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    """Ask a route's endpoint for its model list — no inference request is sent.

    The request is the OpenAI client's own model listing (``GET {base}/models``,
    or ``GET {root}/v1/models`` for ``anthropic`` — see :func:`probe_url`), which
    is the cheapest call an endpoint answers and the one that exercises the
    credential as well as the connection: a key the upstream rejects comes back as
    ``auth`` instead of as a healthy route.

    Any HTTP response proves the endpoint is reachable; the ``status`` field says
    what came back (``ok``, ``auth``, ``method``, ``not_found``, …), so an
    endpoint that answers ``405`` to a listing is still reported as reachable
    rather than as a failure.
    """
    try:
        url = probe_url(route)
    except ValueError as exc:
        return _unreachable("", str(exc))

    # The probe runs on the server, so the URL is a request-forgery sink: the
    # guard rejects non-http(s) schemes, embedded credentials and the
    # link-local / cloud-metadata ranges (see services/urlsafety.py).  A failure
    # is reported like any other unreachable endpoint instead of raising, so the
    # panel shows the reason rather than a 500.
    try:
        url = check_outbound_url(url, allowed_hosts=settings.hub.model_probe_allowed_hosts)
    except UnsafeUrlError as exc:
        return _unreachable(url, str(exc))

    try:
        client, extra_headers = probe_client(route, url=url, timeout=timeout)
    except (UnsafeHeaderError, ValueError, TypeError) as exc:
        # A key that cannot become a header value is reported like any other
        # unusable route: the panel shows why, and nothing is sent.  ``ValueError``
        # / ``TypeError`` are the client's own refusals (a base URL or an option it
        # will not accept), which must not surface as a 500 either.
        return _unreachable(url, str(exc))

    started = time.monotonic()
    try:
        # The raw response, not the parsed page: a probe asserts what the endpoint
        # answered, so a gateway whose listing is not OpenAI-shaped (an enriched
        # ``models`` map, say) is reachable rather than a failure.
        response = client.models.with_raw_response.list(extra_headers=extra_headers)
    except openai.APIStatusError as exc:
        # Every non-2xx — including the 3xx the client refuses to follow — arrives
        # here, and the status is the answer the probe reports.
        latency = int((time.monotonic() - started) * 1000)
        return _reachable(url, exc.status_code, latency)
    except openai.OpenAIError as exc:
        latency = int((time.monotonic() - started) * 1000)
        message = str(exc).strip() or exc.__class__.__name__
        return _unreachable(url, message[:300], latency)
    else:
        latency = int((time.monotonic() - started) * 1000)
        # A non-streaming response has been read by the client already, so its
        # connection goes back to the pool and ``client.close()`` below releases
        # it; the status is all a probe keeps from the answer.
        status_code = response.status_code
    finally:
        client.close()

    return _reachable(url, status_code, latency)


def probe_target(payload: Mapping[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    """Probe an unsaved draft — only ``base_url`` (and friends) are required.

    The draft's ``api_key`` arrives as **plaintext** from the panel (it was
    never stored, so it cannot be sealed), which is exactly the shape
    :func:`effective_api_key` serves from its legacy branch.  It is validated
    here like any other key: this value goes into a request header, and a
    CR/LF-bearing one would otherwise be request splitting aimed at the
    upstream — the same rule the stored path applies before sealing.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("请求体必须是 JSON 对象")
    provider = canonical_provider(payload.get("provider"))
    route = {
        "provider": provider,
        "base_url": normalize_base_url(payload.get("base_url")),
        "api_key": normalize_api_key(payload.get("api_key")),
        "path": normalize_path(payload.get("path"), provider),
    }
    return probe(route, timeout=timeout)


def probe_and_record(
    session: Session,
    name: str,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Probe one saved route (with its key) and remember the result."""
    health = probe(raw_route(session, name), timeout=timeout)
    record_health(session, name, health)
    return health


__all__ = [
    "ANTHROPIC_VERSION",
    "DEFAULT_KINDS",
    "DEFAULT_PATHS",
    "KINDS",
    "KEY_SOURCES",
    "MAX_DESCRIPTION",
    "MAX_NAME",
    "MODEL_ROUTE_KEY_ENV",
    "MODEL_ROUTE_KEY_HELP",
    "PAYLOAD_VERSION",
    "PROVIDERS",
    "PROVIDER_ALIASES",
    "RESERVED_NAMES",
    "SOURCE",
    "DuplicateRouteError",
    "ModelRouteError",
    "RouteNotFoundError",
    "api_key_sealer",
    "build_route",
    "canonical_provider",
    "create",
    "default_kind",
    "delete",
    "effective_api_key",
    "effective_kind",
    "endpoint_url",
    "is_header_safe",
    "load",
    "mask_api_key",
    "migrate_plaintext_keys",
    "normalize_aliases",
    "normalize_api_key",
    "normalize_base_url",
    "normalize_description",
    "normalize_enabled",
    "normalize_kind",
    "normalize_name",
    "normalize_path",
    "normalize_provider",
    "probe",
    "probe_and_record",
    "probe_client",
    "probe_target",
    "probe_url",
    "public_route",
    "raw_route",
    "record_health",
    "request_headers",
    "resolve",
    "resolve_api_key",
    "seal_api_key",
    "update",
]
