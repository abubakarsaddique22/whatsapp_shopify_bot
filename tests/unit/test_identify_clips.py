"""scripts/identify_clips.py: the guess from a transcript (no audio, no provider)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "identify_clips", ROOT / "scripts" / "identify_clips.py"
)
assert spec is not None and spec.loader is not None
ident = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ident)


@pytest.mark.parametrize(
    ("transcript", "language", "slot"),
    [
        ("What is the price of the Rolex Submariner?", "english", "en-01"),
        ("I would like to speak to a real person.", "english", "en-08"),
        ("رولیکس سب میرینر کی پرائس کیا ہے؟", "urdu", "ur-01"),
        ("رولیکس سب میرینر کی پرائس کیا ہے پلیز", "urdu", "mx-01"),
        ("مجھے اس کی تصویر بھیج دیں", "urdu", "ur-06"),
        ("اس واچ کی پکچر سینڈ کر دیں", "urdu", "mx-06"),
        ("دس ہزار روپے کے اندر کوئی اچھی گھڑی ہے", "urdu", "ur-07"),
        ("Casio watch ka black colour available hai?", "english", "mx-02"),
        ("Delivery kitne days mein aa jati hai?", None, "mx-04"),
        ("مینوں کسے نال گل کرنی اے، کوئی بندہ ہے", "urdu", "pa-08"),
        ("Rolex Submariner da ki bhaa hai?", "punjabi", "pa-01"),
    ],
)
def test_guess_slot(transcript, language, slot):
    assert ident.guess_slot(transcript, language).slot == slot


def test_words_of_no_question_stay_unclear():
    assert ident.guess_slot("hello hello", "english").slot == "?"


def test_two_files_on_one_slot_both_become_unclear():
    a = ident.guess_slot("What is the price of the Rolex Submariner?", "english")
    out = ident.assign({"a.aac": a, "b.aac": a})
    assert out["a.aac"].slot == "?" and out["b.aac"].slot == "?"
