#!/usr/bin/env sh
set -eu

compose_file="${1:-$(dirname "$0")/compose.yaml}"

running_services="$(
  docker compose -f "$compose_file" ps --status running --services
)"
for service in embeddings app cloudflared; do
  printf '%s\n' "$running_services" | grep -qx "$service" ||
    {
      docker compose -f "$compose_file" ps
      printf 'verification failed: %s is not running\n' "$service" >&2
      exit 1
    }
done

docker compose -f "$compose_file" exec -T app python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=15).read().decode())"

docker compose -f "$compose_file" exec -T app python -c \
  "import json; from app.routers.services.llm_providers import verify_embedding_backend; print(json.dumps(verify_embedding_backend(timeout=30), sort_keys=True))"

printf 'deployment verification passed\n'
