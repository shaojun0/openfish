# openfish — Docker Compose deployment bundle

Everything Compose needs lives in this directory. In particular **every
bind-mount source is a path under `docker/`** (`./share`, `./npm`, `./data`, …),
so `docker-compose.yml` contains no `../backend/...` and no absolute
`/media/...` host paths. Swapping storage means re-pointing a symlink here —
never editing the Compose file.

## Layout

```
docker/
├── docker-compose.yml          # 4 services: backend, frontend, nginx, db(optional)
├── docker-compose.postgres.yml # override: start backend after db is healthy
├── .env / .env.example         # interpolation source + container env (no host paths)
├── nginx/nginx.conf            # edge gateway :20416 -> frontend / backend
├── prepare-mounts.sh           # create / re-point every bind-mount source
├── examples/                   # committed sample catalogs, copied into a fresh mount
├── tools/    (real dir, in-repo)   → /app/tools         (downloadable tools catalog)
├── docs/     (real dir, in-repo)   → /app/docs          (per-ecosystem Markdown)
├── share     → symlink             → /app/share         (python + python-build-standalone)
├── npm       → symlink             → /app/npm           (local npm tarballs)
├── node-builds → symlink           → /app/node-builds   (nodejs.org/dist mirror)
├── docker-images → symlink         → /app/docker-images (docker save tarballs)
├── debian    → symlink             → /app/debian        (.deb + apt metadata)
├── data      → symlink             → /app/data          (DB + proxy caches)
└── config/model_routes.json → symlink → /app/config/model_routes.json
```

`tools/` and `docs/` are small, versioned catalogs; they stay real directories in
the repository. The five artifact mirrors, the runtime `data/` and the model
routing table are **operator data**: `prepare-mounts.sh` makes each one either a
real directory (seeded from `examples/`) or a symlink to a data disk.

## Mount table

| Compose source (as written) | This deployment | Container path | Mode |
| --- | --- | --- | --- |
| `./share` | `/media/root/Getea/openfish-mirror` | `/app/share` | rw |
| `./tools` | *(real dir, in-repo)* | `/app/tools` | rw |
| `./npm` | `/media/root/Getea/openfish-mirror/npm` | `/app/npm` | rw |
| `./node-builds` | `/media/root/Getea/openfish-mirror/node-builds` | `/app/node-builds` | rw |
| `./docker-images` | `/media/root/Getea/openfish-mirror/docker-images` | `/app/docker-images` | rw |
| `./debian` | `/media/root/Getea/openfish-mirror/debian` | `/app/debian` | rw |
| `./docs` | *(real dir, in-repo)* | `/app/docs` | rw |
| `./config/model_routes.json` | `/home/linaro/dsh/openfish/backend/config/model_routes.json` | `/app/config/model_routes.json` | rw |
| `./data` | `/home/linaro/dsh/openfish/backend/data` | `/app/data` | rw |
| `./nginx/nginx.conf` | *(real file, in-repo)* | `/etc/nginx/conf.d/default.conf` | ro |

`PACKAGES_DIR=/app/share/python` and
`PYTHON_BUILDS_DIR=/app/share/python-build-standalone` are container-internal
sub-paths of the `./share` mount; they are not host paths.

## Preparing (and re-pointing) the mounts

```bash
# Fresh clone: real directories under docker/, seeded from examples/.
./prepare-mounts.sh

# The deployment layout: symlink the five mirrors into a data disk.
./prepare-mounts.sh /media/root/Getea/openfish-mirror

# Re-point an existing layout at another disk.
./prepare-mounts.sh --force /srv/openfish-mirror
```

The script is idempotent: without `--force` an existing path is reported and
left alone. A single mount can equally be re-pointed by hand — that is the whole
extensibility contract:

```bash
ln -sfn /srv/data/npm docker/npm
docker compose -f docker/docker-compose.yml up -d --force-recreate backend
```

Always use an **absolute** target: the Docker daemon resolves bind sources
against the Compose project directory, not your shell's. Stop the backend first
(`docker compose down`, without `-v`) when moving the live state
(`docker/data`): on the default SQLite backend the engine keeps a WAL file open.

## Fresh clone

```bash
cd docker
cp .env.example .env          # fill in SECRET_KEY / AUTH_ASSERT
./prepare-mounts.sh           # or: ./prepare-mounts.sh /path/to/openfish-mirror
docker compose up -d --build
```

`prepare-mounts.sh` matters for one mount in particular:
`./config/model_routes.json` is a **single-file** bind, and Docker cannot create
a missing file source (it would create a directory). The script also keeps
`docker/data` and that file pointing at the same real paths the non-Docker entry
point uses (`cd backend && python app.py`).

All of `docker/share`, `docker/npm`, `docker/node-builds`,
`docker/docker-images`, `docker/debian`, `docker/data`,
`docker/config/model_routes.json` and `docker/.pre-symlink-backup/` are
git-ignored, so the repository stays clean.

## Design notes

- **Why the `*_SRC` Compose variables were dropped.** Previously each mount used
  `${NPM_SRC:-./npm}` etc., so a data-disk path could be injected from `.env`.
  That is a *second* indirection which bypasses the symlink and lets absolute
  host paths back into the deployment. Now the source is always the in-`docker/`
  path and the only redirect mechanism is the symlink — one mechanism, one
  invariant, and `docker/.env` carries no host paths at all.
- **Why `docker/data` → `backend/data` and not the reverse.** The backend resolves
  its local state from `backend/config/paths.py` (`backend/data/...`), so keeping
  `backend/data/` the real directory means `cd backend && python app.py`
  continues to use exactly the same SQLite database and caches as the container
  while the default backend is in use.
  Making `backend/data` the symlink would work too, but it would move the
  canonical location and force every non-Docker entry point through `docker/`.
- **`docker/config/model_routes.json`** is a file symlink to the real, tracked
  `backend/config/model_routes.json`, for the same reason: local runs and the
  container share one routing table. It is git-ignored so the link itself is
  never committed, while the real file remains versioned under `backend/`.
- **`docker/examples/`** holds the sample catalogs that used to live directly in
  `docker/npm`, `docker/debian`, `docker/docker-images` and `docker/node-builds`.
  Those directories are mount sources now, so their committed contents moved one
  level aside; `prepare-mounts.sh` copies them into an empty mount so a fresh
  checkout still starts with a working catalog. The live
  `node-builds/.sync/sync_all.sh` and `run.out` that existed only in the
  repository were copied into the live mirror so neither side lost content.

## Operations

```bash
docker compose up -d --build                     # default stack (backend/frontend/nginx)
docker compose --profile debug up -d             # + backend-debug on :20417
docker compose --profile db up -d                # + postgres (see below to switch the app)
docker compose config                            # validate / inspect resolved mounts
docker compose down                              # keep volumes and bind data
```

Edge gateway: <http://127.0.0.1:20416> — `/health`, `/openapi.json`, `/llms.txt`,
`/docs` and the SPA shell are served from here.

## PostgreSQL

The application runs on SQLite by default: the `API_KEYS_FILE` file inside the
`./data` mount. PostgreSQL is a **configuration switch, not a different build**.
Set `DATABASE_URL` and the users, roles, permissions, API keys and statistics all
move to the server together — nothing is read from `API_KEYS_FILE` afterwards, so
there is no split-brain mode where half the tables live in each engine.

```bash
# docker/.env
POSTGRES_DB=openfish
POSTGRES_USER=openfish
POSTGRES_PASSWORD=<strong-password>
DATABASE_URL=postgresql+psycopg://openfish:<strong-password>@db:5432/openfish

# start — `--profile db` is what enables the db service
docker compose --profile db up -d --build

# optional: same stack, but backend waits for db to pass its healthcheck
docker compose --profile db \
  -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

`DATABASE_URL` must use the Compose service name (`db:5432` on the internal
network) and the credentials must match `POSTGRES_*`; URL-encode a password that
contains `@`, `:`, `/` or `#`. The backend retries the connection for up to 60 s
at startup, so a PostgreSQL container that is still booting does not become a
crash loop even without the override file.

**Moving existing data.** Switching backends copies nothing: the tables are
created empty on first boot. Existing accounts, roles and grants live in the
SQLite file, so recreate what you need with the admin CLI (`--db` accepts either
a SQLite path or a PostgreSQL URL, and defaults to `DATABASE_URL`):

```bash
# what is in the old SQLite file?
docker compose exec backend python /app/cli.py --db /app/data/cpypiserver.db list-users

# recreate accounts/roles on PostgreSQL (DATABASE_URL is already in the env)
docker compose exec backend python /app/cli.py create-admin alice
docker compose exec backend python /app/cli.py grant alice publisher
```

Issued API keys are only stored as SHA-256 hashes, so they cannot be exported —
mint new ones from the `/api-keys` page after the cutover. To verify a backend
end to end (bootstrap, user, role, API key, statistics), run
`python scripts/check_database.py --url <url> --yes` from `backend/`.
