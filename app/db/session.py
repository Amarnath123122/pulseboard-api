from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class Database:
    """Holds the engine/sessionmaker so tests and workers can re-point it at another URL."""

    def __init__(self) -> None:
        self.url: str | None = None
        self.engine: AsyncEngine | None = None
        self.sessionmaker: async_sessionmaker[AsyncSession] | None = None

    def init(self, url: str) -> None:
        self.url = url
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30}
        else:
            kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)
        self.engine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()

        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        assert self.sessionmaker is not None, "Database.init() was not called"
        return self.sessionmaker()

    async def dispose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()


db = Database()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with db.session() as session:
        yield session
