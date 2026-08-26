<#
.SYNOPSIS
  Task 0.3 - Discover and fingerprint the local Parakeet (STT) and Kokoro (TTS)
  endpoints on SKYNET, and record their request/response schemas.

.DESCRIPTION
  Enumerates local listening TCP ports, probes each for an HTTP service, and
  pulls /openapi.json where available so the exact request and response schema
  is recorded rather than guessed. Read-only: it issues GETs only, and never
  posts audio.

.EXAMPLE
  .\probe-speech-stack.ps1
  .\probe-speech-stack.ps1 -ExtraPorts 9000,9001
#>
[CmdletBinding()]
param(
  [int[]]$ExtraPorts = @(),
  [int]$TimeoutSec = 3
)

$ErrorActionPreference = 'Continue'
function Say($s) { Write-Host $s }
function Head($s) { Write-Host ""; Write-Host "== $s ==" -ForegroundColor Cyan }

# Ports worth trying even if nothing appears to be listening on them.
$known = @(8880, 8000, 8001, 8080, 5000, 5001, 7860, 9000, 50021, 8765)

Head "Listening TCP ports on this machine"
$listen = @()
try {
  $listen = Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { $_.LocalPort -lt 65000 } |
            Select-Object -ExpandProperty LocalPort -Unique | Sort-Object
} catch {
  Say "  (Get-NetTCPConnection unavailable; falling back to netstat)"
  $listen = (netstat -ano | Select-String 'LISTENING') -replace '.*:(\d+)\s.*', '$1' |
            ForEach-Object { [int]$_ } | Sort-Object -Unique
}
Say ("  " + ($listen -join ', '))

$candidates = @($listen + $known + $ExtraPorts) | Sort-Object -Unique
Say ""
Say ("Probing {0} candidate ports..." -f $candidates.Count)

$services = @()

foreach ($port in $candidates) {
  $base = "http://127.0.0.1:$port"

  # Cheap liveness check first so we do not wait on dead ports.
  $open = $false
  try {
    $c = New-Object System.Net.Sockets.TcpClient
    $iar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
    $open = $iar.AsyncWaitHandle.WaitOne(250)
    if ($open) { $c.EndConnect($iar) }
    $c.Close()
  } catch { $open = $false }
  if (-not $open) { continue }

  $svc = [ordered]@{
    port = $port; base = $base; reachable = $true
    openapi = $null; title = $null; paths = @(); kind = 'unknown'
  }

  # OpenAPI gives the authoritative request/response schema.
  foreach ($docPath in @('/openapi.json', '/docs/openapi.json', '/v1/openapi.json')) {
    try {
      $r = Invoke-RestMethod -Uri "$base$docPath" -TimeoutSec $TimeoutSec -ErrorAction Stop
      if ($r -and $r.paths) {
        $svc.openapi = "$base$docPath"
        $svc.title   = $r.info.title
        $svc.paths   = @($r.paths.PSObject.Properties.Name | Sort-Object)
        break
      }
    } catch { }
  }

  # Fall back to poking well-known endpoints.
  if (-not $svc.openapi) {
    foreach ($p in @('/health', '/healthz', '/', '/v1/models', '/status')) {
      try {
        $r = Invoke-WebRequest -Uri "$base$p" -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) {
          $body = "$($r.Content)"
          if ($body.Length -gt 300) { $body = $body.Substring(0, 300) + '...' }
          $svc.paths += "$p -> 200 : $body"
        }
      } catch { }
    }
  }

  if ($svc.paths.Count -eq 0) { continue }

  # Classify by the surface it exposes.
  $blob = (@($svc.title) + $svc.paths) -join ' '
  $isTts = $blob -match 'audio/speech|/tts|kokoro|voices|synthesi'
  $isStt = $blob -match 'audio/transcriptions|/transcribe|parakeet|asr|/stt|recogni'
  if ($isTts -and -not $isStt) { $svc.kind = 'TTS (Kokoro-like)' }
  elseif ($isStt -and -not $isTts) { $svc.kind = 'STT (Parakeet-like)' }
  elseif ($isTts -and $isStt) { $svc.kind = 'STT+TTS' }

  $services += [pscustomobject]$svc
}

Head "Services found"
if ($services.Count -eq 0) {
  Say "  NONE. Neither Parakeet nor Kokoro answered on any probed port."
  Say "  Start them, or pass -ExtraPorts with the correct ports."
} else {
  foreach ($s in $services) {
    Say ""
    Say ("  port {0}  [{1}]  {2}" -f $s.port, $s.kind, $s.title)
    Say ("    base    : {0}" -f $s.base)
    if ($s.openapi) { Say ("    openapi : {0}" -f $s.openapi) }
    Say  "    paths   :"
    foreach ($p in $s.paths) { Say ("      {0}" -f $p) }
  }
}

Head "What still has to be recorded by hand"
Say "  The probe reports the HTTP surface. Check 0.3 also needs the AUDIO CONTRACT,"
Say "  which OpenAPI does not describe:"
Say "    - STT  : expected sample rate, encoding, mono/stereo, container vs raw PCM,"
Say "             max utterance length, and whether it streams or is batch-only."
Say "    - TTS  : output sample rate, encoding, and whether it can stream audio out"
Say "             incrementally (this dominates perceived turn latency)."
Say "  Confirm both against the service's own README, then fill them in below."

Head "FINDINGS BLOCK (paste into FINDINGS-TASK-0.md)"
Write-Host ""
Write-Host "### 0.3 - Local speech stack reachability"
Write-Host ""
Write-Host "Result: ____  (PASS only if BOTH endpoints answer and formats are known)"
Write-Host "Date: $(Get-Date -Format o)"
Write-Host ""
Write-Host "Discovered services:"
if ($services.Count -eq 0) {
  Write-Host "  NONE"
} else {
  foreach ($s in $services) {
    Write-Host ("  - {0}  kind={1}  title={2}" -f $s.base, $s.kind, $s.title)
    if ($s.openapi) { Write-Host ("      openapi: {0}" -f $s.openapi) }
    foreach ($p in $s.paths) { Write-Host ("      {0}" -f $p) }
  }
}
Write-Host ""
Write-Host "Parakeet (STT)"
Write-Host "  host:port .............. ____"
Write-Host "  endpoint ............... ____"
Write-Host "  request schema ......... ____"
Write-Host "  response schema ........ ____"
Write-Host "  input sample rate ...... ____ Hz"
Write-Host "  input encoding ......... ____ (e.g. 16-bit PCM mono)"
Write-Host "  streaming or batch ..... ____"
Write-Host ""
Write-Host "Kokoro (TTS)"
Write-Host "  host:port .............. ____"
Write-Host "  endpoint ............... ____"
Write-Host "  request schema ......... ____"
Write-Host "  response schema ........ ____"
Write-Host "  output sample rate ..... ____ Hz"
Write-Host "  output encoding ........ ____"
Write-Host "  streaming supported .... ____   <- drives turn latency"
Write-Host ""
