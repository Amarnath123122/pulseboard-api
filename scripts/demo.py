"""End-to-end walkthrough against a running Pulseboard API.

    python scripts/demo.py                                   # http://localhost:8000
    python scripts/demo.py https://<app>.azurewebsites.net <service-key>

Signs in, chats with the setup assistant, waits for the board to go live, reads every tile, marks an alert as
seen, queues a review and shows the board version moving forward.
"""

import json
import sys
import time

import httpx

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
SERVICE_KEY = sys.argv[2] if len(sys.argv) > 2 else "local-internal-service-key"
USER = f"demo-{int(time.time()) % 100000}"


def step(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


def stream_turn(c: httpx.Client, body: dict) -> dict:
    """Send one assistant turn and print the streamed reply; returns the non-text events."""
    events, text = {}, ""
    with c.stream("POST", "/api/v1/assistant/messages", json=body) as r:
        r.raise_for_status()
        event = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if event == "delta":
                    text += data["text"]
                else:
                    events[event] = data
    print(f"  assistant > {text.strip()}")
    if "error" in events:
        raise SystemExit(f"assistant error: {events['error']}")
    return events


def num(v: float) -> str:
    return f"{int(v):,}" if float(v).is_integer() else f"{v:,}"


def fmt(result: dict | None) -> str:
    if not result:
        return "-"
    if result["shape"] == "stat":
        return f"{num(result['value'])} {result['unit']} ({result['change_pct']:+.1f}% vs avg)"
    if result["shape"] == "categories":
        return ", ".join(f"{r['label']}={num(r['value'])}" for r in result["rows"])
    rows = result["rows"]
    projected = sum(1 for r in rows if r.get("projected"))
    return f"{len(rows)} points, last {num(rows[-1]['value'])} {result['unit']}" + (
        f" ({projected} projected)" if projected else "")


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=60) as c:
        step(f"Health  ({BASE})")
        print("  /healthz ->", c.get("/healthz").json())
        print("  /readyz  ->", c.get("/readyz").json())

        step(f"Sign in as {USER}")
        token = c.post("/api/v1/auth/dev-token", json={"user_id": USER}).json()["access_token"]
        c.headers["Authorization"] = f"Bearer {token}"
        print(f"  got JWT ({token[:24]}...)")
        r = c.get("/api/v1/boards/overview")
        print(f"  overview before setup -> HTTP {r.status_code} ({r.json()['error']['code']})")

        step("Setup assistant (streamed over SSE)")
        convo = stream_turn(c, {"action": "start"})["conversation"]["conversation_id"]
        events = {}
        for answer in ["Engineering", "sales, customers, operations, engineering and finance", "EMEA please"]:
            print(f"  you       > {answer}")
            events = stream_turn(c, {"conversation_id": convo, "text": answer})
        board_id = events["board"]["board_id"]
        print(f"  board {board_id} -> {events['board']['build_status']}")

        step("Wait for the board to go live")
        for _ in range(60):
            board = c.get(f"/api/v1/boards/{board_id}").json()
            print(f"  build_status={board['build_status_cd']} version={board['version']}")
            if board["build_status_cd"] != "BUILDING":
                break
            time.sleep(1)
        s = board["settings"]
        print(f"  team={s['team']!r} areas={s['areas']} region={s['region']} alerts={s['alert_count']}")

        step("Tiles on the overview (each tile's data fetched separately)")
        tiles = c.get(f"/api/v1/boards/{board_id}/tiles").json()
        for t in tiles:
            data = c.get(f"/api/v1/tiles/{t['id']}/data").json()
            tag = f"[{t['tier_cd']:<8} {t['priority_cd']:<6}]"
            print(f"  {t['position']:>2}. {tag} {t['title']:<32} {fmt(data['result'])}")

        alert = next((t for t in tiles if t["tier_cd"] == "ALERT"), None)
        if alert:
            step(f"Alert drill-through: {alert['title']}")
            for label, bid in (("detail", alert["detail_board_id"]), ("briefing", alert["briefing_board_id"])):
                b = c.get(f"/api/v1/boards/{bid}").json()
                print(f"  {label}: {b['title']}")
                for t in c.get(f"/api/v1/boards/{bid}/tiles").json():
                    data = c.get(f"/api/v1/tiles/{t['id']}/data").json()
                    print(f"     - {t['title']:<28} {fmt(data['result'])}")
                if label == "briefing":
                    print(f"     summary:    {b['settings']['summary']}")
                    print(f"     owners:     {', '.join(b['settings']['owners'])}")
                    for n in b["settings"]["next_steps"]:
                        print(f"     next step:  {n}")
            r = c.patch(f"/api/v1/tiles/{alert['id']}/follow-up", json={"follow_up_cd": "SEEN"}).json()
            print(f"  marked alert follow-up -> {r['follow_up_cd']}")

        step("Scheduled review through the queue")
        svc = {"X-Service-Key": SERVICE_KEY}
        print("  enqueue ->", c.post(f"/internal/v1/reviews/{board_id}", headers=svc).json())
        for _ in range(60):
            board = c.get(f"/api/v1/boards/{board_id}").json()
            if board["version"] >= 2 and board["build_status_cd"] == "LIVE":
                break
            time.sleep(2)
        print(f"  board version now {board['version']} (next review at {board['next_review_at']})")
        if alert:
            same = [t for t in c.get(f"/api/v1/boards/{board_id}/tiles").json() if t["slot_key"] == alert["slot_key"]]
            print(f"  alert follow-up after review -> {same[0]['follow_up_cd'] if same else 'alert cleared'}")
        for j in c.get("/internal/v1/jobs", params={"user_id": USER}, headers=svc).json():
            print(f"  job {j['job_type_cd']:<6} {j['status_cd']}")

        step("Done")


if __name__ == "__main__":
    main()
