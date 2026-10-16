# pulseboard-api

Pulseboard turns a short conversation with a setup assistant into a personal **board** of metric **tiles**. Tiles that need attention become **alerts**. Each alert links to a **detail** board and a **briefing** board. A background **reviewer** re-checks every board on a schedule.

This API is built with FastAPI, async SQLAlchemy, Postgres or SQLite, a queue (SQS locally, Azure Storage Queue in Azure) and WebSockets.

## Flows

| Flow | How it works |
|---|---|
| **Setup** | `POST /api/v1/assistant/messages` streams each assistant turn as SSE. Each reply is saved as a preference. After the last answer, or a skip, the assistant **claims** the overview board, builds it in the background, **publishes** it, and the WebSocket sends `board.published`. |
| **Reading** | `GET /api/v1/boards/overview`, `GET /api/v1/boards/{id}`, `GET /api/v1/boards/{id}/tiles`, `GET /api/v1/tiles/{id}/data`. A tile whose query fails returns `status: ERROR` without failing the request. |
| **Follow-up** | `PATCH /api/v1/tiles/{id}/follow-up` (`SEEN` / `DONE`). The value is kept across reviews. |
| **Review** | The scheduler sends due boards to the queue. The worker then: loads the assistant profile → records an `assistant_jobs` row → claims the board → loads preferences → reads the board → queries metrics → publishes → sends `board.published`. |
| **Realtime** | `WS /ws/events?token=…`. With Postgres, events are relayed through `LISTEN/NOTIFY`, so they reach users connected to any instance. |
| **Internal** (`X-Service-Key`) | `/internal/v1/assistant-profiles/{key}`, `/preferences`, `/layouts/claim`, `/layouts/{id}/publish`, `/reviews/{id}`, `/reviews/{id}/run`, `/jobs` |
| **Ops** | `/healthz` (liveness), `/readyz` (database check), `/docs` (not served in production) |

### Safe concurrent publishing
- `claim` reserves a board for one job for a limited time and increments its **generation**.
- `publish` is accepted only from the current claim holder at the current generation. A slow or abandoned job gets `409` and cannot overwrite newer work.
- A publish writes the overview, detail and briefing boards in **one transaction**. Boards and tiles are matched by `slot_key`.
- Every publish adds one `board_versions` row per board.

### Tables
`boards`, `board_versions`, `tiles`, `assistant_profiles`, `assistant_jobs`, `user_preferences`, `conversations`, `conversation_messages`. The schema is managed by Alembic (`migrations/`).

## Run locally

Locally, AWS is mocked: SQS runs on a moto server on `localhost:5000`.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
.\.venv\Scripts\python -m moto.server -p 5000     # terminal 1: mock AWS
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000   # terminal 2
```
Or run `scripts\dev.ps1`. Then open http://localhost:8000/docs.

If moto isn't running, the API still serves requests. The review worker logs a warning and retries with backoff.

To try the Azure queue locally, run Azurite and set these two variables:
```
PB_QUEUE_BACKEND=azure
PB_AZURE_QUEUE_CONNECTION_STRING=UseDevelopmentStorage=true
```

## Test
```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check .
```

## Deploy to Azure (low cost)

```powershell
az login
./scripts/azure-deploy.ps1 -OwnerEmail you@example.com            # rg-pulseboard in centralindia
```

| Resource | Tier | Why |
|---|---|---|
| App Service (Linux) | B1 | Cheapest tier with Always On, which the background worker needs. Supports WebSockets. |
| PostgreSQL Flexible Server | B1ms, 32 GB | Covered by the Azure free account's 750 hours/month |
| Storage account | Standard LRS | Review queue. Managed identity only; shared keys are disabled. |
| Key Vault | Standard, RBAC | Holds the database URL, JWT secret and service key, read through Key Vault references. |
| Application Insights + Log Analytics | 0.15 GB/day cap | Stays within the free ingestion allowance |
| Alerts | 5xx metric alert, monthly budget | Emails `OwnerEmail` |

Generated secrets are saved in `infra/secrets.local.json`, which is git-ignored, so re-runs reuse them.
- To redeploy code only: `./scripts/azure-deploy.ps1 -OwnerEmail … -SkipInfra`
- To remove everything: `az group delete -n rg-pulseboard`

**CI/CD:** `.github/workflows/ci.yml` runs lint and tests on every push. `deploy.yml` deploys `main` to App Service after CI passes. It signs in to Azure with OIDC and needs:
- secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
- variable: `AZURE_WEBAPP_NAME`

### To scale up later
- Raise `appServiceSku` to `S1` or `P0v3` to unlock autoscale and deployment slots.
- Put API Management in front of the API.
- Move Postgres behind VNet integration with private access.
- Replace demo sign-in (`PB_ENABLE_DEV_TOKEN=false`) with Entra ID.

## Configuration

Every setting comes from a `PB_*` environment variable; see `app/core/config.py` and `.env.example`. In production the app refuses to start if it would use the default JWT secret or service key.

## Version 2
- **Metric catalog:** stored in the database, with full CRUD, search, sort and pagination at `/api/v1/metrics`.
- **Browsing:** `/api/v1/meta`, `/api/v1/boards` and `/api/v1/jobs`.
- **Redis cache:** set `PB_REDIS_URL` to use a real Redis. Without it, an in-process fake Redis is used.
- **Docker:** run the whole stack with `docker compose up --build`. The UI is at http://localhost:8080 and the API at http://localhost:8000.
- **Details:** see `docs/PROJECT_GUIDE.md` section 12.
