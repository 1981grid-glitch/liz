import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .config import settings
from .llm import AnthropicRelay
from .session import SessionStore
from .stt_wyoming import WyomingSTT
from .text_utils import IncrementalSentenceSplitter, clean_for_speech
from .tts import build_tts_backend

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("relay")

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"

sessions = SessionStore()
stt = WyomingSTT(settings.stt_wyoming_host, settings.stt_wyoming_port, settings.audio_sample_rate)
llm = AnthropicRelay(settings.anthropic_api_key, settings.relay_model)
tts = build_tts_backend(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    aclose = getattr(tts, "aclose", None)
    if aclose:
        await aclose()


app = FastAPI(title="SKYNET Voice Relay", lifespan=lifespan)


@app.get("/")
@app.get("/voice")
async def index():
    return FileResponse(CLIENT_DIR / "index.html")


async def speak_sentence(websocket: WebSocket, sentence: str) -> None:
    speech_text = clean_for_speech(sentence)
    if not speech_text:
        return
    await websocket.send_json({"type": "tts_start", "sample_rate": tts.sample_rate})
    async for chunk in tts.synthesize_stream(speech_text):
        await websocket.send_bytes(chunk)
    await websocket.send_json({"type": "tts_end"})


async def handle_utterance(session, websocket: WebSocket, audio_bytes: bytes) -> None:
    accumulated_text = ""
    try:
        transcript = await stt.transcribe(audio_bytes)
        if not transcript.strip():
            return
        await websocket.send_json({"type": "transcript", "text": transcript})

        async with session.lock:
            session.add_user_turn(transcript)
            session.trim_to_budget(settings.session_max_tokens)

        splitter = IncrementalSentenceSplitter()
        async for delta in llm.stream_reply(session):
            accumulated_text += delta
            await websocket.send_json({"type": "response_text", "text": delta})
            for sentence in splitter.feed(delta):
                await speak_sentence(websocket, sentence)

        remainder = splitter.flush()
        if remainder:
            await speak_sentence(websocket, remainder)

        async with session.lock:
            session.add_assistant_turn(accumulated_text)
        await websocket.send_json({"type": "response_complete"})

    except asyncio.CancelledError:
        async with session.lock:
            session.add_assistant_turn(accumulated_text)
        raise
    except Exception as exc:
        logger.exception("utterance handling failed")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if token != settings.relay_shared_secret:
        await websocket.close(code=4001)
        return

    await websocket.accept()

    session = None
    audio_buffer = bytearray()
    active_task: asyncio.Task | None = None

    try:
        hello_raw = await websocket.receive_text()
        hello = json.loads(hello_raw)
        session = sessions.get_or_create(hello.get("session_id"))
        await websocket.send_json({"type": "hello", "session_id": session.id})

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                audio_buffer.extend(message["bytes"])
                continue

            if message.get("text") is None:
                continue

            payload = json.loads(message["text"])
            msg_type = payload.get("type")

            if msg_type == "speech_start":
                if active_task and not active_task.done():
                    active_task.cancel()
                    try:
                        await active_task
                    except asyncio.CancelledError:
                        pass
                    await websocket.send_json({"type": "interrupted"})
                audio_buffer.clear()

            elif msg_type == "speech_end":
                utterance = bytes(audio_buffer)
                audio_buffer.clear()
                if utterance:
                    active_task = asyncio.create_task(
                        handle_utterance(session, websocket, utterance)
                    )

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket loop error")
    finally:
        if active_task and not active_task.done():
            active_task.cancel()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.relay_host, port=settings.relay_port)
