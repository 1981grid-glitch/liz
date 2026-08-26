# Architecture Decision — Voice Relay Build Target

**Date:** 2026-08-22
**Status:** Proposed. Awaiting operator go/no-go.
**Driver:** Check 0.4 disproved the brief's Phase 2 assumption. See
[`../FINDINGS-TASK-0.md`](../FINDINGS-TASK-0.md).

---

## Decision

**Build a thin custom WebSocket relay. Do not fork `openclaw-voice`; do not adopt
`voiceclaw`.**

The brief anticipated this outcome: *"If the upstream project does not fit, say so and
propose the smaller custom build rather than forcing it."* It does not fit.

---

## Why not fork upstream

Upstream's server is 1,386 lines. The three modules that would have to be replaced —
`stt.py`, `tts.py`, `backend.py` — are the substance of the project: local Whisper,
the ElevenLabs cascade, and the OpenAI client. Replace all three and what remains is
the WebSocket loop (`main.py`) and the browser client.

Both of those carry defects that have to be fixed anyway (0.4 findings table):
barge-in is structurally impossible in the current loop, conversation history is a
process-global singleton truncated to five exchanges, the server binds `0.0.0.0` with
auth off, and the client page is served from a relative path that breaks under a
Windows service.

So the fork inherits ~1,000 lines of someone else's abstractions, deletes the ~450
that do the work, and still requires rewriting the loop that remains. The glue that
actually has to exist here — audio in, Parakeet, Anthropic, Kokoro, audio out — is a
few hundred lines. Owning that directly is the smaller, more maintainable job, and it
puts the parts that matter for this build (barge-in, session state, binding) under
our control instead of behind a patch set we would have to carry.

**What upstream is still good for:** it is MIT-licensed and worth reading before
writing the loop — the sentence-boundary chunking in `main.py:301-326` and the
markdown-stripping in `text_utils.py` are both reasonable, and re-deriving them would
be wasted effort. Reuse the ideas, not the dependency.

## Why not `voiceclaw`

It is rejected on the objective, not on quality. Its voice path requires a third-party
realtime speech-to-speech provider (Gemini Live, Grok Voice, or OpenAI Realtime), so
operator audio would leave operator-controlled infrastructure — contradicting the
stated success condition. It also has no local STT/TTS path, and ships iOS and macOS
clients rather than a browser page.

---

## Proposed scope

Python 3.11 + FastAPI + `uvicorn`, matching the language SKYNET's speech stack already
runs in. One process. No new heavy dependencies — `anthropic`, `httpx`, `fastapi`.

| Piece | Approach | Addresses |
|---|---|---|
| Browser client | One static page: mic capture, WS, PCM playback queue | C1, C4 |
| Transport | Single WebSocket, binary audio frames, JSON control | — |
| STT | **Wyoming-protocol client** to the existing Parakeet bridge at `SKYNET:10300` (not a new HTTP service — see 0.3) | 0.3, privacy, avoids duplicate GPU load |
| Model | `anthropic` SDK, `messages.stream()`, `claude-sonnet-4-6` | C5 |
| TTS | HTTP to local Kokoro — **not yet installed, per 0.3**; Azure Neural TTS (`~/.throne/`) is a proven but cloud fallback | latency, C1 objective |
| Session state | Per-connection, token-budgeted | C5, Phase 4 |
| Bind | Loopback (`127.0.0.1`) only — `tailscale serve` does the tailnet exposure | Phase 2.5, C1 |
| TLS | `tailscale serve` in front | C1 |

### Three things to get structurally right from the start

These are the points where upstream's design forecloses a Phase 4 requirement, so they
are design constraints, not features to add later.

**1. Barge-in requires a concurrent loop.** Upstream synthesizes and sends the whole
response inline inside its message handler, so the socket is not read again until
playback finishes — an interrupt physically cannot arrive. The relay instead runs the
receive loop and the synth/send task concurrently, with the send task cancellable. When
inbound speech is detected during playback, cancel the task, drop the queued audio, and
truncate the assistant turn in history to what was actually spoken aloud — otherwise
the model believes it said things the operator never heard. Without this, barge-in is
not a limitation to note; it is unreachable.

**2. Conversation state is per-connection and token-budgeted.** A fixed 10-message
window fails the brief's 10+ turn test by construction. Keep full history per
connection, trim by token budget, and — since cellular↔wifi handoff will drop the
socket — key sessions by a client-held session ID so a reconnect resumes rather than
starts over.

**3. Bind to loopback, not the tailnet address.** `0.0.0.0` plus the default-off auth
in upstream is an unauthenticated endpoint that spends an API key, on every interface —
that part of the original reasoning was right. But the fix isn't binding to the
Tailscale IP directly; it's binding to `127.0.0.1` and letting `tailscale serve` do
the actual tailnet exposure. `tailscale serve` terminates TLS and proxies to the app
over loopback — that's the whole mechanism, and it's also what C1 needs, since the app
itself speaks plain `ws://`/`http://`, not `wss://`/`https://`. Bound to the tailnet IP
directly, the raw unencrypted endpoint would be reachable by anything on the tailnet at
`http://<tailnet-ip>:8765`, bypassing `tailscale serve` and its TLS entirely if anyone
— including the client, by mistake — hit that address instead of the HTTPS one. Bound
to loopback, the app is unreachable from any network interface, tailnet included; the
only way in is through `tailscale serve`'s proxy. That also means no inbound firewall
rule is needed for the app's own port — loopback traffic doesn't reach the Windows
Firewall's inbound-rules path, and `tailscale serve` runs through the already-allowed
`tailscaled` process, not a new listening socket. Still require the shared secret on
top of this — defense in depth against anything else on the tailnet.

### Traps inherited from SKYNET's existing speech stack

0.3 turned up detailed build notes from an unrelated project (a Home Assistant voice
bridge) that hit these exact problems on this exact machine. They apply directly:

- **`pythonw.exe` loses inbound reachability.** SKYNET's firewall permits inbound to
  `python.exe`, not `pythonw.exe` — launch the relay as `python.exe` (console or not),
  or add an explicit **port**-scoped inbound rule. Never a program-scoped rule; a venv
  rebuild silently breaks those.
- **`onnxruntime-gpu` ≥1.27 needs `ort.preload_dlls()` before session creation**, or it
  falls back to CPU with no error — only relevant if the relay ever loads a model
  in-process rather than going through the Wyoming bridge, but worth knowing given how
  silent the failure is.
- **A green Task Scheduler result proves nothing.** `LastTaskResult=0` reports success
  whether the service lived or died. Deliverable #3 (startup procedure) needs an actual
  liveness check, whatever the launch mechanism turns out to be.

### Latency budget

The target is <2 s from speech-end to audio-start. The realistic allocation:

| Stage | Budget | Note |
|---|---|---|
| VAD endpointing | 200–300 ms | Silence hangover; the tunable with the largest perceived effect |
| STT (Parakeet) | 150–400 ms | Local, over Wyoming; **measured RTF 0.098** on this hardware confirms the budget holds |
| Model first token | 400–800 ms | Dominated by system-prompt length — keep it short (C5) |
| TTS first audio | 150–400 ms | **Only if Kokoro streams — unconfirmed, Kokoro isn't installed yet.** Batch-only pushes this to full-utterance synthesis |
| Network + playback | 50–100 ms | Tailnet, same premises |

Two properties decide whether this lands: **whether Kokoro streams** (0.3) and **the
first-sentence chunking**. Synthesize on the first sentence boundary rather than the
full response — upstream's approach in `main.py:301-326` is sound here. Instrument
each stage from day one; the brief asks for a measured breakdown, and it cannot be
reconstructed afterward.

On model choice: `claude-sonnet-4-6` is a valid current ID and the right call for
conversational duplex. Omit the `thinking` parameter — on Sonnet 4.6, omitting it
means no thinking, which is what you want for turn latency. `claude-haiku-4-5` is the
fallback if first-token latency proves too high. Note the Anthropic API's interface is
the Messages API, not OpenAI-shaped chat completions.

---

## Risks this does not remove

- **C3 stands regardless of architecture.** If 0.1 measures narrowband, no relay design
  recovers information the Bluetooth link never carried. Upsampling to Parakeet's
  expected rate satisfies the interface without improving accuracy. The mitigation is
  the Yealink, not code.
- **C4 is untested until it is tested on the device.** Screen-off behaviour may require
  a wake lock or PWA install, and may simply not survive. Test it early — it is the
  core use case, and finding out late invalidates the walking-around premise.
- **TTS is a real gap, not a formality.** Kokoro isn't installed anywhere on SKYNET
  (confirmed, per 0.3) — this is new build work, not wiring to something already
  running. The Azure Neural TTS chain proven elsewhere on this machine is a legitimate
  fallback, but using it means accepting that response audio leaves the tailnet.
- **This pipeline remains not cleared for PHI**, per the brief's §7. Nothing here
  changes that, and the relay should carry no path to case data. Notably, SKYNET's
  local dictation tool exists specifically *because* cloud STT is prohibited under a
  BAA — the same reasoning applies here, and is exactly why Kokoro (local) is the
  default over Azure TTS (cloud) despite Azure being the path of least resistance.

## Effort

Roughly a day to a working relay once Task 0 clears, assuming 0.1 passes and 0.3
returns usable endpoints — plus Phase 4 measurement, which is where the real time goes.
Comparable to, or less than, forking upstream, with none of the inherited patch burden.

## If 0.1 fails

The relay design is unaffected — only the audio source changes. Swap the glasses for
the Yealink WH63 E2 and every other constraint holds. Worth knowing before committing
either way, which is why 0.1 should be run before any code is written.
