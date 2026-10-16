// Pulseboard API on Azure, sized for a free-trial / low-cost subscription.
//
//   Client ──HTTPS/WSS──► App Service (Linux B1, Python 3.12, managed identity)
//                              │            │                 │
//                              ▼            ▼                 ▼
//                  PostgreSQL Flexible   Storage Queue     Key Vault (secrets via
//                  (B1ms, free 750h)    (board reviews)    Key Vault references)
//
//   Application Insights (workspace-based, daily cap) + a 5xx alert + a budget alert watch it.
//   Scale up later by changing appServiceSku (S1/P0v3 unlock autoscale and slots).

targetScope = 'resourceGroup'

param location string = resourceGroup().location

@description('B1 is the cheapest tier with Always On (needed for the background review worker).')
param appServiceSku string = 'B1'

param postgresAdminLogin string = 'pbadmin'

@secure()
param postgresAdminPassword string

@secure()
param jwtSecret string

@secure()
param serviceKey string

@description('Demo sign-in endpoint; turn off once an identity provider is integrated.')
param enableDemoSignIn bool = true

@description('Static Web Apps (free tier) is only offered in some regions.')
@allowed(['westus2', 'centralus', 'eastus2', 'westeurope', 'eastasia'])
param webLocation string = 'eastasia'

@description('Receives alert and budget emails.')
param ownerEmail string

@description('Monthly budget for this resource group, in the subscription currency.')
param monthlyBudget int = 2000

param deployBudget bool = true
param budgetStartDate string = utcNow('yyyy-MM-01')

param tags object = { app: 'pulseboard', env: 'prod' }

var suffix = uniqueString(resourceGroup().id)
var dbName = 'pulseboard'
var queueName = 'board-reviews'
var roles = {
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
}

// ---------- Monitoring ----------
resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'pb-logs-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json('0.15') } // stays inside the free 5 GB/month
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'pb-insights-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    SamplingPercentage: 50
  }
}

resource oncall 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'pb-oncall'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'pb-oncall'
    enabled: true
    emailReceivers: [{ name: 'owner', emailAddress: ownerEmail, useCommonAlertSchema: true }]
  }
}

// ---------- Data ----------
resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: 'pb-pg-${suffix}'
  location: location
  tags: tags
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: 32, autoGrow: 'Disabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: dbName
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}

// Lets App Service reach the server. Next step for stricter isolation: VNet integration + private access.
resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'pbq${suffix}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false // managed identity only
    supportsHttpsTrafficOnly: true
  }
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource reviewQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: queueName
}

// ---------- Secrets ----------
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'pb-kv-${suffix}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    softDeleteRetentionInDays: 7
  }
}

resource dbUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'database-url'
  properties: {
    value: 'postgresql+asyncpg://${postgresAdminLogin}:${uriComponent(postgresAdminPassword)}@${postgres.properties.fullyQualifiedDomainName}:5432/${dbName}?ssl=require'
  }
}

resource jwtSecretRef 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'jwt-secret'
  properties: { value: jwtSecret }
}

resource serviceKeyRef 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'service-key'
  properties: { value: serviceKey }
}

// ---------- Web UI ----------
resource web 'Microsoft.Web/staticSites@2023-12-01' = {
  name: 'pb-web-${suffix}'
  location: webLocation
  tags: tags
  sku: { name: 'Free', tier: 'Free' }
  properties: {}
}

// ---------- API ----------
resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'pb-plan-${suffix}'
  location: location
  tags: tags
  kind: 'linux'
  sku: { name: appServiceSku }
  properties: { reserved: true }
}

resource api 'Microsoft.Web/sites@2023-12-01' = {
  name: 'pb-api-${suffix}'
  location: location
  tags: tags
  kind: 'app,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    keyVaultReferenceIdentity: 'SystemAssigned'
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'bash startup.sh'
      alwaysOn: true
      webSocketsEnabled: true
      http20Enabled: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/healthz'
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'WEB_CONCURRENCY', value: '2' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'PB_ENVIRONMENT', value: 'production' }
        { name: 'PB_LOG_JSON', value: 'true' }
        { name: 'PB_DATABASE_URL', value: '@Microsoft.KeyVault(SecretUri=${dbUrlSecret.properties.secretUri})' }
        { name: 'PB_JWT_SECRET', value: '@Microsoft.KeyVault(SecretUri=${jwtSecretRef.properties.secretUri})' }
        { name: 'PB_SERVICE_KEY', value: '@Microsoft.KeyVault(SecretUri=${serviceKeyRef.properties.secretUri})' }
        { name: 'PB_DB_AUTO_CREATE', value: 'false' }
        { name: 'PB_ENABLE_DEV_TOKEN', value: string(enableDemoSignIn) }
        { name: 'PB_QUEUE_BACKEND', value: 'azure' }
        { name: 'PB_REVIEW_QUEUE_NAME', value: queueName }
        { name: 'PB_AZURE_QUEUE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.queue }
        { name: 'PB_CORS_ORIGINS', value: '["https://${web.properties.defaultHostname}","http://localhost:5173"]' }
      ]
    }
  }
}

resource apiReadsSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, api.id, roles.keyVaultSecretsUser)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: api.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource apiUsesQueue 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, api.id, roles.storageQueueDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageQueueDataContributor)
    principalId: api.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource serverErrorsAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'pb-api-5xx'
  location: 'global'
  tags: tags
  properties: {
    description: 'API returned more than 10 server errors in 5 minutes'
    severity: 2
    enabled: true
    scopes: [api.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'http5xx'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'Http5xx'
          operator: 'GreaterThan'
          threshold: 10
          timeAggregation: 'Total'
        }
      ]
    }
    actions: [{ actionGroupId: oncall.id }]
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-05-01' = if (deployBudget) {
  name: 'pb-monthly-budget'
  properties: {
    category: 'Cost'
    amount: monthlyBudget
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual50: {
        enabled: true, operator: 'GreaterThan', threshold: 50, thresholdType: 'Actual', contactEmails: [ownerEmail]
      }
      forecast100: {
        enabled: true, operator: 'GreaterThan', threshold: 100, thresholdType: 'Forecasted', contactEmails: [ownerEmail]
      }
    }
  }
}

output apiAppName string = api.name
output apiUrl string = 'https://${api.properties.defaultHostName}'
output websocketUrl string = 'wss://${api.properties.defaultHostName}/ws/events'
output webAppName string = web.name
output webUrl string = 'https://${web.properties.defaultHostname}'
output keyVaultName string = vault.name
output postgresHost string = postgres.properties.fullyQualifiedDomainName
output appInsightsName string = insights.name
