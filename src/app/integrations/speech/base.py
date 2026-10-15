"""Phase 6, step 1: what every speech-to-text provider looks like (Blueprint 6.7).

    result = await provider.transcribe(wav_path, language_hint="ur")
    result.text, result.confidence, result.duration_s ...

The pipeline only knows this interface, so OpenAI, Google, ElevenLabs, a local model and the
fake provider used in tests can be swapped by the STT_PROVIDER setting. Providers receive a WAV
file that voice/convert.py already made (16 kHz, mono): they never touch WhatsApp or ffmpeg.

A provider raises TranscriptionError (core/errors.py) when it fails. It never puts the audio,
the file path or a key into the error message.
"""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class TranscriptionResult(BaseModel):
    text: str
    language: str | None = None  # the provider's detected language code, if it gives one
    confidence: float | None = Field(None, ge=0.0, le=1.0)  # 0..1, None if not provided
    duration_s: float = Field(0.0, ge=0.0)  # length of the audio, in seconds
    provider: str
    model: str
    latency_ms: int = Field(0, ge=0)


class SpeechToTextProvider(Protocol):
    name: str

    async def transcribe(
        self, wav_path: Path, language_hint: str | None = None
    ) -> TranscriptionResult: ...
