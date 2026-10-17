# Runbook: run, deploy, operate, troubleshoot

Pulseboard is four repos checked out side by side: `pulseboard-identity`, `pulseboard-api`, `pulseboard-web` and `pulseboard-platform`. Every mode below has been run end to end.

| | Local dev (no Docker) | Docker (full system) | Azure |
|---|---|---|---|
| Browser URL | http://localhost:5173 | http://localhost:8080 | Static Web Apps URL |
| Gateway | Angular dev proxy (`proxy.conf.json`) | nginx (`pulseboard-platform/gateway/nginx.conf`) | API Management, Consumption (`/identity`, `/core`) |
| Identity | `uvicorn` :8100, SQLite | container, Postgres `identity` DB | App Service (same B1 plan), Postgres `identity` DB |
| API | `uvicorn` :8000, SQLite | container, Postgres `pulseboard` DB | App Service (B1), Postgres `pulseboard` DB |
| Cache | in-memory | Redis 7 | off by default, or Azure Managed Redis with `-DeployRedis` |
| Queue | SQS on moto :5000 (mock AWS) | moto container | Azure Storage Queue (managed identity) |
| Secrets | `.env` files; dev signing key in `.keys/` | compose env; key in a named volume | Key Vault references |
| WebSocket | via dev proxy | via nginx | browser → API app directly |

**Default admin:**
- **Local and Docker:** `admin@example.com` / `Admin-Pulseboard-2026`.
- **Azure:** the `OwnerEmail` you deploy with. Its password is `adminPassword` in `pulseboard-platform/infra/secrets.local.json`.

New sign-ups are **VIEWER**. Promote them on the Users page.

---

## 1. Local dev, no Docker
```powershell
# identity
cd pulseboard-identity; python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev]"; copy .env.example .env
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8100

# api (second terminal; moto is the mock AWS queue)
cd pulseboard-api; python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev]"; copy .env.example .env
.\.venv\Scripts\python -m moto.server -p 5000        # third terminal
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000

# web (fourth terminal)
cd pulseboard-web; npm install; npm start            # http://localhost:5173
```

## 2. Docker, the whole system
```bash
cd pulseboard-platform
docker compose up --build -d      # postgres, redis, sqs, identity, api, web, gateway
docker compose ps                 # every service healthy or running
docker compose logs -f api identity
docker compose down               # add -v to also delete database volumes and the dev signing key
```
- **Through the gateway:** the app is at http://localhost:8080.
- **Ops only:** the API is also directly on http://localhost:8000. `/internal` is reachable there, and the gateway blocks it.

## 3. Azure
```powershell
az login --tenant <tenant-id>
cd pulseboard-platform
./scripts/azure-deploy.ps1 -OwnerEmail you@example.com              # infra + identity + api + web + smoke test
./scripts/azure-deploy.ps1 -OwnerEmail you@example.com -SkipInfra   # code only
./scripts/azure-stop.ps1                                            # pause billing (keeps data)
az group delete -n rg-pulseboard                                    # remove everything
```

**What `azure-deploy.ps1` does (safe to re-run):**
1. Generates secrets once into `infra/secrets.local.json`: Postgres password, service key, admin password and the RSA signing key.
2. Applies the Bicep:
   - App Service plan B1 hosting both apps
   - Postgres with two databases
   - Key Vault, Storage Queue, API Management Consumption
   - Static Web Apps, App Insights and alerts
3. Starts a paused Postgres and the apps.
4. Zip-deploys identity and api asynchronously, then waits for `/readyz`.
5. Builds the web app with `config.json` pointing at the gateway (and WebSocket at the API), and uploads it.
6. Smoke-tests an admin login and an authorized API call **through the gateway**.

**Region:** Central India. This free-trial subscription is blocked from creating Postgres in South India and East US.

## 4. CI/CD (GitHub Actions)
| Repo | `ci.yml` | `deploy.yml` |
|---|---|---|
| identity | ruff, pytest, Docker boot against Postgres (JWKS, admin login) | n/a (use the platform script) |
| api | ruff, pytest, image against Postgres, Redis and moto, `demo.py` | zip-deploy on `main` after CI, once `AZURE_*` secrets and `AZURE_WEBAPP_NAME` are set |
| web | app and library build, Docker build | Static Web Apps upload, once `API_URL`, `AUTH_URL`, `WS_URL` and the SWA token are set |
| platform | `az bicep build`, `docker compose config`, `nginx -t` | n/a |

## 5. Configuration (no hardcoded data)
| Where | What |
|---|---|
| `metrics` table | The metric catalog (UI-managed). Setup areas come from it. |
| `PB_REGIONS`, `PB_UNITS`, `PB_PAGE_SIZES`, `PB_COUNT_UNITS`, `PB_VOLUME_UNITS` | Served by `GET /api/v1/meta` |
| `ID_SIGNUP_ROLE`, `ID_ALLOW_SIGNUP`, `ID_ACCESS_TOKEN_MINUTES`, `ID_REFRESH_TOKEN_DAYS`, `ID_LOGIN_MAX_ATTEMPTS` | Identity policy |
| Role → permissions | `pulseboard-identity/app/security.py`, published at `GET /api/v1/auth/roles` |
| `/config.json` (web) | `apiUrl`, `authUrl`, `wsUrl`, written per environment at deploy time |

## 6. Troubleshooting
| Symptom | Likely cause | Fix |
|---|---|---|
| "Email or password is incorrect" for admin | Wrong environment's password | Local: `Admin-Pulseboard-2026`. Azure: `adminPassword` in `secrets.local.json`. |
| 429 "Too many failed sign-in attempts" | Lockout after 5 failures | Wait 15 minutes, or clear Redis keys `login:fail:*` |
| 401 on every API call | API can't fetch JWKS, or issuer/audience mismatch | `curl <identity>/.well-known/jwks.json`; check `PB_JWKS_URL`, `PB_JWT_ISSUER`, `PB_JWT_AUDIENCE` |
| 403 with `missing: ["metrics:write"]` | Role lacks the permission | An admin promotes the user; it applies at their next refresh (≤15 min) or sign-in |
| UI can't load its configuration | `/config.json` missing | Re-run the deploy, or check the `API_URL`/`AUTH_URL`/`WS_URL` env vars |
| CORS error in Azure | Origin not in the APIM `cors` policy | Add it in `infra/main.bicep` (`corsPolicy`) and redeploy the infrastructure |
| "Reconnecting…" in the sidebar | WebSocket blocked | Check `wsUrl`; App Service needs `webSocketsEnabled` (set by Bicep) |
| `/readyz` shows `cache: down` | Redis unreachable | The app keeps working from the database; check `PB_REDIS_URL` |
| Azure 503 right after a deploy | Remote `pip install` still running | Wait 3–5 minutes; then `az webapp log download` and check `*_default_docker.log` |
| Docker: port already allocated | Local servers still running | Stop local `uvicorn`, `moto` and `ng serve` |

## 7. Useful commands
```bash
python scripts/demo.py http://localhost:8080 local-internal-service-key http://localhost:8000     # end to end via gateway
docker compose exec postgres psql -U pulseboard -d identity -c "select email, role_cd, is_active from users"
docker compose exec redis redis-cli --scan | cut -d: -f1 | sort | uniq -c
PB_DATABASE_URL=sqlite+aiosqlite:///./_m.db alembic revision --autogenerate -m "change"   # new API migration
ID_DATABASE_URL=sqlite+aiosqlite:///./_m.db alembic revision --autogenerate -m "change"   # new identity migration
```
