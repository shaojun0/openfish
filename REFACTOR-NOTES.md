# REFACTOR-NOTES — catalog namespaces, decoupled

Branch `agent/mirror-decouple`, base `main` = `7924adc`.  Production code only
(`backend/services`, `backend/routes`, `backend/models`, `backend/config`,
`backend/extensions`, `backend/cli.py`); no logging was added anywhere.

## Why this shape

The hub has five catalogs, and every fact about one of them used to be spelled
out wherever it was first needed:

| fact | was | now |
| --- | --- | --- |
| deployment directory (`TOOLS_DIR`, …) | `config/hub.py`, re-read in `mirror_catalog`, `objectstore`, `catalog_seed`, `tool_catalog` | `services/namespaces.Namespace.root` |
| does a row own bytes | implicit (`docs`/`tools` assumed) | `Namespace.owns_objects` |
| which table holds the rows | `catalog_seed._has_rows` branch on `"docs"` | `Namespace.rows` |
| overlay top-level JSON key | `mirror_catalog.top_key` | `Namespace.overlay_key` |
| default overlay path | `mirror_catalog.default_path` map | `Namespace.overlay_path` |
| shipped seed file | `catalog_seed.CATALOGS` literal list | `Namespace.seed_file` |
| namespace spellings | literals in `mirror_catalog` **and** all three routes | registry constants (`namespaces.NPM`, …) |

`services/namespaces.py` is the one answer.  Consumers no longer know *which*
namespaces exist — they ask the registry: `MIRRORS`, `SEEDED`, `root_for()`,
`overlay_key()`, `overlay_path()`.

The second theme is the **three layers** of a mirror catalog, now stated and
enforced instead of assumed:

* the **file** is the artifact (it stays in the operator's directory);
* the **row** is the description (`catalog_entries`, `storage_key = NO_STORAGE_KEY`);
* the **directory** (and the S3 prefix) is a port — `objectstore.catalog_store`
  still decides the medium, and the registry only supplies the local default.

`scan_npm` / `scan_flat` no longer read `catalog.json`; they take an
`Overlay` (a `TypedDict`) built from rows by `mirror_catalog.overlay`.  That is
what makes "the file is the import format, not a source of truth" true in the
request path rather than only in a comment.

## Adding a namespace (the `node-builds` question)

If the new namespace is **overlay-shaped** — files in a directory plus
`catalog_entries` rows describing them — then it is one registration in
`services/namespaces.NAMESPACES` (plus one `config/seed/<ns>.seed.sql` only if it
should ship defaults).  Everything downstream follows automatically:

* `cli.py catalogs import/export --namespace <ns>` gains the choice
  (`choices=list(mirror_catalog.MIRRORS)`, derived);
* `mirror_catalog.overlay/import_file/export_file/default_path` accept it;
* `catalog_seed` installs its defaults, in registry order;
* `objectstore`/`tool_catalog` resolve its root.

If instead the namespace needs a **different scanner or field set** — which is
the case for the existing `node-builds` (`services/index/node_build.py` reads a
nodejs.org-shaped tree with an `index.json`, not rows), and would be for a
`packages` namespace with its own layout — registering it still buys the
directory/seed/validation bookkeeping, but the import/scan code is genuinely new
and belongs in that namespace's own module.  The registry deliberately does not
try to be a plugin system.

Concretely, for `node-builds` as a DB-backed mirror today you would:

1. add `NODE_BUILDS = "node-builds"` and one `Namespace(...)` entry
   (`root=settings.storage.node_builds_dir`, `owns_objects=False`,
   `rows=ENTRY_ROWS`, `overlay_key="builds"` or whatever its key is);
2. write the import/scan path (or point `mirror_catalog` at it if the shape is
   `{"<key>": [items]}` like the others).

Step 2 is the part no registry can absorb, and pretending otherwise is what the
old code did with `docs`.

## Public API changes (verifier: read this)

New:

* `services.namespaces` — the whole module (`Namespace`, `NAMESPACES`,
  `MIRRORS`, `SEEDED`, `SEED_DIR`, `OVERLAY_FILENAME`, `DOCS`/`TOOLS`/`NPM`/
  `DEBIAN`/`DOCKER_IMAGES`, `DOCUMENT_ROWS`/`ENTRY_ROWS`, `get`, `resolve`,
  `root_for`, `overlay_key`, `overlay_path`).
* `services.hub.Overlay`, `services.hub.OverlayItem` — `TypedDict`s, exported.
* `services.mirror_catalog.entry_fields(namespace, item)` — was the private
  `_fields`; the seed-generator recipe builds the same columns, so the mapping
  from JSON key to column is public (and remains the only copy).
* `services.mirror_catalog.fingerprint(overlay)` — the npm registry cache key,
  moved out of `routes/npm.py` (which no longer imports `json`).
* `models.catalog.NO_FILE`, `models.catalog.NO_STORAGE_KEY` — the empty-string
  sentinels (value unchanged: `""`).

Signature/annotation changes (runtime behaviour unchanged):

* `services.hub.scan_npm/scan_flat/scan_docker/scan_debian`: `overlay` is
  annotated `Overlay | None` instead of `dict[str, Any] | None`.
* `services.mirror_catalog.overlay()` returns `Overlay` (was `dict`); `path_for`
  takes `OverlayItem`; `MIRRORS` is now derived from the registry but has the
  same value and order `("npm", "debian", "docker-images")`.
* `services.npm_registry.NpmRegistry.overlay` annotated `hub.Overlay`.
* `services.catalog_seed`: `CATALOGS` and `SEED_DIR` keep their value and
  meaning (now derived/aliased); `ensure_seed` is unchanged.  Private helpers
  changed: `_seed_namespace(engine, Namespace, ...)`, `_root_for` removed,
  `_has_rows`/`_objects_for` read the registry.
* `services.tool_catalog._root_for` (private) now resolves through the registry
  and therefore **raises `ValueError` for an unregistered namespace** instead of
  returning the namespace string as a path.  Only `tools` is ever passed today.
* `services.tool_catalog` still re-exports `OVERLAY_FILENAME`, now imported from
  the registry.
* `models.catalog` column defaults reference the sentinels (same `""` value);
  the columns stay `NOT NULL` — see the constant docstrings for why `NULL`
  would force a SQLite table rebuild.
* No route, schema, OpenAPI or endpoint signature changed.

## Follow-up (task-3): the overlay-import contract

The verifier's `check_mirror_catalog.py` found three defects in
`mirror_catalog.import_file`.  They are fixed, and the semantics are now the
documented contract (the verifier pins all three):

* **F1 — one identity twice in one file.**  The lookup only held *pre-import*
  rows, so the second mention of a path was added again and the commit hit
  `uq_catalog_entries_path`.  The file is collapsed by identity before writing:
  **the last entry wins and the row keeps the first mention's position**, which
  is what the old file-based `scan_npm` did (`dict[name] = item`).  One
  transaction: success writes each row once, and any failure rolls the session
  back, so the table is clean on both paths.  `path_for`'s npm rule ignoring
  `version` means two versions of one package are exactly this case.
* **F2 — missing or corrupt file must be loud.**  `_read_overlay` used to return
  `{}`, so `import --prune` on a mistyped path deleted every row and exited 0.
  It now raises `ValueError` for a missing file, unreadable bytes, invalid JSON
  and a non-object document; `--dry-run` and `--prune` included, nothing is
  touched.  A file that parses to `{"<key>": []}` is still a *valid* empty
  overlay and does prune — the operator said so on purpose.  The CLI already
  maps `ValueError` to a message and exit 1 (`cli.py main`), so no CLI change
  was needed.
* **F3 — `unnamed` is a legal package name.**  `path_for` returned the literal
  `"unnamed"` for "names nothing" and `import_file` skipped that string, so a
  real `unnamed` package could not be registered.  `path_for` now returns the
  empty string for "no identity" (falsy, explicitly skipped) and `"unnamed"`
  is just a name like any other.
* **F4 — recorded, deliberately not fixed.**  npm's `filename` rows do not
  override the matching tarball's `tags`: the scanner keys the overlay by
  *package name* but looks a tarball's metadata up by *filename*
  (`hub.scan_npm`'s `listed` vs `meta`).  This predates the refactor — the
  endpoint baseline is byte-identical — so changing it would be a silent
  behaviour change smuggled into a bug-fix task.  Left as-is, on purpose.

API impact for the verifier: `mirror_catalog.path_for` returns `""` for "no
identity" (was the word `"unnamed"`), and `mirror_catalog.import_file` now
raises `ValueError` for an unreadable overlay (it used to return an
all-zero/`pruned=N` report).  `import_file`'s signature is unchanged.

## Deliberately not abstracted

* **The overlay key literals in the scanners.**  `scan_npm` reads `packages`,
  `scan_flat` reads `artifacts`; those are keys of the `Overlay` shape, not
  namespace facts, and the scanners are handed a root and an overlay — they do
  not know which namespace they are serving.  Pushing the key through the
  registry would couple the pure file scanner to per-namespace configuration.
* **npm's "no arch/kind" rule.**  `entry_fields`/`overlay`/`export_file` still
  branch on `namespace != namespaces.NPM`.  It is one namespace's field set with
  exactly one consumer; a boolean column on every registry entry would be a
  worse fit than the named constant.
* **`models.catalog.TOOLS_NAMESPACE`.**  The registry aliases it instead of
  redefining `"tools"`: models are the lowest layer and must not import
  services, and the constant is already used as a default argument throughout
  `tool_catalog`.
* **`config/hub.py`'s directory fields.**  They stay where the env var names are
  declared (`TOOLS_DIR`, `NPM_DIR`, …); the registry reads them.  Moving them
  into the registry would have made the registry a second configuration reader.
* **The docs / tools import-export bridges.**  `docs.import_tree` and
  `tool_catalog.import_tree` keep their own trees (`document.md`+`meta.json`,
  a tools tree+categories).  Only the *JSON document* handling was unified (both
  already used `services.fileio`); there is no common "import a directory"
  abstraction because the two directories are different formats.
* **The storage medium.**  `objectstore.catalog_store` still owns local-vs-S3;
  the registry only supplies the local default root.
* **`NO_FILE` and `NO_STORAGE_KEY` are both `""`.**  Two names, not one, because
  they mean different things (names no file / owns no bytes) even though the
  column value is the same; merging them would make the model comments lie.
* **The seed SQL stays static.**  `catalog_seed` still applies a checked-in
  `.sql`; the registry does not generate it.  Generation is the recipe in
  `config/seed/README.md`, and `entry_fields` is the API it should use.

## Evidence (all re-run on the final tree)

* **Directory gate** (testing branch, 88 checks):
  `git show testing:backend/scripts/check_catalog_store.py > scratch/gate.py`
  then `cd backend && SEED_CATALOGS=0 <venv>/python ../scratch/gate.py`
  → `✅ catalog store check passed (88 checks)`.
* **Old database upgrade**: a SQLite file created by the pre-change tree
  (`wt-base` = `9518be1`, 2 seed states, no `version/arch/kind`) opens with this
  code: the light migration adds the three columns, `catalog_seed_state` ends up
  with all five namespaces (`debian, docker-images, docs, npm, tools`), the
  mirror rows are installed (`npm` 2, `debian` 3, `docker-images` 2) and the
  existing `tools` 8 entries / 6 documents are left alone.
* **Endpoint payloads byte-for-byte**: `scratch/capture.py` runs the app against
  one shared fixture directory and captures 12 responses (`/api/v1/npm`,
  `/api/v1/debian`, `/api/v1/docker`, `/api/v1/tools`, `/api/v1/docs`,
  `/api/v1/docs/python`, `/api/v1/docs/python/getting-started`, `/docs/python`,
  `/docs/python/getting-started`, `/tools/misc/dsh-intranet.env.example`,
  `/debian/Packages`, `/docker/v2/_catalog`).  Baseline from `wt-base`
  (`9518be1`) vs this tree: `diff -r scratch/out-base scratch/out-current` is
  empty.  (The baseline is also byte-identical to unmodified `main`, checked
  before the first change; re-run after task-3, still empty.)
* **The verifier's gate** (`catalog-verifier`'s `check_mirror_catalog.py`,
  148 checks) run against this tree:
  `BACKEND_DIR=<tree>/backend MIRROR_SCRATCH=<tree>/scratch
  MIRROR_BASELINE_CACHE=/home/linaro/dsh/wt-verify/scratch/mirror-baseline
  <venv>/python <snapshot of check_mirror_catalog.py>`
  → `✅ mirror catalog check passed (148 checks)`.  It includes the
  `CLI import contract (F1/F2/F3)` section (18 checks, all green) and compares
  the three endpoint payloads byte-for-byte against the pre-change code.
* **Task-3 minimal evidence**:
  - F1: `{"packages":[{"name":"pkg","version":"1.0.0"},{"name":"pkg","version":"2.0.0"}]}`
    via `cli.py catalogs import --namespace npm` → exit 0,
    `npm: 新增 1 …`, and `export` shows one `pkg` row at `2.0.0`; a forced
    mid-import error leaves `[]` rows and an empty `Session.new`.
  - F2: `cli.py catalogs import --namespace npm --from missing.json --prune`
    → stderr `error: overlay file not found: …`, exit 1, `export` before ==
    after (no row deleted); corrupt JSON with `--dry-run --prune` → exit 1,
    same.  A valid `{"packages": []}` still reports and performs `清理 N`.
  - F3: `path_for("npm", {}) == ""`; a row named `unnamed` imports and
    `/api/v1/npm` serves it.
* **pyflakes**: no new warning.  Changed files report only the two pre-existing
  `services/npm_registry.py` unused `exc` locals (plus the documented
  `extensions/database.py` side-effect import, untouched).
