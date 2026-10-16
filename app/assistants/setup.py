"""Setup assistant: a short conversation that produces the user's first overview board.

Each turn is streamed as Server-Sent Events. Every reply is saved as a preference. After the last reply
(or a skip) the assistant claims the board and finishes the layout in a background task, so the UI shows
its building state until the `board.published` event arrives.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.assistants.planner import DEFAULT_AREAS, build_plan, parse_areas, parse_region
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound
from app.db.models import AssistantJob, Conversation, ConversationMessage, utcnow
from app.db.session import db
from app.services import layout
from app.services.preferences import as_dict, load_preferences, save_preference

log = logging.getLogger(__name__)
PROFILE_KEY = "SETUP_ASSISTANT"

QUESTIONS: list[dict[str, str]] = [
    {"key": "team", "scope": "LAYOUT",
     "text": "Which team are you on? For example Sales, Customer Success, Operations, Engineering or Finance."},
    {"key": "areas", "scope": "WATCHLIST",
     "text": "Which areas should I watch for you? Pick any of sales, customers, operations, engineering, finance."},
    {"key": "region", "scope": "LAYOUT",
     "text": "Should I focus on one region (NA, EMEA, APAC, LATAM) or all of them?"},
]

_background: set[asyncio.Task] = set()


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _stream(text: str) -> AsyncIterator[str]:
    for word in text.split(" "):
        yield sse("delta", {"text": word + " "})
        await asyncio.sleep(0.02)


async def converse(user_id: str, conversation_id: str | None, action: str, text: str) -> AsyncIterator[str]:
    async with db.session() as session:
        if conversation_id:
            convo = await session.get(Conversation, conversation_id)
            if convo is None or convo.user_id != user_id:
                raise NotFound("Conversation not found")
            if convo.status_cd != "OPEN":
                raise Conflict("Setup is already complete")
        else:
            convo = Conversation(user_id=user_id, purpose_cd="SETUP", status_cd="OPEN", state={"step": 0})
            session.add(convo)
            await session.flush()
        yield sse("conversation", {"conversation_id": convo.id})

        step = int(convo.state.get("step", 0))
        if action == "reply" and text.strip() and step < len(QUESTIONS):
            q = QUESTIONS[step]
            session.add(ConversationMessage(conversation_id=convo.id, author_cd="user", content=text))
            await save_preference(session, user_id=user_id, origin="SETUP", scope=q["scope"], key=q["key"],
                                  value=_interpret(q["key"], text))
            step += 1
        elif action == "skip":
            session.add(ConversationMessage(conversation_id=convo.id, author_cd="user", content="(skipped)"))

        if action != "skip" and step < len(QUESTIONS):
            reply = ("Hi! I'll set up your overview board. Three quick questions. " if step == 0 else "Thanks. ") \
                + QUESTIONS[step]["text"]
            convo.state = {"step": step}
            session.add(ConversationMessage(conversation_id=convo.id, author_cd="assistant", content=reply))
            await session.commit()
            async for chunk in _stream(reply):
                yield chunk
            yield sse("turn.end", {"step": step, "total_steps": len(QUESTIONS), "finished": False})
            return

        origin = "DEFAULT_TEMPLATE" if action == "skip" and step == 0 else "SETUP"
        reply = ("No problem, I'll start you with a standard board and you can tune it later. "
                 if origin == "DEFAULT_TEMPLATE" else "Great, that's all I need. ") + "Building your board now."
        convo.state = {"step": step}
        convo.status_cd = "CLOSED"
        session.add(ConversationMessage(conversation_id=convo.id, author_cd="assistant", content=reply))
        job = AssistantJob(job_type_cd="SETUP", profile_key=PROFILE_KEY, user_id=user_id, status_cd="RUNNING")
        session.add(job)
        await session.commit()
        job_id = job.id

        held = await layout.claim(session, user_id=user_id, origin=origin, claim_holder=f"setup:{job_id}")
        job.board_id = held.board_id
        await session.commit()

    async for chunk in _stream(reply):
        yield chunk
    yield sse("board", {"board_id": held.board_id, "build_status": "BUILDING"})
    yield sse("turn.end", {"step": step, "total_steps": len(QUESTIONS), "finished": True})

    task = asyncio.create_task(_build(user_id, job_id, held))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _build(user_id: str, job_id: str, held: layout.Claim) -> None:
    async with db.session() as session:
        try:
            await asyncio.sleep(get_settings().setup_build_delay_seconds)
            prefs = as_dict(await load_preferences(session, user_id=user_id, scopes=["LAYOUT", "WATCHLIST"]))
            plan = build_plan(team=prefs.get("team", {}).get("text", ""),
                              areas=prefs.get("areas", {}).get("areas", DEFAULT_AREAS),
                              region=prefs.get("region", {}).get("region", "ALL"))
            await layout.publish(session, board_id=held.board_id, claim_holder=held.claim_holder,
                                 generation=held.generation, plan=plan, job_id=job_id)
            await _finish(session, job_id, "SUCCEEDED")
        except Exception as exc:  # noqa: BLE001
            log.exception("setup build failed")
            await layout.release(session, board_id=held.board_id, claim_holder=held.claim_holder,
                                 generation=held.generation)
            await _finish(session, job_id, "FAILED", str(exc)[:2000])


async def _finish(session, job_id: str, status: str, error: str | None = None) -> None:  # noqa: ANN001
    job = await session.get(AssistantJob, job_id)
    if job:
        job.status_cd, job.error, job.finished_at = status, error, utcnow()
        await session.commit()


def _interpret(key: str, text: str) -> dict[str, Any]:
    if key == "areas":
        return {"text": text, "areas": parse_areas(text)}
    if key == "region":
        return {"text": text, "region": parse_region(text)}
    return {"text": text.strip()[:200]}


async def drain() -> None:
    """Wait for in-flight builds (shutdown and tests)."""
    if _background:
        await asyncio.gather(*list(_background), return_exceptions=True)
