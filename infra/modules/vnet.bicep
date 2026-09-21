// =============================================================================
// Virtual Network with subnets for isolation
// Stands in for: Docker Compose networks (internal, cmms, frontend_net)
// =============================================================================

param location string
param vnetName string
param tags object = {}

resource vnet 'Microsoft.Network/virtualNetworks@2023-02-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.0.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'aks-subnet'
        properties: {
          addressPrefix: '10.0.1.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'postgres-subnet'
        properties: {
          addressPrefix: '10.0.2.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'redis-subnet'
        properties: {
          addressPrefix: '10.0.3.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'streaming-subnet'
        properties: {
          addressPrefix: '10.0.4.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'apim-subnet'
        properties: {
          addressPrefix: '10.0.5.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

@description('Resource ID of the AKS subnet')
output aksSubnetId string = vnet.properties.subnets[0].id
@description('Resource ID of the PostgreSQL subnet')
output dbSubnetId string = vnet.properties.subnets[1].id
@description('Resource ID of the Redis subnet')
output redisSubnetId string = vnet.properties.subnets[2].id
@description('Resource ID of the streaming subnet (Event Hubs + Service Bus)')
output streamingSubnetId string = vnet.properties.subnets[3].id
@description('Resource ID of the APIM subnet')
output apimSubnetId string = vnet.properties.subnets[4].id
