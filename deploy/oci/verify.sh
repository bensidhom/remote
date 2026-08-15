#!/usr/bin/env sh
set -eu

compose_file="${1:-$(dirname "$0")/compose.yaml}"
compose_env="${BRIDGEGUARD_COMPOSE_ENV_FILE:?BRIDGEGUARD_COMPOSE_ENV_FILE is required}"
compose() {
  docker compose --env-file "$compose_env" -f "$compose_file" "$@"
}

running_services="$(
  compose ps --status running --services
)"
for service in embeddings app cloudflared; do
  printf '%s\n' "$running_services" | grep -qx "$service" ||
    {
      compose ps
      printf 'verification failed: %s is not running\n' "$service" >&2
      exit 1
    }
done

compose exec -T app python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=15).read().decode())"

# Release-critical storage exercise. The command cannot target customer data;
# it creates and removes its own UUID-named temporary WAL database.
compose exec -T app python -m scripts.run_sqlite_scale_gate

# Commercial publication must be tied to a current, blocker-free scientific
# evaluation receipt. Research deployments report the deliberate screening-only
# state without pretending that an empirical release receipt exists.
compose exec -T app python -c \
  "import os; from app.routers.point_cloud_api import point_cloud_health; from app.services.point_cloud.evaluation import require_current_release_receipt; h=point_cloud_health(); required=bool(h.get('evaluation_required')); r=require_current_release_receipt(os.environ['BRIDGEGUARD_POINT_CLOUD_EVALUATION_RECEIPT']) if required else None; print({'point_cloud_evaluation_required': required, 'point_cloud_evaluation_release_ready': bool(r and r.release_passed), 'screening_only': not required})"

compose exec -T app python -c \
  "import json; from app.routers.services.llm_providers import verify_embedding_backend; print(json.dumps(verify_embedding_backend(timeout=30), sort_keys=True))"

printf 'deployment verification passed\n'
