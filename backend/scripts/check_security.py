#!/usr/bin/env python
"""Gate: the SAST fixes for the 2026-09-20 report stay fixed.

Run from the backend directory (`backend/`)::

    python scripts/check_security.py

Companion to ``docs/security/pypiserver0920-findings.md``: that document says
which findings were real, which were false positives and what changed for the
real ones.  This gate turns the *real* ones into executable assertions, so a
later refactor cannot quietly undo them.

It is deliberately functional rather than textual where behaviour is what
matters — a traversal upload really is run through ``validate_file``, and a
forged log line really is emitted through a handler — and textual only for the
one artifact that has no runtime here, the DSH plugin's JavaScript, which is
read as source:

1. ``services/paths`` reduces a traversal filename and refuses a path that
   escapes its root (Werkzeug's ``secure_filename`` / ``safe_join``);
2. ``validate_file`` returns the *reduced* name, so the error strings that echo
   it and the destination the upload route builds are both safe;
3. ``services/urlsafety`` refuses non-HTTP schemes, embedded credentials and the
   link-local / cloud-metadata ranges, and accepts a real intranet endpoint;
4. ``normalize_api_key`` refuses a CR/LF-bearing key (request splitting into the
   upstream);
5. ``services/logsafe`` escapes a newline so one log record cannot forge a
   second line;
6. every JSON error response carries ``X-Content-Type-Options: nosniff``;
7. the plugin defaults to verifying TLS, has no baked-in platform address, and
   keeps the two opt-in downgrades;
8. the test-only gates carry no credential or address literals.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = REPO_ROOT.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

_TMP = Path(tempfile.mkdtemp(prefix="cpypi-security-"))
_PACKAGES = _TMP / "packages"
_PACKAGES.mkdir(parents=True, exist_ok=True)

#: A throwaway instance with a throwaway administrator, like every other gate.
_ADMIN = "securityadmin"
_PASSWORD = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
os.environ["API_KEYS_FILE"] = str(_TMP / "security.db")
os.environ["PACKAGES_DIR"] = str(_PACKAGES)
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_USERNAME"] = _ADMIN
os.environ["AUTH_ASSERT"] = _PASSWORD
os.environ["OAUTH2_INTROSPECT_URL"] = ""
os.environ["OAUTH2_AUTHORIZE_URL"] = ""
os.environ["ADMIN_USERS"] = f'["{_ADMIN}"]'

from app import app  # noqa: E402 - imported late so the env above applies

from services import logsafe  # noqa: E402
from services.model_routes import normalize_api_key  # noqa: E402
from services.paths import contained, safe_name  # noqa: E402
from services.urlsafety import UnsafeUrlError, check_outbound_url  # noqa: E402
from services.validation import validate_file  # noqa: E402

_AUTH = {"Authorization": "Basic " + base64.b64encode(f"{_ADMIN}:{_PASSWORD}".encode()).decode()}
_PLUGIN_SRC = (
    PROJECT_ROOT / "integrations" / "dsh-plugin-enterprise-intranet" / "lib" / "index.js"
)

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def _sample_wheel(name: str) -> bytes:
    """A minimal but structurally real wheel, writable under *name*."""
    dist_info = f"{name}-1.0.0.dist-info"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        wheel.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: check_security\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0.0\n",
        )
    return buffer.getvalue()


# ── 1/2. Path traversal ──────────────────────────────────────────────

def check_paths() -> None:
    print("── path traversal ──────────────────────────────────────────────")
    check(safe_name("../../etc/passwd") == "etc_passwd", "safe_name flattens a traversal name")
    check(
        safe_name("..\\..\\evil.whl") == "evil.whl",
        "safe_name strips a Windows-style traversal",
    )
    for bad in ("../outside", "a/../../b", "/etc/passwd"):
        try:
            contained(_PACKAGES, bad)
            check(False, f"contained refuses {bad!r}")
        except ValueError:
            check(True, f"contained refuses {bad!r}")
    check(contained(_PACKAGES, "ok.whl").parent == _PACKAGES, "contained keeps a plain name")

    # The real pipeline: a traversal filename must come back *reduced*, which is
    # what makes both the echo (reflected XSS) and the destination (traversal)
    # safe downstream.
    payload = _sample_wheel("evil")
    upload = io.BytesIO(payload)
    upload.filename = "../../evil-1.0.0-py3-none-any.whl"
    is_valid, result = validate_file(upload, upload.filename)
    check(is_valid, f"validate_file accepts a real wheel (got {result!r})")
    check(result == "evil-1.0.0-py3-none-any.whl", f"validate_file reduces the name (got {result!r})")
    check(not (_PACKAGES.parent / "evil-1.0.0-py3-none-any.whl").exists(),
          "nothing was written outside PACKAGES_DIR")


# ── 3/4. Outbound URL + header safety ────────────────────────────────

def check_urls() -> None:
    print()
    print("── outbound URL guard ──────────────────────────────────────────")
    # Literal addresses, not names: the guard resolves the host, and a gate must
    # not depend on DNS.  10.0.0.0/8 is a private range and stays allowed — the
    # model endpoints this product fronts are intranet services.
    allowed = check_outbound_url("http://10.0.0.5:8000/v1")
    check(allowed.startswith("http://10.0.0.5:8000/"), "a plain intranet endpoint passes")
    for bad, label in (
        ("file:///etc/passwd", "a file:// URL is refused"),
        ("http://user:pw@10.0.0.5/v1", "embedded credentials are refused"),
        ("http://169.254.169.254/latest/meta-data/", "the link-local metadata address is refused"),
        ("http://100.100.100.200/latest/meta-data/", "the CGNAT metadata address is refused"),
        ("http://metadata.google.internal/", "the metadata hostname is refused"),
    ):
        try:
            check_outbound_url(bad)
            check(False, label)
        except UnsafeUrlError:
            check(True, label)

    allowlist = ["10.0.0.5:8000"]
    try:
        check_outbound_url("http://10.0.0.6:8000/v1", allowed_hosts=allowlist)
        check(False, "a non-allow-listed host is refused")
    except UnsafeUrlError:
        check(True, "a non-allow-listed host is refused")
    check(
        check_outbound_url("http://10.0.0.5:8000/v1", allowed_hosts=allowlist).startswith("http://10.0.0.5"),
        "an allow-listed host passes",
    )

    try:
        normalize_api_key("sk-abc\r\nX-Injected: 1")
        check(False, "a CR/LF-bearing api_key is refused")
    except ValueError:
        check(True, "a CR/LF-bearing api_key is refused")
    check(normalize_api_key("  sk-abc  ") == "sk-abc", "a normal api_key is trimmed and kept")


# ── 5. Log forging ───────────────────────────────────────────────────

def check_logs() -> None:
    print()
    print("── log injection ───────────────────────────────────────────────")
    logger = logging.getLogger("cpypiserver.gate.security")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        logsafe.install(logger)
        logger.warning("document %s", "evil\n2026-01-01 [cpypiserver] CRITICAL: forged")
    finally:
        logger.removeHandler(handler)

    lines = buffer.getvalue().splitlines()
    check(len(lines) == 1, f"a newline in a logged value stays on one line (got {len(lines)})")
    check(
        bool(lines) and "\\n2026-01-01" in lines[0] and "evil" in lines[0],
        "the control character is escaped and the value is still visible",
    )


# ── 6. Error envelope ────────────────────────────────────────────────

def check_error_headers() -> None:
    print()
    print("── JSON error envelope ─────────────────────────────────────────")
    client = app.test_client()
    # Both are domain errors routed through extensions/error_handlers.py: an
    # invalid bound query (the flask-openapi3 callback) and a missing project
    # (the PypiError handler).  Neither may answer without `nosniff`, because an
    # error body quotes the input it rejected.
    for url, want in (("/simple/?format=xml", 400), ("/simple/no-such-project-xyz/", 404)):
        response = client.get(url, headers=_AUTH)
        check(
            response.status_code == want,
            f"GET {url} -> {response.status_code} (want {want})",
        )
        check(
            response.headers.get("X-Content-Type-Options") == "nosniff",
            f"GET {url} carries X-Content-Type-Options: nosniff",
        )


# ── 7/8. Plugin + fixture hygiene ────────────────────────────────────

def check_plugin() -> None:
    print()
    print("── enterprise-intranet plugin ──────────────────────────────────")
    source = _PLUGIN_SRC.read_text(encoding="utf-8")
    check("verifyTls: true" in source, "the plugin default verifies TLS")
    check("verifyTls = true" in source, "requestJson verifies TLS unless told otherwise")
    check("platformUrl: ''" in source, "no platform address is baked into the plugin")
    check("47.97.243.86" not in source, "the vendor IP is gone from the plugin source")
    check("VERIFY_TLS !== true" not in source, "the generated helper has no implicit downgrade")
    check("cfg.verifyTls !== true" not in source, "gitconfig has no implicit downgrade")
    check("rejectUnauthorized = false" in source, "the explicit opt-in downgrade still exists")
    check(
        "catch { options.rejectUnauthorized = false }" not in source,
        "an unreadable CA file fails closed instead of disabling verification",
    )

    print()
    print("── credential literals in test-only gates ──────────────────────")
    forbidden = {
        "backend/scripts/check_docker_proxy.py": ("devpass",),
        "backend/scripts/check_device_flow.py": (
            "device-gate-secret",
            "ZGV2aWNlYWRtaW46",
            "upstream-secret-key",
            "env-injected-key",
        ),
    }
    for relative, needles in forbidden.items():
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for needle in needles:
            check(needle not in text, f"{relative} no longer contains {needle!r}")


def main() -> int:
    check_paths()
    check_urls()
    check_logs()
    check_error_headers()
    check_plugin()

    print()
    if failures:
        print(f"❌ security check FAILED — {len(failures)} problem(s)")
        return 1
    print("✅ security check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
