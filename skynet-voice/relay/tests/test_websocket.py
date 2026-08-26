"""
Plain-script tests, no pytest dependency — run with:
    python3 tests/test_websocket.py

Exercises the WebSocket handshake and session logic in-process, with no
live Wyoming/Kokoro/Anthropic services required (see test_barge_in.py for
the mocked-pipeline tests that need a running event loop around them).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
os.environ.setdefault("RELAY_HOST", "127.0.0.1")
os.environ.setdefault("RELAY_SHARED_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from server.main import app  # noqa: E402


def test_wrong_token_rejected():
    client = TestClient(app)
    try:
        with client.websocket_connect("/ws?token=WRONG") as ws:
            ws.receive_text()
        raise AssertionError("wrong token was not rejected")
    except AssertionError:
        raise
    except Exception:
        pass  # any disconnect/close is the expected outcome


def test_hello_handshake_and_reconnect():
    client = TestClient(app)
    with client.websocket_connect("/ws?token=test-secret") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        resp = ws.receive_json()
        assert resp["type"] == "hello"
        assert isinstance(resp["session_id"], str) and len(resp["session_id"]) == 36
        session_id = resp["session_id"]

        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}

    with client.websocket_connect("/ws?token=test-secret") as ws:
        ws.send_json({"type": "hello", "session_id": session_id})
        resp2 = ws.receive_json()
        assert resp2["session_id"] == session_id, "reconnect must resume the same session id"


if __name__ == "__main__":
    test_wrong_token_rejected()
    print("PASS: wrong token rejected before accept")
    test_hello_handshake_and_reconnect()
    print("PASS: hello handshake assigns and resumes session id")
    print("\nALL WEBSOCKET TESTS PASSED")
