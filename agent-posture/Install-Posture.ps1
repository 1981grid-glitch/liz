# Install-Posture.ps1
#
# Places the posture renderings on this workstation. Run from a git clone of
# the liz repo:
#
#   git pull
#   .\agent-posture\Install-Posture.ps1 -WhatIf    # dry run, touches nothing
#   .\agent-posture\Install-Posture.ps1            # install
#   .\agent-posture\Install-Posture.ps1 -Repo C:\path\to\repo
#
# -Repo additionally drops .github\copilot-instructions.md into that repo.
#
# DELIBERATELY PURE ASCII. DEPLOY_V18.md section 9 documents the trap: without a
# UTF-8 BOM, Windows PowerShell 5.1 reads a file as cp1252, and an em-dash's third
# byte (0x94) becomes a curly quote, which PS accepts as a string delimiter - the
# string closes early and the rest of the line parses as code. No non-ASCII byte
# here means no BOM is needed and both 5.1 and pwsh 7 parse it identically.
#
# The files this script COPIES do contain non-ASCII text. That is fine: Copy-Item
# moves bytes and never parses them. Do not inline their contents into this file.

param(
    [switch]$WhatIf,
    [string]$Repo
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Fail($msg) {
    Write-Host ""
    Write-Host "STOPPED: $msg" -ForegroundColor Red
    Write-Host "Nothing further was installed." -ForegroundColor Red
    exit 1
}

function Place($src, $dst, $label) {
    if (-not (Test-Path $src)) { Fail "missing source file: $src" }

    $dstDir = Split-Path -Parent $dst
    $exists = Test-Path $dst

    if ($exists) {
        $same = (Get-FileHash $src).Hash -eq (Get-FileHash $dst).Hash
        if ($same) {
            Write-Host "  [same]    $label" -ForegroundColor DarkGray
            Write-Host "            $dst"
            return
        }
    }

    if ($WhatIf) {
        $verb = if ($exists) { "would REPLACE" } else { "would create" }
        Write-Host "  [$verb] $label" -ForegroundColor Yellow
        Write-Host "            $dst"
        if ($exists) { Write-Host "            (a backup would be taken first)" -ForegroundColor DarkGray }
        return
    }

    if (-not (Test-Path $dstDir)) {
        New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
    }

    if ($exists) {
        # Never overwrite an existing posture file without keeping the old one.
        # Timestamped, so repeated installs do not clobber earlier backups.
        $stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
        $backup = "$dst.bak-$stamp"
        Copy-Item $dst $backup
        Write-Host "  [backup]  $backup" -ForegroundColor DarkGray
    }

    Copy-Item $src $dst -Force
    Write-Host "  [ok]      $label" -ForegroundColor Green
    Write-Host "            $dst"
}

Write-Host ""
Write-Host "Install posture renderings" -ForegroundColor Cyan
Write-Host "Source: $PSScriptRoot"
if ($WhatIf) { Write-Host "DRY RUN - nothing will be written." -ForegroundColor Yellow }
Write-Host ""

Place ".\claude\CLAUDE.md" `
      (Join-Path $env:USERPROFILE ".claude\CLAUDE.md") `
      "Claude Code, all projects on this machine"

Place ".\github-copilot\copilot-instructions.md" `
      (Join-Path $env:USERPROFILE "copilot-instructions.md") `
      "GitHub Copilot, all sessions (user-level)"

if ($Repo) {
    if (-not (Test-Path $Repo)) { Fail "-Repo path does not exist: $Repo" }
    Place ".\github-copilot\copilot-instructions.md" `
          (Join-Path $Repo ".github\copilot-instructions.md") `
          "GitHub Copilot, repository-level"
}

Write-Host ""
Write-Host "Still needs a human - no API can do these:" -ForegroundColor Cyan
Write-Host "  1. M365 Copilot custom instructions. There is no Graph write path."
Write-Host "     Paste agent-posture\m365-copilot\custom-instructions.txt into"
Write-Host "     Settings > Personalization > Custom instructions."
Write-Host ""
Write-Host "  2. M365 Copilot declarative agent. POST /appCatalogs/teamsApps is"
Write-Host "     delegated-only, so the app-only Throne connector cannot publish it."
Write-Host "     Paste agent-posture\m365-copilot\instructions.md into the Copilot"
Write-Host "     Studio agent builder."
Write-Host ""
Write-Host "  3. In Visual Studio / SSMS, switch on:"
Write-Host "     Tools > Options > GitHub > Copilot > Copilot Chat >"
Write-Host "     'Enable custom instructions to be loaded from"
Write-Host "      .github/copilot-instructions.md files and added to requests'"
Write-Host ""
Write-Host "See agent-posture\HANDOFF.md for verification steps per surface." -ForegroundColor DarkGray
Write-Host ""
