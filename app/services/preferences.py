from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Preference


async def save_preference(
    session: AsyncSession, *, user_id: str, origin: str, scope: str, key: str, value: dict[str, Any]
) -> Preference:
    """Upsert one preference. The caller owns the transaction."""
    stmt = select(Preference).where(Preference.user_id == user_id, Preference.scope_cd == scope, Preference.key == key)
    pref = (await session.execute(stmt)).scalar_one_or_none()
    if pref is None:
        pref = Preference(user_id=user_id, origin_cd=origin, scope_cd=scope, key=key, value=value)
        session.add(pref)
    else:
        pref.value = value
        pref.origin_cd = origin
    await session.flush()
    return pref


async def load_preferences(session: AsyncSession, *, user_id: str, scopes: Iterable[str]) -> list[Preference]:
    stmt = select(Preference).where(Preference.user_id == user_id, Preference.scope_cd.in_(list(scopes)))
    return list((await session.execute(stmt)).scalars())


def as_dict(prefs: Iterable[Preference]) -> dict[str, dict[str, Any]]:
    return {p.key: p.value for p in prefs}
