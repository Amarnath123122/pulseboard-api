"""Token verification and authorization. Tokens are issued by pulseboard-identity (RS256); this service
holds no passwords or signing keys. It verifies signatures against the identity JWKS (cached, refetched when
an unknown key id appears, so key rotation needs no redeploy) and authorizes with the token's `perms`."""

import base64
from dataclasses import dataclass
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives import serialization
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings
from app.core.errors import Forbidden, Unauthorized

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str
    email: str
    permissions: frozenset[str]


@lru_cache
def _jwks_client() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(get_settings().jwks_url or "", cache_keys=True, lifespan=300, timeout=5)


@lru_cache
def _pinned_key():  # noqa: ANN202
    return serialization.load_pem_public_key(base64.b64decode(get_settings().jwt_public_key_b64 or ""))


def _signing_key(token: str):  # noqa: ANN202
    if get_settings().jwt_public_key_b64:
        return _pinned_key()
    try:
        return _jwks_client().get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientConnectionError as exc:
        raise Unauthorized("Identity service unavailable; try again shortly") from exc


def decode_token(token: str) -> Principal:
    s = get_settings()
    try:
        claims = jwt.decode(token, _signing_key(token), algorithms=["RS256"], issuer=s.jwt_issuer,
                            audience=s.jwt_audience, options={"require": ["exp", "sub", "iss", "aud"]})
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Access token expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid token") from exc
    if claims.get("typ") != "access":
        raise Unauthorized("Invalid token")
    return Principal(claims["sub"], claims.get("role", ""), claims.get("email", ""),
                     frozenset(claims.get("perms", [])))


async def current_principal(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None:
        raise Unauthorized("Missing bearer token")
    return decode_token(creds.credentials)


def require(*permissions: str):  # noqa: ANN201
    """Route dependency: the caller's token must carry every listed permission."""

    async def check(me: Principal = Depends(current_principal)) -> Principal:
        missing = [p for p in permissions if p not in me.permissions]
        if missing:
            raise Forbidden("You don't have permission to do this", details={"missing": missing, "role": me.role})
        return me

    return check
