from typing import Literal, Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str
    relay_model: str = "claude-sonnet-4-6"

    relay_host: str = "127.0.0.1"
    relay_port: int = 8765
    relay_shared_secret: str

    stt_wyoming_host: str = "127.0.0.1"
    stt_wyoming_port: int = 10300

    tts_backend: Literal["kokoro", "azure"] = "kokoro"

    kokoro_url: str = "http://127.0.0.1:8880"
    kokoro_voice: str = "af_bella"

    azure_tts_key: Optional[str] = None
    azure_tts_region: str = "eastus2"
    azure_tts_voice: str = "en-US-AndrewNeural"

    session_max_tokens: int = 8000
    audio_sample_rate: int = 16000

    class Config:
        env_file = ".env"


settings = Settings()
