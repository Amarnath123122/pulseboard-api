"""CI helper: create a throwaway RSA key and print `PUBLIC_KEY_B64 TOKEN` for an identity-style access token.
Lets the API be integration-tested on its own; the full system test (with the real identity service) lives
in pulseboard-platform."""

import base64
import sys
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

role = sys.argv[1] if len(sys.argv) > 1 else "EDITOR"
perms = ["boards:read", "boards:follow_up", "setup:run", "metrics:read", "jobs:read"]
if role in ("EDITOR", "ADMIN"):
    perms.append("metrics:write")
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
now = datetime.now(UTC)
token = jwt.encode({"sub": str(uuid.uuid4()), "email": "ci@example.com", "role": role, "perms": perms,
                    "iss": "pulseboard-identity", "aud": "pulseboard", "iat": now,
                    "exp": now + timedelta(hours=1), "typ": "access"}, key, algorithm="RS256")
pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
print(base64.b64encode(pub).decode(), token)
