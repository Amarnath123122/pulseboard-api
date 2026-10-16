from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AssistantProfile

PROFILES = [
    {
        "key": "SETUP_ASSISTANT",
        "name": "Setup assistant",
        "model": "rules-engine-v1",
        "instructions": "Ask what the user cares about, save their preferences and lay out their overview board.",
        "tools": ["save_preference", "claim_layout", "publish_layout", "find_metrics", "run_query", "project"],
    },
    {
        "key": "BOARD_REVIEWER",
        "name": "Board reviewer",
        "model": "rules-engine-v1",
        "instructions": "Re-check watched metrics, raise alerts with a detail and briefing board for each.",
        "tools": ["load_preferences", "read_board", "find_metrics", "describe_metric", "run_query", "project"],
    },
]


async def seed_reference_data(session: AsyncSession) -> None:
    # Several workers start at once; whoever loses the insert race just moves on.
    for profile in PROFILES:
        if await session.get(AssistantProfile, profile["key"]) is None:
            session.add(AssistantProfile(**profile))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
