"""Roman Urdu lexicon loader (config/roman_urdu_lexicon.yaml).

Phase 3 needs only the `colors` and `numbers` parts (colour words and price words).
Phase 5 will reuse the same loader for language detection (`roman_urdu_words`, stopwords).
"""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from app.core.errors import ConfigError

LEXICON_PATH = Path(__file__).resolve().parents[3] / "config" / "roman_urdu_lexicon.yaml"


class Lexicon(BaseModel):
    roman_urdu_words: frozenset[str] = frozenset()
    english_stopwords: frozenset[str] = frozenset()
    colors: dict[str, list[str]] = {}  # canonical colour -> words customers use
    numbers: dict[str, int] = {}  # price words: {"k": 1000, "lakh": 100000, ...}

    def colour_for(self, word: str) -> str | None:
        """'kala' -> 'black'. None if the word is not a known colour word."""
        word = word.strip().lower()
        for canonical, words in self.colors.items():
            if word == canonical or word in words:
                return canonical
        return None


@lru_cache
def get_lexicon(path: Path = LEXICON_PATH) -> Lexicon:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read {path.name}") from exc
    try:
        return Lexicon.model_validate(data)
    except ValidationError as exc:
        # Typical cause: a YAML word like on / no / yes / off is read as a boolean.
        # Fix: put such words in quotes, for example "on".
        raise ConfigError(
            f'{path.name} has an invalid entry (quote words like "on" or "no" in YAML)'
        ) from exc
