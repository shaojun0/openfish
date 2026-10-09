"""One storage port for the catalogs that hold bytes.

openfish stores two kinds of payload: the artifact catalogs an operator drops
files into (tools, npm, docker, debian, packages) and the documentation an
administrator writes in the browser.  Both used to be reached by handing a
filesystem path around — ``settings.hub.tools_dir`` joined with a name — which
made "where the bytes live" a decision baked into every call site.  This module
is the single place that decision is made instead: callers name a **key**
(a relative POSIX path inside a catalog) and an :class:`ObjectStore` turns it
into bytes.

What the port deliberately keeps, and what Dify's object store does not
---------------------------------------------------------------------
Dify's storage port is flat: keys are ``prefix/<uuid>.<ext>`` and only one
implementation even implements directory listing.  openfish cannot do that —
``/tools/<category>/<file>`` and ``/docs/<ecosystem>/<slug>`` *are* the protocol
surface (pip, npm, apt and Docker clients fetch by path, and an operator's
mental model of the catalog is the directory tree), so the **address** stays in
the database — a row per artifact, carrying its public path, its display name
and its media type — and the **key** is opaque: a bare ``uuid4`` with no
extension and no path structure.  A key never encodes what the object is, so
nothing can start parsing it, and the medium can change (a directory today, a
bucket tomorrow) without rewriting a single name.

Two consequences are deliberate.  :meth:`ObjectStore.walk` still exists — the
import path reads a directory tree, and the integrity check reconciles rows
against objects — but nothing *serves* from it any more.  And every write names
its media type, because a key that carries no extension is only self-describing
if the write says what it wrote.

What the port buys
------------------
A second backend (S3/MinIO, WebDAV, a shared NFS mount) becomes one class plus
one branch in :func:`catalog_store`; no route, service or gate changes.  That is
the whole point: the deployment that has outgrown a local directory is the one
this repository is built for, and it should not need a rewrite to move.

The local backend is not a fallback
-----------------------------------
:class:`LocalStore` is the default and must behave exactly like the direct
``Path`` code it replaces — the atomic-write and path-safety primitives come
from :mod:`services.fileio` and :mod:`services.paths`, the same ones the old
call sites used, so nothing about durability or traversal safety changed when
the call sites did.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Iterable, Iterator, Protocol

from config import settings
from services.digest import sha256_of_reader
from services.fileio import atomic_write_stream, parse_json
from services.namespaces import DOCS, TOOLS, root_for
from services.paths import contained


#: Read size for the key->object translation when a store has to move bytes
#: through this process (the S3 backend spools, :func:`ObjectStore.put`).
_SPOOL_MAX_BYTES = 8 * 1024 * 1024

#: How many keys one ``delete_many`` request may name.  S3's own ceiling.
_DELETE_BATCH = 1000

#: The sub-directory of a catalog root that holds the objects themselves.
#:
#: The key is a bare uuid, so the *store* has to be somewhere the operator's own
#: tree is not — otherwise ``tools import`` would walk the objects it just wrote
#: and import them as artifacts.  A reserved sub-directory keeps the separation
#: without putting anything back into the key, and keeps a catalog's payload on
#: the same disk the catalog is mounted from (which is where an operator who
#: re-pointed ``docker/tools`` at a big disk expects the bytes to be).
OBJECTS_DIRNAME = "objects"


# ── What a store reports about one key ───────────────────────────────

@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """One entry below a store root.

    ``key`` is POSIX and relative to the store root (``ops/backup.sh``); it
    never starts with ``/`` and never contains ``..``, because every key that
    reaches a store is proved by :func:`services.paths.contained` first.

    ``is_dir`` exists because a store is read as a tree in two places: the
    catalog import walks an operator's folder tree, and the integrity check
    reconciles what is stored against what the rows reference.  A store whose
    medium has no real directories reports the ones implied by key prefixes.
    """

    key: str
    is_dir: bool
    size: int
    modified: float | None


# ── The port ─────────────────────────────────────────────────────────

class ObjectStore(Protocol):
    """The half-dozen operations every catalog actually needs.

    Deliberately narrow.  There is no ``copy``, no ``rename`` and no ``scan``
    with filters: the catalogs never needed them, and a port that grows methods
    nobody calls is a port a second backend has to implement for nothing.
    """

    #: Stable catalog name (``tools``, ``docs``) — the future object-store
    #: prefix, and what log lines and integrity reports call this store.
    namespace: str

    #: Human-readable description of where these objects live: a directory for
    #: the local backend, a scheme-qualified prefix (``s3://bucket/tools``) for
    #: one that has no path.  Catalog payloads and health output report it, so
    #: "which store is this deployment actually using?" is answerable.
    location: str

    def put(
        self,
        key: str,
        chunks: Iterable[bytes],
        *,
        content_type: str | None = None,
    ) -> int:
        """Stream *chunks* onto *key*; return the bytes written.

        *content_type* is what the medium should record about the bytes.  It is
        optional because a filesystem has nowhere to put it (the row that owns
        the key is the metadata), but an object store does — and a key that no
        longer carries a file extension is only self-describing if the write
        says what it wrote.
        """
        ...

    def open(self, key: str) -> IO[bytes]:
        """Open *key* for reading; raise ``FileNotFoundError`` when absent."""
        ...

    def stat(self, key: str) -> ObjectInfo | None:
        """Metadata for *key*, or ``None`` when it does not exist."""
        ...

    def exists(self, key: str) -> bool:
        """Whether *key* exists (``""`` asks about the store root itself)."""
        ...

    def delete(self, key: str) -> None:
        """Remove one object.  A missing key is not an error."""
        ...

    def delete_many(self, keys: Iterable[str]) -> None:
        """Remove every key in *keys*.

        The bulk form of :meth:`delete`, because one document is a row per
        revision plus a row per asset — each with its own key — and deleting
        them one request at a time is how a catalog delete becomes slow on a
        bucket.  It returns nothing on purpose: a filesystem knows whether each
        key was there and a bucket's batch delete does not report it, so a count
        would mean different things on the two backends.  A caller that needs to
        know asks :meth:`exists` first.
        """
        ...

    def walk(self, prefix: str = "") -> Iterator[ObjectInfo]:
        """Yield every directory and file below *prefix*, deterministically."""
        ...


# ── Local backend ────────────────────────────────────────────────────

class LocalStore:
    """Keys are paths under one directory — the historical layout, unchanged.

    ``root`` may not exist; :meth:`walk` then yields nothing and callers that
    need to distinguish "missing" from "empty" ask :meth:`exists` with ``""``.
    """

    def __init__(self, namespace: str, root: str) -> None:
        self.namespace = namespace
        self.root = root
        self.location = root

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"LocalStore({self.namespace!r}, {self.root!r})"

    # ── Key → path ───────────────────────────────────────────────

    def _path(self, key: str) -> Path:
        """The path *key* names, proved to stay under the root.

        Reuses :func:`services.paths.contained` rather than re-deriving the
        check locally: traversal safety has exactly one implementation in this
        repository, and a store that grew a second one is how the two drift.
        """
        if not key:
            return Path(self.root)
        return contained(self.root, *key.split("/"))

    # ── Read side ────────────────────────────────────────────────

    def open(self, key: str) -> IO[bytes]:
        return open(self._path(key), "rb")

    def stat(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        try:
            info = os.stat(path)
        except FileNotFoundError:
            return None
        # ``os.stat`` follows symlinks, so a link to a directory reports as a
        # directory — which is what the catalog scanners want, because a mount
        # point is a category like any other.
        is_dir = path.is_dir()
        return ObjectInfo(
            key=key.strip("/"),
            is_dir=is_dir,
            size=0 if is_dir else info.st_size,
            modified=info.st_mtime,
        )

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def walk(self, prefix: str = "") -> Iterator[ObjectInfo]:
        base = self._path(prefix)
        if not base.is_dir():
            return
        # Deterministic order: the catalog pages, the JSON payloads and the
        # gates all compare listings, and an unstable os.walk order would make
        # every one of them flaky.  Symlinks are not descended (os.walk's
        # default) but are reported as the directories they point at, so a
        # sharded catalog still lists its categories.
        for current, dirnames, filenames in os.walk(base):
            dirnames.sort()
            filenames.sort()
            here = Path(current)
            for name in dirnames:
                path = here / name
                yield ObjectInfo(
                    key=self._key(path),
                    is_dir=True,
                    size=0,
                    modified=self._mtime(path),
                )
            for name in filenames:
                path = here / name
                try:
                    info = os.stat(path)
                except FileNotFoundError:  # deleted between walk and stat
                    continue
                yield ObjectInfo(
                    key=self._key(path),
                    is_dir=False,
                    size=info.st_size,
                    modified=info.st_mtime,
                )

    def _key(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    @staticmethod
    def _mtime(path: Path) -> float | None:
        try:
            return os.stat(path).st_mtime
        except OSError:  # pragma: no cover - a race with a deletion
            return None

    # ── Write side ───────────────────────────────────────────────

    def put(
        self,
        key: str,
        chunks: Iterable[bytes],
        *,
        content_type: str | None = None,
    ) -> int:
        # A directory has nowhere to record a media type; the row that owns the
        # key is the metadata here.  Accepted and ignored on purpose, so callers
        # do not branch on the backend.
        return atomic_write_stream(self._path(key), chunks)

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def delete_many(self, keys: Iterable[str]) -> None:
        for key in keys:
            path = self._path(key)
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue


# ── S3-compatible backend ────────────────────────────────────────────
#
# The client surface is deliberately tiny — six operations, all of which boto3
# has — because a store that can be driven by an in-memory double is a store the
# offline gate can actually verify.  Exactly those six are what the backend
# needs; anything more (multipart uploads, ACLs, lifecycle rules) would have to
# be justified by a caller first.

class _S3Client(Protocol):
    """The slice of a boto3 S3 client this backend uses."""

    def put_object(self, **kwargs: Any) -> Any: ...
    def get_object(self, **kwargs: Any) -> Any: ...
    def head_object(self, **kwargs: Any) -> Any: ...
    def delete_object(self, **kwargs: Any) -> Any: ...
    def delete_objects(self, **kwargs: Any) -> Any: ...
    def list_objects_v2(self, **kwargs: Any) -> Any: ...


def _is_missing(exc: Exception) -> bool:
    """Whether a botocore error means "no such object" rather than a failure.

    Reads the error code rather than the exception class so a double, a real
    boto3 client and a non-AWS server that spells the code differently are all
    handled in one place.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}


class S3Store:
    """Keys in an S3-compatible bucket, one catalog per prefix.

    Every catalog shares a bucket and lives under its own prefix
    (``<S3_PREFIX>/<namespace>/…``) — see
    :attr:`config.StorageConfig.s3_bucket_name` for why the split is not
    "a bucket per catalog".

    Two behaviours differ from :class:`LocalStore` and are worth knowing:

    * **Uploads are spooled.**  :meth:`put` receives an iterator of chunks (a
      multipart body) and S3's single-request write needs a sized body, so the
      bytes go to a :class:`tempfile.SpooledTemporaryFile` first — in memory up
      to :data:`_SPOOL_MAX_BYTES`, on disk past that.  One extra local copy on
      the way to the bucket, never the whole artifact in memory.
    * **Directories are implied.**  A medium with no directories can only report
      the ones its keys share a prefix for, which is what :meth:`walk` does, so
      the catalog tree keeps its shape.
    """

    def __init__(
        self,
        namespace: str,
        *,
        prefix: str,
        bucket: str,
        client: _S3Client,
    ) -> None:
        self.namespace = namespace
        self.bucket = bucket
        self.client = client
        self.prefix = _normalized_prefix(prefix, namespace)
        self.location = f"s3://{bucket}/{self.prefix.rstrip('/')}"

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"S3Store({self.namespace!r}, {self.location!r})"

    # ── Key → object ─────────────────────────────────────────────

    def _full(self, key: str) -> str:
        """The bucket key for one catalog key.

        ``contained`` is the local backend's traversal check; on S3 the guard is
        structural — a key can only ever be appended to the store's own prefix —
        so all this has to refuse is the same ``..``/absolute spellings, and it
        does that by reusing the rule rather than re-deriving it.
        """
        stripped = key.strip("/")
        if not stripped:
            return self.prefix
        return self.prefix + contained("", *stripped.split("/")).as_posix().lstrip("/")

    # ── Read side ────────────────────────────────────────────────

    def open(self, key: str) -> IO[bytes]:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=self._full(key))["Body"]
        except Exception as exc:  # noqa: BLE001 - re-raised unless it means 404
            if _is_missing(exc):
                raise FileNotFoundError(key) from exc
            raise
        return body

    def stat(self, key: str) -> ObjectInfo | None:
        if key.strip("/") and key.endswith("/"):
            # A directory key names no object; report the implied directory.
            return ObjectInfo(key=key.strip("/"), is_dir=True, size=0, modified=None)
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self._full(key))
        except Exception as exc:  # noqa: BLE001 - re-raised unless it means 404
            if _is_missing(exc):
                return None
            raise
        modified = head.get("LastModified")
        return ObjectInfo(
            key=key.strip("/"),
            is_dir=False,
            size=int(head.get("ContentLength", 0)),
            modified=modified.timestamp() if modified is not None else None,
        )

    def exists(self, key: str) -> bool:
        if key.strip("/") and self._has_prefix(self.prefix + key.strip("/") + "/"):
            return True
        if not key.strip("/") and self._has_prefix(self.prefix):
            return True
        return self.stat(key) is not None

    def _has_prefix(self, prefix: str) -> bool:
        page = self.client.list_objects_v2(
            Bucket=self.bucket, Prefix=prefix, MaxKeys=1
        )
        return bool(page.get("KeyCount") or page.get("Contents"))

    def walk(self, prefix: str = "") -> Iterator[ObjectInfo]:
        base = self._full(prefix) if prefix.strip("/") else self.prefix
        if base and not base.endswith("/"):
            base += "/"
        # A directory and a key are each reported once even if a server repeats
        # a common prefix across pages — the listing is a tree, and a caller
        # that renders it must not see the same category twice.
        seen_dirs: set[str] = set()
        seen_keys: set[str] = set()
        yield from self._walk_prefix(base, seen_dirs, seen_keys)

    def _walk_prefix(
        self,
        full_prefix: str,
        seen_dirs: set[str],
        seen_keys: set[str],
    ) -> Iterator[ObjectInfo]:
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": full_prefix,
                "Delimiter": "/",
            }
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for common in page.get("CommonPrefixes", []) or []:
                child_full = common["Prefix"]
                child = child_full[len(self.prefix):].rstrip("/")
                if child in seen_dirs:
                    continue
                seen_dirs.add(child)
                yield ObjectInfo(key=child, is_dir=True, size=0, modified=None)
                yield from self._walk_prefix(child_full, seen_dirs, seen_keys)
            for item in page.get("Contents", []) or []:
                key = item["Key"][len(self.prefix):]
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                modified = item.get("LastModified")
                yield ObjectInfo(
                    key=key,
                    is_dir=False,
                    size=int(item.get("Size", 0)),
                    modified=modified.timestamp() if modified is not None else None,
                )
            if not page.get("IsTruncated"):
                return
            token = page.get("NextContinuationToken")

    # ── Write side ───────────────────────────────────────────────

    def put(
        self,
        key: str,
        chunks: Iterable[bytes],
        *,
        content_type: str | None = None,
    ) -> int:
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES) as spool:
            total = 0
            for chunk in chunks:
                if not chunk:
                    continue
                spool.write(chunk)
                total += len(chunk)
            spool.seek(0)
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket,
                "Key": self._full(key),
                "Body": spool,
                "ContentLength": total,
            }
            if content_type:
                # Without this the object lands as binary/octet-stream and a key
                # that carries no extension leaves the bucket unable to say what
                # it holds at all.
                kwargs["ContentType"] = content_type
            self.client.put_object(**kwargs)
        return total

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._full(key))

    def delete_many(self, keys: Iterable[str]) -> None:
        names = [{"Key": self._full(key)} for key in keys]
        for start in range(0, len(names), _DELETE_BATCH):
            batch = names[start:start + _DELETE_BATCH]
            if not batch:
                continue
            self.client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True}
            )


def _normalized_prefix(prefix: str, namespace: str) -> str:
    """``<S3_PREFIX>/<namespace>/`` with exactly one separator and no leading one."""
    parts = [part for part in (prefix or "").strip("/").split("/") if part]
    parts.append(namespace)
    return "/".join(parts) + "/"


def _build_s3_client(config: Any) -> _S3Client:
    """The boto3 client, imported on demand.

    boto3 is an *optional* dependency: a local-storage deployment must not pay
    for it, so the import happens when an S3 store is actually built and a
    missing extra is a message an operator can act on rather than an
    ImportError at boot.
    """
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - exercised by a local install
        raise RuntimeError(
            "OBJECT_BACKEND=s3 需要 boto3：pip install 'cpypiserver[s3]'"
            "（容器镜像用 --build-arg OPENFISH_EXTRAS=s3 重建）"
        ) from exc

    # An empty region is not allowed by the signer, and an S3-compatible server
    # does not care which one it gets.
    region = (config.s3_region or "").strip() or "us-east-1"
    return boto3.client(
        "s3",
        endpoint_url=(config.s3_endpoint or "").strip() or None,
        aws_access_key_id=(config.s3_access_key or "").strip() or None,
        aws_secret_access_key=(config.s3_secret_key or "").strip() or None,
        region_name=region,
        config=Config(
            s3={"addressing_style": config.s3_address_style},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


#: Built stores, keyed by everything that decides how to reach the bucket — so
#: a boto3 client (and its connection pool) is created once per process instead
#: of once per catalog lookup.  The secret is deliberately *not* in the key:
#: configuration is read once at import (see :mod:`config.base`), so a cached
#: client is exactly as fresh as the settings that produced it.
_BUILT: dict[tuple, ObjectStore] = {}
#
# The parse-and-default-on-malformed rule lives in :mod:`services.fileio`; these
# two functions only add "get the text out of a store" to it, so a catalog
# overlay read through the port and one read from a path cannot disagree about
# what a malformed file means.

# ── Text and JSON over a store ───────────────────────────────────────

def etag_of(info: ObjectInfo) -> str:
    """A cheap version tag for one stored object — **unquoted**.

    ``(modified, size)`` is the same identity Werkzeug derives for a file on
    disk, and it is deliberately *not* a content digest: a multi-gigabyte
    artifact must not be hashed on every download.  Unquoted because
    ``Response.set_etag`` is what quotes an entity tag (and rejects one that
    arrives pre-quoted, which is exactly the bug this signature prevents).
    """
    stamp = int((info.modified or 0) * 1_000_000)
    return f"{stamp:x}-{info.size:x}"


def read_text(store: ObjectStore, key: str) -> str | None:
    """The UTF-8 text of *key*, or ``None`` when it is missing or unreadable.

    A malformed overlay must degrade to the default rather than 500 the page
    that reads it, so both a missing file and an undecodable one come back as
    ``None`` — the same posture :func:`services.fileio.read_json` takes.
    """
    try:
        with store.open(key) as handle:
            return handle.read().decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return None


def read_json(store: ObjectStore, key: str, default: Any = None) -> Any:
    """Parse *key* as JSON through *store*, falling back to *default*."""
    text = read_text(store, key)
    if text is None:
        return default
    return parse_json(text, default=default)


def digest_of(
    store: ObjectStore,
    info: ObjectInfo,
    *,
    max_bytes: int | None = None,
) -> str | None:
    """SHA-256 of one stored object, memoised on its version stamp.

    *max_bytes* is the artifact catalogs' "not worth hashing" ceiling: a
    multi-gigabyte installer is still listed, just without a digest.  The stamp
    is ``(namespace, key, modified, size)`` — the store's equivalent of the
    ``(path, mtime_ns, size)`` a file-based caller hands
    :mod:`services.digest`, which is where the hashing loop and the cache live,
    so a store object and a file cannot disagree about a digest.
    """
    if info.is_dir:
        return None
    if max_bytes is not None and info.size > max_bytes:
        return None
    stamp = (store.namespace, info.key, info.modified, info.size)
    try:
        with store.open(info.key) as handle:
            return sha256_of_reader(handle, stamp=stamp)
    except OSError:
        return None


# ── The stores this deployment has ───────────────────────────────────

def catalog_store(namespace: str, root: str) -> ObjectStore:
    """The store backing one catalog.

    The branch that a second backend adds lives here and nowhere else — every
    call site asks for a catalog by name and gets whatever medium this
    deployment configured.  ``root`` is the catalog's local directory, whose
    reserved :data:`OBJECTS_DIRNAME` sub-directory holds the objects; the S3
    backend ignores it and derives its prefix from the namespace instead, so
    switching a deployment to a bucket is a configuration change and nothing
    more.  Both layouts end up at ``<somewhere>/<namespace>/<uuid4>``.
    """
    config = settings.storage
    if config.object_backend != "s3":
        return LocalStore(namespace, os.path.join(root, OBJECTS_DIRNAME))

    cache_key = (
        namespace,
        config.s3_endpoint,
        config.s3_bucket_name,
        config.s3_region,
        config.s3_address_style,
        config.s3_prefix,
    )
    store = _BUILT.get(cache_key)
    if store is None:
        store = _BUILT[cache_key] = S3Store(
            namespace,
            prefix=config.s3_prefix,
            bucket=config.s3_bucket_name,
            client=_build_s3_client(config),
        )
    return store


def tools_store() -> ObjectStore:
    """The ``TOOLS_DIR`` catalog."""
    return catalog_store(TOOLS, root_for(TOOLS))


def docs_store() -> ObjectStore:
    """The ``DOCS_DIR`` catalog."""
    return catalog_store(DOCS, root_for(DOCS))


__all__ = [
    "LocalStore",
    "OBJECTS_DIRNAME",
    "ObjectInfo",
    "ObjectStore",
    "S3Store",
    "catalog_store",
    "digest_of",
    "docs_store",
    "etag_of",
    "read_json",
    "read_text",
    "tools_store",
]
