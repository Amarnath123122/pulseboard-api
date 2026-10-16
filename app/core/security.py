from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings
from app.core.errors import Unauthorized

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str


def issue_token(user_id: str) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    claims = {"sub": user_id, "iat": now, "exp": now + timedelta(minutes=s.jwt_ttl_minutes), "iss": s.app_name}
    return jwt.encode(claims, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(token: str) -> Principal:
    s = get_settings()
    try:
        claims = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm], issuer=s.app_name)
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid or expired token") from exc
    return Principal(user_id=claims["sub"])


async def current_principal(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None:
        raise Unauthorized("Missing bearer token")
    return decode_token(creds.credentials)
