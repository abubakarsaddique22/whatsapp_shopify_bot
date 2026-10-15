"""A pretend speech-to-text provider for tests and local development (Phase 6, step 1).

No network, no key, no real audio needed: the file you pass is not even opened. You decide what
it "hears". Each call to `transcribe()` returns the next prepared answer. A prepared answer is
a TranscriptionResult, a plain string (turned into a result with `fake_result`) or an exception,
which is raised instead (how we test provider failures).

    stt = FakeSTT(["iski price kya hai", TranscriptionError("boom")])
    await stt.transcribe(Path("x.wav"))        # -> text "iski price kya hai"
    stt.calls                                  # -> [(Path("x.wav"), None)]

When the prepared answers run out:
- with `default_text` (local development, STT_FAKE_TEXT in .env) every further call hears that text;
- without it the call raises TranscriptionError, so a test that makes one call too many fails
  loudly instead of getting an invented transcript.
"""

from pathlib import Path

from app.core.errors import TranscriptionError
from app.integrations.speech.base import TranscriptionResult

Answer = TranscriptionResult | str | Exception


def fake_result(text: str, **overrides: object) -> TranscriptionResult:
    """A result with sensible defaults; override any field, for example confidence=0.2."""
    data: dict[str, object] = {
        "text": text,
        "language": None,
        "confidence": 0.95,
        "duration_s": 4.0,
        "provider": "fake",
        "model": "fake",
        "latency_ms": 1,
    }
    data.update(overrides)
    return TranscriptionResult.model_validate(data)


class FakeSTT:
    name = "fake"

    def __init__(self, answers: list[Answer] | None = None, default_text: str = "") -> None:
        self._answers = list(answers or [])
        self._default_text = default_text
        self.calls: list[tuple[Path, str | None]] = []  # (wav_path, language_hint), in order

    async def transcribe(
        self, wav_path: Path, language_hint: str | None = None
    ) -> TranscriptionResult:
        self.calls.append((wav_path, language_hint))
        if self._answers:
            answer = self._answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, str):
                return fake_result(answer)
            return answer
        if self._default_text:
            return fake_result(self._default_text)
        raise TranscriptionError("FakeSTT has no more prepared answers")
