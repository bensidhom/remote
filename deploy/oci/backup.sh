#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if ! docker compose ps --status running --services | grep -qx app; then
  printf 'backup skipped: no running app container (expected on the first deployment)\n'
  exit 0
fi

if command -v flock >/dev/null 2>&1; then
  exec 9>/tmp/bridgeguard-backup.lock
  flock -n 9 || {
    printf 'backup skipped: another backup is already running\n' >&2
    exit 1
  }
fi

docker compose exec -T app python -m app.backup --output /data/backups
