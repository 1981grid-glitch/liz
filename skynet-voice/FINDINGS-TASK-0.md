# Task 0 — Verification Spike Findings

**Build:** SKYNET Conversational Voice Relay over Tailscale
**Date:** 2026-08-22 (opened) — updated 2026-08-26
**Status:** ⚠️ **0.4 fails. 0.2 fails, actionably, retry in progress. 0.3 answered — but from SKYNET's own operational documentation, not the probe script. 0.1 still blocked on hardware.**

---

## Summary

| Check | Subject | Result |
|---|---|---|
| 0.1 | Glasses mic reaches the browser | ⏸️ **NOT RUN** — requires the phone and glasses in hand |
| 0.2 | Tailscale HTTPS cert issuance | ❌ **FAIL** — actionable; HTTPS Certificates was off tailnet-side, retry pending |
| 0.3 | Local Parakeet / Kokoro reachability | ⚠️ **PARTIAL** — Parakeet confirmed real via internal docs; Kokoro confirmed absent |
| 0.4 | Upstream repo reality check | ❌ **FAIL** — verified against source, conclusive |

**Why three checks were initially unrun.** This work started in an ephemeral Linux
container with no route to SKYNET, no Bluetooth, no Android device, and no membership
in the tailnet. Checks 0.1–0.3 are physical measurements of operator-controlled
hardware; a guess recorded as a PASS is worse than a blank. The instrumentation below
(built first) is what made the operator's actual runs fast, and in 0.1's case,
**correct** — the brief's stated method for 0.1 does not measure what it claims to
(see below).

**Check 0.4 required no hardware, and it is the one that reshapes the build.** It is
answered below from a full read of the upstream source at commit `28866f2`, not from
the README.

**Check 0.3 turned out to require neither hardware nor the probe script.** The operator
pointed at existing internal documentation of SKYNET's speech stack, written by prior
sessions while building an unrelated project on the same machine. That documentation is
authoritative and is what answers 0.3 below — the probe script's blind port/HTTP scan
missed the real service entirely (it speaks a non-HTTP protocol) and is not the source
of this finding.

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

## 0.2 — Tailscale HTTPS certificate issuance ❌ FAIL (actionable)

Run via [`spike/probe-tailscale.ps1`](spike/probe-tailscale.ps1) on SKYNET.

```
### 0.2 — Tailscale HTTPS certificate issuance
Result: FAIL
Date: 2026-08-26T03:24:07-04:00

  tailscale_cli ............ C:\Program Files\Tailscale\tailscale.exe
  tailscale_version ........ 1.102.2
  backend_state ............ Running
  magicdns_name ............ skynet.tail552b9c.ts.net
  tailscale_ips ............ 100.85.104.1, fd7a:115c:a1e0::a33:6802
  magicdns_enabled ......... YES (name assigned)
  peer_count ............... 4
  peers_online .............. 2
  android_peer .............. zachs-s25-ultra.tail552b9c.ts.net (online)
  cert_issued ............... NO (exit 1)
  cert_output ................ 500 Internal Server Error: your Tailscale account does not support getting TLS certs
  firewall_rules_port ....... NONE
  serve_status ............... No serve config
```

**Confirmed:** MagicDNS is on and resolving (`skynet.tail552b9c.ts.net`), and the
Android phone (`zachs-s25-ultra`) is already on the tailnet and online — Phase 1's
"phone reaches SKYNET by hostname" exit criterion is effectively already met once
HTTPS clears.

**The failure:** that exact 500 means **HTTPS Certificates is not enabled on this
tailnet** — a manual admin-console toggle, off by default, does not self-resolve.
Fix: [admin console](https://login.tailscale.com/admin/dns) → **DNS** tab → enable
**HTTPS Certificates** → re-run `tailscale cert skynet.tail552b9c.ts.net`.

Do not substitute a self-signed cert, even temporarily — Android Chrome won't trust
it for `getUserMedia()` regardless, so it buys nothing, and C1 forbids it explicitly.

**Retry note:** immediately after flipping the toggle, `tailscale cert` can hang or
fail with the same 500 for several minutes while the tailnet's cert infrastructure
catches up — this is a documented Tailscale behavior
([tailscale/tailscale#20823](https://github.com/tailscale/tailscale/issues/20823)),
not a sign of misconfiguration. Retry every few minutes rather than troubleshooting
firewall/network settings on SKYNET's end.

---

## 0.3 — Local speech stack reachability ⚠️ PARTIAL (answered via internal docs)

`spike/probe-speech-stack.ps1` was run and came back empty-looking and slow — it
enumerates listening ports and probes each for HTTP/OpenAPI, and the real STT service
turned out not to speak HTTP at all, so the probe never recognized it and stalled on
`Invoke-WebRequest` timeouts against a port that accepts TCP but never answers.

The actual answer came from existing documentation: Adaptive Enterprises' Claude
operational memory (synced through the `ZachOperationsHub` SharePoint site),
written while building an unrelated project — a Home Assistant voice bridge — on
this same machine. That documentation is detailed, dated, and specific; it is treated
as authoritative here, not the probe's empty result.

### STT — Parakeet is real, fast, and reachable — but not over HTTP

Parakeet TDT 0.6B **v2** (English-only — deliberately: v3 is multilingual and drifts
into other languages mid-utterance, and `onnx-asr`'s `language=` kwarg is inert for
Parakeet TDT models, so model choice is the only lever that matters) runs locally via
`onnx-asr` 0.12.0 + `onnxruntime-gpu` 1.28 on SKYNET's RTX 5070 Ti. It already backs a
push-to-talk dictation tool, and as of 2026-08-25 the same model is additionally
exposed as a **Wyoming-protocol service on TCP 10300** (built to feed a Home Assistant
voice pipeline over the network).

**Measured performance** (from that build's own test): 4.76 s of audio → 0.47 s to
transcribe — **RTF 0.098**. That's comfortably inside the architecture doc's
150–400 ms STT budget for realistic utterance lengths.

Wyoming is a lightweight JSON-event-over-TCP protocol (Rhasspy/Home Assistant
ecosystem) — not REST, which is exactly why the probe script didn't find it. **The
relay should speak Wyoming directly to `SKYNET:10300`**, not stand up a second
Parakeet process: running two GPU-resident copies of the same model would waste VRAM
for no reason, and the existing build's own notes make the general point explicitly —
*"same box was an unexamined assumption, not a constraint."* The same logic applies to
"same process."

Three operational traps from that build carry directly into deploying the relay on
this machine:

- **`pythonw.exe` silently loses inbound reachability.** SKYNET has an existing
  firewall rule permitting inbound to `python.exe`, but none for `pythonw.exe` — a
  windowless process can be genuinely listening and still unreachable from the network,
  and `Get-NetTCPConnection -State Listen` won't flag anything wrong. Fix is a
  **port**-scoped inbound rule, never a program-scoped one (a venv rebuild silently
  breaks a program-scoped rule).
- **`onnxruntime-gpu` ≥1.27 needs `ort.preload_dlls()` called before session
  creation**, or it silently falls back to CPU with no error — just much slower
  inference.
- **`LastTaskResult=0` from Task Scheduler proves nothing.** The launcher reports
  success whether the service lived or died. Relevant to deliverable #3 (startup
  procedure) — whatever mechanism starts the relay needs a real liveness probe, not a
  task-result code.

### TTS — Kokoro does not exist on SKYNET

No Kokoro install, process, or service anywhere on the machine — confirmed, not
inferred from an empty port scan. There **is** a working local TTS chain, but it's
unrelated and doesn't fit here: Claude Code's own spoken readback of its chat
responses, routed through **Azure Cognitive Services Neural TTS** (cloud, `eastus2`,
voice `en-US-AndrewNeural`) via `~/.throne/`. Proven reliable on this exact
machine — but it's a cloud call, and reusing it for the voice relay means audio
leaving operator-controlled infrastructure, which is precisely what the brief's
success condition rules out.

This is a real, unresolved gap, not a probe artifact. Two ways forward:

1. **Install and stand up Kokoro-82M fresh** — matches the brief, keeps audio local.
   Default recommendation.
2. **Use the proven Azure TTS chain as an interim fallback** — faster to a working
   demo, but a visible, deliberate deviation from "no audio leaves infrastructure."
   Should be an explicit operator decision, not a silent default.

Whichever is chosen, **whether it can stream output incrementally is still unknown**
and matters more than which engine is picked — see the architecture doc's latency
budget.

```
### 0.3 — Local speech stack reachability
Result: PARTIAL
Source: internal documentation (ZachOperationsHub), not the probe script

STT — Parakeet TDT v2, Wyoming protocol, SKYNET:10300
  RTF ...................... 0.098 (measured: 4.76s audio -> 0.47s inference)
  GPU ....................... RTX 5070 Ti
  Integration ............... relay should be a Wyoming client, not a second process

TTS — Kokoro
  Present? .................. NO
  Existing local TTS ........ Azure Neural TTS (~/.throne/) - different purpose, cloud
  Decision needed ............ install Kokoro fresh, or accept Azure as fallback
  Streaming capability ....... UNKNOWN either way
```

---

## Recommendation

**Phase 1 groundwork can start now; the last physical check still gates going live.**

1. **Finish 0.2**, then **run 0.1**. Retry `tailscale cert` every few minutes until
   the HTTPS Certificates toggle propagates; once it issues, serve `mic-probe.html`
   over it and run the check with the phone and glasses. 0.1 remains the check most
   likely to kill the premise — if the glasses can't be captured, the fallback is the
   Yealink WH63 E2, and everything else here is unaffected.
2. **Decide TTS.** Kokoro-fresh vs. Azure-fallback (0.3) is a real tradeoff between
   matching the brief's local-only goal and time-to-working-demo — the operator's call.
3. **Build target given 0.4** is decided: the thin custom relay, not a fork of
   upstream — reasoning and scoped design in
   [`docs/ARCHITECTURE-DECISION.md`](docs/ARCHITECTURE-DECISION.md). Application code
   is now in progress against the corrected 0.3 findings (Wyoming STT client, not an
   HTTP guess).

One correction to carry into Phase 2 regardless of which way you go: the brief's
`claude-sonnet-4-6` is a **valid current model ID** and a sound latency choice. Note
that the Anthropic API's own interface is the Messages API — not OpenAI-shaped chat
completions — which is a code-level difference either path has to absorb.
