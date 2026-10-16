# Runbook: run, deploy, operate, troubleshoot

There are three ways to run Pulseboard, and each one has been run end to end with `scripts/demo.py`.

| | Local (no Docker) | Docker | Azure |
|---|---|---|---|
| UI | `ng serve` on http://localhost:5173 | nginx on http://localhost:8080 | Static Web Apps |
| API | `uvicorn` on :8000 | container on :8000 (gunicorn, 2 workers) | App Service B1 (gunicorn, 2 workers) |
| Database | SQLite `pulseboard.db` | Postgres 16 | Azure Postgres Flexible B1ms |
| Cache | in-memory (`memory`) | Redis 7 (`redis`) | off (`none`), or Azure Managed Redis with `-DeployRedis` |
| Queue | SQS on moto :5000 (mock AWS) | SQS on moto container | Azure Storage Queue (managed identity) |
| Secrets | `.env` | compose env | Key Vault references |
| Realtime across instances | single process | Postgres `LISTEN/NOTIFY` | Postgres `LISTEN/NOTIFY` |

---

## 1. Local, no Docker

```powershell
# API
cd pulseboard-api
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
.\.venv\Scripts\python -m moto.server -p 5000                          # terminal 1: mock AWS SQS
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000    # terminal 2: API

# UI
cd ..\pulseboard-web
npm install
npm start                                                              # terminal 3: http://localhost:5173
```
Sign in with any user ID and follow the setup assistant. API docs are at http://localhost:8000/docs.

## 2. Docker (the whole stack)

```bash
cd pulseboard-api
docker compose up --build -d     # postgres, redis, sqs (moto), api, web
docker compose ps                # every service should be healthy or running
docker compose logs -f api
docker compose down              # stop; add -v to also delete the database volume
```
The UI is at http://localhost:8080 and the API at http://localhost:8000/docs. Docker uses the same ports as local mode, so stop local `uvicorn` and `moto` first.

## 3. Azure

```powershell
az login --tenant <tenant-id>
./scripts/azure-deploy.ps1 -OwnerEmail you@example.com              # infra + API + UI + smoke test
./scripts/azure-deploy.ps1 -OwnerEmail you@example.com -SkipInfra   # code only
./scripts/azure-deploy.ps1 -OwnerEmail you@example.com -DeployRedis # add Azure Managed Redis (extra cost)
./scripts/azure-stop.ps1                                            # pause billing (keeps all data)
az group delete -n rg-pulseboard                                    # remove everything
```
`azure-deploy.ps1` is safe to re-run, and it also resumes after `azure-stop.ps1`:
1. It starts a stopped Postgres.
2. It re-applies the B1 tier.
3. It starts the API and deploys the code.
4. It waits for `/readyz`.
5. It builds the UI, writes `config.json` pointing at this environment's API, and uploads it.

Resources: App Service B1, Postgres B1ms (free 750 h), Storage Queue, Key Vault, App Insights with a daily cap, Static Web Apps (Free), a 5xx alert and a monthly budget alert.

**Region:** Central India. Free-trial subscriptions are blocked from creating Postgres in South India and East US.

## 4. CI/CD (GitHub Actions)

| Repo | Workflow | What it does |
|---|---|---|
| pulseboard-api | `ci.yml` | `test`: ruff + pytest. `integration`: builds the image, runs it against Postgres, Redis and moto service containers, runs `scripts/demo.py`, and asserts the cache is up. |
| pulseboard-api | `deploy.yml` | After CI passes on `main`, zip-deploys to App Service. Skipped until `AZURE_WEBAPP_NAME` and the OIDC secrets are set. |
| pulseboard-web | `ci.yml` | `npm ci`, app build, library build, Docker build |
| pulseboard-web | `deploy.yml` | Builds, writes `config.json` from the `API_URL` and `WS_URL` variables, uploads to Static Web Apps. Skipped until configured. |

**Enabling deploys:**
1. Create an Entra app registration with a federated credential for the repo.
2. Grant it Contributor on `rg-pulseboard`.
3. Add these to the API repo:
   - secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
   - variable `AZURE_WEBAPP_NAME`
4. Add these to the web repo:
   - variables `API_URL`, `WS_URL`
   - secret `AZURE_STATIC_WEB_APPS_API_TOKEN`

## 5. Configuration (no hardcoded data)

| Where | What |
|---|---|
| Database `metrics` table | The metric catalog, managed in the UI. Setup questions list its areas. |
| `PB_REGIONS`, `PB_UNITS`, `PB_PAGE_SIZES`, `PB_COUNT_UNITS`, `PB_VOLUME_UNITS` | Lists the UI shows, served by `GET /api/v1/meta` |
| `PB_CACHE_BACKEND`, `PB_REDIS_URL`, `PB_CACHE_TTL_SECONDS` | Caching (see `CACHING_AND_PERFORMANCE.md`) |
| `PB_QUEUE_BACKEND`, `PB_AWS_ENDPOINT_URL`, `PB_AZURE_QUEUE_ACCOUNT_URL` | Review queue |
| `/config.json` (web) | `apiUrl`, `wsUrl`, read by the browser when the app loads |

The full list is in `app/core/config.py`. In production the app refuses to start with the default JWT secret or service key.

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| UI says "could not load its configuration" | `/config.json` is missing | Local: run `npm start` again. Docker: check `API_URL`/`WS_URL`. Azure: re-run the deploy script. |
| Sign-in fails: "Is the API running?" | API down or wrong `apiUrl` | `curl <api>/readyz`, then check `config.json`. |
| CORS error in the browser | UI origin not in `PB_CORS_ORIGINS` | Add the origin (it's a JSON list). |
| "Reconnecting…" in the sidebar | WebSocket can't connect | Check `wsUrl`. App Service needs `webSocketsEnabled` (set in Bicep). |
| Board stuck on "Building…" | Background build failed | `GET /internal/v1/jobs` (service key) shows the error; the page also re-polls every 3 s. |
| `/readyz` shows `cache: down` | Redis unreachable | The app keeps working from the database. Check `PB_REDIS_URL` and the Redis container. |
| Reviews never run | Queue unreachable | Logs say "queue receive failed". Start moto, or check the storage account role assignment. |
| Docker: port already allocated | Local servers still running | Stop local `uvicorn` and `moto`, or change the ports in compose. |
| Azure 503 after deploy | App still installing, or a startup crash | Wait 3–5 minutes. Then: `az webapp log download`, look in `*_default_docker.log`. |
| Azure deploy: "Server is stopped" | Postgres was paused | `azure-deploy.ps1` starts it automatically. |

## 7. Useful commands

```bash
# Tests and lint
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check .

# New migration after a model change
PB_DATABASE_URL=sqlite+aiosqlite:///./_m.db alembic upgrade head
PB_DATABASE_URL=sqlite+aiosqlite:///./_m.db alembic revision --autogenerate -m "describe change"

# Inspect Docker data
docker compose exec postgres psql -U pulseboard -c "select key, area, enabled from metrics"
docker compose exec redis redis-cli --scan

# End-to-end check against any environment
python scripts/demo.py <api-url> <service-key>
```
