// Central API client (Prompt 6).
//
// Every call to the .NET middleware goes through here so that, uniformly:
//   * the auth token from the login flow is attached;
//   * a correlation ID is generated per browser call and sent as
//     X-Correlation-Id (the middleware logs it and forwards it to the
//     agent service -- one browser action is traceable across the stack);
//   * 401 means "token expired or invalid": we do NOT silently fail -- we
//     notify the registered handler (AuthContext), which logs the user
//     out and returns them to the login screen;
//   * error bodies ({detail: ...} from FastAPI via the passthrough, or
//     {error: ...} from the middleware) become readable Error messages.

const API_BASE = import.meta.env.VITE_API_BASE_URL || '/api/v1'

const TOKEN_KEY = 'pgai_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

// Called (once) by AuthContext so the api layer can force a logout on 401
// without a circular import.
let unauthorizedHandler = null
export function onUnauthorized(handler) {
  unauthorizedHandler = handler
}

function correlationId() {
  if (crypto?.randomUUID) return crypto.randomUUID()
  return `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function extractDetail(payload) {
  if (!payload) return null
  if (typeof payload === 'string') return payload
  return payload.detail || payload.error || null
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

export async function apiFetch(path, { method = 'GET', body, token = getToken() } = {}) {
  const headers = {
    'X-Correlation-Id': correlationId(),
  }
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (err) {
    throw new ApiError(0, `Cannot reach the API (${err.message})`)
  }

  if (response.status === 401) {
    // Token expired/invalid: surfaced, not swallowed. The handler switches
    // the app to the login screen (graceful token-expiry handling).
    if (unauthorizedHandler) unauthorizedHandler()
    throw new ApiError(401, 'Session expired -- please log in again.')
  }

  let payload = null
  const text = await response.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, extractDetail(payload) || `Request failed with HTTP ${response.status}`)
  }
  return payload
}

// ---------------------------------------------------------------------------
// Typed endpoint helpers -- the full product surface, all through the
// middleware passthrough (the frontend never calls the agent service or the
// CMMS directly).
// ---------------------------------------------------------------------------

export const api = {
  // Prompt 7: real OIDC login replaces the dev-token call. The browser flow
  // lives in lib/oidc.js (authorize + PKCE + token exchange); nothing here
  // handles credentials.

  incidents: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.status) qs.set('status', params.status)
    if (params.siteId) qs.set('siteId', params.siteId)
    qs.set('limit', String(params.limit ?? 50))
    qs.set('offset', String(params.offset ?? 0))
    return apiFetch(`/incidents?${qs.toString()}`)
  },

  incident: (id) => apiFetch(`/incidents/${id}`),
  evidence: (id) => apiFetch(`/incidents/${id}/evidence`),
  rootCause: (id) => apiFetch(`/incidents/${id}/root-cause`),
  risk: (id) => apiFetch(`/incidents/${id}/risk`),
  recommendation: (id) => apiFetch(`/incidents/${id}/recommendation`),
  stateHistory: (id) => apiFetch(`/incidents/${id}/state-history`),
  action: (id) => apiFetch(`/incidents/${id}/action`),
  sensors: (siteId) => apiFetch(`/sites/${siteId}/sensors/latest`),

  approve: (id) => apiFetch(`/incidents/${id}/approve`, { method: 'POST', body: {} }),
  reject: (id, justification) =>
    apiFetch(`/incidents/${id}/reject`, { method: 'POST', body: { justification } }),
  resolve: (id, note) =>
    apiFetch(`/incidents/${id}/resolve`, { method: 'POST', body: { note } }),

  ask: (id, question) =>
    apiFetch(`/incidents/${id}/ask`, { method: 'POST', body: { question } }),

  triggerScenario: () =>
    apiFetch('/simulator/trigger-scenario/cooling-tower-incident', { method: 'POST', body: {} }),
  resetSimulator: () => apiFetch('/simulator/reset', { method: 'POST', body: {} }),
  simulatorStatus: () => apiFetch('/simulator/status'),
  healthReady: () => apiFetch('/health/ready'),
  // Who the validated token says you are (role, site, approval capability).
  me: () => apiFetch('/auth/me'),
}
