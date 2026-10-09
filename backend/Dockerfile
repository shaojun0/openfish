# ── openfish — one application image, two runtime planes ───────────────
# Build context is this directory (``backend/``), so the image contains the
# Flask application and the tools an agent checkout needs.  The deployment used
# to build two images from this same context — ``openfish-backend`` (Flask +
# gunicorn) and ``openfish-runner`` (git + the agent-runtime queue worker) —
# which differed only in the apt packages and the start command.  They are now
# **one** image and the plane is a runtime choice:
#
#   docker build -t openfish:latest backend/
#
#   OPENFISH_ROLE=backend  (default)  →  gunicorn app:app
#   OPENFISH_ROLE=runner              →  python -m services.agent_queue worker --loop
#
# ``docker-entrypoint.sh`` is the single place that maps a role to a process; an
# explicit command still wins over the dispatch, which is how the debug profile
# and a one-shot ``worker --once`` run.  Compose wires the variable per service
# (``docker/docker-compose.yml``), so the two planes cannot drift apart in their
# Python dependencies or in their application code, and adding a third plane
# later is an entrypoint case plus a compose service — never a second image.
#
# What the image still deliberately does **not** contain:
#
#   * model weights      — the model is reached over the intranet, never shipped;
#   * host docker socket — the runner does not spawn containers; it executes each
#                          task as a subprocess in its own ``/work/<task_id>``;
#   * credentials        — SECRET_KEY, FORGEJO_*_TOKEN, GIT_IDENTITY_KEY,
#                          RUNNER_CREDENTIAL_KEY … are injected per plane at run
#                          time.  Compose keeps two separate environment anchors
#                          on purpose: the runner container must never receive
#                          the backend plane's secrets (see the anchors and
#                          ``docker/runner/README.md`` §2.4 / §7);
#   * frontend / catalogs — the SPA is its own image and the artifact catalogs
#                          are bind-mounted by Compose.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="openfish"
LABEL org.opencontainers.image.description="Artifact hub and agent runtime — one image, plane selected by OPENFISH_ROLE"

# ── System dependencies ─────────────────────────────────────────────
# The union of what either plane needs, because one image serves both:
#   libmagic1        python-magic upload inspection             (backend plane)
#   curl             HEALTHCHECK + the device-auth client flow   (backend plane)
#   git              read-only clone/fetch, agent/* push         (runner plane)
#   build-essential  a review may have to build the checkout to run its gates
#                                                               (runner plane)
#   openssh-client   only if a deployment's mirrors still use ssh://
#   ca-certificates  TLS trust for both planes
# nginx/supervisor are deliberately *not* installed: the reverse proxy is its
# own container.  docker is deliberately NOT installed: the runner does not
# spawn containers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmagic1 \
        curl \
        git \
        ca-certificates \
        openssh-client \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# A commit made inside the sandbox is made by the platform, not a person.
# ``advice.detachedHead=false`` keeps a targeted-sha checkout from warning on
# every task; ``init.defaultBranch`` keeps a bare init from blessing a name the
# runtime never pushes anyway.  Installed for both planes (the system config is
# inert unless git is actually invoked).
RUN git config --system user.name "openfish-agent" \
    && git config --system user.email "agent@openfish.invalid" \
    && git config --system advice.detachedHead false \
    && git config --system init.defaultBranch agent/work

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# ── Python dependencies ─────────────────────────────────────────────
# Single source of truth: pyproject.toml (shared with uv / local dev).
# Copied first for Docker layer caching — this layer only rebuilds when
# pyproject.toml itself changes.  gunicorn is the backend plane's server; the
# runner plane simply never starts it.
WORKDIR /app
COPY pyproject.toml .
# Dependencies go through a generated requirements file rather than an unquoted
# `$(python -c …)` expansion: `psycopg[binary]>=3.1` contains shell-glob
# metacharacters, and a requirements file is immune to word-splitting and
# pathname expansion.
# Optional dependency groups to install as well: space-separated names from
# `[project.optional-dependencies]`, e.g. `--build-arg OPENFISH_EXTRAS=s3` to
# make `OBJECT_BACKEND=s3` usable in this image.  Empty (the default) keeps the
# image at the mandatory set — a local-directory deployment does not carry boto3.
ARG OPENFISH_EXTRAS=""
RUN python -c "import tomllib; d = tomllib.loads(open('/app/pyproject.toml').read()); deps = [*d['project']['dependencies'], 'gunicorn>=22', *sum([d['project']['optional-dependencies'][e] for e in '${OPENFISH_EXTRAS}'.split()], [])]; open('/tmp/requirements.txt', 'w').write('\n'.join(deps) + '\n')" \
    && pip install -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir \
       -r /tmp/requirements.txt

# ── Application code ────────────────────────────────────────────────
# Everything not excluded by backend/.dockerignore lands here.
COPY . .

# ── Runtime directories + the sandbox identity ──────────────────────
# /app/data, /app/config and the catalog roots either hold state this process
# writes or are bind-mounted over by Compose (so an operator can drop files in
# without rebuilding the image); /work is the isolated root the runner plane
# sweeps 24h after a task finishes (§9.2).
#
# Privilege separation inside the container (slice A): the worker runs as
# **root** (uid 0) and keeps the git credential, while untrusted repository code
# (the repo's own check_*.py and the headless review command) is dropped
# in-child to the dedicated sandbox uid 10002.  Both share gid 10000
# ("openfish"), so the work tree can be made group-usable by the sandbox without
# any chown.
#
# Why a root worker and not a non-root one: Docker grants effective capabilities
# only to a root process.  A non-root worker with `cap_add` gets those caps in
# its *bounding* set only (CapEff=0), so `untrusted_popen_kwargs()` fails closed
# and every task's gates go red.  Root plus `cap_drop: ALL` and the small set the
# worker needs is the effective configuration; the drop to uid 10002 clears the
# child's capabilities (0 → non-zero uid transition).  The backend plane is
# unaffected: it never asks for a privilege drop and compose grants it no extra
# capability.
#
# The worker is also a member of the shared group: `prepare_untrusted_workdir()`
# chgrps the work tree to gid 10000, which a root process may only do for a group
# it belongs to (CAP_CHOWN is deliberately *not* granted).
RUN mkdir -p /app/packages /app/data /app/certs /app/config /app/share /work \
        /app/tools /app/npm /app/node-builds /app/docker-images /app/debian /app/docs \
    && groupadd --gid 10000 openfish \
    && useradd --uid 10002 --gid 10000 --no-create-home \
        --shell /usr/sbin/nologin sandbox \
    && usermod -aG openfish root

# Artifact-hub catalogs default to ``<project>/…`` on a source checkout.  Inside
# the image the project root *is* the container root, so pin them to the /app
# paths created above — that keeps a plain ``docker run`` (no Compose) coherent.
# docker-compose sets the same values and adds the bind mounts on top.
# config/server.py defaults PORT to 9090; pin it to 8080 so the app matches the
# EXPOSE / HEALTHCHECK below and the compose network aliases.
ENV TOOLS_DIR=/app/tools \
    NPM_DIR=/app/npm \
    NODE_BUILDS_DIR=/app/node-builds \
    DOCKER_DIR=/app/docker-images \
    DEBIAN_DIR=/app/debian \
    DOCS_DIR=/app/docs \
    PYTHON_BUILDS_DIR=/app/share/cpypiserver_public/cpython \
    PORT=8080

# ── The role dispatcher ─────────────────────────────────────────────
# One file maps OPENFISH_ROLE to the process each plane runs; its header is the
# reference for the variable.  Installed as the image ENTRYPOINT, so an empty
# argv means "dispatch by role" and an explicit argv is executed verbatim.
COPY docker-entrypoint.sh /usr/local/bin/openfish-entrypoint
RUN chmod 0755 /usr/local/bin/openfish-entrypoint

# Both planes run as root.  For the runner that is load-bearing: Docker grants
# effective capabilities only to a root process, and the worker needs
# CAP_SETUID/CAP_SETGID (added by compose) to drop the untrusted child to uid
# 10002.  The backend plane never asks for a drop and compose grants it no
# capability.  Stated explicitly so the invariant is visible in the file instead
# of being implied by Docker's default user.
USER root

EXPOSE 8080

# Role-aware: the backend plane probes /health over HTTP, the runner plane
# serves no port at all.  Probing a port in the runner would report a
# permanently unhealthy container, which is why the probe lives in the
# entrypoint alongside the dispatch rather than in this file.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["/usr/local/bin/openfish-entrypoint", "healthcheck"]

# No CMD on purpose: with an empty argv the entrypoint selects the process from
# OPENFISH_ROLE.  A CMD here would take the dispatch's place and silently fix
# the plane.
ENTRYPOINT ["/usr/local/bin/openfish-entrypoint"]
