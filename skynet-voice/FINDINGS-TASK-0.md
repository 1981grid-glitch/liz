# Task 0 — Verification Spike Findings

**Build:** SKYNET Conversational Voice Relay over Tailscale
**Date:** 2026-08-22
**Status:** ⚠️ **1 of 4 checks complete. Check 0.4 FAILS. Go/no-go decision required before Phase 1.**

---

## Summary

| Check | Subject | Result |
|---|---|---|
| 0.1 | Glasses mic reaches the browser | ⏸️ **NOT RUN** — requires the phone and glasses in hand |
| 0.2 | Tailscale HTTPS cert issuance | ⏸️ **NOT RUN** — requires a shell on SKYNET |
| 0.3 | Local Parakeet / Kokoro reachability | ⏸️ **NOT RUN** — requires a shell on SKYNET |
| 0.4 | Upstream repo reality check | ❌ **FAIL** — verified against source, conclusive |

**Why three checks are unrun.** This work ran in an ephemeral Linux container with no
route to SKYNET, no Bluetooth, no Android device, and no membership in the tailnet.
Checks 0.1–0.3 are physical measurements of operator-controlled hardware; I cannot
fabricate them, and a guess recorded as a PASS is worse than a blank. Instead I built
the instrumentation that makes them fast and, in 0.1's case, **correct** — the brief's
stated method for 0.1 does not measure what it claims to (see below). Each probe emits
a paste-ready findings block.

**Check 0.4 required no hardware, and it is the one that reshapes the build.** It is
answered below from a full read of the upstream source at commit `28866f2`, not from
the README.

---

## 0.4 — Upstream repo reality check ❌ FAIL

**Repo:** `Purple-Horizons/openclaw-voice` @ `28866f2` (2026-02-01), MIT, Python 3.10+,
FastAPI + WebSocket, 1,386 lines of server source.

The brief flagged STT/TTS pluggability and custom-agent support as *assumed, not
confirmed*. Both assumptions are wrong.

### Are the STT and TTS backends pluggable? **No.**

Both are hardcoded try-import cascades. There is no plugin interface, no backend
registry, and no configuration anywhere that points either at an HTTP endpoint.

**STT** — `src/server/stt.py:12` defines a single `WhisperSTT` class. `_load_model()`
tries `faster-whisper` (`stt.py:31-58`), then `openai-whisper` (`stt.py:65-80`), then
falls back to a mock that returns a literal placeholder string (`stt.py:83-84`,
`stt.py:107-109`). The only STT knob, `OPENCLAW_STT_MODEL`, selects the *Whisper model
size* (`main.py:46`). **Nothing accepts a URL.** Pointing this at Parakeet means
replacing the class.

**TTS** — `src/server/tts.py:31-85` cascades ElevenLabs → Chatterbox → XTTS → mock.
Selection is implicit: whichever import succeeds first, with ElevenLabs winning
whenever `ELEVENLABS_API_KEY` is set. Two documented settings are **inert**:

- `OPENCLAW_TTS_MODEL` (`.env.example:36`) is read into `Settings` (`main.py:50`) and
  then only *logged* (`main.py:103`). It never reaches the constructor
  (`main.py:104-106`). The log line actively misleads — it prints
  `Loading TTS model: chatterbox` while ElevenLabs is what actually loads.
- `ELEVENLABS_VOICE_ID` (`.env.example:22`) is never read from the environment
  anywhere. Voice is pinned to a hardcoded default (`tts.py:25`).

Two further TTS problems matter for this build specifically:

- **Only ElevenLabs streams.** `synthesize_stream()` streams for ElevenLabs
  (`tts.py:112-124`); every other backend falls through to synthesizing the entire
  utterance before yielding one blob (`tts.py:125-128`). Adopting *any* local TTS
  therefore forfeits the sentence-by-sentence streaming that the README sells — and
  that is precisely what keeps perceived turn latency under the 2 s target.
- **It runs `pip install` at runtime.** `tts.py:43-53` shells out to
  `subprocess.check_call(["pip", "install", "elevenlabs", "-q"])` on import failure.
  Unattended package installation into the live environment on a production host is
  not something to inherit.

### Does it support a custom agent endpoint? **Only OpenAI-shaped ones.**

`AIBackend` speaks `chat.completions.create` through `AsyncOpenAI`
(`backend.py:36-43`, `97-102`, `133-139`). **The string `anthropic` does not appear
anywhere in the Python source.** "Works with Claude" means pointing the OpenAI client
at an OpenAI-compatible base URL — the Anthropic Messages API is not that shape, so
targeting `claude-sonnet-4-6` directly requires replacing this module.

The advertised OpenClaw-gateway backend is also **not implemented** in `AIBackend`:
`backend_type="openclaw"` sets up no client (`backend.py:46-48`), and both `chat()` and
`chat_stream()` gate on `backend_type == "openai" and self._client`
(`backend.py:62`, `78`). Anything else silently degrades to an **echo bot** —
`return f"I heard you say: {user_message}"` (`backend.py:66`, `82`). It is not an
error path; it returns HTTP 200 and speaks the words back. `main.py:113-127` dodges
this by constructing the gateway backend with `backend_type="openai"`.

### Other defects found in the read

These are not blockers on their own, but each one lands on a Phase 4 test in the brief:

| Finding | Location | Consequence |
|---|---|---|
| History truncated to the last **10 messages ≈ 5 exchanges** | `backend.py:94`, `128` | The "10+ turns without context loss" test fails as written |
| `AIBackend` is a module-level singleton; history is never cleared | `main.py:77`, `backend.py:157` (`clear_history` called only in tests) | One conversation shared by every browser session, growing without bound |
| **No barge-in is structurally possible** | `main.py:263-349` | The socket is not read again until the full response has been synthesized and sent, so an interrupt cannot arrive mid-playback |
| Binds `0.0.0.0` with auth **off** by default | `main.py:38`, `main.py:42` | An unauthenticated endpoint that spends your API key, on every interface — directly contrary to the brief's bind-to-tailnet instruction |
| `FileResponse("src/client/index.html")` is a **relative path** | `main.py:150` | 404s unless the process CWD is the repo root — exactly what bites a Windows service or scheduled task |
| `src/server/streaming.py` (182 lines) is never imported | — | Dead code |

### Is `yagudaev/voiceclaw` a viable alternative? **No — rejected on the objective.**

- Its voice path **requires a third-party realtime speech-to-speech provider** —
  Gemini Live, Grok Voice, or OpenAI Realtime. Operator audio would stream to Google,
  xAI, or OpenAI. That contradicts the stated success condition that no audio leaves
  operator-controlled infrastructure except the model API call.
- **No local STT/TTS support at all**, so Parakeet and Kokoro are unusable.
- Its clients are a **React Native/Expo iOS app** and an **Electron macOS app**. The
  target is Android Chrome with no app install.
- Claude can only appear as the "brain" behind its `ask_brain` tool, reached over
  OpenAI-shaped chat completions — the same mismatch as above.

### Verdict

The brief's Phase 2 assumption does not hold. Adopting upstream means forking
`stt.py`, `tts.py`, and `backend.py` — which is the entire substance of the project.
What would remain reusable is the WebSocket loop and the browser client, and both
carry defects listed above that have to be fixed regardless.

**Recommendation: build the thin custom relay**, which the brief already names as a
legitimate outcome. Rationale and scope in [`docs/ARCHITECTURE-DECISION.md`](docs/ARCHITECTURE-DECISION.md).

---

## 0.1 — Glasses microphone reaches the browser ⏸️ NOT RUN

Requires the Meta glasses paired to the Android phone, in hand. **Run
[`spike/mic-probe.html`](spike/mic-probe.html) to execute this check** — it prints a
completed findings block.

### ⚠️ The brief's stated method for this check does not measure what it claims

Step 0.1.5 asks for `AudioContext.sampleRate` and `track.getSettings()`. Those numbers
**cannot answer C3.** Two reasons:

1. Android delivers whatever the OS mixer produces — typically 48 kHz — regardless of
   whether the Bluetooth link negotiated CVSD at 8 kHz or mSBC at 16 kHz. The OS
   upsamples silently. `AudioContext.sampleRate` reports the graph rate, not the link.
2. If the page constructs its context with a forced rate — as upstream's own client
   does, `AudioContext({sampleRate: 16000})` at `src/client/index.html:550` — the
   browser resamples and reports back **the number you asked for**. Reading it tells
   you what you typed.

Narrowband is only detectable **spectrally**: a CVSD link carries essentially nothing
above ~4 kHz, because the content was never sampled. So the probe:

- requests the mic with **no `sampleRate` constraint** and all processing off
  (AEC/NS/AGC each band-limit and would corrupt the measurement);
- accumulates a peak-hold spectrum over ~6 s of speech;
- measures the noise floor, then asks whether real content exists at **4.5–6.5 kHz**
  (present for mSBC, absent for CVSD) and at **9–12 kHz** (present only off a non-HFP
  mic, i.e. the phone's own microphone);
- **refuses to classify** when the speech-to-floor range is under 35 dB, rather than
  reporting a confident wrong answer;
- plots the spectrum with 4 kHz and 8 kHz gridlines, so the operator can see the
  cutoff directly and is never dependent on my classifier;
- records a few seconds of audio for playback, so the source can be confirmed by ear.

The classifier is unit-tested against synthetic CVSD / mSBC / wideband spectra,
including degraded-capture cases it must decline —
[`spike/bandwidth-classifier.test.js`](spike/bandwidth-classifier.test.js), run with
`node`. All nine cases pass.

**Ordering note:** the probe needs a secure context, so it cannot run until 0.2 gives
you HTTPS. **Run 0.2 first**, serve the probe with `tailscale serve`, then run 0.1
against it. The page states plainly when it is not in a secure context — which is
itself a live confirmation of C1.

```
### 0.1 — Glasses microphone reaches the browser
Result: ____
(generated by spike/mic-probe.html — paste its output here)
```

---

## 0.2 — Tailscale HTTPS certificate issuance ⏸️ NOT RUN

Requires a shell on SKYNET and tailnet admin visibility. Run
[`spike/probe-tailscale.ps1`](spike/probe-tailscale.ps1); it emits a findings block.

Read-only by default. It reports backend state, MagicDNS name, tailnet peers (flagging
the Android peer), any inbound firewall rule on the app port, and `tailscale serve`
state. Pass `-IssueCert` to actually run `tailscale cert` — issuance is rate-limited,
so it is deliberately opt-in.

One thing the CLI genuinely cannot tell you: **HTTPS Certificates** is a tailnet admin
policy toggle, and its state is not exposed. Attempting issuance is the authoritative
test; if it fails, that toggle is the first place to look.

```
### 0.2 — Tailscale HTTPS certificate issuance
Result: ____
(generated by spike/probe-tailscale.ps1 — paste its output here)
```

---

## 0.3 — Local speech stack reachability ⏸️ NOT RUN

Requires a shell on SKYNET. Run
[`spike/probe-speech-stack.ps1`](spike/probe-speech-stack.ps1).

It enumerates listening TCP ports, probes each for HTTP, and pulls `/openapi.json`
where available — which yields the exact request/response schema the check asks for,
rather than a guess. It classifies each service as Parakeet-like or Kokoro-like from
the surface it exposes. GETs only; it never posts audio.

**The audio contract must still be recorded by hand** — OpenAPI does not describe
sample rate, encoding, or channel count. The block it prints has explicit slots for
these. Two entries deserve attention:

- **Parakeet's expected input rate.** If 0.1 comes back narrowband, an 8 kHz source
  upsampled to 16 kHz still contains no high-frequency information; the resampling
  satisfies the interface without recovering accuracy.
- **Whether Kokoro can stream output incrementally.** This single property dominates
  perceived turn latency far more than model choice does. If it is batch-only, the
  first audio cannot start until the whole utterance is synthesized.

```
### 0.3 — Local speech stack reachability
Result: ____
(generated by spike/probe-speech-stack.ps1 — paste its output here)
```

---

## Recommendation

**Do not start Phase 1 yet.** Two things should happen first:

1. **Run 0.2 → 0.1 → 0.3, in that order** (0.2 first because 0.1 needs HTTPS). Roughly
   30 minutes with the probes. 0.1 is still the check most likely to kill the premise;
   if the glasses cannot be captured, the fallback is the Yealink WH63 E2 you already
   own, and the rest of the architecture is unaffected.
2. **Decide the build target given 0.4.** My recommendation is the thin custom relay
   rather than a fork of upstream — reasoning and a scoped design in
   [`docs/ARCHITECTURE-DECISION.md`](docs/ARCHITECTURE-DECISION.md). No application
   code has been written, per the brief's instruction to hold until Task 0 clears.

One correction to carry into Phase 2 regardless of which way you go: the brief's
`claude-sonnet-4-6` is a **valid current model ID** and a sound latency choice. Note
that the Anthropic API's own interface is the Messages API — not OpenAI-shaped chat
completions — which is a code-level difference either path has to absorb.
