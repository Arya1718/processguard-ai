# Azure Infrastructure (Prompt 11)

This directory contains the Bicep infrastructure-as-code definitions for the
Azure resources that ProcessGuard AI would run on in production.

## What's here

| File | Description |
|---|---|
| `main.bicep` | Root deployment template — orchestrates all modules |
| `main.parameters.json` | Parameter values (production defaults) |
| `modules/vnet.bicep` | Virtual Network with isolated subnets for each tier |
| `modules/postgres.bicep` | Azure Database for PostgreSQL — Flexible Server |
| `modules/redis.bicep` | Azure Cache for Redis (Premium) |
| `modules/servicebus.bicep` | Azure Service Bus namespace + topics |
| `modules/eventhub.bicep` | Azure Event Hubs namespace + capture |
| `modules/aisearch.bicep` | Azure AI Search service |
| `modules/cognitive.bicep` | Azure Cognitive Services (OpenAI) |
| `modules/storage.bicep` | Azure Storage Account (blob + file) |
| `modules/aks.bicep` | Azure Kubernetes Service cluster |
| `modules/apim.bicep` | Azure API Management |
| `modules/monitoring.bicep` | Azure Monitor + App Insights + Managed Grafana |
| `modules/keyvault.bicep` | Azure Key Vault (secrets) |

## How to deploy

```bash
# 1. Log in to Azure
az login

# 2. Create or select a resource group
az group create --name rg-processguard-prod --location eastus2

# 3. Deploy
az deployment group create \
  --resource-group rg-processguard-prod \
  --template-file main.bicep \
  --parameters @main.parameters.json \
  --parameters resourcePrefix=processguard environmentName=prod
```

## Validation

```bash
# Validate before deploying (catches syntax + schema errors)
az deployment group validate \
  --resource-group rg-processguard-prod \
  --template-file main.bicep \
  --parameters @main.parameters.json

# What-if deployment (shows what would change without applying)
az deployment group what-if \
  --resource-group rg-processguard-prod \
  --template-file main.bicep \
  --parameters @main.parameters.json
```

## Local validation (without Azure)

If you have the Bicep CLI installed:

```bash
# Build (validates syntax + type-checks against the Azure resource provider schema)
bicep build main.bicep

# Lint
bicep lint main.bicep
```

## What changes vs. local Docker Compose

Every local stand-in was deliberately built behind an abstraction boundary
(see `docs/azure-migration-map.md`). The Azure deployment above provides the
real managed services; no agent code changes are needed:

| Docker Compose service | Azure replacement |
|---|---|
| `postgres` container | Azure Database for PostgreSQL — Flexible Server |
| `redis` container | Azure Cache for Redis (Premium) |
| `otel-collector` container | Azure Monitor collector (built into App Insights) |
| `prometheus` container | Azure Monitor Managed Service for Prometheus |
| `grafana` container | Azure Managed Grafana |
| `alertmanager` container | Azure Monitor Action Groups + Metric Alerts |
| `agent-service` container | AKS pod (Helm chart / Kustomize deployment) |
| `middleware` container | AKS pod + Azure API Management (public ingress) |
| `frontend` container | Azure Storage static website + Azure CDN / Front Door |
| `mock-erp-cmms` container | Replaced by Buckman's real ERP/CMMS (out of scope) |
| `oidc-provider` container | Microsoft Entra ID |
| `simulator` container | AKS pod (or Azure Container Instances with autoscaling) |

The mock-erp-cmms and oidc-provider containers do NOT have Azure managed
service equivalents in this template — they are local dev stand-ins that are
replaced by real enterprise systems (real CMMS/EPR, Entra ID) when deploying
to Azure. See `docs/azure-migration-map.md` for the full mapping.

## Network security

All Azure resources are deployed into a VNet with private endpoints. The
public ingress path is:

```
Internet → Azure Front Door → Azure API Management (in VNet) →
  AKS (middleware + agent-service pods) →
  Private Endpoint: PostgreSQL / Redis / Event Hubs / Cognitive Services
```

No service has a public IP. The `0.0.0.0/0` default route is blocked.
