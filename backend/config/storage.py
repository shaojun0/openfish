"""Package storage and index configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from config.base import EnvSettings
from config.paths import backend_path, catalog_path


class StorageConfig(EnvSettings):
    """Package storage, upload limits and the database location.

    Also answers **which medium the catalogs live on**.  ``services/objectstore``
    is the only reader of these fields: the tools catalog and the documentation
    objects go through it, and the local directory is just the default backend —
    the same keys against an S3 bucket is the other one.  The S3 names are
    deliberately the ones every S3-compatible deployment already uses
    (``S3_ENDPOINT`` / ``S3_BUCKET_NAME`` / ``S3_ADDRESS_STYLE``), so pointing
    openfish at MinIO or Ceph is the same block an operator writes for any other
    tool.
    """

    packages_dir: str = Field(
        default=backend_path("packages"),
        description="Path to the packages directory",
    )
    max_content_length: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        description="Maximum upload body size in bytes",
    )
    allow_extensions: list[str] = Field(
        default=["whl", "zip", "tar.gz", "tar"],
        description="Allowed file extensions",
    )
    watch_packages: bool = Field(
        default=True,
        description="Use watchdog to maintain in-memory index",
    )
    python_builds_dir: str = Field(
        default=catalog_path("python-build-standalone"),
        description="Path to python-build-standalone releases directory",
    )
    node_builds_dir: str = Field(
        default=catalog_path("node-builds"),
        description=(
            "Path to a nodejs.org/dist-shaped mirror of prebuilt Node.js "
            "archives (release directories named vX.Y.Z), consumed by nvm, fnm "
            "and node-gyp through NODEJS_ORG_MIRROR"
        ),
    )
    database_url: str | None = Field(
        default=None,
        description=(
            "SQLAlchemy database URL for API keys, users, RBAC and statistics. "
            "Empty (the default) keeps the historical single-file SQLite "
            "database at API_KEYS_FILE. Set it to a PostgreSQL URL — e.g. "
            "postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/openfish "
            "— to run the whole "
            "authorization/API-key/statistics layer on PostgreSQL instead. "
            "A bare postgresql:// URL is upgraded to the psycopg (v3) driver."
        ),
    )
    api_keys_file: str = Field(
        default=backend_path("data", "cpypiserver.db"),
        description=(
            "SQLite database path for API keys and stats. Used only when "
            "DATABASE_URL is empty; it is the file that is migrated in place by "
            "the lightweight ALTERs in extensions/database.py."
        ),
    )
    stats_snapshot_file: str = Field(
        default=backend_path("data", "stats_snapshot.json"),
        description=(
            "Where the last successful statistics computation is persisted, so "
            "a restart answers the overview page from the previous result "
            "instead of recomputing it behind the first request. The container "
            "default lives next to the database on the data volume "
            "(/app/data/stats_snapshot.json)."
        ),
    )

    # ── Catalog medium ───────────────────────────────────────────────
    seed_catalogs: bool = Field(
        default=True,
        description=(
            "Install the shipped default catalogs into an empty database at "
            "initialization time (services/catalog_seed). On by default: a "
            "fresh deployment should have documentation and tools to look at. "
            "A test harness that needs a genuinely empty catalog turns it off."
        ),
    )
    object_backend: Literal["local", "s3"] = Field(
        default="local",
        description=(
            "Where the artifact catalogs and the documentation objects live. "
            "`local` (the default) maps an object key to a path under the "
            "catalog root; `s3` puts the same keys in an S3-compatible bucket "
            "(MinIO, Ceph RGW, cloud S3) and needs the `s3` extra — "
            "`pip install 'cpypiserver[s3]'`, or build the image with "
            "`--build-arg OPENFISH_EXTRAS=s3`."
        ),
    )
    s3_endpoint: str = Field(
        default="",
        description=(
            "S3-compatible endpoint URL, e.g. http://minio:9000. Empty means "
            "the default AWS endpoint for the region."
        ),
    )
    s3_bucket_name: str = Field(
        default="",
        description=(
            "Bucket holding every catalog. One bucket with a per-catalog prefix "
            "rather than a bucket per catalog: a bucket is the unit an operator "
            "grants access to, a prefix is the unit the application names. The "
            "bucket must already exist — the server never creates one, because "
            "that needs a permission the runtime should not hold."
        ),
    )
    s3_region: str = Field(
        default="",
        description=(
            "Signing region. Empty means us-east-1: a signature needs *a* "
            "region, and an S3-compatible server usually ignores which one."
        ),
    )
    s3_access_key: str = Field(default="", description="S3 access key id")
    s3_secret_key: str = Field(
        default="",
        description="S3 secret access key. Never logged, never echoed back.",
    )
    s3_address_style: Literal["auto", "virtual", "path"] = Field(
        default="auto",
        description=(
            "Bucket addressing. MinIO and Ceph need `path`; AWS accepts `auto`."
        ),
    )
    s3_prefix: str = Field(
        default="",
        description=(
            "Optional key prefix inside the bucket, e.g. `openfish`. The catalog "
            "name is appended to it (`<prefix>/tools/…`), so several deployments "
            "can share one bucket."
        ),
    )

    # ── Duplicate policy (an existing filename) ─────────────────────
    # One mode replaces the two historical booleans, so there is no "both on"
    # state to define: `conflict` is the old "both switches off", `overwrite` is
    # the old `OVERWRITE=true`, `skip` is the old `SKIP_EXISTING=true` (twine's
    # `--skip-existing` semantics).  Read it through `allows_overwrite` /
    # `skips_existing`, never by comparing the string at each call site.
    on_duplicate: Literal["conflict", "overwrite", "skip"] = Field(
        default="conflict",
        description=(
            "What an upload does when the target filename already exists. "
            "`conflict` (the default) refuses it — `400` + \"already exists\" for "
            "twine, `409` for every other client; that is the historical answer "
            "with both old switches off. "
            "`overwrite` replaces the stored file — the old "
            "`STORAGE__OVERWRITE=true`, and the switch the npm / tools / docker / "
            "debian upload endpoints read. "
            "`skip` treats the upload as a successful no-op that writes nothing — "
            "the old `STORAGE__SKIP_EXISTING=true`, and the server-side "
            "equivalent of twine's `--skip-existing`, which twine 6.2.0 removed "
            "for indexes other than PyPI/TestPyPI. `skip` applies to the PyPI "
            "upload endpoint; the artifact catalogs have no skip protocol, so a "
            "duplicate there still answers `409` unless the mode is `overwrite`."
        ),
    )
    # Deprecated input-only aliases: they keep a deployment that already sets
    # `OVERWRITE` (the compose variable) or `STORAGE__SKIP_EXISTING` working.
    # Folded into `on_duplicate` below and deliberately excluded from dumps;
    # new code must use `allows_overwrite` / `skips_existing`.
    overwrite: bool | None = Field(
        default=None,
        exclude=True,
        description="Deprecated: `true` means `on_duplicate=overwrite`.",
    )
    skip_existing: bool | None = Field(
        default=None,
        exclude=True,
        description="Deprecated: `true` means `on_duplicate=skip`.",
    )

    @model_validator(mode="after")
    def _fold_deprecated_duplicate_switches(self) -> "StorageConfig":
        """Translate the two old booleans into the single mode.

        Only an explicit `true` folds; `false` is the old default and stays a
        no-op.  A legacy switch combined with a non-default `on_duplicate` is
        contradictory, so it fails at start-up instead of silently picking one.
        """
        legacy = (
            "overwrite" if self.overwrite
            else "skip" if self.skip_existing
            else None
        )
        if legacy is None:
            return self
        # A validated instance is re-validated by the settings tree (the default
        # `StorageConfig()` is also read by `Settings`), so folding must be
        # idempotent or the second pass would mistake its own result for a
        # contradiction.
        if self.on_duplicate == legacy:
            return self
        if self.on_duplicate != "conflict":
            raise ValueError(
                "set STORAGE__ON_DUPLICATE or the deprecated "
                "STORAGE__OVERWRITE / STORAGE__SKIP_EXISTING switches, not both"
            )
        self.on_duplicate = legacy
        return self

    @property
    def allows_overwrite(self) -> bool:
        """`overwrite` mode: replace an existing file instead of refusing it."""
        return self.on_duplicate == "overwrite"

    @property
    def skips_existing(self) -> bool:
        """`skip` mode: an existing file is a successful no-op (PyPI upload)."""
        return self.on_duplicate == "skip"
