# The glasses-camera client — Android + Meta DAT

What to build to replace the phone camera with the **glasses** camera, once DAT supports
the device. The bridge does not change: the app posts the same JSON the web client posts.

**Target the Ray-Ban Meta Gen 2 Wayfarer**, which DAT supports today. The Oakley Meta
Vanguard is not supported yet (README → *The device finding*).

## Scope note — what is verified here and what is not

Everything in *Build configuration*, *Manifest* and *The contract with the bridge* below is
verified: the first two from Meta's own `facebook/meta-wearables-dat-android` repo, the
third from `vision_bridge.py` in this directory, which I wrote and tested.

**The Kotlin class and method names are deliberately not written out.** Meta's API
reference (`wearables.developer.meta.com/docs/reference/android/dat/0.9`) is blocked by
this environment's egress proxy, and the repo README documents the *feature areas* —
session and stream setup, video frames, photo capture, section-group walking — without the
signatures. Writing plausible-looking `MWDATCamera.capturePhoto(...)` calls from memory
would produce code that compiles in the imagination and fails in Android Studio, which is
worse than no code: it looks finished. CLAUDE.md's rule against coding to remembered
signatures applies exactly here. Open the reference and fill in the four call sites marked
below; everything around them is specified.

## Prerequisites — all of these need Zach

1. **Meta developer enrollment.** Register at `developers.meta.com`, accept the Wearables
   Developer Terms, create an app in the Wearables Developer Center, and note its
   **Application ID**. Only available in AI-glasses-supported countries.
2. **Developer Mode on the glasses**, enabled in the Meta AI app with the glasses paired.
3. **A GitHub personal access token with `read:packages`.** Meta publishes the Android SDK
   to GitHub Packages, not Maven Central, so Gradle cannot even resolve the dependency
   without one. Put it in `local.properties` as `github_token=` (git-ignored) or export
   `GITHUB_TOKEN`.
4. **Android Studio** on SKYNET. No Mac needed — this is the Kotlin path.
5. **Sideload only.** DAT is in developer preview: apps using it cannot ship to the Play
   Store yet. Install to Zach's own phone over USB or via a release channel he owns.

## Build configuration

```kotlin
// settings.gradle.kts
maven {
    url = uri("https://maven.pkg.github.com/facebook/meta-wearables-dat-android")
    credentials {
        password = System.getenv("GITHUB_TOKEN")
            ?: localProperties.getProperty("github_token")
    }
}
```

```kotlin
// app/build.gradle.kts
implementation("com.meta.wearable:mwdat-core:0.9.0")     // discovery, sessions, registration
implementation("com.meta.wearable:mwdat-camera:0.9.0")   // camera access
debugImplementation("com.meta.wearable:mwdat-mockdevice:0.9.0")  // build and test with no glasses
```

`mwdat-display:0.9.0` also exists; it is for Ray-Ban Display and is not needed here.

## Manifest

```xml
<meta-data android:name="com.meta.wearable.mwdat.APPLICATION_ID"
           android:value="YOUR_APP_ID" />

<!-- Both default to enabled. Worth turning off for a camera app on principle. -->
<meta-data android:name="com.meta.wearable.mwdat.ANALYTICS_OPT_OUT"       android:value="true" />
<meta-data android:name="com.meta.wearable.mwdat.CRASH_REPORTING_OPT_OUT" android:value="true" />
```

## What the app does

Four DAT call sites — fill these from the reference:

1. **Initialize** the SDK and register the Application ID.
2. **Discover and connect** to the glasses, handling the compatibility flags. A device DAT
   does not support surfaces here as `DEVICE_UPDATE_REQUIRED` — surface that as
   *"these glasses aren't supported by the SDK yet"*, not as a generic connection failure.
   That distinction is the single most confusing error in this whole stack.
3. **Open a camera session** and take a **still**. Photo capture tops out at 1440×1080 and
   carries EXIF; video streaming is capped at 720p/30fps by the Bluetooth link. Prefer the
   still: it is higher resolution than the stream, and one frame is all the bridge wants.
4. **Close the session** immediately after the capture. Do **not** hold a stream open —
   on-demand capture is the brief's explicit default for privacy and battery, and nothing
   about this flow needs continuous frames.

Everything else is already solved and can be lifted from `client/index.html`, which is a
working implementation of the same flow: Android's `SpeechRecognizer` for the query,
`TextToSpeech` for the answer, and the same one-image-per-turn discipline.

## The contract with the bridge

`POST {bridge}/v1/look`, `Authorization: Bearer {VISION_BEARER}`:

```json
{
  "image_b64":  "<base64 JPEG, no data: prefix>",
  "media_type": "image/jpeg",
  "query":      "what does this label say",
  "session_id": "<stable per app launch, for follow-ups>"
}
```

Response:

```json
{ "ok": true, "text": "…speak this…", "refused": false,
  "session_id": "…", "usage": {...}, "latency_ms": 812 }
```

Notes that will save a debugging session:

- **`media_type` must match the actual bytes.** The bridge sniffs the magic number and
  returns 400 on a mismatch; it will not take your word for it.
- **Send one frame per turn.** History is stored image-free server-side, so re-sending
  prior frames only costs money.
- **`ok: false` comes back with a real HTTP status** — 401 unauthorized, 400 bad image,
  429 rate limited, 502 upstream. Read `error` and say it out loud; every one of them is
  something the wearer can act on.
- **Downscale before sending.** ~1280px on the longest edge is the sweet spot: image tokens
  scale with area, and Claude downsamples above ~1568px anyway, so a full-size frame costs
  more and shows no more detail.
