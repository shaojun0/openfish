#!/usr/bin/env python
"""Gate: a blank environment variable means "unset" for every field type.

``docker/docker-compose.yml`` forwards each knob it knows about as
``${VAR:-}``, so a variable an operator left blank arrives in the container as
the empty string, not as an absent variable.  ``config/base.py`` promises that
an empty value means "unset" and the field's declared default applies.  For a
scalar that promise is kept by ``_drop_blank_values``, but for a **complex**
field (``list[str]``, ``dict``) pydantic-settings JSON-decodes the raw text
while it is collecting values, so a blank raises ``SettingsError`` *before* any
model validator runs.  That is a boot-time crash::

    ALLOW_EXTENSIONS=               -> SettingsError: allow_extensions
    MODEL_PROBE_ALLOWED_HOSTS=      -> SettingsError: model_probe_allowed_hosts

Those two are only today's examples: every complex-typed field that a future
compose line forwards with ``:-}`` has the same failure mode.  The gate
therefore walks the whole configuration tree instead of pinning two names.

Per field of every ``EnvSettings`` group, three properties:

1. removing the variable builds the group (the baseline);
2. setting it to the empty string builds the group **and yields exactly the
   baseline** — blank must behave like absent, whatever the ambient value is;
3. a real JSON value still parses to itself, so blank handling cannot swallow
   a deliberate setting: ``ALLOW_EXTENSIONS=[]`` legitimately means "no
   extension is allowed" and must stay different from blank.

Run from the repository root::

    python .agent/checks/check_blank_env_defaults.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_blank_env_defaults.py
"""

from __future__ import annotations

import os
import sys
import typing
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"

# The JSON text for the trivial value of each collection origin, paired with
# the value it has to decode back to.  ``set`` / ``tuple`` round-trip through
# ``[]`` because JSON has no syntax of its own for them.
COMPLEX_LITERALS: dict[object, tuple[str, object]] = {
    list: ("[]", []),
    tuple: ("[]", ()),
    set: ("[]", set()),
    frozenset: ("[]", frozenset()),
    dict: ("{}", {}),
}

# The tree under test has to win over any installed copy: an editable install
# of this same backend (``uv pip install -e``) is what a developer box and the
# runner image carry, and it would otherwise answer for ``config`` wherever the
# check is run from.
sys.path.insert(0, str(BACKEND_DIR))


@contextmanager
def _env(name: str, value: str | None) -> Iterator[None]:
    """Set *name* to *value* (``None`` = remove it) and restore it after."""
    saved = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


def _groups(base: type) -> list[type]:
    """Every concrete ``EnvSettings`` subclass, depth first, once each."""
    found: list[type] = []
    seen: set[type] = set()

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            if sub in seen:
                continue
            seen.add(sub)
            found.append(sub)
            walk(sub)

    walk(base)
    return found


def _is_group(annotation: object, base: type) -> bool:
    """True for a field that *contains* another group (the ``Settings`` root)."""
    return isinstance(annotation, type) and issubclass(annotation, base)


def _complex_origin(annotation: object) -> object | None:
    """The collection origin of *annotation*, seeing through ``| None``."""
    origin = typing.get_origin(annotation)
    if origin in COMPLEX_LITERALS:
        return origin
    for argument in typing.get_args(annotation):
        found = _complex_origin(argument)
        if found is not None:
            return found
    return None


def _build(cls: type, field: str) -> tuple[object | None, str | None]:
    """Construct *cls* once; on failure return ``(None, "Exception: text")``."""
    try:
        return getattr(cls(), field), None
    except Exception as exc:  # noqa: BLE001 — the failure *is* the finding
        return None, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"


def main() -> int:
    try:
        # Importing the groups is what registers them as `EnvSettings`
        # subclasses — `_groups` finds them through `__subclasses__()`.
        from pydantic_core import PydanticUndefined

        from config.agent import AgentConfig  # noqa: F401
        from config.auth import AuthConfig  # noqa: F401
        from config.base import EnvSettings
        from config.forgejo import ForgejoConfig  # noqa: F401
        from config.hub import HubConfig  # noqa: F401
        from config.keys import KeysConfig  # noqa: F401
        from config.security import SecurityConfig  # noqa: F401
        from config.server import ServerConfig  # noqa: F401
        from config.storage import StorageConfig  # noqa: F401
    except ImportError as exc:
        print(f"FAIL cannot import the configuration tree: {exc}")
        print(
            "     run it with the backend interpreter, e.g. "
            "backend/.venv/bin/python .agent/checks/check_blank_env_defaults.py"
        )
        return 1

    failures: list[str] = []
    checked = 0
    for cls in _groups(EnvSettings):
        for name, field in cls.model_fields.items():
            if _is_group(field.annotation, EnvSettings):
                continue  # `Settings` itself: its groups are checked on their own
            if field.get_default(call_default_factory=True) is PydanticUndefined:
                continue
            variable = cls.env_name(name)
            where = f"{cls.__name__}.{name} ({variable})"
            checked += 1

            with _env(variable, None):
                baseline, error = _build(cls, name)
            if error is not None:
                failures.append(f"{where}: unset value does not build — {error}")
                continue

            with _env(variable, ""):
                blank, error = _build(cls, name)
            if error is not None:
                failures.append(
                    f"{where}: blank value crashes the settings source — {error}"
                )
            elif blank != baseline:
                failures.append(
                    f"{where}: blank means {blank!r}, unset means {baseline!r}"
                )

            literal = COMPLEX_LITERALS.get(_complex_origin(field.annotation))
            if literal is not None:
                text, expected = literal
                with _env(variable, text):
                    parsed, error = _build(cls, name)
                if error is not None:
                    failures.append(f"{where}: literal {text} does not parse — {error}")
                elif parsed != expected:
                    failures.append(f"{where}: {text} parsed as {parsed!r}")

    for line in failures:
        print(f"FAIL {line}")
    print(
        f"check_blank_env_defaults: {checked} fields, {len(failures)} failure(s)",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
