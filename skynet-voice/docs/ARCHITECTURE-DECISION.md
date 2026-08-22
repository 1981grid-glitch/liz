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
| STT | HTTP to local Parakeet, per 0.3 | 0.3, privacy |
| Model | `anthropic` SDK, `messages.stream()`, `claude-sonnet-4-6` | C5 |
| TTS | HTTP to local Kokoro, streamed if 0.3 says it can | latency |
| Session state | Per-connection, token-budgeted | C5, Phase 4 |
| Bind | Tailscale IP explicitly, never `0.0.0.0` | Phase 2.5 |
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

**3. Bind to the tailnet address explicitly.** `0.0.0.0` plus the default-off auth in
upstream is an unauthenticated endpoint that spends an API key, on every interface.
Bind to the Tailscale IP, scope the firewall rule to the Tailscale interface, and
require a shared secret even on the tailnet.

### Latency budget

The target is <2 s from speech-end to audio-start. The realistic allocation:

| Stage | Budget | Note |
|---|---|---|
| VAD endpointing | 200–300 ms | Silence hangover; the tunable with the largest perceived effect |
| STT (Parakeet) | 150–400 ms | Local; utterance-length dependent |
| Model first token | 400–800 ms | Dominated by system-prompt length — keep it short (C5) |
| TTS first audio | 150–400 ms | **Only if Kokoro streams.** Batch-only pushes this to full-utterance synthesis |
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
- **This pipeline remains not cleared for PHI**, per the brief's §7. Nothing here
  changes that, and the relay should carry no path to case data.

## Effort

Roughly a day to a working relay once Task 0 clears, assuming 0.1 passes and 0.3
returns usable endpoints — plus Phase 4 measurement, which is where the real time goes.
Comparable to, or less than, forking upstream, with none of the inherited patch burden.

## If 0.1 fails

The relay design is unaffected — only the audio source changes. Swap the glasses for
the Yealink WH63 E2 and every other constraint holds. Worth knowing before committing
either way, which is why 0.1 should be run before any code is written.
