# config/seed — the defaults a fresh database is initialized with

A new deployment used to get its documentation and its tools for free: the
samples lived in the catalog directories and the directory *was* the catalog.
The catalog is a table now, so the defaults are installed explicitly — at
**database initialization** (`extensions.database.init_engine` →
`services.catalog_seed.ensure_seed`), which is the one path both the API process
and `cli.py` share.

```
config/seed/
  docs.seed.sql          INSERT … SELECT … WHERE NOT EXISTS, one statement per line
  tools.seed.sql         same, for the tools catalog
  content-types.json     {key: media type} — a key is an opaque uuid4 and says
                         nothing about what it holds, so the type travels beside it
  objects/<namespace>/<key>   the bytes each row references (bare uuid4 names)
```

## Rules this file follows

* **The keys are literal uuid4s** (`uuid5` of `"<namespace>:<path>[:<revision>]"`,
  fixed namespace in the generator), so the seed is a reviewable data artifact
  and installs identically everywhere instead of being generated at boot.
* **The statements are guarded** with `WHERE NOT EXISTS`, and no statement names
  an integer primary key — a child row resolves `document_id` with a subselect.
  That keeps the seed safe to re-apply and keeps PostgreSQL's sequences untouched.
* **It is one-shot.** `catalog_seed_state` (namespace, seeded_at) records the
  installation, so an administrator who deletes the defaults keeps them deleted
  across restarts. `cli.py catalogs seed --force` is the way to ask for them back.

## Regenerating

The seed is generated from a catalog tree by importing it through the normal
code path and dumping the rows, so the SQL cannot drift from the schema:

```bash
cd backend
# 1. import a tree into a throwaway database + store (see services/docs.py and
#    services/tool_catalog.py for `import_tree`)
# 2. dump documents / document_revisions / document_assets and
#    catalog_categories / catalog_entries as INSERT … SELECT … WHERE NOT EXISTS,
#    re-keying each object with uuid5 so the output is reproducible
# 3. copy the object bytes to objects/<namespace>/<key> and write content-types.json
```

The generator used for the first version re-keyed with the fixed namespace
`6f1b0a2c-0000-4000-8000-000000000001` and wrote the timestamps as a constant
(`2026-01-01 00:00:00`), so re-running it produces the same keys and a diff that
shows only real content changes.

The content itself came from the samples that used to live in
`docker/examples/{docs,tools}`; those directories are gone — the defaults are a
property of the software now, not of the deployment layout.
