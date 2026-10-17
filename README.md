# pulseboard-api

The Pulseboard domain service:
- boards, tiles and the metric catalog
- the setup assistant, with replies streamed over SSE
- scheduled reviews, run through a queue
- live updates over WebSocket
- a Redis cache

It's one of four repos:

| Repo | Role |
|---|---|
| [pulseboard-identity](https://github.com/Amarnath123122/pulseboard-identity) | Accounts, RS256 tokens and JWKS, refresh rotation, RBAC |
| **pulseboard-api** (this repo) | Domain logic. **Verifies** identity tokens and authorizes each route by permission. |
| [pulseboard-web](https://github.com/Amarnath123122/pulseboard-web) | Angular 17 monorepo UI |
| [pulseboard-platform](https://github.com/Amarnath123122/pulseboard-platform) | API gateway, Docker Compose for the full system, Azure Bicep, deploy scripts, system docs, live evidence |

## What it does
1. **Setup:** the assistant asks three questions (team, areas, region) over `POST /api/v1/assistant/messages`, streamed as SSE. It then **claims** the user's overview board, builds it in the background, and **publishes** it. The browser hears `board.published` on `/ws/events`.
2. **Boards:** overview, detail and briefing boards with tiles. Alerts come first. `GET /api/v1/boards/{id}/tile-data` returns every tile's data in one call.
3. **Metric catalog:** full CRUD with search, filters, sorting and pagination. It's stored in the database and editable in the UI.
4. **Reviews:** a scheduler queues due boards (SQS via moto locally, Azure Storage Queue in Azure). The worker re-plans and republishes each board, keeping the user's follow-ups.
5. **Safety:** publishing is fenced by claim, generation and one transaction per publish. Redis is used cache-aside with targeted invalidation. Tests guard against N+1 queries.

## Security model
- **Tokens:** the API holds **no passwords or signing keys**. It verifies RS256 access tokens from `pulseboard-identity` against its JWKS (`PB_JWKS_URL`, cached and refetched when an unknown key id appears), checking `iss`, `aud`, `exp` and `typ`.
- **Permissions:** each route requires a permission from the token's `perms` claim, for example `require("metrics:write")`. A missing permission gets **403**, with the missing permission named in the error.
- **Ownership:** boards belong to the token's `sub`. Another user's board returns **404**.
- **Internal endpoints:** `/internal/v1/*` needs `X-Service-Key`, and no gateway exposes it.

| Permission | Routes |
|---|---|
| `boards:read` | boards, tiles, tile data, meta, board list |
| `boards:follow_up` | `PATCH /api/v1/tiles/{id}/follow-up` |
| `setup:run` | `POST /api/v1/assistant/messages` |
| `jobs:read` | `GET /api/v1/jobs` |
| `metrics:read` / `metrics:write` | catalog reads / create, update, delete |

## Run
**The whole system (recommended):** use `pulseboard-platform`:
```bash
cd ../pulseboard-platform && docker compose up --build -d     # http://localhost:8080
```

**This service alone:**
```powershell
python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
.\.venv\Scripts\python -m moto.server -p 5000                          # mock AWS SQS
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000    # needs identity on :8100 for JWKS
```
API docs are at http://localhost:8000/docs.

## Test
```powershell
.\.venv\Scripts\python -m pytest -q      # tests mint identity-style RS256 tokens with a throwaway key
.\.venv\Scripts\python -m ruff check .
python scripts/demo.py <gateway-url> <service-key> <api-url>   # end-to-end walkthrough
```

## CI/CD
- **`ci.yml`:**
  - `test`: ruff and pytest.
  - `integration`: builds the Docker image, runs it against Postgres, Redis and moto with a minted token, then runs `demo.py`.
- **`deploy.yml`:** zip-deploys to App Service after CI passes on `main`, once the Azure secrets are set.
- **Full-system deploy:** `pulseboard-platform/scripts/azure-deploy.ps1`.

## Configuration (`PB_*`)
| Setting | Purpose |
|---|---|
| `DATABASE_URL` | SQLite locally, Postgres in Docker and Azure |
| `JWKS_URL` or `JWT_PUBLIC_KEY_B64` | Token verification: the identity JWKS, or a pinned key |
| `JWT_ISSUER`, `JWT_AUDIENCE` | `pulseboard-identity`, `pulseboard` |
| `CACHE_BACKEND`, `REDIS_URL` | `redis`, `memory` or `none` |
| `QUEUE_BACKEND` | `sqs` (moto locally) or `azure` |
| `REGIONS`, `UNITS`, `PAGE_SIZES` | Data shown in the UI, served by `/api/v1/meta` |
| `SERVICE_KEY` | Protects `/internal/v1/*` |

## Documentation
| Doc | What's in it |
|---|---|
| [docs/PROJECT_GUIDE.md](docs/PROJECT_GUIDE.md) | Flows, data model, API, a code tour, real demo output |
| [docs/CACHING_AND_PERFORMANCE.md](docs/CACHING_AND_PERFORMANCE.md) | Redis keys, TTLs, invalidation, the N+1 fixes |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Run, deploy, operate, troubleshoot |
| [pulseboard-platform/docs/MICROSERVICES.md](https://github.com/Amarnath123122/pulseboard-platform/blob/main/docs/MICROSERVICES.md) | Services, gateway, auth and RBAC, reliability, cost, the end-to-end workflow |
| [pulseboard-platform/docs/EVIDENCE.md](https://github.com/Amarnath123122/pulseboard-platform/blob/main/docs/EVIDENCE.md) | Captured proof from CI, Docker and the live Azure deployment |
