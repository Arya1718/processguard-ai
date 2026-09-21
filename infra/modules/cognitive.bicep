// =============================================================================
// Azure Cognitive Services — OpenAI & Text Analytics
// Stands in for: llm_client/Groq (Prompt 3, docs/llm-provider.md)
//
// Provides the chat completions endpoint used by the Root-Cause Agent,
// Recommendation Agent, and the grounded Q&A ("Why?") endpoint.
// =============================================================================

param location string
param accountName string
param tags object = {}

resource cognitiveAccount 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'  // Standard tier — pay-as-you-go
  }
  properties: {
    // Public network access disabled — only reachable from the VNet
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      virtualNetworkRules: [
        // Allow requests from the VNet where AKS runs
        {
          id: '/subscriptions/${subscription().subscriptionId}/resourceGroups/${resourceGroup().name}/providers/Microsoft.Network/virtualNetworks/processguard-vnet/subnets/aks-subnet'
          ignoreMissingVnetServiceEndpoint: false
        }
      ]
    }
  }
}

@description('Azure OpenAI endpoint URL (for AKS env vars)')
output cognitiveEndpoint string = 'https://${cognitiveAccount.name}.openai.azure.com'

@description('Azure OpenAI API key (injected into Key Vault by CI/CD)')
output cognitiveKey string = 'placeholder-set-via-cd'

@description('Deployment name for the chat model')
output chatDeploymentName string = 'gpt-4o'

@description('Embedding model deployment (for Azure AI Search RAG pipeline)')
output embeddingDeploymentName string = 'text-embedding-3-small'
