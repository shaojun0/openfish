"""Admin-only uploads into the tools and Docker artifact directories.

The tools and Docker catalogs are file-backed: the filesystem is the source of
truth, and the next scan lists whatever is on disk.  Until this module existed
the only way to publish was to copy a file in by hand; the browser upload
endpoints in :mod:`routes.hub` and :mod:`routes.docker` now do that copy safely.

What "safely" buys, in one place so both ecosystems enforce identical rules:

* **One path segment.**  A *filename* is a basename — no ``/``, no ``\\``, no
  ``..``, no dotfile — and a tools *category* is a single directory name with the
  same restrictions.  A category is optional: an empty one places the file at the
  tools root, which :func:`services.hub.scan_tools` reports as the synthetic
  ``root`` (the UI calls it “uncategorized”).
* **An allow-list, not a deny-list.**  Tools may use a broad set of script,
  archive, binary and config suffixes; Docker accepts the ``docker save``
  tarballs and the compose/Dockerfile snippets the catalog already recognises.
  The lists are explicit, so a mistaken upload is a ``400`` instead of a file the
  catalog cannot describe.
* **A name the catalog will actually show.**  ``scan_tools``/``scan_flat``
  deliberately hide dotfiles, ``catalog.json`` and the README/licence files that
  document a tree; accepting one would write a file the response could not then
  report.
* **Streamed, atomic writes.**  The body is read in chunks and written to a
  sibling temp file that is ``os.replace``d into place, so a multi-gigabyte image
  never lands in memory and an aborted upload leaves the previous file (or no
  file) behind — never a half-written one.

The size ceiling is :attr:`config.StorageConfig.max_content_length`, the same one
twine uploads honour.  Werkzeug additionally refuses an oversized body before a
view runs (``MAX_CONTENT_LENGTH``); the cap here also catches a chunked body and
gives a clearer message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from config import settings
from errors import BadRequestError, PypiError
from services.fileio import atomic_write_stream
from services.format import human_size
from services.paths import MAX_NAME_BYTES, contained, single_segment

#: Read size for an upload stream.  Large enough to keep the syscall count low,
#: small enough that a broken client cannot make the server allocate much.
CHUNK_BYTES = 1024 * 1024

#: Kept as this module's public name for the ceiling; the rule itself lives in
#: :mod:`services.paths`, so there is exactly one definition of "too long".
MAX_NAME_LENGTH = MAX_NAME_BYTES

#: Suffixes a tools upload may use.  Every entry is lowercase and matched
#: case-insensitively; the catalog itself places no limit on what a tool is, so
#: this list is the one deliberate gate on the browser path (an operator can
#: still ``cp`` an exotic file straight into ``TOOLS_DIR``).
TOOL_SUFFIXES: tuple[str, ...] = (
    # Scripts and source that an operator runs directly.
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".py", ".rb", ".pl", ".js", ".mjs", ".cjs", ".ts",
    # Executables and self-extracting installers.
    ".exe", ".bin", ".run", ".appimage",
    # Archives and package formats.
    ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2",
    ".zip", ".gz", ".xz", ".bz2", ".7z",
    ".jar", ".whl", ".deb", ".rpm", ".apk",
    # Configuration and data a tool consumes.
    ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg", ".repo", ".service",
)

#: Suffixes a Docker upload may use: a ``docker save`` image tarball, a compose
#: file, or a ``foo.dockerfile``.  This is exactly the set
#: :func:`services.hub.parse_docker_filename` knows how to describe — the
#: catalog does list arbitrary files too, but publishing one through the
#: browser is not worth widening the gate for.
DOCKER_SUFFIXES: tuple[str, ...] = (
    ".tar", ".tar.gz", ".tgz",     # docker save images
    ".yml", ".yaml",               # compose files
    ".dockerfile",                 # foo.dockerfile
)

#: A Dockerfile usually carries no extension, so a bare ``Dockerfile`` (or a
#: staged ``Dockerfile.dev``) is matched by name rather than by suffix.
DOCKER_BARE_NAMES: tuple[str, ...] = ("dockerfile",)

#: The overlay file the catalogs read; uploading it would replace the operator's
#: metadata, and neither scanner lists it.
_RESERVED_NAMES: tuple[str, ...] = ("catalog.json",)

#: ``services.hub._visible`` treats these prefixes as documentation next to the
#: artifacts, not as artifacts.  Kept in sync by hand — the scanners own the
#: decision, this module only refuses to write a file they would hide.
_HIDDEN_PREFIXES: tuple[str, ...] = ("readme", "license", "changelog")


def _single_segment(value: str, *, what: str) -> str:
    """Prove *value* is one safe path segment and return it unchanged.

    The rule lives in :func:`services.paths.single_segment` — one implementation
    shared with the documentation and package-upload paths — and this wrapper
    only maps its ``ValueError`` onto the ``400`` the tools and Docker forms
    expect, so both ecosystems reject the same inputs with the same wording.
    """
    try:
        return single_segment(value, what=what)
    except ValueError as exc:
        raise BadRequestError(f"{what}不合法：{exc}") from exc


def _check_listed(name: str, *, what: str) -> None:
    """Refuse a name the catalog scanners deliberately hide."""
    lower = name.lower()
    if lower in _RESERVED_NAMES or lower.startswith(_HIDDEN_PREFIXES):
        raise BadRequestError(
            f"{what} {name!r} 会被目录扫描忽略（catalog.json 与 README/LICENSE/"
            f"CHANGELOG 属于说明文件），请改用其它名称"
        )


def _check_suffix(
    name: str,
    suffixes: tuple[str, ...],
    *,
    bare_names: tuple[str, ...] = (),
) -> None:
    """Refuse a name that does not end in an allowed suffix (or bare name)."""
    lower = name.lower()
    if any(lower.endswith(suffix) for suffix in suffixes):
        return
    if any(lower == bare or lower.startswith(f"{bare}.") for bare in bare_names):
        return
    allowed = ", ".join(suffixes)
    extra = f"；或以 {', '.join(bare_names)} 开头" if bare_names else ""
    raise BadRequestError(f"不支持的文件类型 {name!r}；允许的扩展名：{allowed}{extra}")


def tools_target(root: str, filename: str, category: str = "") -> Path:
    """The absolute path an uploaded tool will occupy under *root*.

    *category* is the catalog's immediate sub-directory; an empty value means
    the tools root, matching :func:`services.hub.scan_tools`.  Validation happens
    here, before any byte is read, so a rejected upload never creates a file.
    """
    name = _single_segment(filename, what="文件名")
    _check_listed(name, what="文件名")
    _check_suffix(name, TOOL_SUFFIXES)
    if not category:
        return contained(root, name)
    folder = _single_segment(category, what="分类名")
    _check_listed(folder, what="分类名")
    return contained(root, folder, name)


def docker_target(root: str, filename: str) -> Path:
    """The absolute path an uploaded Docker artifact will occupy under *root*."""
    name = _single_segment(filename, what="文件名")
    _check_listed(name, what="文件名")
    _check_suffix(name, DOCKER_SUFFIXES, bare_names=DOCKER_BARE_NAMES)
    return contained(root, name)


def _capped(stream, *, limit: int, name: str) -> Iterator[bytes]:
    """Yield *stream* in chunks, refusing a body above *limit* or an empty one.

    The check runs before the offending chunk is yielded, so the temp file is
    discarded and the destination is left untouched.
    """
    total = 0
    while True:
        chunk = stream.read(CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise PypiError(
                f"{name} 超过上传上限 {human_size(limit)}", status_code=413
            )
        yield chunk
    if total == 0:
        raise BadRequestError(f"{name} 是空文件")


def save(target: Path, upload, *, overwrite: bool | None = None) -> int:
    """Stream one multipart *upload* onto *target*; return the bytes written.

    An existing file is refused with ``409`` unless
    :attr:`config.StorageConfig.overwrite` is true (*overwrite* overrides the
    setting for a caller that knows better).  The read is chunked and capped at
    :attr:`config.StorageConfig.max_content_length`; the write is atomic and the
    published file is chmodded 0644 by :func:`services.fileio.atomic_write_stream`.
    """
    if target.is_dir():
        raise BadRequestError(f"{target.name!r} 已是一个目录，无法作为文件写入")
    allow_overwrite = settings.storage.overwrite if overwrite is None else overwrite
    if target.exists() and not allow_overwrite:
        raise PypiError(
            f"{target.name} 已存在；如需覆盖请设置 STORAGE__OVERWRITE=true",
            status_code=409,
        )
    return atomic_write_stream(
        target,
        _capped(
            upload.stream,
            limit=settings.storage.max_content_length,
            name=target.name,
        ),
    )


__all__ = [
    "CHUNK_BYTES",
    "MAX_NAME_LENGTH",
    "TOOL_SUFFIXES",
    "DOCKER_SUFFIXES",
    "DOCKER_BARE_NAMES",
    "docker_target",
    "save",
    "tools_target",
]
