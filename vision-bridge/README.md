# vision-bridge

Camera + voice from Zach's glasses (or phone) into Claude, answer spoken back. Hands-free.

**Status: the bridge is built, tested and runnable. The glasses-camera client is not,
and the reason is Meta's, not ours — see [The device finding](#the-device-finding).**

---

## The device finding — read this first

The brief assumed a camera frame can be pulled from the Ray-Ban Meta glasses. It can, but
only down one path, and that path decides the whole architecture:

**There is no way to reach the glasses camera except through a phone app.** Meta's
Wearables Device Access Toolkit (DAT) is a *mobile* SDK — iOS Swift and Android Kotlin
only. There is no desktop SDK, no web API, no BLE protocol to talk to directly. SKYNET
cannot see through the glasses; neither can a server. The topology is always:

```
glasses ──DAT──▶ phone app ──HTTPS──▶ bridge ──▶ Claude
```

Two things follow, and the second one is the blocker:

**1. The Vanguard is not supported.** Zach named the Oakley Meta Vanguard. DAT's device
enum contains `oakley_meta_vanguard`, but the firmware that enables DAT on it has not
shipped. Meta's own answer, on the Android SDK tracker (2026-01-28, Lansing Lee): *"The
SDK does not yet support Oakley Meta Vanguard … but we will be adding that support soon."*
No ETA was given then and I found no 2026 announcement that it landed. Connecting one
today returns `DEVICE_UPDATE_REQUIRED` and *"Oakley Meta 007N requires an update to work
with this app."*

**2. The Ray-Ban Meta Gen 2 Wayfarer IS supported** — Zach mentioned owning these too,
and that is the device this project should target. Ray-Ban Meta Gen 1 and Gen 2, Ray-Ban
Meta Optics, and Oakley Meta HSTN are on the supported list today.

⚠️ *Meta's documentation sites (`developers.meta.com`, `wearables.developer.meta.com`) are
blocked by this environment's egress proxy, so the supported-device list and the DAT API
reference could not be read first-hand. Everything above comes from Meta's GitHub repos,
the SDK tracker, and press coverage. **Re-check the supported list before spending money
on this** — that page is the authority and it is one Zach can open and I cannot.*

### What that means for the deliverable

The bridge — the actual engineering, and the device-agnostic 90% — is **built, tested, and
runs**. The client ships in two halves:

| Client | Camera | Works today? | Needs |
|---|---|---|---|
| **Web client** (in this repo, done) | **phone** | **Yes, right now** | Nothing. Open a URL. |
| **Android + DAT app** (specified, not built) | **glasses** | Not yet | Meta dev enrollment; see `ANDROID-DAT-PATH.md` |

The web client gets Zach hands-free voice in and out **through the glasses today** — they
already work as a Bluetooth headset, which is how he uses them now — with the phone
supplying the camera. That is the honest gap: **point-of-view capture still needs the
native app.** The bridge does not change when that arrives; only the client does.

---

## Architecture decision, and why

The brief offered local-on-SKYNET versus remote HTTP MCP. **Remote, on Azure Container
Apps**, matching the cartographer and throne-mcp pattern.

The deciding argument is the use case, not the infrastructure. This is for reading labels,
parts and signs *while out and about* — which means the phone must reach the bridge over
cellular, from anywhere. A SKYNET-hosted bridge only answers on the home network, which
fails the exact scenario the tool exists for. Reachability isn't a nice-to-have here the
way it was for cartographer's tappable map link; it's the whole feature.

The costs of that choice, stated plainly:

- **Frames transit a cloud service.** They are sent to a container Zach owns, in his own
  subscription, and are never written to disk — but they do leave the phone. A local
  bridge would keep them on the LAN. Given that the frames are already going to the
  Claude API either way, this adds one hop, not a new category of exposure.
- **One extra round trip** versus a phone-to-Anthropic direct call. Measured latency is
  reported in the response and shown in the client so this stays honest.
- **It is not free**, though it is close — see [Cost](#cost).

### Two things it is deliberately *not*

**Not an MCP server.** MCP is a *pull* surface: Claude calls a tool when it wants
something. This flow is *push* — the glasses initiate, and Claude is the thing being
called. Modelling it as MCP would mean a Claude session sitting somewhere waiting, which
re-introduces exactly the always-on host that the remote decision was meant to avoid. An
MCP face is a good *second* mode ("Claude, look through my glasses" from a Claude Code
session on SKYNET); it is additive and noted below, not the foundation.

**No audio handling on the server.** Speech-to-text and text-to-speech both run on the
phone, using the browser's built-in engines. That is not a shortcut — it is faster (no
audio upload, no synthesis round trip), free (server-side TTS is a per-character bill),
and it routes correctly by construction: the phone speaks to whatever audio device is
connected, which is the glasses. The bridge is purely image + text → text.

---

## What's here

```
vision_bridge.py        the server — one endpoint, POST /v1/look
client/index.html       the web client, served from GET /
test_vision_bridge.py   94 checks, ast-extracted from the real source
Dockerfile              python:3.12-slim, non-root, port 8080
requirements.txt        pinned — read the comment before changing anthropic
ANDROID-DAT-PATH.md     the glasses-camera app: what to build when DAT is ready
```

## Run it

```bash
cd vision-bridge
python -m venv .venv && .venv/bin/pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
export VISION_BEARER=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
.venv/bin/python vision_bridge.py          # serves on :8080
```

Then open the bridge URL on the phone, tap ⚙, paste the URL and the bearer token, and
add it to the home screen. Tap **Ask**, speak, and the answer comes back out loud.

Tests, which need nothing installed:

```bash
python test_vision_bridge.py               # exit 0 = green
```

The camera requires a **secure context** — HTTPS, or `localhost`. Over plain HTTP on a
LAN IP the browser will refuse to open the camera and the page will say so. That is
another reason the deployed path is the real one.

## Deploy

Same shape as `throne-mcp/CLOUD-SHELL-DEPLOY.md`, browser-only from Azure Cloud Shell.
Per CLAUDE.md, **ask Azure what is live and compute the next free tag — never reuse one:**

```bash
RG=rg-throne-mcp ACR=thronemcpe9ebfc APP=vision-bridge
ENVID=$(az containerapp show -g $RG -n throne-mcp --query properties.environmentId -o tsv)
TAG=1.0                                      # confirm free: az acr repository show-tags -n $ACR --repository vision-bridge
az acr build -r $ACR -t vision-bridge:$TAG .

az containerapp create -g $RG -n $APP --environment "$ENVID" \
  --image $ACR.azurecr.io/vision-bridge:$TAG \
  --target-port 8080 --ingress external --min-replicas 0 --max-replicas 1 \
  --secrets anthropic-key=sk-ant-... bearer=<token> \
  --env-vars ANTHROPIC_API_KEY=secretref:anthropic-key VISION_BEARER=secretref:bearer
```

`--min-replicas 0` is what keeps this near free when idle. It costs a cold start (a few
seconds on the first look after a quiet period), which is the right trade for a tool used
in bursts.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required. Boot fails loudly without it. |
| `VISION_BEARER` | — | Required, or set `VISION_ALLOW_ANON=1` on purpose. Boot fails closed. |
| `VISION_MODEL` | `claude-opus-5` | |
| `VISION_EFFORT` | `low` | Latency lever. Keeps thinking on; see below. |
| `VISION_MAX_TOKENS` | `1000` | Answers are spoken, so they must be short. |
| `VISION_RATE_PER_MIN` | `20` | Per-token. Bounds a stuck client. |
| `VISION_DAILY_MAX` | `500` | Per-token. Bounds a leaked token. |

**On effort:** Opus 5 runs adaptive thinking by default and `low` effort is the sanctioned
way to cut latency and spend on a route like this. Disabling thinking outright is *not* —
on Opus 5 it has two documented failure modes, including writing a tool call into visible
text. Leave thinking on and lower effort.

**On the model:** `claude-opus-5`, deliberately. Vision quality is the product here —
reading a worn label or a part number at an angle is exactly where a stronger model earns
its cost, and the cost is around a cent per look. `VISION_MODEL=claude-sonnet-5` cuts that
roughly 60% if Zach wants it; that's his call to make, not a default to quietly take.

## Cost

Per look, at the default 1280px capture (~1,640 image tokens + ~250 system/query in,
~80 out), on Opus 5 at $5/$25 per MTok:

```
in   1,900 × $5/1M  = $0.0095
out     80 × $25/1M = $0.0020
                      ─────────
                      ~$0.0115 per look
```

**100 looks/month ≈ $1.15. The 500/day cap ≈ $5.75 in a worst-case day.** Container Apps
at `min-replicas 0` adds roughly $0–3/month at this volume. Comfortably inside the ~$10
target, and the daily cap is what keeps a leaked token from blowing through it.

Every response carries its own token counts, latency, and a cost estimate, and the client
prints them under the answer — so this stays measured rather than assumed.

## Security posture

This sits on public HTTPS ingress, holds an Anthropic API key, and sees a camera pointed
at Zach's actual life. Written to the same posture as `onenote-mcp`, with one addition
specific to a camera:

- **Auth fails closed.** No `VISION_BEARER` means nothing is authorized; running open
  requires `VISION_ALLOW_ANON=1` explicitly, and the flag is compared to exactly `"1"`,
  not any truthy string. Constant-time compare. The server refuses to boot misconfigured
  rather than discovering it on the first request.
- **The declared media type is never trusted.** Bytes are sniffed by magic number and the
  claim is checked against them; a mismatch is a 400. The sniffed value is what goes to
  Claude, so a mislabelled file cannot have its label forwarded.
- **The log never records what the wearer looked at.** `_slog` drops `image`, `image_b64`,
  `query`, `text` and `answer` keys structurally, at the choke point — not at call sites —
  so a future edit cannot reintroduce the leak by formatting a query into a log line. Same
  construction as throne-mcp's `_scrub_phi`, for the same reason: the trail outlives the
  request. There is no flag to disable it.
- **Image text is framed as data.** A sign, screen or label in view can contain words that
  read as instructions. The system prompt says to report them and never act on them. The
  blast radius is small — this server has no tools — but it is the right default before an
  MCP face is added, at which point it stops being cheap insurance and starts being load-bearing.
- **Rate limited per token**, per minute and per day, because a leaked bearer spends real
  money at Opus rates.
- **`/health` is unauthenticated and leaks nothing** — booleans for whether the key and
  bearer are configured, never a value, prefix, or length. Pinned by a test.
- **Nothing is written to disk.** Session context is in-process and lost on restart. For a
  camera feed that is the right default, not a limitation.

## Adding an MCP face

The second mode worth building: an MCP tool so a Claude Code session on SKYNET can *pull*
a frame — "look through my glasses and tell me what part this is" — instead of the phone
pushing one. It needs a held-open channel from the phone (WebSocket or long-poll) so a
frame can be requested on demand, which is the piece that isn't here. `_look()` is already
the whole flow with the network call isolated, so an MCP tool would wrap it rather than
duplicate it.

## What still needs Zach

Nothing, for the web client — it runs as soon as the bridge is deployed.

For the glasses camera, `ANDROID-DAT-PATH.md` has the full list. The short version, and
these are genuinely the blockers:

1. **Confirm whether DAT now supports the Vanguard** — or plan to use the **Gen 2
   Wayfarer**, which is supported today. This decides whether the app is worth building yet.
2. **Meta developer enrollment** at `developers.meta.com`, accept the Wearables Developer
   Terms, register an app, and put its Application ID in the manifest. Only Zach can do this.
3. **Developer Mode on the glasses**, via the Meta AI app.
4. **A GitHub personal access token with `read:packages`** — Meta ships the Android SDK
   through GitHub Packages, not Maven Central, so the build needs a token just to resolve.
5. **Confirm which phone platform.** This targets **Android** (Kotlin SDK, Android Studio
   on Windows, no Mac needed), inferred from Zach's own mail signatures — *"Sent from my
   T-Mobile 5G Device / Get Outlook for Android."* If that's wrong the bridge is unchanged
   and only the client swaps; worth confirming before anyone opens Android Studio.

Note that the reference project, `mrdulasolutions/visionclaude`, **cannot be used as-is**:
it requires macOS 13+, an iPhone on iOS 17+, and Xcode, and states plainly that Android is
not supported. On a Windows workstation and an Android phone it is a design reference, not
a starting point.
