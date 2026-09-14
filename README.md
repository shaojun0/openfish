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
  Bearer API keys, and OAuth2 token introspection. Fine-grained permissions
  (`package:read`, `package:write`, `build:download`, …) mapped onto roles.
- **Extension registry** — extensions declare dependencies and are initialized
  in topological order (`extensions/`), so `app.py` stays short.
- **Local packages over HTTP** — packages are served directly from disk with an
  in-memory index kept fresh by `watchdog`.
- **Optional ClamAV scanning** on upload.

## Requirements

- Python **3.12+**
- Docker + **Docker Compose v2** (`docker compose`) for the container workflow.
  The legacy `docker-compose` 1.25 binary does **not** understand the variable
  syntax or `profiles:` used here.

## Quick start (local)

```bash
git clone https://github.com/shaojun0/openfish.git
cd openfish

python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env      # then edit .env — see "Configuration" below
python app.py
```

The server listens on `http://0.0.0.0:9090` by default. Health check:

```bash
curl -fsS http://127.0.0.1:9090/health
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
| `IS_4A`                  | Enable the 4A authentication mode                                 |

Basic Auth is only attempted when **both** `AUTH_USERNAME` and `AUTH_ASSERT`
are non-empty, so an unconfigured deployment cannot be entered with `":"`.

### ClamAV (optional)

`CLAMAV_HOST` (empty disables scanning), `CLAMAV_PORT` (3310),
`CLAMAV_TIMEOUT` (30), `CLAMAV_REQUIRED` (`false`).

## API overview

| Method | Path                                          | Purpose                              |
| ------ | --------------------------------------------- | ------------------------------------ |
| GET    | `/health`                                     | Liveness probe (no auth)             |
| GET    | `/simple/`                                     | Package index                        |
| GET    | `/simple/<package>/`                           | Files for one package                |
| GET    | `/simple/<package>/<filename>`                 | Download a file                      |
| GET    | `/packages/<filename>`                         | Download a file                      |
| POST   | `/` , `/legacy/`                               | Upload (`twine`)                     |
| GET    | `/` (dashboard)                                | API key management UI                |
| GET    | `/api/keys`, POST `/api/keys`                  | List / create API keys               |
| DELETE | `/api/keys/<key_id>`                           | Revoke a key                         |
| GET    | `/api/keys/<key_id>/stats`                     | Per-key usage stats                  |
| GET    | `/python-builds/`                              | Available CPython builds             |
| GET    | `/python-builds/<tag>/<filename>`              | Download a build                     |
| GET    | `/python-builds/<tag>/<filename>/sha256`       | Build checksum                       |
| GET    | `/python-builds/health`                        | Build mirror status                  |
| GET    | `/admin/`, `/admin/stats`, POST `/admin/refresh-stats` | Admin dashboard             |
| GET    | `/auth/login`, `/auth`, `/auth/logout`         | OAuth2 login flow                    |

## Project layout

```
app.py                 entry point — wires extensions, then routes
config/                pydantic-settings models (server, storage, auth, security)
extensions/            pluggable infrastructure + topological init registry
routes/                Flask blueprints (pypi, python_build, api_keys, admin, auth)
auth/                  guards, decorators, permission model, API keys, OAuth2
index/                 package / build discovery and indexing
models/                SQLAlchemy models (API keys, stats)
services/              templates, stats aggregation, validation
static/                HTML dashboard + index templates
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
- OAuth2 token introspection and code exchange run with `verify=False`, so a
  private CA is expected to be trusted at the OS level rather than per request.

## License

No license has been declared yet. Until one is added, all rights are reserved.
