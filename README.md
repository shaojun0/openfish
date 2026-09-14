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
- **API keys** — issue, list, revoke and track per-key usage from a web
  dashboard; keys are stored hashed in SQLite.
- **Pluggable authentication** — HTTP Basic, twine-style `__token__` Basic,
  Bearer API keys, and OAuth2 token introspection.
- **Database-backed RBAC** — the five classic tables (`users`, `roles`,
  `permissions`, `user_roles`, `role_permissions`). Roles are rows, not code:
  granting one is a database write that takes effect on the next request. Every
  permission point is enforced by the route that names it —
  see [Roles and permissions](#roles-and-permissions).- **Extension registry** — extensions declare dependencies and are initialized
  in topological order (`extensions/`), so `app.py` stays short.
- **Local packages over HTTP** — packages are served directly from disk with an
  in-memory index kept fresh by `watchdog`.
- **Optional ClamAV scanning** on upload.
- **Vue 3 admin console** — the browser-facing UI is a Vue 3 + Vite + Element
  Plus SPA in `frontend/`, built into `static/dist/` and served by Flask. Every
  machine-facing endpoint stays server-rendered, so `pip` and `uv` never need
  JavaScript — see [Frontend architecture](#frontend-architecture).

## Requirements

- Python **3.12+**
- Node **20+** — *build-time only*, to compile the SPA. Not needed at runtime.
- Docker + **Docker Compose v2** (`docker compose`) for the container workflow.
  The legacy `docker-compose` 1.25 binary does **not** understand the variable
  syntax or `profiles:` used here.

## Quick start (local)

```bash
git clone https://github.com/shaojun0/openfish.git
cd openfish

python -m venv .venv && source .venv/bin/activate
pip install -e .

# Build the SPA (once; re-run after changing anything under frontend/)
cd frontend && npm install && npm run build && cd ..

cp .env.example .env      # then edit .env — see "Configuration" below

# Designate the first administrator — see "Roles and permissions"
python cli.py create-admin <your-login>

python app.py
```

The server listens on `http://0.0.0.0:9090` by default. Health check:

```bash
curl -fsS http://127.0.0.1:9090/health
```

If you skip the build step the API and the machine-facing endpoints still work;
only `/` returns a "frontend bundle not found" hint.

### Frontend development (hot reload)

Run Flask and Vite side by side. Vite proxies the API and the machine-facing
endpoints to Flask, so everything is reachable on one origin:

```bash
python app.py                  # terminal 1 — http://127.0.0.1:9090
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
non-zero on any Vue warning, unresolved component or runtime error:

```bash
cd frontend && npm run smoke
```

## Quick start (Docker)

```bash
cd docker
cp .env.example .env      # REQUIRED: set SECRET_KEY, otherwise compose aborts
docker compose up -d --build
```

Two services are defined:

| Service             | Container           | Port (host) | Purpose                          |
| ------------------- | ------------------- | ----------- | -------------------------------- |
| `pypiserver`        | `cpypiserver-std`   | `20416`     | The server                       |
| `pypiserver-debug`  | `cpypiserver-debug` | `20417`     | `profile: debug` — idle bash box |

```bash
docker compose --profile debug up -d          # start the debug container too
docker exec -it cpypiserver-debug bash
```

## Configuration

Settings are [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
models in `config/`. They are resolved in this order (later wins):

1. field defaults in `config/*.py`
2. the project-level `.env` file
3. real environment variables

Templates are provided at `.env.example` (local development) and
`docker/.env.example` (Docker Compose, which reads `docker/.env` automatically).

### Common variables

| Variable                | Default                | Description                                        |
| ----------------------- | ---------------------- | -------------------------------------------------- |
| `HOST` / `PORT`         | `0.0.0.0` / `9090`     | Bind address and port                              |
| `DEBUG`                 | `false`                | Flask debug mode                                   |
| `SECRET_KEY`            | *(empty)*              | Session signing key — **set this**                 |
| `SERVER_NAME`           | `cpypiserver`          | Branding name                                      |
| `ROUTE_PREFIX`          | *(empty)*              | Global URL prefix for every route                  |
| `PACKAGES_DIR`          | `packages`             | Where uploaded packages live                       |
| `PYTHON_BUILDS_DIR`     | `python-build-standalone` | Prebuilt CPython releases                       |
| `API_KEYS_FILE`         | `data/cpypiserver.db`  | SQLite database for API keys and stats             |
| `STORAGE__OVERWRITE`    | `false`                | Allow re-uploading an existing filename            |
| `MAX_CONTENT_LENGTH`    | `104857600` (100 MiB)  | Maximum upload size                                |
| `ADMIN_USERS`           | `[]`                   | Admin whitelist — **JSON array**, e.g. `["alice"]` |

Nested fields can also be addressed with the `__` delimiter, e.g.
`SERVER__PORT=9091`.

> **Note:** list-valued variables must be JSON. Writing `ADMIN_USERS=` (empty)
> raises a settings error at startup — leave the line commented out instead.

### Authentication variables

| Variable                 | Description                                                       |
| ------------------------ | ----------------------------------------------------------------- |
| `AUTH_ENABLED`           | Master switch (default `true`)                                    |
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
| `build:read` | `/python-builds/` listings |
| `build:download` | `/python-builds/<tag>/<file>` |
| `build:sha256` | `/python-builds/<tag>/<file>/sha256` |
| `key:list` / `key:create` / `key:delete` / `key:stats` | the matching `/api/v1/keys*` endpoints |
| `admin:view` | `/api/v1/admin/stats` |
| `admin:refresh` | `POST /api/v1/admin/refresh-stats` |
| `admin:roles` | `/api/v1/admin/{roles,permissions,users}` |

### Built-in roles

| Role | Behaviour |
| ---- | --------- |
| `anonymous` | Flagged `is_anonymous_default` — applies to requests that never authenticated (only reachable with `AUTH_ENABLED=false`) |
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
python cli.py create-admin zhangsan

#    In Docker:
docker exec cpypiserver-std python /app/cli.py create-admin zhangsan
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

## API overview

### Machine-facing (consumed by clients — no JavaScript)

| Method | Path                                          | Purpose                              |
| ------ | --------------------------------------------- | ------------------------------------ |
| GET    | `/health`                                     | Liveness probe (no auth)             |
| GET    | `/simple/`                                     | Package index (PEP 503 / PEP 691)    |
| GET    | `/simple/<package>/`                           | Files for one package                |
| GET    | `/simple/<package>/<filename>`                 | Download a file                      |
| GET    | `/packages/<filename>`                         | Download a file                      |
| POST   | `/` , `/legacy/`                               | Upload (`twine`)                     |
| GET    | `/python-builds/`                              | Available CPython builds             |
| GET    | `/python-builds/<tag>/<filename>`              | Download a build                     |
| GET    | `/python-builds/<tag>/<filename>/sha256`       | Build checksum                       |
| GET    | `/python-builds/health`                        | Build mirror status                  |
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

### Browser-facing

`/`, `/packages`, `/api-keys`, `/admin` and `/access` all serve the SPA shell. A
deep link such as `/api-keys` is handled by Flask's history-mode fallback, so
links can be shared and bookmarked.

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
python scripts/check_openapi.py

# Live: validate real responses against the models the spec references
python scripts/check_contract.py --base-url http://127.0.0.1:9090 --api-key cpypi_…
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
python scripts/check_auth_guards.py

# The RBAC tables must actually decide access
python scripts/check_rbac.py
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

## Frontend architecture

The split is by **audience**, not by convenience:

| Audience | Owned by | Why |
| -------- | -------- | --- |
| A human in a browser (`/`, `/packages`, `/api-keys`, `/admin`) | Vue 3 SPA in `frontend/` | Rich interaction, no crawler contract |
| A package manager (`/simple/`, `/packages/<f>`, `/python-builds/`) | Flask + Jinja (`static/*_template/`) | `pip` and `uv` **parse the HTML directly and never run JavaScript** — these are wire protocols, not web pages |
| A script or agent (`/api/v1/*`, `/health`) | Flask JSON | Stable contract for API-key clients |

Adding a new package ecosystem (npm, Maven, …) means adding a backend adapter
plus its protocol routes; the SPA stays unchanged as long as the ecosystem is
surfaced through `/api/v1`.

## Project layout

```
app.py                 entry point — wires extensions, then routes
cli.py                 administrative CLI (roles, grants, superuser bootstrap)
config/                pydantic-settings models (server, storage, auth, security)
extensions/            pluggable infrastructure + topological init registry
routes/                Flask blueprints (pypi, python_build, api_keys, admin,
                       access, session, discovery, spa, auth)
openapi/               API description: metadata registry, spec builder, renderers
auth/                  guards, decorators, permission points, API keys, OAuth2
index/                 package / build discovery and indexing
models/                SQLAlchemy models (users, roles, permissions, API keys, stats)
services/              authorization service, templates, stats, validation
schemas.py             request + response models (single source for /openapi.json)
scripts/               verification gates (check_openapi, check_contract,
                       check_auth_guards, check_rbac)
frontend/              Vue 3 + Vite + Element Plus SPA (build-time only)
static/                machine-facing templates + the built SPA in static/dist/
docker/                docker-compose.yml and its .env template
```

Adding a feature usually means one new module in `extensions/`, one blueprint in
`routes/`, and two lines of registration — see the docstring in
`extensions/__init__.py`.

## Security notes

- **No credentials live in this repository.** Secrets are injected through
  environment variables; `.env` and its Docker counterpart are git-ignored.
- **TLS material is not shipped.** `*.pem`, `*.key` and `static/certs/` are
  ignored. To serve a private CA to clients, drop your chain at
  `docker/certs/ca_chain.pem` and uncomment the corresponding mount in
  `docker/docker-compose.yml`; the dashboard links to it at
  `/static/certs/ca_chain.pem`.
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
