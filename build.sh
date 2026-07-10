#!/usr/bin/env bash
# Build the React frontend and stage it into backend/static so the single
# FastAPI app can serve the SPA alongside the /api endpoints.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Building frontend"
cd "$ROOT/frontend"
npm ci
npm run build

DEST="$ROOT/backend/static"
echo "==> Staging dist -> $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -r "$ROOT/frontend/dist/." "$DEST/"

echo "==> Done. Frontend staged into backend/static"
