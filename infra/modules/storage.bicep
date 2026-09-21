// =============================================================================
// Azure Storage Account
// Stands in for: Postgres local volumes, Redis data volumes, Grafana dashboards
//
// - Container: deployment artifacts, config files, knowledge base documents
// - Blob: Event Hubs Capture archive, Grafana dashboard JSON, audit logs
// - File share: RAG corpus markdown files (mounted as volume by AKS pods)
// =============================================================================

param location string
param storageName string
param tags object = {}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      virtualNetworkRules: [
        {
          action: 'Allow'
          id: '/subscriptions/${subscription().subscriptionId}/resourceGroups/${resourceGroup().name}/providers/Microsoft.Network/virtualNetworks/processguard-vnet/subnets/aks-subnet'
          ignoreMissingVnetServiceEndpoint: false
        }
      ]
    }
    encryption: {
      services: {
        blob: { enabled: true }
        file: { enabled: true }
        table: { enabled: true }
        queue: { enabled: true }
      }
      keySource: 'Microsoft.Storage'
    }
    identity: {
      type: 'SystemAssigned'
    }
    isHnsEnabled: true
  }
}

// Default file service + share for RAG corpus (mounted by AKS pods)
resource fileServices 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource knowledgebaseShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileServices
  name: 'knowledgebase'
  properties: {
    shareQuota: 5120
  }
}

// Default blob service + containers for audit logs and Event Hubs capture
resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource auditBlobContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobServices
  name: 'audit-logs'
  properties: {
    publicAccess: 'None'
    containerType: 'blockBlobContainer'
  }
}

resource captureBlobContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobServices
  name: 'sensor-archive'
  properties: {
    publicAccess: 'None'
    containerType: 'blockBlobContainer'
  }
}

@description('Storage account name')
output storageAccountName string = storageAccount.name

@description('Storage account key (injected into Key Vault by CI/CD)')
output storageAccountKey string = 'placeholder-set-via-cd'

@description('Storage connection string (constructed in-app from name + key via Key Vault)')
output storageConnection string = ''
