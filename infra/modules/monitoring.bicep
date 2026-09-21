// =============================================================================
// Azure Monitor + Application Insights + Log Analytics
// Stands in for: Prometheus + Grafana + Alertmanager + OTel Collector (Prompt 9)
//
// Application Insights receives OpenTelemetry traces directly. Metrics flow
// through the Azure Monitor Metrics addon for AKS. Alert rules in Prometheus
// map to Azure Monitor metric alerts + Action Groups.
// =============================================================================

param location string
param workspaceName string
param appInsightsName string
param grafanaName string
param alertManagerEmail string
param tags object = {}

// Log Analytics workspace — central log store for all Azure Monitor data
resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2021-12-01-preview' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 90
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
      searchVersion: 1
    }
  }
}

// Application Insights — receives OTel traces + metrics + live metrics
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    Flow_Type: 'Bluefield'
    Request_Source: 'rest'
    SamplingPercentage: 100
    DisableIpMasking: false
  }
}

// Azure Managed Grafana (stands in for the OSS Grafana container)
resource grafana 'Microsoft.Dashboard/grafana/workspaces@2023-09-01' = {
  name: grafanaName
  location: location
  tags: tags
  properties: {
    apiKey: 'Disabled'
    autoProvisionFreeUsersAndGroups: false
    deterministicOutboundPath: false
    enabledComponents: ['AzureMonitor', 'ManagedPrometheus']
    grafanaVersion: '11.2.0'
    identity: {
      type: 'SystemAssigned'
    }
  }
}

// Action Group: alert routing to email + Slack webhook
resource alertActionGroup 'Microsoft.Insights/actionGroups@2021-09-01' = {
  name: '${workspaceName}-actions'
  location: location
  tags: tags
  properties: {
    groupShortName: 'pgai-ops'
    enabled: true
    emailReceivers: [
      {
        name: 'Ops Team'
        emailAddress: alertManagerEmail
        status: 'Active'
      }
    ]
    webhookReceivers: []
  }
}

// Alert: Action Agent failures (pgai_action_failed > 0)
resource alertActionFailures 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${workspaceName}-alert-action-failures'
  location: location
  tags: tags
  properties: {
    description: 'Action Agent reported a failure — external CMMS write failed'
    severity: 2
    enabled: true
    scopes: [
      appInsights.id
    ]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      allOf: [
        {
          criterionType: 'DynamicThresholdCriterion'
          metricName: 'pgai_action_failed'
          operator: 'GreaterOrLessThan'
          timeAggregation: 'Total'
          dimensions: []
          alertSensitivity: 'Medium'
          failingPeriodsWithDebounce: 1
        }
      ]
    }
    actions: [
      {
        actionGroupId: alertActionGroup.id
      }
    ]
  }
}

// Alert: Sensor stream lag (Event Hubs consumer lag > threshold)
resource alertStreamLag 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${workspaceName}-alert-stream-lag'
  location: location
  tags: tags
  properties: {
    description: 'Event Hub consumer lag exceeds threshold — Detection Agent is falling behind'
    severity: 3
    enabled: true
    scopes: [
      appInsights.id
    ]
    evaluationFrequency: 'PT30S'
    windowSize: 'PT2M'
    criteria: {
      allOf: [
        {
          metricName: 'IncomingMessages'
          operator: 'GreaterThan'
          threshold: 10000
          timeAggregation: 'Average'
        }
      ]
    }
    actions: [
      {
        actionGroupId: alertActionGroup.id
      }
    ]
  }
}

@description('Log Analytics workspace resource ID')
output logAnalyticsWorkspaceId string = logAnalyticsWorkspace.id

@description('Application Insights connection string (instrumentation key)')
output appInsightsConnectionString string = appInsights.properties.ConnectionString

@description('Grafana workspace resource ID')
output grafanaId string = grafana.id

@description('Action Group resource ID')
output actionGroupId string = alertActionGroup.id
