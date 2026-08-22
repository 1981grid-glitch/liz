# push-through-2026-08-22.ps1
#
# One command: patch the canonical source, prove it with the regression suites,
# then deploy. Stops at the first failure and deploys nothing.
#
#   .\push-through-2026-08-22.ps1 -WhatIf   # dry run, touches nothing
#   .\push-through-2026-08-22.ps1           # patch + test + deploy
#
# DELIBERATELY PURE ASCII. DEPLOY_V18.md section 9 documents the trap: without a
# UTF-8 BOM, Windows PowerShell 5.1 reads a file as cp1252, and an em-dash's third
# byte (0x94) becomes a curly quote, which PS accepts as a string delimiter - the
# string closes early and the rest of the line parses as code. No non-ASCII byte
# here means no BOM is needed and both 5.1 and pwsh 7 parse it identically.

param([switch]$WhatIf)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Fail($msg) {
    Write-Host ""
    Write-Host "STOPPED: $msg" -ForegroundColor Red
    Write-Host "Nothing was deployed." -ForegroundColor Red
    exit 1
}

Write-Host "Throne MCP push-through, 2026-08-22" -ForegroundColor Cyan
Write-Host "Folder: $PSScriptRoot"
Write-Host ""

# --- preflight ------------------------------------------------------------
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Fail "python not found on PATH" }
if (-not (Test-Path .\throne_mcp_server.py)) {
    Fail "throne_mcp_server.py not here. Run this from the canonical throne-mcp-v18-expanded folder."
}
if (-not (Test-Path .\apply-fixes-2026-08-22.py)) {
    Fail "apply-fixes-2026-08-22.py not here. It should have synced down from OneDrive."
}
if (-not (Test-Path .\deploy-throne-mcp.ps1)) {
    Fail "deploy-throne-mcp.ps1 not here."
}

# Guard the hazard the runbook calls out: a second copy of the server source in
# this tree means someone diverged it, and the build target becomes ambiguous.
$copies = @(Get-ChildItem -Filter "throne_mcp_server.py" -Recurse -ErrorAction SilentlyContinue)
if ($copies.Count -gt 1) {
    Write-Host "WARNING: more than one throne_mcp_server.py under this folder:" -ForegroundColor Yellow
    $copies | ForEach-Object { Write-Host ("  " + $_.FullName) -ForegroundColor Yellow }
    Fail "collapse the duplicate before building (DEPLOY_V18.md section 0)"
}

# --- 1/3  patch -----------------------------------------------------------
Write-Host "=== 1/3  Patch ===" -ForegroundColor Cyan
python .\apply-fixes-2026-08-22.py
if ($LASTEXITCODE -ne 0) { Fail "the patch step refused or failed" }

# --- 2/3  tests -----------------------------------------------------------
Write-Host ""
Write-Host "=== 2/3  Regression suites ===" -ForegroundColor Cyan
foreach ($t in @("test_todo_v19.py", "test_phi_audit.py", "test_mcp_fixes_2026_08_22.py")) {
    if (-not (Test-Path ".\$t")) {
        if ($t -eq "test_mcp_fixes_2026_08_22.py") {
            Write-Host "  SKIP  $t (not in this folder; it is in the liz repo)" -ForegroundColor Yellow
            continue
        }
        Fail "$t is missing"
    }
    Write-Host "  running $t ..."
    python ".\$t" | Select-Object -Last 3
    if ($LASTEXITCODE -ne 0) { Fail "$t FAILED" }
}
Write-Host "  all suites green" -ForegroundColor Green

# --- 3/3  deploy ----------------------------------------------------------
Write-Host ""
Write-Host "=== 3/3  Deploy ===" -ForegroundColor Cyan
$az = Get-Command az -ErrorAction SilentlyContinue
if (-not $az) { Fail "az CLI not found. The patch and tests are done; only the deploy is left." }

if ($WhatIf) {
    Write-Host "(dry run)" -ForegroundColor Yellow
    .\deploy-throne-mcp.ps1 -WhatIf
} else {
    .\deploy-throne-mcp.ps1
}
if ($LASTEXITCODE -ne 0) { Fail "deploy-throne-mcp.ps1 reported a failure" }

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Now toggle the Throne connector OFF and ON in claude.ai, then start a NEW"
Write-Host "conversation. calendar_create_event gained a parameter and the tool manifest"
Write-Host "is cached per connection, so a reconnect alone will not pick it up."
