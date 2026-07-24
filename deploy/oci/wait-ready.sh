#!/usr/bin/env sh
set -eu

compose_file="${1:-$(dirname "$0")/compose.yaml}"
timeout_seconds="${BRIDGEGUARD_READY_TIMEOUT_SECONDS:-600}"
deadline="$(( $(date +%s) + timeout_seconds ))"

while ! docker compose -f "$compose_file" exec -T app python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=15).read()"
do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    docker compose -f "$compose_file" ps
    docker compose -f "$compose_file" logs --tail 100 app embeddings
    printf 'readiness timed out after %s seconds\n' "$timeout_seconds" >&2
    exit 1
  fi
  sleep 5
done

printf 'application readiness passed\n'
