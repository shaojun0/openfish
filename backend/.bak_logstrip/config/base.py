"""Shared base for every configuration group.

One group per domain (``server``, ``storage``, ``agent``, ``forgejo``, …), each
a :class:`pydantic_settings.BaseSettings` subclass that reads ``.env`` and the
process environment once, when :data:`config.settings` is built at start-up.

Two rules the whole tree depends on, implemented here so no group re-implements
them:

* **An empty variable means "unset".**  ``docker-compose.yml`` injects every
  knob it knows about, including the ones an operator left blank
  (``AGENT_PR_POLICY=``, ``AGENT_AUTO_FIX=``); a blank would otherwise be parsed
  as a value and rejected for an ``int``/``bool``/``list`` field.  Blank values
  are dropped before validation, so the declared default applies — which is what
  the previous hand-written ``os.environ.get(name) or default`` idiom meant.

* **The variable name is derived, never re-typed.**  :meth:`EnvSettings.env_name`
  answers "which environment variable is this field?" from the group's
  ``env_prefix`` (or an explicit alias).  Error messages, log lines and every
  consumer ask the model instead of hard-coding a second copy of the name, so a
  renamed field cannot silently stop being read.
"""

from __future__ import annotations

from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    """One domain's configuration, read from ``.env`` plus the environment.

    Subclasses declare plain fields; a flat environment name is matched by the
    upper-cased field name, or by the group's ``env_prefix`` when one is set::

        class AgentConfig(EnvSettings):
            model_config = SettingsConfigDict(env_prefix="AGENT_")
            work_root: str = "/work"          # ← AGENT_WORK_ROOT

    Values are read once, at construction.  Nothing in the tree re-reads the
    environment later: a configuration a running process can be steered into
    after start-up is an injection surface, not a feature.  Tests that need a
    second configuration build a second object (or set the field) rather than
    mutating ``os.environ``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_blank_values(cls, data: Any) -> Any:
        """Treat a blank string as "not set" so the field default applies."""
        if not isinstance(data, dict):
            return data
        return {
            key: value
            for key, value in data.items()
            if not (isinstance(value, str) and not value.strip())
        }

    @classmethod
    def env_name(cls, field: str) -> str:
        """The environment variable *field* is read from.

        The one place that answers this question.  ``env_prefix`` is honoured,
        as is an explicit (validation) alias, so a group may spell its variables
        however the deployment already does.
        """
        info = cls.model_fields.get(field)
        if info is not None:
            alias = info.validation_alias or info.alias
            if isinstance(alias, str) and alias:
                return alias.upper()
        prefix = str(cls.model_config.get("env_prefix") or "")
        return f"{prefix}{field}".upper()


__all__ = ["EnvSettings"]
