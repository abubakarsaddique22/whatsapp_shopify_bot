"""Phase 6, step 1 and 5a: build the speech provider named in the settings.

    stt = get_stt_provider()
    result = await stt.transcribe(wav_path)

STT_PROVIDER decides what you get:
- "fake"  FakeSTT. With STT_FAKE_TEXT set it hears that text on every call (local development);
          without it, it has no answers (tests normally build their own FakeSTT directly).
- "groq"  GroqSTT: Whisper on Groq. Needs STT_API_KEY (the same Groq key as LLM_API_KEY is fine)
          and STT_MODEL (default whisper-large-v3-turbo).
- openai, google, elevenlabs, faster_whisper: not written. The project uses Groq, so they stay
  unwritten unless the bake-off shows Groq is not good enough.
- empty or anything else: ConfigError. A missing setting must never silently turn into a fake.
"""

from app.core.errors import ConfigError
from app.core.settings import Settings, get_settings
from app.integrations.speech.base import SpeechToTextProvider
from app.integrations.speech.fake_stt import FakeSTT
from app.integrations.speech.groq_stt import GroqSTT

NOT_WRITTEN = ("openai", "google", "elevenlabs", "faster_whisper")


def get_stt_provider(settings: Settings | None = None) -> SpeechToTextProvider:
    settings = settings or get_settings()
    provider = settings.stt_provider.strip().lower()

    if provider == "fake":
        return FakeSTT([], default_text=settings.stt_fake_text)

    if provider == "groq":
        key = settings.stt_api_key.get_secret_value()
        if not key:
            raise ConfigError("Set STT_API_KEY in .env (the same Groq key as LLM_API_KEY is fine)")
        if not settings.stt_model:
            raise ConfigError("Set STT_MODEL in .env, for example whisper-large-v3-turbo")
        return GroqSTT(key, settings.stt_model, float(settings.stt_timeout_seconds))

    if provider in NOT_WRITTEN:
        raise ConfigError(f"STT_PROVIDER={provider} is not written (the project uses groq)")

    raise ConfigError(f"STT_PROVIDER must be 'fake' or 'groq', got '{provider}'")
