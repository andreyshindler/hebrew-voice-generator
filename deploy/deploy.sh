#!/usr/bin/env bash
#
# Pull the current main and restart the container.
#
# Run by hand on the VPS, or over SSH by .github/workflows/deploy.yml. The
# deploy key in authorized_keys is pinned to this script with a forced command,
# so this file is the entire surface that key can reach - keep it that way.
#
#   ./deploy/deploy.sh            # deploy origin/main
#   ./deploy/deploy.sh --no-pull  # rebuild the working tree as-is
#
# Two things this must never do:
#   * `git clean -xfd`        - deletes .env and data/, both gitignored
#   * `docker compose down -v` - destroys the volume: every account and recording

set -euo pipefail

APP_DIR="${HV_APP_DIR:-/opt/hebrew-voice}"
BRANCH="${HV_DEPLOY_BRANCH:-main}"
CONTAINER="${HV_CONTAINER:-hebrew-voice}"
HEALTH_TIMEOUT="${HV_HEALTH_TIMEOUT:-90}"
KEEP_BACKUPS="${HV_KEEP_BACKUPS:-5}"
LOCK_FILE="${HV_LOCK_FILE:-/tmp/hebrew-voice-deploy.lock}"

PULL=1
for arg in "$@"; do
    case "$arg" in
        --no-pull) PULL=0 ;;
        --help|-h) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# The container name is fixed, so two overlapping deploys would fight over it.
exec 9>"$LOCK_FILE"
flock -n 9 || die "another deploy is already running (lock: $LOCK_FILE)"

cd "$APP_DIR" || die "no such directory: $APP_DIR"
[ -f docker-compose.yml ] || die "$APP_DIR is not the application checkout"
[ -f .env ] || die "$APP_DIR/.env is missing - the app cannot start without it"

compose() { docker compose "$@"; }

# ---------------------------------------------------------------- fetch code

if [ "$PULL" -eq 1 ]; then
    before="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    log "Fetching origin/$BRANCH"
    git fetch --prune origin "$BRANCH"
    # reset, not pull: match the commit exactly and never stop for a merge.
    # Untracked files (.env, data/) are left alone.
    git reset --hard "origin/$BRANCH"
    after="$(git rev-parse --short HEAD)"
    if [ "$before" = "$after" ]; then
        log "Already at $after - rebuilding anyway"
    else
        log "$before -> $after"
        git --no-pager log --oneline "$before..$after" 2>/dev/null | head -10 || true
    fi
fi

# --------------------------------------------------------------- back it up

# Before the restart, while the old container is still up: startup is when
# migrations run, and they are forward-only.
if [ -n "$(compose ps -q "$CONTAINER" 2>/dev/null)" ]; then
    stamp="$(date +%Y%m%d-%H%M%S)"
    log "Backing up the database to /data/backups/$stamp.db"
    # The image has no sqlite3 CLI - use the interpreter it definitely has.
    # .backup() is safe against a live database; a file copy is not.
    if compose exec -T "$CONTAINER" python - "$stamp" "$KEEP_BACKUPS" <<'PY'
import pathlib, sqlite3, sys

stamp, keep = sys.argv[1], int(sys.argv[2])
source = pathlib.Path("/data/hebrew-voice.db")
if not source.exists():
    print("no database yet, nothing to back up")
    raise SystemExit(0)

backups = pathlib.Path("/data/backups")
backups.mkdir(parents=True, exist_ok=True)
target = backups / f"{stamp}.db"

with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, \
        sqlite3.connect(target) as dst:
    src.backup(dst)
print(f"wrote {target} ({target.stat().st_size:,} bytes)")

for old in sorted(backups.glob("*.db"), reverse=True)[keep:]:
    old.unlink()
    print(f"pruned {old.name}")
PY
    then :; else
        die "backup failed - refusing to restart over a database we can't restore"
    fi
else
    log "Container not running - skipping the backup"
fi

# ----------------------------------------------------------------- rebuild

log "Building and restarting"
compose up -d --build

# Old layers accumulate fast on a box that rebuilds on every push.
log "Pruning dangling images"
docker image prune -f >/dev/null || true

# ------------------------------------------------------------ health check

# Inside the container the port is always 8080, whatever HV_PUBLISH_PORT maps
# it to on the host - so this needs no knowledge of .env.
log "Waiting for /healthz (up to ${HEALTH_TIMEOUT}s)"
deadline=$((SECONDS + HEALTH_TIMEOUT))
while [ "$SECONDS" -lt "$deadline" ]; do
    if compose exec -T "$CONTAINER" python -c \
        "import urllib.request,sys; \
         sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3) \
                       .status == 200 else 1)" 2>/dev/null
    then
        # migrate() runs in the lifespan before the app serves, so a healthy
        # answer also means the migrations applied.
        log "Healthy at $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
        exit 0
    fi
    sleep 2
done

printf '\n--- last 50 log lines ---\n' >&2
compose logs --tail=50 "$CONTAINER" >&2 || true
die "the container never became healthy - it has been left running for you to inspect"
