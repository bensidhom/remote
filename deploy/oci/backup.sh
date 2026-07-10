#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"
docker compose exec -T app python -m app.backup --output /data/backups

