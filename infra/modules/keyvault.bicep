// =============================================================================
// Azure Key Vault
// Stands in for: .env files (local dev) / config/rbac-policy.json
//
// All secrets (DB password, CMMS API key, Groq API key, OIDC client secret,
// Redis key, cognitive services key) live here. The AKS cluster's managed
// identity gets Get access; the applications retrieve secrets at runtime via
// the workload identity federation (no secrets in containers).
// =============================================================================

param location string
param keyVaultName string
param principalId string = ''  // AKS managed identity for Key Vault access
param tags object = {}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      name: 'standard'
      family: 'A'
    }
    properties: {
      enabledForTemplateDeployment: false
      enabledForDiskEncryption: false
      enabledForDeployment: false
      networkAcls: {
        defaultAction: 'Allow'
        bypass: 'AzureServices'
        virtualNetworkRules: []
        ipRules: []
      }
      publicNetworkAccess: 'Disabled'
    }
  }
}

// Secret definitions — the secret VALUES are populated out-of-band by CI/CD
// (via az keyvault secret set) from GitHub Secrets. The template creates the
// secret METADATA so the names are documented and auditable.
resource pgPassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'PGAI--DATABASE--PASSWORD'
  properties: {
    value: 'placeholder-set-via-cd'
  }
}

resource cmcsApiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'pgai--cmms--api-key'
  properties: {
    value: 'placeholder-set-via-cd'
  }
}

resource groqApiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'PGAI--OPENAI--APIKEY'
  properties: {
    value: 'placeholder-set-via-cd'
  }
}

resource redisKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'PGAI--REDIS--PASSWORD'
  properties: {
    value: 'placeholder-set-via-cd'
  }
}

@description('URI of the Key Vault for reference by application deployments')
output vaultUri string = keyVault.properties.vaultUri

@description('Name of the Key Vault')
output keyVaultName string = keyVault.name

@description('Key Vault resource ID')
output keyVaultId string = keyVault.id
