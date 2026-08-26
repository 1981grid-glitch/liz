# SKYNET Voice Relay — Task 0 Verification Spike

Verification instrumentation and findings for the conversational voice relay build
(Meta Ray-Ban glasses → Android Chrome → Tailscale → SKYNET → Claude).

**No application code has been written.** The brief holds Phase 1+ until all four
Task 0 checks record a PASS/FAIL, and three of them require hardware that is not
reachable from where this work ran.

## Start here

**[`FINDINGS-TASK-0.md`](FINDINGS-TASK-0.md)** — the Task 0 deliverable.
Check 0.4 is complete and **fails**; 0.1–0.3 are unrun with probes provided.

**[`docs/ARCHITECTURE-DECISION.md`](docs/ARCHITECTURE-DECISION.md)** — what 0.4's
failure means for the build, and the recommended alternative.

## Running the remaining checks

Order matters: **0.2 before 0.1**, because the mic probe needs HTTPS to run at all.

```powershell
# 0.2 — Tailscale cert + tailnet reachability   (on SKYNET)
.\spike\probe-tailscale.ps1                 # read-only survey
.\spike\probe-tailscale.ps1 -IssueCert      # actually issue the cert

# 0.3 — Parakeet / Kokoro discovery + schema   (on SKYNET)
.\spike\probe-speech-stack.ps1
```

```powershell
# 0.1 — serve the mic probe over the HTTPS from 0.2, then open it on the phone
tailscale serve --https=443 --set-path=/probe file:C:\path\to\spike\mic-probe.html
```

Then open `https://<magicdns-name>/probe` in Android Chrome with the glasses
connected, grant mic access, and speak steadily for the full capture.

Each probe prints a paste-ready findings block. Paste them into the matching section
of `FINDINGS-TASK-0.md`.

## What's here

| Path | Purpose |
|---|---|
| `FINDINGS-TASK-0.md` | Task 0 findings — the deliverable |
| `docs/ARCHITECTURE-DECISION.md` | Build-target recommendation following 0.4 |
| `spike/mic-probe.html` | 0.1 — measures capture source and true audio bandwidth |
| `spike/bandwidth-classifier.test.js` | Unit tests for the probe's classifier (`node`) |
| `spike/probe-tailscale.ps1` | 0.2 — cert issuance, MagicDNS, peers, firewall |
| `spike/probe-speech-stack.ps1` | 0.3 — endpoint discovery and OpenAPI schema capture |

## A note on the 0.1 method

The brief asks for `AudioContext.sampleRate` as the record of capture rate. That number
cannot answer the narrowband question — Android resamples to the mixer rate regardless
of what the Bluetooth link negotiated, and a page that forces a rate is simply told
back the value it asked for.

The probe measures **spectrally** instead: it requests the mic unconstrained with
processing disabled, then tests whether real content exists above the noise floor at
4.5–6.5 kHz and 9–12 kHz. It declines to classify when dynamic range is too low rather
than reporting a confident wrong answer, and plots the spectrum so the operator can
judge the cutoff directly. Reasoning in `FINDINGS-TASK-0.md` §0.1.

```bash
node spike/bandwidth-classifier.test.js   # 9 cases, all passing
```
