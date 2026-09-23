"""Application configuration — nested domain models, one group per concern.

Usage::

    from config import settings
    print(settings.server.port)
    print(settings.auth.basic_username)
    print(settings.storage.packages_dir)
    print(settings.security.clamav_host)
    print(settings.agent.work_root)
    print(settings.forgejo.forgejo_base_url)

Environment variables use ``__`` as delimiter::

    SERVER__HOST=0.0.0.0
    AUTH__BASIC_USERNAME=admin
    STORAGE__PACKAGES_DIR=/data/packages

Every environment variable the backend reads is declared here — there is no
second reader.  A flat name is matched by the upper-cased field name
(``SERVER_NAME``, ``AGENT_WORK_ROOT``, ``FORGEJO_BASE_URL``), which is what the
deployment already speaks, and the ``GROUP__FIELD`` form works as well.  Values
are read **once**, when :data:`settings` is built; see
:class:`config.base.EnvSettings` for why nothing re-reads the environment later.

What deliberately does not live here: variable names that are not configuration
but a message between two of our own processes — the credential helper's
``OPENFISH_GIT_TOKEN``/``OPENFISH_GIT_HOST``, the model block's
``OPENFISH_MODEL_*`` prefix, the review child's ``OPENFISH_TASK_KIND``, and the
cache location the GuardDog library is told about.  Those are declared next to
the code that sets them, because their meaning is "who is this child", not "how
is this deployment configured".
"""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict

from config.agent import AgentConfig
from config.auth import AuthConfig
from config.base import EnvSettings
from config.forgejo import ForgejoConfig
from config.hub import HubConfig
from config.keys import KeysConfig
from config.security import SecurityConfig
from config.server import ServerConfig
from config.storage import StorageConfig


class Settings(EnvSettings):
    """The whole configuration, as one object, with ``GROUP__FIELD`` nesting."""

    model_config = SettingsConfigDict(env_nested_delimiter="__")

    server: ServerConfig = ServerConfig()
    storage: StorageConfig = StorageConfig()
    auth: AuthConfig = AuthConfig()
    security: SecurityConfig = SecurityConfig()
    hub: HubConfig = HubConfig()
    agent: AgentConfig = AgentConfig()
    forgejo: ForgejoConfig = ForgejoConfig()
    keys: KeysConfig = KeysConfig()

    # ── Convenience aliases (backward-compat with Flask config) ─────
    @property
    def debug(self) -> bool:
        return self.server.debug

    @property
    def secret_key(self) -> str:
        return self.server.secret_key

    @property
    def max_content_length(self) -> int:
        return self.storage.max_content_length

    @property
    def packages_dir(self) -> str:
        return self.storage.packages_dir


settings = Settings()

__all__ = [
    "AgentConfig",
    "AuthConfig",
    "EnvSettings",
    "ForgejoConfig",
    "HubConfig",
    "KeysConfig",
    "SecurityConfig",
    "ServerConfig",
    "Settings",
    "StorageConfig",
    "settings",
]
