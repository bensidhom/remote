#Requires -Version 5
# Build the React frontend and stage it into backend/static so the single
# FastAPI app can serve the SPA alongside the /api endpoints.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==> Building frontend"
Push-Location (Join-Path $Root "frontend")
try {
    npm ci
    npm run build
} finally {
    Pop-Location
}

$Dest = Join-Path $Root "backend\static"
Write-Host "==> Staging dist -> $Dest"
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "frontend\dist\*") $Dest

Write-Host "==> Done. Frontend staged into backend/static"
