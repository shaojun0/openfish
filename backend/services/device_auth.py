"""Device authorization — hand a browser-less client an API key after login.

This is an RFC 8628-shaped flow, trimmed to what one client actually needs: the
DSH ``enterprise-intranet`` plugin runs inside a container with no browser and
no ability to copy a secret out of the web console, so it asks *this* server to
mint a key on its behalf once a human has signed in.

The three-legged flow::

    POST /api/v1/device/code          (anonymous)
        -> {device_code, user_code, verification_uri, expires_in, interval}

    GET  /device?user_code=XXXX-XXXX  (browser; redirects to the login flow
                                       when the visitor has no session yet)
    POST /device/approve              (signed in; needs key:create)
        -> mints an API key for that account and marks the request approved

    POST /api/v1/device/token         (anonymous, polled by the plugin)
        -> 400 authorization_pending   while nobody has approved
        -> 200 {api_key, ...}          exactly once, then the request is gone

Design notes
------------
* **The device code is the only bearer secret in the exchange.** It is 32 random
  bytes, returned once in the ``code`` response, and stored SHA-256-hashed — a
  leaked ``device_codes.json`` therefore exposes no usable credential.
* **``user_code`` is not a secret.** It only names a pending request so a human
  can confirm it, and it is single-use: approving it consumes it.
* **State lives in a JSON document**, not in memory, so a restart (or the
  gunicorn worker recycle the deployment does on every upgrade) between the
  plugin's ``code`` request and the human's approval does not strand the user.
  The shipped container runs a single process, which is what the in-process
  lock below protects.
* **The flow never widens a caller's rights.** A key minted here carries the
  exact grants of the account that approved it, because that is how
  :class:`auth.api_keys.ApiKeyManager` works — the approver's ``key:create`` is
  checked by the route guard before this module is reached.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping


#: Characters for ``user_code``.  No vowels (so a code cannot spell a word), no
#: ``0/O/1/I`` lookalikes: a code is read off a screen and typed back.
_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"

#: ``user_code`` half-length; the rendered form is ``XXXX-XXXX``.
_CODE_LEN = 4

#: How often the plugin is told to poll, in seconds.
POLL_INTERVAL = 2

#: SHA-256 of the device code is the storage key; the raw code never hits disk.
def _hash(device_code: str) -> str:
    return hashlib.sha256(device_code.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


def _new_user_code() -> str:
    pick = lambda: "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    return f"{pick()}-{pick()}"


def _normalize_user_code(value: Any) -> str:
    """Accept ``abcd-efgh`` casing/spacing variants; return the canonical form."""
    text = str(value or "").strip().upper().replace(" ", "").replace("_", "-")
    if "-" not in text and len(text) == _CODE_LEN * 2:
        text = f"{text[:_CODE_LEN]}-{text[_CODE_LEN:]}"
    return text


class DeviceFlowError(Exception):
    """Base class for the flow's domain errors."""


class UnknownUserCodeError(DeviceFlowError):
    """No pending request carries that ``user_code``."""


class ExpiredUserCodeError(DeviceFlowError):
    """The request existed but its window has closed."""


class AlreadyApprovedError(DeviceFlowError):
    """That ``user_code`` was already used to approve a request."""


class DeviceAuthStore:
    """File-backed store for pending device-authorization requests."""

    def __init__(self, path: str | Path, *, ttl: int = 900) -> None:
        self._path = Path(path)
        self._ttl = int(ttl)
        self._lock = threading.RLock()

    # ── Document I/O ────────────────────────────────────────────────

    def _read(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {}
        raw = data.get("requests") if isinstance(data, dict) else None
        return raw if isinstance(raw, dict) else {}

    def _write(self, requests: Mapping[str, Any]) -> None:
        """Atomically replace the document (temp file + ``os.replace``)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "requests": dict(requests)}
        blob = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-device-", dir=self._path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
            os.replace(tmp_name, self._path)
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    def _purge(self, requests: dict[str, Any]) -> int:
        """Drop expired entries in place; returns how many were removed."""
        cutoff = _now()
        dead = [
            digest for digest, item in requests.items()
            if not isinstance(item, dict) or float(item.get("expires_at") or 0) < cutoff
        ]
        for digest in dead:
            requests.pop(digest, None)
        return len(dead)

    # ── Flow ────────────────────────────────────────────────────────

    def create(self, *, base_url: str) -> dict[str, Any]:
        """Start a request and return the plugin-facing payload."""
        device_code = secrets.token_urlsafe(32)
        user_code = _new_user_code()
        now = _now()
        with self._lock:
            requests = self._read()
            self._purge(requests)
            # Re-roll a code collision rather than shadowing a live request.
            while any(
                isinstance(item, dict) and item.get("user_code") == user_code
                for item in requests.values()
            ):
                user_code = _new_user_code()
            requests[_hash(device_code)] = {
                "user_code": user_code,
                "status": "pending",
                "created_at": now,
                "expires_at": now + self._ttl,
                "approved_at": None,
                "polls": 0,
                "key": None,
                "key_id": None,
                "key_name": None,
                "key_prefix": None,
                "user": None,
                "display_name": None,
            }
            self._write(requests)

        base = base_url.rstrip("/")
        verify = f"{base}/device"
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verify,
            "verification_uri_complete": f"{verify}?user_code={user_code}",
            "expires_in": self._ttl,
            "interval": POLL_INTERVAL,
        }

    def describe_user_code(self, user_code: str) -> dict[str, Any]:
        """Read the pending request named by *user_code* (for the approval page).

        Raises rather than returning a flag so the browser view can render a
        precise message instead of a generic failure.
        """
        wanted = _normalize_user_code(user_code)
        with self._lock:
            requests = self._read()
            self._purge(requests)
            self._write(requests)
            for item in requests.values():
                if not isinstance(item, dict) or item.get("user_code") != wanted:
                    continue
                if item.get("status") == "approved":
                    raise AlreadyApprovedError(wanted)
                return {
                    "user_code": wanted,
                    "created_at": item.get("created_at"),
                    "expires_at": item.get("expires_at"),
                    "status": item.get("status"),
                }
            raise UnknownUserCodeError(wanted)

    def approve(self, user_code: str, *, key: Mapping[str, Any], user: str, display_name: str | None) -> dict[str, Any]:
        """Bind a freshly minted key to *user_code* and mark it approved."""
        wanted = _normalize_user_code(user_code)
        with self._lock:
            requests = self._read()
            self._purge(requests)
            for digest, item in requests.items():
                if not isinstance(item, dict) or item.get("user_code") != wanted:
                    continue
                if item.get("status") == "approved":
                    raise AlreadyApprovedError(wanted)
                item.update({
                    "status": "approved",
                    "approved_at": _now(),
                    "key": key.get("key"),
                    "key_id": key.get("id"),
                    "key_name": key.get("name"),
                    "key_prefix": key.get("prefix"),
                    "key_expires_at": key.get("expires_at"),
                    "user": user,
                    "display_name": display_name,
                })
                requests[digest] = item
                self._write(requests)
                return {"user_code": wanted, "key_id": key.get("id")}
            raise UnknownUserCodeError(wanted)

    def redeem(self, device_code: str) -> tuple[str, dict[str, Any] | None]:
        """One poll.

        Returns ``(status, payload)`` where status is ``pending``, ``approved``,
        ``expired`` or ``unknown``.  An approved request is **consumed**: the
        key is returned exactly once and the entry is deleted, so replaying the
        same ``device_code`` yields ``unknown``.
        """
        if not device_code:
            return "unknown", None
        digest = _hash(str(device_code))
        with self._lock:
            requests = self._read()
            item = requests.get(digest)
            if not isinstance(item, dict):
                return "unknown", None
            if float(item.get("expires_at") or 0) < _now():
                requests.pop(digest, None)
                self._write(requests)
                return "expired", None
            item["polls"] = int(item.get("polls") or 0) + 1
            if item.get("status") != "approved":
                requests[digest] = item
                self._write(requests)
                return "pending", None
            requests.pop(digest, None)
            self._write(requests)
            return "approved", {
                "api_key": item.get("key"),
                "key_id": item.get("key_id"),
                "key_name": item.get("key_name"),
                "key_prefix": item.get("key_prefix"),
                "key_expires_at": item.get("key_expires_at"),
                "user": item.get("user"),
                "display_name": item.get("display_name"),
            }

    def count_pending(self) -> int:
        """Live pending requests; used by tests and diagnostics."""
        with self._lock:
            requests = self._read()
            removed = self._purge(requests)
            if removed:
                self._write(requests)
            return len(requests)


__all__ = [
    "POLL_INTERVAL",
    "DeviceAuthStore",
    "DeviceFlowError",
    "UnknownUserCodeError",
    "ExpiredUserCodeError",
    "AlreadyApprovedError",
]
