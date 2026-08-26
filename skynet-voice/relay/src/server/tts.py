import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator
from xml.sax.saxutils import escape

import httpx

logger = logging.getLogger(__name__)


class TTSBackend(ABC):
    sample_rate: int

    @abstractmethod
    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield raw PCM16 mono chunks at self.sample_rate."""
        if False:
            yield b""


class KokoroTTS(TTSBackend):
    """Kokoro-FastAPI (github.com/remsky/Kokoro-FastAPI), OpenAI-compatible
    /v1/audio/speech. Not yet installed on SKYNET as of Task 0.3 — see
    relay/README.md. 24kHz mono output and the af_bella voice name are
    confirmed against the project's own docs, not assumed."""

    sample_rate = 24000

    def __init__(self, base_url: str, voice: str):
        self.base_url = base_url.rstrip("/")
        self.voice = voice
        self._client = httpx.AsyncClient(timeout=30.0)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        payload = {
            "model": "kokoro",
            "input": text,
            "voice": self.voice,
            "response_format": "pcm",
            "speed": 1.0,
        }
        async with self._client.stream(
            "POST", f"{self.base_url}/v1/audio/speech", json=payload
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=4096):
                if chunk:
                    yield chunk

    async def aclose(self) -> None:
        await self._client.aclose()


class AzureTTS(TTSBackend):
    """Azure Cognitive Services Neural TTS — the chain already proven at
    C:\\Users\\1981g\\.throne\\ on SKYNET, reused here as an explicit,
    deliberate fallback (see docs/ARCHITECTURE-DECISION.md). Cloud call:
    response audio leaves the tailnet. Azure's REST endpoint returns one
    complete audio blob, not a progressive stream, so the first chunk
    yielded here is the whole utterance — TTS-stage latency is therefore
    the full synthesis time, not the ~150-400ms budgeted for a streaming
    backend."""

    sample_rate = 24000

    def __init__(self, key: str, region: str, voice: str):
        self.key = key
        self.region = region
        self.voice = voice
        self._client = httpx.AsyncClient(timeout=30.0)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        ssml = (
            '<speak version="1.0" xml:lang="en-US">'
            f'<voice name="{self.voice}">{escape(text)}</voice>'
            "</speak>"
        )
        response = await self._client.post(
            f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1",
            content=ssml.encode("utf-8"),
            headers={
                "Ocp-Apim-Subscription-Key": self.key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "raw-24khz-16bit-mono-pcm",
            },
        )
        response.raise_for_status()
        yield response.content

    async def aclose(self) -> None:
        await self._client.aclose()


def build_tts_backend(settings) -> TTSBackend:
    if settings.tts_backend == "kokoro":
        return KokoroTTS(settings.kokoro_url, settings.kokoro_voice)
    if settings.tts_backend == "azure":
        if not settings.azure_tts_key:
            raise RuntimeError("TTS_BACKEND=azure requires AZURE_TTS_KEY")
        return AzureTTS(settings.azure_tts_key, settings.azure_tts_region, settings.azure_tts_voice)
    raise RuntimeError(f"Unknown TTS_BACKEND: {settings.tts_backend}")
