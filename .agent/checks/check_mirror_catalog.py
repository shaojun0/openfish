#!/usr/bin/env python
"""Gate: the mirror catalogs' overlay is a table, and a fresh database seeds it.

Run from the backend directory::

    python scripts/check_mirror_catalog.py

Until recently npm / debian / docker-images were "the directory is the catalog,
plus an optional ``catalog.json`` beside the files".  The metadata is now
``catalog_entries`` rows in the namespaces ``npm`` / ``debian`` /
``docker-images`` (``services.mirror_catalog``), and a mirror row never owns
bytes: ``storage_key`` is the empty string and ``filename`` is empty on a row
that only registers metadata.  The shipped defaults install through
``services.catalog_seed`` at database initialization, and the three mirror
catalogs seed rows only.

That buys three properties this gate is here to keep, none of which the route
tests cover:

1. **The overlay file is a format, not a source of truth.**  ``overlay`` builds
   the dict the scanners merge, ``path_for`` is the row identity, and
   ``import_file`` / ``export_file`` round-trip the file without the request
   path ever reading it.  A ``catalog.json`` sitting in the directory must no
   longer change a single byte of what ``/api/v1/npm|debian|docker`` serves.
2. **The scanners merge rows and files the way the UI assumes.**  A row that
   names a file overrides that file's metadata, a row with no file is
   metadata-only, a file with no row is described by its filename, and the
   metadata-only entries keep the order they were written in.
3. **First-run seeding and old-database upgrades work.**  A fresh database gets
   the five catalogs plus five ``catalog_seed_state`` rows, a second start is a
   no-op, deleting the defaults keeps them deleted, ``force=True`` installs them
   back without duplicates or orphaned objects, and a database created by the
   *pre-overlay* code opens, gains the three new columns, and seeds.

The last section pins the contract ``cli.py catalogs import`` has to keep: two
items with one identity merge (last wins) instead of failing the whole import,
a missing or corrupt file is a hard error that never prunes, and ``unnamed`` is
a legal package name.  Those checks are the desired contract rather than a
description of the tree, so they fail on a checkout where the fix is not in yet.

The endpoint section compares this tree's ``app.test_client()`` responses,
byte for byte, against the *pre-change tree*, which this script fetches itself
from the repository under test::

    git -C <repo> archive 9518be1 backend docker/examples | tar -x

That is the whole point of the section, and a run must not depend on a previous
run's leftovers to get there — the gate has to reproduce on a machine that has
never seen this working copy.  A checkout the caller already extracted may be
pointed at with ``BASELINE_BACKEND_DIR``.  When neither works (no ``git``, no
such commit) the endpoint and old-database sections print a ⚠ naming what they
skipped and the script still exits 0 with the reduced count: a missing baseline
is a runnable reduced gate, never a traceback.

One trap this script has to step around: the catalog scanners hide any file
whose *absolute* path has a dot-prefixed component, so fixtures placed under a
scratch directory like ``/home/me/.cache/…`` would look invisible and fail every
scanner check.  The scanner and endpoint fixtures therefore live under a
dot-free directory chosen at start-up (``_FIXTURES``), never under the dotted
scratch root.

Environment overrides (nothing is hard-coded to one worktree):

``BACKEND_DIR``             the tree under test (default: this script's backend)
``BASELINE_BACKEND_DIR``    an already-extracted pre-change checkout
``MIRROR_BASELINE_COMMIT``  the commit whose tree is the pre-change baseline
``MIRROR_SCRATCH``          where the throwaway databases and overlays go
``MIRROR_PYTHON``           interpreter for the subprocess runs (default: this one)

Exits non-zero on the first property that fails.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

def _default_backend(start: Path) -> Path:
    """The backend directory of the checkout this script lives in.

    The script is a suite artifact under ``.agent/checks/``, not application
    code under ``backend/scripts/``, so it cannot assume a fixed distance from
    the package: walk up until a ``backend/config/seed`` turns up and fall back
    to the old ``<here>/..`` layout for a checkout that keeps it elsewhere.
    """
    for parent in (start, *start.parents):
        candidate = parent / "backend"
        if (candidate / "config" / "seed").is_dir():
            return candidate
    return start.parent.parent


BACKEND_DIR = Path(
    os.environ.get("BACKEND_DIR") or _default_backend(Path(__file__).resolve())
).resolve()
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

#: The commit ``docker/examples/*/catalog.json`` and the pre-overlay mirror code
#: last existed in.  Overridable for a future baseline.
BASELINE_COMMIT = os.environ.get("MIRROR_BASELINE_COMMIT") or "9518be1"


def _dot_free_root(*candidates: Path) -> Path | None:
    """The first candidate whose absolute path has no dot-prefixed component.

    ``services.hub._visible_names`` refuses a file when *any* part of its path
    starts with ``.`` — including the scratch directories above it.  A fixture
    tree under ``/home/me/.cache`` would therefore be invisible to the very
    scanners under test, and the gate would report production failures that are
    its own.  ``None`` means no such directory is available and the sections
    that scan a directory have to be skipped.
    """
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - unresolvable path
            continue
        if not any(part.startswith(".") for part in resolved.parts):
            return resolved
    return None


# The app reads its configuration once, at import — so the throwaway roots and
# every switch that could leak in from the outside have to be set before anything
# under `config`/`services` is imported.  `SEED_CATALOGS` in particular: a shell
# that exported it as 0 would otherwise turn the seeding section into a false
# failure instead of a fixture.
_SCRATCH = Path(os.environ.get("MIRROR_SCRATCH") or (BACKEND_DIR.parent / "scratch"))
_SCRATCH.mkdir(parents=True, exist_ok=True)
_TMP = Path(tempfile.mkdtemp(prefix="mirror-check-", dir=_SCRATCH))

#: Where every directory the *scanners* read is built: the scan fixtures, the
#: endpoint directories and the self-fetched baseline.  Kept apart from ``_TMP``
#: because ``MIRROR_SCRATCH`` may be a dotted path.
_FIXTURE_BASE = _dot_free_root(_SCRATCH, Path(tempfile.gettempdir()), BACKEND_DIR.parent)
_FIXTURES = (
    Path(tempfile.mkdtemp(prefix="mirror-fixtures-", dir=_FIXTURE_BASE))
    if _FIXTURE_BASE is not None
    else None
)

os.environ["SEED_CATALOGS"] = "1"
os.environ["OBJECT_BACKEND"] = "local"
os.environ.pop("DATABASE_URL", None)
os.environ["API_KEYS_FILE"] = str(_TMP / "gate.db")
os.environ["DOCS_DIR"] = str(_TMP / "docs")
os.environ["TOOLS_DIR"] = str(_TMP / "tools")
os.environ["NPM_DIR"] = str(_TMP / "npm")
os.environ["DEBIAN_DIR"] = str(_TMP / "debian")
os.environ["DOCKER_DIR"] = str(_TMP / "docker-images")
os.environ["PACKAGES_DIR"] = str(_TMP / "packages")

from sqlalchemy import create_engine, func, inspect, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from unittest import mock  # noqa: E402

from extensions.database import init_engine  # noqa: E402
from models.base import Base  # noqa: E402
from models.catalog import CatalogEntry, CatalogSeedState  # noqa: E402
from models.docs import Document, DocumentAsset, DocumentRevision  # noqa: E402
from services import catalog_seed, hub, mirror_catalog, objectstore  # noqa: E402

PYTHON = os.environ.get("MIRROR_PYTHON") or sys.executable

failures: list[str] = []
checks = 0


def check(ok: bool, label: str) -> None:
    global checks
    checks += 1
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


# ── Small fixtures ───────────────────────────────────────────────────

def _wipe(session, namespace: str) -> None:
    session.query(CatalogEntry).filter(CatalogEntry.namespace == namespace).delete()
    session.commit()


def _write_overlay(path: Path, namespace: str, items: list[dict]) -> Path:
    path.write_text(
        json.dumps({mirror_catalog.top_key(namespace): items}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _import(session, namespace: str, path: Path, **kwargs) -> mirror_catalog.OverlayReport:
    return mirror_catalog.import_file(session, namespace, path, **kwargs)


def _tree_env(
    backend_dir: Path,
    db_path: Path,
    dirs: Path,
    *,
    tag: str,
    seed: str = "1",
) -> dict[str, str]:
    """The environment one subprocess run of *backend_dir*'s app gets."""
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = str(backend_dir)
    env["API_KEYS_FILE"] = str(db_path)
    env["SEED_CATALOGS"] = seed
    env["OBJECT_BACKEND"] = "local"
    env["PACKAGES_DIR"] = str(_TMP / f"sub-{tag}-packages")
    env["TOOLS_DIR"] = str(_TMP / "tools")
    env["DOCS_DIR"] = str(_TMP / "docs")
    env["ADMIN_USERS"] = '["e2e"]'
    env["AUTH_USERNAME"] = "e2e"
    env["AUTH_ASSERT"] = "pw"
    env["OAUTH2_INTROSPECT_URL"] = ""
    env["OAUTH2_AUTHORIZE_URL"] = ""
    env["NPM_DIR"] = str(dirs / "npm")
    env["DEBIAN_DIR"] = str(dirs / "debian")
    env["DOCKER_DIR"] = str(dirs / "docker-images")
    return env


def _run_code(
    backend_dir: Path,
    code: str,
    env: dict[str, str],
    label: str,
    args: list[str] | None = None,
) -> str:
    proc = subprocess.run(
        [PYTHON, "-c", code, *(args or [])],
        cwd=str(backend_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{label} failed (exit {proc.returncode}):\n{proc.stderr[-1500:]}"
        )
    return proc.stdout


# ── The endpoint comparison's subprocess programs ────────────────────
#
# Plain `-c` programs, so no helper file has to exist in a checkout that is not
# this one (the baseline tree has no copy of this gate).  `-c` also puts the
# working directory — the tree — first on ``sys.path``, which is what makes the
# run use *that* tree even though the interpreter's editable install points at
# the main checkout; each program reports the module file it loaded so the
# caller can prove it.

_ENDPOINT_CODE = r'''
import base64
import json
import sys

import app as app_module
import config as config_module
from app import app

client = app.test_client()
auth = base64.b64encode(b"e2e:pw").decode()
headers = {"Authorization": "Basic " + auth}
out = {"_app": app_module.__file__, "_config": config_module.__file__}
for path in ("/api/v1/npm", "/api/v1/debian", "/api/v1/docker"):
    response = client.get(path, headers=headers)
    out[path] = {"status": response.status_code, "body": response.get_data(as_text=True)}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(out, handle, ensure_ascii=False, indent=1, sort_keys=True)
'''

_EMPTY_SEED_CODE = r'''
import json

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from extensions.database import init_engine
from models.catalog import CatalogEntry, CatalogSeedState
from models.docs import Document

engine = init_engine()
session_factory = sessionmaker(bind=engine)
with session_factory() as session:
    print(json.dumps({
        "entries": session.scalar(select(func.count()).select_from(CatalogEntry)),
        "documents": session.scalar(select(func.count()).select_from(Document)),
        "seed_state": session.scalar(select(func.count()).select_from(CatalogSeedState)),
    }))
'''

_LEGACY_CREATE_CODE = r'''
import json

from sqlalchemy import inspect

import extensions.database as database_module
from extensions.database import init_engine

engine = init_engine()
inspector = inspect(engine)
with engine.connect() as connection:
    print(json.dumps({
        "module": database_module.__file__,
        "columns": [item["name"] for item in inspector.get_columns("catalog_entries")],
        "seed_namespaces": sorted(
            row[0] for row in connection.exec_driver_sql(
                "SELECT namespace FROM catalog_seed_state"
            )
        ),
        "entry_counts": {
            row[0]: row[1] for row in connection.exec_driver_sql(
                "SELECT namespace, COUNT(*) FROM catalog_entries GROUP BY namespace"
            )
        },
    }))
'''


def _run_endpoint_dump(
    backend_dir: Path,
    out_path: Path,
    tag: str,
    dirs: Path,
    *,
    db_path: Path | None = None,
) -> dict:
    db = Path(db_path) if db_path is not None else (_TMP / f"endpoint-{tag}.db")
    env = _tree_env(backend_dir, db, dirs, tag=f"endpoint-{tag}")
    _run_code(backend_dir, _ENDPOINT_CODE, env, f"endpoint dump ({tag})", [str(out_path)])
    return json.loads(out_path.read_text(encoding="utf-8"))


def _run_empty_seed_probe() -> dict:
    db_path = _TMP / "empty-seed.db"
    env = _tree_env(BACKEND_DIR, db_path, _TMP, tag="empty-seed", seed="0")
    stdout = _run_code(BACKEND_DIR, _EMPTY_SEED_CODE, env, "SEED_CATALOGS=0 probe")
    return json.loads(stdout.strip().splitlines()[-1])


# ── 1. The overlay shape ─────────────────────────────────────────────

def _check_overlay(session) -> None:
    section("overlay()")
    check(mirror_catalog.top_key("npm") == "packages", "npm's overlay key is `packages`")
    check(
        mirror_catalog.top_key("debian") == "artifacts"
        and mirror_catalog.top_key("docker-images") == "artifacts",
        "the flat mirrors' overlay key is `artifacts`",
    )
    try:
        mirror_catalog.overlay(session, "docker")
        check(False, "an unknown namespace is refused")
    except ValueError as exc:
        check("docker" in str(exc) and "docker-images" in str(exc),
              "an unknown namespace is refused with the known ones named")

    _wipe(session, "npm")
    _import(session, "npm", _write_overlay(_TMP / "o-npm.json", "npm", [
        {"name": "alpha", "version": "1.2.3", "description": "the alpha", "tags": ["t1", "t2"]},
        {"name": "beta"},
        {"name": "gamma", "filename": "gamma-1.0.0.tgz"},
    ]))
    payload = mirror_catalog.overlay(session, "npm")
    check(set(payload) == {"packages"}, "npm's overlay is a single `packages` list")
    items = payload["packages"]
    check([item["name"] for item in items] == ["alpha", "beta", "gamma"],
          "overlay() lists rows in insertion order")
    check(items[0]["version"] == "1.2.3" and items[0]["description"] == "the alpha",
          "name/version/description map onto the overlay keys")
    check(items[0]["tags"] == ["t1", "t2"], "tags survive as a list")
    check("arch" not in items[0] and "kind" not in items[0],
          "an npm item carries neither arch nor kind")
    check("version" not in items[1] and "description" not in items[1],
          "an absent version/description is left out of the item")
    check(items[1]["tags"] == [], "an entry with no tags reports an empty list")
    check("filename" not in items[1],
          "an empty filename never reaches the overlay")
    check("storage_key" not in items[0] and items[0].get("filename") is None,
          "a mirror row's storage_key is not part of the overlay")
    check(items[2].get("filename") == "gamma-1.0.0.tgz",
          "a row that names a file carries the filename")
    rows = mirror_catalog.entries(session, "npm")
    check(all(row.storage_key == "" for row in rows),
          "every npm row has an empty storage_key")

    _wipe(session, "debian")
    _import(session, "debian", _write_overlay(_TMP / "o-deb.json", "debian", [
        {"filename": "curl_8.5.0_arm64.deb", "name": "curl", "version": "8.5.0",
         "arch": "arm64", "kind": "deb", "description": "d", "tags": ["net"]},
        {"name": "meta", "version": "9.9", "arch": "all", "kind": "deb"},
    ]))
    payload = mirror_catalog.overlay(session, "debian")
    check(set(payload) == {"artifacts"}, "a flat mirror's overlay is a single `artifacts` list")
    flat = payload["artifacts"]
    check(flat[0]["arch"] == "arm64" and flat[0]["kind"] == "deb",
          "a flat item carries arch and kind")
    check(flat[0]["tags"] == ["net"] and flat[0]["version"] == "8.5.0",
          "a flat item carries version and tags")
    check("filename" not in flat[1], "an absent flat filename is left out of the item")
    check(flat[1]["arch"] == "all" and flat[1]["kind"] == "deb",
          "an explicit arch and kind are kept")


# ── 2. Row identity ──────────────────────────────────────────────────

def _check_path_for() -> None:
    section("path_for()")
    check(mirror_catalog.path_for("debian", {"filename": "a_1.0.0_all.deb", "name": "a",
                                            "version": "1.0.0", "arch": "all"})
          == "a_1.0.0_all.deb",
          "a filename is the identity when one is named")
    check(mirror_catalog.path_for("npm", {"name": "lodash"}) == "lodash",
          "npm without a filename identifies by package name")
    check(mirror_catalog.path_for("npm", {"name": "lodash", "version": "4.17.21"}) == "lodash",
          "npm's version does not enter the identity")
    check(mirror_catalog.path_for("docker-images", {"name": "nginx", "version": "1.25.3"})
          == "nginx:1.25.3",
          "a flat mirror without a filename identifies by name:version")
    check(mirror_catalog.path_for("debian", {"name": "curl", "version": "8.5.0", "arch": "arm64"})
          == "curl_8.5.0_arm64",
          "an arch turns the flat identity into name_version_arch")
    check(mirror_catalog.path_for("debian", {"name": "curl", "arch": "arm64"}) == "curl_arm64",
          "an arch without a version is still name_arch")
    check(mirror_catalog.path_for("debian", {"name": "x", "filename": "   "}) == "x",
          "a whitespace-only filename is not an identity")


# ── 3. Import ────────────────────────────────────────────────────────

def _check_import(session) -> None:
    section("import_file()")
    _wipe(session, "npm")
    path = _write_overlay(_TMP / "i-npm.json", "npm", [
        {"name": "one", "version": "1.0.0", "description": "first", "tags": ["a"]},
        {"name": "two", "version": "2.0.0"},
    ])
    report = _import(session, "npm", path)
    check((report.created, report.updated, report.unchanged) == (2, 0, 0),
          "a first import creates every row")
    check(len(mirror_catalog.entries(session, "npm")) == 2, "the rows are really there")

    report = _import(session, "npm", path)
    check((report.created, report.updated, report.unchanged) == (0, 0, 2),
          "re-importing an unchanged file reports everything unchanged")
    check(len(mirror_catalog.entries(session, "npm")) == 2,
          "re-importing does not duplicate rows")

    _write_overlay(_TMP / "i-npm.json", "npm", [
        {"name": "one", "version": "1.0.0", "description": "changed", "tags": ["a"]},
        {"name": "two", "version": "2.0.0"},
    ])
    report = _import(session, "npm", path)
    check((report.updated, report.unchanged) == (1, 1), "one changed row is updated")
    check(mirror_catalog.entries(session, "npm")[0].description == "changed",
          "the update reaches the column")

    _write_overlay(_TMP / "i-npm.json", "npm", [
        {"name": "one", "version": "1.0.0", "description": "changed", "tags": ["b", "c"]},
        {"name": "two", "version": "2.0.0"},
    ])
    report = _import(session, "npm", path)
    check(report.updated == 1 and mirror_catalog.entries(session, "npm")[0].tag_list() == ["b", "c"],
          "tags are replaced, not merged")

    _write_overlay(_TMP / "i-npm.json", "npm", [
        {"name": "one", "version": "1.0.0", "description": "changed", "tags": ["b", "c"]},
        {"name": "two", "version": "2.0.0"},
        {"name": "three", "version": "3.0.0"},
    ])
    report = _import(session, "npm", path, dry_run=True)
    check(report.created == 1 and len(mirror_catalog.entries(session, "npm")) == 2,
          "--dry-run reports the create without writing it")

    report = _import(session, "npm", path, prune=True, dry_run=True)
    check(report.pruned == 0, "--dry-run prunes nothing while the file is the same")

    _write_overlay(_TMP / "i-npm.json", "npm", [{"name": "one", "version": "1.0.0"}])
    report = _import(session, "npm", path, prune=True)
    check(report.pruned == 1 and len(mirror_catalog.entries(session, "npm")) == 1,
          "--prune deletes the row the file no longer names")
    check(mirror_catalog.entries(session, "npm")[0].path == "one",
          "the surviving row is the one the file names")

    # A missing or corrupt file is a hard error, and a duplicate identity merges:
    # both are asserted in the "CLI import contract" section below, next to the
    # `unnamed` package, rather than here.

    bad = _TMP / "unknown.json"
    _write_overlay(bad, "npm", [{"name": "x"}])
    try:
        _import(session, "docker", bad)
        check(False, "an unknown namespace raises ValueError")
    except ValueError:
        check(True, "an unknown namespace raises ValueError")
    try:
        mirror_catalog.export_file(session, "docker", _TMP / "unknown.json")
        check(False, "export refuses an unknown namespace too")
    except ValueError:
        check(True, "export refuses an unknown namespace too")


# ── 4. Export ────────────────────────────────────────────────────────

def _check_export(session) -> None:
    section("export_file()")
    for namespace in mirror_catalog.MIRRORS:
        _wipe(session, namespace)
        _import(session, namespace, _write_overlay(
            _TMP / f"rt-{namespace}.json", namespace,
            [
                {"name": "one", "version": "1.0.0", "description": "d1", "tags": ["x"]},
                {"name": "two", "version": "2.0.0", "arch": "arm64", "kind": "deb"},
            ] if namespace != "npm" else [
                {"name": "one", "version": "1.0.0", "description": "d1", "tags": ["x"]},
                {"name": "two", "version": "2.0.0"},
            ],
        ))

    original = {namespace: mirror_catalog.overlay(session, namespace)
                for namespace in mirror_catalog.MIRRORS}
    out = _TMP / "export"
    for namespace in mirror_catalog.MIRRORS:
        report = mirror_catalog.export_file(session, namespace, out / f"{namespace}.json")
        payload = json.loads((out / f"{namespace}.json").read_text(encoding="utf-8"))
        check(report.exported == 2 and set(payload) == {mirror_catalog.top_key(namespace)},
              f"{namespace}: export writes the two rows under the file's key")
        check(len(payload[mirror_catalog.top_key(namespace)]) == 2,
              f"{namespace}: export writes one item per row")

    check(json.loads((out / "npm.json").read_text(encoding="utf-8"))["packages"][0]["tags"] == ["x"],
          "export writes tags back out as a list")
    check("arch" not in json.loads((out / "npm.json").read_text(encoding="utf-8"))["packages"][0],
          "export does not invent arch/kind for npm")

    fresh = create_engine(f"sqlite:///{_TMP / 'roundtrip.db'}")
    Base.metadata.create_all(fresh)
    Fresh = sessionmaker(bind=fresh, expire_on_commit=False)
    with Fresh() as other:
        for namespace in mirror_catalog.MIRRORS:
            mirror_catalog.import_file(other, namespace, out / f"{namespace}.json")
    with Fresh() as other:
        for namespace in mirror_catalog.MIRRORS:
            check(mirror_catalog.overlay(other, namespace) == original[namespace],
                  f"{namespace}: export -> import reproduces the same overlay")

    _wipe(session, "docker-images")
    empty = out / "empty.json"
    report = mirror_catalog.export_file(session, "docker-images", empty)
    payload = json.loads(empty.read_text(encoding="utf-8"))
    check(report.exported == 0 and payload == {"artifacts": []},
          "an empty namespace exports an empty list")
    with Fresh() as other:
        seeded = mirror_catalog.import_file(other, "docker-images", empty)
    check(seeded.created == 0, "an empty file imports nothing")


# ── 5. The scanners ──────────────────────────────────────────────────

def _find(items: list[dict], key: str, value: str) -> dict:
    """The one item whose *key* is *value*, or an empty dict.

    A lookup that misses is a failed check, never a ``KeyError``: this gate is
    also run cold, where a surprise should be reported, not raised.
    """
    for item in items:
        if item.get(key) == value:
            return item
    return {}


def _check_scanners(session) -> None:
    section("hub.scan_npm()")
    if _FIXTURES is None:
        print("   ⚠ no dot-free directory for scanner fixtures — section skipped")
        return
    npm_dir = _FIXTURES / "scan-npm"
    npm_dir.mkdir(parents=True, exist_ok=True)
    (npm_dir / "widget-1.0.0.tgz").write_bytes(b"tgz-widget")
    (npm_dir / "gadget-2.0.0.tgz").write_bytes(b"tgz-gadget")
    (npm_dir / "catalog.json").write_text(
        json.dumps({"packages": [{"name": "from-file", "version": "0.0.1"}]}),
        encoding="utf-8",
    )
    (npm_dir / "README.txt").write_text("not a package", encoding="utf-8")

    bare = hub.scan_npm(str(npm_dir), url_prefix="/npm/files")
    check(bare["package_count"] == 2
          and sorted(item["name"] for item in bare["packages"]) == ["gadget", "widget"],
          "without an overlay only the two tarballs are listed")
    check(all("from-file" != item["name"] for item in bare["packages"]),
          "a catalog.json in the directory is not read at all")

    _wipe(session, "npm")
    _import(session, "npm", _write_overlay(_TMP / "s-npm.json", "npm", [
        {"name": "zzz-meta", "version": "3.0.0", "description": "metadata zzz", "tags": ["z"]},
        {"name": "aaa-meta", "version": "4.0.0"},
        {"name": "widget", "version": "1.0.0", "filename": "widget-1.0.0.tgz",
         "description": "bound to the file by name"},
        {"name": "ghost", "filename": "ghost-9.9.9.tgz"},
    ]))
    payload = hub.scan_npm(
        str(npm_dir), overlay=mirror_catalog.overlay(session, "npm"), url_prefix="/npm/files",
    )
    by_name = {item["name"]: item for item in payload["packages"]}
    check("from-file" not in by_name, "the directory's catalog.json still changes nothing")
    widget = _find(payload["packages"], "name", "widget")
    zzz = _find(payload["packages"], "name", "zzz-meta")
    ghost = _find(payload["packages"], "name", "ghost")
    check(widget.get("description") == "bound to the file by name",
          "a row is merged onto the tarball by package name")
    check(widget.get("size") is not None
          and str(widget.get("download_url") or "").endswith("/widget-1.0.0.tgz"),
          "a file-backed entry reports its size and download URL")
    check(zzz.get("download_url") is None and zzz.get("size") is None,
          "a metadata-only row has nothing to download")
    check(zzz.get("tags") == ["z"] and zzz.get("version") == "3.0.0",
          "a metadata-only row keeps its tags and version")
    check(ghost.get("filename") == "ghost-9.9.9.tgz",
          "a row whose file is absent is reported with its filename")
    order = [item["name"] for item in payload["packages"]]
    check("zzz-meta" in order and "aaa-meta" in order
          and order.index("zzz-meta") < order.index("aaa-meta"),
          "metadata-only rows keep insertion order, not path order")
    check(payload["package_count"] == len(payload["packages"]),
          "package_count matches the list")
    missing = hub.scan_npm(str(_TMP / "no-such-npm"))
    check(missing["exists"] is False and missing["packages"] == [],
          "a missing npm directory degrades to an empty catalog")

    section("hub.scan_flat()")
    flat_dir = _FIXTURES / "scan-debian"
    flat_dir.mkdir(parents=True, exist_ok=True)
    (flat_dir / "curl_8.5.0_arm64.deb").write_bytes(b"deb-curl")
    (flat_dir / "sources.list.example").write_text("deb http://x\n", encoding="utf-8")
    (flat_dir / "README.txt").write_text("docs, not an artifact", encoding="utf-8")
    (flat_dir / "catalog.json").write_text(
        json.dumps({"artifacts": [{"filename": "curl_8.5.0_arm64.deb", "name": "from-file"}]}),
        encoding="utf-8",
    )

    bare = hub.scan_debian(str(flat_dir), url_prefix="/debian/files", mirror="")
    names = [item["name"] for item in bare["artifacts"]]
    check("curl" in names and "from-file" not in names,
          "flat scanning derives metadata from the filename and ignores catalog.json")
    check(any(item["filename"] == "sources.list.example" for item in bare["artifacts"]),
          "a file with no row is still listed")
    check(all(item["filename"] != "catalog.json" for item in bare["artifacts"]),
          "the overlay file itself is never an artifact")

    _wipe(session, "debian")
    _import(session, "debian", _write_overlay(_TMP / "s-deb.json", "debian", [
        {"filename": "curl_8.5.0_arm64.deb", "name": "Curl Display", "version": "8.5.0",
         "arch": "arm64", "kind": "deb", "description": "overridden", "tags": ["net"]},
        {"name": "meta-zzz", "version": "9.9", "arch": "all", "kind": "deb"},
        {"name": "meta-aaa", "version": "1.1", "arch": "amd64", "kind": "deb"},
        {"filename": "absent_1.0.0_all.deb", "name": "absent", "version": "1.0.0",
         "arch": "all", "kind": "deb"},
    ]))
    payload = hub.scan_debian(
        str(flat_dir), overlay=mirror_catalog.overlay(session, "debian"),
        url_prefix="/debian/files", mirror="",
    )
    by_filename = {item["filename"]: item for item in payload["artifacts"] if item["filename"]}
    bound = by_filename.get("curl_8.5.0_arm64.deb") or {}
    check(bound.get("name") == "Curl Display" and bound.get("description") == "overridden"
          and bound.get("tags") == ["net"],
          "a row naming a file overrides that file's metadata")
    check(str(bound.get("download_url") or "").endswith("/curl_8.5.0_arm64.deb")
          and bound.get("size") is not None and bound.get("sha256"),
          "the file-backed entry keeps its size, digest and URL")
    absent = by_filename.get("absent_1.0.0_all.deb") or {}
    check(bool(absent) and absent.get("download_url") is None,
          "a row whose file is absent is metadata-only")
    order = [item["name"] for item in payload["artifacts"]]
    check("meta-zzz" in order and "meta-aaa" in order
          and order.index("meta-zzz") < order.index("meta-aaa"),
          "flat metadata-only rows keep insertion order")
    check(payload["artifact_count"] == len(payload["artifacts"]),
          "artifact_count matches the list")

    docker_dir = _FIXTURES / "scan-docker"
    docker_dir.mkdir(parents=True, exist_ok=True)
    (docker_dir / "nginx-1.25.3.tar").write_bytes(b"tar")
    (docker_dir / "docker-compose.example.yml").write_text("services: {}\n", encoding="utf-8")
    docker = hub.scan_docker(str(docker_dir), url_prefix="/docker/files", registry="")
    kinds = {item["filename"]: item["kind"] for item in docker["artifacts"]}
    check(kinds.get("nginx-1.25.3.tar") == "image"
          and kinds.get("docker-compose.example.yml") == "compose",
          "scan_docker parses image tarballs and compose files")


# ── 5b. The import command's contract ────────────────────────────────

def _check_cli_fixes(session) -> None:
    """The behaviour ``cli.py catalogs import`` has to keep.

    F1: one overlay file naming the same identity twice (npm's ``path_for``
        ignores the version, so two versions of one package collide) merges —
        the last item wins, the import does not fail, and the table never holds
        a half-imported catalog.
    F2: a missing or corrupt file is a hard error, not an empty overlay: with a
        parse failure ``--prune`` would otherwise delete every row in the
        namespace and report success.  Rows must survive untouched.
    F3: ``unnamed`` is a legal package name, so a fallback identity may not
        double as a sentinel that silently drops the row.

    These are the contract, not a description of today's tree: on a checkout
    that has not had the fix they fail on purpose.
    """
    section("CLI import contract (F1/F2/F3)")
    good = _write_overlay(_TMP / "fix-good.json", "npm",
                          [{"name": "alpha"}, {"name": "beta"}])

    # ── F1: last item wins for one identity ──────────────────────────
    _wipe(session, "npm")
    _import(session, "npm", good)
    duplicate = _write_overlay(_TMP / "fix-dup.json", "npm", [
        {"name": "pkg", "version": "1.0.0"},
        {"name": "pkg", "version": "2.0.0"},
    ])
    raised: Exception | None = None
    try:
        _import(session, "npm", duplicate)
    except Exception as exc:  # noqa: BLE001 - the failure is the check
        raised = exc
        session.rollback()
    check(raised is None,
          f"[F1] a duplicate identity no longer fails the import ({type(raised).__name__})"
          if raised else "[F1] a duplicate identity no longer fails the import")
    rows = list(mirror_catalog.entries(session, "npm"))
    merged = [row for row in rows if row.path == "pkg"]
    check({row.path for row in rows} == {"alpha", "beta", "pkg"},
          "[F1] the previous rows survive and exactly one merged row appears")
    check(len(merged) == 1 and merged[0].version == "2.0.0",
          "[F1] the last item for one identity wins, once")

    # ── F2: a missing/corrupt file never prunes ──────────────────────
    missing = _TMP / "fix-missing.json"
    broken = _TMP / "fix-broken.json"
    broken.write_text("{ this is not json", encoding="utf-8")

    def reset_rows() -> int:
        _wipe(session, "npm")
        _import(session, "npm", good)
        return len(mirror_catalog.entries(session, "npm"))

    cases = (
        ("a missing file with --prune", missing, {"prune": True}),
        ("a missing file without --prune", missing, {}),
        ("a missing file with --dry-run", missing, {"dry_run": True}),
        ("a corrupt file with --prune", broken, {"prune": True}),
        ("a corrupt file without --prune", broken, {}),
    )
    for label, source, kwargs in cases:
        before = reset_rows()
        outcome: Exception | None = None
        try:
            _import(session, "npm", source, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the failure is the check
            outcome = exc
            session.rollback()
        check(isinstance(outcome, ValueError),
              f"[F2] {label} raises ValueError ({type(outcome).__name__})"
              if outcome else f"[F2] {label} raises ValueError (no exception)")
        check(len(mirror_catalog.entries(session, "npm")) == before,
              f"[F2] {label} leaves every row in place")

    # ── F3: a package actually named `unnamed` ───────────────────────
    fix_db = _TMP / "unnamed.db"
    fix_engine = init_engine(f"sqlite:///{fix_db}")
    Fix = sessionmaker(bind=fix_engine, expire_on_commit=False)
    unnamed = _write_overlay(_TMP / "fix-unnamed.json", "npm",
                             [{"name": "unnamed", "version": "1.0.0"}])
    with Fix() as other:
        _wipe(other, "npm")
        report = mirror_catalog.import_file(other, "npm", unnamed)
        paths = [row.path for row in mirror_catalog.entries(other, "npm")]
        names = [item.get("name") for item in mirror_catalog.overlay(other, "npm")["packages"]]
    check(report.created == 1, "[F3] a package actually named `unnamed` is imported")
    check(paths == ["unnamed"], "[F3] its row identity is `unnamed`")
    check("unnamed" in names, "[F3] it appears in the npm overlay")

    unnamed_dirs = _FIXTURES / "unnamed-dirs" if _FIXTURES is not None else None
    if unnamed_dirs is None:
        print("   ⚠ no dot-free directory for the endpoint fixture — "
              "the /api/v1/npm check is skipped")
    else:
        for namespace in ("npm", "debian", "docker-images"):
            (unnamed_dirs / namespace).mkdir(parents=True, exist_ok=True)
        dump = _run_endpoint_dump(BACKEND_DIR, _TMP / "endpoint-unnamed.json", "unnamed",
                                  unnamed_dirs, db_path=fix_db)
        check(dump["/api/v1/npm"]["status"] == 200, "[F3] /api/v1/npm still answers 200")
        served = json.loads(dump["/api/v1/npm"]["body"])
        check(any(item.get("name") == "unnamed" for item in served.get("packages", [])),
              "[F3] the package named `unnamed` is served by /api/v1/npm")


# ── 6. Endpoints versus the pre-change tree ─────────────────────────

def _repo_root() -> Path | None:
    """The git work tree that holds ``BASELINE_COMMIT``, or ``None``."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(BACKEND_DIR.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    root = Path(proc.stdout.strip())
    return root if root.is_dir() else None


def _prepare_baseline() -> tuple[Path, Path] | None:
    """The pre-change ``(backend, docker/examples)``, or ``None``.

    ``BASELINE_BACKEND_DIR`` is honoured when the caller has already extracted a
    checkout.  Otherwise the tree is fetched from the repository under test with
    ``git archive <BASELINE_COMMIT> backend docker/examples`` — so a cold run on
    a machine that has never seen this gate still gets the full comparison
    instead of depending on a previous run's cache.  Everything is unpacked
    under the dot-free ``_FIXTURES``, because the example directories are served
    by the app and therefore scanned.
    """
    configured = os.environ.get("BASELINE_BACKEND_DIR")
    if configured:
        backend = Path(configured).resolve()
        examples = backend.parent / "docker" / "examples"
        if (backend / "app.py").is_file() and examples.is_dir():
            return backend, examples
        return None
    if _FIXTURES is None:
        return None
    root = _repo_root()
    if root is None:
        return None

    target = _FIXTURES / "baseline"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "archive", BASELINE_COMMIT,
             "backend", "docker/examples"],
            capture_output=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as archive:
            archive.extractall(target, filter="data")
    except TypeError:  # pragma: no cover - Python without the extraction filter
        with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as archive:
            archive.extractall(target)
    except tarfile.TarError:
        return None
    backend = target / "backend"
    examples = target / "docker" / "examples"
    if not (backend / "app.py").is_file() or not examples.is_dir():
        return None
    return backend, examples


def _copy_example_dirs(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for namespace in ("npm", "debian", "docker-images"):
        shutil.copytree(source / namespace, target / namespace)


def _remove_catalog_json(dirs: Path) -> None:
    for namespace in ("npm", "debian", "docker-images"):
        (dirs / namespace / "catalog.json").unlink(missing_ok=True)


def _restore_catalog_json(pristine: Path, dirs: Path) -> None:
    for namespace in ("npm", "debian", "docker-images"):
        shutil.copy2(pristine / namespace / "catalog.json", dirs / namespace / "catalog.json")


def _write_bogus_catalog_json(dirs: Path) -> None:
    """A file an old deployment *would* have served, to prove it is not read."""
    for namespace, key in (("npm", "packages"), ("debian", "artifacts"),
                           ("docker-images", "artifacts")):
        (dirs / namespace / "catalog.json").write_text(
            json.dumps({key: [{"name": "bogus-from-file",
                               "filename": "bogus-from-file.bin",
                               "description": "must never be served"}]}),
            encoding="utf-8",
        )


_ENDPOINT_PATHS = ("/api/v1/npm", "/api/v1/debian", "/api/v1/docker")


def _check_endpoints(baseline: tuple[Path, Path] | None) -> None:
    section("Endpoint byte-equality versus the pre-change code")
    if baseline is None:
        print("   ⚠ no pre-change baseline (no git / no BASELINE_BACKEND_DIR) — "
              "section skipped")
        return
    backend, examples = baseline
    if backend.resolve() == BACKEND_DIR:
        print("   ⚠ BASELINE_BACKEND_DIR is the tree under test — section skipped")
        return

    pristine = _FIXTURES / "example-dirs"
    dirs = _FIXTURES / "endpoint-dirs"
    _copy_example_dirs(examples, pristine)
    _copy_example_dirs(pristine, dirs)

    baseline_payload = _run_endpoint_dump(backend, _TMP / "baseline.json", "baseline", dirs)
    _remove_catalog_json(dirs)
    baseline_nocat = _run_endpoint_dump(backend, _TMP / "baseline-nocat.json",
                                        "baseline-nocat", dirs)
    _restore_catalog_json(pristine, dirs)

    check(Path(baseline_payload["_app"]).resolve() == (backend / "app.py").resolve(),
          "the baseline run really imported the pre-change app")
    check(Path(baseline_payload["_app"]).resolve()
          != Path(BACKEND_DIR, "app.py").resolve(),
          "the captured baseline came from a different tree than this one")

    # One app boot per phase: with the real catalog.json, with it deleted, and
    # with a bogus one in its place.  All three must equal the baseline.
    phases: list[tuple[str, dict]] = []
    phases.append(("with catalog.json", _run_endpoint_dump(
        BACKEND_DIR, _TMP / "endpoint-with.json", "with", dirs)))
    _remove_catalog_json(dirs)
    phases.append(("without catalog.json", _run_endpoint_dump(
        BACKEND_DIR, _TMP / "endpoint-without.json", "without", dirs)))
    _write_bogus_catalog_json(dirs)
    phases.append(("with a bogus catalog.json", _run_endpoint_dump(
        BACKEND_DIR, _TMP / "endpoint-bogus.json", "bogus", dirs)))
    _restore_catalog_json(pristine, dirs)

    check(all(Path(payload["_app"]).resolve() == (BACKEND_DIR / "app.py").resolve()
              for _, payload in phases),
          "every comparison run really imported this tree's app")
    for label, payload in phases:
        for path in _ENDPOINT_PATHS:
            check(payload[path]["status"] == 200, f"{path} answers 200 {label}")
            check(payload[path]["body"] == baseline_payload[path]["body"],
                  f"{path} {label} is byte-for-byte the pre-change response")

    # The baseline has to be a real baseline: the *old* code must change when the
    # file is taken away, or the comparisons above prove nothing.
    changed = [path for path in _ENDPOINT_PATHS
               if baseline_nocat[path]["body"] != baseline_payload[path]["body"]]
    check(len(changed) == 3,
          "the old code's own response depends on catalog.json (all three endpoints)")


# ── 7. First-run seeding ─────────────────────────────────────────────

def _counts(engine) -> dict:
    factory = sessionmaker(bind=engine)
    with factory() as session:
        entries = dict(session.execute(
            select(CatalogEntry.namespace, func.count()).group_by(CatalogEntry.namespace)
        ).all())
        documents = session.scalar(select(func.count()).select_from(Document))
        revisions = session.scalar(select(func.count()).select_from(DocumentRevision))
        assets = session.scalar(select(func.count()).select_from(DocumentAsset))
        state = sorted(session.scalars(select(CatalogSeedState.namespace)))
    return {"entries": entries, "documents": documents, "revisions": revisions,
            "assets": assets, "seed_state": state}


def _check_seed(seed_engine) -> None:
    section("First-run seed")
    counts = _counts(seed_engine)
    check(counts["documents"] == 6, "a fresh database has the six seeded documents")
    check(counts["revisions"] == 6, "each seeded document has its revision")
    check(counts["entries"].get("tools") == 8, "the tools catalog is seeded with 8 entries")
    check(counts["entries"].get("npm") == 2, "the npm overlay is seeded with 2 rows")
    check(counts["entries"].get("debian") == 3, "the debian overlay is seeded with 3 rows")
    check(counts["entries"].get("docker-images") == 2,
          "the docker overlay is seeded with 2 rows")
    check(counts["seed_state"] == ["debian", "docker-images", "docs", "npm", "tools"],
          "all five catalogs are recorded in catalog_seed_state")
    check(set(catalog_seed.CATALOGS) == {
        ("docs", "docs.seed.sql"), ("tools", "tools.seed.sql"), ("npm", "npm.seed.sql"),
        ("debian", "debian.seed.sql"), ("docker-images", "docker-images.seed.sql"),
    }, "the seed knows exactly the five catalogs")

    again = init_engine(f"sqlite:///{_TMP / 'seed.db'}")
    check(_counts(again) == counts, "opening the database a second time changes nothing")
    outcomes = catalog_seed.ensure_seed(again)
    check(all(outcome.action == "already" for outcome in outcomes),
          "a second seed pass reports every catalog already seeded")

    empty = _run_empty_seed_probe()
    check(empty == {"entries": 0, "documents": 0, "seed_state": 0},
          "SEED_CATALOGS=0 leaves a genuinely empty database")


def _check_consistency(engine) -> None:
    section("Consistency")
    factory = sessionmaker(bind=engine)
    with factory() as session:
        rows = list(session.scalars(select(CatalogEntry)))
        by_namespace: dict[str, list[CatalogEntry]] = {}
        for row in rows:
            by_namespace.setdefault(row.namespace, []).append(row)
        keyless = {namespace for namespace, group in by_namespace.items()
                   if all(row.storage_key == "" for row in group)}
        check(keyless == {"npm", "debian", "docker-images"},
              "the keyless namespaces are exactly the three mirrors")
        check(not any(row.storage_key for row in rows if row.namespace in mirror_catalog.MIRRORS),
              "no mirror row owns an object key")
        tools = [row for row in rows if row.namespace == "tools"]
        check(tools and all(row.storage_key for row in tools),
              "every tools row still names an object")
        tools_store = objectstore.tools_store()
        check(all(tools_store.exists(row.storage_key) for row in tools),
              "every tools row's object exists")
        documents = list(session.scalars(select(Document)))
        revisions = list(session.scalars(select(DocumentRevision)))
        assets = list(session.scalars(select(DocumentAsset)))
        docs_keys = [item.storage_key for item in (*documents, *revisions, *assets)]
        check(docs_keys and all(docs_keys), "every document row still names an object")
        docs_store = objectstore.docs_store()
        check(all(docs_store.exists(key) for key in docs_keys),
              "every document row's object exists")


def _check_one_shot(engine) -> None:
    section("One-shot seeding")
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.query(DocumentAsset).delete()
        session.query(DocumentRevision).delete()
        session.query(Document).delete()
        session.query(CatalogEntry).delete()
        session.commit()
    check(_counts(engine)["entries"] == {} and _counts(engine)["documents"] == 0,
          "the administrator's deletion empties every catalog")

    init_engine(f"sqlite:///{_TMP / 'seed.db'}")
    after = _counts(engine)
    check(after["entries"] == {} and after["documents"] == 0,
          "a restart does not resurrect a deleted catalog")
    check(after["seed_state"] == ["debian", "docker-images", "docs", "npm", "tools"],
          "the seed markers survive the deletion")

    outcomes = catalog_seed.ensure_seed(engine, force=True)
    check(all(outcome.action == "seeded" for outcome in outcomes),
          "force=True installs every catalog again")
    forced = _counts(engine)
    check(forced["documents"] == 6 and forced["entries"].get("tools") == 8
          and forced["entries"].get("npm") == 2 and forced["entries"].get("debian") == 3
          and forced["entries"].get("docker-images") == 2,
          "the defaults come back exactly once")
    check(len(set(forced["seed_state"])) == 5, "no seed marker is duplicated")

    with factory() as session:
        documents = list(session.scalars(select(Document)))
        revisions = list(session.scalars(select(DocumentRevision)))
        assets = list(session.scalars(select(DocumentAsset)))
        tools = list(session.scalars(
            select(CatalogEntry).where(CatalogEntry.namespace == "tools")
        ))
        referenced = {item.storage_key for item in (*documents, *revisions, *assets, *tools)}
    docs_objects = {info.key for info in objectstore.docs_store().walk() if not info.is_dir}
    tools_objects = {info.key for info in objectstore.tools_store().walk() if not info.is_dir}
    check(docs_objects == {item.storage_key for item in (*documents, *revisions, *assets)},
          "the docs store holds exactly the objects the document rows reference")
    check(tools_objects == {row.storage_key for row in tools},
          "the tools store holds exactly the objects the tool rows reference")
    check(not (referenced - (docs_objects | tools_objects)),
          "re-seeding left no row pointing at a missing object")


# ── 8. A database that predates the overlay rows ─────────────────────

def _check_legacy_upgrade(baseline: tuple[Path, Path] | None) -> None:
    section("Upgrading a pre-overlay database")
    if baseline is None:
        print("   ⚠ no pre-change baseline (no git / no BASELINE_BACKEND_DIR) — "
              "section skipped")
        return
    backend, _examples = baseline
    if backend.resolve() == BACKEND_DIR:
        print("   ⚠ BASELINE_BACKEND_DIR is the tree under test — section skipped")
        return

    legacy_path = _TMP / "legacy.db"
    env = _tree_env(backend, legacy_path, _FIXTURES, tag="legacy")
    stdout = _run_code(backend, _LEGACY_CREATE_CODE, env, "legacy database creation")
    before = json.loads(stdout.strip().splitlines()[-1])
    if not legacy_path.is_file():
        print("   ⚠ the pre-change code wrote no database — section skipped")
        return

    check(not {"version", "arch", "kind"} & set(before["columns"]),
          "the pre-change schema really has no version/arch/kind columns")
    check(before["seed_namespaces"] == ["docs", "tools"],
          "the pre-change seed records only docs and tools")
    check(before["entry_counts"].get("tools") == 8,
          "the pre-change database has the tools rows")

    upgraded = init_engine(f"sqlite:///{legacy_path}")
    columns = {item["name"] for item in inspect(upgraded).get_columns("catalog_entries")}
    check({"version", "arch", "kind"} <= columns,
          "opening it adds the three overlay columns")
    after = _counts(upgraded)
    check(after["seed_state"] == ["debian", "docker-images", "docs", "npm", "tools"],
          "the mirror catalogs are seeded into the upgraded database")
    check(after["entries"].get("tools") == 8 and after["entries"].get("npm") == 2
          and after["entries"].get("debian") == 3 and after["entries"].get("docker-images") == 2,
          "the upgraded database ends with every catalog's rows")
    check(after["documents"] == 6, "the pre-change documents are kept")
    with sessionmaker(bind=upgraded)() as session:
        mirrors = list(session.scalars(
            select(CatalogEntry).where(CatalogEntry.namespace.in_(mirror_catalog.MIRRORS))
        ))
    check(all(row.storage_key == "" for row in mirrors),
          "the rows added to an upgraded database own no object key")


# ── 9. The seed never writes a mirror object ─────────────────────────

def _check_seed_objects() -> None:
    section("Seeding objects")
    written: list[tuple[str, str]] = []
    original = objectstore.catalog_store

    class _Recording:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def put(self, key, chunks, *, content_type=None):
            written.append((self._inner.namespace, key))
            return self._inner.put(key, chunks, content_type=content_type)

    def recording(namespace, root):
        return _Recording(original(namespace, root))

    with mock.patch.object(objectstore, "catalog_store", recording):
        init_engine(f"sqlite:///{_TMP / 'seed-objects.db'}")
    namespaces = {namespace for namespace, _ in written}
    check({"docs", "tools"} <= namespaces,
          "seeding the application-managed catalogs does write their objects")
    check(not (set(mirror_catalog.MIRRORS) & namespaces),
          "seeding a mirror namespace never writes an object")


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    print(f"backend under test: {BACKEND_DIR}")
    print(f"temporary root:     {_TMP}")
    print(f"fixture root:       {_FIXTURES}")

    baseline = _prepare_baseline()
    print(f"pre-change baseline: {baseline[0] if baseline else 'none (sections will skip)'}")

    main_engine = init_engine(f"sqlite:///{_TMP / 'gate.db'}")
    Main = sessionmaker(bind=main_engine, expire_on_commit=False)
    check(Path(BACKEND_DIR, "services", "mirror_catalog.py").is_file(),
          "the tree under test has the mirror overlay module")

    with Main() as session:
        _check_overlay(session)
        _check_path_for()
        _check_import(session)
        _check_export(session)
        _check_scanners(session)
        _check_cli_fixes(session)

    seed_engine = init_engine(f"sqlite:///{_TMP / 'seed.db'}")
    _check_seed(seed_engine)
    _check_consistency(seed_engine)

    _check_legacy_upgrade(baseline)
    _check_seed_objects()
    _check_one_shot(seed_engine)
    _check_endpoints(baseline)

    print()
    if failures:
        print(f"❌ {len(failures)} of {checks} check(s) failed")
        for item in failures:
            print(f"   - {item}")
        print(f"temporary root kept for inspection: {_TMP}")
        if _FIXTURES is not None:
            print(f"fixture root kept for inspection:   {_FIXTURES}")
        return 1
    print(f"✅ mirror catalog check passed ({checks} checks)")
    shutil.rmtree(_TMP, ignore_errors=True)
    if _FIXTURES is not None:
        shutil.rmtree(_FIXTURES, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
