#!/usr/bin/env bash
# ── openfish: create the bind-mount sources docker-compose.yml expects ──────
#
# docker-compose.yml only ever mounts paths inside this directory (./share,
# ./npm, ./data, ./config/model_routes.json, …).  This script creates them, so a
# fresh checkout can go straight to `docker compose up -d --build`.
#
# Usage
# -----
#   ./prepare-mounts.sh
#       Local defaults.  Each artifact mirror becomes a real, empty directory
#       seeded from docker/examples/<name>/, docker/data links to ../backend/data
#       and docker/config/model_routes.json links to the tracked routing table.
#       Both of those links are what keep `cd backend && python app.py` and the
#       containers on one database and one route table.
#
#   ./prepare-mounts.sh /media/disk/openfish-mirror
#       The deployment layout: the five artifact mirrors become symlinks into
#       that directory (share -> the root itself, npm -> <root>/npm, …).  The
#       target must already exist — this script never invents a data disk.
#
#   ./prepare-mounts.sh --force /media/disk/openfish-mirror
#       Re-point even a path that already exists (an existing *directory* is
#       only removed when it is empty).
#
# The script is idempotent: with no --force it reports what is already in place
# and changes nothing else.  Re-pointing a single mount is just as valid done by
# hand:  ln -sfn /srv/data/npm docker/npm
set -euo pipefail

FORCE=0
DATA_ROOT=""
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) DATA_ROOT="$arg" ;;
    esac
done

DOCKER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$DOCKER_DIR")"

#: Artifact mirrors, in the order the compose file lists them.  `share` maps to
#: the data root itself because PACKAGES_DIR / PYTHON_BUILDS_DIR are sub-paths
#: of it (`/app/share/python`, `/app/share/python-build-standalone`).
MIRRORS=(share npm node-builds docker-images debian)

log()  { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }

#: Replace $1 with a symlink to $2 unless it is already that link.
link() {
    local path="$1" target="$2"
    if [ -L "$path" ] && [ "$(readlink "$path")" = "$target" ]; then
        log "ok      $path -> $target"
        return
    fi
    if [ -e "$path" ] && [ "$FORCE" -eq 0 ]; then
        warn "$path exists (not a link to $target) — leaving it; use --force to replace"
        return
    fi
    if [ -d "$path" ] && [ -n "$(ls -A "$path" 2>/dev/null)" ]; then
        warn "--force: discarding the contents of $path"
    fi
    rm -rf -- "$path"
    ln -s "$target" "$path"
    log "link    $path -> $target"
}

#: Create $1 as a real directory, seeding it from docker/examples/$2 when that
#: sample exists and the directory is empty.
seed_dir() {
    local path="$1" sample="$DOCKER_DIR/examples/$2"
    if [ -L "$path" ]; then
        log "keep    $path -> $(readlink "$path")"
        return
    fi
    mkdir -p "$path"
    if [ -d "$sample" ] && [ -z "$(ls -A "$path" 2>/dev/null)" ]; then
        cp -a "$sample/." "$path/"
        log "seed    $path (from examples/$2)"
    else
        log "ok      $path"
    fi
}

echo "openfish — preparing bind-mount sources in $DOCKER_DIR"

# ── Artifact mirrors ────────────────────────────────────────────────────────
if [ -n "$DATA_ROOT" ]; then
    [ -d "$DATA_ROOT" ] || { echo "no such data directory: $DATA_ROOT" >&2; exit 2; }
    DATA_ROOT="$(cd "$DATA_ROOT" && pwd)"
    echo "artifact mirrors -> $DATA_ROOT"
    for name in "${MIRRORS[@]}"; do
        if [ "$name" = "share" ]; then
            link "$DOCKER_DIR/share" "$DATA_ROOT"
        else
            link "$DOCKER_DIR/$name" "$DATA_ROOT/$name"
        fi
    done
else
    echo "artifact mirrors -> local directories under docker/ (sample catalogs)"
    for name in "${MIRRORS[@]}"; do
        seed_dir "$DOCKER_DIR/$name" "$name"
    done
fi

# ── Runtime state: one database and one route table for both entry points ───
mkdir -p "$PROJECT_DIR/backend/data"
link "$DOCKER_DIR/data" "$PROJECT_DIR/backend/data"

mkdir -p "$DOCKER_DIR/config"
link "$DOCKER_DIR/config/model_routes.json" "$PROJECT_DIR/backend/config/model_routes.json"

# ── Agent Hub: git plane + agent work directories ───────────────────────────
# Forgejo's bare repositories and SQLite database.  A real directory, always
# writable, and re-pointed at the data disk when one was given.
if [ -n "$DATA_ROOT" ]; then
    mkdir -p "$DATA_ROOT/forgejo"
    link "$DOCKER_DIR/forgejo" "$DATA_ROOT/forgejo"
else
    mkdir -p "$DOCKER_DIR/forgejo"
fi
# The instance's secrets live in this file, not in the repository: copy the
# template on first run and lock it down (compose reads it via env_file, so a
# missing file aborts `docker compose up` rather than starting an insecure one).
if [ ! -f "$DOCKER_DIR/forgejo/forgejo.env" ]; then
    cp "$DOCKER_DIR/forgejo/forgejo.env.example" "$DOCKER_DIR/forgejo/forgejo.env"
    chmod 600 "$DOCKER_DIR/forgejo/forgejo.env"
    echo "  created docker/forgejo/forgejo.env — fill in SECRET_KEY / INTERNAL_TOKEN /"
    echo "  JWT_SECRET (and later FORGEJO_ADMIN_TOKEN) before starting Forgejo."
fi
# Each agent task gets /work/<task_id>; kept 24h after it finishes for triage.
mkdir -p "$DOCKER_DIR/agent-work"

echo
echo "Done.  Next:  cp .env.example .env   then   docker compose up -d --build"
