from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.core.errors import Unauthorized
from app.core.security import decode_token
from app.services.events import hub

router = APIRouter()


@router.websocket("/ws/events")
async def events(ws: WebSocket, token: str) -> None:
    # Browsers can't set headers on a WebSocket upgrade, so the token comes in the query string.
    try:
        me = decode_token(token)
    except Unauthorized:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    await hub.connect(me.user_id, ws)
    await ws.send_json({"type": "connected", "userId": me.user_id})
    try:
        while True:
            if await ws.receive_text() == "ping":  # keeps proxies from idling the socket out
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(me.user_id, ws)
