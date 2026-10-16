<#
.SYNOPSIS
  Pauses Azure billing without deleting anything: stops the API, moves its plan to the Free tier, stops Postgres.
  Resume with ./scripts/azure-deploy.ps1 (it restores the B1 tier and starts everything).
  Note: Azure restarts a stopped Postgres automatically after 7 days.
#>
param([string]$ResourceGroup = "rg-pulseboard")
$ErrorActionPreference = "Continue"

$app = az webapp list -g $ResourceGroup --query "[0].name" -o tsv
$plan = az appservice plan list -g $ResourceGroup --query "[0].name" -o tsv
$pg = az postgres flexible-server list -g $ResourceGroup --query "[0].name" -o tsv

if ($app) {
  az webapp stop -g $ResourceGroup -n $app --output none
  az webapp config set -g $ResourceGroup -n $app --always-on false --output none  # Free tier has no Always On
}
if ($plan) { az appservice plan update -g $ResourceGroup -n $plan --sku F1 --output none }
if ($pg) { az postgres flexible-server stop -g $ResourceGroup -n $pg --output none }

Write-Host "API: $(az webapp show -g $ResourceGroup -n $app --query state -o tsv)"
Write-Host "Plan: $(az appservice plan show -g $ResourceGroup -n $plan --query sku.name -o tsv)"
Write-Host "Postgres: $(az postgres flexible-server show -g $ResourceGroup -n $pg --query state -o tsv)"
