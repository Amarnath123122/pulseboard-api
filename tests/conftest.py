import os
import tempfile
from collections.abc import AsyncIterator

# Configure before the app (and its cached settings) is imported.
_tmp = tempfile.mkdtemp()
os.environ.update({
    "PB_ENVIRONMENT": "test",
    "PB_DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/test.db",
    "PB_RUN_BACKGROUND_WORKERS": "false",
    "PB_SETUP_BUILD_DELAY_SECONDS": "0",
    "PB_LOG_JSON": "false",
    "PB_AWS_ENDPOINT_URL": "",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
})
os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.seed import seed_reference_data  # noqa: E402
from app.db.session import db  # noqa: E402
from app.main import app  # noqa: E402

SERVICE = {"X-Service-Key": "local-internal-service-key"}


@pytest.fixture(autouse=True)
async def database() -> AsyncIterator[None]:
    db.init(os.environ["PB_DATABASE_URL"])
    async with db.engine.begin() as conn:  # type: ignore[union-attr]
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    from app.services import cache, catalog

    await cache.reset()
    async with db.session() as s:
        await seed_reference_data(s)
        await catalog.seed_if_empty(s)
    yield
    await db.dispose()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def sign_in(client: httpx.AsyncClient, user_id: str = "U1001") -> dict[str, str]:
    r = await client.post("/api/v1/auth/dev-token", json={"user_id": user_id})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
