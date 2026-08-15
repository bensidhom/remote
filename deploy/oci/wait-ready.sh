#!/usr/bin/env sh
set -eu

compose_file="${1:-$(dirname "$0")/compose.yaml}"
compose_env="${BRIDGEGUARD_COMPOSE_ENV_FILE:?BRIDGEGUARD_COMPOSE_ENV_FILE is required}"
compose() {
  docker compose --env-file "$compose_env" -f "$compose_file" "$@"
}
timeout_seconds="${BRIDGEGUARD_READY_TIMEOUT_SECONDS:-600}"
deadline="$(( $(date +%s) + timeout_seconds ))"

while ! compose exec -T app python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=15).read()"
do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    compose ps
    compose logs --tail 100 app embeddings
    printf 'readiness timed out after %s seconds\n' "$timeout_seconds" >&2
    exit 1
  fi
  sleep 5
done

printf 'application readiness passed\n'
