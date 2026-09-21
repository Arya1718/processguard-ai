# CI/CD (Prompt 11)

## Overview

ProcessGuard AI uses **GitHub Actions** for CI/CD. There are three independent
CI workflows (one per tier) and two CD workflows (staging auto-deploy, gated
production deploy). All CI checks are required status checks on the `main`
branch via GitHub branch protection rules.

## CI Workflows

All workflows live in `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| `ci-frontend.yml` | push/PR on frontend paths | npm ci → lint → type check → unit tests (Vitest) → E2E (Playwright) → npm audit → Docker build + Trivy scan |
| `ci-middleware.yml` | push/PR on middleware/config paths | dotnet restore → build → unit tests → `dotnet list package --vulnerable` → dotnet format check → Docker build + Trivy scan |
| `ci-agent.yml` | push/PR on agent-service/mock-erp-cmms paths | pip install → ruff lint → ruff format check → mypy type check → pytest (unit + contract + regression, 75% coverage gate) → pip-audit → Docker build + Trivy scan (agent-service + mock-erp-cmms) |

### Coverage threshold as a CI gate

The 75% coverage threshold from Prompt 10 is enforced in CI via
`pytest.ini`'s `[coverage:report] fail_under = 75` (pytest-cov aborts if
below 75%) **and** a secondary verification step in `ci-agent.yml` that
parses the `coverage.xml` report and fails the job if the line-rate is below
75%. This is not a local-only check — PRs cannot merge without it passing.

### Security scanning

Three layers of dependency scanning:

1. **Frontend**: `npm audit --audit-level=moderate` — catches known
   vulnerabilities in npm packages.
2. **.NET middleware**: `dotnet list package --vulnerable --include-transitive` —
   catches known NuGet vulnerabilities.
3. **Python agent-service**: `pip-audit` against `requirements.txt` for known
   CVEs (PyPI advisory database).
4. **All three**: **Trivy** container image scanner on every built image —
   fails the build on HIGH/CRITICAL findings (`--exit-code 1`).

> **Note:** Trivy is installed at runtime via its official install script in the
> CI runner if not already present. In an environment with Trivy pre-installed
> (e.g., self-hosted runners), this step is a no-op.

## CD Workflows

### Staging — auto-deploy on merge to main

`.github/workflows/cd-staging.yml`

- Triggers on every `push` to `main` (after all CI checks pass via branch
  protection).
- Builds and pushes staging-tagged images to GitHub Container Registry
  (`ghcr.io`).
- Deploys to the staging host via SSH using `docker compose up -d`.
- Uses `docker-compose.staging.yml` override (same services, staging image tags,
  `ASPNETCORE_ENVIRONMENT=Staging`).
- **Staging deploy secrets** (GitHub repository secrets): `STAGING_DEPLOY_KEY`,
  `STAGING_DEPLOY_HOST`, `STAGING_DEPLOY_USER`, `STAGING_DEPLOY_PATH`.

### Production — manual approval gate

`.github/workflows/cd-production.yml`

- **Manual trigger only**: `workflow_dispatch` with required confirmation input.
- The deployer must type `PRODUCTION-DEPLOYED-CONFIRMED` in the `confirm`
  input field — this is a hard gate; without it the job is skipped entirely.
- In addition, **branch protection rules** on `main` should require:
  - CI status checks (all three workflows) to pass
  - A minimum number of approvals (recommended: 2) from designated reviewers
  - If using GitHub's "Required reviewers" feature, the workflow
    `workflow_dispatch` trigger is gated until approval is granted
- Builds and pushes production-tagged images (tagged with commit SHA).
- Deploys to the production host via SSH.
- Runs a **smoke test** after deploy (`curl -sf /health/live`).
- **Production deploy secrets**: `PROD_DEPLOY_KEY`, `PROD_DEPLOY_HOST`,
  `PROD_DEPLOY_USER`, `PROD_DEPLOY_PATH`, plus all application secrets
  (`PROD_OIDC_*`, `PROD_PG_CONNSTRING`, `PROD_REDIS_CONNSTRING`,
  `PROD_GROQ_API_KEY`, `PROD_CMMS_*`).

### Rollback procedure

**Documented in [`docs/cd-rollback.md`](../docs/cd-rollback.md).**

The rollback is tested at least once before this system is considered
production-ready (see "Rollback test" below). The procedure:

1. Identify the last-known-good commit SHA.
2. Re-run the `cd-production.yml` workflow with `workflow_dispatch`, entering
   the previous commit SHA as the `ref` input.
3. Confirm with `PRODUCTION-DEPLOYED-CONFIRMED`.
4. The workflow pulls the old images and runs `docker compose up -d`.
5. Smoke test confirms the rollback succeeded.

**Rollback test procedure** (documented in `docs/cd-rollback.md`):

```
1. Deploy a deliberately broken change (e.g., a syntax error in the middleware)
   to production via cd-production.yml.
2. Verify the smoke test fails and services are degraded.
3. Re-run cd-production.yml with the previous working commit SHA.
4. Verify the smoke test passes and the previous state is restored.
```

## Branch protection rules

The following branch protection rules should be configured on the `main`
branch in GitHub repository settings:

1. **Require status checks to pass before merging** — all three CI workflows
   (`ci-frontend`, `ci-middleware`, `ci-agent`) must pass.
2. **Require branches to be up to date before merging** — prevents merging
   stale PRs.
3. **Require linear history** — enforces clean commit history.
4. **Include administrators** — even repo admins must satisfy the rules.
5. **Require signed commits** (recommended) — for audit integrity.

If the repository is not hosted on GitHub, the equivalent mechanism is:
- For Azure DevOps: use **Pipelines** with branch policies requiring all
  pipelines to succeed before merge.
- For GitLab: use **Merge Request Rules** with "Pipelines must succeed" and
  "Merge request must be approved" requirements.

## Compose override files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Base configuration (all services, local dev defaults) |
| `docker-compose.staging.yml` | Overrides for staging (staging images, Staging env) |
| `docker-compose.prod.yml` | Overrides for production (prod images, replicas, restart policies, prod ports) |

Both override files set `build: null` so only the CI-built images are used —
no on-host building at deploy time.
