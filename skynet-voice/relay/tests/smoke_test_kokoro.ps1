<#
.SYNOPSIS
  Confirms Kokoro-FastAPI is actually producing audible speech before the
  relay is wired to it. Run this after `.\start-gpu.ps1`, before anything
  else.

.DESCRIPTION
  SKYNET's own tts-readback-chain.md documented weeks of hidden breakage
  caused by treating "a wav file appeared" as proof the chain worked --
  the Azure call and the playback were separate links, and only one was
  ever tested. This script does not repeat that mistake: it plays the
  file back and makes you confirm you heard it, rather than declaring
  success on the HTTP response alone.

.EXAMPLE
  .\smoke_test_kokoro.ps1
  .\smoke_test_kokoro.ps1 -KokoroUrl http://127.0.0.1:8880 -Voice af_bella
#>
[CmdletBinding()]
param(
  [string]$KokoroUrl = "http://127.0.0.1:8880",
  [string]$Voice = "af_bella",
  [string]$Text = "This is a test of the local text to speech service.",
  [switch]$SkipPlayback
)

$ErrorActionPreference = 'Stop'
function Say($s) { Write-Host $s }
function Head($s) { Write-Host ""; Write-Host "== $s ==" -ForegroundColor Cyan }

Head "Reachability"
try {
  $null = Invoke-WebRequest -Uri $KokoroUrl -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
} catch {
  if ($_.Exception.Response -eq $null) {
    Say "FAIL - cannot reach $KokoroUrl at all. Is start-gpu.ps1 still running in another window?"
    exit 1
  }
  # A non-200 on '/' (e.g. 404) still proves the server is up and answering.
}
Say "PASS - something is answering on $KokoroUrl"

Head "Synthesis request"
$body = @{
  model = "kokoro"
  input = $Text
  voice = $Voice
  response_format = "wav"
} | ConvertTo-Json

$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
  $response = Invoke-WebRequest -Uri "$KokoroUrl/v1/audio/speech" -Method Post `
    -ContentType "application/json" -Body $body -TimeoutSec 30 -ErrorAction Stop
} catch {
  Say "FAIL - POST /v1/audio/speech failed:"
  Say "  $($_.Exception.Message)"
  if ($_.ErrorDetails.Message) { Say "  $($_.ErrorDetails.Message)" }
  Say ""
  Say "Common cause: -Voice '$Voice' isn't a name this build actually has loaded."
  Say "Check the server's own startup log for the voices it reports having found."
  exit 1
}
$sw.Stop()

$bytes = $response.Content
if (-not $bytes -or $bytes.Length -lt 1000) {
  Say "FAIL - response was $($bytes.Length) bytes. That's not a real WAV file."
  exit 1
}
Say ("PASS - received {0:N0} bytes of audio in {1:N2}s" -f $bytes.Length, $sw.Elapsed.TotalSeconds)

$outPath = Join-Path $env:TEMP "kokoro-smoke-test.wav"
[System.IO.File]::WriteAllBytes($outPath, $bytes)

if ($SkipPlayback) {
  Head "Playback skipped (-SkipPlayback)"
  Say "Saved to $outPath — copy it somewhere with speakers if you want to check by ear."
  Say ""
  Say "PASS (unconfirmed by ear) - Kokoro returned real audio bytes over HTTP."
  Say "SKYNET itself never plays audio in the real system anyway -- the phone"
  Say "does, through the glasses -- so this is the right level of confirmation"
  Say "for now. The real audible test happens during the phone conversation test."
  Say "  Set TTS_BACKEND=kokoro and KOKORO_URL=$KokoroUrl in relay/.env"
} else {
  Head "Playback — the strongest local success test"
  Say "Saved to $outPath"
  Say "Playing now — turn your volume up."

  $player = New-Object System.Media.SoundPlayer $outPath
  $player.PlaySync()

  Write-Host ""
  $heard = Read-Host "Did you actually hear the sentence spoken? (y/n)"
  if ($heard -eq 'y' -or $heard -eq 'Y') {
    Say ""
    Say "PASS - Kokoro is real and working. Safe to point the relay at it."
    Say "  Set TTS_BACKEND=kokoro and KOKORO_URL=$KokoroUrl in relay/.env"
  } else {
    Say ""
    Say "FAIL - a file was produced but you did not hear it. Do not treat this"
    Say "as working. Check: correct Windows playback device selected, volume,"
    Say "and that $outPath actually contains speech and not silence."
    exit 1
  }
}
