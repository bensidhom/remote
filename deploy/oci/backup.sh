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

reuse_seconds="${BRIDGEGUARD_BACKUP_REUSE_SECONDS:-0}"
case "$reuse_seconds" in
  ''|*[!0-9]*)
    printf 'backup failed: BRIDGEGUARD_BACKUP_REUSE_SECONDS must be a nonnegative integer\n' >&2
    exit 1
    ;;
esac

if [ "$reuse_seconds" -gt 0 ]; then
  recent_backup="$(
    docker compose exec -T app python - "$reuse_seconds" <<'PY'
import sys
import time
from pathlib import Path

maximum_age = int(sys.argv[1])
now = time.time()
candidates = []
for target in Path("/data/backups").iterdir():
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
    docker compose exec -T app python -m app.backup \
      --verify "$recent_backup" >/dev/null
    printf 'backup reused: %s (verified, at most %s seconds old)\n' \
      "$recent_backup" "$reuse_seconds"
    exit 0
  fi
fi

docker compose exec -T app python -m app.backup --output /data/backups
