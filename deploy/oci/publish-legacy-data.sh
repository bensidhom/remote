#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"
compose_file="$(pwd)/compose.yaml"
compose_env="${BRIDGEGUARD_COMPOSE_ENV_FILE:?BRIDGEGUARD_COMPOSE_ENV_FILE is required}"
[ -r "$compose_env" ] || {
  printf 'published-data cutover failed: Compose environment file is unreadable\n' >&2
  exit 1
}
compose() {
  docker compose --env-file "$compose_env" -f "$compose_file" "$@"
}
candidate() {
  compose run --rm --no-deps -T app "$@"
}

source_db=/data/snbi_bridges.db
candidate_db=/data/snbi_bridges.published.next.db
workspace_id=shared-research-workspace
actor_id=oci-release-validation
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
receipt_root=/data/backups/published-cutover-"$stamp"

published_count="$(candidate python - "$source_db" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
try:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='published_snapshots'"
    ).fetchone()
    print(connection.execute("SELECT COUNT(*) FROM published_snapshots").fetchone()[0] if exists else 0)
finally:
    connection.close()
PY
)"
case "$published_count" in
  ''|*[!0-9]*)
    printf 'published-data cutover failed: invalid current publication count\n' >&2
    exit 1
    ;;
esac
if [ "$published_count" -gt 0 ]; then
  printf 'published-data cutover skipped: %s snapshots already published\n' "$published_count"
  exit 0
fi

candidate python - "$source_db" "$candidate_db" "$receipt_root" <<'PY'
import os
from pathlib import Path
import sqlite3
import sys

source = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2]).resolve()
receipt_root = Path(sys.argv[3]).resolve()
if source.parent != Path('/data') or target.parent != Path('/data'):
    raise SystemExit('database paths escaped /data')
if target.name != 'snbi_bridges.published.next.db':
    raise SystemExit('unexpected candidate database name')
receipt_root.mkdir(parents=True, exist_ok=False)
for suffix in ('', '-wal', '-shm'):
    stale = Path(str(target) + suffix)
    if stale.exists():
        stale.unlink()
source_connection = sqlite3.connect(str(source), timeout=30.0)
try:
    checkpoint = source_connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    if checkpoint is None or int(checkpoint[0]) != 0:
        raise SystemExit('source WAL checkpoint did not complete')
    target_connection = sqlite3.connect(str(target))
    try:
        source_connection.backup(target_connection, pages=4096)
        target_connection.commit()
    finally:
        target_connection.close()
finally:
    source_connection.close()
with target.open('rb') as handle:
    os.fsync(handle.fileno())
print(receipt_root)
PY

dry_receipt="$receipt_root/backfill-dry-run.json"
apply_receipt="$receipt_root/backfill-apply.json"
forecast_receipt="$receipt_root/forecast-training.json"
validation_receipt="$receipt_root/validation.json"

candidate python -m scripts.backfill_legacy_projection dry-run \
  --source "$source_db" \
  --destination "$candidate_db" \
  --workspace-id "$workspace_id" \
  --actor-id "$actor_id" \
  --profile-id research_historical_import \
  --request-id "oci-published-dry-$stamp" \
  --receipt "$dry_receipt" >/dev/null

manifest_sha="$(candidate python - "$dry_receipt" <<'PY'
import json
from pathlib import Path
import sys

receipt = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
value = str(receipt.get('manifest_sha256', ''))
if len(value) != 64:
    raise SystemExit('invalid dry-run manifest')
print(value)
PY
)"

candidate python -m scripts.backfill_legacy_projection apply \
  --source "$source_db" \
  --destination "$candidate_db" \
  --workspace-id "$workspace_id" \
  --actor-id "$actor_id" \
  --profile-id research_historical_import \
  --request-id "oci-published-apply-$stamp" \
  --expected-manifest-sha256 "$manifest_sha" \
  --publish \
  --receipt "$apply_receipt" >/dev/null

candidate python -m scripts.train_published_forecast \
  --database "$candidate_db" \
  --workspace-id "$workspace_id" \
  --state-code 34 \
  --through-year 2025 \
  --origins 2019,2020,2021,2022,2023,2024 \
  --created-by "$actor_id" \
  --receipt "$forecast_receipt" >/dev/null

candidate python -m scripts.validate_published_database \
  --database "$candidate_db" \
  --workspace-id "$workspace_id" \
  --state-code 34 \
  --bridge-number 0161151 \
  --actor-id "$actor_id" \
  --receipt "$validation_receipt" >/dev/null

candidate python - "$source_db" "$candidate_db" "$receipt_root" <<'PY'
import json
import os
from pathlib import Path
import sqlite3
import sys

source = Path(sys.argv[1]).resolve(strict=True)
candidate = Path(sys.argv[2]).resolve(strict=True)
receipt_root = Path(sys.argv[3]).resolve(strict=True)
archive = receipt_root / 'snbi_bridges.pre-published.db'
if source.parent != Path('/data') or candidate.parent != Path('/data'):
    raise SystemExit('database paths escaped /data')
for database in (source, candidate):
    connection = sqlite3.connect(str(database), timeout=30.0)
    try:
        result = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if result is None or int(result[0]) != 0:
            raise SystemExit(f'WAL checkpoint failed for {database.name}')
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise SystemExit(f'quick_check failed for {database.name}')
    finally:
        connection.close()
for database in (source, candidate):
    for suffix in ('-wal', '-shm'):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            sidecar.unlink()
os.replace(source, archive)
try:
    os.replace(candidate, source)
except BaseException:
    os.replace(archive, source)
    raise
directory_fd = os.open(str(source.parent), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
validation = json.loads((receipt_root / 'validation.json').read_text(encoding='utf-8'))
print(json.dumps({
    'cutover': 'complete',
    'archive': archive.name,
    'bridge_snapshot_count': validation['bridge_snapshot_count'],
    'current_concrete_deck_percent': validation['current_concrete_deck_percent'],
    'forecast_prediction_count': validation['forecast_prediction_count'],
}, sort_keys=True, separators=(',', ':')))
PY
