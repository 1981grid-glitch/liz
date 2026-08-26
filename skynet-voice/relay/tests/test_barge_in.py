"""
Verifies the relay's central design commitment (docs/ARCHITECTURE-DECISION.md
"Three things to get structurally right"): a new speech_start mid-response
must cancel the in-flight reply, notify the client, and commit only the
partial assistant text actually spoken -- never the full untruncated reply
-- so the model's own history never claims it said something the operator
didn't hear.

STT/LLM/TTS are mocked; no live Wyoming/Kokoro/Anthropic services required.
Run with: python3 tests/test_barge_in.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
os.environ.setdefault("RELAY_HOST", "127.0.0.1")
os.environ.setdefault("RELAY_SHARED_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

import server.main as main_mod  # noqa: E402

FULL_REPLY = "I'm here and listening carefully to everything you say."


class FakeSTT:
    async def transcribe(self, audio_bytes: bytes) -> str:
        return "hello are you there"


class FakeTTS:
    sample_rate = 24000

    async def synthesize_stream(self, text: str):
        yield b"\x00\x01\x02\x03"


class FakeLLM:
    async def stream_reply(self, session):
        for word in (w + " " for w in FULL_REPLY.split(" ")):
            yield word
            await asyncio.sleep(0.05)


def test_barge_in_truncates_history():
    main_mod.stt = FakeSTT()
    main_mod.tts = FakeTTS()
    main_mod.llm = FakeLLM()

    client = TestClient(main_mod.app)
    with client.websocket_connect("/ws?token=test-secret") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        session_id = ws.receive_json()["session_id"]

        ws.send_json({"type": "speech_start"})
        ws.send_bytes(b"\x00" * 3200)
        ws.send_json({"type": "speech_end"})

        transcript_msg = ws.receive_json()
        assert transcript_msg == {"type": "transcript", "text": "hello are you there"}

        first_delta = ws.receive_json()
        assert first_delta["type"] == "response_text"

        # Barge in while the reply is still streaming.
        ws.send_json({"type": "speech_start"})

        saw_interrupted = False
        for _ in range(30):
            try:
                msg = ws.receive_json()
            except Exception:
                continue
            if msg.get("type") == "interrupted":
                saw_interrupted = True
                break
        assert saw_interrupted, "never received 'interrupted' after barge-in"

    session = main_mod.sessions.get_or_create(session_id)
    assert session.history[-2] == {"role": "user", "content": "hello are you there"}
    assert session.history[-1]["role"] == "assistant"
    spoken = session.history[-1]["content"]
    assert spoken, "partial assistant text should be non-empty"
    assert spoken != FULL_REPLY, (
        "the full untruncated reply was committed -- cancellation did not "
        "actually cut off generation, which defeats the point of barge-in"
    )


if __name__ == "__main__":
    test_barge_in_truncates_history()
    print("PASS: barge-in cancels in-flight reply and commits only partial text")
    print("\nALL BARGE-IN TESTS PASSED")
