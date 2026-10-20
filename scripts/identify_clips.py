r"""Which recorded voice note is which sentence of the voice script? (Phase 6 voice pilot)

    uv run python scripts\identify_clips.py "C:\path\to\zain_voice_message"
    uv run python scripts\identify_clips.py "C:\path\to\zain_voice_message" --apply

The client recorded the 32 sentences of evals\voice_manifest.csv (4 groups x 8: urdu, punjabi,
english, mixed) and sent them on WhatsApp. The saved files are named by time ('WhatsApp Audio
... at 6.19.41 AM.aac'), so the order is not known. This script transcribes every file once with
Groq Whisper (STT_API_KEY in .env), guesses the sentence from its words, and prints a table:

    file                         -> slot    transcript
    WhatsApp Audio ... 6.19.41   -> ur-03   کیا یہ گھڑی ابھی اسٹاک میں موجود ہے

CHECK the table yourself (listen to any row marked '?'). With --apply every file is copied into
data\audio\ under its slot name (ur-03.aac, ...), the names voice_manifest.csv expects. The
original files are not changed. Transcripts are kept in <folder>\_transcripts.json, so a run
stopped by Groq's rate limit (HTTP 429) goes on where it stopped: delete that file together
with the clips (blueprint 6.9).
"""

import argparse
import asyncio
import io
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIO_DIR = ROOT / "data" / "audio"
AUDIO_EXT = {".aac", ".ogg", ".opus", ".m4a", ".mp3", ".wav", ".amr"}
CLASSES = {"urdu": "ur", "punjabi": "pa", "english": "en", "mixed": "mx"}

# What each of the 8 questions is about, in every script Whisper may return (Latin, Urdu script,
# Devanagari). Lower case.
TOPICS: dict[int, tuple[str, ...]] = {
    1: ("rolex", "submariner", "رولیکس", "رولکس", "سب میرینر", "سبمیرینر", "price", "پرائس",
        "قیمت", "bhaa", "بھا", "रोलेक्स", "प्राइस"),
    2: ("casio", "کیسیو", "کاسیو", "colour", "color", "کلر", "رنگ", "rang", "कैसियो", "रंग"),
    3: ("stock", "اسٹاک", "سٹاک", "available", "موجود", "स्टॉक", "मौजूद"),
    4: ("delivery", "ڈیلیوری", "ڈلیوری", "ڈیلیوری", "days", "din", "दिन", "डिलीवरी"),
    5: ("return", "refund", "ریفنڈ", "wapas", "wapis", "واپس", "وапس", "वापस", "पसंद"),
    6: ("picture", "پکچر", "تصویر", "tasveer", "तस्वीर", "पिक्चर"),
    7: ("thousand", "hazar", "ہزار", "10000", "دس", "under", "हज़ार", "हजार"),
    8: ("person", "banda", "بندہ", "insaan", "انسان", "support", "سپورٹ", "baat", "بات",
        "gal karni", "گل", "बात", "इंसान"),
}
# Words that only the MIXED sentence of a question has (English words inside an Urdu sentence).
MIXED: dict[int, tuple[str, ...]] = {
    1: ("please", "پلیز"),
    2: ("black", "بلیک", "available", "ایویلیبل"),
    3: ("out of stock", "آؤٹ", "آوٹ"),
    4: ("days", "ڈیز", "ڈےز"),
    5: ("refund", "ریفنڈ"),
    6: ("picture", "پکچر", "send", "سینڈ"),
    7: ("thousand", "ٹین", "تھاؤزنڈ", "تھاوزنڈ", "good", "گڈ"),
    8: ("customer", "support", "کسٹمر", "سپورٹ"),
}
PUNJABI = ("دا", "دی", "وچ", "کہڑے", "کیہڑے", "ملدی", "ہنے", "کنے", "لگدے", "جے", "آوے", "مینوں",
           "مینو", "دیو", "چنگی", "نال", "گل", "بندہ", "da", "vich", "kehre", "mildi", "hune",
           "kinne", "lagde", "aave", "menu", "deo", "changi", "naal", "gal", "banda")
ENGLISH = ("what", "which", "does", "how many", "can i", "please send", "do you", "i would",
           "is this", "the ")
URDU_GRAMMAR = (" ki ", " ka ", " ke ", " hai", " kya", " mein", " karni", " dein", " se ")


@dataclass
class Guess:
    slot: str  # ur-03, or "?" when the words do not decide it
    question: int | None
    clip_class: str | None
    reason: str = ""


def _has(text: str, word: str) -> bool:
    """Whole words only, in every script: 'دی' must not match inside 'دیں'."""
    return f" {word.strip()} " in text


def _words(transcript: str) -> str:
    """Lower case, punctuation (also ؟ ، ۔) turned into spaces, one space at each end."""
    cleaned = re.sub(r"[^\w\s,]", " ", (transcript or "").lower()).replace(",", " ")
    return " " + " ".join(cleaned.split()) + " "


def guess_slot(transcript: str, language: str | None = None) -> Guess:
    """The slot (class + question number) a transcript belongs to. Pure: unit tested."""
    text = _words(transcript)
    scores = {n: sum(_has(text, w) for w in words) for n, words in TOPICS.items()}
    best = max(scores.values())
    tops = [n for n, s in scores.items() if s == best]
    if best == 0 or len(tops) > 1:
        return Guess("?", None, None, f"question unclear {scores}")
    n = tops[0]
    lang = (language or "").lower()
    latin = sum(c.isascii() and c.isalpha() for c in text)
    letters = sum(c.isalpha() for c in text) or 1
    mostly_latin = latin / letters > 0.8
    urdu_grammar = any(w in text for w in URDU_GRAMMAR)
    plain_english = mostly_latin and any(w in text for w in ENGLISH) and not urdu_grammar
    if lang.startswith("pa") or lang == "punjabi" or any(_has(text, w) for w in PUNJABI):
        cls = "punjabi"
    elif any(_has(text, w) for w in MIXED[n]) and not plain_english:
        cls = "mixed"
    elif mostly_latin and not urdu_grammar:
        cls = "english"
    else:
        cls = "urdu"
    return Guess(f"{CLASSES[cls]}-{n:02d}", n, cls)


def assign(guesses: dict[str, Guess]) -> dict[str, Guess]:
    """Two files on one slot: both become '?', so nothing is copied over the wrong name."""
    seen: dict[str, list[str]] = {}
    for name, g in guesses.items():
        if g.slot != "?":
            seen.setdefault(g.slot, []).append(name)
    out = dict(guesses)
    for slot, names in seen.items():
        if len(names) > 1:
            for name in names:
                out[name] = Guess("?", None, None, f"{slot} claimed by {len(names)} files")
    return out


CACHE_NAME = "_transcripts.json"  # next to the clips (git-ignored); delete it with the clips
RETRIES = 5
RATE_LIMIT_WAIT = 30.0  # seconds after an HTTP 429 (Groq free tier: about 20 audio requests/min)


async def _transcribe_all(folder: Path, delay: float) -> dict[str, tuple[str, str | None]]:
    from app.core.errors import TranscriptionError
    from app.core.settings import get_settings
    from app.integrations.speech.groq_stt import GroqSTT
    from app.voice.convert import convert_to_wav

    settings = get_settings()
    key = settings.stt_api_key.get_secret_value()
    if not key:
        raise SystemExit("Set STT_API_KEY in .env (the Groq key is fine)")
    stt = GroqSTT(key, settings.stt_model, float(settings.stt_timeout_seconds))
    cache_path = folder / CACHE_NAME
    found: dict[str, tuple[str, str | None]] = {}
    if cache_path.is_file():  # a run stopped by the rate limit: do not pay for those clips again
        found = {k: (v[0], v[1]) for k, v in json.loads(cache_path.read_text("utf-8")).items()}
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXT)
    for number, path in enumerate(files, start=1):
        if path.name in found:
            continue
        with tempfile.TemporaryDirectory(prefix="wa-identify-", ignore_cleanup_errors=True) as tmp:
            wav = await convert_to_wav(path, Path(tmp), max_seconds=settings.max_audio_seconds)
            for attempt in range(1, RETRIES + 1):
                try:
                    result = await stt.transcribe(wav.path, None)
                    break
                except TranscriptionError as exc:
                    if "429" not in exc.message or attempt == RETRIES:
                        raise
                    print(f"  rate limit, waiting {RATE_LIMIT_WAIT:.0f} s", file=sys.stderr)
                    await asyncio.sleep(RATE_LIMIT_WAIT)
        found[path.name] = (result.text, result.language)
        cache_path.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
        print(f"  transcribed {number}/{len(files)}", file=sys.stderr)
        await asyncio.sleep(delay)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Match recorded voice notes to the script")
    parser.add_argument("folder", help="folder with the WhatsApp audio files")
    parser.add_argument("--apply", action="store_true", help="copy into data\\audio\\<slot>.<ext>")
    parser.add_argument(
        "--delay", type=float, default=3.5, help="seconds between requests (Groq rate limit)"
    )
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    folder = Path(args.folder)
    if not folder.is_dir():
        raise SystemExit(f"Not a folder: {folder}")

    transcripts = asyncio.run(_transcribe_all(folder, args.delay))
    guesses = assign({n: guess_slot(t, lang) for n, (t, lang) in transcripts.items()})
    print(f"\n{'file':<46} {'slot':<6} {'lang':<9} transcript")
    for name in sorted(guesses, key=lambda k: guesses[k].slot):
        g = guesses[name]
        text, lang = transcripts[name]
        print(f"{name:<46} {g.slot:<6} {lang or '-':<9} {text.strip()}")
        if g.reason:
            print(f"{'':<46} {'':<6} {'':<9} ({g.reason})")
    unclear = [n for n, g in guesses.items() if g.slot == "?"]
    missing = sorted(
        f"{c}-{i:02d}" for c in CLASSES.values() for i in range(1, 9)
        if f"{c}-{i:02d}" not in {g.slot for g in guesses.values()}
    )
    print(f"\n{len(guesses) - len(unclear)} of {len(guesses)} matched; unclear: {len(unclear)}")
    if missing:
        print(f"slots without a file: {', '.join(missing)}")

    if args.apply:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        for name, g in guesses.items():
            if g.slot != "?":
                shutil.copy2(folder / name, AUDIO_DIR / f"{g.slot}{Path(name).suffix.lower()}")
        print(f"copied {len(guesses) - len(unclear)} files into {AUDIO_DIR}")
        if unclear:
            print("listen to the unclear files and copy them by hand with the right slot name")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
