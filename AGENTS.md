# AGENTS.md

Common commands for the ProcessGuard AI repository.

## Python agent-service

```bash
# Install dependencies (from agent-service/)
pip install -r requirements.txt

# Run all tests (excludes test_health_and_bus.py which has a pre-existing
# httpx/starlette version compatibility issue unrelated to these changes)
python -m pytest tests/ -q --ignore=tests/test_health_and_bus.py

# Run specific test files
python -m pytest tests/test_observability.py tests/test_telemetry_endpoints.py -v

# Type checking
mypy app/ tests/ --ignore-missing-imports

# Linting
ruff check app/ tests/
ruff format --check app/ tests/
```

## .NET middleware

```bash
# Build
dotnet build src/ProcessGuard.Middleware.csproj

# Run migrations + seed
dotnet run --project src/ProcessGuard.Middleware.csproj

# Format
dotnet format src/ProcessGuard.Middleware.csproj

# Test (if test project exists)
dotnet test
```

## React/Vite frontend

```bash
cd frontend
npm install
npm run dev
npm test
npm run lint
npm run typecheck
```

## Docker Compose

```bash
# Start all services
docker compose up -d

# View logs
docker compose logs -f

# Run migrations manually (middleware auto-migrates on startup)
docker compose exec middleware dotnet ef database update

# Stop everything
docker compose down -v
```

## Observability (Prompt 9)

After `docker compose up`:
- **Grafana**: http://localhost:3000 (admin/admin)
- **Prometheus**: http://localhost:9090
- **AlertManager**: http://localhost:9093
- **Middleware /metrics**: http://localhost:8080/metrics
- **Agent service /metrics**: http://localhost:8001/metrics
- **Telemetry summary**: http://localhost:8001/api/v1/telemetry/summary
- **Incident cost**: http://localhost:8001/api/v1/incidents/{id}/cost

Set `PGAI_OTEL__DISABLE=true` to skip OTel init (useful for CI unit tests).

## Testing (Prompt 10)

### Python unit tests

```bash
cd agent-service
# Run unit tests with 75% coverage threshold (enforced via pytest.ini)
python -m pytest tests/unit/ -q --cov=app --cov-report=term-missing

# Run contract tests (verify JSON schema of API payloads)
python -m pytest tests/contract/ -q

# Run integration tests (requires `docker compose up`)
python -m pytest tests/integration/ -q
```

### .NET middleware tests

```bash
cd middleware
# Build the test project
dotnet build tests/ProcessGuard.Middleware.Tests.csproj

# Run middleware unit tests
dotnet test tests/ProcessGuard.Middleware.Tests.csproj --no-build --verbosity minimal
```

### Load tests (k6)

Requires k6 (`winget install k6 --id Grafana.k6` or `docker pull grafana/k6`).
Run after `docker compose up`:

```bash
cd agent-service
k6 run load/sensor_ingestion.js         # sensor ingestion path
k6 run load/incident_reads.js           # dashboard read path
k6 run load/concurrent_incidents.js     # full pipeline concurrent incidents
```

See `load/README.md` for expected performance numbers and environment variables.

## CI/CD (Prompt 11)

### Local test/lint commands (mirror what CI runs)

```bash
cd agent-service
PGAI_OTEL__DISABLE=true ruff check app/ tests/
PGAI_OTEL__DISABLE=true mypy app/ tests/ --ignore-missing-imports
PGAI_OTEL__DISABLE=true ruff format --check app/ tests/
PGAI_OTEL__DISABLE=true python -m pytest tests/unit/ tests/contract/ tests/regression/ -q -m "not integration and not data"

cd ../middleware
dotnet build src/ProcessGuard.Middleware.csproj
dotnet test tests/ProcessGuard.Middleware.Tests.csproj --no-build --verbosity minimal

cd ../frontend
npm ci
npm run lint
npm run typecheck
npm test
npx playwright test e2e/full-demo.spec.js
```

### Security scanning (CI runs these)

```bash
# Frontend
npm audit

# .NET middleware
dotnet list package --vulnerable

# Python agent-service
pip install pip-audit && pip-audit --requirement requirements.txt

# Container image scanning (runs in CI after Docker build)
# Trivy scans both agent-service and mock-erp-cmms images
trivy image pgai-agent-service:sha-$GITHUB_SHA
trivy image pgai-mock-erp-cmms:sha-$GITHUB_SHA
```

### Azure deployment (Bicep)

```bash
# Validate Bicep templates
az deployment group validate \
  --resource-group rg-processguard-prod \
  --template-file infra/main.bicep \
  --parameters @infra/main.parameters.json

# Deploy
az deployment group create \
  --resource-group rg-processguard-prod \
  --template-file infra/main.bicep \
  --parameters @infra/main.parameters.json

# Populate Key Vault secrets from local .env (one-time bootstrap)
for var in PGAI_DATABASE__PASSWORD PGAI_REDIS__PASSWORD PGAI_OPENAI__APIKEY PGAI_CMMS__API_KEY; do
  az keyvault secret set --vault-name $PGAI_KEYVAULT_NAME --name $var --value "${!var}"
done
```

### Rollback (production incident)

See `docs/cd-rollback.md` for the full procedure. Briefly:
```bash
# 1. Promote previous image tag
az webapp config container set ... # or kubectl set image

# 2. Verify
curl https://api.processguard.ai/health

# 3. Record outcome in the rollback table
```
