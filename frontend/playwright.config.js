import { defineConfig } from '@playwright/test'

// E2E runs against the real compose stack: `docker compose up -d` first, then
// `npm run e2e`. No webServer override -- the frontend container (nginx on
// host port 5173) proxies /api to the middleware, so the browser exercises
// the exact production path: frontend -> middleware -> agent-service ->
// Postgres/Redis/CMMS.
//
// PLAYWRIGHT_BROWSERS_PATH: set it when installing/running so browser
// binaries land on the storage-rich drive (D: on this machine) instead of C:\.
export default defineConfig({
  testDir: './e2e',
  timeout: 240_000, // the scenario ramp alone takes ~30-40s
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1, // one shared demo stack; parallel runs would trip the idempotency fixtures
  retries: 1,
  reporter: [['list']],
  use: {
    baseURL: process.env.PGAI_E2E_BASE_URL || 'http://localhost:5173',
    trace: 'retain-on-failure',
    actionTimeout: 15_000,
  },
})
