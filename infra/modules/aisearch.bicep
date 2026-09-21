// =============================================================================
// Azure AI Search Service
// Stands in for: app/rag/RetrievalIndex (TF-IDF, Prompt 3 — RAG)
//
// Hosts the SOP knowledge base index. The RetrievalIndex interface
// (index/search) is identical; only the backend storage engine changes.
// =============================================================================

param location string
param searchName string
param tags object = {}

resource searchService 'Microsoft.Search/searchServices@2023-02-01' = {
  name: searchName
  location: location
  tags: tags
  sku: {
    name: 'basic'
  }
  properties: {
    authOptions: {
      // API key auth for admin; managed identity for agent service
      // In production: disable API key auth, use managed identity only
    }
    partitionCount: 1
    replicaCount: 1
    publicNetworkAccess: 'Disabled'
    identity: {
      type: 'SystemAssigned'
    }
  }
}

@description('Search service endpoint URL')
output searchEndpoint string = 'https://${searchService.name}.search.windows.net'

@description('Search service admin key (retrieved from keys API; stored in Key Vault by CI/CD)')
output searchKey string = 'placeholder-set-via-cd'
