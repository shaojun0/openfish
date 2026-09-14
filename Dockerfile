# ── Final stage ─────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="cpypiserver"
LABEL org.opencontainers.image.description="Lightweight Python package server with nginx index cache"

# ── System dependencies ─────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
    libmagic1 \
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
    gunicorn>=22

# ── Application code ────────────────────────────────────────────────
# Everything not excluded by .dockerignore lands here.
# When you add a new directory (e.g. "templates/"), just add it to
# .dockerignore only if it should be excluded — no Dockerfile change needed.
COPY . .

# ── Runtime ─────────────────────────────────────────────────────────
RUN mkdir -p /app/packages /app/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8080/health || exit 1

CMD ["python", "app.py"]
