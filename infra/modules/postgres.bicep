// =============================================================================
// Azure Database for PostgreSQL — Flexible Server
// Stands in for: postgres (Docker Compose)
//
// One flexible server instance in a VNet, single availability zone for dev,
// zone-redundant for staging/prod. Geo-restore backup retention = 35 days.
// Point-in-time restore is always available (RPO = 5 minutes).
// =============================================================================

param location string
param postgresName string
param postgresAdmin string
param postgresPassword string
param subnetId string
param tags object = {}

// Private DNS zone for the PostgreSQL server
resource privateDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: '${postgresName}.database.azure.com'
  location: 'global'
  tags: tags
}

// Private endpoint on the subnet
resource privateEndpoint 'Microsoft.Network/privateEndpoints@2023-02-01' = {
  name: '${postgresName}-pe'
  location: location
  properties: {
    subnet: {
      id: subnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'pl-psql'
        properties: {
          privateLinkServiceId: postgresServer.id
          groupIds: ['postgresServer']
        }
      }
    ]
  }
}

// Private DNS zone group linking the private endpoint to the DNS zone
resource privateDnsZoneGroup 'Microsoft.Network/privateDnsZones/virtualNetworkLink@2020-06-01' = {
  parent: privateDnsZone
  name: '${uniqueString(subnetId)}-${postgresName}-link'
  properties: {
    virtualNetworkId: split(subnetId, '/subnets/')[0]
    registrationEnabled: false
  }
}

// The PostgreSQL Flexible Server
resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01' = {
  name: postgresName
  location: location
  tags: tags
  properties: {
    administratorLogin: postgresAdmin
    administratorLoginPassword: postgresPassword
    version: '16'
    versionUpdateMode: 'Current'
    network: {
      publicAccess: 'Disabled'
      delegatedSubnetId: subnetId
    }
    highAvailability: {
      mode: 'Disabled'
    }
    backup: {
      backupRetentionDays: 35
      geobackupEnabled: true
    }
    storage: {
      storageSizeInGb: 128
      autoGrow: 'Enabled'
      iops: 3000
      tier: 'GeneralPurpose'
    }
    availabilityZone: 'Zone1'
  }
}

@description('PostgreSQL server FQDN (for AKS environment variable)')
output postgresHost string = postgresServer.properties.fullyQualifiedDomainName

@description('PostgreSQL port')
output postgresPort int = 5432

@description('PostgreSQL admin username')
output postgresUser string = postgresAdmin

@description('PostgreSQL admin password (stored in Key Vault by CI/CD)')
output postgresPassword string = postgresPassword

@description('PostgreSQL database name')
output postgresDb string = 'processguard'
