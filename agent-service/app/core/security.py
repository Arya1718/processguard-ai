"""Token validation + RBAC for the agent-service (Prompt 7).

DEFENSE IN DEPTH: the agent service is never directly internet-facing (the
.NET middleware is the single entry point), but it does NOT trust the
middleware blindly. Every API request must carry the user's OIDC bearer
token; this module re-validates it INDEPENDENTLY -- RSA signature via the
identity provider's JWKS (fetched + cached), issuer, audience and lifetime
-- and derives the per-request site scope and role from the token's claims.

The role->capability table is the SAME config/rbac-policy.json the .NET
middleware loads (mounted into both containers): the two services enforce
one policy from one source of truth. Site scoping is enforced in the query
layer (app.api.incidents + app.core.db), so a caller cannot reach another
site's data even by addressing ids directly.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException

from app.core.config import get_settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)


class TokenRejected(Exception):
    """Raised when a presented token fails validation (bad signature,
    expired, wrong issuer/audience, or missing role/site claims)."""


# ---------------------------------------------------------------------------
# RBAC policy table (shared with the .NET middleware)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RbacPolicy:
    max_approval_level: dict[str, int]
    incident_read_roles: frozenset[str]
    chat_roles: frozenset[str]
    resolve_roles: frozenset[str]
    simulator_roles: frozenset[str]

    def level_for(self, role: str) -> int:
        return self.max_approval_level.get(role, 0)

    def can_read_incidents(self, role: str) -> bool:
        return role in self.incident_read_roles

    def can_chat(self, role: str) -> bool:
        return role in self.chat_roles

    def can_resolve(self, role: str) -> bool:
        return role in self.resolve_roles

    def can_control_simulator(self, role: str) -> bool:
        return role in self.simulator_roles


def load_rbac_policy(path: str | None = None) -> RbacPolicy:
    path = path or os.environ.get("PGAI_RBAC__POLICYFILE", "config/rbac-policy.json")
    if not os.path.exists(path):
        raise RuntimeError(
            f"RBAC policy file not found at '{path}'. Set PGAI_RBAC__POLICYFILE "
            "(docker-compose mounts the shared config/ directory)."
        )
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    roles = {
        name: int(spec.get("max_approval_level", 0))
        for name, spec in (data.get("roles") or {}).items()
    }
    return RbacPolicy(
        max_approval_level=roles,
        incident_read_roles=frozenset(data.get("incident_read_roles") or []),
        chat_roles=frozenset(data.get("chat_roles") or []),
        resolve_roles=frozenset(data.get("resolve_roles") or []),
        simulator_roles=frozenset(data.get("simulator_roles") or []),
    )


_policy: RbacPolicy | None = None


def get_rbac_policy() -> RbacPolicy:
    global _policy
    if _policy is None:
        _policy = load_rbac_policy()
    return _policy


# ---------------------------------------------------------------------------
# Principal: what one validated token says about the caller
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    token: str
    payload: dict

    @property
    def site_id(self) -> str:
        return str(self.payload.get("site_id") or "")

    @property
    def role(self) -> str:
        roles = self.payload.get("roles")
        if isinstance(roles, list):
            return str(roles[0]) if roles else ""
        return str(roles or "")

    @property
    def username(self) -> str:
        return str(
            self.payload.get("preferred_username")
            or self.payload.get("name")
            or self.payload.get("sub")
            or "unknown"
        )


# ---------------------------------------------------------------------------
# JWKS-backed validation (standard library flow; mirrors the .NET JwtBearer
# handler: signature -> issuer -> audience -> lifetime)
# ---------------------------------------------------------------------------

_jwk_client: jwt.PyJWKClient | None = None


def _get_jwk_client() -> jwt.PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        settings = get_settings()
        jwks_url = settings.oidc_jwks_url or f"{settings.oidc_internal_issuer}/jwks.json"
        # cache_keys=True: keys are cached and refreshed by PyJWKClient; a
        # provider key rotation is picked up via the kid-miss path.
        _jwk_client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
    return _jwk_client


def validate_token(token: str) -> Principal:
    """Validate a bearer token and return the principal it represents.

    Raises TokenRejected on ANY failure -- the caller maps that to 401.
    """
    settings = get_settings()
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise TokenRejected("no bearer token presented")

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
    except (jwt.PyJWKClientError, jwt.PyJWTError) as exc:
        # PyJWTError too: a non-JWT string (garbage bearer) raises DecodeError
        # inside the JWK client's parse, which is NOT a PyJWKClientError --
        # unmapped, it would escape as a 500 instead of a clean 401.
        raise TokenRejected(f"cannot resolve signing key: {exc}") from exc

    try:
        # verify_iss is handled explicitly below because the provider is
        # reachable under two issuer URLs (host-facing and in-network);
        # audience + lifetime + signature are verified by PyJWT.
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.oidc_audience,
            options={"verify_iss": False, "require": ["exp", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenRejected("token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise TokenRejected("token audience mismatch") from exc
    except jwt.PyJWTError as exc:
        raise TokenRejected(f"token validation failed: {exc}") from exc

    if payload.get("iss") not in settings.oidc_valid_issuers:
        raise TokenRejected(f"untrusted issuer {payload.get('iss')!r}")

    principal = Principal(token=token, payload=payload)
    if not principal.site_id or not principal.role:
        raise TokenRejected("token carries no site_id/role claims")
    return principal


def require_principal(authorization: str = Header(default="")) -> Principal:
    """FastAPI dependency: validate the Authorization header or 401."""
    try:
        return validate_token(authorization)
    except TokenRejected as exc:
        raise HTTPException(
            status_code=401,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def ensure_site(principal: Principal, incident_site_id: str | None) -> None:
    """Query-layer site enforcement: the incident's owning site must equal
    the principal's site. A mismatch is a 403 POLICY refusal, not a 404."""
    if incident_site_id is None:
        return  # unknown incident -> the route renders 404
    if not incident_site_id.lower() == principal.site_id.lower():
        raise HTTPException(
            status_code=403,
            detail="this incident belongs to another site and is outside your access scope",
        )


def ensure_role(principal: Principal, allowed: frozenset[str], capability: str) -> None:
    if principal.role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"role '{principal.role}' is not permitted to {capability}",
        )


def ensure_approval_level(principal: Principal, required_level: int) -> None:
    """Second-layer HITL gate (mirrors the .NET policy): the caller's role
    must have max_approval_level >= the incident's required level."""
    allowed = get_rbac_policy().level_for(principal.role)
    if allowed < required_level:
        raise HTTPException(
            status_code=403,
            detail=(
                f"role '{principal.role}' cannot decide a HITL level {required_level} "
                f"recommendation (maximum approved level for this role: {allowed})"
            ),
        )
