# pulseboard-api

The domain microservice: boards, metric catalog, setup assistant (SSE), scheduled reviews (queue), live updates (WebSocket), Redis cache. It verifies the identity service's tokens and enforces role permissions on every route.

**Everything about Pulseboard (architecture, how to run, deploy, test, troubleshoot and the evidence it works) is in one guide:**

➡️ **[pulseboard-platform/README.md](https://github.com/Amarnath123122/pulseboard-platform#readme)**

## Quick start for this repo
```
python -m venv .venv && .venv/Scripts/python -m pip install -e .[dev]
.venv/Scripts/python -m pytest -q                              # 20 tests
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```
