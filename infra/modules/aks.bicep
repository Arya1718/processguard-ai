// =============================================================================
// AKS Cluster — hosts the agent-service, simulator, and mock-erp-cmms containers
// Stands in for: docker compose services (agent-service, simulator, mock-erp-cmms)
//
// In production, the .NET middleware and React frontend run behind Azure
// Front Door + Application Gateway; the agent-service runs on AKS with managed
// identity pulling secrets from Key Vault. The mock-erp-cmms is replaced by
// Buckman's real ERP/CMMS (out of scope for this template).
// =============================================================================

param location string
param aksName string
param dnsPrefix string
param postgresHost string
param postgresPort int
param postgresUser string
param postgresDb string
param redisHost string
param redisPort int
param serviceBusConnection string
param eventHubConnection string
param aiSearchEndpoint string
param aiSearchKey string
param cognitiveEndpoint string
param cognitiveKey string
param storageConnection string
param keyVaultUri string
param oidcAuthority string
param oidcAudience string
param otelEndpoint string
param subnetId string
param logAnalyticsWorkspaceId string
param tags object = {}

resource aksCluster 'Microsoft.ContainerService/managedClusters@2024-06-01' = {
  name: aksName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    dnsPrefix: dnsPrefix
    agentPoolProfiles: [
      {
        name: 'systempool'
        count: 2
        vmSize: 'Standard_D4s_v5'
        osType: 'Linux'
        mode: 'System'
        enableNodePublicIP: false
        vnetSubnetID: subnetId
        type: 'VirtualMachineScaleSets'
        orchestratorType: 'Kubernetes'
        maxPods: 110
        osDiskSizeGB: 128
      }
    ]
    oidcIssuerProfile: {
      enabled: true
    }
    workloadIdentityProfile: {
      enabled: true
    }
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      serviceCidr: '10.240.0.0/16'
      dnsServiceIP: '10.240.0.10'
      dockerBridgeCidr: '172.17.0.1/16'
      outboundType: 'loadBalancer'
    }
    addonProfiles: {
      omsagent: {
        enabled: true
        config: {
          'log-analytics-workspace-id': logAnalyticsWorkspaceId
        }
      }
    }
    kubernetesVersion: '1.30'
    enableRBAC: true
  }
}

// Managed Identity for the agent-service pods
// This identity gets Key Vault access to retrieve secrets at runtime
resource agentServiceIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${aksName}-agent-mi'
  location: location
  tags: tags
  properties: {}
}

@description('AKS cluster resource ID')
output aksClusterId string = aksCluster.id

@description('AKS cluster FQDN for ingress')
output aksFqdn string = aksCluster.properties.fqdn

@description('Managed identity client ID for the agent service')
output agentServiceIdentityClientId string = agentServiceIdentity.properties.clientId

@description('Managed identity principal ID for the agent service')
output agentServiceIdentityPrincipalId string = agentServiceIdentity.principalId
