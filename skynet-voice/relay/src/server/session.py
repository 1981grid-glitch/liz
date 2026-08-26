import asyncio
import time
import uuid
from dataclasses import dataclass, field


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Session:
    id: str
    history: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)
    last_active: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def add_user_turn(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})
        self.last_active = time.monotonic()

    def add_assistant_turn(self, text: str) -> None:
        if text.strip():
            self.history.append({"role": "assistant", "content": text})
        self.last_active = time.monotonic()

    def trim_to_budget(self, max_tokens: int) -> None:
        total = sum(_estimate_tokens(m["content"]) for m in self.history)
        while total > max_tokens and len(self.history) > 2:
            dropped = self.history.pop(0)
            total -= _estimate_tokens(dropped["content"])
            if self.history and self.history[0]["role"] == "assistant":
                dropped = self.history.pop(0)
                total -= _estimate_tokens(dropped["content"])


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str | None) -> Session:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        new_id = session_id or str(uuid.uuid4())
        session = Session(id=new_id)
        self._sessions[new_id] = session
        return session
