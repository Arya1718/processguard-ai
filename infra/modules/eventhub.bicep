// =============================================================================
// Azure Event Hubs Namespace + Capture (stream)
// Stands in for: Redis Streams (sensor-readings stream)
//
// The simulator writes telemetry to the "sensor-readings" event hub; the
// Detection Agent reads from the "detection-agent" consumer group.
// Capture streams to Azure Storage for long-term retention.
// =============================================================================

param location string
param namespaceName string
param streamName string
param consumerGroupName string
param subnetId string
param tags object = {}

// Storage account for Event Hubs capture (must be created before capture config)
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: '${replace(namespaceName, '-', '')}cap${uniqueString(resourceGroup().id)}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Cool'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    encryption: {
      services: {
        blob: { enabled: true }
        queue: { enabled: true }
        table: { enabled: true }
        file: { enabled: true }
      }
      keySource: 'Microsoft.Storage'
    }
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
  }
}

// Blob service + container for the capture archive
resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource captureContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobServices
  name: 'sensor-archive'
  properties: {
    publicAccess: 'None'
    containerType: 'blockBlobContainer'
  }
}

// Event Hubs namespace
resource eventHubNamespace 'Microsoft.EventHub/namespaces@2021-11-01' = {
  name: namespaceName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    isAutoInflateEnabled: true
    maximumThroughputUnits: 10
    zoneRedundant: true
    captureDescription: {
      enabled: true
      encoding: 'UTF8'
      intervalInSeconds: 300
      sizeLimitInBytes: 10485760
      destination: {
        name: 'EventHubArchive.Azure.BlockBlob'
        properties: {
          storageAccountResourceId: storageAccount.id
          blobContainer: 'sensor-archive'
          archiveNameFormat: '{Namespace}/{EventHub}/{PartitionId}/{Year}/{Month}/{Day}/{Hour}/{Minute}/{Second}'
        }
      }
    }
  }
}

// The sensor-readings event hub
resource sensorReadingsHub 'Microsoft.EventHub/namespaces/eventhubs@2021-11-01' = {
  parent: eventHubNamespace
  name: streamName
  properties: {
    messageRetentionInDays: 7
    partitionCount: 4
    consumers: [
      consumerGroupName
    ]
  }
}

@description('Event Hubs namespace name')
output namespaceName string = eventHubNamespace.name

@description('Event Hub name (sensor-readings)')
output eventHubName string = sensorReadingsHub.name

@description('Event Hubs connection string (primary key injected via Key Vault by CI/CD)')
output eventHubConnection string = ''

@description('Full namespace FQDN')
output namespaceFqdn string = '${eventHubNamespace.name}.servicebus.windows.net'
