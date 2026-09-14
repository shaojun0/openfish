"""Pydantic request models and shared dataclasses."""

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field


class FormatQuery(BaseModel):
    """Optional ``?format=json`` on the simple API."""

    format: str | None = Field(default=None, pattern=r"^(json)?$")


# ── Package file ─────────────────────────────────────────────────────

@dataclass
class PackageFile:
    filename: str
    path: str
    url: str
    package_name: str
    version: str
    packagetype: str = "sdist"
    python_version: str = "source"
    requires_python: Optional[str] = None
    size: int = 0
    upload_time: Optional[str] = None
    md5_digest: Optional[str] = None
    sha256_digest: Optional[str] = None


# ── Python-build file ────────────────────────────────────────────────

@dataclass
class PythonBuildFile:
    filename: str
    path: str
    release_tag: str       # e.g. "20260602"
    flavor: str            # "cpython"
    version: str           # e.g. "3.12.13"
    full_version: str      # e.g. "cpython-3.12.13+20260602"
    target_triple: str     # e.g. "aarch64-unknown-linux-gnu"
    variant: str           # e.g. "install_only_stripped"
    extension: str         # e.g. "tar.gz"
    size: int = 0
    sha256_digest: Optional[str] = None


# ── Sort helpers ─────────────────────────────────────────────────────

OS_ORDER = {
    "apple-darwin": "macOS",
    "unknown-linux-gnu": "Linux (glibc)",
    "unknown-linux-musl": "Linux (musl)",
    "pc-windows-msvc": "Windows",
}

ARCH_ORDER = {
    "x86_64": 0, "aarch64": 1, "armv7": 2,
    "i686": 3, "ppc64le": 4, "riscv64": 5, "s390x": 6,
}

VARIANT_ORDER = {
    "install_only_stripped": 0, "install_only": 1,
    "pgo+lto-full": 2, "lto-full": 3,
    "pgo-full": 4, "debug-full": 5, "noopt-full": 6,
}
