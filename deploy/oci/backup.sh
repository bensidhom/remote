#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"
compose_file="$(pwd)/compose.yaml"
compose_env="${BRIDGEGUARD_COMPOSE_ENV_FILE:?BRIDGEGUARD_COMPOSE_ENV_FILE is required}"
[ -r "$compose_env" ] || {
  printf 'backup failed: missing or unreadable Compose environment file: %s\n' "$compose_env" >&2
  exit 1
}
compose() {
  docker compose --env-file "$compose_env" -f "$compose_file" "$@"
}
candidate() {
  # The release workflow builds the checked-out candidate before this script.
  # Never execute backup or restore validation with the old running app image.
  compose run --rm --no-deps -T app "$@"
}

if command -v flock >/dev/null 2>&1; then
  exec 9>/tmp/bridgeguard-backup.lock
  flock -n 9 || {
    printf 'backup failed: another backup is already running\n' >&2
    exit 1
  }
fi

app_container="$(compose ps -q app)"
if [ -z "$app_container" ] || [ "$(docker inspect -f '{{.State.Running}}' "$app_container" 2>/dev/null || true)" != "true" ]; then
  # Existing customer data with no live writer is not proof that a recoverable
  # pre-release state exists.  Treat this as an operator error, never a
  # successful/no-op backup (including a stopped app on the first rollout).
  printf 'backup failed: the existing app container is not running\n' >&2
  exit 1
fi

tunnel_container="$(compose ps -q cloudflared)"
tunnel_was_running=false
if [ -n "$tunnel_container" ] && [ "$(docker inspect -f '{{.State.Running}}' "$tunnel_container" 2>/dev/null || true)" = "true" ]; then
  tunnel_was_running=true
fi

reuse_seconds="${BRIDGEGUARD_BACKUP_REUSE_SECONDS:-0}"
case "$reuse_seconds" in
  ''|*[!0-9]*)
    printf 'backup failed: BRIDGEGUARD_BACKUP_REUSE_SECONDS must be a nonnegative integer\n' >&2
    exit 1
    ;;
esac

recover_old_release=true
recover_old_release_on_failure() {
  status="$?"
  if [ "$status" -ne 0 ] && [ "$recover_old_release" = true ]; then
    printf 'backup failed: restarting the pre-release app container\n' >&2
    docker start "$app_container" >/dev/null || true
    if [ "$tunnel_was_running" = true ] && [ -n "$tunnel_container" ]; then
      # The old app container retains its old image and configuration.  Give it
      # a bounded opportunity to become healthy before restoring tunnel access.
      attempts=0
      app_recovered=false
      while [ "$attempts" -lt 30 ]; do
        health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$app_container" 2>/dev/null || true)"
        if [ "$health" = "healthy" ] || [ "$health" = "running" ]; then
          app_recovered=true
          break
        fi
        attempts=$((attempts + 1))
        sleep 2
      done
      if [ "$app_recovered" = true ]; then
        docker start "$tunnel_container" >/dev/null || true
      else
        printf 'backup recovery warning: old app did not become healthy; tunnel remains stopped\n' >&2
      fi
    fi
  fi
  exit "$status"
}
trap recover_old_release_on_failure 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Drain external traffic first, then stop the only supported application
# writer.  The candidate one-shot container is the sole process that mounts
# /data until both the backup and restore drill have passed.
compose stop -t 30 cloudflared
compose stop -t 120 app
if [ "$(docker inspect -f '{{.State.Running}}' "$app_container")" = "true" ]; then
  printf 'backup failed: app writer did not stop\n' >&2
  exit 1
fi

if [ "$reuse_seconds" -gt 0 ]; then
  recent_backup="$(
    candidate python - "$reuse_seconds" <<'PY'
import sys
import time
from pathlib import Path

maximum_age = int(sys.argv[1])
now = time.time()
backup_root = Path("/data/backups")
candidates = []
if backup_root.is_dir():
    for target in backup_root.iterdir():
        manifest = target / "manifest.json"
        if target.is_dir() and manifest.is_file():
            age = now - manifest.stat().st_mtime
            if 0 <= age <= maximum_age:
                candidates.append((manifest.stat().st_mtime, target))

if candidates:
    print(max(candidates)[1])
PY
  )"
  if [ -n "$recent_backup" ]; then
    # This deliberately rejects v1-v3.  They remain operator-restorable, but
    # their weaker identity/catalog evidence is not reusable for a rollout.
    candidate python -m app.backup \
      --verify-current "$recent_backup" >/dev/null
    candidate python -m app.backup \
      --restore-drill-current "$recent_backup" >/dev/null
    printf 'backup reused: %s (current format, verified, at most %s seconds old)\n' \
      "$recent_backup" "$reuse_seconds"
    recover_old_release=false
    exit 0
  fi
fi

new_backup="$(candidate python -m app.backup --output /data/backups)"
[ -n "$new_backup" ] || {
  printf 'backup failed: candidate did not report a backup path\n' >&2
  exit 1
}
candidate python -m app.backup \
  --verify-current "$new_backup" >/dev/null
candidate python -m app.backup \
  --restore-drill-current "$new_backup" >/dev/null
printf 'restore drill passed with candidate image: %s\n' "$new_backup"

# Leave the old containers stopped.  The deploy workflow starts and verifies
# the candidate next.  The trap restarts the captured old containers only when
# a backup/drill command fails.
recover_old_release=false
