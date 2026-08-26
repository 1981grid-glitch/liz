import logging
from typing import AsyncIterator

from anthropic import AsyncAnthropic

from .session import Session

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "This is a real-time spoken conversation, heard aloud through a"
    " Bluetooth headset, not read as text. Keep replies short and"
    " conversational — one or two sentences unless the operator asks for"
    " more detail. Never use markdown, headers, bullet points, or code"
    " blocks; everything you say is spoken aloud as plain sentences."
)


class AnthropicRelay:
    def __init__(self, api_key: str, model: str):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def stream_reply(self, session: Session) -> AsyncIterator[str]:
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=session.history,
        ) as stream:
            async for text in stream.text_stream:
                yield text
