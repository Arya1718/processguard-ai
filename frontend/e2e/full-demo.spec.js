// Full reference demo, automated (Prompts 6+7 e2e) -- doubles as the
// live-demo rehearsal script (docs/demo-script.md is derived from this file).
//
// Flow: OIDC sign-in as maintenance@site12.demo (real identity provider,
// authorization-code + PKCE, through the browser) -> trigger the cooling-tower
// scenario from the UI -> wait for the new incident to appear in the alert
// feed -> open its investigation timeline -> wait for the full pipeline to
// populate (detection -> evidence -> root cause -> risk -> recommendation ->
// awaiting approval) -> approve (MaintenanceEngineer may decide Level 2) ->
// confirm the Action Agent's CMMS work order appears -> resolve -> resolved.
//
// Prereqs: the compose stack is up (including oidc-provider on :8090);
// .env has PGAI_LLM__FAKEMODE=true (or a real GROQ_API_KEY).
import { test, expect } from '@playwright/test'

test.describe.configure({ mode: 'serial' })

// Seeded identity-provider credentials (oidc-provider/app/users.py). The
// provider is the ONLY component that ever sees a password -- the app itself
// handles only the returned OIDC token.
const E2E_USER = {
  email: 'maintenance@site12.demo',
  password: 'maintenance-pass',
}
const OIDC_BASE = process.env.PGAI_E2E_OIDC_URL || 'http://localhost:8090'
const OIDC_API_KEY = process.env.OIDC_API_KEY || 'oidc-dev-key-change-me'

// Mint a REAL provider-signed token for the API fixtures (cleanup calls).
// /test/tokens is the provider's own test endpoint -- same keys, same
// claims, so the middleware and agent-service validate it like any other.
async function mintToken(email = E2E_USER.email) {
  const res = await fetch(`${OIDC_BASE}/test/tokens`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Api-Key': OIDC_API_KEY },
    body: JSON.stringify({ email }),
  })
  if (!res.ok) throw new Error(`token mint failed (HTTP ${res.status})`)
  const { access_token: token } = await res.json()
  return token
}

// The Detection Agent folds a new anomaly into any still-open incident on
// CP-04 (idempotency by design), so the run must start from a clean slate:
// resolve leftover incidents from earlier demos/tests via the API.
test.beforeAll(async () => {
  const base = process.env.PGAI_E2E_BASE_URL || 'http://localhost:5173'
  const apiBase = `${base}/api/v1`
  const token = await mintToken()
  const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }

  const listRes = await fetch(`${apiBase}/incidents?limit=200`, { headers })
  const { items } = await listRes.json()
  for (const incident of items) {
    if (incident.status === 'resolved' || incident.status === 'rejected') continue
    // Every non-terminal status can legally transition to 'resolved'.
    await fetch(`${apiBase}/incidents/${incident.id}/resolve`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ note: 'e2e cleanup before demo run' }),
    })
  }
  // Simulator back to normal mode so the run starts from a clean signal.
  await fetch(`${base}/api/v1/simulator/reset`, { method: 'POST', headers })
})

test('full demo: login -> trigger -> timeline -> approve -> action -> resolve', async ({ page }) => {
  // -- OIDC sign-in as maintenance@site12.demo (Prompt 7) -------------------
  // Real browser flow: app -> identity provider (same-origin /oidc proxy) ->
  // provider login page -> authorization code back to /auth/callback ->
  // code+PKCE exchange at /token -> token stored -> feed renders.
  await page.goto('/')
  await expect(page.getByTestId('login-card')).toBeVisible()
  await page.getByTestId('login-as-maintenance@site12.demo').click()
  // Provider-hosted login page (the email is pre-filled by the deep link).
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible({ timeout: 20_000 })
  await page.locator('#password').fill(E2E_USER.password)
  await page.getByRole('button', { name: 'Sign in' }).click()
  // Back through /auth/callback; the polled feed appears once authenticated.
  await expect(page.getByTestId('trigger-scenario')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByTestId('header-role')).toHaveText('Maintenance Engineer')

  // -- Trigger the reference scenario from the UI --------------------------
  // Snapshot existing incident ids so we always work on the NEW incident,
  // even when earlier demo runs left incidents in the feed.
  const beforeIds = await page.getByTestId('incident-card').evaluateAll((cards) =>
    cards.map((c) => c.dataset.incidentId),
  )
  await page.getByTestId('trigger-scenario').click()
  await expect(page.getByText(/Scenario triggered/)).toBeVisible()

  // -- Wait for the NEW incident to appear in the polled feed --------------
  // (Ramp is ~30-40s; the poll interval is 4s.) Diff card ids against the
  // pre-trigger snapshot so we always work on the new incident.
  let freshId = null
  await expect(async () => {
    const ids = await page
      .getByTestId('incident-card')
      .evaluateAll((cards) => cards.map((c) => c.dataset.incidentId))
    freshId = ids.find((id) => !beforeIds.includes(id)) || null
    expect(freshId).toBeTruthy()
  }).toPass({ timeout: 180_000, intervals: [4_000] })

  const newCard = page.locator(`[data-testid="incident-card"][data-incident-id="${freshId}"]`)
  await expect(newCard).toBeVisible()

  // -- Open the Investigation Timeline -------------------------------------
  await newCard.click()
  await expect(page.getByTestId('timeline')).toBeVisible()

  // -- Detection stage: all five drifting sensors visible ------------------
  await expect(page.getByTestId('detection-sensors')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('[data-testid="detection-sensors"] li')).toHaveCount(5)

  // -- Evidence: at least one SOP citation and one historical match --------
  await expect(page.locator('[data-testid="evidence-panel"] .citation-sop').first()).toBeVisible({
    timeout: 60_000,
  })
  await expect(page.locator('[data-testid="evidence-panel"] .citation-history').first()).toBeVisible()

  // -- Root cause: pump-degradation hypothesis with cited evidence ---------
  await expect(page.getByTestId('root-cause')).toBeVisible({ timeout: 60_000 })
  await expect(page.getByTestId('root-cause')).toContainText(/pump degradation/i)
  await expect(page.getByTestId('confidence')).toContainText('%')
  await expect(page.locator('[data-testid="root-cause"] [data-testid="citation-chip"]').first()).toBeVisible()

  // -- Risk: severity + HITL level + plain-language why --------------------
  await expect(page.getByTestId('risk-panel')).toBeVisible({ timeout: 60_000 })
  await expect(page.getByTestId('hitl-level')).toHaveText(/[23]/)

  // -- Recommendation: actions + visible tool-call log ---------------------
  await expect(page.getByTestId('recommendation-panel')).toBeVisible({ timeout: 90_000 })
  await expect(page.getByTestId('tool-log')).toContainText('get_equipment_maintenance_status')

  // -- Pipeline reaches the human gate -------------------------------------
  await expect(page.getByTestId('stage-approval')).toBeVisible({ timeout: 60_000 })
  const approveButton = page.getByTestId('approve-button')
  await expect(approveButton).toBeEnabled({ timeout: 60_000 })

  // -- Approve; the Action Agent creates the CMMS work order ---------------
  // (Work-order references are UUIDs issued by the mock CMMS.)
  await approveButton.click()
  await expect(
    page
      .getByTestId('cmms-reference')
      .filter({ hasText: /[0-9a-f]{8}-[0-9a-f]{4}/i })
      .first(),
  ).toBeVisible({ timeout: 90_000 })
  await expect(page.getByTestId('status-action_taken')).toBeVisible()

  // -- "Why?" panel: grounded answer citing recorded evidence --------------
  await page.getByTestId('chat-input').fill('Why is the pump temperature high?')
  await page.getByTestId('chat-send').click()
  const answer = page.locator('.chat-answer').first()
  await expect(answer).toBeVisible({ timeout: 60_000 })
  await expect(answer).not.toHaveText(/I don't have evidence/i)
  await expect(page.locator('.chat-message.chat-assistant [data-testid="citation-chip"]').first()).toBeVisible()

  // -- Resolve: closes the learning loop ------------------------------------
  await page.getByTestId('resolve-button').click()
  await page
    .getByLabel('Resolution note')
    .fill('E2E run: inspected pump and suction filter, cleared blockage.')
  await page.getByTestId('confirm-resolve').click()
  await expect(page.getByTestId('status-resolved')).toBeVisible({ timeout: 60_000 })
})
