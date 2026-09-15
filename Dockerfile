# ── Frontend build stage ────────────────────────────────────────────
# Builds the Vue 3 SPA.  Vite emits straight into static/dist (outDir is
# ../static/dist relative to frontend/), which is copied into the runtime
# image below — Node never ships to production.
FROM node:24-slim AS frontend

WORKDIR /frontend
COPY frontend/package*.json frontend/.npmrc ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


# ── Final stage ─────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="cpypiserver"
LABEL org.opencontainers.image.description="Lightweight Python package server with nginx index cache"

# ── System dependencies ─────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
    libmagic1 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

# ── Nginx cache directories ─────────────────────────────────────────
RUN mkdir -p /var/cache/nginx/index /var/cache/nginx/packages \
    && chown -R www-data:www-data /var/cache/nginx \
    && mkdir -p /var/log/supervisor

# ── Python dependencies ─────────────────────────────────────────────
# Single source of truth: pyproject.toml (shared with uv / local dev).
# Copied first for Docker layer caching — this layer only rebuilds when
# pyproject.toml itself changes.
WORKDIR /app
COPY pyproject.toml .
RUN pip install -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir \
    $(python -c "import tomllib; deps = tomllib.loads(open('/app/pyproject.toml','r').read())['project']['dependencies']; print(' '.join(deps))") \
    "gunicorn>=22"

# ── Application code ────────────────────────────────────────────────
# Everything not excluded by .dockerignore lands here.
# When you add a new directory (e.g. "templates/"), just add it to
# .dockerignore only if it should be excluded — no Dockerfile change needed.
COPY . .

# ── Built frontend bundle ───────────────────────────────────────────
# Copied after the source so it always wins over any stale local static/dist.
COPY --from=frontend /static/dist ./static/dist

# ── Runtime ─────────────────────────────────────────────────────────
# `tools/`, `npm/`, `node-builds/`, `docker-images/`, `debian/` and `docs/` are
# the artifact-hub catalogs; they start empty (or with the sample entries
# shipped in the repo) and are normally bind-mounted so an operator can drop
# files in without rebuilding the image.
RUN mkdir -p /app/packages /app/data \
    /app/tools /app/npm /app/node-builds /app/docker-images /app/debian /app/docs \
    /app/certs

# config/server.py defaults PORT to 9090; pin it to 8080 so the app matches
# the EXPOSE / HEALTHCHECK below (and the compose port mapping).
ENV PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8080/health || exit 1

CMD ["python", "app.py"]
