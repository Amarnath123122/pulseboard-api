<#
.SYNOPSIS
  Deploys (or restarts) Pulseboard on Azure: infrastructure -> wake stopped services -> API -> web UI -> smoke test.

.EXAMPLE
  ./scripts/azure-deploy.ps1 -OwnerEmail you@example.com                 # everything
  ./scripts/azure-deploy.ps1 -OwnerEmail you@example.com -SkipInfra      # code only
  ./scripts/azure-deploy.ps1 -OwnerEmail you@example.com -DeployRedis    # also Azure Managed Redis (extra cost)

  Requires the Azure CLI (az login), Node 18+, and pulseboard-web checked out next to this repo.
  Secrets are generated once into infra/secrets.local.json (git-ignored). Pause billing: ./scripts/azure-stop.ps1
#>
param(
  [Parameter(Mandatory = $true)][string]$OwnerEmail,
  [string]$ResourceGroup = "rg-pulseboard",
  [string]$Location = "centralindia",
  [string]$WebRepo = (Join-Path $PSScriptRoot "..\..\pulseboard-web"),
  [switch]$SkipInfra,
  [switch]$SkipWeb,
  [switch]$DeployRedis
)
$ErrorActionPreference = "Continue"  # az writes warnings to stderr; real failures are caught by Assert-Ok
$root = Resolve-Path (Join-Path $PSScriptRoot "..")

function New-Secret([int]$bytes = 32) {
  $b = New-Object byte[] $bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
  return ([Convert]::ToBase64String($b) -replace '[+/=]', '')
}
function Assert-Ok([string]$what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

$secretsFile = Join-Path $root "infra\secrets.local.json"
if (Test-Path $secretsFile) {
  $secrets = Get-Content $secretsFile -Raw | ConvertFrom-Json
} else {
  $secrets = [pscustomobject]@{
    postgresAdminPassword = "Pb1!" + (New-Secret 18)
    jwtSecret             = New-Secret 48
    serviceKey            = New-Secret 32
  }
  $secrets | ConvertTo-Json | Out-File -Encoding utf8 $secretsFile
}

if (-not $SkipInfra) {
  Write-Host "==> Resource group $ResourceGroup ($Location)"
  az group create --name $ResourceGroup --location $Location --tags app=pulseboard --output none
  Assert-Ok "az group create"

  # A stopped Postgres server rejects deployments, so wake it first.
  $pg = az postgres flexible-server list -g $ResourceGroup --query "[0].{name:name,state:state}" -o json 2>$null | ConvertFrom-Json
  if ($pg -and $pg.state -eq "Stopped") {
    Write-Host "==> Starting Postgres $($pg.name)"
    az postgres flexible-server start -g $ResourceGroup -n $pg.name --output none
    Assert-Ok "Postgres start"
  }

  Write-Host "==> Infrastructure (first run ~10 min)"
  az deployment group create `
    --resource-group $ResourceGroup `
    --name "pulseboard-infra" `
    --template-file (Join-Path $root "infra\main.bicep") `
    --parameters ownerEmail=$OwnerEmail `
                 postgresAdminPassword=$($secrets.postgresAdminPassword) `
                 jwtSecret=$($secrets.jwtSecret) `
                 serviceKey=$($secrets.serviceKey) `
                 deployRedis=$($DeployRedis.IsPresent.ToString().ToLower()) `
    --output none
  Assert-Ok "Infrastructure deployment"
}

$out = az deployment group show --resource-group $ResourceGroup --name "pulseboard-infra" `
  --query properties.outputs --output json | ConvertFrom-Json
Assert-Ok "Reading deployment outputs"
$app = $out.apiAppName.value
az webapp start -g $ResourceGroup -n $app --output none

Write-Host "==> API code -> $app"
$zip = Join-Path $env:TEMP "pulseboard-api.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Push-Location $root
try {
  # bsdtar writes forward-slash paths; Compress-Archive on PowerShell 5.1 writes backslashes Linux can't unpack.
  tar -a -c -f $zip --exclude "__pycache__" app migrations alembic.ini requirements.txt startup.sh
  Assert-Ok "Packaging"
} finally { Pop-Location }
# Startup (pip install + migrations) can outlast the CLI's own status polling, so readiness is checked below instead.
az webapp deploy --resource-group $ResourceGroup --name $app --src-path $zip --type zip --timeout 900000 `
  --track-status false --only-show-errors --output none
Assert-Ok "API deployment"

Write-Host "==> Waiting for the API"
$ready = $false
for ($i = 0; $i -lt 40 -and -not $ready; $i++) {
  try { $ready = (Invoke-RestMethod "$($out.apiUrl.value)/readyz" -TimeoutSec 20).status -eq "ok" } catch { Start-Sleep 15 }
}
if (-not $ready) { throw "API did not become ready; check: az webapp log tail -g $ResourceGroup -n $app" }

if (-not $SkipWeb) {
  Write-Host "==> Web UI -> $($out.webAppName.value)"
  $token = az staticwebapp secrets list -n $out.webAppName.value -g $ResourceGroup --query properties.apiKey -o tsv
  Assert-Ok "Reading Static Web Apps token"
  Push-Location $WebRepo
  try {
    npm ci --no-audit --no-fund; Assert-Ok "npm ci"
    npm run build; Assert-Ok "Web build"
    # Runtime config: the one build artifact is pointed at this environment's API.
    @{ apiUrl = $out.apiUrl.value; wsUrl = $out.websocketUrl.value } | ConvertTo-Json |
      Out-File -Encoding ascii "dist\pulseboard\browser\config.json"
    npx --yes @azure/static-web-apps-cli@2.0.2 deploy ./dist/pulseboard/browser --deployment-token $token --env production
    Assert-Ok "Web deployment"
  } finally { Pop-Location }
}

Write-Host ""
Write-Host "Deployed."
Write-Host "  Web:        $($out.webUrl.value)"
Write-Host "  API:        $($out.apiUrl.value)"
Write-Host "  WebSocket:  $($out.websocketUrl.value)"
