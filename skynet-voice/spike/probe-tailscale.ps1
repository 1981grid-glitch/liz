<#
.SYNOPSIS
  Task 0.2 + Phase 1 — Tailscale HTTPS certificate and tailnet reachability.

.DESCRIPTION
  Records everything check 0.2 asks for: MagicDNS name, whether HTTPS
  certificates are enabled on the tailnet, and whether a cert actually issues.
  Read-only by default. Certificate issuance is a real, rate-limited action, so
  it only happens when you pass -IssueCert.

.EXAMPLE
  .\probe-tailscale.ps1
  .\probe-tailscale.ps1 -IssueCert
#>
[CmdletBinding()]
param(
  [switch]$IssueCert,
  [int]$AppPort = 8765
)

$ErrorActionPreference = 'Continue'
$findings = [ordered]@{}
function Say($s) { Write-Host $s }
function Head($s) { Write-Host ""; Write-Host "== $s ==" -ForegroundColor Cyan }

Head "Tailscale binary"
$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) {
  foreach ($p in @("$env:ProgramFiles\Tailscale\tailscale.exe",
                   "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe")) {
    if (Test-Path $p) { $ts = Get-Item $p; break }
  }
}
if (-not $ts) {
  Say "FAIL - tailscale CLI not found. Install Tailscale on SKYNET first."
  $findings['tailscale_cli'] = 'NOT FOUND'
  $findings | Format-List; exit 1
}
$tsExe = if ($ts.Source) { $ts.Source } else { $ts.FullName }
Say "found: $tsExe"
$findings['tailscale_cli'] = $tsExe
$findings['tailscale_version'] = (& $tsExe version 2>&1 | Select-Object -First 1)

Head "Backend state"
$statusJson = (& $tsExe status --json 2>&1 | Out-String)
try { $status = $statusJson | ConvertFrom-Json } catch { $status = $null }
if (-not $status) {
  Say "FAIL - could not parse 'tailscale status --json'. Is the service running / are you logged in?"
  $findings['backend_state'] = 'UNPARSEABLE'
  $findings | Format-List; exit 1
}
$findings['backend_state'] = $status.BackendState
Say "BackendState: $($status.BackendState)"
if ($status.BackendState -ne 'Running') {
  Say "WARN - not Running. Run 'tailscale up' and authenticate."
}

Head "This node"
$self = $status.Self
$dns  = if ($self.DNSName) { $self.DNSName.TrimEnd('.') } else { '' }
$findings['magicdns_name'] = $dns
$findings['tailscale_ips']  = ($self.TailscaleIPs -join ', ')
Say "MagicDNS name : $dns"
Say "Tailscale IPs : $($self.TailscaleIPs -join ', ')"
if (-not $dns) {
  Say "FAIL - no MagicDNS name. Enable MagicDNS in the tailnet admin console (DNS tab)."
}

Head "MagicDNS / HTTPS certificates enabled on the tailnet"
# MagicDNS name containing a tailnet domain implies MagicDNS; HTTPS certs is a
# separate toggle and the only reliable check is attempting issuance.
$findings['magicdns_enabled'] = if ($dns) { 'YES (name assigned)' } else { 'NO' }
Say "MagicDNS: $($findings['magicdns_enabled'])"
Say "HTTPS Certificates: cannot be read from the CLI - it is a tailnet admin"
Say "  policy toggle (Admin console > DNS > HTTPS Certificates)."
Say "  The authoritative test is issuing a cert: re-run with -IssueCert."

Head "Peers visible on the tailnet"
$peers = @()
if ($status.Peer) {
  $peers = $status.Peer.PSObject.Properties | ForEach-Object { $_.Value }
}
if ($peers.Count -eq 0) {
  Say "WARN - no peers visible. The Android phone must be logged into the SAME tailnet."
} else {
  foreach ($peer in $peers) {
    $on     = if ($peer.Online) { 'online ' } else { 'offline' }
    $pdns   = if ($peer.DNSName) { $peer.DNSName.TrimEnd('.') } else { '(no name)' }
    $pos    = if ($peer.OS) { $peer.OS } else { '?' }
    Say ("  {0}  {1}  {2}  [{3}]" -f $on, $pdns, ($peer.TailscaleIPs -join ','), $pos)
  }
}
$findings['peer_count'] = $peers.Count
$findings['peers_online'] = ($peers | Where-Object { $_.Online }).Count
$android = $peers | Where-Object { $_.OS -eq 'android' }
if ($android) {
  $a1 = ($android | Select-Object -First 1).DNSName
  $findings['android_peer'] = if ($a1) { $a1.TrimEnd('.') } else { '(unnamed android peer)' }
} else {
  $findings['android_peer'] = 'NONE SEEN'
}
Say "Android peer: $($findings['android_peer'])"

Head "Certificate issuance (check 0.2)"
if (-not $IssueCert) {
  Say "SKIPPED - read-only run. Re-run with -IssueCert to actually issue."
  Say "  Command it will run:  tailscale cert $dns"
  $findings['cert_issued'] = 'NOT ATTEMPTED'
} elseif (-not $dns) {
  Say "SKIPPED - no MagicDNS name to issue against."
  $findings['cert_issued'] = 'SKIPPED (no name)'
} else {
  Say "Running: tailscale cert $dns"
  $certOut = & $tsExe cert $dns 2>&1
  $code = $LASTEXITCODE
  $certOut | ForEach-Object { Say "  $_" }
  if ($code -eq 0) {
    Say "PASS - certificate issued for $dns"
    $findings['cert_issued'] = "YES ($dns)"
  } else {
    Say "FAIL - issuance failed (exit $code)."
    Say "  Most common cause: HTTPS Certificates is disabled in the tailnet admin console."
    $findings['cert_issued'] = "NO (exit $code)"
  }
  $findings['cert_output'] = ($certOut -join ' | ')
}

Head "Windows Firewall - inbound on port $AppPort"
# Phase 1 asks that the app port be reachable on the Tailscale interface only.
$rules = Get-NetFirewallRule -Direction Inbound -Enabled True -ErrorAction SilentlyContinue |
  Where-Object {
    $pf = $_ | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
    $pf -and $pf.LocalPort -contains "$AppPort"
  }
if ($rules) {
  $rules | ForEach-Object { Say ("  {0}  profile={1}  action={2}" -f $_.DisplayName, $_.Profile, $_.Action) }
  $findings['firewall_rules_port'] = ($rules | ForEach-Object { "$($_.DisplayName)[$($_.Profile)/$($_.Action)]" }) -join '; '
} else {
  Say "  no explicit inbound rule for $AppPort"
  Say "  NOTE: Tailscale traffic arrives on the Tailscale adapter. Scope any rule you"
  Say "  add to that interface only - do NOT open $AppPort on Private/Public profiles."
  $findings['firewall_rules_port'] = 'NONE'
}

Head "tailscale serve status"
$serve = & $tsExe serve status 2>&1
$serve | ForEach-Object { Say "  $_" }
$findings['serve_status'] = ($serve -join ' | ')

Head "FINDINGS BLOCK (paste into FINDINGS-TASK-0.md)"
Write-Host ""
Write-Host "### 0.2 - Tailscale HTTPS certificate issuance"
Write-Host ""
Write-Host "Result: ____  (PASS if cert_issued = YES)"
Write-Host "Date: $(Get-Date -Format o)"
Write-Host ""
foreach ($k in $findings.Keys) {
  Write-Host ("  {0} {1}" -f ($k + ' ').PadRight(26, '.'), $findings[$k])
}
Write-Host ""
