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
# edit .env — at minimum: ANTHROPIC_API_KEY, RELAY_HOST (your Tailscale IP,
# from `tailscale ip -4`), RELAY_SHARED_SECRET
```

Run the tests first — they need no live services:

```powershell
python tests\test_websocket.py
python tests\test_barge_in.py
```

### Firewall — port-scoped, not program-scoped

Per Task 0.3's findings: SKYNET has an existing rule permitting inbound to
`python.exe`, but none for `pythonw.exe`, and a program-scoped rule silently breaks
on a venv rebuild. Add a **port**-scoped rule instead, matching the pattern already
used for `Enable-ParakeetFirewall.ps1`:

```powershell
New-NetFirewallRule -DisplayName "SKYNET Voice Relay" -Direction Inbound `
    -Protocol TCP -LocalPort 8765 -Profile Private -Action Allow
```

Scope this to the **Tailscale interface only** — do not open it on the Public
profile.

### Run

```powershell
venv\Scripts\python.exe -m src.server.main
```

Front it with `tailscale serve` for TLS (see `../spike/probe-tailscale.ps1` and the
Task 0 findings for the cert-issuance prerequisite):

```powershell
tailscale serve --https=443 --set-path=/ http://127.0.0.1:8765
```

Bookmark `https://<magicdns-name>/?token=<RELAY_SHARED_SECRET>` on the phone — the
client reads the token from the URL once and persists it to `localStorage`.

### TTS — Kokoro is not installed yet (Task 0.3)

Default backend is `kokoro`, pointed at `http://127.0.0.1:8880` (Kokoro-FastAPI's
standard port). Nothing is listening there yet. Options, from
[`Kokoro-FastAPI`](https://github.com/remsky/Kokoro-FastAPI):

```powershell
docker run -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-gpu:latest
```

or a bare install per that repo's README if Docker isn't set up on SKYNET (it wasn't,
per 0.3's findings — `docker` wasn't on PATH).

To use the proven Azure fallback instead while Kokoro gets set up, set
`TTS_BACKEND=azure` and `AZURE_TTS_KEY` in `.env` (see
`../docs/ARCHITECTURE-DECISION.md` for the tradeoff — this means response audio
leaves the tailnet).

**The Kokoro-FastAPI output sample rate (assumed 24kHz in `src/server/tts.py`) is
unconfirmed** — verify it against a real instance and correct `KokoroTTS.sample_rate`
if it differs; a mismatch here means pitched-wrong audio, not a crash.

### STT — Parakeet, via Wyoming, not a new install

`STT_WYOMING_HOST`/`STT_WYOMING_PORT` in `.env` should point at the existing
`parakeet-wyoming-bridge` service (`127.0.0.1:10300` if the relay runs on SKYNET
itself). Nothing to install — this is Task 0.3's key finding: reuse it, don't
duplicate the GPU-resident model in a second process.

## Known gaps for Phase 4

- **No server-side VAD / continuous mode.** The client is hold-to-talk (matching
  the lesson in SKYNET's own dictation build notes — toggle mode was the
  documented source of real bugs there). Continuous mode is a v2 feature, not
  required by the brief.
- **Screen-off/pocket behavior (C4) is untested** — needs the actual phone.
- **Latency isn't instrumented yet** — the architecture doc's stage breakdown
  (STT/model/TTS) needs real logging added once this runs against live services,
  per the brief's Phase 4 deliverable.
