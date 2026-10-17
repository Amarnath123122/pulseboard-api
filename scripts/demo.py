"""End-to-end walkthrough against a running Pulseboard API.

    python scripts/demo.py                                        # via the gateway on http://localhost:8080
    python scripts/demo.py <gateway-url> <service-key> <api-url>   # api-url: direct API, for /internal calls

Signs in, chats with the setup assistant, waits for the board to go live, reads every tile, marks an alert as
seen, queues a review and shows the board version moving forward.
"""

import base64
import json
import os
import sys
import time

import httpx

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8080"
SERVICE_KEY = sys.argv[2] if len(sys.argv) > 2 else "local-internal-service-key"
AUTH = BASE  # the gateway routes /api/v1/auth/* to the identity service
INTERNAL = sys.argv[3].rstrip("/") if len(sys.argv) > 3 else "http://localhost:8000"  # never exposed by the gateway
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
        print("  api /healthz ->", httpx.get(f"{INTERNAL}/healthz", timeout=30).json())
        print("  api /readyz  ->", httpx.get(f"{INTERNAL}/readyz", timeout=30).json())

        step(f"Sign in as {USER}")
        if os.getenv("DEMO_TOKEN"):  # CI: a pre-minted identity-style token (see scripts/mint_token.py)
            token = os.environ["DEMO_TOKEN"]
            claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
            user_id, role = claims["sub"], claims["role"]
        else:
            creds = {"email": f"{USER}@example.com", "password": "Demo-Password-2026", "display_name": USER}
            session = httpx.post(f"{AUTH}/api/v1/auth/register", json=creds, timeout=30).json()
            token, user_id, role = session["access_token"], session["user"]["id"], session["user"]["role"]
        c.headers["Authorization"] = f"Bearer {token}"
        print(f"  signed in via identity service; role={role} token={token[:24]}...")
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
        print("  enqueue ->", httpx.post(f"{INTERNAL}/internal/v1/reviews/{board_id}", headers=svc, timeout=30).json())
        for _ in range(60):
            board = c.get(f"/api/v1/boards/{board_id}").json()
            if board["version"] >= 2 and board["build_status_cd"] == "LIVE":
                break
            time.sleep(2)
        print(f"  board version now {board['version']} (next review at {board['next_review_at']})")
        if alert:
            same = [t for t in c.get(f"/api/v1/boards/{board_id}/tiles").json() if t["slot_key"] == alert["slot_key"]]
            print(f"  alert follow-up after review -> {same[0]['follow_up_cd'] if same else 'alert cleared'}")
        for j in httpx.get(f"{INTERNAL}/internal/v1/jobs", params={"user_id": user_id}, headers=svc, timeout=30).json():
            print(f"  job {j['job_type_cd']:<6} {j['status_cd']}")

        step("Done")


if __name__ == "__main__":
    main()
