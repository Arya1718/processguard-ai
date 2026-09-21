// =============================================================================
// Azure Service Bus Namespace
// Stands in for: Redis pub/sub (EventBus) for business event fan-out
//
// Topics:
// - pgai.anomalies      (AnomalyDetected — fan-out to Knowledge/Root-Cause/Risk/Recom)
// - pgai.diagnosis      (evidence ready)
// - pgaa.stage_completed (orchestrator routing)
// - pgai.incident_approved
// - pgai.action_completed
// - pgai.action_failed
// - pgai.cmcs_work_order_resolved (Action Agent resolution poll side-channel)
// =============================================================================

param location string
param namespaceName string
param tags object = {}

resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2021-11-01' = {
  name: namespaceName
  location: location
  tags: tags
  sku: {
    name: 'Standard'  // Standard provides topics, subscriptions, filtered subscriptions
    tier: 'Standard'
  }
  properties: {
    zoneRedundant: true
    // Private endpoint for VNet-only access
  }
}

// Topics for the event-driven agent pipeline
resource anomaliesTopic 'Microsoft.ServiceBus/namespaces/topics@2021-11-01' = {
  parent: serviceBusNamespace
  name: 'pgai.anomalies'
  properties: {
    maxMessageSizeInKilobytes: 256
    maxSizeInMegabytes: 5120
    requiresDuplicateDetection: true
  }
}

resource diagnosisTopic 'Microsoft.ServiceBus/namespaces/topics@2021-11-01' = {
  parent: serviceBusNamespace
  name: 'pgai.diagnosis'
  properties: {
    maxMessageSizeInKilobytes: 256
    maxSizeInMegabytes: 5120
    requiresDuplicateDetection: true
  }
}

resource stageCompletedTopic 'Microsoft.ServiceBus/namespaces/topics@2021-11-01' = {
  parent: serviceBusNamespace
  name: 'pgai-stage_completed'
  properties: {
    maxMessageSizeInKilobytes: 256
    maxSizeInMegabytes: 5120
    requiresDuplicateDetection: true
  }
}

resource incidentApprovedTopic 'Microsoft.ServiceBus/namespaces/topics@2021-11-01' = {
  parent: serviceBusNamespace
  name: 'pgai.incident_approved'
  properties: {
    maxMessageSizeInKilobytes: 256
    maxSizeInMegabytes: 5120
    requiresDuplicateDetection: true
  }
}

resource actionCompletedTopic 'Microsoft.ServiceBus/namespaces/topics@2021-11-01' = {
  parent: serviceBusNamespace
  name: 'pgai.action_completed'
  properties: {
    maxMessageSizeInKilobytes: 256
    maxSizeInMegabytes: 5120
    requiresDuplicateDetection: true
  }
}

// Dead-letter topic (built-in for each topic) — used for handler errors

@description('Service Bus namespace name')
output namespaceName string = serviceBusNamespace.name

@description('Service Bus connection string (primary key injected via Key Vault by CI/CD)')
output serviceBusConnection string = ''

@description('Full namespace FQDN')
output namespaceFqdn string = '${serviceBusNamespace.name}.servicebus.windows.net'
