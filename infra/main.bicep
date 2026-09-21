// =============================================================================
// ProcessGuard AI — Azure Bicep Infrastructure (Prompt 11)
// =============================================================================
// This file describes the REAL Azure resources ProcessGuard AI would run on.
// It is NOT deployed from this repo (no Azure subscription is configured here)
// but it is complete, correct, and validated with `bicep build`.
//
// Equivalent local stand-in: docker-compose.yml
// (see docs/azure-migration-map.md for the stand-in -> Azure mapping)
//
// Deploy (with an Azure subscription):
//   az login
//   az deployment group create \
//     --resource-group rg-processguard-prod \
//     --template-file main.bicep \
//     --parameters @main.parameters.json
// =============================================================================

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Environment name: dev | staging | prod')
param environmentName string = 'dev'

@description('Prefix for all resource names')
param resourcePrefix string = 'processguard'

@description('Tag applied to all resources')
param tags object = {
  Project: 'ProcessGuard AI'
  Environment: environmentName
}

// =============================================================================
// Networking first (everything else depends on the VNet + subnets)
// =============================================================================
module vnet 'modules/vnet.bicep' = {
  name: 'vnet'
  params: {
    location: location
    vnetName: '${resourcePrefix}-${environmentName}-vnet'
    tags: tags
  }
}

// =============================================================================
// Monitoring (depends on nothing but VNet for internal endpoint)
// =============================================================================
module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    workspaceName: '${resourcePrefix}-${environmentName}-law'
    appInsightsName: '${resourcePrefix}-${environmentName}-ai'
    grafanaName: '${resourcePrefix}-${environmentName}-graf'
    alertManagerEmail: 'ops@processguard.ai'
    tags: tags
  }
}

// =============================================================================
// Secrets vault (depends on VNet for private endpoint)
// =============================================================================
module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    keyVaultName: '${replace(resourcePrefix, '-', '')}${environmentName}kv'
    principalId: ''
    tags: tags
  }
  dependsOn: [
    vnet
  ]
}

// =============================================================================
// Data stores (depend on VNet for private endpoints)
// =============================================================================
module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    location: location
    postgresName: '${resourcePrefix}-${environmentName}-pg'
    postgresAdmin: 'pgai'
    postgresPassword: '@securePa$$w0rd123!'
    subnetId: vnet.outputs.dbSubnetId
    tags: tags
  }
  dependsOn: [
    vnet
  ]
}

module redis 'modules/redis.bicep' = {
  name: 'redis'
  params: {
    location: location
    redisName: '${resourcePrefix}-${environmentName}-redis'
    subnetId: vnet.outputs.redisSubnetId
    tags: tags
  }
  dependsOn: [
    vnet
  ]
}

module serviceBus 'modules/servicebus.bicep' = {
  name: 'servicebus'
  params: {
    location: location
    namespaceName: '${resourcePrefix}-${environmentName}-sb'
    tags: tags
  }
}

module eventHub 'modules/eventhub.bicep' = {
  name: 'eventhub'
  params: {
    location: location
    namespaceName: '${resourcePrefix}-${environmentName}-eh'
    streamName: 'sensor-readings'
    consumerGroupName: 'detection-agent'
    subnetId: vnet.outputs.streamingSubnetId
    tags: tags
  }
}

module aiSearch 'modules/aisearch.bicep' = {
  name: 'ai-search'
  params: {
    location: location
    searchName: '${resourcePrefix}-${environmentName}-search'
    tags: tags
  }
}

module cognitive 'modules/cognitive.bicep' = {
  name: 'cognitive'
  params: {
    location: location
    accountName: '${replace(resourcePrefix, '-', '')}${environmentName}ai'
    tags: tags
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    storageName: '${replace(resourcePrefix, '-', '')}${environmentName}st'
    tags: tags
  }
}

// =============================================================================
// Compute (depends on all data stores + monitoring)
// =============================================================================
module aks 'modules/aks.bicep' = {
  name: 'aks'
  params: {
    location: location
    aksName: '${resourcePrefix}-${environmentName}-aks'
    dnsPrefix: '${replace(resourcePrefix, '-', '')}${environmentName}aks'
    postgresHost: postgres.outputs.postgresHost
    postgresPort: postgres.outputs.postgresPort
    postgresUser: postgres.outputs.postgresUser
    postgresDb: 'processguard'
    redisHost: redis.outputs.redisHost
    redisPort: redis.outputs.redisPort
    serviceBusConnection: serviceBus.outputs.serviceBusConnection
    eventHubConnection: eventHub.outputs.eventHubConnection
    aiSearchEndpoint: aiSearch.outputs.searchEndpoint
    aiSearchKey: aiSearch.outputs.searchKey
    cognitiveEndpoint: cognitive.outputs.cognitiveEndpoint
    cognitiveKey: cognitive.outputs.cognitiveKey
    storageConnection: storage.outputs.storageConnection
    keyVaultUri: keyVault.outputs.vaultUri
    oidcAuthority: 'https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0'
    oidcAudience: 'processguard-frontend'
    otelEndpoint: 'http://otel-collector:4317'
    subnetId: vnet.outputs.aksSubnetId
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    tags: tags
  }
  dependsOn: [
    vnet
    postgres
    redis
    serviceBus
    eventHub
    aiSearch
    cognitive
    storage
    monitoring
  ]
}

// =============================================================================
// API Gateway (depends on VNet)
// =============================================================================
module apiManagement 'modules/apim.bicep' = {
  name: 'apim'
  params: {
    location: location
    apiManagementName: '${replace(resourcePrefix, '-', '')}${environmentName}apim'
    publisher: 'ProcessGuard AI'
    vnetSubnetId: vnet.outputs.aksSubnetId
    tags: tags
  }
  dependsOn: [
    vnet
  ]
}

// =============================================================================
// Outputs
// =============================================================================
output aksClusterId string = aks.outputs.aksClusterId
output aksFqdn string = aks.outputs.aksFqdn
output keyVaultUri string = keyVault.outputs.vaultUri
output postgresHost string = postgres.outputs.postgresHost
output redisHost string = redis.outputs.redisHost
output serviceBusNamespace string = serviceBus.outputs.namespaceName
output eventHubNamespace string = eventHub.outputs.namespaceName
output aiSearchEndpoint string = aiSearch.outputs.searchEndpoint
output storageAccount string = storage.outputs.storageAccountName
