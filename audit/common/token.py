"""Short-lived, signed case tokens (HMAC-SHA256, JWT-like but stdlib only).

The orchestrator mints a token scoped to one engagement (one entity, one
quarter, AP tables only). The sandbox only ever sees the token; the broker
verifies it and enforces the scope on every request.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field

from audit.common.canon import AP_TABLES


class TokenError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class CaseScope:
    engagement_id: str
    entity: str
    quarter: str
    tables: tuple[str, ...] = AP_TABLES
    token_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    exp: int = 0


def mint(scope: CaseScope, secret: str, ttl_seconds: int = 7200) -> str:
    claims = {
        "eng": scope.engagement_id,
        "ent": scope.entity,
        "qtr": scope.quarter,
        "tbl": list(scope.tables),
        "jti": scope.token_id,
        "exp": int(time.time()) + min(ttl_seconds, 7200),
    }
    body = _b64(json.dumps(claims, sort_keys=True).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), "sha256").digest())
    return f"{body}.{sig}"


def verify(token: str, secret: str, now: float | None = None) -> CaseScope:
    try:
        body, sig = token.split(".")
    except (ValueError, AttributeError):
        raise TokenError("malformed token")
    expected = _b64(hmac.new(secret.encode(), body.encode(), "sha256").digest())
    if not hmac.compare_digest(sig, expected):
        raise TokenError("bad signature")
    claims = json.loads(_unb64(body))
    if (now or time.time()) >= claims["exp"]:
        raise TokenError("token expired")
    return CaseScope(
        engagement_id=claims["eng"],
        entity=claims["ent"],
        quarter=claims["qtr"],
        tables=tuple(claims["tbl"]),
        token_id=claims["jti"],
        exp=claims["exp"],
    )
