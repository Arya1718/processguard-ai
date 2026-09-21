"""Local OIDC provider -- a PROTOCOL-COMPATIBLE STAND-IN for Microsoft Entra ID.

Implements the small slice of OpenID Connect the demo needs, with standard
shapes so nothing downstream is provider-specific:

    GET  /.well-known/openid-configuration   discovery
    GET  /jwks.json                          RSA public keys (verified by both services)
    GET  /authorize                          code flow (+ PKCE), HTML login form
    POST /authorize                          authenticate, 302 to redirect_uri?code=...
    POST /token                              authorization_code + refresh_token grants
    GET  /me                                 demo identity list (API-key, for the UI)
    GET  /health/live | /health/ready        health probes

AZURE NOTE (docs/auth-flow.md): moving to real Entra ID later is a
CONFIGURATION change -- issuer URL, client id/secret, redirect URI in
.env -- not a code rewrite. The .NET middleware validates tokens with
Microsoft.Identity.Model.JsonWebTokens against issuer + JWKS; the frontend
does standard code+PKCE. Nothing here is special-cased downstream.

SECURITY NOTE: this container is DEV/DEMO tooling. It must NOT be exposed
beyond the host mapping in docker-compose.yml and must never ship to
staging/prod.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
import urllib.parse
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.users import SITE_12_ID, USERS, SITES, find_user, hash_password, verify_password

app = FastAPI(title="ProcessGuard Local OIDC Provider", version="1.0.0")

if config.FRONTEND_ORIGIN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[config.FRONTEND_ORIGIN],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

# ---------------------------------------------------------------------------
# Keys (RSA, RS256) -- generated at startup, served via /jwks.json. In Entra
# ID the keys live in the tenant; here an ephemeral keypair stands in.
#
# ROTATION NOTE: the kid is a FINGERPRINT of the public key, not a constant.
# A container restart mints a fresh keypair; because the kid changes with it,
# every client's JWKS cache (PyJWKClient, ASP.NET MetadataManager) misses on
# the unknown kid and re-fetches /jwks.json automatically -- the same way
# real providers signal rotation. A constant kid + ephemeral key would
# poison every cache until each client restarted (observed as a wave of
# "Signature verification failed" 401s after a provider rebuild).
# ---------------------------------------------------------------------------
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_NUMBERS = PRIVATE_KEY.public_key().public_numbers()


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


JWKS = {
    "keys": [
        {
            "kty": "RSA",
            "kid": None,  # fingerprint, computed below
            "use": "sig",
            "alg": "RS256",
            "n": _b64url_uint(_PUBLIC_NUMBERS.n),
            "e": _b64url_uint(_PUBLIC_NUMBERS.e),
        }
    ]
}
# RFC 7638-style thumbprint truncated to 10 chars -- unique per key material.
_thumb = hashlib.sha256(
    json.dumps({"e": JWKS["keys"][0]["e"], "kty": "RSA", "n": JWKS["keys"][0]["n"]},
               sort_keys=True, separators=(",", ":")).encode()
).digest()
JWKS["keys"][0]["kid"] = "demo-" + base64.urlsafe_b64encode(_thumb).rstrip(b"=").decode()[:10]
KID = JWKS["keys"][0]["kid"]


def _issue_access_token(user: tuple[str, str, str, str, str], client_id: str, nonce: str | None) -> str:
    email, _password, role, site_id, display_name = user
    now = int(time.time())
    payload = {
        "iss": config.ISSUER,
        "aud": client_id or config.CLIENT_ID,
        "sub": email,  # stable per-user subject (the demo has no object ids)
        "name": display_name,
        "preferred_username": email,
        "email": email,
        "roles": [role],  # Entra-style app-roles array claim
        "site_id": site_id,
        "site_name": SITES.get(site_id, ""),
        "iat": now,
        "nbf": now - 5,
        "exp": now + config.ACCESS_TOKEN_MINUTES * 60,
        "oid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"pgai-demo:{email}")),
    }
    if nonce:
        payload["nonce"] = nonce
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})


def _issue_refresh_token(email: str, client_id: str) -> str:
    payload = {
        "typ": "refresh",
        "iss": config.ISSUER,
        "aud": client_id,
        "sub": email,
        "jti": secrets.token_urlsafe(24),
        "iat": int(time.time()),
        "exp": int(time.time()) + 60 * 60 * 24,
    }
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})


# ---------------------------------------------------------------------------
# Authorization-code store (in-memory; single-process dev service).
# ---------------------------------------------------------------------------
_CODES: dict[str, dict] = {}


def _cleanup_codes() -> None:
    now = time.time()
    for code in [c for c, v in _CODES.items() if now - v["created"] > config.CODE_TTL_SECONDS]:
        _CODES.pop(code, None)


def _allowed_redirect(uri: str) -> bool:
    return any(
        uri == allowed or uri.startswith(allowed + ("?" if "?" not in allowed else "#"))
        for allowed in _ALLOWED_REDIRECTS
    )


# Allowed redirect URIs: env-configurable (OIDC_ALLOWED_REDIRECTS,
# comma-separated) so a real deployment can add its own callback origins
# without a code change; the defaults cover the compose demo surfaces.
_DEFAULT_REDIRECTS = "http://localhost:5173/auth/callback,http://127.0.0.1:5173/auth/callback,http://localhost:8090/dev/callback"
_ALLOWED_REDIRECTS = [
    uri.strip()
    for uri in os.environ.get("OIDC_ALLOWED_REDIRECTS", _DEFAULT_REDIRECTS).split(",")
    if uri.strip()
]


def _html_page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ProcessGuard AI - Sign in</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #10151c; color: #e7ecf3;
         display: flex; min-height: 100vh; align-items: center; justify-content: center; margin: 0; }}
  .card {{ background: #1a222d; border: 1px solid #2c3a4d; border-radius: 12px;
          padding: 32px; width: 380px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  p.sub {{ color: #9db0c6; font-size: 13px; margin: 0 0 20px; }}
  label {{ display: block; font-size: 12px; color: #9db0c6; margin: 12px 0 4px; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px; border-radius: 8px;
          border: 1px solid #2c3a4d; background: #10151c; color: #e7ecf3; font-size: 14px; }}
  button {{ margin-top: 18px; width: 100%; padding: 10px; border: 0; border-radius: 8px;
           background: #2f81f7; color: white; font-size: 14px; cursor: pointer; }}
  button[disabled] {{ opacity: .6; }}
  .error {{ color: #ff7a7a; font-size: 13px; margin-top: 12px; }}
  .hint {{ font-size: 12px; color: #9db0c6; margin-top: 16px; line-height: 1.6; }}
  code {{ background: #10151c; padding: 1px 5px; border-radius: 4px; }}
</style>
</head>
<body>
  <div class="card">
    <h1>ProcessGuard AI</h1>
    <p class="sub">Sign in to continue (local identity provider)</p>
    {body}
  </div>
</body>
</html>""",
        status_code=status,
    )


# ---------------------------------------------------------------------------
# Discovery + JWKS
#
# Metadata is HOST-AWARE: the issuer + endpoint URLs in the document match
# whichever hostname the caller used to reach us (browser -> host-published
# port, in-network services -> the container name). This is how real
# providers behave behind multiple endpoints and it keeps the standard
# JwtBearer handler working unchanged: it fetches discovery from the
# in-network authority and gets a jwks_uri IT can reach.
# ---------------------------------------------------------------------------
def _issuer_for_request(request: Request) -> str:
    host = (request.headers.get("host") or "").lower()
    for candidate in (config.ISSUER_INTERNAL, config.ISSUER):
        if urllib.parse.urlparse(candidate).netloc.lower() == host:
            return candidate
    return config.ISSUER


@app.get("/.well-known/openid-configuration")
def discovery(request: Request) -> dict:
    issuer = _issuer_for_request(request)
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "userinfo_endpoint": f"{issuer}/me",
        "jwks_uri": f"{issuer}/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "profile", "email"],
        "subject_types_supported": ["public"],
        "claims_supported": ["sub", "name", "email", "preferred_username", "roles", "site_id", "site_name"],
    }


@app.get("/jwks.json")
def jwks() -> dict:
    return JWKS


@app.get("/health/live")
def health_live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready() -> dict:
    return {"status": "ok", "checks": {"users": {"status": "ok", "count": len(USERS)}}}


# ---------------------------------------------------------------------------
# Authorize endpoint (code flow + PKCE) with a server-rendered login form.
# ---------------------------------------------------------------------------
@app.get("/authorize")
def authorize_get(
    request: Request,
    response_type: str = "code",
    client_id: str = "",
    redirect_uri: str = "",
    scope: str = "openid",
    state: str = "",
    nonce: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    email: str = "",
    error: str | None = None,
) -> Response:
    if response_type != "code" or client_id != config.CLIENT_ID or not _allowed_redirect(redirect_uri):
        return _html_page('<p class="error">Invalid authorization request (client_id or redirect_uri).</p>', 400)

    error_html = f'<p class="error">{error}</p>' if error else ""
    # The demo app's account picker deep-links with ?email= to PRE-FILL the
    # email field (pure UX -- the provider still authenticates the password).
    # HTML-escape for the attribute (NOT urllib.quote -- %40 in the value
    # would be submitted literally and fail authentication).
    safe_email = html.escape(email or "", quote=True)
    email_value = f' value="{safe_email}"' if safe_email else ""
    body = f"""
    <form method="post" action="">
      <input type="hidden" name="response_type" value="{response_type}">
      <input type="hidden" name="client_id" value="{client_id}">
      <input type="hidden" name="redirect_uri" value="{redirect_uri}">
      <input type="hidden" name="scope" value="{scope}">
      <input type="hidden" name="state" value="{state}">
      <input type="hidden" name="nonce" value="{nonce}">
      <input type="hidden" name="code_challenge" value="{code_challenge}">
      <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" autocomplete="username" required autofocus{email_value}>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
      {error_html}
    <div class="hint">
        Demo accounts (passwords in <code>oidc-provider/app/users.py</code>):<br>
        <code>operator@site12.demo</code> &middot; Operator &middot; Site 12<br>
        <code>maintenance@site12.demo</code> &middot; MaintenanceEngineer &middot; Site 12<br>
        <code>manager@site12.demo</code> &middot; PlantManager &middot; Site 12<br>
        <code>operator@site07.demo</code> &middot; Operator &middot; Site 07
      </div>
    </form>"""
    # action="" posts back to the CURRENT url, so the login form works both
    # direct on :8090 and behind any path prefix (e.g. nginx /oidc/ proxy).
    return _html_page(body)


@app.post("/authorize")
async def authorize_post(request: Request) -> Response:
    form = await request.form()
    response_type = str(form.get("response_type", ""))
    client_id = str(form.get("client_id", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    scope = str(form.get("scope", ""))
    state = str(form.get("state", ""))
    nonce = str(form.get("nonce", ""))
    code_challenge = str(form.get("code_challenge", ""))
    code_challenge_method = str(form.get("code_challenge_method", ""))
    email = str(form.get("email", ""))
    password = str(form.get("password", ""))
    if response_type != "code" or client_id != config.CLIENT_ID or not _allowed_redirect(redirect_uri):
        return _html_page('<p class="error">Invalid authorization request.</p>', 400)

    user = find_user(email)
    if user is None or not verify_password(password, hash_password(user[1])):
        return _html_page('<p class="error">Invalid email or password.</p>', 401)

    code = secrets.token_urlsafe(32)
    _cleanup_codes()
    _CODES[code] = {
        "email": user[0],
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "method": code_challenge_method,
        "created": time.time(),
    }

    separator = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{separator}code={urllib.parse.quote(code)}&state={urllib.parse.quote(state)}"
    return RedirectResponse(target, status_code=302)


# ---------------------------------------------------------------------------
# Token endpoint: authorization_code (+ PKCE verify) and refresh_token.
# ---------------------------------------------------------------------------
@app.post("/token")
async def token(request: Request) -> Response:
    form = await request.form()
    grant_type = str(form.get("grant_type", ""))
    client_id = str(form.get("client_id", ""))
    client_secret = str(form.get("client_secret", ""))

    # Client authentication: this demo client is registered as a PUBLIC SPA
    # client -- per the OAuth 2.1 SPA pattern, PKCE (verified below) is the
    # proof of possession and no shared secret is required. A confidential
    # client that DOES present the registered secret is also accepted (the
    # pattern a server-side client would use later).
    if client_id != config.CLIENT_ID:
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    if client_secret and not hmac.compare_digest(client_secret, config.CLIENT_SECRET):
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if grant_type == "authorization_code":
        code = str(form.get("code", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        record = _CODES.pop(code, None)
        if record is None:
            return JSONResponse({"error": "invalid_grant", "error_description": "code expired or unknown"}, status_code=400)
        if record["client_id"] != client_id or record["redirect_uri"] != redirect_uri:
            return JSONResponse({"error": "invalid_grant", "error_description": "client/redirect mismatch"}, status_code=400)

        if record["code_challenge"]:
            verifier = str(form.get("code_verifier", ""))
            method = record["method"] or "S256"
            if not verifier or method != "S256":
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)
            expected = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            ).rstrip(b"=").decode()
            if not hmac.compare_digest(expected, record["code_challenge"]):
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

        user = find_user(record["email"])
        if user is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access = _issue_access_token(user, client_id, record["nonce"])
        refresh = _issue_refresh_token(user[0], client_id)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": config.ACCESS_TOKEN_MINUTES * 60,
            "refresh_token": refresh,
            "scope": record.get("scope", "openid"),
        }

    if grant_type == "refresh_token":
        raw = str(form.get("refresh_token", ""))
        try:
            claims = jwt.decode(
                raw, PRIVATE_KEY.public_key(), algorithms=["RS256"], audience=client_id,
                options={"verify_exp": True},
            )
        except jwt.PyJWTError:
            return JSONResponse({"error": "invalid_grant", "error_description": "refresh token invalid or expired"}, status_code=400)
        if claims.get("typ") != "refresh":
            return JSONResponse({"error": "invalid_grant", "error_description": "not a refresh token"}, status_code=400)
        user = find_user(str(claims.get("sub", "")))
        if user is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access = _issue_access_token(user, client_id, None)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": config.ACCESS_TOKEN_MINUTES * 60,
            "refresh_token": raw,
            "scope": "openid",
        }

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ---------------------------------------------------------------------------
# Demo identity directory (API-key protected, used by the UI's account picker)
# ---------------------------------------------------------------------------
@app.get("/me")
def me(request: Request) -> dict:
    key = request.headers.get("X-Api-Key", "")
    if not hmac.compare_digest(key, config.API_KEY):
        raise HTTPException(status_code=401, detail="invalid api key")
    return {
        "users": [
            {"email": u[0], "role": u[2], "site_id": u[3], "site_name": SITES.get(u[3], ""), "display_name": u[4]}
            for u in USERS
        ],
        "demoSiteId": SITE_12_ID,
    }


# Test helper: mint a token with overridden claims, signed by the REAL
# provider key. Used ONLY by the security test-suite to produce genuinely
# expired / wrong-audience tokens (you cannot unit-test 'expired token is
# rejected' without a token that really is expired). API-key protected.
@app.post("/test/tokens")
async def test_tokens(request: Request) -> dict:
    key = request.headers.get("X-Api-Key", "")
    if not hmac.compare_digest(key, config.API_KEY):
        raise HTTPException(status_code=401, detail="invalid api key")
    body = json.loads((await request.body()) or b"{}")
    email = body.get("email", USERS[0][0])
    user = find_user(email)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown user")
    now = int(time.time())
    overrides = body.get("claims") or {}
    payload = {
        "iss": config.ISSUER,
        "aud": body.get("audience") or config.CLIENT_ID,
        "sub": user[0],
        "name": user[4],
        "preferred_username": user[0],
        "roles": [user[2]],
        "site_id": user[3],
        "site_name": SITES.get(user[3], ""),
        "iat": now,
        "nbf": now - 5,
        "exp": now + config.ACCESS_TOKEN_MINUTES * 60,
    }
    payload.update(overrides)
    token = jwt.encode(payload, PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})
    # Standard OAuth token-response keys (access_token/token_type) so test
    # consumers read it exactly like a real /token response; "token" kept
    # for brevity in quick scripts.
    return {"token": token, "access_token": token, "token_type": "Bearer"}
