# Pulseboard API: Backend Architecture

> **Current architecture (version 3):** Pulseboard is now four repos: `pulseboard-identity` (accounts, RS256 tokens, JWKS, RBAC), `pulseboard-api`, `pulseboard-web` and `pulseboard-platform` (API gateway, Compose, Azure Bicep, deploy scripts).
> - **Sign-in:** the demo token endpoint is gone. Users register or sign in with email and password through the gateway, and the API verifies RS256 tokens via JWKS (no shared HS256 secret).
> - **Where to read more:** [MICROSERVICES.md](https://github.com/Amarnath123122/pulseboard-platform/blob/main/docs/MICROSERVICES.md) for services, gateway, auth/RBAC and workflow; [RUNBOOK.md](RUNBOOK.md) for commands.
> - **Older sections:** where they mention `/auth/dev-token`, `PB_JWT_SECRET`/HS256, or Compose and Azure scripts inside `pulseboard-api`, read them as superseded by the above.


*How the backend works, and why each part is there.*

---

## 1. What the product needs

Pulseboard gives each user a **personal metrics dashboard built by an assistant**, and keeps that dashboard current without anyone touching it.

That one sentence gives the backend five jobs:

| # | Product promise | What the backend has to do |
|---|---|---|
| 1 | "Tell me what you care about" | A conversation with state, streamed to the browser |
| 2 | "I'll build your board" | Slow work done in the background, while the UI shows it building |
| 3 | "I'll keep watching" | Reviews on a schedule that survive crashes, restarts and scale-out |
| 4 | "You'll see changes live" | Push updates to the browser on any server instance |
| 5 | "One broken source won't break the board" | Each tile fails on its own |

Every component below exists to serve one of these jobs. Anything that didn't would be overhead.

---

## 2. System overview

```
                ┌──────────────────────── Browser (pulseboard-web) ────────────────────────┐
                │  REST (JWT)          SSE stream (assistant)          WebSocket (events)  │
                └─────┬──────────────────────┬──────────────────────────────┬──────────────┘
                      │                      │                              │
┌─────────────────────▼──────────────────────▼──────────────────────────────▼──────────────┐
│ FastAPI app  (gunicorn + N uvicorn workers, behind a load balancer)                      │
│                                                                                          │
│  api/public.py   api/realtime.py   api/internal.py (X-Service-Key)   api/health.py       │
│        │                │                     │                                          │
│  assistants/setup.py   services/events.py   assistants/reviewer.py ◄── services/workers  │
│        └───────┬────────────────────────────────────┘                    ▲      │        │
│          assistants/planner.py ──► services/metrics.py (mock warehouse)  │      │        │
│                │                                                         │      │        │
│          services/layout.py  (claim → publish, the single write path)    │      │        │
└────────────────┬─────────────────────────────────────────────────────────┼──────┼────────┘
                 │                                                         │      │
        ┌────────▼─────────┐       LISTEN/NOTIFY                ┌──────────┴──────▼───┐
        │ SQLite (local)   │◄──── cross-instance events ───────►│ Review queue         │
        │ Postgres (prod)  │                                    │ SQS / Azure Storage  │
        └──────────────────┘                                    └──────────────────────┘
```

**Layers** (requests only call downward):

1. **`api/`**: HTTP edge. Auth, input validation, ownership checks, response shapes. No business logic.
2. **`assistants/`**: the "agents". They coordinate a whole workflow (setup chat, periodic review).
3. **`services/`**: building blocks the agents use: layout, metrics, preferences, queue, events, workers.
4. **`db/`**: models, sessions, seed data. Alembic migrations sit beside it.
5. **`core/`**: config, logging, errors, security. Everything else depends on these.

**Why it's layered this way:** the assistants are written as rule engines today (`model: rules-engine-v1`). The plan is to swap them for LLM agents later. Their seeded `tools` list (`save_preference`, `claim_layout`, `publish_layout`, `run_query`, …) maps one-to-one onto the service functions and the `/internal/v1` endpoints. Swapping in a real model then means **replacing the planner, not rebuilding the system**.

---

## 3. The core idea: claim → publish

[services/layout.py](../app/services/layout.py) is the most important file in the backend.

### The problem
Several writers can rebuild the same board:
- the setup assistant (when the user finishes onboarding),
- the scheduled reviewer (every hour),
- an operator triggering a review by hand,
- any of these running on **different server instances**, possibly **retried** by the queue.

Without coordination you get lost updates. Worse, a slow job that started first can finish last and overwrite a newer board with stale data.

### The solution: a lease plus a fencing token
```
claim(user)  →  sets claim_holder, claim_expires_at (now + 120s), generation += 1
publish(board, claim_holder, generation, plan)
             →  accepted only if holder AND generation still match AND lease not expired
```

- **Lease (`claim_expires_at`)**: if a worker crashes, its claim expires on its own. Nobody has to clean up.
- **Fencing token (`generation`)**: a zombie job that wakes up after its lease ran out has an old generation number, so its publish is **rejected with 409**. This is the same idea as fencing tokens in distributed locking (Kleppmann). A plain lock isn't enough.
- **`SELECT … FOR UPDATE`** on the root board makes claim and publish serialise on Postgres.
- **One transaction per family**: the overview plus every detail and briefing board commits together. Users never see half a board.
- **Matching by slot key** (`alert:sales.bookings`, `detail:…`): boards keep their IDs across rebuilds, so links and bookmarks keep working. The user's follow-up state (`PENDING/SEEN/DONE`) carries over unless the plan sets it explicitly.
- **`board_versions` snapshots**: an audit log of every publish, linked to the `job_id` that produced it. You can answer "why does my board look like this?" and roll back if needed.
- **`release()` on failure**: the last good layout stays `LIVE`. Only a first-ever build can end up `FAILED`.

> **Why it matters:** this gives *at-least-once* job delivery while publishes behave as if they ran *exactly once*. The queue can retry as often as it likes and correctness doesn't depend on it.

---

## 4. Workflows end to end

### 4.1 Onboarding (setup assistant): SSE + background build
[assistants/setup.py](../app/assistants/setup.py), endpoint `POST /api/v1/assistant/messages`

1. The browser posts `{action: start|reply|skip, text}`.
2. The response is **`text/event-stream`**, which sends `conversation`, `delta` (word by word), `board` and `turn.end` events.
   - *Why SSE and not WebSocket here:* each chat turn is one request with one streamed answer. SSE is plain HTTP and works with auth headers, proxies and gzip. `X-Accel-Buffering: no` stops nginx-style proxies from holding the stream back.
   - *Why stream at all:* it's the UX people expect from an assistant. Wiring it in now means a real LLM's token stream can plug straight in later.
3. Each answer is saved as a **Preference** (`LAYOUT` / `WATCHLIST` scope). The conversation step lives in `Conversation.state`, so the server keeps no state between requests and any instance can handle the next turn.
4. On the last answer: write an `AssistantJob` → `claim()` → reply "Building your board now" → start `_build()` as a background task.
5. `_build` → `planner.build_plan()` → `layout.publish()` → a `board.published` event reaches the user's WebSocket.

*Why build in the background and not inside the request:* plan generation is slow (and will be slower with a real model). Holding the HTTP request open would tie up a worker and risk proxy timeouts. Also, the board shows `BUILDING` the moment `claim()` runs, so the UI has an honest state to display.

Validation happens **before** the stream opens (the conversation ownership check in `public.py`), so bad requests get a real 404 and not an error buried inside a 200 stream.

### 4.2 Keeping boards fresh (scheduler + queue + worker)
[services/workers.py](../app/services/workers.py), [assistants/reviewer.py](../app/assistants/reviewer.py)

```
review_scheduler (every 30s) ── finds boards with next_review_at <= now
        │   SELECT … FOR UPDATE SKIP LOCKED   ← many instances, no double-scheduling
        ▼
   Queue (SQS / Azure Storage Queue)          ← durable buffer, retries, visibility timeout
        ▼
review_worker ── review_board() ── claim → plan → publish ── delete message
```

Why each piece is there:

- **`next_review_at` in the database is the schedule.** No cron daemon, no extra state to lose. The scheduler clears it after enqueueing and `publish()` sets it again, so a board can't pile up repeat reviews.
- **`SKIP LOCKED`**: every instance runs the scheduler and they still never pick the same board. It scales out with zero coordination.
- **A real queue rather than calling the reviewer directly:** it separates *deciding* what to review from *doing* the review, smooths out load spikes, and retries for free. A worker that crashes mid-review leaves its message unacknowledged, so after the visibility timeout (300s) someone else picks it up.
- **Poison-message guard**: after `max_delivery_attempts` (5) the message is dropped and logged. One bad board can't block the queue forever.
- **Backoff on queue outages**: workers keep running and recover by themselves when the queue comes back.
- **A claim conflict counts as `SKIPPED`, not failed:** if the user is in setup while a review comes due, the review steps aside.
- **A `ReviewQueue` Protocol with two adapters:** AWS SQS (moto locally) and Azure Storage Queues (managed identity). The business code doesn't know which cloud it runs on.
- **`asyncio.to_thread`** around boto3/azure SDK calls, because those SDKs block and must not stall the event loop.

### 4.3 Reading a board: one query per tile
`GET /boards/overview` → `GET /boards/{id}/tiles` → `GET /tiles/{id}/data` (one per tile)

- Tiles store a **`query_spec`** (`{metric, op, params}`), not the numbers themselves. Data stays fresh on every read and the layout stays small.
- **Failures are isolated:** a source error comes back as `200 {status: "ERROR"}` for that one tile. The other tiles render. (Job #5 in the table.)
- **Ownership returns 404, not 403**, so a caller can't find out whether another user's board ID exists.

### 4.4 Live updates across instances
[services/events.py](../app/services/events.py), [api/realtime.py](../app/api/realtime.py)

- `/ws/events?token=…` keeps one socket per tab. The token is in the query string because browsers can't set headers on a WebSocket upgrade. Ping/pong keeps idle proxies from dropping the socket.
- **The problem:** a review publishes on instance A, but the user's socket is held by instance B.
- **The fix:** on Postgres, `publish()` calls `pg_notify`. Every instance `LISTEN`s and delivers to the sockets it holds locally. With SQLite (one process) delivery is in-process.
- *Why Postgres NOTIFY and not Redis pub/sub:* it's **one less piece of infrastructure**. The database is already there and the event rate is low (a few per board per hour). Move to Redis or Azure Web PubSub only when connection counts or throughput call for it.
- If `pg_notify` fails, the event is delivered locally rather than dropped. A dead socket is removed without breaking the publisher.

---

## 5. Data model, and why it's shaped this way

| Table | Purpose | Key design choice |
|---|---|---|
| `boards` | Overview / Detail / Briefing pages | Self-referencing `parent_board_id` (a board family). `UNIQUE(parent, slot_key)` gives stable identity. The claim columns live on the row itself, so no separate lock table is needed. |
| `tiles` | Widgets on a board | `query_spec` + `presentation` as JSON. Tiers (`ALERT/HEADLINE/CONTEXT`) set the order. Links to drill-down boards. |
| `board_versions` | Immutable publish history | Snapshot plus the `job_id` that wrote it, for audit and rollback. |
| `user_preferences` | What the assistant knows about the user | Upsert on `(user, scope, key)`. `origin_cd` records who learned it (SETUP / REVIEW / USER). |
| `assistant_profiles` | Registry of agents (model, instructions, tools) | Agents are configured as **data**, not code, so you can change models or prompts or turn an agent off without a deploy. |
| `assistant_jobs` | Every agent run | Status, error, enqueued / started / finished timestamps, for queue-latency metrics and debugging. |
| `conversations`, `conversation_messages` | Chat state + transcript | The server stays stateless and the chat can be replayed. |

Conventions: UUID string keys (safe to generate on any instance), `*_cd` code columns, timezone-aware UTC everywhere (`as_utc` covers SQLite dropping tzinfo), and JSON columns for parts of the schema that change shape over time (plans, specs).

---

## 6. Cross-cutting concerns: what "production-ready" means here

### Configuration ([core/config.py](../app/core/config.py))
- Typed `pydantic-settings`, every value overridable with `PB_*` env vars (12-factor).
- **Fail-fast guard:** the app won't start in `production` with the default JWT secret or service key.
- `/docs` is turned off in production.

### Security ([core/security.py](../app/core/security.py), [api/internal.py](../app/api/internal.py))
- **Two trust zones:**
  - `/api/v1/*`: browser users, JWT bearer tokens (HS256, issuer checked, 8h TTL).
  - `/internal/v1/*`: service-to-service and agent tools, `X-Service-Key` compared with `secrets.compare_digest` (no timing leak), not exposed through the gateway.
- `dev-token` is a demo sign-in behind a flag. It's a deliberate seam for plugging in a real IdP (Entra ID / Auth0) later.
- CORS allow-list, plus `nosniff`, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer` on every response.
- The container runs as a non-root user (`uid 10001`).

### Observability ([core/logging.py](../app/core/logging.py), [main.py](../app/main.py))
- **A request ID on every request**, taken from the caller's `X-Request-ID` or generated, held in a `contextvar`, put on every log line and every error body, and returned in a response header. One ID traces a request from browser to log.
- Structured JSON logs (method, path, status, ms).
- Azure Application Insights via OpenTelemetry, switched on only when its connection string is set. No vendor lock-in in the code.

### Errors ([core/errors.py](../app/core/errors.py))
- One typed hierarchy (`NotFound`, `Conflict`, `Unauthorized`, …) and **one error shape**:
  `{"error": {"code", "message", "details", "request_id"}}`.
  The frontend handles errors one way. Unexpected exceptions are logged with stack traces and return a generic 500, so internals don't leak.

### Health
- `/healthz` = liveness (is the process up?). `/readyz` = readiness (can it reach the DB? returns 503 if not).
  They're separate so the orchestrator **restarts** a dead process but only **stops routing traffic** to one that can't reach the database.

### Lifecycle (`lifespan` in main.py)
- On startup: init DB, seed agent profiles (safe when several workers race), start relay, worker and scheduler tasks.
- On shutdown: signal `stop`, wait for the background loops, **drain in-flight setup builds**, dispose of the pool. Deploys don't leave half-built boards behind.

### Database portability
- SQLite locally (WAL mode, foreign keys on, zero setup), Postgres in production (pool 10+20, `pool_pre_ping`, recycle 30 min). The code is the same and the URL picks the database.
- Locally the tables are created automatically. Deployed environments run **`alembic upgrade head`** in `startup.sh`, so schema changes are versioned and reviewable.

### Runtime and delivery
- `gunicorn` (process manager, graceful restarts) + `uvicorn` workers (async I/O). `--forwarded-allow-ips` because it runs behind App Service's proxy. Keep-alive is 75s, longer than the LB idle timeout.
- GZip for responses over 1 KB.
- Slim Docker image with a HEALTHCHECK. Bicep IaC in `infra/`. GitHub Actions for CI (ruff, mypy, pytest with moto) and deploy.

---

## 7. Mock boundaries (on purpose)

| Mock | Stands in for | Why it's safe to swap |
|---|---|---|
| `services/metrics.py` | Data warehouse / semantic layer | Same four operations (`stat`, `series`, `by_region`, `projection`) + `describe_metric`. Values are seeded from a hash of (metric, region, hour), so reads within an hour agree and tests are repeatable. |
| `assistants/planner.py` | LLM reasoning | Input: preferences. Output: a typed `LayoutPlan` (Pydantic). A model only has to produce valid JSON for that schema. |
| `dev-token` | Identity provider | Only `current_principal` needs to change. |
| moto | AWS SQS | Same boto3 calls. |

The pattern: **put the expensive or external dependency behind a narrow, typed contract**, build the rest of the system for real, and swap the mock out later.

---

## 8. Trade-offs and next steps

| Decision | Benefit | Cost / when to revisit |
|---|---|---|
| Workers run inside the web process | One deployable, cheap | Heavy reviews compete with API latency. Split into a separate worker app once review volume grows (it's already just a flag: `PB_RUN_BACKGROUND_WORKERS`). |
| Setup build as an in-process `asyncio` task | Instant, no queue hop | Lost if the instance dies mid-build (the claim expires, so it's safe but not retried). Route it through the queue for durability. |
| Postgres NOTIFY for fan-out | No Redis | 8 KB payload limit, and each instance holds one LISTEN connection. Revisit at large socket counts. |
| Tile data fetched per tile | Failure isolation, fresh data | N+1 requests per board. Add a batch endpoint or a short cache in front of the real warehouse. |
| HS256 shared secret | Simple | Use RS256/JWKS from an IdP for multi-service setups. |
| No rate limiting | – | Add it at the gateway (APIM / Front Door) before going public. |

---

## 9. Summary

The backend is built around one guarantee: **a user's board is always complete, never overwritten by stale data, and always current**, even with many instances, queue retries, crashing workers and slow assistants all running at once.

- Claim/publish with **generation fencing** gives correctness.
- **Queue + `SKIP LOCKED` scheduler** gives reliable, horizontally scalable freshness.
- **SSE + WebSocket + pg_notify** gives a live UX on any instance.
- **Per-tile queries** give resilience.
- **Typed contracts at the mock boundaries** mean the rule engine can become a real AI agent without a rewrite.

Everything else (config guards, request IDs, health probes, graceful drain, migrations, IaC) is what makes it safe to run in production.
