"""Phase 6, step 4a: one voice note in, one transcript (or a reason to ask again) out.

    outcome = await transcribe_voice_note(
        media_id, stt, max_bytes=..., max_seconds=..., min_words=..., min_confidence=...
    )
    if outcome.ok:
        text = outcome.transcript.text          # goes on like a typed message (source = voice)
    else:
        outcome.reason                          # GateReason: ask the customer to repeat

Order of work (blueprint 6.4, 6.8, 6.9):
1. prepared_audio: download + ffmpeg to WAV. The audio exists only inside this block.
2. check_audio_limits: too long or too big is refused BEFORE the speech provider is called.
3. stt.transcribe (once more with STT_RETRY_LANGUAGE when Whisper names a language we do not
   serve, see transcribe_with_retry).
4. The audio is already deleted here. check_transcript decides: go on, or ask again.

What happens to errors:
- too big (MediaTooLarge), too long, or a damaged file (AudioConversionError): the customer is
  asked to send again. Nothing is raised.
- UnsupportedMedia (not audio), TranscriptionError and WhatsAppTemporaryError are raised: the
  worker decides (a polite "unsupported" reply, or a retry of the message).

A doubtful transcript is dropped on purpose: the outcome has no text, so nothing can answer it.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AudioConversionError, MediaTooLarge
from app.integrations.speech.base import SpeechToTextProvider, TranscriptionResult
from app.voice.convert import PreparedAudio, prepared_audio
from app.voice.gate import (
    ALLOWED_LANGUAGES,
    GateReason,
    check_audio_limits,
    check_transcript,
    normalize_language,
    words_of,
)

log = logging.getLogger(__name__)

Prepare = Callable[..., AbstractAsyncContextManager[PreparedAudio]]


@asynccontextmanager
async def dev_prepared_audio(
    media_id: str, *, max_bytes: int, max_seconds: int
) -> AsyncIterator[PreparedAudio]:
    """LOCAL DEVELOPMENT ONLY (the worker chooses it, see worker.voice_outcome).

    /dev/simulate has no real WhatsApp voice note to download. This stands in for
    prepared_audio: no download, no ffmpeg, no file. FakeSTT never opens the path, and
    STT_FAKE_TEXT decides what the "voice note" says. The 4 second length is a normal clip, so
    the hallucination speed check does not fire on a fake text of up to about 25 words.
    """
    yield PreparedAudio(
        wav_path=Path("dev-voice-note.wav"), duration_s=4.0, size_bytes=1, mime_type="audio/ogg"
    )


@dataclass(frozen=True)
class VoiceOutcome:
    transcript: TranscriptionResult | None = None  # set when the transcript can be used
    reason: GateReason | None = None  # set when the customer must be asked to repeat

    @property
    def ok(self) -> bool:
        return self.transcript is not None


async def transcribe_with_retry(
    stt: SpeechToTextProvider,
    wav_path: Path,
    *,
    language_hint: str | None = None,
    allowed_languages: frozenset[str] = ALLOWED_LANGUAGES,
    retry_language: str | None = None,
) -> TranscriptionResult:
    """Transcribe; if Whisper names a language we do not serve, transcribe ONCE more with
    `retry_language` (for example 'ur').

    Found in the voice pilot (2026-10-20, 32 clips of Pakistani speakers, no language hint):
    Whisper's own language guess was wrong now and then (Punjabi heard as 'Tagalog', English with
    a local accent as 'Bengali'), and such text is useless to the router. Telling Whisper the
    language on the second try gives readable Urdu-script text. A first try with a hint, or a
    language we serve, is never repeated, so this costs one extra call only when it is needed.
    """
    result = await stt.transcribe(wav_path, language_hint)
    if language_hint or not retry_language:
        return result
    language = normalize_language(result.language)
    if language is None or language in allowed_languages:
        return result
    log.info("voice_language_retry detected=%s retry=%s", result.language, retry_language)
    return await stt.transcribe(wav_path, retry_language)


async def transcribe_voice_note(
    media_id: str,
    stt: SpeechToTextProvider,
    *,
    max_bytes: int,
    max_seconds: int,
    min_words: int,
    min_confidence: float,
    language_hint: str | None = None,
    allowed_languages: frozenset[str] = ALLOWED_LANGUAGES,
    retry_language: str | None = None,
    prepare: Prepare = prepared_audio,  # tests pass a fake, so no network and no ffmpeg
) -> VoiceOutcome:
    duration_s = 0.0
    try:
        async with prepare(media_id, max_bytes=max_bytes, max_seconds=max_seconds) as audio:
            limits = check_audio_limits(
                audio.duration_s, audio.size_bytes, max_seconds=max_seconds, max_bytes=max_bytes
            )
            if not limits.ok:
                return VoiceOutcome(reason=limits.reason)
            duration_s = audio.duration_s
            result = await transcribe_with_retry(
                stt,
                audio.wav_path,
                language_hint=language_hint,
                allowed_languages=allowed_languages,
                retry_language=retry_language,
            )
    except MediaTooLarge:
        return VoiceOutcome(reason=GateReason.TOO_LARGE)
    except AudioConversionError:
        return VoiceOutcome(reason=GateReason.EMPTY)  # nothing could be heard in this file

    if result.duration_s <= 0:
        # many providers do not report the length; the WAV length we measured is the truth
        result = result.model_copy(update={"duration_s": duration_s})

    decision = check_transcript(
        result,
        min_words=min_words,
        min_confidence=min_confidence,
        allowed_languages=allowed_languages,
    )
    if not decision.ok:
        # why was it dropped? No transcript text here (privacy): only numbers and the language.
        log.info(
            "voice_gate_stopped reason=%s language=%s words=%d duration_s=%.1f confidence=%s",
            decision.reason,
            result.language,
            len(words_of(result.text)),
            result.duration_s,
            result.confidence,
        )
        return VoiceOutcome(reason=decision.reason)
    return VoiceOutcome(transcript=result)
