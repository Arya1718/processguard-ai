// =============================================================================
// Azure API Management
// Stands in for: nginx reverse proxy + middleware (.NET) as the API gateway
//
// In production, the .NET middleware is deployed inside the VNet behind APIM,
// which provides DDoS protection, rate limiting, and public ingress. The
// middleware still handles JWT validation and RBAC; APIM adds WAF + throttling.
// =============================================================================

param location string
param apiManagementName string
param publisher string
param vnetSubnetId string
param tags object = {}

resource apiManagement 'Microsoft.ApiManagement/service@2023-09-01' = {
  name: apiManagementName
  location: location
  tags: tags
  sku: {
    name: 'Premium_1'
    capacity: 1
  }
  properties: {
    publisher: {
      email: 'ops@processguard.ai'
      name: publisher
    }
    virtualNetworkType: 'External'
    virtualNetworkConfig: {
      subnetResourceId: vnetSubnetId
      internal: {
        dnsZoneId: 'default'
      }
    }
    enableClientCertificate: false
    hostnameConfigurations: [
      {
        type: 'Gateway'
        hostName: 'api.processguard.ai'
        keyVaultId: ''
        identityClientId: ''
        defaultSslBinding: true
      }
      {
        type: 'Proxy'
        hostName: 'api.processguard.ai'
        keyVaultId: ''
        identityClientId: ''
        defaultSslBinding: true
      }
    ]
  }
}

@description('API Management service resource ID')
output serviceId string = apiManagement.id

@description('API Management default proxy hostname')
output defaultHostName string = apiManagement.properties.hostnameConfigurations[1].hostName
