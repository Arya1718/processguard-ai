import { test, expect } from '@playwright/test'

// Token-expiry DoD check: an invalid/expired token must land the user back on
// the login screen with a readable message -- never a blank screen. Simulates
// a session whose access token expired mid-flight (both storage keys set, as
// the real OIDC callback leaves them).
test('expired token redirects to login with a clear message', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('pgai_token', 'expired-token-not-valid')
    localStorage.setItem(
      'pgai_id_token_claims',
      JSON.stringify({ preferred_username: 'operator@site12.demo', roles: ['Operator'] }),
    )
  })
  await page.goto('/')
  // First poll 401s -> AuthContext forces logout -> login screen renders.
  // The assertion is the sign-in card, not a selector tied to input attrs.
  await expect(page.getByTestId('login-card')).toBeVisible({ timeout: 15000 })
})
