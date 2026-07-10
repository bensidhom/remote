# run.ps1 - start the BridgeGuard web app (API + built React UI on one port).
#
#   .\run.ps1                 # http://localhost:8000
#   .\run.ps1 -Port 9000      # custom port
#   .\run.ps1 -Reload         # auto-reload on backend code changes (dev)
#   .\run.ps1 -Build          # rebuild the frontend first, then run
#
# On first run (or if .venv is missing) it creates the venv and installs deps.
param(
    [int]$Port = 8000,
    [switch]$Reload,
    [switch]$Build
)
$ErrorActionPreference = "Stop"
$root    = $PSScriptRoot
$backend = Join-Path $root "backend"
$venvPy  = Join-Path $backend ".venv\Scripts\python.exe"

# 1. Ensure the virtualenv + dependencies exist.
if (-not (Test-Path $venvPy)) {
    Write-Host "[run] Creating virtualenv + installing backend deps (first run only)..." -ForegroundColor Cyan
    python -m venv (Join-Path $backend ".venv")
    & $venvPy -m pip install --upgrade pip
    & $venvPy -m pip install -r (Join-Path $backend "requirements.txt")
}

# 2. Optionally (re)build the frontend into backend/static.
if ($Build) {
    Write-Host "[run] Building frontend..." -ForegroundColor Cyan
    & (Join-Path $root "build.ps1")
}
$hasStatic = Test-Path (Join-Path $backend "static")
$hasDist   = Test-Path (Join-Path $root "frontend\dist")
if ((-not $hasStatic) -and (-not $hasDist)) {
    Write-Host "[run] No frontend build found. API will run but the UI wont be served. Run .\build.ps1 or pass -Build." -ForegroundColor Yellow
}

# 3. Launch the server (serves /api + the SPA on http://localhost:$Port).
$uvArgs = @("-m", "uvicorn", "app.main:app", "--app-dir", $backend, "--port", "$Port", "--host", "0.0.0.0")
if ($Reload) { $uvArgs += "--reload" }
Write-Host "[run] Starting BridgeGuard on http://localhost:$Port  (Ctrl+C to stop)" -ForegroundColor Green
& $venvPy @uvArgs
