"""Core configuration for the local OIDC provider.

The provider is a PROTOCOL-COMPATIBLE STAND-IN for Microsoft Entra ID.
Everything configurable comes from environment variables (OIDC_*), with
local-dev defaults matching .env.example. Pointing the stack at real Entra
ID later is a configuration change (issuer URL, client id/secret, redirect
URIs) -- NOT a code rewrite -- because the middleware validates tokens with
standard JWT/OIDC libraries against issuer + JWKS, never anything
provider-specific (docs/auth-flow.md).
"""
from __future__ import annotations

import os

ISSUER = os.environ.get("OIDC_ISSUER", "http://localhost:8090")
CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "processguard-frontend")
CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "processguard-frontend-secret")
ACCESS_TOKEN_MINUTES = int(os.environ.get("OIDC_ACCESS_TOKEN_MINUTES", "60"))
CODE_TTL_SECONDS = int(os.environ.get("OIDC_CODE_TTL_SECONDS", "120"))
API_KEY = os.environ.get("OIDC_API_KEY", "oidc-dev-key-change-me")

# Issuer used INSIDE the docker network (what the middleware + agent-service
# see) may differ from the browser-facing issuer above; the middleware is
# told both and matches either so tokens validate from both paths.
ISSUER_INTERNAL = os.environ.get("OIDC_ISSUER_INTERNAL", ISSUER)

# Optional CORS allowance for browser-based flows that bypass the nginx proxy.
FRONTEND_ORIGIN = os.environ.get("OIDC_FRONTEND_ORIGIN", "")
