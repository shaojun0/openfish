#!/usr/bin/env python
"""Gate: the catalogs are DB-indexed, versioned, conflict-checked and consistent.

Run from the repository root::

    python .agent/checks/check_catalog_store.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_catalog_store.py

The documentation feature stopped being a directory tree: metadata lives in
``documents`` / ``document_revisions`` / ``document_assets`` and the bytes live
behind the storage port in ``services/objectstore.py``.  That buys three
properties this gate is here to keep, none of which a test suite elsewhere
covers:

1. **The storage port behaves.**  A key maps to a path under its root, traversal
   out of that root is refused, ``walk`` reports directories (the import path
   reads a tree, and the integrity check reconciles rows against objects) and
   ``delete_many`` removes a whole document's payload in one call per medium.
2. **Revisioning and the conflict check work.**  A create is revision 1, a save
   with the revision it read is revision 2, a save based on a stale revision is
   refused with a ``409`` rather than winning, and history keeps every revision's
   own object.
3. **The folder bridges are faithful and idempotent.**  Importing a hand-written
   tree creates the rows — documents from ``document.md`` plus ``meta.json``, tool
   entries from the files plus ``catalog.json`` — preserves the source timestamps,
   and a second import reports everything unchanged instead of inflating history
   or rewriting objects.  Export reproduces the trees.

Both catalogs share one model, so both are checked here: a row holds the public
path, the display metadata and the media type, and the object key is an opaque
``uuid4`` with no extension and no path to parse.

It also runs the integrity check the design implies: every row's object exists,
and nothing under the store's ``objects/`` prefix is unaccounted for.  Run on a
throwaway SQLite database and a throwaway ``DOCS_DIR``, like the other gates.

Exits non-zero on the first property that fails.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

# This gate builds its own rows and then reconciles them against the store, so
# the first-run seed has to stay out of the way (`config/storage.seed_catalogs`
# is on by default for a fresh deployment).  A suite manifest can only name a
# command, so the switch is set here, before anything under `config` is imported.
os.environ["SEED_CATALOGS"] = "0"

# The app reads its configuration once, at import — so the throwaway roots have
# to be in the environment before anything under `config`/`services` is imported.
_TMP = Path(tempfile.mkdtemp(prefix="check-docs-store-"))
os.environ["DOCS_DIR"] = str(_TMP / "docs")
os.environ["TOOLS_DIR"] = str(_TMP / "tools")
os.environ["API_KEYS_FILE"] = str(_TMP / "gate.db")
os.environ.pop("DATABASE_URL", None)

from datetime import datetime, timezone
from io import BytesIO
from uuid import UUID

from flask import Flask  # noqa: E402

from errors import RevisionConflictError  # noqa: E402
from extensions.database import Session, init_engine  # noqa: E402
from routes.hub_common import stream_object  # noqa: E402
from services import docs, objectstore, tool_catalog  # noqa: E402
from services.objectstore import S3Store  # noqa: E402

failures: list[str] = []
checks = 0

#: Objects the earlier sections of this gate put in the store directly, which by
#: construction belong to no row: they exercise the port itself.
_FIXTURE_KEYS = {"top.txt", "blob.bin"}


def _is_uuid4(value: str) -> bool:
    """Whether *value* is a canonical uuid4 — the whole key naming scheme."""
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


class _FakeMissing(Exception):
    """A botocore-shaped 404, so the store's own error mapping is exercised."""

    response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3:
    """The six operations ``S3Store`` uses, over a dict (no boto3, no network).

    A faithful-enough S3 double for the mapping this backend owns — prefix
    composition, directory synthesis from delimiters, the continuation-token
    loop and batched deletes — and only that.  It approximates S3's listing
    semantics rather than reproducing them (a real server's ``MaxKeys`` counts
    common prefixes too); ``.agent/scripts/backend/s3_smoke.py`` is what validates
    the real
    client against a real bucket.
    """

    def __init__(self, page_size: int | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.page_size = page_size
        self.deleted_batches: list[int] = []

    def _stamp(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)

    def put_object(self, **kwargs) -> dict:
        self.objects[kwargs["Key"]] = kwargs["Body"].read()
        if kwargs.get("ContentType"):
            self.content_types[kwargs["Key"]] = kwargs["ContentType"]
        return {}

    def get_object(self, **kwargs) -> dict:
        key = kwargs["Key"]
        if key not in self.objects:
            raise _FakeMissing(key)
        data = self.objects[key]
        return {"Body": BytesIO(data), "ContentLength": len(data), "LastModified": self._stamp()}

    def head_object(self, **kwargs) -> dict:
        key = kwargs["Key"]
        if key not in self.objects:
            raise _FakeMissing(key)
        return {"ContentLength": len(self.objects[key]), "LastModified": self._stamp()}

    def delete_object(self, **kwargs) -> dict:
        self.objects.pop(kwargs["Key"], None)
        return {}

    def delete_objects(self, **kwargs) -> dict:
        batch = kwargs["Delete"]["Objects"]
        self.deleted_batches.append(len(batch))
        for item in batch:
            self.objects.pop(item["Key"], None)
        return {}

    def list_objects_v2(self, **kwargs) -> dict:
        prefix = kwargs["Prefix"]
        delimiter = kwargs.get("Delimiter")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        start = int(kwargs.get("ContinuationToken") or 0)
        limit = kwargs.get("MaxKeys") or self.page_size or 1000
        window = keys[start:start + limit]
        grouped: list[str] = []
        contents: list[dict] = []
        for key in window:
            rest = key[len(prefix):]
            if delimiter and delimiter in rest:
                head = prefix + rest.split(delimiter, 1)[0] + delimiter
                if head not in grouped:
                    grouped.append(head)
            else:
                contents.append({
                    "Key": key,
                    "Size": len(self.objects[key]),
                    "LastModified": self._stamp(),
                })
        nxt = start + limit
        truncated = nxt < len(keys)
        page = {
            "Contents": contents,
            "CommonPrefixes": [{"Prefix": item} for item in grouped],
            # Roughly what a real server reports; the store only reads truthiness.
            "KeyCount": len(contents) + len(grouped),
            "IsTruncated": truncated,
        }
        if truncated:
            page["NextContinuationToken"] = str(nxt)
        return page


def check(ok: bool, label: str) -> None:
    global checks
    checks += 1
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def main() -> int:
    engine = init_engine(f"sqlite:///{_TMP / 'gate.db'}")
    Session.configure(bind=engine)
    store = objectstore.docs_store()

    # ── 1. The storage port ──────────────────────────────────────────
    section("ObjectStore")
    store.put("ops/a.sh", [b"a"])
    store.put("ops/b.sh", [b"bb"])
    store.put("top.txt", [b"t"])
    check(store.exists("ops/a.sh"), "put makes a key exist")
    check(store.stat("ops/a.sh").size == 1, "stat reports the size that was put")
    with store.open("ops/a.sh") as handle:
        check(handle.read() == b"a", "open returns the bytes that were put")
    walked = {info.key: info.is_dir for info in store.walk()}
    check(walked.get("ops") is True, "walk reports a category directory")
    check(walked.get("ops/a.sh") is False, "walk reports the files below it")
    check("top.txt" in walked, "walk reports a root-level file")
    try:
        store.put("../escape.txt", [b"x"])
        check(False, "a key that climbs out of the root is refused")
    except ValueError:
        check(True, "a key that climbs out of the root is refused")
    try:
        store.open("nonexistent")
        check(False, "a missing key raises FileNotFoundError")
    except FileNotFoundError:
        check(True, "a missing key raises FileNotFoundError")
    store.delete_many(["ops/a.sh", "ops/b.sh"])
    check(not store.exists("ops/a.sh") and not store.exists("ops/b.sh"),
          "delete_many removes every key it is given")
    store.delete_many(["ops/a.sh"])
    check(True, "deleting an absent key is not an error")
    digest = objectstore.digest_of(store, store.stat("top.txt"))
    check(digest is not None and len(digest) == 64, "digest_of hashes a stored object")
    check(objectstore.read_json(store, "missing.json", default=None) is None,
          "read_json degrades to the default when the object is missing")

    # ── 1a. The S3 backend, offline ──────────────────────────────────
    #
    # Same port, different medium: the objectives that matter are that the
    # catalog namespace lands in the prefix (so one bucket can hold several
    # deployments), that a medium without directories still reports the
    # catalog's tree, and that a delete takes a whole document's payload.
    section("S3 backend")
    fake = _FakeS3(page_size=2)
    s3 = S3Store("tools", prefix="openfish", bucket="catalogs", client=fake)
    check(s3.location == "s3://catalogs/openfish/tools", "the location names bucket and prefix")
    s3.put("ops/a.sh", [b"aa"])
    s3.put("ops/nested/b.sh", [b"bb"])
    s3.put("top.txt", [b"t"])
    check(sorted(fake.objects) == [
        "openfish/tools/ops/a.sh", "openfish/tools/ops/nested/b.sh", "openfish/tools/top.txt",
    ], "every key is stored under the catalog's prefix")
    check(s3.stat("ops/a.sh").size == 2, "stat reports the size S3 reports")
    s3.put("typed.sh", [b"#!/bin/sh\n"], content_type="text/x-sh")
    check(fake.content_types.get("openfish/tools/typed.sh") == "text/x-sh",
          "put records the media type a key with no extension cannot carry")
    with s3.open("ops/a.sh") as handle:
        check(handle.read() == b"aa", "open returns the stored bytes")
    try:
        s3.open("nope")
        check(False, "a missing key raises FileNotFoundError")
    except FileNotFoundError:
        check(True, "a missing key raises FileNotFoundError")
    try:
        s3._full("../escape")
        check(False, "a key that climbs out of the prefix is refused")
    except ValueError:
        check(True, "a key that climbs out of the prefix is refused")
    check(s3.exists("") and s3.exists("ops") and s3.exists("ops/a.sh"), "exists answers for root, prefix and key")
    tree = {info.key: info.is_dir for info in s3.walk()}
    check(tree.get("ops") is True and tree.get("ops/nested") is True,
          "walk synthesises the directories the keys imply")
    check(tree.get("ops/a.sh") is False and tree.get("top.txt") is False,
          "walk reports the files below them")
    check("ops/nested/b.sh" in tree, "walk descends into a nested prefix")

    many = _FakeS3(page_size=2)
    s3_many = S3Store("docs", prefix="", bucket="catalogs", client=many)
    for index in range(7):
        s3_many.put(f"x/{index}.md", [b"x"])
    walked = [info for info in s3_many.walk()]
    check(len([info for info in walked if not info.is_dir]) == 7,
          "walk follows the continuation token across pages")
    check(len({info.key for info in walked}) == len(walked),
          "walk reports each entry exactly once")
    keys = [f"x/{index}.md" for index in range(7)]
    s3_many.delete_many(keys)
    check(not many.objects, "delete_many removes every key it is given")
    s3_many.delete_many(keys)
    check(not many.objects, "deleting absent keys is not an error")

    bulk = _FakeS3()
    s3_bulk = S3Store("docs", prefix="", bucket="catalogs", client=bulk)
    many_keys = [f"bulk/{index:04d}" for index in range(1001)]
    for key in many_keys:
        s3_bulk.put(key, [b"x"])
    s3_bulk.delete_many(many_keys)
    check(not bulk.objects, "delete_many removes more than one request's worth")
    check(len(bulk.deleted_batches) == 2 and max(bulk.deleted_batches) <= 1000,
          "a large delete is split into backend-sized batches")

    # ── 1b. Downloads keep their HTTP semantics ──────────────────────
    #
    # `store.open(key)` hands `send_file` a stream it cannot stat, which used
    # to drop Content-Length, Last-Modified, ETag and Range support — the
    # headers a client resuming a large artifact depends on. `stream_object`
    # rebuilds them from the store's metadata; these checks are what keep that
    # from regressing silently.
    section("Download semantics")
    app = Flask(__name__)
    store.put("blob.bin", [b"0123456789"])
    blob = store.stat("blob.bin")

    with app.test_request_context("/blob.bin"):
        response = stream_object(blob, store.open("blob.bin"), as_attachment=True,
                                 download_name="blob.bin")
        check(response.headers.get("Content-Length") == "10", "a download carries Content-Length")
        check(bool(response.headers.get("Last-Modified")), "a download carries Last-Modified")
        etag = response.headers.get("ETag")
        check(bool(etag) and etag.startswith('"'), "a download carries a quoted ETag")

    with app.test_request_context("/blob.bin", headers={"If-None-Match": etag or ""}):
        response = stream_object(blob, store.open("blob.bin"), as_attachment=True,
                                 download_name="blob.bin")
        check(response.status_code == 304, "If-None-Match is answered 304")

    with app.test_request_context("/blob.bin", headers={"Range": "bytes=2-4"}):
        response = stream_object(blob, store.open("blob.bin"))
        check(response.status_code == 206 and response.headers.get("Content-Range") == "bytes 2-4/10",
              "a Range request is answered 206 with Content-Range")
        check(response.headers.get("Content-Length") == "3", "a partial response is sized to the range")

    # ── 2. Revisions, conflicts, history ─────────────────────────────
    section("Revisions and conflicts")
    entry, replaced = docs.save_document(Session, "python", title="快速开始", content="# 一\n")
    check(replaced is False and entry["revision"] == 1, "a create is revision 1")
    check(entry["id"] == "快速开始", "the entry's id is the slug")
    check(docs.read(Session, "python", "快速开始")["content"] == "# 一\n", "the body round-trips")

    try:
        docs.save_content(Session, "python", "快速开始", "# 抢写\n", revision=99)
        check(False, "a save based on a stale revision is refused")
    except RevisionConflictError as exc:
        check(exc.status_code == 409 and exc.expected == 99 and exc.current == 1,
              "a save based on a stale revision is refused with 409")

    saved = docs.save_content(Session, "python", "快速开始", "# 二\n", revision=1)
    check(saved["revision"] == 2, "a save carrying the current revision wins")
    check(saved["title"] != "# 二", "the title comes from the heading, not the raw text")
    history = docs.history(Session, "python", "快速开始")
    check([item["revision"] for item in history] == [2, 1], "history lists every revision, newest first")
    check(history[0]["current"] and not history[1]["current"], "history marks the current revision")

    info, stream, content_type = docs.open_body(Session, "python", "快速开始", revision=1)
    check(stream.read() == b"# \xe4\xb8\x80\n", "an old revision's body is still readable")
    check(info.size == len("# 一\n".encode()) and _is_uuid4(info.key),
          "the stream comes with store metadata and an opaque key")
    check(content_type == "text/markdown", "the body reports its media type")
    stream.close()

    entry2, replaced2 = docs.save_document(Session, "python", title="快速开始", content="x")
    check(replaced2 is True and entry2["revision"] == 3, "creating an existing slug replaces it as a new revision")

    # ── 3. Assets ────────────────────────────────────────────────────
    section("Assets")
    asset = docs.save_asset(Session, "python", "快速开始", "diagram.png", b"\x89PNG")
    check(asset["name"] == "diagram.png" and asset["is_image"], "an asset records its name and kind")
    check(asset["size"] == 4 and asset["url"].endswith("/assets/diagram.png"), "the asset entry carries size and URL")
    check([a["name"] for a in docs.list_assets(Session, "python", "快速开始")] == ["diagram.png"],
          "the asset is listed")
    asset_info, asset_stream, asset_row = docs.open_asset(
        Session, "python", "快速开始", "diagram.png"
    )
    check(asset_row.content_type.startswith("image/") and asset_stream.read() == b"\x89PNG",
          "the asset's bytes and content type round-trip")
    check(asset_info.size == 4 and _is_uuid4(asset_info.key),
          "the asset stream comes with its own metadata and an opaque key")
    asset_stream.close()

    # ── 4. Import / export ───────────────────────────────────────────
    section("Folder bridge")
    tree = _TMP / "tree"
    (tree / "python" / "guide" / "assets").mkdir(parents=True)
    (tree / "npm" / "pub").mkdir(parents=True)
    (tree / "python" / "guide" / "document.md").write_text("# Guide\n\nhello\n", encoding="utf-8")
    (tree / "python" / "guide" / "meta.json").write_text(
        json.dumps({"title": "指南", "created": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    (tree / "python" / "guide" / "assets" / "logo.png").write_bytes(b"PNG")
    (tree / "npm" / "pub" / "document.md").write_text("# Publishing\n", encoding="utf-8")

    first = docs.import_tree(Session, tree)
    check(first.documents == 2 and first.assets == 1, "import creates both documents and the asset")
    second = docs.import_tree(Session, tree)
    check(second.unchanged == 2 and second.documents == 0,
          "re-importing an unchanged tree adds no revisions")
    check(docs.scan(Session, "python")["documents"][0]["title"] == "指南", "import keeps meta.json's title")
    check(docs.scan(Session, "python")["documents"][0]["created"].startswith("2026-01-01"),
          "import keeps meta.json's created stamp")

    out = _TMP / "out"
    exported = docs.export_tree(Session, out)
    total = sum(item["doc_count"] for item in docs.scan_all(Session)["ecosystems"])
    check(exported == total, "export writes every document back out")
    check((out / "python" / "guide" / "document.md").read_text(encoding="utf-8") == "# Guide\n\nhello\n",
          "the exported Markdown is byte-for-byte the imported source")
    check((out / "python" / "guide" / "assets" / "logo.png").read_bytes() == b"PNG",
          "the exported asset is byte-for-byte the imported file")

    # ── 4b. The tools catalog, on the same model ─────────────────────
    section("Tools catalog")
    tools = _TMP / "tools"
    (tools / "ops" / "nested").mkdir(parents=True)
    (tools / "net").mkdir()
    (tools / "ops" / "check.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    (tools / "ops" / "nested" / "deep.py").write_bytes(b"print(1)\n")
    (tools / "README.txt").write_bytes(b"docs, not an artifact\n")
    (tools / "catalog.json").write_text(
        json.dumps({
            "categories": {"ops": {"name": "运维", "description": "运维脚本", "icon": "Tools"}},
            "tools": {"ops/check.sh": {"name": "健康检查", "tags": ["ops"]}},
        }),
        encoding="utf-8",
    )

    tools_report = tool_catalog.import_tree(Session, tools)
    check(tools_report.created == 2 and tools_report.categories == 2,
          "import creates one entry per artifact (not the README or the overlay)")
    empty = [item for item in tool_catalog.scan(Session)["categories"] if item["slug"] == "net"]
    check(len(empty) == 1 and empty[0]["tools"] == [],
          "an empty category is imported and still shown")
    check("root" not in {item["slug"] for item in tool_catalog.scan(Session)["categories"]},
          "no synthetic root category until something sits in the root")

    (tools / "top.sh").write_bytes(b"#!/bin/sh\n")
    rooted = tool_catalog.import_tree(Session, tools)
    check(rooted.created == 1, "a file added to the root is imported as a new entry")

    payload = tool_catalog.scan(Session, url_prefix="/tools")
    categories = {item["slug"]: item for item in payload["categories"]}
    check(payload["tool_count"] == 3 and set(categories) == {"ops", "net", "root"},
          "the payload groups entries into their categories plus the synthetic root")
    check(categories["ops"]["name"] == "运维" and categories["ops"]["icon"] == "Tools",
          "catalog.json's category metadata is a row now")
    check(categories["root"]["name"] is None, "the synthetic root keeps the page's own label")
    check(categories["ops"]["tools"][0]["name"] == "健康检查"
          and categories["ops"]["tools"][0]["tags"] == ["ops"],
          "catalog.json's per-tool metadata is a row now")
    check(categories["ops"]["tools"][0]["download_url"] == "/tools/ops/check.sh",
          "the download URL is still the public path")
    check([tool["relative_path"] for tool in categories["ops"]["tools"]]
          == ["ops/check.sh", "ops/nested/deep.py"],
          "entries are ordered by their path inside the category")

    rows = tool_catalog._entries(Session)
    check(all(_is_uuid4(row.storage_key) for row in rows),
          "every tool's object key is a bare uuid4 — no extension, no path")
    check({row.content_type for row in rows} >= {"text/x-sh", "text/x-python"},
          "each entry records the media type its extension used to imply")
    tools_store = objectstore.tools_store()
    check(all(tools_store.exists(row.storage_key) for row in rows),
          "every entry's object exists in the tools store")

    again = tool_catalog.import_tree(Session, tools)
    check(again.unchanged == 3 and again.created == 0,
          "re-importing an unchanged tree is a no-op")
    check(tools_store.location.endswith("/tools/objects"),
          "the local store lives in the catalog's reserved objects/ directory")

    info, stream, row = tool_catalog.open_entry(Session, "ops/check.sh")
    check(stream.read() == b"#!/bin/sh\necho hi\n" and row.filename == "check.sh",
          "an entry streams back through the port with its row")
    stream.close()
    try:
        tool_catalog.open_entry(Session, "ops/absent.sh")
        check(False, "an unknown path is a FileNotFoundError")
    except FileNotFoundError:
        check(True, "an unknown path is a FileNotFoundError")

    out_tools = _TMP / "tools-out"
    exported_tools = tool_catalog.export_tree(Session, out_tools)
    check(exported_tools == 3 and (out_tools / "ops" / "check.sh").read_bytes() == b"#!/bin/sh\necho hi\n",
          "export reproduces the tree byte for byte")
    overlay = json.loads((out_tools / "catalog.json").read_text(encoding="utf-8"))
    check(overlay["categories"]["ops"]["name"] == "运维"
          and overlay["tools"]["ops/check.sh"]["name"] == "健康检查",
          "export rebuilds catalog.json from the rows")

    (tools / "ops" / "nested" / "deep.py").unlink()
    pruned = tool_catalog.import_tree(Session, tools, prune=True)
    check(pruned.removed == 1 and tool_catalog.scan(Session)["tool_count"] == 2,
          "prune removes the entry whose file is gone")

    old_key = tool_catalog.require(Session, "top.sh").storage_key
    tool_catalog.delete_entry(Session, "top.sh")
    check(tool_catalog.find(Session, "top.sh") is None and not tools_store.exists(old_key),
          "deleting an entry takes its row and its object")

    # ── 5. Integrity: rows and objects agree ─────────────────────────
    section("Integrity")
    from sqlalchemy import select

    from models.catalog import CatalogEntry
    from models.docs import Document, DocumentAsset, DocumentRevision

    keys = {info.key for info in store.walk() if not info.is_dir}
    tool_keys = {info.key for info in tools_store.walk() if not info.is_dir}
    expected: set[str] = set()
    for document in Session.scalars(select(Document)):
        expected.add(document.storage_key)
    for revision_row in Session.scalars(select(DocumentRevision)):
        expected.add(revision_row.storage_key)
    for asset_row in Session.scalars(select(DocumentAsset)):
        expected.add(asset_row.storage_key)
    tool_expected: set[str] = set()
    for tool_row in Session.scalars(select(CatalogEntry)):
        tool_expected.add(tool_row.storage_key)
    check(expected <= keys, "every document row's object exists")
    check(tool_expected <= tool_keys, "every catalog row's object exists")
    check(all(_is_uuid4(key) for key in expected | tool_expected),
          "every key a row references is a bare uuid4 — no extension, no path")
    # Each store is checked against the rows that own it: the docs store also
    # holds the two fixtures the earlier sections wrote, and the tools store must
    # hold nothing else at all — anything left over is a delete that failed.
    check(keys - expected <= _FIXTURE_KEYS,
          "nothing but the gate's own fixtures is unreferenced in the docs store")
    check(not (tool_keys - tool_expected),
          "the tools store holds exactly what the catalog references")

    # ── 6. Deleting a document takes its payload ─────────────────────
    section("Deletion")
    keys = docs._document_keys(Session, docs.require_document(Session, "python", "快速开始"))
    check(len(keys) >= 4, "a document's rows name every object it owns")
    docs.delete(Session, "python", "快速开始")
    check(docs.scan(Session, "python")["doc_count"] == 1, "the document leaves the catalog")
    check(not any(store.exists(key) for key in keys),
          "every one of its objects is gone")

    print()
    if failures:
        print(f"❌ {len(failures)} of {checks} check(s) failed")
        for item in failures:
            print(f"   - {item}")
        return 1
    print(f"✅ catalog store check passed ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
