import os
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

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

# Tests mint tokens the way pulseboard-identity does (RS256, same claims) and pin the matching public key.
import base64  # noqa: E402

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
os.environ["PB_JWT_PUBLIC_KEY_B64"] = base64.b64encode(_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

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


_VIEWER = ["boards:read", "boards:follow_up", "setup:run", "metrics:read", "jobs:read"]
PERMS = {"VIEWER": _VIEWER, "EDITOR": _VIEWER + ["metrics:write"],
         "ADMIN": _VIEWER + ["metrics:write", "users:read", "users:write"]}


def token_for(user_id: str, role: str = "VIEWER", **overrides) -> str:
    import jwt

    now = datetime.now(UTC)
    claims = {"sub": user_id, "email": f"{user_id}@example.com", "role": role, "perms": PERMS[role],
              "iss": "pulseboard-identity", "aud": "pulseboard", "iat": now, "exp": now + timedelta(minutes=15),
              "typ": "access"} | overrides
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": "test"})


async def sign_in(client: httpx.AsyncClient, user_id: str = "U1001", role: str = "VIEWER") -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(user_id, role)}"}


async def sign_in_admin(client: httpx.AsyncClient) -> dict[str, str]:
    return await sign_in(client, "admin", "ADMIN")
