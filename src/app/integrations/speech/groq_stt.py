"""Phase 6, step 5a: speech to text with Groq's Whisper models.

    stt = GroqSTT(api_key, "whisper-large-v3-turbo")
    result = await stt.transcribe(wav_path, language_hint="ur")

Groq has an OpenAI-compatible endpoint (https://console.groq.com/docs/speech-to-text):
POST /openai/v1/audio/transcriptions with the WAV file, `model`, `response_format=verbose_json`,
`temperature=0` and, if we know it, `language` (en, ur or pa). Models: whisper-large-v3-turbo
(cheaper, faster) and whisper-large-v3 (more accurate). A plain httpx call is enough, no SDK.

Whisper gives no single confidence number. verbose_json gives, for every segment, `avg_logprob`
(closer to 0 is surer) and `no_speech_prob`. Two rules turn that into our TranscriptionResult:
- A segment that Whisper itself would call silence (no_speech_prob > 0.6 and avg_logprob < -1.0,
  the defaults in Whisper's own code) is dropped. This is the "invented text on silence" case.
- confidence = exp(average avg_logprob of the kept segments, weighted by their length): the
  typical probability of one word. It is an APPROXIMATION, not a true probability. That is why
  STT_MIN_CONFIDENCE stays 0.0 (off) until the bake-off shows which value separates good
  transcripts from bad ones.

`language` is what Whisper reports: usually the full name ("urdu"), which voice/gate.py reads.
The audio is sent to Groq (tell the client, blueprint 6.9). Errors never contain the key, the
file path or the audio.
"""

import math
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.errors import TranscriptionError
from app.integrations.speech.base import TranscriptionResult

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
SILENT_NO_SPEECH_PROB = 0.6  # Whisper's own defaults for "this segment is silence"
SILENT_AVG_LOGPROB = -1.0


def _make_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _looks_silent(segment: dict[str, Any]) -> bool:
    no_speech = _num(segment.get("no_speech_prob"))
    logprob = _num(segment.get("avg_logprob"))
    if no_speech is None or logprob is None:
        return False
    return no_speech > SILENT_NO_SPEECH_PROB and logprob < SILENT_AVG_LOGPROB


def _confidence(segments: list[dict[str, Any]]) -> float | None:
    """exp(length-weighted mean of avg_logprob), kept between 0 and 1. None if unknown."""
    total = 0.0
    weighted = 0.0
    for segment in segments:
        logprob = _num(segment.get("avg_logprob"))
        if logprob is None:
            continue
        length = (_num(segment.get("end")) or 0.0) - (_num(segment.get("start")) or 0.0)
        length = max(length, 0.01)
        total += length
        weighted += logprob * length
    if total == 0.0:
        return None
    return min(1.0, max(0.0, math.exp(weighted / total)))


def parse_response(payload: dict[str, Any], *, model: str, latency_ms: int) -> TranscriptionResult:
    """Groq's verbose_json answer -> TranscriptionResult (see the rules in the module text)."""
    raw_segments = payload.get("segments") or []
    segments = [s for s in raw_segments if isinstance(s, dict)]
    if segments:
        kept = [s for s in segments if not _looks_silent(s)]
        text = " ".join(str(s.get("text") or "").strip() for s in kept).strip()
        confidence = _confidence(kept)
    else:
        text = str(payload.get("text") or "").strip()
        confidence = None

    language = payload.get("language")
    return TranscriptionResult(
        text=text,
        language=language if isinstance(language, str) and language else None,
        confidence=confidence,
        duration_s=max(0.0, _num(payload.get("duration")) or 0.0),
        provider="groq",
        model=model,
        latency_ms=latency_ms,
    )


class GroqSTT:
    name = "groq"

    def __init__(self, api_key: str, model: str, timeout: float = 30.0, prompt: str = "") -> None:
        self._api_key = api_key
        self.model = model
        self._timeout = timeout
        self._prompt = prompt.strip()

    async def transcribe(
        self, wav_path: Path, language_hint: str | None = None
    ) -> TranscriptionResult:
        form = {"model": self.model, "response_format": "verbose_json", "temperature": "0"}
        if language_hint:
            form["language"] = language_hint
        if self._prompt:
            form["prompt"] = self._prompt  # spelling hint for Whisper (script, product names)
        audio = wav_path.read_bytes()  # at most a few MB: the worker caps the length

        started = time.perf_counter()
        try:
            async with _make_client(self._timeout) as http:
                response = await http.post(
                    URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    data=form,
                    files={"file": ("audio.wav", audio, "audio/wav")},
                )
        except httpx.HTTPError:
            raise TranscriptionError("Groq speech request failed (network or timeout)") from None
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            raise TranscriptionError(
                f"Groq speech request refused (HTTP {response.status_code})",
                detail={"status": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError:
            raise TranscriptionError("Groq speech answer was not JSON") from None
        if not isinstance(payload, dict):
            raise TranscriptionError("Groq speech answer had an unexpected shape")
        return parse_response(payload, model=self.model, latency_ms=latency_ms)
