"""Build-mirror catalogs — the JSON shape the SPA renders.

Two mirrors answer here and they are deliberately presented identically, so the
dropdown on the Python page and the one on the npm page can share a single
component:

* :mod:`index.python_build` — ``python-build-standalone`` archives, consumed by
  ``uv python install`` through ``UV_PYTHON_INSTALL_MIRROR``.
* :mod:`index.node_build` — a ``nodejs.org/dist`` mirror, consumed by ``nvm``,
  ``fnm`` and ``node-gyp`` through ``NVM_NODEJS_ORG_MIRROR``.

The filenames differ (``cpython-3.12.13+20260602-aarch64-…`` vs
``node-v20.11.0-linux-x64.tar.xz``) but a human asking "what builds do I have,
and how do I point a client at them?" asks the same question of both.  Flatten
that difference into ``label``/``platform``/``variant`` here so no route or
template has to know which mirror it is looking at.

Like every catalog in this project the filesystem is the source of truth: only
what is actually on disk is listed, so a client is never handed a URL this
server would answer with a 404.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from index.node_build import NodeBuildIndex
from index.python_build import PythonBuildIndex
from schemas import NodeBuildFile, PythonBuildFile
from services.hub import human_size

#: OpenAPI shape of the payloads below.  A plain JSON Schema rather than a
#: pydantic model: the two mirrors differ at the leaves (a CPython archive has a
#: target triple and a variant, a Node one a platform/arch and a format), and a
#: model per mirror would only restate this object.  Both catalog routes
#: reference it, so `/python-builds` and `/node-builds` document one shape.
CATALOG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "description": "`python` or `node`"},
        "root": {"type": "string"},
        "exists": {"type": "boolean"},
        "url_prefix": {"type": "string"},
        "mirror_url": {
            "type": "string",
            "description": "What a client points its mirror env var at",
        },
        "index_url": {
            "type": "string",
            "description": "The machine-facing index document",
        },
        "env_var": {"type": "string"},
        "client": {"type": "string"},
        "release_count": {"type": "integer"},
        "file_count": {"type": "integer"},
        "total_size": {"type": "integer"},
        "total_size_human": {"type": "string"},
        "releases": {"type": "array", "items": {"type": "object"}},
    },
}


def _file_urls(url_prefix: str, release_tag: str, filename: str) -> tuple[str, str]:
    """``(download_url, sha256_url)`` for one artifact, relative to this server."""
    base = f"{url_prefix.rstrip('/')}/{quote(release_tag, safe='')}/{quote(filename, safe='')}"
    return base, f"{base}/sha256"


def _python_file(url_prefix: str, bf: PythonBuildFile) -> dict[str, Any]:
    download_url, sha256_url = _file_urls(url_prefix, bf.release_tag, bf.filename)
    return {
        "filename": bf.filename,
        "release_tag": bf.release_tag,
        "version": bf.version,
        "label": f"{bf.flavor} {bf.version}",
        "platform": bf.target_triple,
        "variant": bf.variant,
        "extension": bf.extension,
        "size": bf.size,
        "size_human": human_size(bf.size),
        "download_url": download_url,
        "sha256_url": sha256_url,
        # Never computed eagerly: hashing every archive on every page load is
        # not worth it.  `sha256_url` resolves it on demand.
        "sha256": bf.sha256_digest,
    }


def _node_file(url_prefix: str, bf: NodeBuildFile) -> dict[str, Any]:
    download_url, sha256_url = _file_urls(url_prefix, bf.release_tag, bf.filename)
    platform = f"{bf.platform}-{bf.arch}" if bf.arch else bf.platform
    return {
        "filename": bf.filename,
        "release_tag": bf.release_tag,
        "version": bf.version,
        "label": f"node {bf.version}",
        "platform": platform,
        "variant": bf.extension,
        "extension": bf.extension,
        "size": bf.size,
        "size_human": human_size(bf.size),
        "download_url": download_url,
        "sha256_url": sha256_url,
        "sha256": bf.sha256_digest,
    }


def _envelope(
    *,
    kind: str,
    root: str,
    exists: bool,
    url_prefix: str,
    mirror_url: str,
    index_url: str,
    env_var: str,
    client: str,
    releases: list[dict[str, Any]],
) -> dict[str, Any]:
    files = [f for release in releases for f in release["files"]]
    total_size = sum(f["size"] or 0 for f in files)
    return {
        "kind": kind,
        "root": root,
        "exists": exists,
        "url_prefix": url_prefix.rstrip("/"),
        "mirror_url": mirror_url,
        "index_url": index_url,
        "env_var": env_var,
        "client": client,
        "releases": releases,
        "release_count": len(releases),
        "file_count": len(files),
        "total_size": total_size,
        "total_size_human": human_size(total_size),
    }


def _release(url_prefix: str, release_tag: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    total_size = sum(f["size"] or 0 for f in files)
    return {
        "release_tag": release_tag,
        "file_count": len(files),
        "total_size": total_size,
        "total_size_human": human_size(total_size),
        "files": files,
    }


def python_catalog(
    index: PythonBuildIndex,
    *,
    url_prefix: str,
    mirror_url: str,
    index_url: str,
    exists: bool,
) -> dict[str, Any]:
    """Every mirrored CPython build, grouped by ``python-build-standalone`` tag."""
    releases = [
        _release(
            url_prefix,
            release_tag,
            [_python_file(url_prefix, bf) for bf in index.get_files_for_release(release_tag) or []],
        )
        for release_tag in index.get_releases()
    ]
    return _envelope(
        kind="python",
        root=index.root,
        exists=exists,
        url_prefix=url_prefix,
        mirror_url=mirror_url,
        index_url=index_url,
        env_var="UV_PYTHON_INSTALL_MIRROR",
        client="uv",
        releases=releases,
    )


def node_catalog(
    index: NodeBuildIndex,
    *,
    url_prefix: str,
    mirror_url: str,
    index_url: str,
    exists: bool,
) -> dict[str, Any]:
    """Every mirrored Node.js build, grouped by release tag (``v20.11.0``)."""
    releases = [
        _release(
            url_prefix,
            release_tag,
            [_node_file(url_prefix, bf) for bf in index.get_files_for_release(release_tag) or []],
        )
        for release_tag in index.get_releases()
    ]
    return _envelope(
        kind="node",
        root=index.root,
        exists=exists,
        url_prefix=url_prefix,
        mirror_url=mirror_url,
        index_url=index_url,
        env_var="NVM_NODEJS_ORG_MIRROR",
        client="nvm / fnm",
        releases=releases,
    )


__all__ = ["python_catalog", "node_catalog", "CATALOG_SCHEMA"]
