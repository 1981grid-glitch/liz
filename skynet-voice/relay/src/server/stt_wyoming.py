import logging

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient

logger = logging.getLogger(__name__)

_CHUNK_SAMPLES = 1600  # 100ms at 16kHz


class WyomingSTT:
    """Client for the parakeet-wyoming-bridge service on SKYNET:10300
    (see Task 0.3 findings — this is an existing service, not one this
    relay hosts). Wyoming is a network protocol; there is no reason to
    also run a second, GPU-resident copy of the model in-process."""

    def __init__(self, host: str, port: int, sample_rate: int = 16000):
        self.host = host
        self.port = port
        self.sample_rate = sample_rate

    async def transcribe(self, pcm16_mono: bytes) -> str:
        async with AsyncTcpClient(self.host, self.port) as client:
            await client.write_event(Transcribe(language="en").event())
            await client.write_event(
                AudioStart(rate=self.sample_rate, width=2, channels=1).event()
            )

            chunk_bytes = _CHUNK_SAMPLES * 2
            for offset in range(0, len(pcm16_mono), chunk_bytes):
                chunk = pcm16_mono[offset : offset + chunk_bytes]
                await client.write_event(
                    AudioChunk(
                        audio=chunk, rate=self.sample_rate, width=2, channels=1
                    ).event()
                )

            await client.write_event(AudioStop().event())

            while True:
                event = await client.read_event()
                if event is None:
                    logger.warning("Wyoming connection closed before a transcript arrived")
                    return ""
                if Transcript.is_type(event.type):
                    return Transcript.from_event(event).text or ""
