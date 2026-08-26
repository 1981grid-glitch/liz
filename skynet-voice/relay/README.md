# SKYNET Voice Relay

The thin custom relay from
[`../docs/ARCHITECTURE-DECISION.md`](../docs/ARCHITECTURE-DECISION.md). Browser ↔
WebSocket ↔ (Wyoming STT → Claude → TTS).

## What's verified vs. what still needs SKYNET

This was built and tested from a container with no route to SKYNET, so the honest
boundary is:

**Verified here** (`tests/`, run against this exact code, no mocking of the logic
under test):
- The WebSocket auth gate, `hello` handshake, and session reconnect-by-id.
- **Barge-in**, end to end: a new `speech_start` mid-reply cancels the in-flight
  response, the client receives `interrupted`, and only the text actually spoken
  before cancellation is committed to history — never the full untruncated reply.
  This was the single most important structural fix over upstream (openclaw-voice's
  design makes this physically impossible); it's now proven, not just designed.
- Incremental sentence splitting and markdown-for-speech cleaning.
- Session token-budget trimming keeps user/assistant turns paired.
- Every module imports cleanly and constructs against the real `anthropic` and
  `wyoming` package APIs (verified against the actual SDK source, not guessed).

**Not verified here, needs SKYNET**: an actual Wyoming round-trip to the real
`parakeet-wyoming-bridge` on `:10300`, an actual Kokoro instance once installed, and
an actual Anthropic API call with a real key. `tests/` mocks all three specifically
because none of them are reachable from where this was written.

## Setup on SKYNET

```powershell
cd C:\path\to\liz\skynet-voice\relay
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# edit .env — at minimum: ANTHROPIC_API_KEY, RELAY_SHARED_SECRET (make one up).
# Leave RELAY_HOST at its default (127.0.0.1) -- see "Bind to loopback" in
# docs/ARCHITECTURE-DECISION.md for why binding to the Tailscale IP directly
# would be wrong here.
```

For running tests specifically (not the real server), the shared secret needs to
match what the test files hardcode:

```powershell
$env:RELAY_SHARED_SECRET = "test-secret"
python tests\test_websocket.py
python tests\test_barge_in.py
```

No firewall rule needed for the relay's own port: bound to loopback, it's
unreachable from any network interface regardless of firewall state, and
`tailscale serve` (below) runs through the already-allowed `tailscaled` process
rather than opening a new listening socket.

### Run

```powershell
venv\Scripts\python.exe -m src.server.main
```

Front it with `tailscale serve` — this both provides the TLS C1 requires and is the
actual tailnet-exposure mechanism (the app itself is bound to loopback, see above):

```powershell
tailscale serve --https=443 --bg http://127.0.0.1:8765
```

`--bg` makes this persist past closing the window. `tailscale serve status` shows
the current mapping; `tailscale serve off` removes it.

Bookmark `https://<magicdns-name>/?token=<RELAY_SHARED_SECRET>` on the phone — the
client reads the token from the URL once and persists it to `localStorage`.

### TTS — Kokoro is not installed yet (Task 0.3)

Default backend is `kokoro`, pointed at `http://127.0.0.1:8880` (Kokoro-FastAPI's
standard port). Nothing is listening there yet. Docker isn't set up on SKYNET (per
0.3's findings), so use the native path — `uv` is already installed:

```powershell
git clone https://github.com/remsky/Kokoro-FastAPI.git
cd Kokoro-FastAPI
python docker/scripts/download_model.py --output api/src/models/v1_0
.\start-gpu.ps1
```

Requires `espeak-ng` installed system-wide first (fallback phonemizer for unknown
words) — grab the Windows installer from the
[espeak-ng releases page](https://github.com/espeak-ng/espeak-ng/releases). Leave
`start-gpu.ps1` running in its own window; it's the server.

Confirm it's actually producing audible speech — not just responding to HTTP —
before wiring it to the relay: `tests\smoke_test_kokoro.ps1`.

To use the proven Azure fallback instead while Kokoro gets set up, set
`TTS_BACKEND=azure` and `AZURE_TTS_KEY` in `.env` (see
`../docs/ARCHITECTURE-DECISION.md` for the tradeoff — this means response audio
leaves the tailnet).

24kHz output and the `af_bella` voice name are confirmed against Kokoro-FastAPI's
own docs, not assumed.

### STT — Parakeet, via Wyoming, not a new install

`STT_WYOMING_HOST`/`STT_WYOMING_PORT` in `.env` should point at the existing
`parakeet-wyoming-bridge` service (`127.0.0.1:10300` if the relay runs on SKYNET
itself). Nothing to install — this is Task 0.3's key finding: reuse it, don't
duplicate the GPU-resident model in a second process.

## Known gaps for Phase 4

- **Kokoro is running on CPU, not the 5070 Ti, until upstream ships CUDA 13
  support.** Confirmed 2026-08-26: `start-gpu.ps1` runs clean and the server
  comes up, but its own startup log is self-contradictory — banner text says
  `"...on cuda"` / `"warmed up on cuda"`, but the actual status line says
  `CUDA: False` and `"Loading Kokoro model on cpu"`. Root cause is
  [remsky/Kokoro-FastAPI#443](https://github.com/remsky/Kokoro-FastAPI/issues/443):
  the project pins CUDA 12.8; the RTX 5070 Ti needs CUDA 13, so torch silently
  falls back to CPU rather than erroring. A community fix exists but is
  Docker-based (a different base image) and unmerged as of this writing —
  doesn't apply to the native `uv` install path used here, and Docker isn't
  set up on SKYNET regardless. Kokoro-82M is small enough that CPU inference
  is likely still usable, just slower than the architecture doc's 150–400 ms
  TTS-stage budget assumed — expect that number to be optimistic until this
  lands upstream. Re-check periodically; not worth chasing a Docker install
  just for this.
- **No server-side VAD / continuous mode.** The client is hold-to-talk (matching
  the lesson in SKYNET's own dictation build notes — toggle mode was the
  documented source of real bugs there). Continuous mode is a v2 feature, not
  required by the brief.
- **Screen-off/pocket behavior (C4) is untested** — needs the actual phone.
- **Latency isn't instrumented yet** — the architecture doc's stage breakdown
  (STT/model/TTS) needs real logging added once this runs against live services,
  per the brief's Phase 4 deliverable.
