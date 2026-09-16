"""Debian offline relay — the air-gap protocol between two openfish hubs.

An internet-connected deployment can reach an apt mirror; an intranet one
cannot.  This module is the *protocol* that moves packages across that gap in
three deliberately human-carryable artifacts, and nothing else in the codebase
needs to know about it:

1. **snapshot** (internet → intranet) — a text file describing every package the
   internet deployment can offer: name, version, architecture, the apt
   ``Filename:`` it lives at, size, SHA-256 and its dependency fields.  It is
   generated from the apt ``Packages`` indexes of the configured
   suites/components/arches, local mirror first, upstream second.
2. **plan** (intranet → internet) — the intranet compares the snapshot against
   the ``.deb`` files it already holds and writes a text file listing exactly
   what it needs: new packages, upgrades, and the transitive dependency closure
   computed from the snapshot's own ``Depends``/``Pre-Depends`` fields.
3. **bundle** (internet → intranet) — the internet side reads the plan,
   re-resolves each request against its *own* package metadata (a plan is a
   request, not an instruction), downloads the files, verifies size and
   SHA-256, and packs them into a ``.tar.gz`` that carries its own manifest.

Importing the bundle unpacks the ``.deb`` files into ``DEBIAN_DIR`` so the flat
``/debian/Packages`` repository — and the local-first half of the apt proxy —
serve the updated set with no further step.

Why a text format instead of just ``Packages``
----------------------------------------------
The artifacts cross an air gap by hand, often by USB stick and sometimes through
a person retyping a package list on a console.  A tab-separated, self-describing
document that a human can read, ``grep`` and diff is worth more there than a
binary blob; the trailing ``sha256`` line catches a truncated copy, and every
document carries a ``columns`` header so a future field can be added without
breaking an older reader.

Honest limits, stated up front
------------------------------
* The dependency resolver is conservative, not a full apt solver: it picks the
  highest available version satisfying each constraint, honours ``Provides`` for
  virtual packages, and reports anything it cannot satisfy instead of silently
  dropping it.  It never removes a package and does not model ``Conflicts``.
* ``Architecture: all`` packages satisfy every architecture; a package built for
  a different architecture does not.
* The plan's SHA-256 comparison for already-present packages is opt-in
  (``verify_hashes``), because hashing an entire local repository on every plan
  is expensive; by default "same version" means "up to date".
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cmp_to_key
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import unquote

from config import settings
from services import debian_apt
from services.digest import compute_sha256, sha256_or_none
from services.format import human_size
from services.upstream import CHUNK, UpstreamError, stream_into

logger = logging.getLogger("cpypiserver.debian.offline")

# ── Document identities ──────────────────────────────────────────────
SNAPSHOT_MAGIC = "OFDEB-SNAPSHOT 1"
PLAN_MAGIC = "OFDEB-PLAN 1"
BUNDLE_MAGIC = "OFDEB-BUNDLE 1"

#: The manifest's name inside a bundle.
BUNDLE_MANIFEST = "openfish-debian-bundle.txt"
#: The flat apt index a bundle carries for the pool files it contains.
BUNDLE_INDEX = "Packages"

#: Snapshot columns, in file order.  ``columns`` in the header names them so a
#: reader binds by name rather than by position and a new field can be appended.
SNAPSHOT_COLUMNS = (
    "name", "version", "arch", "suite", "component", "filename", "size",
    "sha256", "depends", "pre_depends", "provides", "recommends",
    "essential", "priority", "section", "description",
)
PLAN_COLUMNS = ("action", "name", "version", "arch", "filename", "size", "sha256", "source")
BUNDLE_COLUMNS = ("name", "version", "arch", "filename", "size", "sha256", "verified_from")

#: A ``Packages`` document is tens of megabytes at most; this is the ceiling
#: that turns an unexpectedly huge upstream body into an error instead of an OOM.
_MAX_INDEX_BYTES = 512 * 1024 * 1024

#: The warning a plan carries when an artifact's own trailing digest does not
#: match its body.  Kept as a constant so the gate can assert the wording.
INTEGRITY_WARNING = (
    "文件自校验不一致：可能被截断或手工编辑。协议仍会继续，但请核对内容。"
)


# ── Small helpers ────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).strftime("%Y%m%dT%H%M%SZ")


def _cell(value: Any) -> str:
    """One tab-separated cell: a tab or newline would break the row shape."""
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def split_list(value: str | Iterable[str] | None) -> list[str]:
    """Normalise a configured list: commas, whitespace and repeats all collapse."""
    if value is None:
        return []
    items = value if not isinstance(value, str) else re.split(r"[\s,]+", value)
    seen: list[str] = []
    for item in items:
        cleaned = str(item).strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Tabular text documents ───────────────────────────────────────────
#
# One encoder/decoder pair backs all three artifacts.  Shape:
#
#     <MAGIC>
#     # <title>
#     <header key>\t<value>
#     columns\t<name> <name> ...
#     #
#     <row cells joined by tabs>
#     # end
#     sha256\t<hex of every byte above>
#
# The trailer makes a truncated copy detectable.  Parsing keeps the integrity
# verdict rather than raising, because a hand-edited snapshot is a legitimate
# thing for an operator to try and the plan should say so instead of failing.

def encode_document(
    magic: str,
    *,
    title: str,
    header: Sequence[tuple[str, str]],
    columns: Sequence[str],
    rows: Iterable[dict[str, Any]],
) -> str:
    out = [magic, "#", f"# {title}", "#"]
    for key, value in header:
        out.append(f"{key}\t{_cell(value)}")
    out.append("columns\t" + " ".join(columns))
    out.append("#")
    for row in rows:
        out.append("\t".join(_cell(row.get(name, "")) for name in columns))
    out.append("# end")
    body = "\n".join(out) + "\n"
    return body + f"sha256\t{_sha256_bytes(body.encode('utf-8'))}\n"


@dataclass
class Document:
    """A parsed tabular document plus its integrity verdict."""

    magic: str
    header: dict[str, str]
    columns: list[str]
    rows: list[dict[str, str]]
    body_sha256: str
    declared_sha256: str = ""
    integrity_ok: bool = True

    def get(self, key: str, default: str = "") -> str:
        return self.header.get(key, default)


def decode_document(text: str, *, expected_magic: str | None = None) -> Document:
    """Parse a relay document.  Raises ``ValueError`` on a malformed shape.

    The integrity flag is reported, never fatal: ``expected_magic`` guards
    against feeding the wrong artifact to the wrong step, which *is* fatal
    because there is no sensible interpretation of it.
    """
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise ValueError("空文档")
    magic = lines[0].strip()
    if expected_magic is not None and magic != expected_magic:
        raise ValueError(f"文档类型不匹配：期望 {expected_magic!r}，实际 {magic!r}")

    # The trailer is the first (and only) line whose key is `sha256`; rows never
    # start with that token, so locating it needs no state.
    trailer_index = len(lines)
    declared = ""
    for index, line in enumerate(lines):
        key, sep, value = line.partition("\t")
        if index > 0 and sep and key == "sha256":
            trailer_index, declared = index, value.strip()
            break
    body_lines = lines[:trailer_index]

    header: dict[str, str] = {}
    columns: list[str] = []
    rows: list[dict[str, str]] = []
    in_rows = False
    for line in body_lines[1:]:
        if line.startswith("#") or not line.strip():
            continue
        if in_rows:
            cells = line.split("\t")
            rows.append(
                {columns[i]: (cells[i] if i < len(cells) else "") for i in range(len(columns))}
            )
            continue
        key, sep, value = line.partition("\t")
        if key == "columns":
            columns = value.split()
            in_rows = True
        elif sep:
            header[key.strip()] = value.strip()
    if not columns:
        raise ValueError("文档缺少 columns 表头")

    body_text = "\n".join(body_lines) + "\n"
    computed = _sha256_bytes(body_text.encode("utf-8"))
    return Document(
        magic=magic,
        header=header,
        columns=columns,
        rows=rows,
        body_sha256=computed,
        declared_sha256=declared,
        integrity_ok=not declared or declared == computed,
    )


# ── apt Packages parsing ─────────────────────────────────────────────

def parse_packages_index(text: str) -> list[dict[str, str]]:
    """Parse an apt ``Packages`` index into one dict per stanza.

    Continuation lines (leading whitespace) append to the previous field.  A
    blank line ends a stanza.
    """
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_key: str | None = None
    for raw in text.splitlines():
        if not raw.strip():
            if current:
                stanzas.append(current)
                current = {}
                last_key = None
            continue
        if raw[0] in (" ", "\t"):
            if last_key:
                current[last_key] = f"{current[last_key]} {raw.strip()}".strip()
            continue
        key, sep, value = raw.partition(":")
        if not sep:
            continue
        last_key = key.strip()
        current[last_key] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


# ── Packages ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Package:
    """One installable package version, as described by a ``Packages`` index."""

    name: str
    version: str
    arch: str
    suite: str = ""
    component: str = ""
    filename: str = ""
    size: int = 0
    sha256: str = ""
    depends: str = ""
    pre_depends: str = ""
    provides: str = ""
    recommends: str = ""
    essential: str = "no"
    priority: str = ""
    section: str = ""
    description: str = ""

    def as_row(self, columns: Sequence[str] = SNAPSHOT_COLUMNS) -> dict[str, Any]:
        return {name: getattr(self, name, "") for name in columns}

    def as_packages_stanza(self) -> str:
        """Render one apt ``Packages`` stanza, pointing at the pool path."""
        lines = [
            f"Package: {self.name}",
            f"Version: {self.version or '0'}",
            f"Architecture: {self.arch or 'all'}",
            f"Filename: {self.filename}",
            f"Size: {self.size}",
        ]
        if self.sha256:
            lines.append(f"SHA256: {self.sha256}")
        if self.depends:
            lines.append(f"Depends: {self.depends}")
        if self.pre_depends:
            lines.append(f"Pre-Depends: {self.pre_depends}")
        if self.provides:
            lines.append(f"Provides: {self.provides}")
        if self.section:
            lines.append(f"Section: {self.section}")
        if self.priority:
            lines.append(f"Priority: {self.priority}")
        if self.essential and self.essential.lower() in ("yes", "true"):
            lines.append("Essential: yes")
        lines.append(f"Description: {self.description or self.name}")
        return "\n".join(lines)


def package_from_stanza(
    stanza: dict[str, str], *, suite: str, component: str, fallback_arch: str
) -> Package | None:
    """Turn one ``Packages`` stanza into a :class:`Package`, or ``None``.

    A stanza without a name, version or ``Filename:`` cannot be fetched, so it
    is dropped rather than listed with an address apt would fail to resolve.
    """
    name = (stanza.get("Package") or "").strip()
    version = (stanza.get("Version") or "").strip()
    filename = (stanza.get("Filename") or "").strip()
    if not name or not version or not filename:
        return None
    raw_size = (stanza.get("Size") or "0").strip()
    description = (stanza.get("Description") or "").split("\n", 1)[0].strip()
    return Package(
        name=name,
        version=version,
        arch=(stanza.get("Architecture") or fallback_arch).strip() or fallback_arch,
        suite=suite,
        component=component,
        filename=filename.lstrip("./"),
        size=int(raw_size) if raw_size.isdigit() else 0,
        sha256=(stanza.get("SHA256") or "").strip().lower(),
        depends=(stanza.get("Depends") or "").strip(),
        pre_depends=(stanza.get("Pre-Depends") or "").strip(),
        provides=(stanza.get("Provides") or "").strip(),
        recommends=(stanza.get("Recommends") or "").strip(),
        essential=(stanza.get("Essential") or "no").strip(),
        priority=(stanza.get("Priority") or "").strip(),
        section=(stanza.get("Section") or "").strip(),
        description=description,
    )


def render_packages(packages: Iterable[Package]) -> str:
    """A flat apt ``Packages`` index for *packages*, sorted for reproducibility."""
    stanzas = [
        pkg.as_packages_stanza()
        for pkg in sorted(packages, key=lambda p: (p.name, p.version, p.arch))
    ]
    return "\n\n".join(stanzas) + ("\n" if stanzas else "")


# ── Debian version comparison (dpkg semantics) ───────────────────────
#
# Ported from dpkg's ``verrevcmp``.  The rule that catches people out is ``~``:
# it sorts *before* everything, including the empty string, which is how
# ``1.0~rc1`` is older than ``1.0``.

def _order(char: str) -> int:
    if not char:
        return 0
    if char.isdigit():
        return 0
    if char.isalpha():
        return ord(char)
    if char == "~":
        return -1
    return ord(char) + 256


def _verrevcmp(left: str, right: str) -> int:
    i = j = 0
    while i < len(left) or j < len(right):
        first_diff = 0
        while (i < len(left) and not left[i].isdigit()) or (
            j < len(right) and not right[j].isdigit()
        ):
            a = _order(left[i]) if i < len(left) else 0
            b = _order(right[j]) if j < len(right) else 0
            if a != b:
                return a - b
            i += 1
            j += 1
        while i < len(left) and left[i] == "0":
            i += 1
        while j < len(right) and right[j] == "0":
            j += 1
        while i < len(left) and j < len(right) and left[i].isdigit() and right[j].isdigit():
            if not first_diff:
                first_diff = ord(left[i]) - ord(right[j])
            i += 1
            j += 1
        if i < len(left) and left[i].isdigit():
            return 1
        if j < len(right) and right[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def _split_version(version: str) -> tuple[int, str, str]:
    epoch = 0
    rest = version or ""
    head, sep, tail = rest.partition(":")
    if sep and head.isdigit():
        epoch, rest = int(head), tail
    upstream, sep, revision = rest.rpartition("-")
    if not sep:
        upstream, revision = rest, ""
    return epoch, upstream, revision


def compare_versions(left: str, right: str) -> int:
    """Return ``-1`` / ``0`` / ``1`` for *left* older than / equal to / newer."""
    left_epoch, left_upstream, left_rev = _split_version(left)
    right_epoch, right_upstream, right_rev = _split_version(right)
    if left_epoch != right_epoch:
        return -1 if left_epoch < right_epoch else 1
    verdict = _verrevcmp(left_upstream, right_upstream)
    if verdict:
        return -1 if verdict < 0 else 1
    verdict = _verrevcmp(left_rev, right_rev)
    if verdict:
        return -1 if verdict < 0 else 1
    return 0


_VERSION_KEY = cmp_to_key(compare_versions)


# ── .deb filename parsing ────────────────────────────────────────────

_DEB_SUFFIX = ".deb"

#: ``<pkg>_<version>_<arch>.deb``.  apt percent-encodes a version's epoch colon
#: (``2%3a9.1-1``), so the version cell is decoded before it is compared.
_DEB_RE = re.compile(r"^(?P<name>[^_]+)_(?P<version>[^_]+)_(?P<arch>[^_]+)\.deb$", re.I)


@dataclass(frozen=True)
class DebFile:
    name: str
    version: str
    arch: str
    filename: str


def parse_deb_filename(filename: str) -> DebFile | None:
    """Parse a ``.deb`` basename into name/version/arch, or ``None``."""
    base = Path(filename).name
    if not base.lower().endswith(_DEB_SUFFIX):
        return None
    match = _DEB_RE.match(base)
    if not match:
        return None
    return DebFile(
        name=match.group("name"),
        version=unquote(match.group("version")),
        arch=unquote(match.group("arch")),
        filename=base,
    )


def scan_local_packages(root: str | Path) -> dict[tuple[str, str], Package]:
    """Index the ``.deb`` files under *root* by ``(name, architecture)``.

    The scan is recursive so both layouts work: a flat repository at the root
    and a mirror-shaped ``pool/...`` tree beneath it.  When two files describe
    the same ``(name, arch)`` the newer version wins, which mirrors what apt
    would actually install.
    """
    base = Path(root)
    inventory: dict[tuple[str, str], Package] = {}
    if not base.is_dir():
        return inventory
    for path in sorted(base.rglob(f"*{_DEB_SUFFIX}")):
        if not path.is_file() or path.name.startswith("."):
            continue
        parsed = parse_deb_filename(path.name)
        if parsed is None:
            logger.debug("ignoring unparseable .deb name: %s", path)
            continue
        entry = Package(
            name=parsed.name,
            version=parsed.version,
            arch=parsed.arch,
            filename=path.relative_to(base).as_posix(),
            size=path.stat().st_size,
        )
        key = (parsed.name, parsed.arch)
        current = inventory.get(key)
        if current is None or compare_versions(entry.version, current.version) > 0:
            inventory[key] = entry
    return inventory


# ── Dependency expressions ───────────────────────────────────────────

_RELATION_RE = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9+.-]*)"
    r"(?::(?P<profile>[a-z0-9-]+))?"
    r"(?:\s*\(\s*(?P<op><<|<=|=|>=|>>)\s*(?P<version>[^)]+?)\s*\))?$",
    re.IGNORECASE,
)

#: The shape a real Debian package name has; reused to filter `Provides:` tokens.
_PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


@dataclass(frozen=True)
class Alternative:
    name: str
    op: str = ""
    version: str = ""


def parse_dependency_groups(expression: str) -> list[list[Alternative]]:
    """Parse a ``Depends`` field into groups of alternatives.

    ``a (>= 1) | b, c:any`` becomes ``[[a>=1, b], [c]]``: satisfied when one
    member of *each* group is available.
    """
    groups: list[list[Alternative]] = []
    for clause in (expression or "").split(","):
        alternatives: list[Alternative] = []
        for part in clause.split("|"):
            item = part.strip()
            if not item:
                continue
            match = _RELATION_RE.match(item)
            if match:
                alternatives.append(
                    Alternative(
                        name=match.group("name").lower(),
                        op=(match.group("op") or "").strip(),
                        version=(match.group("version") or "").strip(),
                    )
                )
            else:
                # A relation this parser does not model (a build-profile
                # restriction, say).  Keep the name so it stays visible.
                alternatives.append(Alternative(name=item.split()[0].lower()))
        if alternatives:
            groups.append(alternatives)
    return groups


def satisfies(version: str, op: str, wanted: str) -> bool:
    if not op or not wanted:
        return True
    verdict = compare_versions(version, wanted)
    if op == ">=":
        return verdict >= 0
    if op == "<=":
        return verdict <= 0
    if op == "=":
        return verdict == 0
    if op == ">>":
        return verdict > 0
    if op == "<<":
        return verdict < 0
    return True


# ── The package universe ─────────────────────────────────────────────

@dataclass
class Universe:
    """Every package version a snapshot knows, indexed for resolution."""

    packages: list[Package] = field(default_factory=list)
    #: ``(name, arch) -> newest Package``.
    best: dict[tuple[str, str], Package] = field(default_factory=dict)
    #: ``(name, version, arch) -> Package``.
    exact: dict[tuple[str, str, str], Package] = field(default_factory=dict)
    #: ``virtual name -> [(provider name, arch, provided version or "")]``.
    providers: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    #: Every real package name, for a cheap membership test.
    names: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, packages: Iterable[Package]) -> "Universe":
        universe = cls()
        for pkg in packages:
            universe.packages.append(pkg)
            universe.names.add(pkg.name)
            universe.exact[(pkg.name, pkg.version, pkg.arch)] = pkg
            key = (pkg.name, pkg.arch)
            current = universe.best.get(key)
            if current is None or compare_versions(pkg.version, current.version) > 0:
                universe.best[key] = pkg
            for token in re.split(r"[\s,]+", pkg.provides or ""):
                # ``Provides: mail-transport-agent, foo (= 1.2)``: only the bare
                # package names matter, so relation operators and versions are
                # dropped by the name-shape test rather than by a keyword list.
                name = token.strip().lower()
                if not _PACKAGE_NAME_RE.match(name):
                    continue
                universe.providers.setdefault(name, []).append(
                    (pkg.name, pkg.arch, pkg.version)
                )
        return universe

    def candidates(self, name: str, arch: str) -> list[Package]:
        """Every version of *name* usable on *arch* (exact architecture or all)."""
        return [
            pkg for pkg in self.packages
            if pkg.name == name and pkg.arch in (arch, "all")
        ]

    def best_for(self, name: str, arch: str, alt: Alternative) -> Package | None:
        """The newest candidate for *alt* on *arch* that meets its constraint."""
        pool = [
            pkg for pkg in self.candidates(name, arch)
            if satisfies(pkg.version, alt.op, alt.version)
        ]
        if not pool:
            return None
        return max(pool, key=lambda pkg: _VERSION_KEY(pkg.version))


# ── Loading the universe from an apt mirror ──────────────────────────

def _decode_index(data: bytes, name: str) -> str:
    """Decode a ``Packages`` document, transparently un-gzipping/un-xz-ing it."""
    lowered = name.lower()
    if lowered.endswith(".gz"):
        import gzip

        data = gzip.decompress(data)
    elif lowered.endswith((".xz", ".lzma")):
        import lzma

        data = lzma.decompress(data)
    return data.decode("utf-8", errors="replace")


def _local_index(suite: str, component: str, arch: str) -> tuple[str | None, str]:
    """Read one ``Packages`` index from the local mirror tree, if present."""
    relative = f"{suite}/{component}/binary-{arch}/Packages"
    for candidate in (relative, f"{relative}.gz", f"{relative}.xz"):
        try:
            path = debian_apt.local_mirror_file("dists", candidate)
        except debian_apt.PathError:
            path = None
        if path is not None:
            try:
                return _decode_index(path.read_bytes(), path.name), f"local:{candidate}"
            except OSError as exc:  # pragma: no cover - unreadable local file
                logger.warning("cannot read local apt index %s: %s", path, exc)
    return None, ""


def load_packages_index(
    suite: str, component: str, arch: str, *, fresh: bool = False
) -> tuple[str | None, str]:
    """Load one ``Packages`` index, local mirror first, then the upstream proxy.

    Returns ``(text, source)``; ``text`` is ``None`` when neither the local tree
    nor the upstream mirror carries the combination — which the caller reports
    as a note rather than an error, because a mirror legitimately lacks e.g.
    ``bookworm-security/binary-arm64`` on some days.
    """
    text, source = _local_index(suite, component, arch)
    if text is not None:
        return text, source
    if not debian_apt.configured():
        return None, ""

    relative = f"dists/{suite}/{component}/binary-{arch}/Packages"
    store = debian_apt.cache()
    client = debian_apt.upstream()
    for candidate in (relative, f"{relative}.gz", f"{relative}.xz"):
        key = f"aptoffline:{debian_apt.effective_upstream()}|{candidate}"
        if not fresh:
            cached = store.get(key, max_age=int(settings.hub.debian_metadata_ttl))
            if cached is not None:
                try:
                    return _decode_index(cached.read_bytes(), candidate), f"cache:{candidate}"
                except OSError:
                    pass
        try:
            frozen = client.get_bytes(candidate, max_bytes=_MAX_INDEX_BYTES)
        except UpstreamError as exc:
            logger.info("apt offline index %s unavailable: %s", candidate, exc)
            continue
        if frozen.status_code >= 400:
            continue
        # Cache the bytes exactly as they arrived (a `.gz` stays compressed);
        # `_decode_index` is told the candidate name on the way back out.
        store.put_bytes(key, frozen.content)
        return _decode_index(frozen.content, candidate), f"upstream:{candidate}"
    return None, ""


def _dedupe(packages: Iterable[Package]) -> list[Package]:
    """Collapse identical entries (the same file offered by two suites)."""
    seen: dict[tuple[str, str, str, str], Package] = {}
    for pkg in packages:
        key = (pkg.name, pkg.version, pkg.arch, pkg.filename)
        seen.setdefault(key, pkg)
    return list(seen.values())


def load_universe(
    *,
    suites: Sequence[str] | None = None,
    components: Sequence[str] | None = None,
    arches: Sequence[str] | None = None,
    fresh: bool = False,
    seed: Iterable[Package] = (),
) -> tuple[Universe, list[dict[str, str]]]:
    """Build the universe of every package an internet deployment can offer.

    Returns the universe plus one note per suite/component/arch combination that
    could not be loaded, so a snapshot can say what it was *unable* to see
    instead of quietly under-reporting.
    """
    suite_list = list(suites) if suites else split_list(settings.hub.debian_suites)
    component_list = list(components) if components else split_list(settings.hub.debian_components)
    arch_list = list(arches) if arches else split_list(settings.hub.debian_arches)
    notes: list[dict[str, str]] = []
    packages: list[Package] = list(seed)

    for suite in suite_list:
        for component in component_list:
            for arch in arch_list:
                text, source = load_packages_index(suite, component, arch, fresh=fresh)
                if text is None:
                    notes.append(
                        {
                            "suite": suite,
                            "component": component,
                            "arch": arch,
                            "reason": "Packages 索引不可用（本地与上游都未命中）",
                        }
                    )
                    continue
                for stanza in parse_packages_index(text):
                    pkg = package_from_stanza(
                        stanza, suite=suite, component=component, fallback_arch=arch
                    )
                    if pkg is not None:
                        packages.append(pkg)
                logger.debug(
                    "apt offline index %s/%s/binary-%s loaded from %s",
                    suite, component, arch, source,
                )
    return Universe.build(_dedupe(packages)), notes


# ── Snapshot ─────────────────────────────────────────────────────────

@dataclass
class Snapshot:
    text: str
    sha256: str
    filename: str
    package_count: int
    generated: str
    suites: list[str]
    components: list[str]
    arches: list[str]
    notes: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "package_count": self.package_count,
            "generated": self.generated,
            "suites": self.suites,
            "components": self.components,
            "arches": self.arches,
            "notes": self.notes,
        }


def build_snapshot(
    *,
    suites: Sequence[str] | None = None,
    components: Sequence[str] | None = None,
    arches: Sequence[str] | None = None,
    fresh: bool = False,
) -> Snapshot:
    """Describe every package the internet deployment can currently offer."""
    suite_list = list(suites) if suites else split_list(settings.hub.debian_suites)
    component_list = list(components) if components else split_list(settings.hub.debian_components)
    arch_list = list(arches) if arches else split_list(settings.hub.debian_arches)
    moment = _now()
    filename = f"openfish-debian-snapshot-{_stamp(moment)}.txt"
    universe, notes = load_universe(
        suites=suite_list, components=component_list, arches=arch_list, fresh=fresh,
    )
    packages = sorted(universe.packages, key=lambda p: (p.name, p.version, p.arch))
    header = [
        ("file", filename),
        ("generated", _iso(moment)),
        ("server", settings.server.server_name),
        ("upstream", debian_apt.effective_upstream() or "local"),
        ("suites", " ".join(suite_list)),
        ("components", " ".join(component_list)),
        ("arches", " ".join(arch_list)),
        ("count", str(len(packages))),
        ("notes", str(len(notes))),
        ("hint", "columns 之后每行一个包；文件末尾 sha256 行是此前全部字节的摘要"),
    ]
    for index, note in enumerate(notes[:50], start=1):
        header.append(
            (
                f"missing_index_{index}",
                f"{note['suite']}/{note['component']}/binary-{note['arch']}",
            )
        )
    text = encode_document(
        SNAPSHOT_MAGIC,
        title="openfish Debian 离线快照 —— 互联网侧可提供的全部软件包状态",
        header=header,
        columns=SNAPSHOT_COLUMNS,
        rows=[pkg.as_row() for pkg in packages],
    )
    return Snapshot(
        text=text,
        sha256=_sha256_bytes(text.encode("utf-8")),
        filename=filename,
        package_count=len(packages),
        generated=_iso(moment),
        suites=suite_list,
        components=component_list,
        arches=arch_list,
        notes=notes,
    )


def snapshot_from_text(text: str) -> tuple[list[Package], Document]:
    """Parse a snapshot document into packages plus its raw header."""
    document = decode_document(text, expected_magic=SNAPSHOT_MAGIC)
    packages: list[Package] = []
    for row in document.rows:
        name = row.get("name", "").strip()
        version = row.get("version", "").strip()
        filename = row.get("filename", "").strip()
        if not name or not version or not filename:
            continue
        raw_size = row.get("size", "")
        packages.append(
            Package(
                name=name,
                version=version,
                arch=row.get("arch", "").strip() or "all",
                suite=row.get("suite", "").strip(),
                component=row.get("component", "").strip(),
                filename=filename,
                size=int(raw_size) if raw_size.isdigit() else 0,
                sha256=row.get("sha256", "").strip().lower(),
                depends=row.get("depends", "").strip(),
                pre_depends=row.get("pre_depends", "").strip(),
                provides=row.get("provides", "").strip(),
                recommends=row.get("recommends", "").strip(),
                essential=row.get("essential", "no").strip(),
                priority=row.get("priority", "").strip(),
                section=row.get("section", "").strip(),
                description=row.get("description", "").strip(),
            )
        )
    return packages, document


# ── Plan (the intranet-side diff) ────────────────────────────────────

@dataclass
class PlanRow:
    action: str
    package: Package
    source: str

    def as_row(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "name": self.package.name,
            "version": self.package.version,
            "arch": self.package.arch,
            "filename": self.package.filename,
            "size": self.package.size,
            "sha256": self.package.sha256,
            "source": self.source,
        }


@dataclass
class Plan:
    text: str
    sha256: str
    filename: str
    rows: list[PlanRow]
    summary: dict[str, int]
    warnings: list[str]
    unresolved: list[str]
    snapshot_sha256: str
    generated: str

    @property
    def package_count(self) -> int:
        return len(self.rows)

    @property
    def total_bytes(self) -> int:
        return sum(row.package.size for row in self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "package_count": self.package_count,
            "total_bytes": self.total_bytes,
            "total_size_human": human_size(self.total_bytes),
            "summary": self.summary,
            "warnings": self.warnings,
            "unresolved": self.unresolved,
            "snapshot_sha256": self.snapshot_sha256,
            "generated": self.generated,
        }


def _local_entry(
    inventory: dict[tuple[str, str], Package], name: str, arch: str
) -> Package | None:
    return inventory.get((name, arch)) or inventory.get((name, "all"))


def _classify(
    pkg: Package,
    inventory: dict[tuple[str, str], Package],
    base: Path,
    *,
    verify_hashes: bool,
    allow_downgrade: bool,
) -> tuple[str, str] | None:
    """Return ``(action, source)`` for a snapshot package, or ``None`` to skip.

    ``None`` means the local repository already has that package and version.
    """
    local = _local_entry(inventory, pkg.name, pkg.arch)
    if local is None:
        return "install", "missing"
    verdict = compare_versions(pkg.version, local.version)
    if verdict > 0:
        return "upgrade", "outdated"
    if verdict < 0:
        if allow_downgrade:
            return "downgrade", "downgrade"
        return None
    if verify_hashes and pkg.sha256:
        local_sha = sha256_or_none(base / local.filename)
        if local_sha and local_sha != pkg.sha256:
            return "reinstall", "sha-mismatch"
    return None


def build_plan(
    snapshot_text: str,
    *,
    root: str | Path | None = None,
    only: Sequence[str] | None = None,
    allow_downgrade: bool = False,
    verify_hashes: bool = False,
    recommends: bool | None = None,
) -> Plan:
    """Diff a snapshot against the local ``.deb`` repository and emit a plan.

    Without *only* the plan is a full mirror sync: every package the snapshot
    offers that the local repository lacks or has at an older version.  With
    *only* the direct pass is restricted to the named packages (a targeted
    update), and the dependency closure supplies whatever those packages need —
    which is where the closure earns its keep.
    """
    moment = _now()
    filename = f"openfish-debian-plan-{_stamp(moment)}.txt"
    packages, document = snapshot_from_text(snapshot_text)
    universe = Universe.build(packages)
    base = Path(root) if root is not None else Path(settings.hub.debian_dir)
    inventory = scan_local_packages(base)
    include_recommends = (
        settings.hub.debian_offline_recommends if recommends is None else recommends
    )
    wanted = {name.strip().lower() for name in (only or []) if name.strip()}

    selected: dict[tuple[str, str], PlanRow] = {}
    warnings: list[str] = []
    if not document.integrity_ok:
        warnings.append(INTEGRITY_WARNING)

    # 1. Direct differences, one per (name, arch) in the universe.
    for key, pkg in sorted(universe.best.items()):
        if wanted and pkg.name.lower() not in wanted:
            continue
        verdict = _classify(
            pkg, inventory, base,
            verify_hashes=verify_hashes, allow_downgrade=allow_downgrade,
        )
        if verdict is None:
            continue
        action, source = verdict
        selected[key] = PlanRow(action=action, package=pkg, source=source)

    missing_direct = sorted(name for name in wanted if name not in universe.names)
    for name in missing_direct:
        warnings.append(f"快照中没有名为 {name!r} 的包；它不会被纳入本次更新")

    # 2. Transitive dependency closure over what will be fetched.
    arch_contexts = split_list(document.get("arches")) or ["all"]
    queue: list[tuple[Package, str]] = [
        (row.package, row.package.arch if row.package.arch != "all" else arch_contexts[0])
        for row in selected.values()
    ]
    unresolved: list[str] = []
    while queue:
        pkg, context = queue.pop(0)
        for arch_context in (arch_contexts if pkg.arch == "all" else [context]):
            groups: list[list[Alternative]] = []
            for field_value in (pkg.pre_depends, pkg.depends):
                groups.extend(parse_dependency_groups(field_value))
            if include_recommends:
                groups.extend(parse_dependency_groups(pkg.recommends))
            for group in groups:
                chosen, satisfied = _resolve_group(
                    group, arch_context, universe, inventory, selected,
                )
                if chosen is None and not satisfied:
                    unresolved.append(
                        f"{pkg.name} {pkg.version} ({arch_context}) 需要 "
                        + " | ".join(alt.name for alt in group)
                    )
                    continue
                if chosen is None:
                    continue
                key = (chosen.name, chosen.arch)
                if key in selected:
                    continue
                action, source = _classify(
                    chosen, inventory, base,
                    verify_hashes=verify_hashes, allow_downgrade=allow_downgrade,
                )
                if action is None:
                    action, source = "install", "dependency"
                selected[key] = PlanRow(action=action, package=chosen, source="dependency")
                queue.append((chosen, chosen.arch if chosen.arch != "all" else arch_context))

    rows = sorted(
        selected.values(),
        key=lambda row: (row.package.name, row.package.arch, row.package.version),
    )
    summary = _plan_summary(
        rows, universe, unresolved, verify_hashes, allow_downgrade, include_recommends,
    )
    snapshot_name = document.get("file", "snapshot.txt")
    header = [
        ("file", filename),
        ("generated", _iso(moment)),
        ("server", settings.server.server_name),
        ("snapshot", snapshot_name),
        ("snapshot_sha256", _sha256_bytes(snapshot_text.encode("utf-8"))),
        ("snapshot_integrity", "ok" if document.integrity_ok else "mismatch"),
        ("suites", document.get("suites")),
        ("components", document.get("components")),
        ("arches", document.get("arches")),
        ("hash_policy", "sha256" if verify_hashes else "version-only"),
        ("allow_downgrade", "yes" if allow_downgrade else "no"),
        ("recommends", "yes" if include_recommends else "no"),
        ("only", " ".join(sorted(wanted))),
        ("count", str(len(rows))),
        ("bytes", str(sum(row.package.size for row in rows))),
        ("unresolved", str(len(unresolved))),
    ]
    if warnings:
        header.append(("warning", " / ".join(warnings)))
    for index, message in enumerate(unresolved[:50], start=1):
        header.append((f"unresolved_{index}", message))

    text = encode_document(
        PLAN_MAGIC,
        title="openfish Debian 待更新清单 —— 内网侧相对快照需要补齐的软件包",
        header=header,
        columns=PLAN_COLUMNS,
        rows=[row.as_row() for row in rows],
    )
    return Plan(
        text=text,
        sha256=_sha256_bytes(text.encode("utf-8")),
        filename=filename,
        rows=rows,
        summary=summary,
        warnings=warnings,
        unresolved=unresolved,
        snapshot_sha256=_sha256_bytes(snapshot_text.encode("utf-8")),
        generated=_iso(moment),
    )


def _resolve_group(
    group: list[Alternative],
    arch: str,
    universe: Universe,
    inventory: dict[tuple[str, str], Package],
    selected: dict[tuple[str, str], PlanRow],
) -> tuple[Package | None, bool]:
    """Pick one member of a dependency group.

    Returns ``(package, satisfied)``: ``(None, True)`` when the local repository
    or the pending update already satisfies the group, ``(pkg, False)`` when
    *pkg* must be fetched, and ``(None, False)`` when nothing can satisfy it
    (reported as unresolved).
    """
    # 1. Already satisfied locally?
    for alt in group:
        local = _local_entry(inventory, alt.name, arch)
        if local is not None and satisfies(local.version, alt.op, alt.version):
            return None, True

    # 2. Already queued for this update?
    for alt in group:
        for candidate_arch in (arch, "all"):
            row = selected.get((alt.name, candidate_arch))
            if row is not None and satisfies(row.package.version, alt.op, alt.version):
                return None, True

    # 3. The newest available candidate: a real package first, then a provider.
    for alt in group:
        chosen = universe.best_for(alt.name, arch, alt)
        if chosen is not None:
            return chosen, False
        for provider, provider_arch, provided_version in universe.providers.get(alt.name, []):
            if provider_arch not in (arch, "all"):
                continue
            if alt.op and alt.version and not satisfies(provided_version, alt.op, alt.version):
                continue
            chosen = universe.best_for(provider, arch, Alternative(name=provider))
            if chosen is not None:
                return chosen, False
    return None, False


def _plan_summary(
    rows: list[PlanRow],
    universe: Universe,
    unresolved: list[str],
    verify_hashes: bool,
    allow_downgrade: bool,
    recommends: bool,
) -> dict[str, int]:
    from collections import Counter

    actions = Counter(row.action for row in rows)
    sources = Counter(row.source for row in rows)
    return {
        "total": len(rows),
        "install": actions.get("install", 0),
        "upgrade": actions.get("upgrade", 0),
        "reinstall": actions.get("reinstall", 0),
        "downgrade": actions.get("downgrade", 0),
        "direct": sum(1 for row in rows if row.source != "dependency"),
        "dependency": sources.get("dependency", 0),
        "unresolved": len(unresolved),
        "universe": len(universe.best),
        "hash_policy_sha256": 1 if verify_hashes else 0,
        "allow_downgrade": 1 if allow_downgrade else 0,
        "recommends": 1 if recommends else 0,
    }


def plan_from_text(text: str) -> tuple[list[dict[str, str]], Document]:
    document = decode_document(text, expected_magic=PLAN_MAGIC)
    return document.rows, document


# ── Bundle (internet-side download + pack) ───────────────────────────

@dataclass
class Bundle:
    path: Path
    filename: str
    sha256: str
    size: int
    bundled: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    plan_sha256: str
    plan_integrity_ok: bool
    generated: str
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "size": self.size,
            "size_human": human_size(self.size),
            "total_bytes": self.total_bytes,
            "total_size_human": human_size(self.total_bytes),
            "packages": len(self.bundled),
            "skipped": len(self.skipped),
            "skipped_packages": self.skipped[:50],
            "plan_sha256": self.plan_sha256,
            "plan_integrity": "ok" if self.plan_integrity_ok else "mismatch",
            "generated": self.generated,
        }


def _candidate_local_files(filename: str, root: Path) -> list[Path]:
    base = Path(filename).name
    return [root / filename, root / base, root / "pool" / base]


def _fetch_package(filename: str, destination: Path, *, root: Path) -> tuple[bool, str]:
    """Materialise one package file at *destination*; return ``(ok, origin)``.

    The local repository is tried first (a synced ``pool/`` tree wins over the
    network), then the upstream mirror streams the file to disk.
    """
    for candidate in _candidate_local_files(filename, root):
        try:
            if candidate.is_file():
                shutil.copyfile(candidate, destination)
                return True, "local"
        except OSError:
            continue
    if not debian_apt.configured():
        return False, "no-upstream"
    client = debian_apt.upstream()
    try:
        resp = client.request("GET", filename, stream=True)
    except UpstreamError as exc:
        logger.warning("offline bundle fetch %s failed: %s", filename, exc)
        return False, "unreachable"
    if resp.status_code >= 400:
        status = resp.status_code
        resp.close()
        logger.info("offline bundle fetch %s -> %s", filename, status)
        return False, f"upstream-{status}"
    try:
        stream_into(resp, destination)
    except (UpstreamError, OSError) as exc:  # pragma: no cover - network abort
        logger.warning("offline bundle fetch %s failed mid-body: %s", filename, exc)
        return False, "download-failed"
    return True, "upstream"


def snapshot_from_text_rows(rows: list[dict[str, str]]) -> list[Package]:
    """A seed universe built from a plan's own rows.

    Used when the internet side has no upstream metadata to re-resolve against:
    the plan already carries the exact ``Filename:`` and SHA-256 it observed, so
    the same rows become the fallback universe and verification still happens
    against a real digest rather than being skipped.
    """
    seed: list[Package] = []
    for row in rows:
        name = row.get("name", "").strip()
        version = row.get("version", "").strip()
        filename = row.get("filename", "").strip()
        if not name or not version or not filename:
            continue
        raw_size = row.get("size", "")
        seed.append(
            Package(
                name=name,
                version=version,
                arch=row.get("arch", "").strip() or "all",
                filename=filename,
                size=int(raw_size) if raw_size.isdigit() else 0,
                sha256=row.get("sha256", "").strip().lower(),
            )
        )
    return seed


def build_bundle(
    plan_text: str,
    *,
    root: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> Bundle:
    """Resolve a plan against current metadata, download it, and pack a bundle."""
    moment = _now()
    rows, document = plan_from_text(plan_text)
    base = Path(root) if root is not None else Path(settings.hub.debian_dir)
    target_dir = (
        Path(output_dir) if output_dir is not None else Path(settings.hub.debian_offline_dir)
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    declared_total = sum(
        int(row["size"]) for row in rows if row.get("size", "").isdigit()
    )
    ceiling = max(int(settings.hub.debian_offline_max_mb), 0) * 1024 * 1024
    if ceiling and declared_total > ceiling:
        raise ValueError(
            f"待更新清单声明的总大小 {human_size(declared_total)} 超过 "
            f"DEBIAN_OFFLINE_MAX_MB={settings.hub.debian_offline_max_mb} 的上限，"
            "已拒绝开始下载。请提高上限或缩小清单。"
        )

    # Re-resolve the requested suites/components/arches from *our* metadata: a
    # plan is a request, and the authoritative filename/sha256 is ours.  The
    # plan's own rows seed the universe so a deployment without upstream access
    # can still verify-and-pack what it was asked for.
    universe, _notes = load_universe(
        suites=split_list(document.get("suites")) or None,
        components=split_list(document.get("components")) or None,
        arches=split_list(document.get("arches")) or None,
        seed=snapshot_from_text_rows(rows),
    )

    staging = Path(tempfile.mkdtemp(prefix="openfish-bundle-", dir=str(target_dir)))
    bundled: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        for row in rows:
            name = row.get("name", "").strip()
            version = row.get("version", "").strip()
            arch = row.get("arch", "").strip()
            if not name or not version:
                continue
            resolved = universe.exact.get((name, version, arch))
            filename = (resolved.filename if resolved else row.get("filename", "")).strip()
            sha256 = (
                resolved.sha256 if resolved and resolved.sha256
                else row.get("sha256", "")
            ).strip().lower()
            raw_size = row.get("size", "")
            size = int(raw_size) if raw_size.isdigit() else (resolved.size if resolved else 0)
            verified_from = "metadata" if resolved else "plan"
            if not filename:
                skipped.append(
                    {"name": name, "version": version, "arch": arch, "reason": "清单缺少 filename"}
                )
                continue
            arcname = filename if filename.startswith("pool/") else f"pool/{Path(filename).name}"
            destination = staging / arcname
            destination.parent.mkdir(parents=True, exist_ok=True)
            ok, origin = _fetch_package(filename, destination, root=base)
            if not ok:
                skipped.append(
                    {"name": name, "version": version, "arch": arch,
                     "filename": filename, "reason": origin}
                )
                continue
            actual_size = destination.stat().st_size
            actual_sha = compute_sha256(destination, sidecar=False)
            problems: list[str] = []
            if size and actual_size != size:
                problems.append(f"size {actual_size} != {size}")
            if sha256 and actual_sha != sha256:
                problems.append("sha256 不一致")
            if problems:
                skipped.append(
                    {"name": name, "version": version, "arch": arch,
                     "filename": filename, "reason": "; ".join(problems)}
                )
                destination.unlink(missing_ok=True)
                continue
            bundled.append(
                {
                    "name": name,
                    "version": version,
                    "arch": arch,
                    "filename": arcname,
                    "size": actual_size,
                    "sha256": actual_sha,
                    "verified_from": verified_from,
                    "origin": origin,
                }
            )

        # The bundle's own flat index lets an operator serve the unpacked
        # directory (`deb [trusted=yes] file:/.../ ./`) without openfish.
        index_packages = [
            Package(
                name=item["name"], version=item["version"], arch=item["arch"],
                filename=item["filename"], size=item["size"], sha256=item["sha256"],
            )
            for item in bundled
        ]
        (staging / BUNDLE_INDEX).write_text(render_packages(index_packages), encoding="utf-8")

        plan_name = document.get("file", "plan.txt")
        manifest = encode_document(
            BUNDLE_MAGIC,
            title="openfish Debian 离线更新包 —— 互联网侧打包的 .deb 与校验清单",
            header=[
                ("file", ""),
                ("generated", _iso(moment)),
                ("server", settings.server.server_name),
                ("upstream", debian_apt.effective_upstream() or "local"),
                ("plan", plan_name),
                ("plan_sha256", _sha256_bytes(plan_text.encode("utf-8"))),
                ("plan_integrity", "ok" if document.integrity_ok else "mismatch"),
                ("count", str(len(bundled))),
                ("bytes", str(sum(item["size"] for item in bundled))),
                ("skipped", str(len(skipped))),
                ("index", BUNDLE_INDEX),
                ("layout", "pool/<apt Filename>"),
            ],
            columns=BUNDLE_COLUMNS,
            rows=bundled,
        )
        (staging / BUNDLE_MANIFEST).write_text(manifest, encoding="utf-8")

        filename = f"openfish-debian-bundle-{_stamp(moment)}.tar.gz"
        out_path = target_dir / filename
        _write_tar(staging, out_path, scratch_dir=target_dir)
        sha256 = compute_sha256(out_path, sidecar=False)
        total_bytes = sum(item["size"] for item in bundled)
        return Bundle(
            path=out_path,
            filename=filename,
            sha256=sha256,
            size=out_path.stat().st_size,
            bundled=bundled,
            skipped=skipped,
            plan_sha256=_sha256_bytes(plan_text.encode("utf-8")),
            plan_integrity_ok=document.integrity_ok,
            generated=_iso(moment),
            total_bytes=total_bytes,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _write_tar(staging: Path, out_path: Path, *, scratch_dir: Path) -> None:
    """Pack *staging* into a gzip tarball, atomically.

    Members are added in sorted order and keep their own mtime, so an operator
    can see when each package was fetched.
    """
    fd, tmp_name = tempfile.mkstemp(prefix=".bundle-", suffix=".part", dir=str(scratch_dir))
    os.close(fd)
    try:
        with tarfile.open(tmp_name, "w:gz") as tar:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(staging).as_posix(), recursive=False)
        os.replace(tmp_name, out_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


# ── Import (intranet-side unpack) ────────────────────────────────────

@dataclass
class ImportReport:
    imported: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    failed: list[dict[str, Any]]
    total_bytes: int
    manifest_sha256: str
    generated: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported": len(self.imported),
            "skipped": len(self.skipped),
            "failed": len(self.failed),
            "total_bytes": self.total_bytes,
            "total_size_human": human_size(self.total_bytes),
            "manifest_sha256": self.manifest_sha256,
            "generated": self.generated,
            "imported_packages": self.imported[:100],
            "skipped_packages": self.skipped[:50],
            "failed_packages": self.failed[:50],
        }


def _safe_member_path(root: Path, name: str) -> Path:
    """Resolve a tar member under *root*, refusing escapes."""
    if not name or name.startswith(("/", "\\")) or "\\" in name or "\x00" in name:
        raise ValueError(f"拒绝不安全的包内路径：{name!r}")
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"拒绝越界的包内路径：{name!r}")
    return target


def _extract_bundle(source: Path, staging: Path) -> tuple[str | None, int]:
    """Extract a bundle's regular files into *staging*.

    Returns ``(manifest text, file count)``.  Directories are created as needed;
    symlinks, hardlinks and device nodes are refused because a repository is a
    directory of ordinary files and nothing in this protocol needs them.
    """
    manifest_text: str | None = None
    count = 0
    with tarfile.open(source, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isdir():
                _safe_member_path(staging, member.name).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"离线包内含不允许的成员类型：{member.name!r}")
            target = _safe_member_path(staging, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:  # pragma: no cover - isfile() guards this
                continue
            with extracted, open(target, "wb") as handle:
                shutil.copyfileobj(extracted, handle, CHUNK)
            count += 1
            if Path(member.name).name == BUNDLE_MANIFEST:
                manifest_text = target.read_text(encoding="utf-8", errors="replace")
    return manifest_text, count


def _publish_flat(destination: Path, base: Path) -> None:
    """Publish a pool file at the repository root for the flat ``Packages`` index.

    A hard link costs no extra space and keeps the two layouts the same inode;
    on a filesystem that refuses links (or across a mount point) it falls back
    to a copy, and a failure there is a warning rather than an error because the
    canonical ``pool/`` copy is already in place.
    """
    flat = base / destination.name
    if flat == destination or flat.exists():
        return
    try:
        os.link(destination, flat)
        return
    except OSError:
        pass
    try:
        shutil.copyfile(destination, flat)
    except OSError as exc:  # pragma: no cover - read-only repo
        logger.warning("cannot publish flat copy of %s: %s", destination, exc)


def import_bundle(
    bundle_path: str | Path,
    *,
    root: str | Path | None = None,
) -> ImportReport:
    """Verify and unpack a bundle into the local ``.deb`` repository.

    Nothing is written into the repository until *every* declared file has been
    extracted and verified, so a truncated or corrupted bundle leaves the repo
    exactly as it was.  Files already present with the same digest are reported
    as skipped rather than rewritten, which makes a repeated import a no-op.
    """
    moment = _now()
    base = Path(root) if root is not None else Path(settings.hub.debian_dir)
    base.mkdir(parents=True, exist_ok=True)
    source = Path(bundle_path)
    manifest_sha256 = compute_sha256(source, sidecar=False)
    staging = Path(tempfile.mkdtemp(prefix="openfish-import-", dir=str(base)))

    failed: list[dict[str, Any]] = []
    try:
        try:
            manifest_text, _count = _extract_bundle(source, staging)
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise ValueError(f"离线包无法解包（不是有效的 gzip tar 归档）：{exc}") from exc
        if manifest_text is None:
            raise ValueError("离线包缺少 openfish-debian-bundle.txt 清单")
        document = decode_document(manifest_text, expected_magic=BUNDLE_MAGIC)
        if not document.integrity_ok:
            raise ValueError("离线包清单自校验失败，拒绝导入")

        # Phase 1 — verify every declared file before touching the repository.
        validated: list[tuple[dict[str, str], Path]] = []
        for row in document.rows:
            filename = row.get("filename", "")
            try:
                staged = _safe_member_path(staging, filename)
            except ValueError as exc:
                failed.append({**row, "reason": str(exc)})
                continue
            if not staged.is_file():
                failed.append({**row, "reason": "包内缺少文件"})
                continue
            expected_sha = row.get("sha256", "").strip().lower()
            if expected_sha and compute_sha256(staged, sidecar=False) != expected_sha:
                failed.append({**row, "reason": "sha256 校验失败"})
                continue
            raw_size = row.get("size", "")
            if raw_size.isdigit() and int(raw_size) != staged.stat().st_size:
                failed.append({**row, "reason": "大小校验失败"})
                continue
            validated.append((row, staged))
        if failed:
            sample = failed[0]
            raise ValueError(
                f"离线包校验失败：{len(failed)} 个文件不可用，未写入任何文件"
                f"（示例：{sample.get('filename', '?')} — {sample.get('reason', '?')}）"
            )

        # Phase 2 — publish.
        imported: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        total_bytes = 0
        for row, staged in validated:
            filename = row["filename"]
            destination = _safe_member_path(base, filename)
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = row.get("sha256", "").strip().lower()
            if digest and destination.is_file() and sha256_or_none(destination) == digest:
                skipped.append({"filename": filename, "reason": "已存在且校验一致"})
                continue
            os.replace(staged, destination)
            _publish_flat(destination, base)
            size = destination.stat().st_size
            total_bytes += size
            imported.append(
                {
                    "filename": filename,
                    "size": size,
                    "sha256": digest,
                    "name": row.get("name", ""),
                    "version": row.get("version", ""),
                    "arch": row.get("arch", ""),
                }
            )

        # Keep the manifest as provenance, in a dotted sub-directory so the flat
        # catalog's `iterdir()` scan does not list it as an artifact.
        try:
            provenance = base / ".openfish" / BUNDLE_MANIFEST
            provenance.parent.mkdir(parents=True, exist_ok=True)
            provenance.write_text(manifest_text, encoding="utf-8")
        except OSError as exc:  # pragma: no cover - read-only repo
            logger.warning("cannot store bundle provenance in %s: %s", base, exc)

        return ImportReport(
            imported=imported,
            skipped=skipped,
            failed=failed,
            total_bytes=total_bytes,
            manifest_sha256=manifest_sha256,
            generated=_iso(moment),
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ── Status (the SPA overview) ────────────────────────────────────────

def status_payload() -> dict[str, Any]:
    """Describe the relay's configuration and the bundles already built."""
    offline_dir = Path(settings.hub.debian_offline_dir)
    bundles: list[dict[str, Any]] = []
    if offline_dir.is_dir():
        for path in sorted(offline_dir.glob("*.tar.gz")):
            try:
                st = path.stat()
            except OSError:
                continue
            bundles.append(
                {
                    "filename": path.name,
                    "size": st.st_size,
                    "size_human": human_size(st.st_size),
                    "modified": _iso(datetime.fromtimestamp(st.st_mtime, timezone.utc)),
                }
            )
    bundles.sort(key=lambda item: item["modified"], reverse=True)
    return {
        "configured": debian_apt.configured(),
        "upstream": debian_apt.effective_upstream(),
        "root": settings.hub.debian_dir,
        "offline_dir": str(offline_dir),
        "suites": split_list(settings.hub.debian_suites),
        "components": split_list(settings.hub.debian_components),
        "arches": split_list(settings.hub.debian_arches),
        "recommends": bool(settings.hub.debian_offline_recommends),
        "max_mb": int(settings.hub.debian_offline_max_mb),
        "bundle_count": len(bundles),
        "bundles": bundles[:50],
    }


__all__ = [
    "BUNDLE_INDEX",
    "BUNDLE_MANIFEST",
    "INTEGRITY_WARNING",
    "PLAN_MAGIC",
    "SNAPSHOT_MAGIC",
    "Alternative",
    "Bundle",
    "Document",
    "ImportReport",
    "Package",
    "Plan",
    "PlanRow",
    "Snapshot",
    "Universe",
    "build_bundle",
    "build_plan",
    "build_snapshot",
    "compare_versions",
    "decode_document",
    "encode_document",
    "import_bundle",
    "load_universe",
    "parse_deb_filename",
    "parse_dependency_groups",
    "parse_packages_index",
    "plan_from_text",
    "render_packages",
    "satisfies",
    "scan_local_packages",
    "snapshot_from_text",
    "split_list",
    "status_payload",
]
