// Minimal OIDC authorization-code + PKCE client (Prompt 7).
//
// Runs the standard browser flow directly against the identity provider
// (a protocol-compatible local stand-in for Microsoft Entra ID):
//   /authorize?...  -> user signs in on the provider's page
//   302 ?code=...   -> /auth/callback on this origin exchanges code+v_P at /token
//   access_token    -> stored and sent as Bearer on every API call
//
// STANDARD libraries/flows only -- nothing provider-specific. Pointing the
// demo at real Entra ID later = swapping the issuer/clientId config and
// adding the real redirect URI (docs/auth-flow.md).
const STORAGE = {
  token: 'pgai_token',
  idToken: 'pgai_id_token_claims',
  verifier: 'pgai_pkce_verifier',
  returnTo: 'pgai_return_to',
}

export function getStoredAuth() {
  try {
    const raw = localStorage.getItem(STORAGE.idToken)
    const token = localStorage.getItem(STORAGE.token)
    if (!raw || !token) return null
    return { token, claims: JSON.parse(raw) }
  } catch {
    return null
  }
}

export function clearAuth() {
  localStorage.removeItem(STORAGE.token)
  localStorage.removeItem(STORAGE.idToken)
}

function b64urlToBytes(s) {
  const pad = s.length % 4 === 0 ? '' : '='.repeat(4 - (s.length % 4))
  const base64 = s.replace(/-/g, '+').replace(/_/g, '/') + pad
  const bin = atob(base64)
  return Uint8Array.from(bin, (c) => c.charCodeAt(0))
}

function decodeJwtPayload(jwt) {
  const parts = jwt.split('.')
  if (parts.length !== 3) throw new Error('malformed token')
  return JSON.parse(new TextDecoder().decode(b64urlToBytes(parts[1])))
}

function randomString(bytes = 32) {
  const arr = new Uint8Array(bytes)
  crypto.getRandomValues(arr)
  return Array.from(arr, (b) => b.toString(16).padStart(2, '0')).join('')
}

async function pkcePair() {
  const verifier = randomString(32)
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  const challenge = btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return { verifier, challenge }
}

export function loginRedirect({ authority, clientId, returnTo = '/', email = '' }) {
  localStorage.setItem(STORAGE.returnTo, returnTo)
  return (async () => {
    const { verifier, challenge } = await pkcePair()
    localStorage.setItem(STORAGE.verifier, verifier)
    const redirectUri = `${window.location.origin}/auth/callback`
    const params = new URLSearchParams({
      response_type: 'code',
      client_id: clientId,
      redirect_uri: redirectUri,
      scope: 'openid profile email',
      state: randomString(16),
      nonce: randomString(16),
      code_challenge: challenge,
      code_challenge_method: 'S256',
    })
    // Demo-account prefill (pure UX): deep-link the provider's login page
    // with the email so one click signs in. The provider still verifies the
    // password -- the UI never handles credentials.
    if (email) params.set('email', email)
    window.location.assign(`${authority}/authorize?${params.toString()}`)
  })()
}

export async function completeLogin({ authority, clientId }) {
  const url = new URL(window.location.href)
  const code = url.searchParams.get('code')
  const state = url.searchParams.get('state')
  if (!code) throw new Error(url.searchParams.get('error') || 'no authorization code in callback URL')

  const returnTo = sessionStorage.getItem(STORAGE.returnTo) || localStorage.getItem(STORAGE.returnTo) || '/'
  localStorage.removeItem(STORAGE.returnTo)
  const verifier = localStorage.getItem(STORAGE.verifier)
  if (!verifier) throw new Error('missing PKCE verifier (stale callback?)')

  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    redirect_uri: `${window.location.origin}/auth/callback`,
    client_id: clientId,
    code_verifier: verifier,
  })

  const response = await fetch(`${authority}/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  if (!response.ok) {
    const text = await response.text()
    throw new Error(`token exchange failed (HTTP ${response.status}): ${text.slice(0, 200)}`)
  }
  const tokens = await response.json()
  if (!tokens.access_token) throw new Error('token response carried no access_token')
  if (state) window.history.replaceState({}, '', '/auth/callback')

  localStorage.setItem(STORAGE.token, tokens.access_token)
  localStorage.setItem(
    STORAGE.idToken,
    JSON.stringify(decodeJwtPayload(tokens.access_token)),
  )
  return returnTo
}
