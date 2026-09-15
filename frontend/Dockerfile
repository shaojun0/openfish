# ── openfish frontend ───────────────────────────────────────────────
# Builds the Vue 3 SPA and serves the static bundle with nginx.  Build context
# is this directory (``frontend/``), so the image never sees the backend:
#
#   docker build -t openfish-frontend:latest frontend/
#
# Node exists only in the build stage and never ships to production.

FROM node:24-slim AS build

WORKDIR /app

# Optional mirror, e.g. --build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=

# Dependency manifests first, for layer caching: this layer only rebuilds when
# the lockfile changes.
COPY package.json package-lock.json .npmrc ./
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm ci --no-audit --no-fund

# Sources.  `.dockerignore` keeps node_modules/ and any stale dist/ out, so
# this cannot clobber the install above.
COPY . .

# Vite writes to frontend/dist (see vite.config.ts).
RUN npm run build


# ── Runtime: static file server ─────────────────────────────────────
FROM nginx:1.27-alpine

LABEL org.opencontainers.image.title="openfish-frontend"
LABEL org.opencontainers.image.description="Vue 3 SPA for the openfish artifact hub"

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
