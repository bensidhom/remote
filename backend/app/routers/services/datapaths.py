# backend/app/routers/services/datapaths.py
# Single source of truth for where ALL persistent app data lives. Everything sits
# under ONE folder, `data/`, at the repo root (sibling of backend/ frontend/
# electron/), so it's easy to find, back up, and relocate.
#
# Override the root with the BRIDGEGUARD_DATA_DIR env var — the packaged Electron
# app sets it to a user-writable directory, since the repo path doesn't exist
# there. Per-store env overrides (RAG_WORKING_DIR, SNBI_TEMP_DIR) still work.
import os
from pathlib import Path

# datapaths.py = <repo>/backend/app/routers/services/datapaths.py
#   parents[3] = <repo>/backend   parents[4] = <repo>
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = Path(__file__).resolve().parents[4]

# Load .env (KEY=VALUE lines) into the environment before any env reads below or
# elsewhere (e.g. CEREBRAS_API_KEY / NVIDIA_API_KEY for the hosted AI providers).
# Checks backend/.env first (the app's own config, self-contained deploy bundle),
# then <repo>/.env. Real environment variables always win. Dependency-free.
for _env_file in (_BACKEND_DIR / '.env', _REPO_ROOT / '.env'):
    try:
        if _env_file.is_file():
            for _line in _env_file.read_text(encoding='utf-8').splitlines():
                _line = _line.strip()
                if not _line or _line.startswith('#') or '=' not in _line:
                    continue
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except Exception:
        pass

# Data lives in backend/data by default so the backend/ folder is a self-contained
# deploy bundle. Override with BRIDGEGUARD_DATA_DIR (e.g. a persistent disk / EFS
# mount on the server, or an Electron user-data dir).
DATA_DIR = Path(os.getenv('BRIDGEGUARD_DATA_DIR', str(_BACKEND_DIR / 'data')))

USER_DB = str(DATA_DIR / 'snbi_bridges.db')          # main bridge inventory DB
FORECAST_DB = str(DATA_DIR / 'snbi_forecasts.db')    # forecasting runs
ATTACHMENTS_DIR = str(DATA_DIR / 'attachments')      # reports/ + point_clouds/ per bridge
SAVE_DIR = str(DATA_DIR / 'exports')                 # SNBI/NBI export output
RAG_WORKING_DIR = os.getenv('RAG_WORKING_DIR', str(DATA_DIR / 'rag_storage'))  # embeddings, images, KG
TEMP_DIR = Path(os.getenv('SNBI_TEMP_DIR', str(DATA_DIR / 'temp')))            # scratch

# Create the directories up front (DB files are created by their writers/SQLite).
for _d in (DATA_DIR, Path(ATTACHMENTS_DIR), Path(SAVE_DIR), Path(RAG_WORKING_DIR), TEMP_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
