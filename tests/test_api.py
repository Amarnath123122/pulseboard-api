import json

import httpx
from moto import mock_aws

from app.assistants.setup import drain
from tests.conftest import SERVICE, sign_in


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((fields["event"], json.loads(fields["data"])))
    return out


async def run_setup(client: httpx.AsyncClient, headers: dict, replies: list[str] | None) -> str:
    r = await client.post("/api/v1/assistant/messages", json={"action": "start"}, headers=headers)
    convo_id = parse_sse(r.text)[0][1]["conversation_id"]
    if replies is None:
        r = await client.post("/api/v1/assistant/messages", json={"conversation_id": convo_id, "action": "skip"},
                              headers=headers)
    for text in replies or []:
        r = await client.post("/api/v1/assistant/messages", json={"conversation_id": convo_id, "text": text},
                              headers=headers)
    board = next(d for e, d in parse_sse(r.text) if e == "board")
    await drain()
    return board["board_id"]


async def test_health(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).status_code == 200


async def test_requires_auth(client):
    r = await client.get("/api/v1/boards/overview")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


async def test_setup_builds_overview_and_reads_work(client):
    h = await sign_in(client)
    board_id = await run_setup(client, h, ["Customer Success", "customers and engineering", "EMEA"])

    board = (await client.get(f"/api/v1/boards/{board_id}", headers=h)).json()
    assert board["build_status_cd"] == "LIVE" and board["origin_cd"] == "SETUP" and board["version"] == 1
    assert board["settings"]["region"] == "EMEA"
    assert set(board["settings"]["areas"]) == {"customers", "engineering"}

    tiles = (await client.get(f"/api/v1/boards/{board_id}/tiles", headers=h)).json()
    assert tiles and [t["position"] for t in tiles] == sorted(t["position"] for t in tiles)
    for t in tiles:
        assert (await client.get(f"/api/v1/tiles/{t['id']}/data", headers=h)).json()["status"] == "OK"
    for t in (t for t in tiles if t["tier_cd"] == "ALERT"):
        assert t["detail_board_id"] and t["briefing_board_id"] and t["follow_up_cd"] == "NONE"
        detail = (await client.get(f"/api/v1/boards/{t['detail_board_id']}/tiles", headers=h)).json()
        assert len(detail) == 3


async def test_skip_uses_default_template(client):
    h = await sign_in(client, "U2")
    board_id = await run_setup(client, h, None)
    board = (await client.get("/api/v1/boards/overview", headers=h)).json()
    assert board["id"] == board_id and board["origin_cd"] == "DEFAULT_TEMPLATE"


async def test_users_cannot_read_each_others_boards(client):
    board_id = await run_setup(client, await sign_in(client, "U3"), None)
    r = await client.get(f"/api/v1/boards/{board_id}", headers=await sign_in(client, "U4"))
    assert r.status_code == 404


async def test_generation_rejects_stale_publish(client):
    req = {"user_id": "U9", "origin": "SETUP", "claim_holder": "job-a"}
    first = (await client.post("/internal/v1/layouts/claim", json=req, headers=SERVICE)).json()
    blocked = await client.post("/internal/v1/layouts/claim", json={**req, "claim_holder": "job-b"}, headers=SERVICE)
    assert blocked.status_code == 409
    second = (await client.post("/internal/v1/layouts/claim", json=req, headers=SERVICE)).json()
    assert second["generation"] == first["generation"] + 1

    plan = {"overview": {"slot_key": "overview", "kind": "OVERVIEW", "title": "Overview", "tiles": []}}
    url = f"/internal/v1/layouts/{first['board_id']}/publish"
    stale = {"claim_holder": "job-a", "generation": first["generation"], "plan": plan}
    assert (await client.post(url, json=stale, headers=SERVICE)).status_code == 409
    fresh = await client.post(url, json={**stale, "generation": second["generation"]}, headers=SERVICE)
    assert fresh.status_code == 200 and fresh.json()["build_status_cd"] == "LIVE"


async def test_review_keeps_follow_up_and_bumps_version(client):
    h = await sign_in(client, "U5")
    board_id = await run_setup(client, h, None)
    target = (await client.get(f"/api/v1/boards/{board_id}/tiles", headers=h)).json()[0]
    r = await client.patch(f"/api/v1/tiles/{target['id']}/follow-up", json={"follow_up_cd": "SEEN"}, headers=h)
    assert r.status_code == 200

    job = (await client.post(f"/internal/v1/reviews/{board_id}/run", headers=SERVICE)).json()
    assert job["status_cd"] == "SUCCEEDED", job
    board = (await client.get("/api/v1/boards/overview", headers=h)).json()
    assert board["version"] == 2 and board["next_review_at"]
    after = (await client.get(f"/api/v1/boards/{board_id}/tiles", headers=h)).json()
    for t in after:
        if t["slot_key"] == target["slot_key"]:
            assert t["follow_up_cd"] == "SEEN"


async def test_internal_requires_service_key(client):
    assert (await client.get("/internal/v1/assistant-profiles/BOARD_REVIEWER")).status_code == 422
    wrong = await client.get("/internal/v1/assistant-profiles/BOARD_REVIEWER", headers={"X-Service-Key": "nope"})
    assert wrong.status_code == 401
    ok = await client.get("/internal/v1/assistant-profiles/BOARD_REVIEWER", headers=SERVICE)
    assert ok.json()["key"] == "BOARD_REVIEWER"


async def test_preferences_roundtrip(client):
    body = {"user_id": "U6", "origin": "USER", "scope": "WATCHLIST", "key": "areas", "value": {"areas": ["finance"]}}
    assert (await client.post("/internal/v1/preferences", json=body, headers=SERVICE)).status_code == 201
    got = (await client.get("/internal/v1/preferences", params={"user_id": "U6"}, headers=SERVICE)).json()
    assert got[0]["value"] == {"areas": ["finance"]}


async def test_review_is_enqueued_on_sqs(client):
    with mock_aws():
        from app.services.queue import review_queue
        from app.services.workers import enqueue_review

        review_queue.cache_clear()
        board_id = await run_setup(client, await sign_in(client, "U7"), None)
        await enqueue_review(board_id, "U7")
        msgs = review_queue().receive(5, 0)
        assert msgs[0].body["boardId"] == board_id and msgs[0].attempts == 1
        review_queue.cache_clear()


def test_regional_rates_stay_realistic():
    from app.db.seed_metrics import INITIAL_METRICS
    from app.services import metrics

    for region in ("NA", "EMEA", "APAC", "LATAM"):
        stat = metrics.run_query(INITIAL_METRICS, {"metric": "engineering.availability", "op": "stat",
                                                   "params": {"region": region}})
        assert 90 <= stat["value"] <= 100, stat


def test_unknown_metric_raises():
    import pytest

    from app.services import metrics

    with pytest.raises(metrics.MetricsError):
        metrics.run_query({}, {"metric": "nope", "op": "stat"})


async def test_metric_crud_search_pagination_and_cache(client):
    h = await sign_in(client, "admin1")
    body = {"key": "sales.win_rate", "area": "sales", "title": "Win rate", "unit": "%", "base": 24,
            "volatility": 0.05, "higher_is_better": True, "owners": ["Sales Ops"], "sources": ["crm.deals"]}
    assert (await client.post("/api/v1/metrics", json=body, headers=h)).status_code == 201
    assert (await client.post("/api/v1/metrics", json=body, headers=h)).status_code == 409

    page1 = (await client.get("/api/v1/metrics", params={"size": 4}, headers=h)).json()
    assert page1["total"] == 10 and page1["pages"] == 3 and len(page1["items"]) == 4 and not page1["cached"]
    again = (await client.get("/api/v1/metrics", params={"size": 4}, headers=h)).json()
    assert again["cached"] and again["items"] == page1["items"]
    found = (await client.get("/api/v1/metrics", params={"q": "win"}, headers=h)).json()
    assert [m["key"] for m in found["items"]] == ["sales.win_rate"]

    upd = {k: v for k, v in body.items() if k != "key"} | {"title": "Deal win rate"}
    assert (await client.put("/api/v1/metrics/sales.win_rate", json=upd, headers=h)).json()["title"] == "Deal win rate"
    after = (await client.get("/api/v1/metrics", params={"q": "deal win"}, headers=h)).json()
    assert after["total"] == 1 and not after["cached"]  # write invalidated the cached pages
    assert "sales" in (await client.get("/api/v1/metrics/areas", headers=h)).json()

    assert (await client.delete("/api/v1/metrics/sales.win_rate", headers=h)).status_code == 204
    assert (await client.get("/api/v1/metrics/sales.win_rate", headers=h)).status_code == 404
    bad = await client.post("/api/v1/metrics", json=body | {"unit": "parsecs"}, headers=h)
    assert bad.status_code == 422


async def test_disabled_metric_leaves_boards(client):
    h = await sign_in(client, "U8")
    put = {"area": "finance", "title": "Cloud spend", "unit": "USD", "base": 210000, "volatility": 0.05,
           "higher_is_better": False, "owners": ["FinOps"], "sources": ["cloud.billing_export"], "enabled": False}
    assert (await client.put("/api/v1/metrics/finance.cloud_spend", json=put, headers=h)).status_code == 200
    board_id = await run_setup(client, h, None)
    tiles = (await client.get(f"/api/v1/boards/{board_id}/tiles", headers=h)).json()
    assert all("cloud_spend" not in t["slot_key"] for t in tiles)
    data = (await client.get(f"/api/v1/tiles/{tiles[0]['id']}/data", headers=h)).json()
    again = (await client.get(f"/api/v1/tiles/{tiles[0]['id']}/data", headers=h)).json()
    assert not data["cached"] and again["cached"] and again["result"] == data["result"]


async def test_meta_boards_and_jobs_lists(client):
    h = await sign_in(client, "U10")
    meta = (await client.get("/api/v1/meta", headers=h)).json()
    assert "EMEA" in meta["regions"] and "USD" in meta["units"] and meta["areas"]
    await run_setup(client, h, None)
    boards = (await client.get("/api/v1/boards", params={"size": 2}, headers=h)).json()
    assert boards["items"][0]["kind_cd"] == "OVERVIEW" and boards["items"][0]["tile_count"] > 0
    assert boards["pages"] == -(-boards["total"] // 2)
    jobs = (await client.get("/api/v1/jobs", params={"job_type": "SETUP"}, headers=h)).json()
    assert jobs["total"] == 1 and jobs["items"][0]["status_cd"] == "SUCCEEDED"
    other = (await client.get("/api/v1/boards", headers=await sign_in(client, "U11"))).json()
    assert other["total"] == 0


async def test_board_tile_data_is_batched_cached_and_constant_queries(client):
    """N+1 guard: the batch endpoint runs the same number of SQL statements however many tiles a board has."""
    from sqlalchemy import event

    from app.db.session import db

    h = await sign_in(client, "U12")
    board_id = await run_setup(client, h, None)
    statements: list[str] = []

    def count(*_args, **_kw):
        statements.append("q")

    event.listen(db.engine.sync_engine, "before_cursor_execute", count)
    try:
        first = (await client.get(f"/api/v1/boards/{board_id}/tile-data", headers=h)).json()
        cold = len(statements)
        statements.clear()
        second = (await client.get(f"/api/v1/boards/{board_id}/tile-data", headers=h)).json()
        warm = len(statements)
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", count)

    assert len(first) > 3 and all(d["status"] == "OK" for d in first)
    assert not any(d["cached"] for d in first) and all(d["cached"] for d in second)
    assert cold <= 3, f"{cold} statements for {len(first)} tiles"  # tiles + catalog, never one per tile
    assert warm <= 1  # board ownership and results come from Redis; only the tile list query remains


async def test_follow_up_invalidates_cached_tiles(client):
    h = await sign_in(client, "U13")
    board_id = await run_setup(client, h, None)
    tiles = (await client.get(f"/api/v1/boards/{board_id}/tiles", headers=h)).json()  # now cached
    await client.patch(f"/api/v1/tiles/{tiles[0]['id']}/follow-up", json={"follow_up_cd": "DONE"}, headers=h)
    again = (await client.get(f"/api/v1/boards/{board_id}/tiles", headers=h)).json()
    assert again[0]["follow_up_cd"] == "DONE"


async def test_scheduler_batches_queue_sends():
    from moto import mock_aws

    with mock_aws():
        from app.services.queue import review_queue

        review_queue.cache_clear()
        sent = review_queue().send_many([{"boardId": str(i), "userId": "u"} for i in range(23)])
        received = []
        for _ in range(10):
            received += review_queue().receive(10, 0)
        assert sent == 23 and len(received) == 23
        review_queue.cache_clear()
