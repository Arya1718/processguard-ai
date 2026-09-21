"""Token-validation endpoint (Prompt 7).

POST /api/v1/auth/validate { token }: the .NET middleware calls this to make
its auth/relay decisions end to end -- the agent service re-validates the
token against the identity provider's public keys (JWKS), issuer, audience,
and lifetime, INDEPENDENTLY of the middleware's own JwtBearer validation.

This is the concrete form of the defense-in-depth rule that "internal" never
means "trusted blindly": both services reach their own verdict on every
token (docs/auth-flow.md). 200 -> valid; 401 -> rejected with the reason.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.security import TokenRejected, validate_token
from app.core.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/validate")
async def validate(body: dict) -> dict:
    try:
        principal = validate_token(str(body.get("token") or ""))
    except TokenRejected as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {
        "valid": True,
        "sub": principal.payload.get("sub"),
        "role": principal.role,
        "site_id": principal.site_id,
        "username": principal.username,
    }
