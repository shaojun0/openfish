#!/bin/sh
# ── openfish role dispatcher ───────────────────────────────────────────
# This image carries both planes of the platform, selected at run time by
# ``OPENFISH_ROLE`` instead of by two Dockerfiles:
#
#   OPENFISH_ROLE=backend  (default)  gunicorn app:app — API, registries,
#                                     artifact catalogs, the whole HTTP surface
#   OPENFISH_ROLE=runner              services.agent_queue worker --loop — the
#                                     agent-runtime plane (clone, gates, review,
#                                     fix/PR), one task per /work/<task_id>
#
# Everything that still differs between the planes — secrets, capabilities, a
# read-only root, mounts, resource limits — is enforced by Compose per service
# (``docker/docker-compose.yml``), not by a second image.  In particular the
# runner never inherits the backend's environment anchor, so it never receives
# SECRET_KEY / FORGEJO_ADMIN_TOKEN / GIT_IDENTITY_KEY.
#
# An explicit command still wins over the dispatch, which keeps
# ``docker run openfish bash``, the ``debug`` profile and a one-shot worker
# (``docker compose --profile runner run --rm runner python -m
# services.agent_queue worker --once``) working.
#
# ``openfish-entrypoint healthcheck`` is the role-aware image HEALTHCHECK: only
# the backend plane listens on HTTP, so probing a port in the runner would report
# a permanently unhealthy container.  For the runner the process was exec'd
# below, so a dead worker takes the container down and Docker restarts it —
# "alive" is the whole health signal there.
set -eu

role() {
    printf '%s' "${OPENFISH_ROLE:-backend}"
}

# ── healthcheck subcommand (Dockerfile HEALTHCHECK) ────────────────────
if [ "${1:-}" = "healthcheck" ]; then
    case "$(role)" in
        backend)
            exec curl -fsS "http://127.0.0.1:${PORT:-8080}/health" > /dev/null
            ;;
        runner)
            exit 0
            ;;
        *)
            printf 'openfish: unknown OPENFISH_ROLE=%s (expected: backend | runner)\n' "$(role)" >&2
            exit 1
            ;;
    esac
fi

# ── an explicit command beats the dispatch ─────────────────────────────
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# ── role dispatch ──────────────────────────────────────────────────────
cd /app
case "$(role)" in
    backend)
        # A single gunicorn worker, deliberately: each worker caches the RBAC
        # grant sets it has resolved, so one process keeps a role change
        # immediate rather than eventually-consistent across workers (README.md).
        # Threads give concurrency without that cache skew.  Raise --workers only
        # if you accept the AuthzService.CACHE_TTL_SECONDS (30s) propagation delay.
        exec gunicorn \
            --bind "${HOST:-0.0.0.0}:${PORT:-8080}" \
            --workers 1 \
            --threads 8 \
            --timeout 300 \
            --access-logfile - \
            --error-logfile - \
            app:app
        ;;
    runner)
        exec python -m services.agent_queue worker --loop
        ;;
    *)
        printf 'openfish: unknown OPENFISH_ROLE=%s (expected: backend | runner)\n' "$(role)" >&2
        exit 64
        ;;
esac
