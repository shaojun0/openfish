"""The model-routing registry — read, edit and probe the routes DSH talks to.

``MODELS_FILE`` (default ``config/model_routes.json``) is the table a downstream
intranet DSH reads to decide which model endpoints exist.  This module owns the
whole lifecycle of that document:

* **Read** — :func:`load` returns the API-shaped payload (never the raw API
  keys) the SPA renders.
* **Write** — :func:`create`, :func:`update` and :func:`delete` validate one
  route, rewrite the JSON document atomically and keep the separate health
  store in step.  The document is the source of truth; there is no database.
* **Probe** — :func:`probe` performs one connectivity check against a route's
  URL and :func:`record_health` remembers the answer.  The published route
  table stays a pure route table: health lives in ``MODEL_HEALTH_FILE``.

Layout::

    config/model_routes.json      # what downstream DSH reads (route + api_key)
    data/model_health.json        # last probe per route name (not published)

Every route carries a ``provider`` naming the wire format — ``openai``,
``mineru`` or ``anthropic`` — plus the ``base_url``, an optional ``api_key``,
the endpoint ``path``, an optional ``model`` id and display ``aliases``.  A
route's ``name`` and ``description`` are mandatory; the API key may be empty.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

from services.fileio import read_json, write_json
from services.format import utc_now_iso

logger = logging.getLogger("cpypiserver.model_routes")

#: Wire formats a route may speak.  The values are the canonical spellings the
#: UI select offers and the JSON document stores.
PROVIDERS: tuple[str, ...] = ("openai", "mineru", "anthropic")

#: Spellings seen in hand-edited files that map onto a canonical provider.
PROVIDER_ALIASES: dict[str, str] = {
    "openai": "openai",
    "openai-compatible": "openai",
    "openai_compatible": "openai",
    "openai_compat": "openai",
    "mineru": "mineru",
    "miner": "mineru",
    "anthropic": "anthropic",
    "claude": "anthropic",
}

#: Endpoint used when a route does not name one itself.
DEFAULT_PATHS: dict[str, str] = {
    "openai": "/v1/chat/completions",
    "mineru": "/file_parse",
    "anthropic": "/v1/messages",
}

#: Route names that would collide with the probe endpoint's own path.
RESERVED_NAMES: frozenset[str] = frozenset({"probe"})

#: A probe is a liveness check, not an inference call.
_PROBE_USER_AGENT = "cpypiserver-model-probe/1.0"

#: An ``api_key_env`` is an environment-variable *name*, so it must be a POSIX
#: shell identifier.  Enforced so a value pasted into the field is rejected at
#: write time instead of silently never resolving.
_API_KEY_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: ``name``/``description`` ceilings — generous, but bounded so one edit cannot
#: balloon the published document.
MAX_NAME = 128
MAX_DESCRIPTION = 500

#: Serialises read-modify-write cycles within this process.  The shipped
#: container runs a single process (see README), which is what this protects;
#: cross-process writers would still need file locking.
_lock = threading.RLock()


class ModelRouteError(Exception):
    """Base class for the registry's domain errors."""


class RouteNotFoundError(ModelRouteError):
    """No route in the document carries the requested name."""


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
        raise ValueError(f"不支持的格式 {str(value or '').strip()!r}，可选：{choices}")
    return provider


def normalize_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    if len(name) > MAX_NAME:
        raise ValueError(f"名称不能超过 {MAX_NAME} 个字符")
    if name.lower() in RESERVED_NAMES:
        raise ValueError(f"名称 {name!r} 为系统保留，请换一个")
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


def resolve_api_key(payload: Mapping[str, Any], existing: Mapping[str, Any]) -> str:
    """Apply the API key's three-state edit rule.

    An absent or ``null`` ``api_key`` keeps the stored key (so a GET that never
    returned the secret can still round-trip through an edit); an empty string
    clears it; anything else replaces it.
    """
    if "api_key" not in payload or payload.get("api_key") is None:
        return str(existing.get("api_key") or "")
    return str(payload.get("api_key") or "").strip()


def normalize_api_key_env(value: Any) -> str:
    """Validate an environment-variable *name* a route's key is read from.

    The whole point of ``api_key_env`` is that the route document can be
    committed and shared while the secret stays in the process environment —
    so this field holds a name, never a value, and is validated as one.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if not _API_KEY_ENV_RE.match(text):
        raise ValueError(
            f"api_key_env 必须是合法的环境变量名（[A-Za-z_][A-Za-z0-9_]*），得到 {text!r}"
        )
    return text


def resolve_api_key_env(payload: Mapping[str, Any], existing: Mapping[str, Any]) -> str:
    """Three-state edit rule for ``api_key_env``, mirroring :func:`resolve_api_key`."""
    if "api_key_env" not in payload or payload.get("api_key_env") is None:
        return str(existing.get("api_key_env") or "")
    return normalize_api_key_env(payload.get("api_key_env"))


def effective_api_key(route: Mapping[str, Any]) -> tuple[str, str]:
    """The key a route authenticates with, and where it came from.

    A stored ``api_key`` wins; otherwise an ``api_key_env`` naming a non-empty
    environment variable is used.  The second element is a non-secret label for
    configuration surfaces: ``stored``, ``env``, ``env-missing`` (the name is
    set but the variable is empty/absent — a deployment mistake worth showing
    rather than silently sending an unauthenticated probe) or ``none``.
    """
    stored = str(route.get("api_key") or "").strip()
    if stored:
        return stored, "stored"
    env_name = str(route.get("api_key_env") or "").strip()
    if not env_name:
        return "", "none"
    value = os.environ.get(env_name, "").strip()
    return (value, "env") if value else ("", "env-missing")


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
        "base_url": normalize_base_url(payload.get("base_url", existing.get("base_url"))),
        "api_key": resolve_api_key(payload, existing),
        "api_key_env": resolve_api_key_env(payload, existing),
        "model": str(payload.get("model", existing.get("model") or "") or "").strip(),
        "aliases": normalize_aliases(payload.get("aliases", existing.get("aliases"))),
        "path": normalize_path(payload.get("path", existing.get("path")), provider),
        "enabled": normalize_enabled(payload.get("enabled", existing.get("enabled", True))),
        "description": normalize_description(
            payload.get("description", existing.get("description"))
        ),
    }
    tags = payload.get("tags", existing.get("tags"))
    if tags:
        route["tags"] = normalize_aliases(tags)
    return route


def public_route(item: Mapping[str, Any], *, health: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One route shaped for the API — the raw API key is never included.

    ``api_key_env`` names the variable an injected key is read from, so the
    console can show *how* a route is authenticated (and flags
    ``env-missing`` when the name resolves to nothing) without ever seeing a
    value.
    """
    api_key, key_source = effective_api_key(item)
    return {
        "name": item.get("name") or item.get("model") or "unnamed",
        "provider": canonical_provider(item.get("provider")),
        "base_url": item.get("base_url") or item.get("baseUrl") or "",
        "api_key": None,
        "has_api_key": bool(api_key),
        "api_key_hint": mask_api_key(api_key),
        "api_key_env": str(item.get("api_key_env") or ""),
        "api_key_source": key_source,
        "model": item.get("model") or "",
        "aliases": normalize_aliases(item.get("aliases")),
        "path": item.get("path") or normalize_path("", canonical_provider(item.get("provider"))),
        "enabled": item.get("enabled", True) is not False,
        "description": item.get("description"),
        "tags": normalize_aliases(item.get("tags")),
        "health": dict(health) if health else None,
    }


# ── Document I/O ─────────────────────────────────────────────────────

def _read_document(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(document, routes)``, preserving unknown top-level keys.

    Raises :class:`ValueError` when the file exists but is not a usable route
    document, so a write never silently discards a hand-edited file.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return {}, []
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法解析模型路由文件 {file_path}：{exc}") from exc

    if isinstance(data, dict):
        document: dict[str, Any] = data
        raw = data.get("routes")
    elif isinstance(data, list):
        document = {}
        raw = data
    else:
        raise ValueError("模型路由文件必须是一个 JSON 对象或数组")

    if raw is None:
        return document, []
    if not isinstance(raw, list):
        raise ValueError("模型路由文件中的 routes 必须是数组")
    routes = [item for item in raw if isinstance(item, dict)]
    return document, routes


def _write_document(
    path: str | Path,
    document: Mapping[str, Any],
    routes: list[dict[str, Any]],
) -> None:
    payload = dict(document) if isinstance(document, Mapping) else {}
    payload.setdefault("version", 1)
    payload["routes"] = routes
    write_json(Path(path), payload)


def _index_of(routes: list[dict[str, Any]], name: str) -> int | None:
    for index, item in enumerate(routes):
        if (item.get("name") or item.get("model")) == name:
            return index
    return None


# ── Health store ─────────────────────────────────────────────────────

def load_health(path: str | Path | None) -> dict[str, dict[str, Any]]:
    """Read the remembered probe results; a missing or broken file is empty."""
    if not path:
        return {}
    file_path = Path(path)
    data = read_json(file_path, default={})
    raw = data.get("routes") if isinstance(data, dict) and "routes" in data else data
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): value
        for name, value in raw.items()
        if isinstance(value, dict)
    }


def _save_health(path: str | Path, mapping: Mapping[str, Any]) -> None:
    write_json(Path(path), {"routes": mapping})


def record_health(path: str | Path | None, name: str, health: Mapping[str, Any]) -> None:
    if not path:
        return
    with _lock:
        mapping = load_health(path)
        mapping[name] = dict(health)
        _save_health(path, mapping)


def forget_health(path: str | Path | None, *names: str) -> None:
    if not path or not names:
        return
    with _lock:
        mapping = load_health(path)
        changed = False
        for name in names:
            if name in mapping:
                mapping.pop(name, None)
                changed = True
        if changed:
            _save_health(path, mapping)


# ── Read / create / update / delete ──────────────────────────────────

def load(path: str | Path, *, health_path: str | Path | None = None) -> dict[str, Any]:
    """The API payload for the route table; a missing file is not an error."""
    file_path = Path(path)
    if not file_path.is_file():
        return {
            "source": str(file_path),
            "exists": False,
            "error": None,
            "providers": list(PROVIDERS),
            "default_paths": dict(DEFAULT_PATHS),
            "routes": [],
        }

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("cannot read model routes from %s: %s", file_path, exc)
        return {
            "source": str(file_path),
            "exists": True,
            "error": str(exc),
            "providers": list(PROVIDERS),
            "default_paths": dict(DEFAULT_PATHS),
            "routes": [],
        }

    raw = data.get("routes") if isinstance(data, dict) else data
    health = load_health(health_path)
    routes: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model") or "unnamed"
            routes.append(public_route(item, health=health.get(str(name))))

    return {
        "source": str(file_path),
        "exists": True,
        "error": None,
        "version": data.get("version") if isinstance(data, dict) else None,
        "providers": list(PROVIDERS),
        "default_paths": dict(DEFAULT_PATHS),
        "routes": routes,
    }


def raw_route(path: str | Path, name: str) -> dict[str, Any]:
    """One stored route **including** its API key — for probing, not the API."""
    _document, routes = _read_document(path)
    index = _index_of(routes, name)
    if index is None:
        raise RouteNotFoundError(name)
    return routes[index]


def resolve(path: str | Path, *, health_path: str | Path | None = None) -> dict[str, Any]:
    """The route table **with** each route's API key — for a downstream client.

    :func:`load` is the browsing view: it masks every secret, which is right for
    the SPA but useless to a client that has to actually authenticate against a
    route's upstream.  This is that machine view, and it exists because the DSH
    ``enterprise-intranet`` plugin has to configure a provider from the table
    without a human copying keys around.

    The exposure is deliberate and bounded: the endpoint that serves this is
    guarded by ``model:resolve`` (seeded to the ``authenticated`` role), so an
    anonymous caller and a docs-only role can never reach it.  Each route is
    returned exactly as stored, with ``api_key`` set to the stored value (or an
    empty string when the route needs no key), plus the derived ``endpoint_url``
    so a client does not have to join ``base_url`` and ``path`` itself.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return {
            "source": str(file_path),
            "exists": False,
            "error": None,
            "providers": list(PROVIDERS),
            "default_paths": dict(DEFAULT_PATHS),
            "routes": [],
        }

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("cannot read model routes from %s: %s", file_path, exc)
        return {
            "source": str(file_path),
            "exists": True,
            "error": str(exc),
            "providers": list(PROVIDERS),
            "default_paths": dict(DEFAULT_PATHS),
            "routes": [],
        }

    raw = data.get("routes") if isinstance(data, dict) else data
    health = load_health(health_path)
    routes: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("model") or "unnamed")
            route = dict(item)
            route["name"] = name
            route["provider"] = canonical_provider(item.get("provider"))
            # The value may be stored inline OR injected via the environment;
            # the client only cares that it authenticates, so hand it the
            # resolved value and a non-secret label saying where it came from.
            api_key, key_source = effective_api_key(item)
            route["api_key"] = api_key
            route["api_key_env"] = str(item.get("api_key_env") or "")
            route["api_key_source"] = key_source
            route["has_api_key"] = bool(api_key)
            route["model"] = str(item.get("model") or "")
            route["aliases"] = normalize_aliases(item.get("aliases"))
            route["path"] = item.get("path") or normalize_path("", route["provider"])
            route["enabled"] = item.get("enabled", True) is not False
            route["health"] = dict(health[name]) if name in health else None
            try:
                route["endpoint_url"] = endpoint_url(route)
            except ValueError:
                route["endpoint_url"] = ""
            routes.append(route)

    return {
        "source": str(file_path),
        "exists": True,
        "error": None,
        "version": data.get("version") if isinstance(data, dict) else None,
        "providers": list(PROVIDERS),
        "default_paths": dict(DEFAULT_PATHS),
        "routes": routes,
    }


def create(path: str | Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    with _lock:
        document, routes = _read_document(path)
        route = build_route(payload)
        if _index_of(routes, route["name"]) is not None:
            raise DuplicateRouteError(route["name"])
        routes.append(route)
        _write_document(path, document, routes)
    return route


def update(path: str | Path, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    with _lock:
        document, routes = _read_document(path)
        index = _index_of(routes, name)
        if index is None:
            raise RouteNotFoundError(name)
        route = build_route(payload, existing=routes[index])
        if route["name"] != name and _index_of(routes, route["name"]) is not None:
            raise DuplicateRouteError(route["name"])
        routes[index] = route
        _write_document(path, document, routes)
    return route


def delete(path: str | Path, name: str) -> dict[str, Any]:
    with _lock:
        document, routes = _read_document(path)
        index = _index_of(routes, name)
        if index is None:
            raise RouteNotFoundError(name)
        removed = routes.pop(index)
        _write_document(path, document, routes)
    return removed


# ── Connectivity probe ───────────────────────────────────────────────

def endpoint_url(route: Mapping[str, Any]) -> str:
    """``base_url`` + ``path`` as one probe URL."""
    base = normalize_base_url(route.get("base_url"))
    path = str(route.get("path") or "").strip()
    if not path:
        return base
    return base.rstrip("/") + (path if path.startswith("/") else "/" + path)


def request_headers(route: Mapping[str, Any]) -> dict[str, str]:
    """Auth headers for the route's provider, when a key is configured.

    Uses :func:`effective_api_key`, so a route whose key is injected through
    ``api_key_env`` probes authenticated too — otherwise a perfectly good route
    would report ``auth`` after every check.
    """
    headers = {"User-Agent": _PROBE_USER_AGENT, "Accept": "*/*"}
    api_key, _source = effective_api_key(route)
    if not api_key:
        return headers
    if canonical_provider(route.get("provider")) == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


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


def probe(route: Mapping[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    """Check that a route's URL answers — no inference request is sent.

    Any HTTP response proves the endpoint is reachable; the ``status`` field
    says what came back (``ok``, ``auth``, ``method``, ``not_found``, …), so a
    POST-only endpoint that answers ``405`` to our ``GET`` is still reported as
    reachable rather than as a failure.
    """
    try:
        url = endpoint_url(route)
    except ValueError as exc:
        return _unreachable("", str(exc))

    headers = request_headers(route)
    started = time.monotonic()
    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        latency = int((time.monotonic() - started) * 1000)
        message = str(exc).strip() or exc.__class__.__name__
        return _unreachable(url, message[:300], latency)

    latency = int((time.monotonic() - started) * 1000)
    status = _classify(response.status_code)
    return {
        "reachable": True,
        "ok": status == "ok",
        "status": status,
        "http_status": response.status_code,
        "latency_ms": latency,
        "url": url,
        "error": None,
        "checked_at": _checked_at(),
    }


def probe_target(payload: Mapping[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    """Probe an unsaved draft — only ``base_url`` (and friends) are required."""
    if not isinstance(payload, Mapping):
        raise ValueError("请求体必须是 JSON 对象")
    provider = canonical_provider(payload.get("provider"))
    route = {
        "provider": provider,
        "base_url": normalize_base_url(payload.get("base_url")),
        "api_key": str(payload.get("api_key") or "").strip(),
        "path": normalize_path(payload.get("path"), provider),
    }
    return probe(route, timeout=timeout)


def probe_and_record(
    path: str | Path,
    name: str,
    *,
    health_path: str | Path | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Probe one saved route (with its key) and remember the result."""
    health = probe(raw_route(path, name), timeout=timeout)
    record_health(health_path, name, health)
    return health


__all__ = [
    "PROVIDERS",
    "DEFAULT_PATHS",
    "RESERVED_NAMES",
    "ModelRouteError",
    "RouteNotFoundError",
    "DuplicateRouteError",
    "canonical_provider",
    "normalize_provider",
    "normalize_name",
    "normalize_description",
    "normalize_base_url",
    "normalize_path",
    "normalize_aliases",
    "normalize_enabled",
    "resolve_api_key",
    "resolve_api_key_env",
    "normalize_api_key_env",
    "effective_api_key",
    "mask_api_key",
    "build_route",
    "public_route",
    "load",
    "resolve",
    "raw_route",
    "create",
    "update",
    "delete",
    "load_health",
    "record_health",
    "forget_health",
    "endpoint_url",
    "request_headers",
    "probe",
    "probe_target",
    "probe_and_record",
]
