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

Text and vision generation use the configured **Cerebras** endpoint. Embeddings
use a private Hugging Face Text Embeddings Inference (TEI) sidecar running the
exact pinned `nomic-ai/nomic-embed-text-v1.5` revision at 768 dimensions—no
Ollama or NVIDIA API is required. Point-cloud cleaning and segmentation use
bounded CPU geometry plus Cerebras vision judgment.

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

## AI and embedding model

Generation and embedding are deliberately separate:

| Role                         | Provider |
|------------------------------|----------|
| Text, Bridgette, RAG completion, and image/point-cloud judgment | **Cerebras** (`CEREBRAS_MODEL`) |
| Document and query embeddings | **Nomic v1.5 via TEI**, revision `20d1e6057a2e1fa08b45123e41ce9381fc338f0e`, normalized 768-D |

### Embedding profiles and migrations

The supplied Compose files start TEI internally:

```env
TEI_EMBED_BASE_URL=http://embeddings:80
TEI_EMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
TEI_EMBED_REVISION=20d1e6057a2e1fa08b45123e41ce9381fc338f0e
TEI_EMBED_DIM=768
TEI_RUNTIME_VERSION=1.9.3
```

The revision is intentionally pinned. A later config-only Hub snapshot adds
legacy position aliases that TEI 1.9.3 reads as duplicate fields; the pinned
snapshot uses the same Nomic v1.5 weights and tokenizer with a TEI-compatible
config. The revision remains part of the persisted embedding profile, so older
or unprofiled vectors are re-embedded while parsed chunks are reused.

Nomic uses `search_document:` for indexed passages and `search_query:` for
questions. Those prefixes, normalization, model revision, TEI runtime version,
and dimension are part of the persisted embedding profile. Existing PDFs, parsed chunks, image
descriptions, page renders, and point-cloud analyses are reusable. Existing
Ollama/GGUF or other-provider vector matrices are not: the server removes those
vectors and re-embeds the preserved chunks once with TEI. Startup performs that
re-index sequentially in the background by default
(`BRIDGEGUARD_REINDEX_STALE_REPORTS=1`).

To merge a desktop data directory without replacing the server database:

```bash
python backend/scripts/migrate_local_artifacts.py /path/to/local/data backend/data
```

`GET /api/ai-provider` remains as a read-only compatibility/status endpoint.

---

## Point-cloud processing

The server point-cloud path is deliberately narrower than the desktop GPU
runtime. NumPy/SciPy geometry removes outliers, segments structural regions,
fits bridge surfaces, and computes clearance. Rendered candidate regions are
sent to Cerebras for scene judgment and component naming. Local vision-model
inference, Open3D, and photogrammetry are not required by the server deployment.

Run one point-cloud job at a time. A single Gunicorn worker does not by itself
prevent overlapping `asyncio.to_thread` jobs, and large scans can outlive normal
reverse-proxy request timeouts. Use a bounded background-job queue/status
endpoint before enabling concurrent users. The production x86_64 Python 3.12
image installs the published `pye57` wheel, so E57, NPZ, LAS, PLY, XYZ, and CSV
inputs can use the server parsing path.

The budget production baseline is 2 vCPUs, 12 GB RAM, and a 50 GB or larger
persistent disk. Keep point-cloud work serialized at that size. Increase memory
and storage before enabling concurrent scans or retaining a much larger archive.

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
docker compose up -d embeddings
# For a host-run backend, override the Compose-internal TEI hostname:
TEI_EMBED_BASE_URL=http://127.0.0.1:8080 uvicorn app.main:app --reload --port 8000
```

In PowerShell, set `$env:TEI_EMBED_BASE_URL='http://127.0.0.1:8080'` before the
`uvicorn` command. The root development Compose binds TEI only to loopback; the
OCI production Compose keeps it private to the Docker network.

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

The Docker image performs the frontend build in its own build stage, so do not
commit `frontend/dist` or `backend/static`. Before deployment, GitHub Actions
runs the frontend production build, backend tests, deployment-configuration
checks, and a complete production container build. The OCI deployment workflow
is manual, uses the protected `production` GitHub Environment, and deploys the
exact commit that passed CI.

Configure these GitHub Environment secrets before running it:

```text
OCI_HOST
OCI_KNOWN_HOSTS
OCI_USER
OCI_SSH_KEY
```

The server keeps API keys and Cloudflare settings in
`/etc/bridgeguard/*.env`; they are never copied into GitHub.

## Authentication and roles

The public landing is served at `/`; it contains the BridgeGuard logo and a
single link to the protected workspace at `/app`. The landing does not request
bridge data. Authentication is enforced by FastAPI on every `/api/*` request,
not by the React bundle.

For the recommended Cloudflare deployment, protect `/app`, `/app/*`, and
`/api/*` in one self-hosted Access application while leaving `/` public. Set
`BRIDGEGUARD_AUTH_MODE=cloudflare`, configure the Access team domain and AUD,
and set `BRIDGEGUARD_OWNER_EMAIL` to the owner's actual email. The five approved
editor accounts are listed in `backend/.env.example`. Keep the viewer email and
domain allowlists empty to prevent additional accounts from entering.
Cloudflare supplies the login/SSO experience; the in-app email/password form is
only for optional `password` mode.

Database administration is restricted to the server-issued `admin` role;
browser-stored passwords and admin PINs are not used.

For local development only, `BRIDGEGUARD_AUTH_DISABLED=1` creates an implicit
local admin session. Never set it in a deployed environment.

## Docker and OCI E4

The root `Dockerfile` builds the React SPA and Python service into a multi-stage
image. The supplied Compose deployment targets Linux x86_64 so it matches the
OCI AMD E4 host and pinned TEI CPU image. Persistent data is not copied into the
image; mount it at `/data` and set `BRIDGEGUARD_DATA_DIR=/data`.

```bash
docker compose up --build
```

The recommended budget deployment is an OCI `VM.Standard.E4.Flex` VM with 1
OCPU (2 vCPUs) and 12 GB RAM behind Cloudflare Tunnel and Access. The
replacement-instance procedure, data migration, production Compose file,
checksum-verifying backup command, CI/CD cutover, and rollback guide are in
`deploy/oci/`. Keep one Gunicorn worker until SQLite, RAG state, and ingestion
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
   `CEREBRAS_API_KEY` and the reachable `TEI_EMBED_BASE_URL`. Set
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
