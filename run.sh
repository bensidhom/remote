#!/usr/bin/env bash
# run.sh — start the BridgeGuard web app (API + built React UI on one port).
#
#   ./run.sh                 # http://localhost:8000
#   PORT=9000 ./run.sh       # custom port
#   RELOAD=1 ./run.sh        # auto-reload on backend code changes (dev)
#   BUILD=1  ./run.sh        # rebuild the frontend first, then run
#
# On first run (or if .venv is missing) it creates the venv and installs deps.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
PORT="${PORT:-8000}"

# venv python (Windows Git-Bash vs Linux/mac layout)
if [ -x "$BACKEND/.venv/Scripts/python.exe" ]; then VENV_PY="$BACKEND/.venv/Scripts/python.exe"
else VENV_PY="$BACKEND/.venv/bin/python"; fi

# 1. Ensure the virtualenv + dependencies exist.
if [ ! -x "$VENV_PY" ]; then
  echo "[run] Creating virtualenv + installing backend deps (first run only)..."
  python -m venv "$BACKEND/.venv"
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -r "$BACKEND/requirements.txt"
fi

# 2. Optionally (re)build the frontend into backend/static.
if [ "${BUILD:-0}" = "1" ]; then
  echo "[run] Building frontend..."
  bash "$ROOT/build.sh"
fi
if [ ! -d "$BACKEND/static" ] && [ ! -d "$ROOT/frontend/dist" ]; then
  echo "[run] No frontend build found — API will run but the UI won't be served. Run ./build.sh or set BUILD=1."
fi

# 3. Launch the server (serves /api + the SPA on http://localhost:$PORT).
UV_ARGS=(-m uvicorn app.main:app --app-dir "$BACKEND" --port "$PORT" --host 0.0.0.0)
[ "${RELOAD:-0}" = "1" ] && UV_ARGS+=(--reload)
echo "[run] Starting BridgeGuard on http://localhost:$PORT  (Ctrl+C to stop)"
exec "$VENV_PY" "${UV_ARGS[@]}"
