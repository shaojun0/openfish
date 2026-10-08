#!/usr/bin/env python
"""Mint one API key against the configured database, and print it.

Run from the backend directory (`backend/`)::

    python scripts/mint_contract_key.py --user contract-gate

This exists for `make contract-gate`: ``scripts/check_contract.py`` validates
*live* responses, so the Makefile boots a throwaway server and needs a
credential to call the authenticated half of the contract with.  Minting it
through the same :class:`ApiKeyManager` the server uses — rather than through a
hard-coded hash — is what keeps the check honest: the key it presents is a real
one, created the way the console creates them.

Targets ``DATABASE_URL`` (or the SQLite default) exactly like the server does,
so point both at the same database or the key will not be recognised.  Prints
only the raw key on stdout; everything else goes to stderr, so ``$(...)`` gets
the key alone.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extensions.database import Session, init_engine  # noqa: E402
from auth.api_keys import ApiKeyManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint an API key on the configured database.")
    parser.add_argument("--user", default="contract-gate",
                        help="created_by / owner label recorded on the key")
    parser.add_argument("--name", default="contract-gate",
                        help="human-readable key name")
    parser.add_argument("--db", default=None,
                        help="SQLite path or SQLAlchemy URL (defaults to DATABASE_URL)")
    args = parser.parse_args()

    engine = init_engine(args.db)
    Session.configure(bind=engine)
    minted = ApiKeyManager(Session).create_key(args.name, args.user)
    print(minted["key"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
