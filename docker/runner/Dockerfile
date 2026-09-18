# ── openfish-runner ─────────────────────────────────────────────────────
# The sandbox the agent runtime executes a task in (§9.2).
#
#   docker build -t openfish-runner:latest -f docker/runner/Dockerfile backend/
#
# The build context is ``backend/`` because the runner *is* the backend
# application plus the tools a checkout needs (git + a minimal compiler
# toolchain) and nothing else.  Deliberately absent:
#
#   * model weights      — the model is reached over the intranet; they are
#                          never shipped into the sandbox;
#   * host docker socket — the runner does not spawn containers.  It runs the
#                          queue worker and executes each task as a subprocess in
#                          its own ``/work/<task_id>`` directory;
#   * frontend / catalogs / published ports.
#
# See docker/runner/README.md for the compose fragment, mounts and limits.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="openfish-runner"
LABEL org.opencontainers.image.description="Agent runtime sandbox — git + headless agent worker, no host docker socket"

# ── System dependencies ─────────────────────────────────────────────────
# git is the only protocol client the runtime needs (read-only clone/fetch, and
# an agent/* push in fix mode).  build-essential is here because a review may
# have to build the project to run its gates; openssh-client only if a
# deployment's mirrors still use ssh://.  docker is deliberately NOT installed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        openssh-client \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# A commit made inside the sandbox is made by the platform, not a person.
# ``advice.detachedHead=false`` keeps a targeted-sha checkout from warning on
# every task; ``init.defaultBranch`` keeps a bare init from blessing a name the
# runtime never pushes anyway.
RUN git config --system user.name "openfish-agent" \
    && git config --system user.email "agent@openfish.invalid" \
    && git config --system advice.detachedHead false \
    && git config --system init.defaultBranch agent/work

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend

# ── Application + Python dependencies ───────────────────────────────────
# Same single source of truth as backend/Dockerfile: backend/pyproject.toml.
WORKDIR /app/backend
COPY pyproject.toml /app/backend/pyproject.toml
RUN python -c "import tomllib; deps = tomllib.loads(open('/app/backend/pyproject.toml').read())['project']['dependencies']; open('/tmp/requirements.txt', 'w').write('\n'.join(deps) + '\n')" \
    && pip install --no-cache-dir \
       -i https://mirrors.aliyun.com/pypi/simple/ \
       -r /tmp/requirements.txt

COPY . /app/backend

# ── Runtime directories ─────────────────────────────────────────────────
# /work is the isolated root the platform sweeps 24h after a task finishes
# (§9.2); /app/data and /app/config are bind-mounted from docker/ by compose.
RUN mkdir -p /work /app/data /app/config \
    && useradd --create-home --uid 10001 runner \
    && chown -R runner:runner /work /app
USER runner
WORKDIR /work

# The runner container runs the queue worker; the API container only enqueues.
# ``--loop`` is S0's CLI (services/agent_queue.py, §9.1); ``--once`` is what a
# cron/one-shot deployment uses instead.
CMD ["python", "-m", "services.agent_queue", "worker", "--loop"]
