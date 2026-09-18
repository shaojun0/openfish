#!/usr/bin/env python
"""Gate: the Agent Hub works *end to end*, across every slice's seam.

Run from the backend directory (`backend/`)::

    python scripts/check_agent_hub_e2e.py

The per-slice gates each prove their own half in isolation.  This one proves the
seams, because every serious defect so far lived *between* slices rather than
inside one:

* the permission constants S1–S4 guard with are importable from
  ``auth.permissions`` (what ``check_permission_catalog.py`` resolves by name);
* the routes are reachable at the paths §5.3 promises, under the real
  ``routes/__init__.py`` registration — not a hand-built app;
* a finding ingested by S3's state machine survives a second run (I1), and the
  I2/I3 refusals reach HTTP as ``409``;
* S2's context search answers over the same mirrored issue rows;
* S1's git-credential hand-off mints a real key through the real manager.

It boots the real Flask app on a throwaway SQLite database, so it is offline and
safe to run beside the other gates.  Everything it asserts is an invariant from
``docs/agent-hub/DEVELOPMENT.md`` (I1, I2, I3) or a §5.3 contract, never a bare
"some 200 came back".
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"   ✅ {label}")
        return
    print(f"   ❌ {label}" + (f" — {detail}" if detail else ""))
    failures.append(label + (f" — {detail}" if detail else ""))


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Hub end-to-end gate.")
    parser.add_argument("--db", default=None, help="SQLite path (default: temp file)")
    parser.add_argument("--keep-db", action="store_true", help="do not delete the temp DB")
    parser.add_argument("--user", default="e2e-gate",
                        help="API-key owner; also bootstrapped as the superuser")
    args = parser.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="openfish-e2e-")
    db_path = args.db or str(Path(tmpdir) / "e2e.db")
    url = db_path if "://" in db_path else f"sqlite:///{db_path}"
    os.environ["DATABASE_URL"] = url
    os.environ["AUTH_ENABLED"] = "true"
    # Bootstrap the *API-key principal* as the cold-start superuser, which is
    # what lets this gate exercise the admin-only half (repo:write,
    # finding:decide) with a real credential.  The identity is the bare key
    # owner: `ApiKeyManager.validate` provisions a user whose `external_id` is
    # the key's `created_by` — `api_key:` + owner is the *session* provider's
    # naming, so prefixing it here would silently bootstrap a second, unused
    # account and every admin call would 403.
    os.environ["SERVER__ADMIN_USERS"] = f'["{args.user}"]'

    from extensions.database import Session, init_engine
    from auth.api_keys import ApiKeyManager

    engine = init_engine(url)
    # The scoped session has no bind of its own until the Flask extension
    # configures it, and the key must exist before the app boots (it is what the
    # app's own auth will verify), so bind it here first.
    Session.configure(bind=engine)
    key = ApiKeyManager(Session).create_key("e2e", args.user)["key"]

    from app import app

    client = app.test_client()
    auth = _bearer(key)

    print("── 1 · anonymous floor ─────────────────────────────────────")
    response = client.get("/api/v1/repos")
    check("anonymous is refused the repository list", response.status_code in (401, 403),
          str(response.status_code))
    response = client.get("/api/v1/repos", headers=auth)
    check("the minted key is accepted", response.status_code == 200, str(response.status_code))

    print("── 2 · repository create + detail (§5.3) ───────────────────")
    response = client.post(
        "/api/v1/repos",
        json={"slug": "openfish/e2e", "kind": "workspace"},
        headers=auth,
    )
    check("POST /api/v1/repos creates a workspace repo", response.status_code in (200, 201),
          f"{response.status_code} {response.get_data(as_text=True)[:200]}")
    response = client.get("/api/v1/repos/openfish/e2e", headers=auth)
    check("GET /api/v1/repos/<slug> answers on a slashed slug",
          response.status_code == 200, str(response.status_code))
    repo_doc = response.get_json() or {}
    repo_id = repo_doc.get("id")
    check("the repo document carries an id", isinstance(repo_id, int), str(repo_doc)[:200])

    print("── 3 · S3 state machine at the seam (I1 / I2 / I3) ─────────")
    from models.agent_hub import Finding, ReviewRun
    from services import findings as findings_service

    session = Session()
    run = ReviewRun(repo_id=repo_id, commit_sha="a" * 40, policy_hash="h", status="ok")
    session.add(run)
    session.commit()
    run_id = run.id

    debt = {
        "rule_id": "docs.missing-route-doc",
        "level": "debt",
        "severity": "low",
        "file_path": "backend/routes/e2e.py",
        "symbol": "e2e_route",
        "title": "route is undocumented",
    }
    blocking = dict(debt, rule_id="backend.no-typing-optional",
                    level="blocking", severity="high", file_path="backend/services/e2e.py",
                    symbol="E2e.run")

    first = findings_service.ingest(repo_id, run_id, [debt, blocking], session=session)
    check("first run opens two findings", first.get("new") == 2, str(first))

    # I1: the *same* problem on a later commit is matched, never re-opened.  The
    # line hint and commit differ; the identity must not.
    moved = dict(debt, line_hint=999, title="route is still undocumented")
    run2 = ReviewRun(repo_id=repo_id, commit_sha="b" * 40, policy_hash="h", status="ok")
    session.add(run2)
    session.commit()
    second = findings_service.ingest(repo_id, run2.id, [moved], session=session)
    check("second run matches instead of re-opening (I1)",
          second.get("new") == 0 and second.get("matched") == 1, str(second))
    total = session.query(Finding).filter(Finding.repo_id == repo_id).count()
    check("the table still holds exactly two rows", total == 2, f"count={total}")
    session.close()

    response = client.get("/api/v1/findings", headers=auth)
    check("GET /api/v1/findings answers", response.status_code == 200, str(response.status_code))
    items = (response.get_json() or {}).get("items") or []
    check("the list carries both findings", len(items) == 2, f"len={len(items)}")
    by_rule = {item["rule_id"]: item for item in items}
    debt_id = (by_rule.get("docs.missing-route-doc") or {}).get("id")

    response = client.post(
        f"/api/v1/findings/{debt_id}/decide",
        json={"action": "wontfix", "reason": "later, honestly"},
        headers=auth,
    )
    check("wontfix without owner/due is refused (I2)", response.status_code == 409,
          str(response.status_code))
    from datetime import date, timedelta

    future = (date.today() + timedelta(days=30)).isoformat()
    response = client.post(
        f"/api/v1/findings/{debt_id}/decide",
        json={"action": "wontfix", "owner": "shaojun0", "due": future, "reason": "later"},
        headers=auth,
    )
    check("wontfix with owner+due is accepted", response.status_code == 200,
          f"{response.status_code} {response.get_data(as_text=True)[:200]}")

    blocking_id = by_rule["backend.no-typing-optional"]["id"]
    response = client.post(
        f"/api/v1/findings/{blocking_id}/decide",
        json={"action": "wontfix", "owner": "shaojun0", "due": future},
        headers=auth,
    )
    check("a blocking finding refuses wontfix entirely (I3)", response.status_code == 409,
          str(response.status_code))

    print("── 4 · S2 context search over the seam (§8.3) ──────────────")
    from models.agent_hub import RepoIssue

    session = Session()
    session.add(RepoIssue(
        repo_id=repo_id, number=1234, is_pull_request=False,
        title="pypi routes keep the annotation", state="closed", author="someone",
        labels="[]", source_id="gh-1234",
    ))
    session.commit()
    session.close()
    response = client.get(
        "/api/v1/repos/openfish/e2e/context/search?q=annotation", headers=auth)
    check("context search answers", response.status_code == 200, str(response.status_code))
    payload = response.get_json() or {}
    text = payload.get("text") or ""
    check("the hit is wrapped as untrusted data (§8.4)",
          "<untrusted-" in text, text[:200])
    check("the budget fields are reported",
          {"truncated", "budget_used"} <= set(payload), str(sorted(payload))[:200])

    print("── 5 · S1 git credential hand-off (§5.2) ───────────────────")
    response = client.get("/api/v1/repos/openfish/e2e/git-credential", headers=auth)
    check("git-credential answers", response.status_code == 200, str(response.status_code))
    cred = response.get_json() or {}
    check("it returns a clone URL and a one-time password",
          cred.get("clone_url", "").endswith(".git") and bool(cred.get("password")),
          str(sorted(cred))[:200])
    check("the minted password is a platform API key",
          str(cred.get("password", "")).startswith("cpypi_"), str(cred.get("password"))[:12])

    print("── 6 · S4 task surface (§5.3) ──────────────────────────────")
    response = client.get("/api/v1/agent/tasks", headers=auth)
    check("GET /api/v1/agent/tasks answers", response.status_code == 200,
          str(response.status_code))

    print("── 7 · response contract (no undocumented endpoint) ────────")
    response = client.get("/openapi.json")
    spec = response.get_json() or {}
    paths = spec.get("paths") or {}
    for expected in ("/api/v1/repos", "/api/v1/findings", "/api/v1/agent/tasks"):
        check(f"{expected} is in the published contract", expected in paths)

    if not args.keep_db:
        Path(db_path).unlink(missing_ok=True)
    try:
        Path(tmpdir).rmdir()
    except OSError:
        pass

    print()
    if failures:
        print(f"❌ agent-hub e2e check FAILED — {len(failures)}/{checks} problem(s)")
        for item in failures:
            print(f"   - {item}")
        return 1
    print(f"✅ agent-hub e2e check passed — {checks} checks across all five slices")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
