// =============================================================================
// Azure Cache for Redis
// Stands in for: redis (Docker Compose) — used for both pub/sub (Service Bus
// stand-in) AND the Redis Streams (Event Hub stand-in) until they are
// migrated to dedicated Azure services.
// =============================================================================

param location string
param redisName string
param subnetId string
param tags object = {}

resource redisCache 'Microsoft.Cache/Redis/stable@2023-06-01' = {
  name: redisName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'Premium'
      family: 'P'
      capacity: 2
    }
    enableRedisCluster: false
    minimumTlsVersion: '1.2'
    redisConfiguration: {
      enableNonSslPort: false
      maxmemoryReserved: 10
      maxmemoryDelta: 2
      maxmemoryPolicy: 'allkeys-lru'
      slowlogLogLimit: 1024
      slowlogSlowTime: 10000
    }
    // Private endpoint for VNet-only access — created as a separate resource
  }
}

// Private endpoint for VNet-only access
resource privateEndpoint 'Microsoft.Network/privateEndpoints@2023-02-01' = {
  name: '${redisName}-pe'
  location: location
  properties: {
    subnet: {
      id: subnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'pl-redis'
        properties: {
          privateLinkServiceId: redisCache.id
          groupIds: ['redisCache']
        }
      }
    ]
  }
}

@description('Redis hostname (for AKS)')
output redisHost string = redisCache.hostname

@description('Redis port (always 6380 for SSL on Premium tier)')
output redisPort int = 6380

@description('Redis primary key (retrieved from Redis access keys; stored in Key Vault by CI/CD)')
output redisPrimaryKey string = 'placeholder-set-via-cd'
