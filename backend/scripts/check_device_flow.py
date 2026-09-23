#!/usr/bin/env python
"""Gate: the device-authorization flow hands out a working key, exactly once.

Run from the backend directory (`backend/`)::

    python scripts/check_device_flow.py

This is the contract the DSH ``enterprise-intranet`` plugin is written against,
so it is pinned end-to-end rather than by inspection.  A throwaway instance
(fresh database, fresh device-code file) is booted and the full exchange is
driven over HTTP:

1. ``POST /api/v1/device/code`` anonymously returns a pending request;
2. polling before approval answers ``400 authorization_pending``;
3. ``GET /device`` bounces an anonymous visitor into the login flow, and an
   authenticated, ``key:create``-holding visitor sees the approval page;
4. ``POST /device/approve`` mints a key and binds it to the ``user_code``;
5. the next poll returns that key **once**; a replay answers ``expired_token``;
6. the issued key authenticates as a Bearer token and can read
   ``GET /api/v1/models/resolved`` — the endpoint the plugin actually consumes —
   while an anonymous caller is refused;
7. a ``user_code`` is single-use: approving it twice fails;
8. ``?next=`` cannot steer the post-login ``Location`` off-origin — a tab or
   newline in the value used to collapse ``/\\t/`` into a protocol-relative
   ``//`` — while the ``/device`` landing still round-trips the ``user_code``.

Every step runs against the real Flask app, so a change that desynchronises the
device store, the guards or the response shape fails here.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

#: The gate's throwaway administrator.  The password is generated per run: a
#: literal one is a credential in the tree even when it only ever exists in a
#: temporary database, and a scanner cannot tell the two apart.
ADMIN_USER = "deviceadmin"
ADMIN_PASS = secrets.token_urlsafe(18)

#: The two upstream keys the fixture route table resolves.  Generated for the
#: same reason; the assertions below compare against these constants.
STORED_UPSTREAM_KEY = "sk-" + secrets.token_urlsafe(24)
PLAINTEXT_UPSTREAM_KEY = "sk-" + secrets.token_urlsafe(24)
FOREIGN_UPSTREAM_KEY = "sk-" + secrets.token_urlsafe(24)

#: The master key every route's ``api_key`` is sealed under.  Prompt-free and
#: literal because it is a fixture, never a deployment value — any non-empty
#: secret works, which is the point of deriving the Fernet key.  Set *before*
#: ``app`` is imported: ``config.settings`` reads the environment once, so a
#: value set afterwards would never be seen.
#:
#: The name is spelled out because it cannot be imported yet — ``config.keys``
#: is only reachable through ``config/__init__``, which builds ``settings`` on
#: import.  The assertion in §6 binds this literal back to
#: ``services.model_routes.MODEL_ROUTE_KEY_ENV``, so a rename fails the gate
#: instead of silently sealing under no key at all.
os.environ["MODEL_ROUTE_KEY"] = "gate-only-model-route-key"

_TMP = Path(tempfile.mkdtemp(prefix="cpypi-device-"))
os.environ["API_KEYS_FILE"] = str(_TMP / "device.db")
os.environ["DEVICE_CODES_FILE"] = str(_TMP / "device_codes.json")
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_USERNAME"] = ADMIN_USER
os.environ["AUTH_ASSERT"] = ADMIN_PASS
os.environ["OAUTH2_INTROSPECT_URL"] = ""
os.environ["OAUTH2_AUTHORIZE_URL"] = ""
os.environ["ADMIN_USERS"] = f'["{ADMIN_USER}"]'
os.environ["SERVER__PUBLIC_BASE_URL"] = "https://registry.example.invalid:9443"

# The route table the plugin resolves.  One enabled default route with a sealed
# key, seeded through the service (so the gate exercises the same validation —
# including the sealing — the panel does), plus two rows written straight to the
# table to pin the states a real deployment can be in: one whose key predates
# sealing, and one sealed under a *different* master key.
ROUTE_FIXTURES: list[dict] = [
    {
        "name": "gate-default",
        "provider": "openai",
        "base_url": "https://api.example.invalid",
        "api_key": STORED_UPSTREAM_KEY,
        "model": "gate-model",
        "aliases": ["default"],
        "path": "/v1/chat/completions",
        "enabled": True,
        "description": "gate fixture",
    },
]

from app import app  # noqa: E402 - imported late so the env above applies
from extensions.database import Session  # noqa: E402
from models.model_route import API_KEY_PREFIX, ModelRoute  # noqa: E402
from services import model_routes  # noqa: E402
from services.sealing import SecretSealer  # noqa: E402

for _fixture in ROUTE_FIXTURES:
    model_routes.create(Session, _fixture)


def _seed_raw_routes() -> None:
    """Insert the two rows that only a *pre-existing* database would have.

    Written through the model rather than the service on purpose: the service
    seals what it is given, and neither of these is sealable — one is a bare key
    from before sealing existed, the other an envelope this deployment has no
    key for.  They are the fixtures for ``api_key_source``'s two "act on this"
    states, which replaced the retired ``api_key_env`` cases.
    """
    foreign = SecretSealer("a-different-deployments-master-key", prefix=API_KEY_PREFIX)
    assert foreign.seal(FOREIGN_UPSTREAM_KEY).startswith(API_KEY_PREFIX)
    session = Session()
    try:
        legacy = ModelRoute(
            name="gate-plaintext",
            provider="openai",
            kind="chat",
            base_url="https://api.example.invalid",
            path="/v1/chat/completions",
            model="gate-plaintext-model",
            api_key=PLAINTEXT_UPSTREAM_KEY,
            enabled=True,
            description="gate fixture (key predates sealing)",
        )
        legacy.set_aliases(["plaintext"])
        sealed_elsewhere = ModelRoute(
            name="gate-unreadable",
            provider="openai",
            kind="chat",
            base_url="https://api.example.invalid",
            path="/v1/chat/completions",
            model="gate-unreadable-model",
            api_key=foreign.seal(FOREIGN_UPSTREAM_KEY),
            enabled=True,
            description="gate fixture (sealed under a key this deployment lacks)",
        )
        sealed_elsewhere.set_aliases(["unreadable"])
        session.add_all([legacy, sealed_elsewhere])
        session.commit()
    finally:
        session.close()


_seed_raw_routes()

ADMIN = (ADMIN_USER, ADMIN_PASS)

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def main() -> int:
    anon = app.test_client()
    admin = app.test_client()
    credentials = base64.b64encode(f"{ADMIN_USER}:{ADMIN_PASS}".encode()).decode()
    admin_auth = {"Authorization": f"Basic {credentials}"}

    print("── 1. start a request (anonymous) ──────────────────────────────")
    started = anon.post("/api/v1/device/code", json={})
    check(started.status_code == 200, f"POST /api/v1/device/code -> {started.status_code}")
    code = started.get_json() or {}
    device_code = code.get("device_code")
    user_code = code.get("user_code")
    check(bool(device_code) and len(str(device_code)) >= 32, "device_code is a long secret")
    check(bool(user_code) and "-" in str(user_code), f"user_code is human shaped ({user_code})")
    check(
        str(code.get("verification_uri_complete", "")).startswith(
            "https://registry.example.invalid:9443/device?user_code="
        ),
        "verification_uri_complete uses SERVER__PUBLIC_BASE_URL",
    )

    print("── 2. polling before approval ──────────────────────────────────")
    pending = anon.post("/api/v1/device/token", json={"device_code": device_code})
    check(pending.status_code == 400, f"pending poll -> {pending.status_code}")
    check(
        (pending.get_json() or {}).get("error") == "authorization_pending",
        "pending poll reports authorization_pending",
    )

    print("── 3. the approval page ────────────────────────────────────────")
    bounced = anon.get(f"/device?user_code={user_code}", follow_redirects=False)
    check(bounced.status_code == 302, f"anonymous /device -> {bounced.status_code}")
    check(
        "/auth/login" in (bounced.headers.get("Location") or "")
        and "user_code" in (bounced.headers.get("Location") or ""),
        "anonymous /device redirects into login with next back to the code",
    )
    page = admin.get(f"/device?user_code={user_code}", headers=admin_auth)
    check(page.status_code == 200, f"authenticated /device -> {page.status_code}")
    check(
        str(user_code).encode() in page.data,
        "approval page shows the user_code",
    )

    print("── 4. approve ──────────────────────────────────────────────────")
    approved = admin.post(
        "/device/approve", data={"user_code": user_code}, headers=admin_auth,
    )
    check(approved.status_code == 200, f"POST /device/approve -> {approved.status_code}")

    print("── 5. redeem exactly once ──────────────────────────────────────")
    granted = anon.post("/api/v1/device/token", json={"device_code": device_code})
    check(granted.status_code == 200, f"approved poll -> {granted.status_code}")
    body = granted.get_json() or {}
    api_key = body.get("api_key")
    check(bool(api_key) and str(api_key).startswith("cpypi_"), "a platform API key was minted")
    check(body.get("platform_url") == "https://registry.example.invalid:9443", "platform_url echoed")
    replay = anon.post("/api/v1/device/token", json={"device_code": device_code})
    check(replay.status_code == 400, f"replayed poll -> {replay.status_code}")
    check(
        (replay.get_json() or {}).get("error") == "expired_token",
        "replayed poll reports expired_token (one-shot consumption)",
    )

    print("── 6. the minted key works as a Bearer credential ──────────────")
    bearer = app.test_client()
    resolved = bearer.get(
        "/api/v1/models/resolved", headers={"Authorization": f"Bearer {api_key}"},
    )
    check(resolved.status_code == 200, f"Bearer GET /api/v1/models/resolved -> {resolved.status_code}")
    routes = (resolved.get_json() or {}).get("routes") or []
    by_name = {r.get("name"): r for r in routes}
    check(len(routes) == 3, "three routes resolved")
    check(
        by_name.get("gate-default", {}).get("endpoint_url")
        == "https://api.example.invalid/v1/chat/completions",
        "resolved route carries a pre-joined endpoint_url",
    )

    print("── 6b. a sealed key is opened for the client ───────────────────")
    check(
        model_routes.MODEL_ROUTE_KEY_ENV == "MODEL_ROUTE_KEY",
        "the gate exported the variable the application actually reads",
    )
    if "gate-default" in by_name:
        check(
            by_name["gate-default"].get("api_key") == STORED_UPSTREAM_KEY,
            "resolved route carries the key decrypted from its envelope",
        )
        check(
            by_name["gate-default"].get("api_key_source") == "stored",
            "resolved route reports api_key_source=stored",
        )

    print("── 6c. a key that predates sealing still authenticates ─────────")
    if "gate-plaintext" in by_name:
        check(
            by_name["gate-plaintext"].get("api_key") == PLAINTEXT_UPSTREAM_KEY,
            "a legacy plaintext row still yields its key",
        )
        check(
            by_name["gate-plaintext"].get("api_key_source") == "plaintext",
            "and reports api_key_source=plaintext so the operator can seal it",
        )

    print("── 6d. an envelope this deployment cannot open is reported ─────")
    if "gate-unreadable" in by_name:
        check(
            by_name["gate-unreadable"].get("api_key") == "",
            "a foreign envelope resolves to no key rather than a guess",
        )
        check(
            by_name["gate-unreadable"].get("api_key_source") == "unreadable",
            "and reports api_key_source=unreadable rather than looking configured",
        )

    masked = bearer.get("/api/v1/models", headers={"Authorization": f"Bearer {api_key}"})
    masked_routes = (masked.get_json() or {}).get("routes") or []
    masked_by_name = {r.get("name"): r for r in masked_routes}
    check(
        masked.status_code == 200
        and all(r.get("api_key") is None for r in masked_routes),
        "the browsing view still masks every key",
    )
    check(
        masked_by_name.get("gate-default", {}).get("has_api_key") is True
        and masked_by_name.get("gate-default", {}).get("api_key_source") == "stored",
        "the browsing view shows the sealed route as configured",
    )
    check(
        masked_by_name.get("gate-plaintext", {}).get("api_key_hint")
        == "••••" + PLAINTEXT_UPSTREAM_KEY[-4:],
        "and still hints at a not-yet-sealed key's last four characters",
    )
    check(
        masked_by_name.get("gate-unreadable", {}).get("has_api_key") is False,
        "and shows the unopenable envelope as NOT configured",
    )
    refused = anon.get("/api/v1/models/resolved")
    check(refused.status_code in (401, 403), f"anonymous resolved read -> {refused.status_code}")

    print("── 7. user_code is single use ──────────────────────────────────")
    again = admin.post(
        "/device/approve", data={"user_code": user_code}, headers=admin_auth,
    )
    check(again.status_code in (404, 409, 410), f"second approve -> {again.status_code}")

    print("── 8. ?next= cannot steer the login redirect off-origin ────────")
    # A same-origin check on the *raw* value is not enough: "/\t/evil.example"
    # satisfies startswith("/") and defeats startswith("//"), and the tab is
    # then dropped — by Werkzeug's iri_to_uri on the way out and by the
    # browser's own URL parser — collapsing the path to "//evil.example", a
    # protocol-relative URL.  The landing is now a closed set of keys rebuilt
    # with url_for, so no request argument can name the target.
    for payload in ("/%09/evil.example", "/%0A/evil.example", "/%0D/evil.example",
                    "//evil.example", "%2F%2Fevil.example"):
        resp = admin.get(
            f"/auth/login?next={payload}", headers=admin_auth,
            follow_redirects=False,
        )
        location = resp.headers.get("Location") or ""
        seen = (location.replace("\t", "").replace("\n", "")
                .replace("\r", "").replace("\\", "/"))
        check(
            resp.status_code == 302 and not seen.startswith("//")
            and "://" not in seen,
            f"?next={payload} lands on-origin ({location or resp.status_code})",
        )

    # The one non-SPA landing that must survive the hardening: the device page,
    # rebuilt by url_for so the browser never names the URL.
    landing = admin.get(
        f"/auth/login?next=/device?user_code={user_code}", headers=admin_auth,
        follow_redirects=False,
    )
    location = landing.headers.get("Location") or ""
    check(
        location.startswith("/device?user_code=") and str(user_code) in location,
        f"a /device landing still carries the code ({location or landing.status_code})",
    )

    print()
    if failures:
        print(f"❌ device flow check FAILED — {len(failures)} problem(s)")
        return 1
    print("✅ device flow check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
