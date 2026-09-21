# Authentication Flow (Prompt 7)

How a browser request becomes an authenticated, site-scoped, role-carrying
request through the whole stack — and what changes to move from the local
demo identity provider to Microsoft Entra ID.

## The identity provider stand-in

`oidc-provider/` is a **minimal, OIDC-protocol-compatible local provider**
(its own FastAPI app, its own container). It exists only so the demo has a
real issuer to log in against. It implements exactly what the demo needs and
nothing more:

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/openid-configuration` | discovery (host-aware issuer/URLs) |
| `GET /jwks.json` | RSA public keys (RS256), fingerprint-based `kid` |
| `GET /authorize` | sign-in page (authorization-code flow, S256 PKCE) |
| `POST /authorize` | credential check → 302 with `?code=` |
| `POST /token` | code (+ PKCE verifier) → access/refresh tokens |
| `POST /test/tokens` | **API-key-protected test endpoint** used by the security test-suite to mint real (including deliberately expired) tokens |

Seeded identities (`oidc-provider/app/users.py`, passwords are local-demo
only, scrypt-hashed):

| Email | Role | Site |
|---|---|---|
| `operator@site12.demo` | Operator | Site 12 — Paper Mill Cooling Tower |
| `maintenance@site12.demo` | MaintenanceEngineer | Site 12 |
| `manager@site12.demo` | PlantManager | Site 12 |
| `operator@site07.demo` | Operator | Site 07 — Leather Tannery Effluent Line (isolation proof) |

Two engineering notes baked into the provider:

- **Host-aware discovery.** The metadata document hands each caller an
  issuer/URLs it can actually reach (browser → `localhost:8090`,
  in-network services → `oidc-provider:8090`) — how real providers behave
  behind multiple endpoints.
- **Fingerprint kids.** The signing key is ephemeral (per container start),
  so the `kid` is a SHA-256 thumbprint of the public key. When the key
  rotates, the kid changes and every client's JWKS cache (PyJWKClient,
  ASP.NET `MetadataManager`) misses and re-fetches automatically. A
  constant kid + ephemeral key would poison caches until each client
  restarted (observed as a wave of "Signature verification failed" 401s).

## The browser flow (authorization code + PKCE)

```
Browser                frontend (nginx)        oidc-provider            middleware (.NET)      agent-service (Python)
   |  GET /                  |                      |                        |                      |
   |------------------------>|  SPA loads           |                        |                      |
   |  no session -> LoginForm: "Sign in" demo-account picker        |                        |                      |
   |  GET {authority}/authorize?response_type=code&client_id=...            |                      |
   |     &code_challenge=..&code_challenge_method=S256&email=<prefill>      |                      |
   |------------------------>|  /oidc/* proxy ----->|  sign-in page          |                      |
   |  POST /authorize (email+password)  <─────────── user signs in          |                      |
   |<- 302 /auth/callback?code=..&state=..           |                        |                      |
   |  GET /auth/callback?code=..      |                      |                        |                      |
   |------------------------>|  AuthCallback:       |                        |                      |
   |  POST {authority}/token (code + code_verifier, public client)          |                      |
   |------------------------>|  /oidc/* proxy ----->|  validates PKCE        |                      |
   |<- access_token (RS256, ~15 min)  |  stored in localStorage             |                      |
   |  GET /api/v1/incidents  |                      |                        |                      |
   |   Authorization: Bearer ──────────────────────────────────────────────>|  JwtBearer validation:|
   |                         |                      |                        |   signature/iss/aud/ |
   |                         |                      |                        |   lifetime + RBAC    |
   |                         |                      |   forwarded token ──────────────────────────>|
   |                         |                      |                        |   re-validated here: |
   |                         |                      |                        |   signature+expiry,  |
   |                         |                      |                        |   site_id + level    |
```

The SPA is a **public client**: PKCE is the proof of possession, no client
secret exists in browser code. The app itself never sees a password —
credentials are typed only on the provider's own page.

## Claim structure (Entra-style)

```json
{
  "iss": "http://localhost:8090",
  "aud": "processguard-frontend",
  "sub": "maintenance@site12.demo",
  "name": "Site 12 Maintenance Engineer",
  "preferred_username": "maintenance@site12.demo",
  "roles": ["MaintenanceEngineer"],
  "site_id": "11111111-1111-1111-1111-111111111111",
  "site_name": "Site 12 - Paper Mill Cooling Tower",
  "iat": 1758350000, "nbf": 1758349995, "exp": 1758350900,
  "oid": "d38e..."   // stable uuid5(sub) -- stands in for Entra object id
}
```

`roles` is the Entra **app-roles array** shape; `site_id`/`site_name` stand
in for what a real deployment would take from the user's directory profile
(or a custom extension attribute).

## Validation at each layer

1. **.NET middleware** — standard `JwtBearer` handler: signature via the
   provider's JWKS, issuer (both network-facing issuer URLs accepted),
   audience, lifetime. No hand-rolled parsing. Claims are mapped to
   ASP.NET authorization policies built from `config/rbac-policy.json`.
2. **Python agent-service** — re-validates the SAME forwarded token
   independently (PyJWKClient + PyJWT: signature, expiry, audience,
   issuer). It does not trust the middleware's decision; it enforces
   site-scoping and the approval-level rule a second time
   (`app/core/security.py`).
3. **Mock CMMS** — API-key protected, reachable only from the agent-service
   network; the Action Agent is the only caller.

## Migrating to Microsoft Entra ID (a config change, not a rewrite)

| Setting | Today (local demo) | Entra ID |
|---|---|---|
| Issuer/authority | `http://localhost:8090` | `https://login.microsoftonline.com/<tenant>/v2.0` |
| Client id | `processguard-frontend` | the app registration's client id |
| Redirect URI | `http://localhost:5173/auth/callback` | the registered SPA redirect URI |
| Sign certificate | ephemeral RSA + fingerprint kid | tenant JWKS (already consumed via the same JWKS path) |
| Claims | `roles`/`site_id` issued by the demo provider | app roles + directory extension attributes (or an on-behalf token mapping) |

What does **not** change: the SPA's authorization-code+PKCE client, the
middleware's JwtBearer validation, the RBAC policy table, the Python layer's
independent re-validation, and every site-scoping rule. They already speak
standard OIDC and read claims generically — that was the point.

## Testing hooks

`POST /test/tokens` (header `X-Api-Key: $OIDC_API_KEY`) mints real
provider-signed tokens for arbitrary users, including deliberately expired
or claim-overridden ones — this is what makes "expired token is rejected by
BOTH layers" a runnable test (`tests/test_rbac_auth.py`) rather than a claim.
