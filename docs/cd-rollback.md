# CD Rollback Procedure

## When to use this

Deploy a deliberately broken change, confirm it breaks, then roll back to the
previous working state. This procedure is tested as part of the CI/CD setup
(Prompt 11) and must pass at least once before the system is considered
production-ready.

## Prerequisites

- GitHub Actions access to the `processguard-ai` repository
- The `cd-production.yml` workflow is configured with `workflow_dispatch`
  trigger and required confirmation input
- Staging and production deploy secrets are configured (see `docs/ci-cd.md`)
- The staging host (or a local Docker environment) is reachable

## Step-by-step rollback procedure

### Step 1: Identify the last-known-good commit

```bash
git log --oneline -10 main
```

Pick the most recent commit whose deployed state was known-good. Record its
SHA (e.g., `abc1234`).

### Step 2: (Test only) Deploy a deliberately broken change

To verify the rollback path actually works, first deploy a broken change:

1. Create a branch: `git checkout -b rollback-test-broken`
2. Introduce a deliberate break (e.g., add a syntax error to
   `middleware/src/Program.cs` or `agent-service/app/main.py`)
3. Commit and push: `git push origin rollback-test-broken`
4. Create a PR, get approval, merge to `main`
5. The `cd-staging.yml` workflow auto-deploys the broken change to staging
6. Verify staging is broken: `curl -sf http://<staging-host>:8080/api/v1/health/live`
   returns a non-200 or the service crashes

### Step 3: Deploy the good state (rollback)

1. Open the GitHub Actions tab → find **CD — Production Deploy** (or Staging
   if testing) → click **Run workflow** → select the workflow dispatch button
2. Fill in:
   - `ref`: the last-known-good commit SHA (e.g., `abc1234`)
   - `confirm`: type exactly `PRODUCTION-DEPLOYED-CONFIRMED`
3. Click **Run workflow**

The workflow will:
- Checkout the good commit
- Pull and deploy the previously-tagged images at that commit
- Run the smoke test

### Step 4: Verify rollback succeeded

```bash
# Health check
curl -sf http://<staging-host>:8080/api/v1/health/live

# Verify an incident can be created (end-to-end check)
# Trigger the demo scenario and confirm it flows through
curl -X POST http://<staging-host>:8001/api/v1/simulator/trigger-scenario/cooling-tower-incident
sleep 10
curl -s http://<staging-host>:8080/api/v1/incidents?status=open | jq '.items | length'
```

Expected: health returns 200, incidents endpoint returns data. The system is
back to the last-known-good state.

### Step 5: Clean up (test only)

```bash
git checkout main
git branch -D rollback-test-broken
git push origin --delete rollback-test-broken
```

## Why this works

- **Images are tagged by commit SHA**: each CI build produces
  `processguard-middleware:<sha>` and `processguard-agent-service:<sha>` on
  GitHub Container Registry. Deploy at commit `abc1234` pulls the exact images
  built from that commit.
- **The workflow is idempotent**: re-running with the same `ref` produces the
  same state. No manual database migrations or data changes are needed for a
  rollback (migrations are additive and run automatically).
- **Docker Compose handles the swap**: `docker compose pull` fetches the old
  images; `docker compose up -d` stops the old containers and starts the new
  ones. The `--remove-orphan` flag cleans up any leaked containers.
- **Smoke test gates**: if the "good" commit's images have been garbage-
  collected from GHCR (images are retained for 90 days by default), the
  deploy will fail to pull — in that case, the last resort is to rebuild from
  source: `docker compose build && docker compose push` on the good commit.

## Rollback test record

| Test | Date | Broken commit | Rollback commit | Result |
|---|---|---|---|---|
| Staging rollback | _pending_ | _pending_ | _pending_ | _pending_ |

> Fill in this table after running the rollback test. The test must pass
> before the system is considered production-ready.
