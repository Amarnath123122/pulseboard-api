<#
.SYNOPSIS
  Deploys the Pulseboard API to Azure: resource group -> infrastructure (Bicep) -> code -> smoke test.

.EXAMPLE
  ./scripts/azure-deploy.ps1 -OwnerEmail you@example.com
  ./scripts/azure-deploy.ps1 -OwnerEmail you@example.com -ResourceGroup rg-pulseboard -Location centralindia

  Requires the Azure CLI and `az login`. Generated secrets are kept in infra/secrets.local.json
  (git-ignored) so re-runs reuse them. Remove everything with: az group delete -n <ResourceGroup>
#>
param(
  [Parameter(Mandatory = $true)][string]$OwnerEmail,
  [string]$ResourceGroup = "rg-pulseboard",
  [string]$Location = "centralindia",
  [switch]$SkipInfra
)
$ErrorActionPreference = "Continue"  # native az warnings go to stderr; failures are caught by Assert-Ok
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

  Write-Host "==> Infrastructure (first run ~10 min)"
  az deployment group create `
    --resource-group $ResourceGroup `
    --name "pulseboard-infra" `
    --template-file (Join-Path $root "infra\main.bicep") `
    --parameters ownerEmail=$OwnerEmail `
                 postgresAdminPassword=$($secrets.postgresAdminPassword) `
                 jwtSecret=$($secrets.jwtSecret) `
                 serviceKey=$($secrets.serviceKey) `
    --output none
  Assert-Ok "Infrastructure deployment"
}

$out = az deployment group show --resource-group $ResourceGroup --name "pulseboard-infra" `
  --query properties.outputs --output json | ConvertFrom-Json
Assert-Ok "Reading deployment outputs"
$app = $out.apiAppName.value

Write-Host "==> Code -> $app"
$zip = Join-Path $env:TEMP "pulseboard-api.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Push-Location $root
try {
  # bsdtar writes forward-slash paths; Compress-Archive on PowerShell 5.1 writes backslashes Linux can't unpack.
  tar -a -c -f $zip --exclude "__pycache__" app migrations alembic.ini requirements.txt startup.sh
  Assert-Ok "Packaging"
} finally { Pop-Location }
az webapp deploy --resource-group $ResourceGroup --name $app --src-path $zip --type zip --timeout 900000 --track-status false --only-show-errors --output none
Assert-Ok "Code deployment"

Write-Host "==> Smoke test"
$ready = $false
for ($i = 0; $i -lt 30 -and -not $ready; $i++) {
  try {
    $r = Invoke-RestMethod "$($out.apiUrl.value)/readyz" -TimeoutSec 20
    $ready = $r.status -eq "ok"
  } catch { Start-Sleep -Seconds 10 }
}
if (-not $ready) { throw "API did not become ready; check: az webapp log tail -g $ResourceGroup -n $app" }

Write-Host ""
Write-Host "Deployed."
Write-Host "  API:        $($out.apiUrl.value)"
Write-Host "  WebSocket:  $($out.websocketUrl.value)"
Write-Host "  Key Vault:  $($out.keyVaultName.value)"
Write-Host "  Telemetry:  Application Insights '$($out.appInsightsName.value)'"
