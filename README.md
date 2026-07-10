# BridgeGuard

BridgeGuard is an SNBI (Specifications for the National Bridge Inventory) bridge
inspection web application. In one app it provides:

- **Interactive maps** of the bridge inventory (ArcGIS / Leaflet).
- **Bridge database** browse, search, and per-bridge detail views.
- **SNBI editor** for correcting and enriching bridge records.
- **KPIs** and per-state / per-county condition dashboards.
- **Markov deterioration forecasts** and forecast-driven life-cycle cost analysis (LCCA).
- **SNBI ⇄ NBI converter** (NBI `.txt` + elements `.xml` → SNBI JSON, and reverse).
- **Bridgette** — a natural-language → SQL chat assistant over the bridge DB.
- **Document & image Q&A** over uploaded inspection reports, backed by LightRAG.

All AI features are powered by **hosted LLM APIs — there is no local model
inference** (no Ollama, torch, point-cloud, or photogrammetry code paths). This
makes the app deployable on a single modest AWS server.

---

## Architecture

A **single FastAPI application** serves everything:

- The JSON API under `/api/*` (plus `/health`).
- The **built React SPA** as static files, with a catch-all fallback to
  `index.html` so client-side routing works. Unknown `/api/*` paths return
  404 JSON instead of the SPA.

```
Browser ──▶ FastAPI (gunicorn + uvicorn worker)
              ├── /api/*   → JSON endpoints
              ├── /assets  → hashed SPA build assets
              └── /*       → index.html (React Router)
```

The frontend build directory is resolved from the `FRONTEND_DIST` env var, else
the first existing of `backend/static/` then `frontend/dist/`. If no build is
found the API still runs (SPA serving is simply skipped).

---

## LLM provider model

Two hosted, multimodal providers are switchable **at runtime** (Settings dialog,
or `POST /api/ai-provider`) and via the `BRIDGEGUARD_AI_PROVIDER` env var:

| Role                         | Provider |
|------------------------------|----------|
| **Default** text **and** image (multimodal) chat | **Cerebras** (`gemma-4-31b`) |
| Alternative text and image   | **NVIDIA / Qwen** VLM (`qwen/qwen3.5-397b-a17b`) |
| RAG knowledge-graph **completion** | **Follows the toggle** (Cerebras or NVIDIA) |
| RAG document **embeddings**  | **Always NVIDIA** (`nvidia/nv-embedqa-e5-v5`, dim 1024) |

Everything that generates text — chat, Bridgette NL→SQL, image Q&A, and the RAG
answer/entity-extraction — follows the selected provider. **Embeddings are the one
exception: they always use NVIDIA**, because Cerebras exposes no embeddings
endpoint and "no local inference" rules out a local embedding model. A vector
store is also dim-locked, so the embedding model must stay fixed. Net effect:
NVIDIA is required for document Q&A (embeddings) even when Cerebras is the selected
chat provider. Set both `CEREBRAS_API_KEY` and `NVIDIA_API_KEY` for full
functionality. Keys are read from environment variables (recommended in
production) or from `backend/.env` for local development.

Relevant endpoints:
- `GET /api/ai-provider` → `{ provider, cerebras_key_set, nvidia_key_set, message }`
- `POST /api/ai-provider` with `{ "provider": "cerebras" | "nvidia" }`

---

## Local development

### Backend

```bash
cd backend
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit .env and add your API keys
uvicorn app.main:app --reload --port 8000
```

The API is now on `http://127.0.0.1:8000` (health check at `/health`).

### Frontend

```bash
cd frontend
npm install
npm run dev
```

> **Dev proxy note:** the current `frontend/vite.config.ts` proxies `/api` to
> **port 5000** by default (`VITE_BACKEND_PORT || 5000`), while the dev server
> runs on `http://localhost:8001`. If you run the backend on 8000 (as above),
> either start the dev server with `VITE_BACKEND_PORT=8000 npm run dev`
> (or set `VITE_BACKEND_URL=http://127.0.0.1:8000`), **or** run the backend on
> port 5000 to match the default.

---

## Production build

Build the SPA and stage it into `backend/static/` so the one server serves both
the API and the UI:

```bash
# Windows
./build.ps1

# macOS / Linux
./build.sh
```

Each script runs `npm ci && npm run build` in `frontend/`, then copies
`frontend/dist/*` into `backend/static/`.

## Authentication and roles

Authentication is enforced by FastAPI, not by the React bundle. Configure a
long shared deployment password, a random session-signing secret, and the
viewer/editor/admin email lists in `backend/.env` (see `.env.example`). Sessions
use signed, HttpOnly, SameSite cookies. Database administration is restricted to
the server-issued `admin` role; browser-stored passwords and admin PINs are not
used.

For local development only, `BRIDGEGUARD_AUTH_DISABLED=1` creates an implicit
local admin session. Never set it in a deployed environment.

## Docker and OCI Always Free

The root `Dockerfile` builds the React SPA and Python service into a multi-stage,
ARM64-compatible image. Persistent data is not copied into the image; mount it
at `/data` and set `BRIDGEGUARD_DATA_DIR=/data`.

```bash
docker compose up --build
```

The recommended free MVP deployment is an OCI Ampere A1 VM. The production
Compose file, Caddy TLS proxy, online SQLite backup command, and setup guide are
in `deploy/oci/`. Keep one Gunicorn worker until SQLite, RAG state, and ingestion
jobs have moved to external services.

---

## AWS deployment (no Docker)

### Option A — Elastic Beanstalk (Python platform)

1. Run the build (`build.ps1` / `build.sh`) so `backend/static/` exists.
2. Deploy the **`backend/` directory as the application root** — it contains the
   `Procfile`, `requirements.txt`, `app/`, and the staged `static/`.
3. The `Procfile` launches:
   ```
   web: gunicorn -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:${PORT:-8000} --timeout 300 --workers 1
   ```
4. Set env properties in the EB console (Configuration → Software): at minimum
   `CEREBRAS_API_KEY`, `NVIDIA_API_KEY`, and `BRIDGEGUARD_AI_PROVIDER`. Set
   `BRIDGEGUARD_DATA_DIR` to a writable path if the app root is read-only.
   A commented starter lives in `backend/.ebextensions/01_env.config`.

### Option B — plain EC2 + nginx

```bash
git clone <repo> && cd <repo>
./build.sh                                   # build + stage the SPA
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                         # add real keys (or export env vars)
gunicorn -k uvicorn.workers.UvicornWorker app.main:app \
    --bind 0.0.0.0:8000 --timeout 300 --workers 1
```

Put nginx in front as a reverse proxy to `127.0.0.1:8000` (and to terminate TLS).
Because the app already serves the SPA, nginx only needs to proxy — no separate
static host is required.

---

## Data files

The bridge datasets are **large and are NOT committed to git**:

- `snbi_bridges.db` (~1.2 GB) — main bridge inventory.
- `snbi_forecasts.db` (~100 MB) — forecast training/prediction runs.

Place both in `backend/data/` (or wherever `BRIDGEGUARD_DATA_DIR` points). All
persistent app data — DBs, uploaded attachments, RAG storage, and temp files —
lives under that single data directory.

---

## Notes

- **ArcGIS key:** any ArcGIS API key committed for the frontend is
  referrer-locked to `localhost`. On other origins the map automatically falls
  back to free, keyless basemaps (OpenStreetMap / Esri World Imagery). To use
  premium ArcGIS basemaps elsewhere, add that origin to the key's allowed
  referrers (or supply your own key via `VITE_ARCGIS_API_KEY`).
- **Secrets:** `backend/.env` is gitignored. Never commit real API keys; prefer
  real environment variables in production.
