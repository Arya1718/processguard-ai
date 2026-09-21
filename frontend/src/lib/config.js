// Runtime OIDC configuration (Prompt 7).
//
// The container entrypoint rewrites the __OIDC_*__ placeholders in the BUILT
// bundle's copy of config.js at startup (see docker-entrypoint.d/), so the
// same bundle works in any environment without a rebuild. In local dev the
// placeholders are untouched and the compose defaults below apply.
//
// `authority` is the URL the BROWSER can reach (the provider's host-published
// port in the demo; a real Entra ID tenant URL in production).

const DEV_DEFAULTS = {
  authority: 'http://localhost:8090',
  clientId: 'processguard-frontend',
}

const PLACEHOLDER_AUTHORITY = '__OIDC_AUTHORITY__'
const PLACEHOLDER_CLIENT_ID = '__OIDC_CLIENT_ID__'

// The entrypoint writes window.PGAI_OIDC = {...} into the served config.js.
const injected = typeof window !== 'undefined' ? window.PGAI_OIDC || {} : {}

// Detect the literal placeholders (entrypoint has not run) and fall back.
function resolve(value, placeholder, fallback) {
  if (value && value !== placeholder) return value
  return fallback
}

export const OIDC = {
  authority: resolve(injected.authority, PLACEHOLDER_AUTHORITY, DEV_DEFAULTS.authority),
  clientId: resolve(injected.clientId, PLACEHOLDER_CLIENT_ID, DEV_DEFAULTS.clientId),
}
