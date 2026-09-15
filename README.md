# cpypiserver

A lightweight, self-hosted Python package server built with Flask, with an
optional index in front of it, API-key management, and OAuth2 support.

Designed to be small enough to read in an afternoon and structured so that new
features are added as self-contained extensions rather than edits spread across
the codebase.

## Features

- **PyPI-compatible index** — `pip` / `uv` / `twine` work out of the box
  (`/simple/`, upload via `POST /`).
- **python-build-standalone hosting** — serve and checksum prebuilt CPython
  distributions.
- **nodejs.org/dist mirror** — serve prebuilt Node.js archives alongside the
  `index.json`, `index.tab`, `SHASUMS256.txt` and `latest` / `latest-v20.x`
  aliases that `nvm`, `fnm` and `node-gyp` resolve against.
- **Artifact hub** — beyond Python, the sidebar is grouped by ecosystem and each
  group is a real protocol server, not a listing: an **npm registry**
  (packuments, manifests, tarballs, `/-/v1/search`), a **Docker Registry v2**
  pull endpoint, an **apt repository** (flat local index plus a mirror proxy), a
  downloadable **tools** directory (`tools/<category>/`) and a
  **model-routing** table for downstream DSH (`backend/config/model_routes.json`). npm,
  Docker and Debian are read-through proxies: the local directory is the first
  source, an optional upstream mirror is fetched on demand and cached — see
  [Artifact hub](#artifact-hub-tools--npm--docker--debian--model-routing).
- **Ecosystem documentation** — every ecosystem group owns its own
  documentation leaf (`/documentation/python`, `/documentation/npm`,
  `/documentation/docker`, …) holding
  that ecosystem's Markdown documents, stored as folder projects under
  `DOCS_DIR/<ecosystem>/` with their own assets. Any signed-in user can read
  and download them; only an administrator can create, edit or delete one —
  through the **in-browser Markdown editor** (formatting toolbar + live
  preview). See [Ecosystem documentation](#ecosystem-documentation).
- **API keys** — issue, list, revoke and track per-key usage from a web
  dashboard; keys are stored hashed in SQLite.
- **Pluggable authentication** — HTTP Basic, twine-style `__token__` Basic,
  Bearer API keys, and OAuth2 token introspection.
- **Database-backed RBAC** — the five classic tables (`users`, `roles`,
  `permissions`, `user_roles`, `role_permissions`). Roles are rows, not code:
  granting one is a database write that takes effect on the next request. Every
  permission point is enforced by the route that names it —
  see [Roles and permissions](#roles-and-permissions).
- **Extension registry** — extensions declare dependencies and are initialized
  in topological order (`extensions/`), so `app.py` stays short.
- **Local packages over HTTP** — packages are served directly from disk with an
  in-memory index kept fresh by `watchdog`.
- **Optional ClamAV scanning** on upload.
- **Vue 3 admin console** — the browser-facing UI is a Vue 3 + Vite + Element
  Plus SPA in `frontend/`, built to `frontend/dist/` and served by its own nginx
  container (or by Flask when `FRONTEND_DIST_DIR` points at the build). Every
  machine-facing endpoint stays server-rendered, so `pip` and `uv` never need
  JavaScript — see [Frontend architecture](#frontend-architecture).

## Requirements

- Python **3.12+** — backend only.
- Node **20+** — frontend build only. Not needed at runtime.
- Docker + **Docker Compose v2** (`docker compose`) for the container workflow.
  The legacy `docker-compose` 1.25 binary does **not** understand the variable
  syntax or `profiles:` used here.

## Layout

The repository is split so that each half builds on its own:

```
backend/     Flask application, its own Dockerfile, its own venv and tests/gates
frontend/    Vue 3 + Vite SPA, its own Dockerfile (node build → nginx)
docker/      Compose orchestration, edge nginx, .env template and the catalogs:
             tools/ npm/ node-builds/ docker-images/ debian/ docs/
integrations/   downstream client code (never part of an image)
```

The full tree and what each directory owns is in
[Project layout](#project-layout).

## Quick start (local)

Two processes, two terminals. The backend is the only one that needs Python.

```bash
git clone https://github.com/shaojun0/openfish.git
cd openfish

# ── Backend ────────────────────────────────────────────────────────
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env      # then edit .env — see "Configuration" below

# Designate the first administrator — see "Roles and permissions"
python cli.py create-admin <your-login>

python app.py             # http://127.0.0.1:9090

# ── Frontend (second terminal) ─────────────────────────────────────
cd frontend
npm install
npm run dev               # http://127.0.0.1:5173, proxies to the backend
```

The backend listens on `http://0.0.0.0:9090` by default. Health check:

```bash
curl -fsS http://127.0.0.1:9090/health
```

Running the backend on its own is enough for the API and every machine-facing
endpoint — `pip`, `uv`, `npm`, `docker` and `apt` never need the SPA. Only `/`
(a browser page) needs the frontend; without it, `/` returns a
"frontend bundle not found" hint. To have Flask serve a built bundle instead of
running Vite, build once and point `FRONTEND_DIST_DIR` at it:

```bash
cd frontend && npm run build          # writes frontend/dist
cd ../backend && FRONTEND_DIST_DIR=../frontend/dist python app.py
```

### Frontend development (hot reload)

Run Flask and Vite side by side. Vite proxies the API and the machine-facing
endpoints to Flask, so everything is reachable on one origin:

```bash
cd backend && python app.py    # terminal 1 — http://127.0.0.1:9090
cd frontend && npm run dev     # terminal 2 — http://127.0.0.1:5173
```

> **Filesystems without symlink support.** `npm install` creates symlinks in
> `node_modules/.bin`, which fails on NTFS/exFAT volumes (e.g. a USB stick).
> The repo ships `frontend/.npmrc` with `bin-links=false` and the npm scripts
> invoke the toolchain as `node ./node_modules/vite/bin/vite.js`, so an install
> still succeeds — but such volumes also waste ~1 MB per directory on NTFS, so
> keep `node_modules/` on a native Linux filesystem.

### Frontend smoke test

`npm run smoke` renders every route in jsdom — no browser required — and exits
non-zero on any Vue warning, unresolved component or runtime error. It also
asserts the `usePagination` slicing/sorting behaviour every table pager relies
on:

```bash
cd frontend && npm run smoke
```

## Quick start (Docker)

```bash
cd docker
cp .env.example .env      # REQUIRED: set SECRET_KEY, otherwise compose aborts
docker compose up -d --build
```

Four services are defined, each built from its own directory:

| Service          | Container               | Port (host)        | Built from            | Purpose                                                     |
| ---------------- | ----------------------- | ------------------ | --------------------- | ----------------------------------------------------------- |
| `nginx`          | `openfish-nginx`        | `20416` → 80       | `docker/nginx/`       | **The only public entry point** — routes by path             |
| `backend`        | `openfish-backend`      | *(internal 8080)*  | `backend/Dockerfile`  | Flask + gunicorn: JSON API, registry and mirror protocols    |
| `frontend`       | `openfish-frontend`     | *(internal 80)*    | `frontend/Dockerfile` | Vue SPA built by node, served as static files by nginx       |
| `db`             | `openfish-db`           | *(internal 5432)*  | `postgres:16-alpine`  | `profile: db` — **reserved**, the app does not use it yet    |

Reach the whole application through the edge port:

```
http://127.0.0.1:20416/
```

The edge splits traffic like this — the backend and frontend containers are not
published to the host at all:

| Path | Upstream | Why |
| ---- | -------- | --- |
| `/` (GET) | frontend | the SPA shell |
| `/` (POST) | backend | `twine` / `pip` upload |
| `/api/`, `/health`, `/openapi.json`, `/llms.txt`, `/.well-known/` | backend | JSON contract and discovery |
| `/simple/`, `/packages/`, `/legacy/`, `/python-builds/`, `/node-builds/` | backend | package-manager wire protocols |
| `/tools/`, `/npm/`, `/docker/`, `/debian/`, `/docs/`, `/certs/`, `/auth/` | backend | artifact hub, docs, login |
| `/static/dist/` | frontend | the hashed SPA bundle |
| everything else (`/admin`, `/models`, `/documentation/<eco>`, …) | frontend | SPA history-mode deep links |

> The trailing slash is load-bearing: `/tools` is an SPA page while `/tools/` is
> the machine-facing catalog. The same holds for `npm`, `docker`, `debian` and
> `packages`.

Rebuild one side without touching the other:

```bash
docker compose build backend
docker compose build frontend
```

Optional profiles:

```bash
docker compose --profile debug up -d          # idle backend bash box on 20417
docker exec -it openfish-backend-debug bash

docker compose --profile db up -d             # PostgreSQL, reserved for later
```

> **About the `db` service.** It is defined so the layout is ready for a future
> multi-instance deployment, but **the application does not connect to it**:
> authorization, API keys and statistics still live in the SQLite database at
> `backend/data/cpypiserver.db`. Enabling the profile changes nothing today.

## Configuration

Settings are [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
models in `backend/config/`. They are resolved in this order (later wins):

1. field defaults in `backend/config/*.py`
2. the `.env` file in the working directory (i.e. `backend/.env` when the
   backend is started from `backend/`)
3. real environment variables

Templates are provided at `backend/.env.example` (local development) and
`docker/.env.example` (Docker Compose, which reads `docker/.env` automatically).

### Common variables

| Variable                | Default                | Description                                        |
| ----------------------- | ---------------------- | -------------------------------------------------- |
| `HOST` / `PORT`         | `0.0.0.0` / `9090`     | Bind address and port                              |
| `DEBUG`                 | `false`                | Flask debug mode                                   |
| `SECRET_KEY`            | *(empty)*              | Session signing key — **set this**                 |
| `SERVER_NAME`           | `cpypiserver`          | Branding name                                      |
| `ROUTE_PREFIX`          | *(empty)*              | Global URL prefix for every route                  |
| `PACKAGES_DIR`          | `<backend>/packages`   | Where uploaded packages live                       |
| `PYTHON_BUILDS_DIR`     | `<project>/docker/python-build-standalone` | Prebuilt CPython releases    |
| `NODE_BUILDS_DIR`       | `<project>/docker/node-builds` | Prebuilt Node.js mirror (`nodejs.org/dist` layout) |
| `API_KEYS_FILE`         | `<backend>/data/cpypiserver.db` | SQLite database for API keys and stats     |
| `FRONTEND_DIST_DIR`     | *(empty)* = `<backend>/static/dist` | Directory holding the built SPA for Flask to serve. In the split Docker deployment the `frontend` container serves it instead, so this stays empty |
| `STORAGE__OVERWRITE`    | `false`                | Allow re-uploading an existing filename            |
| `MAX_CONTENT_LENGTH`    | `104857600` (100 MiB)  | Maximum upload size                                |
| `ADMIN_USERS`           | `[]`                   | Admin whitelist — **JSON array**, e.g. `["alice"]` |
| `TOOLS_DIR`             | `<project>/docker/tools` | Tools catalog root — each sub-directory is a category |
| `NPM_DIR`               | `<project>/docker/npm` | Local npm catalog (`*.tgz` / `catalog.json`)       |
| `NPM_UPSTREAM`          | `https://registry.npmmirror.com` | Upstream npm registry — both the advertised `npm config set registry` target and the read-through source |
| `NPM_PROXY_ENABLED`     | `true`                 | Serve the npm registry protocol; `false` answers only for already-cached packages |
| `NPM_UPSTREAM_TOKEN`    | *(empty)*              | Bearer token for a private upstream npm registry   |
| `NPM_TIMEOUT`           | `30`                   | Upstream npm read timeout (seconds)                |
| `NPM_CACHE_DIR`         | `<backend>/data/cache/npm` | Packument + tarball cache                      |
| `NPM_CACHE_MAX_MB`      | `512`                  | Byte budget for the npm cache (LRU eviction)       |
| `DOCKER_DIR`            | `<project>/docker/docker-images` | `docker save` tarballs + compose/Dockerfile |
| `DOCKER_REGISTRY`       | *(empty)*              | Intranet registry advertised on the docker page    |
| `DOCKER_UPSTREAM`       | *(empty)*              | Registry v2 endpoint to proxy pulls from (`https://registry-1.docker.io`, or an intranet registry); empty = cached-only |
| `DOCKER_UPSTREAM_USERNAME` / `DOCKER_UPSTREAM_PASSWORD` | *(empty)* | HTTP Basic credentials for that registry |
| `DOCKER_DEFAULT_NAMESPACE` | `library`           | Namespace assumed for a single-segment image name  |
| `DOCKER_TIMEOUT`        | `60`                   | Upstream registry read timeout (seconds)           |
| `DOCKER_CACHE_DIR`      | `<backend>/data/cache/docker` | Manifest + blob cache                       |
| `DOCKER_CACHE_MAX_MB`   | `1024`                 | Byte budget for the docker cache                   |
| `DEBIAN_DIR`            | `<project>/docker/debian` | Local `.deb` files + apt config snippets        |
| `DEBIAN_MIRROR`         | *(empty)*              | Intranet apt mirror advertised on the debian page  |
| `DEBIAN_UPSTREAM`       | *(empty)*              | apt mirror to proxy `dists/` and `pool/` from; empty = flat local repository only |
| `DEBIAN_TIMEOUT`        | `60`                   | Upstream apt mirror read timeout (seconds)         |
| `DEBIAN_CACHE_DIR`      | `<backend>/data/cache/debian` | Proxied apt metadata cache                  |
| `DEBIAN_CACHE_MAX_MB`   | `256`                  | Byte budget for the apt metadata cache             |
| `DEBIAN_METADATA_TTL`   | `300`                  | Seconds a proxied `Release`/`Packages` document is trusted |
| `MODELS_FILE`           | `<backend>/config/model_routes.json` | Model-routing table for downstream DSH; editable from `/models` by `model:write`, so it must be writable |
| `MODEL_HEALTH_FILE`     | `<backend>/data/model_health.json` | Last connectivity probe per route (kept out of `MODELS_FILE`) |
| `MODEL_PROBE_TIMEOUT`   | `5`                    | Seconds allowed for one route connectivity probe    |
| `DOCS_DIR`              | `<project>/docker/docs` | Per-ecosystem Markdown documentation root — one sub-directory per ecosystem, one folder project per document |

Nested fields can also be addressed with the `__` delimiter, e.g.
`SERVER__PORT=9091`.

> **`<backend>` and `<project>`.** Path defaults are anchored to the repository
> rather than to the working directory, so they are correct wherever the server
> is started from: `<backend>` is the `backend/` directory (code, templates and
> local state — `data/`, `packages/`, `certs/`, `config/`), and `<project>` is
> the repository root. The operator-managed artifact catalogs
> (`tools/`, `npm/`, `node-builds/`, `docker-images/`, `debian/`, `docs/`) live
> under `<project>/docker/`, so the repository root stays a short list of build
> units. Docker Compose overrides every one of them with an absolute `/app/…`
> path.

> **Note:** list-valued variables must be JSON. Writing `ADMIN_USERS=` (empty)
> raises a settings error at startup — leave the line commented out instead.

### Authentication variables

| Variable                 | Description                                                       |
| ------------------------ | ----------------------------------------------------------------- |
| `AUTH_ENABLED`           | Master switch (default `true`). Setting it to `false` makes credentials **optional, not ignored**: a request that presents none stays anonymous and is authorized by the `anonymous` role, which holds `doc:read` only. So "off" means *documentation-only*, not *open* — mirrors, catalogues and the web console still answer `403` |
| `AUTH_USERNAME`          | HTTP Basic username                                               |
| `AUTH_ASSERT`            | HTTP Basic password *(legacy name; leave empty to disable Basic)*  |
| `OAUTH2_INTROSPECT_URL`  | RFC 7662 introspection endpoint — empty disables OAuth2 entirely  |
| `OAUTH2_TOKEN_URL`       | Token endpoint                                                    |
| `OAUTH2_AUTHORIZE_URL`   | Authorization endpoint                                            |
| `OAUTH2_CLIENT_ID`       | OAuth2 client ID                                                  |
| `OAUTH2_CLIENT_SECRET`   | OAuth2 client secret                                              |
| `OAUTH2_PRODUCT_ID`      | Provider-specific product ID (default `0`)                        |
| `OAUTH2_AUTH_PREFERENCE` | Optional `auth-preference` query parameter for 4A-style flows      |
| `OAUTH2_CA_BUNDLE`       | CA bundle (PEM) for verifying the provider's TLS certificate. Empty uses the system trust store; there is deliberately no way to disable verification |
| `IS_4A`                  | Enable the 4A authentication mode                                 |
| `ADMIN_USERS`            | JSON array. A **one-shot cold-start seed** for the first superuser only — inert once any superuser exists. Use `cli.py` afterwards |

Basic Auth is only attempted when **both** `AUTH_USERNAME` and `AUTH_ASSERT`
are non-empty, so an unconfigured deployment cannot be entered with `":"`.

## Roles and permissions

Authorization is five tables in the same SQLite file as the API keys:

```
users ──< user_roles >── roles ──< role_permissions >── permissions
```

| Table | Holds | Who owns it |
| ----- | ----- | ----------- |
| `users` | One row per human, keyed by a **stable** `external_id` | created automatically on first login |
| `roles` | `code`, display name, and three behaviour flags | fully database-owned |
| `permissions` | The permission *points* the code checks | seeded from code, editable in the console |
| `user_roles` | Which account holds which role | database-owned |
| `role_permissions` | Which role holds which permission point | database-owned |

**The split that matters.** A route must name the permission it requires, so
permission *codes* are declared in code:

```python
@pypi_bp.route("/", methods=["POST"])
@require_permission(PACKAGE_WRITE)
def upload(): ...
```

Everything else is data. There is no role → permission mapping anywhere in
Python — `auth/permissions.py` contains a catalog of points and their default
labels, and nothing else. Adding a role, granting it permissions, and handing it
to somebody are all database writes that take effect on the **next request**,
with no redeploy. That is the whole point of the rewrite: the previous design
carried a `ROLES` dict and read `ADMIN_USERS` from the environment, so every
authorization change was a code change and a restart.

> Serving with several `gunicorn` workers? Each worker caches the grant sets it
> has resolved, so a change made through one worker is visible immediately there
> and within `AuthzService.CACHE_TTL_SECONDS` (30s) everywhere else. The shipped
> container runs a single process, where the change is immediate. Lower the TTL,
> or call `AuthzService.invalidate()`, if you need tighter cross-worker
> propagation.

The permission points shipped today, and the routes that enforce them:

| Permission | Guards |
| ---------- | ------ |
| `package:read` | `/simple/*`, `/packages/<file>`, `GET /api/v1/packages` |
| `package:write` | `POST /` and `/legacy/` (twine upload) |
| `build:read` | `/python-builds/` listings, `GET /api/v1/python-builds` |
| `build:download` | `/python-builds/<tag>/<file>` |
| `build:sha256` | `/python-builds/<tag>/<file>/sha256` |
| `nodebuild:read` | `/node-builds/` listings, `index.json`, `index.tab`, `GET /api/v1/node-builds` |
| `nodebuild:download` | `/node-builds/<tag>/<file>` and `/node-builds/<tag>/SHASUMS256.txt` |
| `nodebuild:sha256` | `/node-builds/<tag>/<file>/sha256` |
| `tool:read` | `/tools/` listing, `GET /api/v1/tools` |
| `tool:download` | `/tools/<path>` |
| `npm:read` | `/npm/` packuments, `/-/all`, `/-/ping`, `/-/v1/search`, `GET /api/v1/npm` |
| `npm:download` | `/npm/<pkg>/-/<file>` and `/npm/files/<file>` (tarballs, local or upstream-cached) |
| `docker:read` | `/docker/` listing, `/docker/v2/_catalog`, `/docker/v2/<name>/tags/list`, `/docker/v2/<name>/manifests/<ref>`, `GET /api/v1/docker` |
| `docker:download` | `/docker/v2/<name>/blobs/<digest>` (layer/config bytes) and `/docker/files/<file>` |
| `debian:read` | `/debian/` index, `/debian/Packages`, `/debian/dists/<path>`, `GET /api/v1/debian` |
| `debian:download` | `/debian/pool/<path>` (package bytes) and `/debian/files/<file>` |
| `key:list` / `key:create` / `key:delete` / `key:stats` | the matching `/api/v1/keys*` endpoints |
| `admin:view` | `/api/v1/admin/stats` |
| `admin:refresh` | `POST /api/v1/admin/refresh-stats` |
| `admin:roles` | `/api/v1/admin/{roles,permissions,users}` |
| `model:read` | `GET /api/v1/models` |
| `model:resolve` | `GET /api/v1/models/resolved` — the same table **with** each route's real upstream `api_key`, for a downstream DSH client; the console's masked view stays on `model:read` |
| `model:write` | `POST /api/v1/models`, `PUT`/`DELETE /api/v1/models/<name>`, `POST /api/v1/models/probe`, `POST /api/v1/models/<name>/check` — adds, edits, removes and re-probes model routes |
| `doc:read` | `GET /api/v1/docs*`, `/docs/<ecosystem>` (308 → `/docs/<ecosystem>/`), `/docs/<ecosystem>/`, `/docs/<ecosystem>/<id>`, `/docs/<ecosystem>/<id>/assets/<name>` — **everything under `/docs/`** |
| `doc:upload` | `POST`/`PUT`/`DELETE` on `/api/v1/docs/<ecosystem>[/<id>[/assets/<name>]]` and `POST /api/v1/docs/<ecosystem>/<id>/preview` — creates, edits and deletes documents and their assets |
| `app:read` | the browser console itself: `/`, every SPA route, the history-mode catch-all and `/static/dist/*` *when Flask serves them*. It gates the **shell only** — each view's data is still checked by that ecosystem's own point. Held by `authenticated`; deliberately absent from `anonymous`, which is what keeps the console closed while `AUTH_ENABLED=false`. In the split Docker deployment the frontend container serves the shell, so the guard is enforced there by the SPA's 401 → `/auth/login` redirect instead — see the note under [Browser-facing](#browser-facing) |

> **Adding a point is a migration — and the server now applies it for you.**
> The per-role seed data above applies only when a built-in role is *first
> created*, so a point added to a seed in a later release would never reach an
> existing `authenticated` row. `npm:download`, `nodebuild:*` and `app:read` all
> shipped that way. Each of them is now listed in
> `services.authz._SEED_TOPUPS` as a named one-time delta; `sync_builtin_roles`
> grants it on the next boot and records it in the `seed_migrations` table, so it
> runs exactly once. An upgrade therefore needs no manual step.
>
> The design is deliberately additive: a top-up names the exact points one
> release added to one role, so it cannot resurrect a grant an administrator
> removed from something else. If a seeded point is *still* missing after the
> top-ups run — because somebody revoked it — startup logs a
> `does not hold seeded point(s)` warning and `/access` marks the row. `admin` is
> topped up automatically, which is why the gap is invisible there: the `admin`
> role holding a point proves only that the point exists, never that ordinary
> users can reach the feature. Which points a mirror ecosystem is *supposed* to
> give every signed-in user, and which the anonymous role gets, are deliberate
> decisions recorded in `backend/scripts/check_permission_catalog.py`; a new built-in
> point nobody has classified fails that gate.

### Built-in roles

| Role | Behaviour |
| ---- | --------- |
| `anonymous` | Flagged `is_anonymous_default` — applies to requests that never authenticated (only reachable with `AUTH_ENABLED=false`). Holds **`doc:read` and nothing else**: an unauthenticated caller may read `/docs/*` and is refused everything else, including the web console (`app:read`) |
| `authenticated` | Flagged `auto_grant` — handed to every account the moment it is created |
| `admin` | Flagged `is_builtin` — always holds every permission point, and cannot be deleted |

The flags are columns, not code, so "everyone who logs in may publish" is
expressed by editing the `authenticated` row rather than by changing a Python
dict. Roles you create yourself are ordinary rows; deleting one removes its
grants and nothing else.

### Bootstrapping an administrator

Superuser status is a flag on the account (`users.is_superuser`) that **bypasses
every permission lookup**. It is separate from the `admin` role on purpose: if a
grant is deleted by mistake, a superuser can still get in and repair it.

Because that is a chicken-and-egg problem, there are four ways in, in order of
preference:

```bash
# 1. The normal path — works before the person has ever logged in,
#    because the account row is created by the command itself.
cd backend && python cli.py create-admin zhangsan

#    In Docker:
docker exec openfish-backend python /app/cli.py create-admin zhangsan
```

2. **`ADMIN_USERS` / `AUTH_USERNAME`** — applied at startup **only while the
   server has zero superusers**, and logged loudly when it fires. It is a
   cold-start seed, not a standing grant: whoever can set an environment
   variable cannot quietly promote themselves later.

3. **The console** — an existing superuser can toggle the flag at
   `/access`. The API refuses this for anyone else, and refuses to demote the
   last remaining superuser.

4. **Last resort**, with the server stopped:

   ```sql
   UPDATE users SET is_superuser = 1 WHERE external_id = 'zhangsan';
   ```

Other `cli.py` verbs: `show <id>`, `grant <id> <role>`, `revoke <id> <role>`,
`demote <id>`, `list-admins`, `list-users`, `list-roles`, `list-permissions`.
Every one is idempotent.

> **Identity is `external_id`, never the display name.** It is the corporate
> login (the local part of the e-mail address) or the IdP's `sub`, and it is
> what roles attach to. Renaming somebody in the directory changes their
> `display_name` only — their roles survive. Accounts are unique on
> `external_id` alone rather than `(provider, external_id)`, so the same person
> reaching the server through OAuth2, the HTTP Basic fallback, or `ADMIN_USERS`
> is one account, not three.

### ClamAV (optional)

`CLAMAV_HOST` (empty disables scanning), `CLAMAV_PORT` (3310),
`CLAMAV_TIMEOUT` (30), `CLAMAV_REQUIRED` (`false`).

## Artifact hub (tools / npm / docker / debian / model routing)

The sidebar is grouped by ecosystem rather than listed flat, so the server can
grow beyond Python without the menu turning into a junk drawer. npm, Docker and
Debian are not merely catalogs: each speaks its ecosystem's **real wire
protocol** and is a *read-through proxy* — the local directory is consulted
first, and an optional upstream mirror is fetched on demand and cached:

| Group        | Page       | Local store          | Upstream (optional) | Protocol served |
| ------------ | ---------- | -------------------- | ------------------- | --------------- |
| Python       | `/packages` (dropdown) | `PACKAGES_DIR` + `PYTHON_BUILDS_DIR` | — | PEP 503 / PEP 691 + uv CPython mirror |
| npm / Node   | `/npm` (dropdown) | `NPM_DIR` + `NODE_BUILDS_DIR` | `NPM_UPSTREAM` | npm registry — packuments, manifests, tarballs, `/-/v1/search` — plus a `nodejs.org/dist` mirror for nvm/fnm |
| Docker       | `/docker`  | `DOCKER_DIR`         | `DOCKER_UPSTREAM`   | Docker Registry v2 — tags, manifests, blobs |
| Debian       | `/debian`  | `DEBIAN_DIR`         | `DEBIAN_UPSTREAM`   | flat `Packages` + apt mirror proxy (`dists/`, `pool/`) |
| 工具 / Tools | `/tools`   | `TOOLS_DIR`          | —                   | direct file downloads |
| 模型路由     | `/models`  | `MODELS_FILE` + `MODEL_HEALTH_FILE` | —    | JSON route table — read by everyone, **added/edited/probed by admins** |
| 文档 / Docs  | `/documentation/<eco>` | `DOCS_DIR/<eco>/<id>/` | —                | Markdown folder projects — read by everyone, **created/edited by admins** |

The Python and npm pages each carry a **dropdown** that switches the page
between its two sub-elements — packages vs. prebuilt builds for Python, npm
packages vs. the Node.js mirror for Node — so related views share one page
instead of multiplying sidebar entries. Both build views are the same Vue
component (`BuildCatalogView.vue`) fed by `/api/v1/python-builds` and
`/api/v1/node-builds`; see
[Prebuilt interpreter mirrors](#prebuilt-interpreter-mirrors).

The three proxies share one caching contract, implemented once in
`services/upstream.py` and used by `services/npm_registry.py`,
`services/docker_registry.py` and `services/debian_apt.py`:

* **Local first.** A file already in the local directory — or already in the
  cache — is served without touching the network.
* **Metadata expires, content does not.** A packument, a tag manifest and an
  apt `Release`/`Packages` document are cached with a TTL; a tarball, a
  digest-addressed blob and a `.deb` are immutable.
* **Every cache has a budget.** `NPM_CACHE_MAX_MB`, `DOCKER_CACHE_MAX_MB` and
  `DEBIAN_CACHE_MAX_MB` are enforced by least-recently-used eviction, and every
  body is written through a temp file and renamed, so a reader never sees a
  half-written entry and an aborted download changes nothing.
* **Streaming, not buffering.** A 300 MB blob is piped from the upstream socket
  to the client socket; nothing of that size is ever held in memory.
* **Empty upstream means local-only.** Leaving `NPM_UPSTREAM`,
  `DOCKER_UPSTREAM` or `DEBIAN_UPSTREAM` empty turns a proxy into a pure local
  server rather than an error.

**Tools.** Every immediate sub-directory of `TOOLS_DIR` is a category and every
file below it is a downloadable tool. An optional `TOOLS_DIR/catalog.json`
overrides display names, descriptions and tags:

```json
{
  "categories": { "ops": { "name": "运维脚本", "description": "…", "icon": "Tools" } },
  "tools": { "ops/backup.sh": { "name": "备份脚本", "tags": ["ops"] } }
}
```

The catalog is re-scanned on every request, so dropping a file in is the whole
publish step. Downloads go through `/tools/<category>/<filename>` and require
the `tool:download` permission; the listing requires `tool:read`.

**npm.** `npm config set registry <server>/npm/` is all a client needs. The
server answers the registry protocol: a packument per package (abbreviated when
the client sends `Accept: application/vnd.npm.install-v1+json`, full
otherwise), one version manifest, the tarball itself, `npm ping` and the modern
`/-/v1/search`. A `*.tgz` in `NPM_DIR` and an entry in `NPM_DIR/catalog.json` are
the **local** source for that exact version, but they are *merged with* the
`NPM_UPSTREAM` packument rather than replacing it — npm resolves a dependency
range such as `accepts@^2.0.0` against the whole `versions` map, so a partially
synced mirror must still advertise every upstream version. Locally synced
versions keep their local `dist.tarball` (served from this server); every other
version's tarball is fetched from `NPM_UPSTREAM` and cached. Every `dist.tarball`
URL a client receives is rewritten to point back at this server, so a client
never needs to reach the upstream registry itself. If `NPM_UPSTREAM` is empty,
unknown or unreachable, a mirrored package is served from the local files alone.

**Docker.** `GET /docker/v2/` is the API version probe every client makes
first; from there the usual pull sequence works — `tags/list`, then a manifest
by tag or digest, then the blobs that manifest references. When the upstream is
Docker Hub (or any registry that issues a `WWW-Authenticate: Bearer` challenge)
the server performs the token exchange on the client's behalf, so `docker login`
is only needed when *this* server asks for credentials. The offline path is
unchanged: `docker save` tarballs in `DOCKER_DIR` are downloadable and import
with `docker load -i`.

**Debian.** Two shapes coexist. The **flat local repository** is
`/debian/Packages`, rendered from the `.deb` files that actually exist on disk
(apt fails on a `Filename:` that does not resolve), with
`deb [trusted=yes] <server>/debian/ ./` as the matching source line. With
`DEBIAN_UPSTREAM` set, `dists/` and `pool/` are additionally proxied, so a
normal `deb <server>/debian bookworm main` line works too — metadata is cached
with `DEBIAN_METADATA_TTL`, packages are streamed through, and `Range` requests
are forwarded so a resumed download still works.

**Model routing.** `MODELS_FILE` (default `config/model_routes.json`, i.e.
`backend/config/model_routes.json` when the backend runs from `backend/`) is a
small JSON document describing the endpoints a downstream intranet DSH may talk to.
This server publishes the table and **lets an administrator maintain it in the
browser**; it does not proxy inference. Reading needs `model:read` (held by the
`authenticated` role), while adding, editing, deleting or re-probing a route
needs `model:write` (admin only).

Each route names a wire format — `openai`, `mineru` or `anthropic` — a
`base_url`, an optional `api_key`, a `path` (defaulted per provider:
`/v1/chat/completions`, `/file_parse`, `/v1/messages`), an optional `model` id
and display `aliases`. `name` and `description` are mandatory; the API key may
be empty. The raw key is **never returned by the API** — the SPA sees
`has_api_key` and a last-four hint, and an edit that leaves the field blank
keeps the stored key (an empty value clears it).

**Keep the document secret-free: prefer `api_key_env`.** A route may name an
**environment variable** instead of carrying a value:

```json
{ "name": "deepseek-flash", "base_url": "https://api.deepseek.com",
  "api_key": "", "api_key_env": "ENTERPRISE_DEEPSEEK_API_KEY", "…": "…" }
```

The key is then read from the process environment whenever it is needed, so
`backend/config/model_routes.json` can be committed and shared — which matters because
this repository's own rule is that no credentials live in it (see
[Security notes](#security-notes)). A stored `api_key` wins over the variable;
a variable that is set but resolves to nothing is reported as
`api_key_source: "env-missing"` in `GET /api/v1/models` rather than quietly
looking configured, and connectivity probes authenticate with the resolved key
so an env-backed route does not report `auth` after every check. Values are
injected at deploy time (`--env-file`, or `.env` for local runs); only the
*name* is ever written to the document.

On the page, “新增路由” opens an **inline editor as the first table row**; the
same row edits an existing route. Saving validates the entry, rewrites the
document atomically, and immediately **probes the URL for reachability** — a
plain `GET` that never sends an inference request. Any HTTP answer proves the
endpoint is reachable and is classified as `ok` / `auth` / `method` /
`not_found` / `client_error` / `server_error`, with `unreachable` for no answer
at all; a per-row “重新检测” and a “检测全部” button re-run it. Results live in
`MODEL_HEALTH_FILE` (default `data/model_health.json`), keyed by route name, so
the document downstream DSH reads stays a pure route table. The page also
renders an alias-expanded snippet ready to paste into a DSH config.

Because the panel writes `MODELS_FILE`, the file and its directory must be
writable by the server process — a container bind-mount of that one file must
not use `:ro`.

### Ecosystem documentation

Every ecosystem group in the sidebar carries a **documentation leaf of its own**
— `/documentation/python`, `/documentation/npm`, `/documentation/docker`,
`/documentation/debian`, `/documentation/tools` and `/documentation/models` —
rather than one shared entry at the top of the menu. A document
is a small **folder project**: its Markdown source, a `meta.json` title record,
and its own `assets/` directory, so screenshots belong to the document that uses
them instead of every ecosystem sharing one flat pile:

```
docker/docs/
  python/
    getting-started/
      document.md          # the Markdown source
      meta.json            # {title, created, modified}
      assets/              # images the document references as assets/<name>
  npm/
    publishing/
      document.md
```

**Who may do what.** Reading and downloading require `doc:read`, which the
built-in `authenticated` role holds — every signed-in user can read the
handbook. Creating, editing, deleting a document, or uploading one of its
assets requires `doc:upload`, which only the built-in `admin` role holds. An
administrator edits the Markdown **in the browser** — a GitHub-style formatting
toolbar over a source box, with a live preview rendered by the same server
renderer so what you see is what gets saved — creates a document from the
list's "+" control (optionally seeded with an uploaded `.md`; without a file
the document starts empty), and attaches images through the editor's asset
panel. Creating or seeding a document whose title/id already exists
**replaces** it rather than duplicating it.

**Reading.** The SPA renders the document; the server does the Markdown → HTML
conversion with a small, dependency-free renderer (`services/markdown.py`) that
HTML-escapes the source *before* emitting any markup, so raw HTML in a document
can never become live markup. A document's relative `assets/…` references are
rewritten to their absolute URL only while rendering, so the stored Markdown
stays portable. The URL layout follows the permission boundary exactly: the
browser page is a public SPA shell at `/documentation/<ecosystem>`, while
**every URL under `/docs/` requires `doc:read`** — one URL per resource, so
appending a trailing slash can never change who is allowed to read it. Each
document also has raw forms:

* `GET /docs/<ecosystem>` — a 308 redirect to the canonical slashed form, so
  bookmarks made before the split keep working.
* `GET /docs/<ecosystem>/` — the server-rendered index (HTML, or JSON with
  `?format=json`), the docs counterpart of `/tools/`.
* `GET /docs/<ecosystem>/<id>` — the raw `.md` (`?download=1` for an
  attachment), also available as JSON via
  `GET /api/v1/docs/<ecosystem>/<id>`.
* `GET /docs/<ecosystem>/<id>/assets/<name>` — one asset (images inline, other
  types as an attachment).

The directory is the catalog, so a change is visible on the next request — no
restart, no database.

All the catalogs are file-backed and read-only over HTTP **except this one**:
there is no upload API for tools, npm, Docker or Debian, by design — but a
package installed through a proxy does leave a cached copy behind, and an
administrator can publish documentation. The Docker image creates
`/app/tools`, `/app/npm`, `/app/node-builds`, `/app/docker-images`,
`/app/debian` and `/app/docs`, and
`docker/docker-compose.yml` bind-mounts the repository copies so an operator can
edit them in place; the caches live under `/app/data/cache`, on the same
persistent volume as the API-key database.


### Static index elements

Every ecosystem gets a **server-rendered index** next to its rich SPA page, so a
script (or a browser with JavaScript off) can still enumerate it. Both live in
the same sidebar group — the index entries carry an external-link icon and open
in a new tab. Templates are grouped by ecosystem under `backend/static/`:

| Index | Path | HTML form | JSON form |
| ----- | ---- | --------- | --------- |
| Python | `/simple/` | `simple_index.html` lists every project | `?format=json` → PEP 691 |
| Tools  | `/tools/` | `tools/index.html` lists categories and files | `?format=json` → the `/api/v1/tools` document |
| npm    | `/npm/` | `npm/index.html` lists local packages | `?format=json` → the `/-/all` document |
| Docker | `/docker/` | `docker/index.html` lists images and config snippets | `?format=json` → the `/api/v1/docker` document |
| Debian | `/debian/` | `debian/index.html` lists `.deb` files | `?format=json` → the `/api/v1/debian` document |
| Docs   | `/docs/<eco>/` | `docs/index.html` lists an ecosystem's document projects | `?format=json` → the `/api/v1/docs/<eco>` document (the browser page is `/documentation/<eco>`) |

Python and tools follow the content-negotiation convention already used by
`/simple/`. npm has no official HTML index, so the JSON side follows the two
conventions the ecosystem actually recognises:

* `GET /npm/-/ping` → `{}` — the health probe every npm client makes first.
* `GET /npm/-/all` → the **legacy full-index** shape, keyed by package name with
  `dist-tags` and `versions`. npm shut its own copy down in 2017 in favour of
  `GET /-/v1/search` and the replication feed
  ([npm blog](https://blog.npmjs.org/post/157615772423/deprecating-the-all-registry-endpoint)),
  but private registries (Verdaccio, cnpm, …) still answer it, which makes it the
  closest thing npm has to a static index. The endpoint npm actually uses today,
  `GET /npm/-/v1/search`, is served as well.

Docker and Debian each have one recognised enumeration endpoint too, and the
indexes use them:

* `GET /docker/v2/_catalog` → `{"repositories": [...]}` — the OCI distribution
  spec's repository list, the only enumeration endpoint the docker registry
  protocol defines. It is a genuine `_catalog`, not a stub: the registry routes
  around it serve real manifests and blobs.
* `GET /debian/Packages` → the flat apt **`Packages`** index, rendered from the
  `.deb` files that actually exist on disk (apt fails on a `Filename:` that does
  not resolve). Pair it with `deb [trusted=yes] <base>/debian/ ./` in
  `sources.list`. The proxied `dists/` tree is available alongside it when
  `DEBIAN_UPSTREAM` is configured.

Both protocols are proxied now: with `DOCKER_UPSTREAM` and `DEBIAN_UPSTREAM`
set, `docker pull` and `apt update` work against this server, and the static
indexes above remain the enumeration surface for scripts.

### Prebuilt interpreter mirrors

The Python and Node ecosystems each have a **prebuilt-runtime mirror** next to
their package registry. Both are plain directories on disk — the filesystem is
the source of truth, so publishing is a file copy and the next request sees it —
and both are surfaced twice: a machine-facing index a tool consumes directly,
and a table in the SPA behind the page dropdown.

| Mirror | Directory | Machine-facing index | Client |
| ------ | --------- | -------------------- | ------ |
| CPython (`python-build-standalone`) | `PYTHON_BUILDS_DIR`, one directory per release date | `GET /python-builds/` (HTML listing) | `uv python install`, via `UV_PYTHON_INSTALL_MIRROR` |
| Node.js (`nodejs.org/dist`) | `NODE_BUILDS_DIR`, one directory per `vX.Y.Z` | `GET /node-builds/`, `/node-builds/index.json`, `/node-builds/index.tab` | `nvm` / `fnm` / `node-gyp`, via `NVM_NODEJS_ORG_MIRROR` |

**Node.js.** The mirror is shaped exactly like `nodejs.org/dist`, so the real
clients need no special casing: `index.json` and `index.tab` list the versions
`nvm ls-remote` / `fnm ls-remote` enumerate, `SHASUMS256.txt` is what they verify
a download against, and `latest` / `latest-v20.x` are the aliases `nvm install
node` and `nvm install 20` resolve through before they know a version number.
Only releases that are actually on disk are listed, so the mirror can never
advertise a version it would then 404. When an authentic `SHASUMS256.txt` was
mirrored alongside the archives it is served as-is; otherwise it is generated
from the files on disk (hashed on first access, then cached). An authentic
`index.json` at the mirror root is read as an overlay for the metadata a filename
cannot carry (`lts`, `date`, `npm`, …) without ever inventing a release. See
`docker/node-builds/README.txt` for the layout and
`docker/tools/net/node-builds-mirror.sh` for a sync helper.

**CPython.** `GET /python-builds/` returns the release listing `uv` expects when
`UV_PYTHON_INSTALL_MIRROR` points here; each release directory is a date tag and
each archive is individually downloadable and checksummable. Its JSON catalog
(`GET /api/v1/python-builds`) is what the `/packages` dropdown renders.

Both catalogs answer the same JSON shape — `releases[]` of `files[]`, each file
carrying a `download_url` and a `sha256_url` — which is why one Vue component
renders both. Digests are *not* computed while listing: `sha256_url` resolves one
on demand (and caches it), so opening the page never hashes gigabytes.

## API overview

### Machine-facing (consumed by clients — no JavaScript)

| Method | Path                                          | Purpose                              |
| ------ | --------------------------------------------- | ------------------------------------ |
| GET    | `/health`                                     | Liveness probe (no auth)             |
| GET    | `/certs/ca_chain.pem`                         | Private CA chain (`TLS_CA_FILE`, no auth) |
| GET    | `/simple/`                                     | Package index (PEP 503 / PEP 691)    |
| GET    | `/simple/<package>/`                           | Files for one package                |
| GET    | `/simple/<package>/<filename>`                 | Download a file                      |
| GET    | `/packages/<filename>`                         | Download a file                      |
| POST   | `/` , `/legacy/`                               | Upload (`twine`)                     |
| GET    | `/python-builds/`                              | Available CPython builds             |
| GET    | `/python-builds/<tag>/<filename>`              | Download a build                     |
| GET    | `/python-builds/<tag>/<filename>/sha256`       | Build checksum                       |
| GET    | `/python-builds/health`                        | Build mirror status                  |
| GET    | `/node-builds/`                                | Available Node.js builds (HTML, or JSON with `?format=json`) |
| GET    | `/node-builds/index.json`                      | Node version index (`nvm ls-remote`, fnm) |
| GET    | `/node-builds/index.tab`                       | The same index, tab-separated        |
| GET    | `/node-builds/<tag>/SHASUMS256.txt`            | Per-release checksums (`<tag>` may be `latest` / `latest-v20.x`) |
| GET    | `/node-builds/<tag>/<filename>`                | Download a build                     |
| GET    | `/node-builds/<tag>/<filename>/sha256`         | Build checksum                       |
| GET    | `/node-builds/health`                          | Build mirror status                  |
| GET    | `/tools/`                                       | Tools index (HTML, or JSON with `?format=json`) |
| GET    | `/tools/<category>/<filename>`                  | Download a tool from the hub         |
| GET    | `/npm/`                                         | npm catalog index (HTML, or the `/-/all` JSON) |
| GET    | `/npm/-/all`                                    | npm legacy full-index JSON           |
| GET    | `/npm/-/ping`                                   | npm health convention — returns `{}` |
| GET    | `/npm/-/v1/search`                              | npm search (`?text=&size=&from=`)    |
| GET    | `/npm/<package>`                                | npm packument — abbreviated or full per `Accept` |
| GET    | `/npm/<package>/<version>`                      | One npm version manifest             |
| GET    | `/npm/<package>/-/<filename>`                   | npm tarball (local, cached, or proxied) |
| GET    | `/npm/@<scope>/<name>` + the two variants above | The same three npm endpoints for a scoped package |
| GET    | `/npm/files/<filename>`                        | Download a local npm tarball         |
| GET    | `/docker/`                                      | Docker catalog index (HTML or JSON)  |
| GET    | `/docker/v2/`                                   | Registry v2 API version probe        |
| GET    | `/docker/v2/_catalog`                           | Registry v2 repository list          |
| GET    | `/docker/v2/<name>/tags/list`                   | Tags of one repository               |
| GET/HEAD | `/docker/v2/<name>/manifests/<reference>`     | Manifest by tag or digest            |
| GET/HEAD | `/docker/v2/<name>/blobs/<digest>`            | Blob by digest (supports `Range`)    |
| GET    | `/docker/files/<filename>`                     | Download an image tarball / config   |
| GET    | `/debian/`                                      | Debian catalog index (HTML or JSON)  |
| GET    | `/debian/Packages`                              | Flat apt `Packages` index            |
| GET    | `/debian/dists/<path>`                          | Proxied apt metadata (`Release`, `Packages`, …) |
| GET/HEAD | `/debian/pool/<path>`                         | Proxied `.deb` (supports `Range`)    |
| GET    | `/debian/files/<filename>`                     | Download a `.deb` / apt config snippet |
| GET    | `/auth/login`, `/auth`, `/auth/logout`         | OAuth2 login flow                    |

Add `?format=json` or `Accept: application/vnd.pypi.simple.v1+json` to the
`/simple/` endpoints for the PEP 691 JSON representation.

### JSON API for the SPA (`/api/v1`)

| Method | Path                              | Purpose                                  |
| ------ | --------------------------------- | ---------------------------------------- |
| GET    | `/api/v1/session`                 | Current user, role and permissions (200 even when anonymous) |
| GET    | `/api/v1/packages`                | Package list with sizes and call counts  |
| GET    | `/api/v1/keys`                    | List API keys                            |
| POST   | `/api/v1/keys`                    | Create an API key (raw key returned once)|
| DELETE | `/api/v1/keys/<key_id>`           | Revoke a key                             |
| GET    | `/api/v1/keys/<key_id>/stats`     | Per-key usage stats                      |
| GET    | `/api/v1/admin/stats`             | System-wide aggregates (admin)           |
| POST   | `/api/v1/admin/refresh-stats`     | Recompute and cache them (admin)         |
| GET    | `/api/v1/admin/roles`             | Roles with their permissions (admin:roles) |
| POST   | `/api/v1/admin/roles`             | Create a role (admin:roles)              |
| DELETE | `/api/v1/admin/roles/<role_id>`   | Delete a non-builtin role (admin:roles)  |
| PUT    | `/api/v1/admin/roles/<role_id>/permissions` | Replace a role's permissions (admin:roles) |
| GET    | `/api/v1/admin/permissions`       | All permission points (admin:roles)      |
| GET    | `/api/v1/admin/users`             | Accounts and their roles (admin:roles)   |
| POST   | `/api/v1/admin/users/<id>/roles`  | Grant a role (admin:roles)               |
| DELETE | `/api/v1/admin/users/<id>/roles/<role>` | Revoke a role (admin:roles)        |
| PUT    | `/api/v1/admin/users/<id>/superuser` | Toggle the superuser bypass (superuser only) |
| GET    | `/api/v1/tools`                   | Tools catalog grouped by category (tool:read) |
| GET    | `/api/v1/python-builds`           | CPython build catalog for the SPA (build:read) |
| GET    | `/api/v1/node-builds`             | Node.js build catalog for the SPA (nodebuild:read) |
| GET    | `/api/v1/npm`                     | Local npm catalog scaffold (npm:read)    |
| GET    | `/api/v1/docker`                  | Local docker catalog (docker:read)       |
| GET    | `/api/v1/debian`                  | Local debian catalog (debian:read)       |
| GET    | `/api/v1/models`                  | Model-routing table, keys masked (model:read) |
| GET    | `/api/v1/models/resolved`         | Same table **with** upstream keys + `endpoint_url` (model:resolve) |
| POST   | `/api/v1/models`                  | Add a route + probe it (model:write)     |
| PUT    | `/api/v1/models/<name>`           | Edit (or rename) a route + re-probe (model:write) |
| DELETE | `/api/v1/models/<name>`           | Remove a route (model:write)             |
| POST   | `/api/v1/models/probe`            | Probe an unsaved draft URL (model:write) |
| POST   | `/api/v1/models/<name>/check`     | Re-probe a saved route (model:write)     |
| GET    | `/api/v1/docs`                    | Per-ecosystem document counts (doc:read) |
| GET    | `/api/v1/docs/<ecosystem>`        | One ecosystem's document catalog (doc:read) |
| POST   | `/api/v1/docs/<ecosystem>`        | Create/replace a document, optional `.md` seed (doc:upload) |
| GET    | `/api/v1/docs/<ecosystem>/<id>`   | Document source + rendered HTML + assets (doc:read) |
| PUT    | `/api/v1/docs/<ecosystem>/<id>`   | Save the browser editor's Markdown (doc:upload) |
| DELETE | `/api/v1/docs/<ecosystem>/<id>`   | Delete a document and its assets (doc:upload) |
| POST   | `/api/v1/docs/<ecosystem>/<id>/preview` | Render unsaved Markdown for the live preview (doc:upload) |
| GET    | `/api/v1/docs/<ecosystem>/<id>/assets` | List a document's assets (doc:read) |
| POST   | `/api/v1/docs/<ecosystem>/<id>/assets` | Upload an asset into the document (doc:upload) |
| DELETE | `/api/v1/docs/<ecosystem>/<id>/assets/<name>` | Delete an asset (doc:upload) |

### Device authorization (the DSH key hand-off)

A browser-less client — the DSH `enterprise-intranet` plugin — cannot copy a
secret out of the console. These endpoints let it ask this server to mint one on
its behalf once a human has signed in. Both JSON halves are **anonymous on
purpose**: the caller has no credential yet, which is the entire point.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| POST   | `/api/v1/device/code`  | Start a request — returns `device_code`, `user_code`, `verification_uri_complete`, `expires_in`, `interval` |
| POST   | `/api/v1/device/token` | Poll with `device_code`; `400 authorization_pending` until approved, then the minted `api_key` **exactly once** |
| GET    | `/device`              | Approval page. Bounces an unauthenticated visitor through `/auth/login?next=…` (so it works with the corporate OAuth2/4A provider) and renders a one-button confirm |
| POST   | `/device/approve`      | Mints the key for the signed-in account and binds it to the `user_code` (`key:create`) |

The `device_code` is the only secret and is stored SHA-256-hashed; the
`user_code` is a short, single-use, human-typed confirmation and is not a
credential. The user's explicit confirmation is required by design — silently
auto-approving a `user_code` from a link would let any site bind a key to a
victim's account and have it delivered to the attacker's poller.

#### The client that uses it: `integrations/dsh-plugin-enterprise-intranet`

The reference consumer of the flow above is the **DSH enterprise-intranet
plugin**, which lives in this repository under `integrations/`. It is a standard
DSH bundle package (JavaScript, installed by DSH — never by this server, which
is why `integrations/` is in `.dockerignore`). Once installed it:

* treats an API key as a **required** option — with none, it refuses to enable
  the mode and sends the user through `/device` instead;
* redeems the minted key from the polling half automatically, with no
  copy-and-paste;
* reads `GET /api/v1/models/resolved` and registers one `llm-pi-ai` provider per
  enabled route, pointing `agent-default-model` at the route whose aliases
  contain `default`;
* points pip / npm / apt / docker / nvm at this server's mirrors;
* surfaces `/api/v1/tools` and `/api/v1/docs` in its panel.

The folder's own `README.md` documents installation, configuration and the sync
relationship with the Docker build copy used by the deployment.

### Browser-facing

`/`, `/packages`, `/npm`, `/docker`, `/debian`, `/tools`, `/models`,
`/documentation/<ecosystem>`, `/api-keys`,
`/admin` and `/access` all serve the SPA shell. A deep link such as `/api-keys`
is handled by Flask's history-mode fallback, so links can be shared and
bookmarked.

`/device` is the one browser page that is **not** the SPA: it is rendered
server-side so it keeps working through an OAuth round trip and does not depend
on a rebuilt frontend bundle. It is described in
[Device authorization](#device-authorization-the-dsh-key-hand-off) below.

**Where the console's protection lives now.** In the split Docker deployment the
SPA *shell and bundle* are static files served by the `frontend` container, so
they are reachable without a session: an anonymous browser loads the app, its
first `GET /api/v1/session` answers `401`, and `frontend/src/api/client.ts`
redirects it to `/auth/login`. Nothing behind that shell is public — every data
endpoint, every `/docs/*` page and the whole `/api/v1` surface keep their own
guards, and `app:read` still gates the console whenever Flask is the one serving
it. What changes is only *where the shell is served from*, never *what data is
reachable*; the bundle contains no registry data.

> **Want the old behaviour back** (the shell itself refusing anonymous callers)?
> Send `/` and `/static/dist/` to the `backend` upstream instead of the
> `frontend` one in `docker/nginx/nginx.conf`, and give the backend the build
> by setting `FRONTEND_DIST_DIR=/app/static/dist` with the bundle mounted in.
> Flask's `app:read` guard then answers before any JavaScript runs.

When Flask does serve the bundle, it does so through **`GET
/static/dist/<path>`** (behind `app:read`). Flask's built-in static handler is
disabled (`static_folder=None`), so the rest of `static/` is not reachable: in
particular the Jinja templates in `backend/static/<ecosystem>/` that
`services/templates.py` loads. `GET /certs/ca_chain.pem` is the one anonymous
file route, because a client must be able to fetch the CA before it can trust
the mirror at all.

### Discovery surface (anonymous)

An agent that knows only the base URL can bootstrap itself:

| Path | Purpose |
| ---- | ------- |
| `/openapi.json` | OpenAPI 3.1 document — every endpoint, schema and security scheme |
| `/docs` | The same document as plain HTML — no JavaScript, no CDN |
| `/llms.txt` | Curated Markdown index laid out per the [llms.txt](https://llmstxt.org/) v2 proposal |
| `/.well-known/api-catalog` | [RFC 9727](https://www.rfc-editor.org/info/rfc9727/) linkset pointing at the three above |

Every response also carries `Link: </openapi.json>; rel="service-desc"`, so the
description is discoverable from any URL without guessing a path. These four
routes are anonymous on purpose: they publish the *contract*, never registry
data.

**Authentication for agents.** Create an API key on `/api-keys`, then send it as
`Authorization: Bearer <key>`. A key **inherits the role of the user who created
it**, so a key minted by an administrator can also call `/api/v1/admin/*`. Ask
`GET /api/v1/session` what a given key may do — the response carries the exact
`permissions` array.

### Keeping the description honest

`/openapi.json` is **generated from the live Flask `url_map`** plus an
`@api_operation(...)` decorator on each view, so it cannot advertise an endpoint
the server does not serve. The reverse direction — a served endpoint missing
from the description — is a gate rather than a hope:

```bash
# Static: coverage, $ref resolution, unique operationIds, OpenAPI 3.1 validity
python backend/scripts/check_openapi.py

# Live: validate real responses against the models the spec references
python backend/scripts/check_contract.py --base-url http://127.0.0.1:9090 --api-key cpypi_…
```

`check_openapi.py` exits non-zero when a machine endpoint carries no
`@api_operation` metadata, a `$ref` dangles, two operations share an
`operationId`, or the document fails `openapi-spec-validator` (installed via
`pip install -e '.[dev]'`). `check_contract.py` closes the loop by calling each
documented endpoint and handing the response to the pydantic model the spec
points at.

### Keeping authorization honest

The same principle applies to access control: a permission table nothing
consults is worse than none, because it looks like security.

```bash
# Every protected route must actually refuse an anonymous request
python backend/scripts/check_auth_guards.py

# The RBAC tables must actually decide access
python backend/scripts/check_rbac.py
```

`check_auth_guards.py` runs two independent checks. Statically, it parses every
module in `routes/` and fails if a `require_*` decorator is written *above* a
`@*.route` decorator — decorators apply bottom-up, so that ordering registers the
unguarded view and silently drops the check. This is not hypothetical: it is how
every `/python-builds/*` route came to be readable without credentials. At
runtime it boots the app and requests every registered rule with no credentials,
failing anything reachable that is not explicitly declared public through
`security=[]` or the documented `PUBLIC_ENDPOINTS` list — which also catches a
blueprint that simply forgot to attach a guard.

`check_rbac.py` drives the model end to end against a throwaway database: that a
cold start promotes exactly one superuser and only once; that granting a role
takes effect on the next request without a restart; that a role lacking
`package:write` gets 403 from the upload endpoint; that `is_superuser` bypasses
the tables; and that neither of the two escalation guards can be talked around
(a non-superuser cannot set the flag, and the last superuser cannot be demoted).

Both scripts exit non-zero on failure and were each verified to fail when the
bug they guard against is reintroduced.

### Keeping the renderer honest

The Markdown renderer is the only path from a stored document to HTML, so its
inline passes have to compose — a code span inside bold is still a code span —
and its output has to stay escaped:

```bash
python backend/scripts/check_markdown.py
```

It exists because that composition regressed: `**upload a `.md` file**`
rendered the code span as a bare `0`, because the placeholder restore ran a
single `re.sub` pass and never rescanned the fragment substituted for the outer
emphasis token. The gate also pins the safety properties: raw HTML stays
escaped, and `javascript:`/`data:` URLs never become live tags. It exits
non-zero on failure and was verified to fail when the single-pass restore is
reintroduced.

### Keeping the proxies honest

A protocol proxy is easy to get *almost* right: the packument looks correct, the
manifest parses, and the one header a client actually depends on is missing. So
each ecosystem ships an offline conformance gate that stands a fake upstream in
front of the proxy and drives the real wire sequence against it:

```bash
python backend/scripts/check_npm_proxy.py       # packuments, manifests, tarballs, search
python backend/scripts/check_docker_proxy.py    # token flow, manifests, blobs, Range
python backend/scripts/check_debian_proxy.py    # Release/Packages, gzip passthrough, Range
```

None of them needs network access: each starts a small HTTP server that plays
the upstream, points the proxy at it with a temporary cache directory, and
asserts on the bytes and headers a real client would see — including that the
second request for immutable content is served from the cache. The fake upstream
counts hits, so "we cached it" is verified rather than assumed.

## Frontend architecture

The split is by **audience**, not by convenience:

| Audience | Owned by | Why |
| -------- | -------- | --- |
| A human in a browser (`/`, `/packages`, `/api-keys`, `/admin`) | Vue 3 SPA in `frontend/` | Rich interaction, no crawler contract |
| A package manager (`/simple/`, `/packages/<f>`, `/python-builds/`, `/node-builds/`) | Flask + Jinja (`backend/static/<ecosystem>/`) | `pip`, `uv`, `nvm` and `fnm` **parse the HTML/JSON directly and never run JavaScript** — these are wire protocols, not web pages |
| A script or agent (`/api/v1/*`, `/health`) | Flask JSON | Stable contract for API-key clients |

Adding a new package ecosystem (npm, Maven, …) means adding a backend adapter
plus its protocol routes; the SPA stays unchanged as long as the ecosystem is
surfaced through `/api/v1`. A page that covers two related sub-elements puts a
small dropdown in its toolbar rather than growing another sidebar entry: the
Python page switches between packages and CPython builds, the npm page between
npm packages and the Node.js mirror. Both build sub-views are the one
`BuildCatalogView.vue`, because the server flattens the two mirrors into a
single JSON shape.

Every SPA table pages **client-side**: the catalog endpoints return the whole
list, `usePagination()` slices it in the browser and `TablePager.vue` renders a
shared footer (total, page size, pager, jumper). Columns that need a global sort
use `sortable="custom"` and feed `@sort-change` back into the composable, so a
sort orders the whole list rather than one page. This keeps the JSON contract
unchanged while stopping `el-table` from rendering thousands of DOM rows.

## Project layout

```
backend/                        the Flask application — one build unit
├── app.py                      entry point — wires extensions, then routes
├── cli.py                      administrative CLI (roles, grants, superuser bootstrap)
├── config/                     pydantic-settings models (server, storage, auth,
│                               security, hub) + model_routes.json
├── extensions/                 pluggable infrastructure + topological init registry
├── routes/                     Flask blueprints (pypi, python_build, node_build,
│                               api_keys, admin, access, session, discovery, spa,
│                               auth) plus one per hub ecosystem: hub (tools,
│                               models), npm, docker, debian, docs
├── openapi/                    API description: registry, spec builder, renderers
├── auth/                       guards, decorators, permission points, API keys, OAuth2
├── index/                      package / interpreter-build discovery and indexing
│                               (packages.py, python_build.py, node_build.py)
├── models/                     SQLAlchemy models (users, roles, permissions,
│                               API keys, stats)
├── services/                   authorization service, hub catalogs, per-ecosystem
│                               Markdown docs (docs.py, markdown.py), build-mirror
│                               catalogs (build_mirror.py), the shared upstream
│                               proxy/cache (upstream.py) and the per-ecosystem
│                               registry adapters (npm_registry, docker_registry,
│                               debian_apt), templates, stats, validation
├── schemas.py                  request + response models (single source for /openapi.json)
├── scripts/                    verification gates (check_openapi, check_contract,
│                               check_auth_guards, check_rbac, check_markdown, and
│                               one offline conformance gate per proxy)
├── static/                     machine-facing Jinja templates grouped by ecosystem
│                               (python/ node/ tools/ npm/ docker/ debian/ docs/).
│                               Not a public directory — see Security notes.
├── pyproject.toml, uv.lock     dependency source of truth
├── .env.example                local-development environment template
├── Dockerfile                  backend image (python only)
├── packages/, data/, certs/    runtime state, git-ignored
└── .venv/                      local virtualenv, git-ignored

frontend/                       the Vue 3 SPA — one build unit
├── src/                        api/ components/ composables/ layouts/ locales/
│                               router/ stores/ styles/ utils/ views/
├── index.html, vite.config.ts  Vite entry and config (outDir: dist/)
├── package.json, .npmrc        npm toolchain (build-time only)
├── scripts/smoke-render.ts     jsdom smoke test for every route
├── nginx.conf                  static server for the built bundle
├── Dockerfile                  node build stage → nginx runtime
└── dist/                       build output, git-ignored

docker/                         orchestration — no application code
├── docker-compose.yml          backend + frontend + nginx + db (profiles)
├── nginx/nginx.conf            the edge gateway: path routing, upload size
├── .env.example                Compose variable template (copied to docker/.env)
├── certs/                      optional TLS material, git-ignored
└── artifact-hub catalogs       operator data, bind-mounted — no rebuild needed
    ├── tools/                  tools/<category>/<file> + catalog.json
    ├── npm/                    local npm tarballs + catalog.json
    ├── node-builds/            nodejs.org/dist-shaped Node.js mirror
    ├── docker-images/          image tarballs + compose/Dockerfile
    ├── debian/                 local .deb files + apt snippets
    └── docs/                   docs/<ecosystem>/<id>/document.md (+ assets/)

integrations/                   downstream *client* code, versioned here because it is
                                tightly coupled to this server's contract. Currently:
                                dsh-plugin-enterprise-intranet (the DSH plugin that
                                redeems a device-authorization API key, adopts the model
                                routing table's default model and switches package
                                sources to this server). Outside every build context,
                                so it is never part of an image.
```

Adding a feature usually means one new module in `backend/extensions/`, one
blueprint in `backend/routes/`, and two lines of registration — see the
docstring in `backend/extensions/__init__.py`.

The artifact-hub catalogs (`tools/`, `npm/`, `node-builds/`, `docker-images/`,
`debian/`, `docs/`) live under `docker/` on purpose: they are operator data, not
code, and keeping them in the Compose directory leaves the repository root a
short list of build units. Compose bind-mounts them into the backend container,
so dropping a file in one takes effect without rebuilding anything. Each mount
source can be overridden in `docker/.env` with `TOOLS_SRC`, `NPM_SRC`,
`NODE_BUILDS_SRC`, `DOCKER_IMAGES_SRC`, `DEBIAN_SRC` or `DOCS_SRC`, so a large
mirror can stay on a data disk and simply be pointed at instead of moved.

## Security notes

- **No credentials live in this repository.** Secrets are injected through
  environment variables; `.env` (at `backend/` or `docker/`) is git-ignored.
- **TLS material is not shipped and is not in the web root.** `*.pem`, `*.key`
  and `certs/` are ignored. To serve a private CA to clients, drop your chain at
  `docker/certs/ca_chain.pem` and uncomment the corresponding mount in
  `docker/docker-compose.yml` — it lands on `/app/certs/ca_chain.pem`
  (`TLS_CA_FILE`) and is published at **`GET /certs/ca_chain.pem`**. That route
  serves exactly that one file: a private key sitting next to it is not
  reachable. (`static/certs/` used to be the drop point; it is gone, because
  Flask's blanket static handler would have served anything put there.)
- **`backend/static/` is not a public directory.** When Flask serves the bundle
  it does so through one explicit route, `GET /static/dist/<path>`
  (`spa.dist_asset`). Everything else under `backend/static/` — the Jinja
  templates in `backend/static/<ecosystem>/` that `services/templates.py` reads,
  for instance — is **not** reachable over HTTP.
  `backend/scripts/check_auth_guards.py` fails the build if a blanket `static`
  handler is ever reintroduced.
- **The SPA bundle is static, the data behind it is not.** In the split Docker
  deployment the shell and its hashed assets are served by the `frontend`
  container, so they are fetchable without a session; every `/api/v1` call, every
  `/docs/*` page and every mirror protocol still enforces its own permission
  point. The bundle contains no registry data. See
  [Browser-facing](#browser-facing) for the single-line way to route the shell
  back through Flask's `app:read` guard instead.
- **Only the edge is published.** `backend` and `frontend` are reachable solely
  on the Compose network (`expose:`, never `ports:`), so there is exactly one
  HTTP entry point to reason about. `client_max_body_size` in
  `docker/nginx/nginx.conf` must stay ≥ `MAX_CONTENT_LENGTH`, or uploads are
  rejected by nginx before Flask sees them.
- Generate `SECRET_KEY` with:
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- **OAuth2 introspection verifies TLS.** Set `OAUTH2_CA_BUNDLE` when the
  provider uses a private CA; otherwise the system trust store is used. There is
  no option to disable verification, because an unverified introspection call
  lets anyone who can intercept the connection fabricate an identity — and this
  server trusts the result enough to mint a session from it.
- The `admin` role and `is_superuser` are different things. The role grants the
  administrative permission points; the flag bypasses the permission tables
  entirely and is what makes it impossible to lock yourself out by mis-editing a
  grant. Only a superuser can hand out the flag, and the last superuser cannot
  be demoted.

## License

No license has been declared yet. Until one is added, all rights are reserved.
