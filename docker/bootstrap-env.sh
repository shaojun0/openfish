#!/usr/bin/env bash
# ── openfish bootstrap: make docker/.env existable AND safe ──────────────────
#
#   ./bootstrap-env.sh [--dry-run] [--host <name-or-ip>]
#
# What it does, in order:
#   1. docker/.env      missing -> copy docker/.env.example, mode 600
#   2. Fill EVERY empty secret with a freshly generated value, so the deployment
#      never starts on an insecure default. Values already present are NEVER
#      touched (idempotent), so re-running is safe.
#   3. docker/forgejo/forgejo.env missing -> copy the template, mode 600, fill
#      its three instance keys, and point ROOT_URL/DOMAIN at the host address
#      (--host, else auto-detected).
#   4. Walk the trees compose bind-mounts and warn about symlinks that will not
#      resolve *inside* the container (a bind mount carries the directory, never
#      the host's path space).
#   5. Print exactly what still needs a human (tokens that only exist after the
#      instance is up).
#
# There is deliberately no --force: an existing .env is never overwritten or
# reset, only topped up where a secret is blank. To really start over, delete
# docker/.env (and docker/forgejo/forgejo.env) yourself.
#
# Why not a plain `cp .env.example .env`: SECRET_KEY is REQUIRED by compose
# (`${SECRET_KEY:?...}` aborts on empty), and an empty SECRET_KEY would be worse
# than a missing file, because the failure would move from "compose refuses to
# start" to "the app runs on a blank signing key".
#
# No host tooling beyond a shell is assumed: secrets are generated inside the
# openfish image (python + cryptography), with `openssl` as a fallback, and the
# file edits below are POSIX awk — the host itself needs no python at all.
set -euo pipefail
# Every file this script creates or rewrites holds secrets, so 600 is the
# default here: `cp .env.example .env` would otherwise land 644 under a 022
# umask, and the awk temp file below would be readable while it exists. The
# write-back path preserves the mode of an existing file either way.
umask 077

DRY=0; HOST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --host)    shift; HOST="${1:-}" ;;
    --host=*)  HOST="${1#--host=}" ;;
    # Print this file's leading comment block, whatever its length: stop at the
    # first line that does not continue it (`set -euo pipefail`).
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^#/, ""); sub(/^ /, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

DOCKER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DOCKER_DIR"

IMAGE="${OPENFISH_IMAGE:-openfish:latest}"

# The public address users and git clients will reach. Precedence:
# --host > $OPENFISH_PUBLIC_HOST > the host's primary outbound IP > localhost.
HOST_EXPLICIT=0
if [ -z "$HOST" ]; then
  HOST="${OPENFISH_PUBLIC_HOST:-}"
fi
if [ -n "$HOST" ]; then
  HOST_EXPLICIT=1
fi
if [ -z "$HOST" ]; then
  # `ip route get 1.1.1.1` needs no connectivity, it just resolves the source IP.
  HOST="$(ip route get 1.1.1.1 2>/dev/null | grep -oE 'src [0-9.]+' | awk '{print $2}' | head -1)"
fi
[ -z "$HOST" ] && HOST=127.0.0.1
# Read the advertised port from .env if it is already there; a missing .env must
# not abort the script (`set -e` + a failing grep would kill it before step 1).
PORT=""
if [ -f .env ]; then
  PORT="$(grep -E '^PYPISERVER_PORT=' .env 2>/dev/null | cut -d= -f2- || true)"
fi
[ -z "$PORT" ] && PORT=20416
PUBLIC_BASE="http://${HOST}:${PORT}"

say()  { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }

# Keys a file no longer carries (a .env predating a rename, a retired knob):
# collected here and reported in the closing checklist instead of stopping the
# run — a stale file must not make the whole bootstrap unusable.
DRIFTED=""

# check_mode <file> — these files hold every secret of the deployment, so a mode
# wider than 600 is a leak. A file this script created is already 600; one made
# by hand with `cp .env.example .env` (the README's older path) is 644 and gets
# reported rather than silently chmod'ed — that could fight an operator's ACLs.
check_mode() {
  local f="$1" perms
  perms="$(stat -c %a "$f" 2>/dev/null || echo '?')"
  case "$perms" in
    600) ;;
    *) warn "${f} is mode ${perms}, not 600, but it holds secrets" ;;
  esac
}

# ── secret generation ───────────────────────────────────────────────────────
# Prefer the image: it already has python3 + cryptography, and it is guaranteed
# present in an offline deployment (it is one of the two tarballs we ship).
gen_urlsafe() {
  local n="${1:-48}"
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker run --rm --entrypoint python "$IMAGE" -c \
      "import secrets;print(secrets.token_urlsafe($n))" 2>/dev/null && return 0
  fi
  # 48 bytes of urlsafe base64 ~ 64 chars; 32 bytes of hex is an acceptable
  # substitute for every consumer here (all are opaque HMAC/session keys).
  openssl rand -hex 32
}

gen_fernet() {
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker run --rm --entrypoint python "$IMAGE" -c \
      "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" 2>/dev/null && return 0
  fi
  # 32 random bytes, urlsafe-base64 = a valid Fernet key, via coreutils only.
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'; echo
}

# set_empty <file> <KEY> <value>  — replace a bare "KEY=" line only.
set_empty() {
  local f="$1" k="$2" v="$3"
  if ! grep -qE "^${k}=" "$f"; then
    # The key is nowhere in the file: a .env created before this knob existed,
    # or one that was renamed upstream. Warn instead of silently doing nothing.
    warn "${k}: not in ${f} (stale file?) — left alone"
    DRIFTED="${DRIFTED}${k} "
    return 0
  fi
  grep -qE "^${k}=$" "$f" || return 0   # already has a value: never touch it
  if [ -z "$v" ]; then
    # Every generator failed (no image, no openssl). Writing the blank back
    # would report success on a file that still cannot start, which is the one
    # outcome this script exists to prevent.
    warn "${k}: no value available — refusing to write an empty secret"
    exit 1
  fi
  if [ "$DRY" -eq 1 ]; then say "would set ${k}"; return 0; fi
  # awk, not sed: a generated secret contains `/` and `-`, and sed would read
  # `&` or `\` in the replacement as syntax. The value travels in the
  # environment, so awk never parses it either. The rewritten text is copied
  # back over the original rather than `mv`-ed, so the file keeps its 600 mode.
  local tmp="${f}.tmp.$$"
  K="$k" V="$v" awk '$0 == ENVIRON["K"] "=" { print ENVIRON["K"] "=" ENVIRON["V"]; next } { print }' "$f" > "$tmp"
  cat "$tmp" > "$f"
  rm -f "$tmp"
  say "set ${k}"
}

# set_value <file> <KEY> <value>  — rewrite KEY=... unconditionally (used for the
# instance's public address, where the template ships a placeholder host name).
# Returns non-zero when the key is absent, so callers must tolerate that: under
# `set -e` an unguarded non-zero would kill the script mid-way.
set_value() {
  local f="$1" k="$2" v="$3"
  grep -qE "^${k}=" "$f" || return 3
  if [ -z "$v" ]; then
    warn "${k}: no value available — refusing to write an empty value"
    exit 1
  fi
  if [ "$DRY" -eq 1 ]; then say "would set ${k}=${v}"; return 0; fi
  # Same write path as set_empty: awk via the environment, `cat` back into the
  # original inode so the 600 mode survives, `index()` instead of a regex
  # because an env name is a literal.
  local tmp="${f}.tmp.$$"
  K="$k" V="$v" awk 'index($0, ENVIRON["K"] "=") == 1 { print ENVIRON["K"] "=" ENVIRON["V"]; next } { print }' "$f" > "$tmp"
  cat "$tmp" > "$f"
  rm -f "$tmp"
  say "set ${k}=${v}"
}

echo "openfish — bootstrapping environment files in $DOCKER_DIR"
say "public base for this host: $PUBLIC_BASE"

# ── 1+2. docker/.env ────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  if [ "$DRY" -eq 1 ]; then
    say "would create .env from .env.example"
  else
    cp .env.example .env
    chmod 600 .env
    say "created .env from .env.example"
  fi
elif [ -f .env ]; then
  say "kept the existing .env (only blank secrets are filled; delete it to reset)"
fi

if [ -f .env ]; then
  check_mode .env
  set_empty .env SECRET_KEY           "$(gen_urlsafe 48)"
  set_empty .env FORGEJO_WEBHOOK_SECRET "$(gen_urlsafe 32)"
  set_empty .env GIT_IDENTITY_KEY     "$(gen_fernet)"
  set_empty .env RUNNER_CREDENTIAL_KEY "$(gen_fernet)"
  set_empty .env MODEL_ROUTE_KEY      "$(gen_fernet)"
  # Deliberately NOT generated: FORGEJO_ADMIN_TOKEN and FORGEJO_RUNNER_TOKEN can
  # only be minted inside a running Forgejo, by an admin who has just created the
  # account. Filling them with junk would be worse than leaving them empty (the
  # app fails closed on both). See the closing checklist.
fi

# ── 2b. JSON-array knobs (no longer patched) ────────────────────────────────
# Older revisions of this project could not survive a blank value for a
# `list[str]` field: compose forwards knobs as `${VAR:-}`, and pydantic-settings
# JSON-decodes complex fields while collecting values, so `ALLOW_EXTENSIONS=`
# raised a SettingsError and the backend crash-looped. Upstream fixed that at the
# source (``env_ignore_empty=True`` in ``config/base.py``), verified with the
# repository's own gate: `.agent/checks/check_blank_env_defaults.py`.
#
# So blank genuinely means "unset" now, and this script must NOT rewrite those
# variables to `[]`: an explicit `[]` is a *meaningful* setting (for
# ALLOW_EXTENSIONS it means "reject every extension"), which is deliberately
# different from "use the default". Rewriting would silently change behaviour.

# ── 3. docker/forgejo/forgejo.env ───────────────────────────────────────────
FE=forgejo/forgejo.env
if [ ! -f "$FE" ]; then
  if [ "$DRY" -eq 1 ]; then
    say "would create $FE from forgejo.env.example"
  else
    cp forgejo/forgejo.env.example "$FE"
    chmod 600 "$FE"
    say "created $FE from forgejo.env.example"
  fi
fi

if [ -f "$FE" ]; then
  check_mode "$FE"
  set_empty "$FE" FORGEJO__security__SECRET_KEY    "$(gen_urlsafe 48)"
  set_empty "$FE" FORGEJO__security__INTERNAL_TOKEN "$(gen_urlsafe 48)"
  set_empty "$FE" FORGEJO__oauth2__JWT_SECRET      "$(gen_urlsafe 48)"
  # Point the instance at this host. The template ships registry.example.invalid,
  # which would produce wrong clone and webhook URLs. ROOT_URL's trailing /git/ is
  # load-bearing. An explicit --host/OPENFISH_PUBLIC_HOST always wins; otherwise
  # an address the operator already chose is respected.
  cur_root="$(grep -E '^FORGEJO__server__ROOT_URL=' "$FE" | cut -d= -f2- || true)"
  cur_dom="$(grep -E '^FORGEJO__server__DOMAIN=' "$FE" | cut -d= -f2- || true)"
  if [ "$HOST_EXPLICIT" -eq 1 ] || [ -z "$cur_root" ] || [ "${cur_root#*example.invalid}" != "$cur_root" ]; then
    set_value "$FE" FORGEJO__server__ROOT_URL "${PUBLIC_BASE}/git/" || true
  else
    say "kept FORGEJO__server__ROOT_URL=$cur_root"
  fi
  if [ "$HOST_EXPLICIT" -eq 1 ] || [ -z "$cur_dom" ] || [ "${cur_dom#*example.invalid}" != "$cur_dom" ]; then
    set_value "$FE" FORGEJO__server__DOMAIN "$HOST" || true
  else
    say "kept FORGEJO__server__DOMAIN=$cur_dom"
  fi
fi

# ── 3b. docker/forgejo/ ownership ───────────────────────────────────────────
# The Forgejo image runs as the non-root `git` user (uid/gid 1000), and
# docker/forgejo is bind-mounted as its /data.  A checkout or copy made by root
# is root:root 0755, so Forgejo cannot create /data/forgejo (SQLite + logs) and
# restart-loops with:
#   ORM engine initialization ... failed. Error: ... mkdir /data/forgejo:
#   permission denied
# Every other bind mount is served by a root process, so this is the only one
# that needs the ownership step.  Fix it when this script runs as root;
# otherwise print the exact command instead of failing silently.
FORGEJO_UID=1000
if [ -d forgejo ]; then
  current_owner="$(stat -c '%u:%g' forgejo 2>/dev/null || true)"
  if [ "$current_owner" != "${FORGEJO_UID}:${FORGEJO_UID}" ]; then
    if [ "$DRY" -eq 1 ]; then
      say "would chown -R ${FORGEJO_UID}:${FORGEJO_UID} forgejo"
    elif [ "$(id -u)" -eq 0 ]; then
      chown -R "${FORGEJO_UID}:${FORGEJO_UID}" forgejo
      say "chown -R ${FORGEJO_UID}:${FORGEJO_UID} forgejo (Forgejo runs as the non-root git user)"
    else
      warn "forgejo/ is owned by ${current_owner}; Forgejo runs as ${FORGEJO_UID} and cannot write there."
      warn "  fix with: sudo chown -R ${FORGEJO_UID}:${FORGEJO_UID} docker/forgejo"
    fi
  fi
  if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null || true)" = "Enforcing" ]; then
    warn "SELinux is Enforcing: if Forgejo still reports permission denied after the chown,"
    warn "  relabel the data dir: sudo chcon -Rt container_file_t docker/forgejo"
    warn "  (or append :Z to the forgejo volume in docker-compose.yml)"
  fi
fi

# ── 4. mount links ──────────────────────────────────────────────────────────
# A bind mount carries the directory, never the host's path space: a link
# *inside* one of these trees is resolved by the container, where a host
# absolute target does not exist.  Only the top-level source link
# (`docker/npm` -> /srv/...) may be absolute — Docker resolves that one on the
# host, which is why it is not scanned here.  `share/` matters most: it holds
# `PACKAGES_DIR`, a directory the backend creates on boot, so a host-absolute
# link there stops the container before it serves anything.
#
# The big mirrors (npm/, node-builds/, docker-images/, debian/) are deliberately
# not walked — the backend only reads them, and a depth-3 scan of a full mirror
# on a network disk is expensive.  The same rule still applies to links in them.
LINK_WARNINGS=0
for tree in share tools docs data; do
  root="$(readlink -f "$tree" 2>/dev/null || true)"
  [ -d "$root" ] || continue
  while IFS= read -r link; do
    target="$(readlink "$link")"
    case "$target" in
      /*)
        warn "container cannot follow ${link} -> ${target} (use a relative target)"
        LINK_WARNINGS=$((LINK_WARNINGS + 1))
        ;;
      *)
        if [ ! -e "$link" ]; then
          warn "broken link: ${link} -> ${target}"
          LINK_WARNINGS=$((LINK_WARNINGS + 1))
        fi
        ;;
    esac
  done < <(find "$root" -maxdepth 3 -type l -print 2>/dev/null || true)
done
# `if`, not `[ ... ] && say`: the test is false on a clean tree, and its status
# would become the script's under `set -e` if it were the last command.
if [ "$LINK_WARNINGS" -eq 0 ]; then
  say "mount links: nothing the container cannot follow"
fi

# ── 5. human checklist ──────────────────────────────────────────────────────
echo
if [ -n "$DRIFTED" ]; then
  warn "not in the file, so nothing was generated for: ${DRIFTED}"
  warn "either the .env predates that key, or the knob was renamed upstream"
  echo
fi
if [ "$LINK_WARNINGS" -gt 0 ]; then
  warn "${LINK_WARNINGS} mount link(s) will not resolve inside the container (see above)"
  warn "a link inside a bind mount has to be relative; docker/README.md §4"
  echo
fi
echo "openfish — still needs a human:"
say "1. docker/.env : after the stack is up, create the Forgejo admin account and"
say "   mint its token, then put it in docker/.env as FORGEJO_ADMIN_TOKEN:"
say "     docker exec -u git openfish-forgejo forgejo admin user create \\"
say "       --username openfish-admin --password '<strong>' \\"
say "       --email openfish-admin@example.invalid --admin --must-change-password=false"
say "     docker exec -u git openfish-forgejo forgejo admin user generate-access-token \\"
say "       --username openfish-admin --token-name openfish-platform --scopes all --raw"
say "2. Same for FORGEJO_RUNNER_TOKEN if you use the runner plane (non-admin bot account)."
echo
echo "If this host is not reachable at ${PUBLIC_BASE}, re-run with:"
echo "  ./bootstrap-env.sh --host <intranet-ip-or-dns-name>"
echo
echo "Then:  docker compose up -d        (add --build on a source checkout)"
echo "       docker compose exec backend python /app/cli.py create-admin <login>"
# A trailing `[ ... ] && echo` would make a successful run exit 1 whenever the
# condition is false (the test's status becomes the script's), so use an if.
if [ "$DRY" -eq 1 ]; then
  echo "(dry run — nothing was written)"
fi
exit 0
