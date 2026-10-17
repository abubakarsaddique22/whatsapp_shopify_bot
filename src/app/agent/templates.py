"""Phase 9, step 4: templated replies (no model call). Blueprint 8.5, 8.6, 8.8.

    render("ask_which_product", "roman_ur", names="GA-2100 or GA-B2100")

The texts live in prompts/clarify_templates.yaml and prompts/refusal_templates.yaml, so a wording
change is a reviewed text change, not a code change. Every kind has en, roman_ur and en_roman_ur.
"""

from functools import lru_cache
from pathlib import Path

import yaml

from app.language.policy import ReplyLang

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
FILES = ("clarify_templates.yaml", "refusal_templates.yaml")

# A ReplyLang without its own text uses the closest one that has text.
FALLBACK: dict[str, str] = {"mixed": "roman_ur", "ur": "roman_ur"}


@lru_cache(maxsize=1)
def load_templates() -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for name in FILES:
        data = yaml.safe_load((PROMPTS_DIR / name).read_text(encoding="utf-8")) or {}
        for key, per_lang in data.items():
            if key in merged:
                raise ValueError(f"duplicate template key: {key}")
            merged[key] = per_lang
    return merged


def render(kind: str, lang: ReplyLang, **values: str) -> str:
    per_lang = load_templates()[kind]  # KeyError on a typo: the unit test catches it
    text = per_lang.get(FALLBACK.get(lang, lang)) or per_lang["en"]
    return text.format(**values) if values else text
