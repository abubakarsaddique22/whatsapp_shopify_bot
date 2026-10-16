"""Phase 6, step 5b: the speech bake-off (Blueprint 6.6).

Runs every clip in the manifest through every candidate model, with the SAME ffmpeg conversion
and the SAME confidence gate the bot uses, and prints a table (WER, CER, script, latency, gate,
intent). The arithmetic lives in app/voice/scoring.py (tested); this file only does the work
that needs audio and a provider.

    uv run python scripts\\bakeoff.py --limit 3
    uv run python scripts\\bakeoff.py --providers groq:whisper-large-v3,groq:whisper-large-v3-turbo
    uv run python scripts\\bakeoff.py --router            (also scores intent; judged by Ollama)
    uv run python scripts\\bakeoff.py --class punjabi

Folders:
    evals\\voice_manifest.csv   one row per clip: file,class,reference,intent,noisy
                               (class: urdu | punjabi | english | mixed; reference = the exact
                               transcript written by a native speaker, in the SAME script the
                               provider returns; intent = one of the Intent values, optional)
    data\\audio\\               the audio files (git-ignored). Use neutral names such as ur-001.ogg
    data\\audio\\bakeoff\\      results (git-ignored): they contain real transcripts

Needs: ffmpeg on PATH, STT_API_KEY in .env (the Groq key is fine). Only Groq is written now.
Privacy (blueprint 6.9): each clip is converted inside a temporary folder that is deleted when
the clip is done. The audio goes to the provider you list in --providers, nowhere else. Get
permission for every clip, and delete the clips when the bake-off is finished.
"""

import argparse
import asyncio
import csv
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from app.agent.router import route_intent
from app.core.errors import AppError, AudioConversionError, ConfigError, TranscriptionError
from app.core.settings import Settings, get_settings
from app.integrations.llm.base import ChatModel
from app.integrations.llm.chat_factory import get_eval_chat_model
from app.integrations.speech.base import SpeechToTextProvider
from app.integrations.speech.groq_stt import GroqSTT
from app.voice.convert import convert_to_wav
from app.voice.gate import check_transcript
from app.voice.scoring import ClipResult, build_result, format_report, summarize

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "evals" / "voice_manifest.csv"
DEFAULT_AUDIO_DIR = ROOT / "data" / "audio"
DEFAULT_PROVIDERS = "groq:whisper-large-v3,groq:whisper-large-v3-turbo"
REQUIRED_COLUMNS = {"file", "class", "reference"}

Provider = tuple[str, SpeechToTextProvider]


class ManifestRow(BaseModel):
    file: str
    clip_class: str
    reference: str
    intent: str | None = None
    noisy: str | None = None


def load_manifest(path: Path) -> list[ManifestRow]:
    if not path.is_file():
        raise ConfigError(f"Manifest not found: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ConfigError(f"The manifest needs the columns {sorted(REQUIRED_COLUMNS)}")
        rows = []
        for line in reader:
            name = (line.get("file") or "").strip()
            if not name:
                continue
            rows.append(
                ManifestRow(
                    file=name,
                    clip_class=(line.get("class") or "").strip().lower(),
                    reference=(line.get("reference") or "").strip(),
                    intent=(line.get("intent") or "").strip() or None,
                    noisy=(line.get("noisy") or "").strip() or None,
                )
            )
    return rows


def build_providers(specs: str, settings: Settings) -> list[Provider]:
    """'groq:whisper-large-v3,groq:whisper-large-v3-turbo' -> [(label, provider), ...]"""
    providers: list[Provider] = []
    for spec in (s.strip() for s in specs.split(",") if s.strip()):
        name, _, model = spec.partition(":")
        if name.lower() != "groq":
            raise ConfigError(f"Bake-off provider '{name}' is not written yet (only groq)")
        key = settings.stt_api_key.get_secret_value()
        if not key:
            raise ConfigError("Set STT_API_KEY in .env (the Groq key is fine)")
        model = model or settings.stt_model
        providers.append(
            (f"groq:{model}", GroqSTT(key, model, float(settings.stt_timeout_seconds)))
        )
    if not providers:
        raise ConfigError("--providers is empty")
    return providers


def _failed(row: ManifestRow, label: str, error: str) -> ClipResult:
    return build_result(
        provider=label,
        file=row.file,
        clip_class=row.clip_class,
        reference=row.reference,
        error=error,
        intent_expected=row.intent,
    )


async def _one(
    row: ManifestRow,
    label: str,
    stt: SpeechToTextProvider,
    wav: Path,
    duration_s: float,
    settings: Settings,
    llm: ChatModel | None,
) -> ClipResult:
    try:
        result = await stt.transcribe(wav, None)  # no language hint, like the real bot
    except TranscriptionError as exc:
        return _failed(row, label, exc.message)

    decision = check_transcript(
        result, min_words=settings.stt_min_words, min_confidence=settings.stt_min_confidence
    )
    intent_got: str | None = None
    if llm is not None:
        if result.text.strip():
            try:
                intent_got = (await route_intent(result.text, llm)).intent.value
            except AppError:
                intent_got = None
        else:
            intent_got = "unclear"  # nothing heard: the bot could not have answered

    return build_result(
        provider=label,
        file=row.file,
        clip_class=row.clip_class,
        reference=row.reference,
        duration_s=duration_s,
        hypothesis=result.text,
        language=result.language,
        confidence=result.confidence,
        latency_ms=result.latency_ms,
        gate_reason=decision.reason.value if decision.reason else None,
        intent_expected=row.intent,
        intent_got=intent_got,
    )


async def run_clip(
    row: ManifestRow,
    audio_dir: Path,
    providers: list[Provider],
    settings: Settings,
    llm: ChatModel | None,
    delay: float,
) -> list[ClipResult]:
    source = audio_dir / row.file
    if not source.is_file():
        return [_failed(row, label, "audio file not found") for label, _ in providers]

    with tempfile.TemporaryDirectory(prefix="wa-bakeoff-", ignore_cleanup_errors=True) as name:
        try:
            wav = await convert_to_wav(source, Path(name), max_seconds=settings.max_audio_seconds)
        except AudioConversionError as exc:
            return [_failed(row, label, exc.message) for label, _ in providers]

        results = []
        for label, stt in providers:
            results.append(await _one(row, label, stt, wav.path, wav.duration_s, settings, llm))
            await asyncio.sleep(delay)  # be kind to the provider's rate limit
        return results


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    rows = load_manifest(Path(args.manifest))
    if args.clip_class:
        rows = [r for r in rows if r.clip_class == args.clip_class.lower()]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print(
            "No clips to run. Add rows to evals\\voice_manifest.csv (and the files to data\\audio)."
        )
        return 1

    providers = build_providers(args.providers, settings)
    llm = get_eval_chat_model(settings) if args.router else None  # eval -> Ollama

    results: list[ClipResult] = []
    for index, row in enumerate(rows, start=1):
        print(f"[{index}/{len(rows)}] {row.file}", flush=True)
        results.extend(
            await run_clip(row, Path(args.audio_dir), providers, settings, llm, args.delay)
        )

    report = format_report(summarize(results))
    print()
    print(report)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results_path = out_dir / f"results-{stamp}.csv"
    with results_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ClipResult.model_fields))
        writer.writeheader()
        for result in results:
            writer.writerow(result.model_dump())
    summary_path = out_dir / f"summary-{stamp}.txt"
    summary_path.write_text(report + "\n", encoding="utf-8")
    print(f"\nPer-clip results: {results_path}\nSummary:          {summary_path}")
    print("These files hold real transcripts: they stay out of Git (data\\audio\\ is ignored).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Speech-to-text bake-off (blueprint 6.6)")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR))
    parser.add_argument("--out", default=str(DEFAULT_AUDIO_DIR / "bakeoff"))
    parser.add_argument("--providers", default=DEFAULT_PROVIDERS)
    parser.add_argument("--class", dest="clip_class", help="only this class, e.g. punjabi")
    parser.add_argument("--limit", type=int, help="only the first N clips (a quick try)")
    parser.add_argument("--router", action="store_true", help="also score intent (needs LLM_*)")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between provider calls")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles and Urdu text
    try:
        return asyncio.run(run(args))
    except ConfigError as exc:
        print(f"Config problem: {exc.message}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
