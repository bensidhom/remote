#!/usr/bin/env sh
set -eu

compose_file="${1:-$(dirname "$0")/compose.yaml}"
script_dir="$(CDPATH= cd "$(dirname "$0")" && pwd)"
repo_root="$(CDPATH= cd "$script_dir/../.." && pwd)"
fail() {
  printf 'preflight failed: %s\n' "$*" >&2
  exit 1
}

compose_env="${BRIDGEGUARD_COMPOSE_ENV_FILE:?BRIDGEGUARD_COMPOSE_ENV_FILE is required}"
[ -r "$compose_env" ] || {
  printf 'preflight failed: missing or unreadable Compose environment file: %s\n' "$compose_env" >&2
  exit 1
}

compose() {
  docker compose --env-file "$compose_env" -f "$compose_file" "$@"
}

env_value() {
  awk -F= -v wanted="$1" '
    /^[[:space:]]*#/ { next }
    $1 == wanted { sub(/^[^=]*=/, ""); gsub(/^\"|\"$/, ""); print; exit }
  ' "$compose_env"
}

data_dir="${BRIDGEGUARD_DATA_DIR_HOST:-$(env_value BRIDGEGUARD_DATA_DIR_HOST)}"
models_dir="${BRIDGEGUARD_MODELS_DIR:-$(env_value BRIDGEGUARD_MODELS_DIR)}"
app_env="${BRIDGEGUARD_ENV_FILE:-$(env_value BRIDGEGUARD_ENV_FILE)}"
tunnel_env="${CLOUDFLARE_ENV_FILE:-$(env_value CLOUDFLARE_ENV_FILE)}"
[ -n "$data_dir" ] || fail "BRIDGEGUARD_DATA_DIR_HOST is missing from $compose_env"
[ -n "$models_dir" ] || fail "BRIDGEGUARD_MODELS_DIR is missing from $compose_env"
[ -n "$app_env" ] || fail "BRIDGEGUARD_ENV_FILE is missing from $compose_env"
[ -n "$tunnel_env" ] || fail "CLOUDFLARE_ENV_FILE is missing from $compose_env"

[ "$(uname -m)" = "x86_64" ] ||
  fail "this deployment requires an x86_64 host (OCI VM.Standard.E4.Flex)"

command -v git >/dev/null 2>&1 || fail "git is not installed"
command -v docker >/dev/null 2>&1 || fail "Docker is not installed"
command -v python3 >/dev/null 2>&1 || fail "python3 is not installed"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is not installed"

cpu_count="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '0')"
[ "$cpu_count" -ge 2 ] ||
  fail "at least 2 vCPUs are required; found $cpu_count"

memory_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
[ "${memory_kib:-0}" -ge 11000000 ] ||
  fail "at least 12 GB of configured RAM is required"

[ -d "$data_dir" ] || fail "missing persistent data directory: $data_dir"
[ -d "$models_dir" ] || fail "missing persistent model directory: $models_dir"
[ -r "$app_env" ] || fail "missing or unreadable app environment file: $app_env"
[ -r "$tunnel_env" ] || fail "missing or unreadable Cloudflare environment file: $tunnel_env"

# This OCI target is explicitly research-only. Commercial/license/procurement
# receipt checks are retained in application code for any future separately
# authorized product deployment, but they are not a precondition for updating
# this research instance.
printf 'preflight: research deployment; commercial entitlement gate not applicable\n'

data_owner="$(stat -c '%u' "$data_dir" 2>/dev/null || printf 'unknown')"
[ "$data_owner" = "10001" ] ||
  fail "$data_dir must be owned by container uid 10001 (found $data_owner)"

if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 ||
    fail "sudo is required to inspect the container-owned databases"
  sudo -n true >/dev/null 2>&1 ||
    fail "passwordless sudo is required to inspect the container-owned databases"
fi

run_data_python() {
  if [ "$(id -u)" -eq 0 ]; then
    python3 "$@"
  else
    sudo -n python3 "$@"
  fi
}

run_data_python - "$data_dir" "${BRIDGEGUARD_MIN_BRIDGES:-1}" "$app_env" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

data_dir = Path(sys.argv[1]).resolve()
minimum_bridges = int(sys.argv[2])
bridge_db = data_dir / "snbi_bridges.db"
forecast_db = data_dir / "snbi_forecasts.db"

if not bridge_db.is_file() or bridge_db.stat().st_size < 10 * 1024 * 1024:
    raise SystemExit("preflight failed: the bridge database is missing or unexpectedly small")
if not forecast_db.is_file() or forecast_db.stat().st_size < 1024 * 1024:
    raise SystemExit("preflight failed: the forecast database is missing or unexpectedly small")

with sqlite3.connect(f"file:{bridge_db.as_posix()}?mode=ro", uri=True) as connection:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    # Research deployments may upgrade an older projection database in place.
    # Dedicated-customer marker/binding and current-migration checks belong to
    # a separately provisioned commercial stack, not this research rollout.
    required = {"bridges"}
    missing_tables = sorted(required - tables)
    if missing_tables:
        raise SystemExit(
            "preflight failed: missing database tables: " + ", ".join(missing_tables)
        )
    quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
    if not quick_check or str(quick_check[0]) != "ok":
        raise SystemExit("preflight failed: bridge database integrity check failed")
    inspected_workspace_tables = 0
    for table_name in sorted(tables):
        if not table_name.replace("_", "a").isalnum():
            raise SystemExit("preflight failed: unsafe database table identifier")
        quoted = '"' + table_name + '"'
        table_columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")
        }
        if "workspace_id" not in table_columns:
            continue
        inspected_workspace_tables += 1
    bridge_count = int(connection.execute("SELECT COUNT(*) FROM bridges").fetchone()[0])
    if bridge_count < minimum_bridges:
        raise SystemExit(
            f"preflight failed: expected at least {minimum_bridges} bridges, "
            f"found {bridge_count}"
        )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(bridges)")
    }
    year_count = (
        int(
            connection.execute(
                "SELECT COUNT(DISTINCT data_year) FROM bridges "
                "WHERE data_year IS NOT NULL"
            ).fetchone()[0]
        )
        if "data_year" in columns
        else 0
    )
    attachment_rows = (
        connection.execute(
            """
            SELECT attachment_type, filepath
            FROM bridge_attachments
            WHERE attachment_type IN ('inspection_report', 'point_cloud')
            """
        ).fetchall()
        if "bridge_attachments" in tables
        else []
    )

missing_files = []
attachment_counts = {"inspection_report": 0, "point_cloud": 0}
for attachment_type, raw_path in attachment_rows:
    attachment_counts[attachment_type] += 1
    path = Path(str(raw_path))
    resolved = path if path.is_absolute() else data_dir / path
    if not resolved.is_file():
        missing_files.append(str(raw_path))

if missing_files:
    preview = ", ".join(missing_files[:3])
    raise SystemExit(
        f"preflight failed: {len(missing_files)} attachment file(s) are missing; "
        f"first entries: {preview}"
    )

summary = {
    "bridges": bridge_count,
    "data_years": year_count,
    "attachments": attachment_counts,
    "workspace_tables_verified": inspected_workspace_tables,
    "rag_report_caches": len(
        list((data_dir / "rag_storage" / "bridge_reports").glob("*.json"))
    ),
    "processed_point_clouds": len(
        list(
            (data_dir / "attachments" / "point_clouds").glob(
                "*/processed/analysis.json"
            )
        )
    ),
}
print("persistent data:", json.dumps(summary, sort_keys=True))
PY

available_kib="$(df -Pk "$data_dir" | awk 'NR == 2 {print $4}')"
[ "${available_kib:-0}" -ge 10485760 ] ||
  fail "less than 10 GiB is free on the persistent data filesystem"

compose config --quiet

rendered_data_source="$(compose config --format json | python3 -c '
import json,sys
doc=json.load(sys.stdin)
mounts=doc["services"]["app"].get("volumes",[])
matches=[m.get("source") for m in mounts if m.get("target")=="/data"]
if len(matches)!=1: raise SystemExit(2)
print(matches[0])
')" || fail "could not resolve the rendered app /data bind"
[ "$(readlink -f "$rendered_data_source")" = "$(readlink -f "$data_dir")" ] ||
  fail "rendered /data bind does not match the marker-backed customer data root"

printf 'preflight passed: %s, %s vCPUs, %s MiB RAM, %s MiB free\n' \
  "$(uname -m)" \
  "$cpu_count" \
  "$((memory_kib / 1024))" \
  "$((available_kib / 1024))"
