#!/usr/bin/env python
"""Smoke-check an S3-compatible catalog backend against a real endpoint.

Run from the backend directory (`backend/`)::

    OBJECT_BACKEND=s3 S3_ENDPOINT=http://minio:9000 S3_BUCKET_NAME=openfish \
    S3_ACCESS_KEY=… S3_SECRET_KEY=… S3_ADDRESS_STYLE=path \
    python scripts/s3_smoke.py

Why this is not one of the `check_*.py` gates: the gate suite must run offline
and must not require a bucket, so it drives ``S3Store`` through an in-memory
double (`scripts/check_catalog_store.py`).  A double proves the mapping; it cannot
prove that boto3 signs the requests the way this server wants, that the endpoint
speaks path-style addressing, or that an ETag/Last-Modified actually arrives.
Those are exactly the things that go wrong when an operator wires up MinIO or
Ceph, so this script exists to be run once against the real thing.

It writes only under the ``smoke/`` namespace of the configured bucket and
removes everything it created before exiting, so it is safe to run against a
live deployment's bucket.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config import settings  # noqa: E402
from services import objectstore  # noqa: E402

failures: list[str] = []
checks = 0


def check(ok: bool, label: str) -> None:
    global checks
    checks += 1
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def main() -> int:
    config = settings.storage
    print("object backend:", config.object_backend)
    if config.object_backend != "s3":
        print("❌ OBJECT_BACKEND is not `s3` — set it (and the S3_* variables) first.")
        return 2
    print("bucket:", config.s3_bucket_name, "endpoint:", config.s3_endpoint or "(AWS default)",
          "addressing:", config.s3_address_style)

    store = objectstore.catalog_store("smoke", "unused-for-s3")
    print("store:", store.location)

    store.put("probe/hello.txt", [b"hello ", b"s3\n"])
    info = store.stat("probe/hello.txt")
    check(info is not None and info.size == 9, "put_object then head_object round-trips the size")
    check(info is not None and info.modified is not None, "the server reports Last-Modified")
    check(store.exists("probe") and store.exists("probe/hello.txt"),
          "exists answers for a prefix and for a key")
    with store.open("probe/hello.txt") as handle:
        check(handle.read() == b"hello s3\n", "get_object streams the bytes back")
    try:
        store.open("probe/missing.txt")
        check(False, "a missing key raises FileNotFoundError")
    except FileNotFoundError:
        check(True, "a missing key raises FileNotFoundError")

    store.put("probe/nested/one.bin", [b"1"])
    store.put("probe/nested/two.bin", [b"22"])
    first = [(item.key, item.is_dir) for item in store.walk()]
    tree = dict(first)
    check(tree.get("probe") is True and tree.get("probe/nested") is True,
          "walk synthesises the directories the keys imply")
    check(sorted(key for key, is_dir in first if not is_dir) ==
          ["probe/hello.txt", "probe/nested/one.bin", "probe/nested/two.bin"],
          "walk lists every file below the prefix")
    # Order is the server's; determinism is what a catalog payload needs.
    check(first == [(item.key, item.is_dir) for item in store.walk()],
          "walk answers the same listing twice")
    check(objectstore.digest_of(store, store.stat("probe/hello.txt")) is not None,
          "digest_of hashes an object it read through the port")

    store.delete_many(["probe/hello.txt", "probe/nested/one.bin", "probe/nested/two.bin"])
    check(not store.exists("probe"), "delete_many removed every probe object")

    print()
    if failures:
        print(f"❌ {len(failures)} of {checks} check(s) failed")
        for item in failures:
            print(f"   - {item}")
        return 1
    print(f"✅ s3 smoke passed ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
