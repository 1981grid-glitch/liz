# deploy-onenote-mcp.ps1
#
# Build, push, and deploy onenote-mcp to Azure Container Apps.
#
#   .\deploy-onenote-mcp.ps1 -WhatIf    # dry run, changes nothing
#   .\deploy-onenote-mcp.ps1            # next free tag, auto-detected
#
# DELIBERATELY PURE ASCII, same reason as push-through-2026-08-22.ps1: without a UTF-8
# BOM, Windows PowerShell 5.1 reads a file as cp1252, and an em-dash's third byte (0x94)
# becomes a curly quote, which PS accepts as a string delimiter - the string closes early
# and the rest of the line parses as code. No non-ASCII byte here means no BOM is needed
# and both 5.1 and pwsh 7 parse it identically.

param(
    [switch]$WhatIf,
    [string]$ResourceGroup = $env:AE_RESOURCE_GROUP,
    [string]$Registry      = $env:AE_REGISTRY,
    [string]$AppName       = "onenote-mcp",
    [string]$Environment   = $env:AE_ACA_ENVIRONMENT,
    [string]$Location      = "eastus2"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Fail($msg) {
    Write-Host ""
    Write-Host "STOPPED: $msg" -ForegroundColor Red
    Write-Host "Nothing was deployed." -ForegroundColor Red
    exit 1
}

Write-Host "onenote-mcp deploy" -ForegroundColor Cyan
Write-Host "Folder: $PSScriptRoot"
Write-Host ""

# --- preflight ------------------------------------------------------------
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { Fail "az CLI not found on PATH" }
if (-not (Test-Path .\onenote_mcp_server.py)) { Fail "onenote_mcp_server.py not here" }
if (-not (Test-Path .\Dockerfile))            { Fail "Dockerfile not here" }
if (-not $ResourceGroup) { Fail "resource group not set (pass -ResourceGroup or set AE_RESOURCE_GROUP)" }
if (-not $Registry)      { Fail "registry not set (pass -Registry or set AE_REGISTRY)" }
if (-not $Environment)   { Fail "ACA environment not set (pass -Environment or set AE_ACA_ENVIRONMENT)" }

# Secrets come from the environment, never from this file and never from the image.
$TenantId     = $env:AE_ONENOTE_TENANT_ID
$ClientId     = $env:AE_ONENOTE_CLIENT_ID
$ClientSecret = $env:AE_ONENOTE_CLIENT_SECRET
if (-not $TenantId)     { Fail "AE_ONENOTE_TENANT_ID not set" }
if (-not $ClientId)     { Fail "AE_ONENOTE_CLIENT_ID not set" }
if (-not $ClientSecret) { Fail "AE_ONENOTE_CLIENT_SECRET not set" }

# --- tag ------------------------------------------------------------------
# Compute the next free 1.x tag rather than hardcoding one, same discipline as the Throne
# deploy script - reusing a live tag makes "which image is running" unanswerable.
$existing = @()
try {
    $existing = az acr repository show-tags --name $Registry --repository $AppName `
                    --output tsv 2>$null
} catch { $existing = @() }
$next = 1
foreach ($t in $existing) {
    if ($t -match '^1\.(\d+)$') { $n = [int]$Matches[1]; if ($n -ge $next) { $next = $n + 1 } }
}
$Tag = "1.$next"
$Image = "$Registry.azurecr.io/$AppName`:$Tag"
Write-Host "Image: $Image" -ForegroundColor Yellow

if ($WhatIf) {
    Write-Host ""
    Write-Host "WhatIf: would build+push $Image, then create/update container app '$AppName'" -ForegroundColor Yellow
    Write-Host "WhatIf: nothing was changed." -ForegroundColor Yellow
    exit 0
}

# --- 1/3  build + push ----------------------------------------------------
Write-Host ""
Write-Host "=== 1/3  Build and push ===" -ForegroundColor Cyan
az acr build --registry $Registry --image "$AppName`:$Tag" --file Dockerfile .
if ($LASTEXITCODE -ne 0) { Fail "az acr build failed" }

# --- 2/3  create or update ------------------------------------------------
Write-Host ""
Write-Host "=== 2/3  Deploy ===" -ForegroundColor Cyan

$exists = $false
try {
    az containerapp show --name $AppName --resource-group $ResourceGroup --output none 2>$null
    $exists = ($LASTEXITCODE -eq 0)
} catch { $exists = $false }

# PUBLIC_BASE_URL must be this app's own FQDN, and on a first deploy the FQDN does not
# exist until the app does. So: create first with a placeholder, read the FQDN back, then
# set it. Getting this wrong yields a server whose OAuth metadata advertises the wrong
# issuer, which fails at the callback rather than at startup - i.e. it looks like an
# Entra misconfiguration and is not one.
if (-not $exists) {
    Write-Host "Creating container app '$AppName'..." -ForegroundColor Yellow
    az containerapp create `
        --name $AppName --resource-group $ResourceGroup --environment $Environment `
        --image $Image --target-port 8000 --ingress external --transport http `
        --min-replicas 1 --max-replicas 1 `
        --secrets "oauth-client-secret=$ClientSecret" `
        --env-vars "AZURE_TENANT_ID=$TenantId" "OAUTH_CLIENT_ID=$ClientId" `
                   "OAUTH_CLIENT_SECRET=secretref:oauth-client-secret" `
                   "PUBLIC_BASE_URL=https://placeholder.invalid" `
        --output none
    if ($LASTEXITCODE -ne 0) { Fail "containerapp create failed" }
} else {
    Write-Host "Updating container app '$AppName' to $Tag..." -ForegroundColor Yellow
    az containerapp secret set --name $AppName --resource-group $ResourceGroup `
        --secrets "oauth-client-secret=$ClientSecret" --output none
    if ($LASTEXITCODE -ne 0) { Fail "containerapp secret set failed" }
    az containerapp update --name $AppName --resource-group $ResourceGroup `
        --image $Image --output none
    if ($LASTEXITCODE -ne 0) { Fail "containerapp update failed" }
}

$Fqdn = az containerapp show --name $AppName --resource-group $ResourceGroup `
            --query "properties.configuration.ingress.fqdn" --output tsv
if (-not $Fqdn) { Fail "could not read the app FQDN back" }
$BaseUrl = "https://$Fqdn"

# min-replicas 1 is not a performance nicety: a cold start stalls the OAuth handshake
# long enough that claude.ai reports the connector as broken.
az containerapp update --name $AppName --resource-group $ResourceGroup `
    --min-replicas 1 `
    --set-env-vars "AZURE_TENANT_ID=$TenantId" "OAUTH_CLIENT_ID=$ClientId" `
                   "OAUTH_CLIENT_SECRET=secretref:oauth-client-secret" `
                   "PUBLIC_BASE_URL=$BaseUrl" `
    --output none
if ($LASTEXITCODE -ne 0) { Fail "could not set PUBLIC_BASE_URL" }

# Optional: site alias for the MasterChiefsThrone site-hosted notebooks.
if ($env:AE_THRONE_SITE_ID) {
    az containerapp update --name $AppName --resource-group $ResourceGroup `
        --set-env-vars "THRONE_SITE_ID=$($env:AE_THRONE_SITE_ID)" --output none
}

# --- 3/3  verify ----------------------------------------------------------
Write-Host ""
Write-Host "=== 3/3  Verify ===" -ForegroundColor Cyan
Start-Sleep -Seconds 15
try {
    $health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 45
    Write-Host "  /health: $($health.status)" -ForegroundColor Green
    Write-Host "  entra redirect uri: $($health.entra_redirect_uri)" -ForegroundColor Green
} catch {
    Fail "deployed, but $BaseUrl/health did not answer: $_"
}

Write-Host ""
Write-Host "Deployed $Image" -ForegroundColor Green
Write-Host ""
Write-Host "NEXT, and the build is not finished without it:" -ForegroundColor Cyan
Write-Host "  1. Register this EXACT Web redirect URI on the Entra app 'AE OneNote MCP':"
Write-Host "       $BaseUrl/auth/callback" -ForegroundColor Yellow
Write-Host "     (NOT the claude.ai callback - see BUILD-NOTES section 3.)"
Write-Host "  2. Add the connector in claude.ai with MCP server URL:"
Write-Host "       $BaseUrl/mcp" -ForegroundColor Yellow
Write-Host "  3. Connect; expect a Microsoft sign-in prompt. Completing it proves the chain."
