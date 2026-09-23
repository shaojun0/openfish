"""Accept an ``npm publish`` — the one write the npm registry protocol has.

``GET`` is almost the whole npm registry protocol; publishing is the single
mutation.  ``npm publish`` builds one JSON document and ``PUT``s it to
``<registry>/<package>``::

    {
      "name": "left-pad",
      "dist-tags": {"latest": "1.3.0"},
      "versions": {"1.3.0": {..., "dist": {"shasum": ..., "integrity": ...}}},
      "_attachments": {
        "left-pad-1.3.0.tgz": {"content_type": ..., "data": "<base64>", "length": N}
      }
    }

The tarball travels base64-encoded inside ``_attachments``; the client also
states the ``shasum``/``integrity`` it computed.  Nothing about that body can be
trusted, so this module re-derives every fact it stores:

* the request path and the body's ``name`` must agree, and the name must be a
  legal (lowercase, optionally scoped) npm name;
* every attachment must resolve to a version the document actually declares;
* the bytes must decode, must be a gzipped tar carrying
  ``package/package.json``, and that manifest's ``name``/``version`` must match
  the version being published;
* ``shasum`` / ``integrity``, when the client sent them, must equal what the
  bytes hash to — a mismatch is a ``400``, not a stored package.

Only then is the tarball written to ``NPM_DIR`` (atomically, under npm's flat
``<name>-<version>.tgz`` convention — a scoped ``@scope/name`` becomes
``name-<version>.tgz``, exactly as the public registry serves it) and the
dist-tags are recorded in :data:`services.npm_registry.PUBLISH_INDEX_FILENAME`.

The dist-tag sidecar exists because the registry's normal read path derives
``latest`` from the highest version on disk.  ``npm publish --tag next`` sends
*only* the tag it is setting, and a tag that is not persisted could never
resolve — ``next`` would silently become "whatever version sorts last".

An already-published version is refused (:class:`errors.PublishConflictError`,
``409``) because a published version is immutable; ``overwrite=true`` in the
deployment config relaxes that to match the twine upload path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from errors import BadRequestError, PublishConflictError
from services.fileio import atomic_write_bytes, read_json, write_json
from services.npm_registry import PUBLISH_INDEX_FILENAME

logger = logging.getLogger("cpypiserver.npm")

#: Largest tarball this endpoint accepts, measured on the *decoded* bytes.
#: npm's own default is looser, but this is a single-request JSON body and the
#: server's ``max_content_length`` (100 MiB) has to carry the base64 expansion.
MAX_TARBALL_BYTES = 64 * 1024 * 1024

#: One publish carries one new tarball; the merged packument may list many more
#: versions, but those arrive without attachments.
MAX_ATTACHMENTS = 16

#: Cap on ``package/package.json`` read out of an uploaded tarball.
MAX_MANIFEST_BYTES = 1024 * 1024

#: npm's package-name grammar, restricted to the lowercase form the CLI
#: enforces for a publish: ``left-pad`` or ``@scope/left-pad``.
_NAME_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")
#: A version is semver-ish; the important part is that it can never contain a
#: path separator or start a traversal.
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
#: The scope is stripped from the stored filename, so no ``/`` may survive here.
_TARBALL_STEM_RE = re.compile(r"^[A-Za-z0-9._~@+-]+$")

#: Serialises the read-modify-write of the dist-tag sidecar inside one worker.
#: Cross-worker publishes can still lose a tag update, which is why
#: ``services/npm_registry`` treats the file as a cache of metadata rather than
#: the authority on which versions exist (the tarballs are that).
_publish_lock = threading.Lock()


@dataclass(frozen=True)
class PublishedVersion:
    """What one accepted attachment became on disk."""

    name: str
    version: str
    filename: str
    size: int
    shasum: str
    integrity: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Small parsers ────────────────────────────────────────────────────

def _attachment_basename(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return ""
    path = urlparse(url).path
    return unquote(path.rsplit("/", 1)[-1]) if path else ""


def _version_for_attachment(
    key: str, name: str, versions: Mapping[str, Any],
) -> str | None:
    """The version an ``_attachments`` key belongs to, or None.

    The client's attachment key is not authoritative: for a scoped package it
    is ``@scope/name-1.0.0.tgz`` (a slash, which no filename could keep) while
    the ``dist.tarball`` basename it maps to is ``name-1.0.0.tgz``.  So the
    version manifests are searched for one whose ``dist.tarball`` ends in the
    key, falling back to the conventional flat spelling and finally to a
    ``-<version>.tgz`` suffix match.
    """
    short = str(name).split("/")[-1]
    for version, manifest in versions.items():
        dist = manifest.get("dist") if isinstance(manifest, dict) else None
        basename = _attachment_basename(dist.get("tarball")) if isinstance(dist, dict) else ""
        # The CLI names the attachment after the tarball URL it built, so the two
        # agree for both spellings; `key.endswith` also accepts a client that
        # scoped the tarball URL but left the scope on the attachment key.
        if basename and (basename == key or key.endswith("/" + basename)):
            return str(version)
        if key in (f"{name}-{version}.tgz", f"{short}-{version}.tgz"):
            return str(version)
    # Last resort: the key itself ends in ``-<version>.tgz`` for a declared
    # version.  Longest version first, so 1.0.0 cannot shadow 1.0.0-rc.1.
    for version in sorted((str(v) for v in versions), key=len, reverse=True):
        if key.endswith(f"-{version}.tgz"):
            return version
    return None


def _read_manifest(data: bytes, *, version: str) -> dict[str, Any]:
    """``package/package.json`` out of an uploaded tarball, size-capped.

    A publish that is not a readable npm tarball is rejected outright — unlike
    the mirror's reader, which silently falls back to the filename, a *client*
    claiming a version must prove it.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            member = None
            for candidate in archive:
                if not candidate.isfile() or not candidate.name.endswith("package.json"):
                    continue
                if candidate.name == "package/package.json":
                    member = candidate
                    break
                if member is None and candidate.name.count("/") == 1:
                    member = candidate
            if member is None:
                raise BadRequestError(
                    f"tarball for {version} does not contain package/package.json"
                )
            if member.size > MAX_MANIFEST_BYTES:
                raise BadRequestError(
                    f"package.json inside the tarball for {version} is too large"
                )
            handle = archive.extractfile(member)
            if handle is None:
                raise BadRequestError(f"cannot read package.json from the tarball for {version}")
            document = handle.read(MAX_MANIFEST_BYTES + 1)
    except (BadRequestError, tarfile.TarError, OSError, EOFError) as exc:
        if isinstance(exc, BadRequestError):
            raise
        raise BadRequestError(f"tarball for {version} is not a readable .tgz: {exc}") from exc
    try:
        manifest = json.loads(document.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadRequestError(f"package.json inside {version} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BadRequestError(f"package.json inside {version} is not a JSON object")
    return manifest


def _digests(data: bytes) -> tuple[str, str]:
    """``(sha1 hex, sha512 SRI)`` — the two values npm states in ``dist``."""
    return (
        hashlib.sha1(data).hexdigest(),
        "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii"),
    )


def _stored_filename(name: str, version: str) -> str:
    """npm's flat tarball name: the scope is stripped, ``@`` never survives."""
    short = name.split("/")[-1]
    if not short or short.startswith(".") or not _TARBALL_STEM_RE.match(short):
        raise BadRequestError(f"package name {name} cannot be stored as a filename")
    filename = f"{short}-{version}.tgz"
    if Path(filename).name != filename:
        raise BadRequestError(f"refusing unsafe tarball name {filename}")
    return filename


# ── Document validation ──────────────────────────────────────────────

def _validate(
    document: Any, path_name: str,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, str], str]:
    """``(name, versions, attachments, dist-tags, description)`` or a 400."""
    if not isinstance(document, dict):
        raise BadRequestError("Request body must be a JSON npm publish document")

    raw_name = document.get("name") or document.get("_id")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise BadRequestError("Publish document has no 'name'")
    name = raw_name.strip()
    if not _NAME_RE.match(name):
        raise BadRequestError(
            f"invalid package name {name} — npm names are lowercase, optionally "
            f"scoped as @scope/name"
        )
    # The route already decoded %2F, but a client may double-encode.
    requested = unquote(path_name or "")
    if requested and requested != name:
        raise BadRequestError(
            f"body name {name} does not match the request path {requested}"
        )

    versions = document.get("versions")
    if not isinstance(versions, dict) or not versions:
        raise BadRequestError("Publish document has no 'versions'")
    if any(not _VERSION_RE.match(str(version)) for version in versions):
        raise BadRequestError("Publish document declares an unusable version string")

    attachments = document.get("_attachments")
    if not isinstance(attachments, dict) or not attachments:
        raise BadRequestError("Publish document has no '_attachments' tarball")
    if len(attachments) > MAX_ATTACHMENTS:
        raise BadRequestError(
            f"too many attachments ({len(attachments)}); one publish carries one tarball"
        )

    tags_in = document.get("dist-tags") or {}
    tags: dict[str, str] = {}
    if isinstance(tags_in, dict):
        for tag, version in tags_in.items():
            if isinstance(tag, str) and tag and isinstance(version, str) and version:
                tags[tag] = version

    description = document.get("description")
    return name, versions, attachments, tags, description if isinstance(description, str) else ""


# ── The publish itself ───────────────────────────────────────────────

def publish(
    root: str | Path,
    document: Any,
    *,
    path_name: str = "",
    overwrite: bool = False,
    publisher: str = "",
) -> list[PublishedVersion]:
    """Store every tarball *document* carries; return what was written.

    Raises :class:`errors.BadRequestError` for a malformed or unverifiable
    document and :class:`errors.PublishConflictError` when the version already
    exists and *overwrite* is false.
    """
    name, versions, attachments, tags, description = _validate(document, path_name)

    root = Path(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BadRequestError(f"npm directory {root} is not writable: {exc}") from exc

    published: list[PublishedVersion] = []
    for key, attachment in attachments.items():
        if not isinstance(attachment, dict):
            raise BadRequestError(f"attachment {key} is not an object")
        version = _version_for_attachment(str(key), name, versions)
        if version is None:
            raise BadRequestError(
                f"attachment {key} does not match any version of {name}"
            )
        manifest = versions.get(version)
        if not isinstance(manifest, dict):
            raise BadRequestError(f"version {version} of {name} is not an object")
        published.append(
            _store_attachment(
                root, name, version, manifest, attachment, overwrite=overwrite,
            )
        )

    if not published:
        raise BadRequestError("Publish document carried no usable attachment")

    _record_metadata(
        root,
        name,
        tags=tags,
        versions=[item.version for item in published],
        description=description,
        publisher=publisher,
    )
    logger.info(
        "npm publish: %s stored %s",
        name,
        ", ".join(f"{item.version} ({item.size} B)" for item in published),
    )
    return published


def _store_attachment(
    root: Path,
    name: str,
    version: str,
    manifest: Mapping[str, Any],
    attachment: Mapping[str, Any],
    *,
    overwrite: bool,
) -> PublishedVersion:
    encoded = attachment.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise BadRequestError(f"attachment for {name}@{version} has no base64 'data'")
    try:
        data = base64.b64decode(encoded, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise BadRequestError(f"attachment for {name}@{version} is not valid base64: {exc}") from exc
    if not data:
        raise BadRequestError(f"attachment for {name}@{version} is empty")
    if len(data) > MAX_TARBALL_BYTES:
        raise BadRequestError(
            f"tarball for {name}@{version} is {len(data)} bytes, above the "
            f"{MAX_TARBALL_BYTES} byte limit"
        )

    inner = _read_manifest(data, version=version)
    for field, expected in (("name", name), ("version", version)):
        actual = inner.get(field)
        if isinstance(actual, str) and actual and actual != expected:
            raise BadRequestError(
                f"tarball declares {field} {actual} but is being published as {expected}"
            )

    shasum, integrity = _digests(data)
    dist = manifest.get("dist")
    dist = dist if isinstance(dist, dict) else {}
    stated_sha1 = dist.get("shasum")
    if isinstance(stated_sha1, str) and stated_sha1 and stated_sha1 != shasum:
        raise BadRequestError(
            f"shasum mismatch for {name}@{version}: document says {stated_sha1}, "
            f"uploaded bytes hash to {shasum}"
        )
    stated_integrity = dist.get("integrity")
    if isinstance(stated_integrity, str) and stated_integrity and stated_integrity != integrity:
        raise BadRequestError(
            f"integrity mismatch for {name}@{version}: the uploaded bytes do not "
            f"match the declared sha512"
        )

    filename = _stored_filename(name, version)
    dest = root / filename
    if dest.exists() and not overwrite:
        raise PublishConflictError(name, version)
    # A different package must not silently take over a filename: scopes are
    # stripped on disk, so @a/core and @b/core both want core-1.0.0.tgz.  The
    # mirror's own reader resolves that ambiguity by reading each tarball's
    # package.json, and it must not see a mixed-up file appear.  An existing file
    # that cannot be read back (too large, corrupt) is replaced rather than
    # guessed at — reaching here at all required `overwrite`.
    if dest.exists():
        existing_name = _existing_package_name(dest, version=version)
        if existing_name and existing_name != name:
            raise PublishConflictError(existing_name, version)

    atomic_write_bytes(dest, data)
    try:
        os.chmod(dest, 0o644)
    except OSError:  # pragma: no cover - non-POSIX or read-only metadata
        pass
    return PublishedVersion(
        name=name,
        version=version,
        filename=filename,
        size=len(data),
        shasum=shasum,
        integrity=integrity,
    )


def _existing_package_name(path: Path, *, version: str) -> str | None:
    """The name inside an already-stored tarball, or None when it cannot be read."""
    try:
        if path.stat().st_size > MAX_TARBALL_BYTES:
            return None
        manifest = _read_manifest(path.read_bytes(), version=version)
    except (BadRequestError, OSError):
        return None
    name = manifest.get("name")
    return name if isinstance(name, str) and name else None


def _record_metadata(
    root: Path,
    name: str,
    *,
    tags: Mapping[str, str],
    versions: list[str],
    description: str,
    publisher: str,
) -> None:
    """Merge this publish into ``publish.json`` (dist-tags, times, description).

    Best-effort: the tarball on disk is the source of truth for what exists, so
    a failure here costs a tag, never a package.  That is also why the write is
    a read-modify-write under a process lock rather than a bare overwrite.
    """
    now = time.time()
    path = root / PUBLISH_INDEX_FILENAME
    try:
        with _publish_lock:
            data = read_json(path, default={})
            packages = data.get("packages") if isinstance(data, dict) else None
            packages = dict(packages) if isinstance(packages, dict) else {}
            record = packages.get(name)
            record = dict(record) if isinstance(record, dict) else {}

            merged: dict[str, str] = {}
            for tag, version in (record.get("dist-tags") or {}).items():
                if isinstance(tag, str) and isinstance(version, str):
                    merged[tag] = version
            for tag, version in tags.items():
                merged[str(tag)] = str(version)
            record["dist-tags"] = merged

            times = dict(record.get("time") or {})
            for version in versions:
                times[version] = now
            record["time"] = times
            record["modified"] = now
            if description:
                record["description"] = description
            if publisher:
                record["publisher"] = publisher

            packages[name] = record
            write_json(path, {"version": 1, "packages": packages})
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("npm publish: could not record metadata for %s: %s", name, exc)


__all__ = ["MAX_TARBALL_BYTES", "PublishedVersion", "publish"]
