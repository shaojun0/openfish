# ── cpypiserver backend ─────────────────────────────────────────────
# Build context is this directory (``backend/``), so the image contains the
# Flask application and nothing else.  The Vue SPA is *not* built here any
# more — it has its own image (``frontend/Dockerfile``) and its own container.
#
#   docker build -t openfish-backend:latest backend/
#
# docker/docker-compose.yml is the supported way to run this together with the
# frontend, the edge nginx and the optional database.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="openfish-backend"
LABEL org.opencontainers.image.description="Flask artifact hub — API, registry and mirror endpoints"

# ── System dependencies ─────────────────────────────────────────────
# libmagic1 is required by python-magic (upload inspection); curl backs the
# HEALTHCHECK below.  nginx/supervisor are deliberately *not* installed: the
# reverse proxy is its own container now.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── Python dependencies ─────────────────────────────────────────────
# Single source of truth: pyproject.toml (shared with uv / local dev).
# Copied first for Docker layer caching — this layer only rebuilds when
# pyproject.toml itself changes.
WORKDIR /app
COPY pyproject.toml .
# Dependencies go through a generated requirements file rather than an unquoted
# `$(python -c …)` expansion: `psycopg[binary]>=3.1` contains shell-glob
# metacharacters, and a requirements file is immune to word-splitting and
# pathname expansion.
RUN python -c "import tomllib; deps = tomllib.loads(open('/app/pyproject.toml').read())['project']['dependencies']; open('/tmp/requirements.txt', 'w').write('\n'.join([*deps, 'gunicorn>=22']) + '\n')" \
    && pip install -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir \
       -r /tmp/requirements.txt

# ── Application code ────────────────────────────────────────────────
# Everything not excluded by backend/.dockerignore lands here.
COPY . .

# ── Runtime directories ─────────────────────────────────────────────
# /app/packages, /app/data and /app/certs hold state this process writes.
# The artifact-hub catalogs (tools/, npm/, node-builds/, docker-images/,
# debian/, docs/) live at the *project* root and are bind-mounted over these
# mount points by docker-compose, so an operator can drop files in without
# rebuilding the image.
RUN mkdir -p /app/packages /app/data /app/certs /app/share \
    /app/tools /app/npm /app/node-builds /app/docker-images /app/debian /app/docs

# Artifact-hub catalogs default to ``<project>/…`` on a source checkout.  Inside
# the image the project root *is* the container root, so pin them to the /app
# paths created above — that keeps a plain ``docker run`` (no Compose) coherent.
# docker-compose sets the same values and adds the bind mounts on top.
ENV TOOLS_DIR=/app/tools \
    NPM_DIR=/app/npm \
    NODE_BUILDS_DIR=/app/node-builds \
    DOCKER_DIR=/app/docker-images \
    DEBIAN_DIR=/app/debian \
    DOCS_DIR=/app/docs \
    PYTHON_BUILDS_DIR=/app/share/cpypiserver_public/cpython

# config/server.py defaults PORT to 9090; pin it to 8080 so the app matches
# the EXPOSE / HEALTHCHECK below and the compose network aliases.
ENV PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8080/health || exit 1

# A single gunicorn worker, deliberately: each worker caches the RBAC grant
# sets it has resolved, so one process keeps a role change immediate rather
# than eventually-consistent across workers (see the note in README.md).
# Threads give concurrency without that cache skew.  Raise --workers only if
# you accept the AuthzService.CACHE_TTL_SECONDS (30s) propagation delay.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "300", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
