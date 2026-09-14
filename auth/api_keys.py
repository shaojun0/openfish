"""API Key Manager — CRUD for Bearer tokens used by pip/uv/twine.

Storage: SQLite via SQLAlchemy.  Keys SHA256-hashed; raw key only revealed once.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import scoped_session

from auth.permissions import role_for
from models.api_key import ApiKey, ApiKeyStats

KEY_PREFIX = "cpypi_"
KEY_BYTES = 32


class ApiKeyManager:
    """API key CRUD + validation + usage stats."""

    def __init__(self, session_factory: scoped_session) -> None:
        self._session_factory = session_factory

    @property
    def _s(self):
        return self._session_factory()

    # ── Create ──────────────────────────────────────────────────────

    def create_key(
        self, name: str, created_by: str, expires_in_days: int | None = None,
    ) -> dict:
        raw = KEY_PREFIX + secrets.token_hex(KEY_BYTES)
        key_hash = _sha256(raw)
        key_id = f"k_{secrets.token_hex(6)}"
        prefix = raw[:12] + "…"

        expires_at: str | None = None
        if expires_in_days is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        session = self._s
        try:
            entry = ApiKey(
                id=key_id, name=name, prefix=prefix, hash=key_hash,
                created_by=created_by, created_at=_now_iso(), expires_at=expires_at,
            )
            session.add(entry)
            session.commit()
            return entry.to_dict() | {"key": raw}
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_keys(self, created_by: str | None = None) -> list[dict]:
        session = self._s
        try:
            q = session.query(ApiKey)
            if created_by:
                q = q.filter(ApiKey.created_by == created_by)
            keys = q.order_by(ApiKey.created_at.desc()).all()
            return [k.to_dict() for k in keys]
        finally:
            session.close()

    def delete_key(self, key_id: str) -> bool:
        session = self._s
        try:
            entry = session.get(ApiKey, key_id)
            if entry is None:
                return False
            session.delete(entry)
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── Validate ────────────────────────────────────────────────────

    def validate(self, raw_key: str) -> Optional[dict]:
        """Validate a raw API key. Returns user info dict (with role) or None."""
        if not raw_key or not raw_key.startswith(KEY_PREFIX):
            return None
        key_hash = _sha256(raw_key)
        session = self._s
        try:
            entry = session.query(ApiKey).filter(ApiKey.hash == key_hash).first()
            if entry is None or entry.is_expired():
                return None
            entry.last_used = _now_iso()
            session.commit()
            return {
                "sub": entry.created_by,
                "key_id": entry.id,
                "key_name": entry.name,
                # A key inherits the role of the user it was issued to, resolved
                # by the same rule session and OAuth2 use. Without this an API
                # key could never reach the admin endpoints.
                "role": role_for(entry.created_by),
                "auth_method": "api_key",
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── Stats ───────────────────────────────────────────────────────

    def record_download(self, key_id: str, package_name: str) -> None:
        self._increment(key_id, package_name, "download")

    def record_upload(self, key_id: str, package_name: str) -> None:
        self._increment(key_id, package_name, "upload")

    def get_key_stats(self, key_id: str) -> dict:
        session = self._s
        try:
            stats = (
                session.query(ApiKeyStats)
                .filter(ApiKeyStats.key_id == key_id).all()
            )
            downloads = sum(s.count for s in stats if s.event_type == "download")
            uploads = sum(s.count for s in stats if s.event_type == "upload")
            return {
                "key_id": key_id,
                "total_downloads": downloads,
                "total_uploads": uploads,
                "per_package": [
                    {"package_name": s.package_name, "event_type": s.event_type, "count": s.count}
                    for s in sorted(stats, key=lambda x: (-x.count, x.package_name))
                ],
            }
        finally:
            session.close()

    def _increment(self, key_id: str, package_name: str, event_type: str) -> None:
        session = self._s
        try:
            stat = (
                session.query(ApiKeyStats)
                .filter(
                    ApiKeyStats.key_id == key_id,
                    ApiKeyStats.package_name == package_name,
                    ApiKeyStats.event_type == event_type,
                ).first()
            )
            if stat:
                stat.count = ApiKeyStats.count + 1
            else:
                session.add(ApiKeyStats(
                    key_id=key_id, package_name=package_name,
                    event_type=event_type, count=1,
                ))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
