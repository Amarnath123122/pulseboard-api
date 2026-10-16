# Local dev: mock AWS (moto server on :5000) + API with reload on :8000. Run from the repo root.
$ErrorActionPreference = "Stop"
if (-not (Test-Path .venv)) { python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev]" }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
Start-Process -WindowStyle Minimized .\.venv\Scripts\python.exe -ArgumentList "-m","moto.server","-p","5000"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
