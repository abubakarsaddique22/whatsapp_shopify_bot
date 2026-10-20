"""Text and option-name normalisation for catalogue search (Phase 3, step 2).

Customers write 'GA-2100', 'ga2100', 'g shock', 'kala'. The merchant writes 'Colour', 'Color',
'Case size'. This module makes both sides comparable. Pure functions: no database, no Shopify.

Note: normalize_text keeps only a-z and 0-9, so Urdu-script text becomes empty. That is fine in
Phase 3 (Roman Urdu only); the router in Phase 5 turns Urdu script into an English product_query.
"""

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

from app.core.errors import ConfigError
from app.language.lexicon import get_lexicon

ALIASES_PATH = Path(__file__).resolve().parents[3] / "config" / "aliases.yaml"

MIN_CASE_MM = 10  # same limits as RouterOutput.size_mm
MAX_CASE_MM = 80

_APOSTROPHES = re.compile(r"['\u2019`]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MM = re.compile(r"(?<![a-z\d.])(\d{2,3})(?:\.\d+)?\s*mm\b")


# ---- text


def normalize_text(text: str) -> str:
    """'Men's GA-2100!' -> 'mens ga 2100': lowercase, no accents, only a-z 0-9, single spaces."""
    text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return _NON_ALNUM.sub(" ", _APOSTROPHES.sub("", text)).strip()


def compact(text: str) -> str:
    """'GA-2100' and 'ga 2100' both become 'ga2100', so spaced and glued spellings match."""
    return normalize_text(text).replace(" ", "")


# ---- aliases (config/aliases.yaml)


@lru_cache
def load_aliases(path: Path = ALIASES_PATH) -> dict[str, list[str]]:
    """{'g-shock': ['gshock', 'g shock']}: canonical name -> other spellings."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = data.get("aliases") or {}
        return {str(name): [str(v) for v in values] for name, values in raw.items()}
    except (OSError, yaml.YAMLError, AttributeError, TypeError) as exc:
        raise ConfigError(f"Cannot read {path.name}") from exc


def expand_aliases(text: str, path: Path = ALIASES_PATH) -> str:
    """Normalise `text`, then replace every alias with its canonical spelling.

    'gshock ga2100' -> 'g shock ga2100'. Only whole words are replaced.
    """
    result = normalize_text(text)
    pairs = [
        (normalize_text(alias), normalize_text(canonical))
        for canonical, aliases in load_aliases(path).items()
        for alias in aliases
    ]
    for alias, canonical in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        if alias and alias != canonical:
            result = re.sub(rf"(?<!\S){re.escape(alias)}(?!\S)", canonical, result)
    return result


# ---- brand short forms (config/aliases.yaml, section brands)

MAX_BRAND_VARIANTS = 4  # spellings of one query that search tries (the first is the query)


@lru_cache
def load_brand_groups(path: Path = ALIASES_PATH) -> list[tuple[str, ...]]:
    """[('rolex', 'rlx'), ...]: every spelling of one brand, normalised, full name first."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = data.get("brands") or {}
        groups = [
            tuple(normalize_text(str(word)) for word in [name, *(values or [])])
            for name, values in raw.items()
        ]
    except (OSError, yaml.YAMLError, AttributeError, TypeError) as exc:
        raise ConfigError(f"Cannot read {path.name}") from exc
    return [tuple(w for w in group if w) for group in groups if len(group) > 1]


def brand_variants(words: str, path: Path = ALIASES_PATH) -> list[str]:
    """'rolex submariner' -> ['rolex submariner', 'rlx submariner'].

    `words` is already normalised (parse_query). The first item is always `words` itself; the
    others swap a brand name for its short form or the other way round. Whole words only, so
    'apple' never becomes 'audemars piguetple'.
    """
    variants = [words]
    for group in load_brand_groups(path):
        for member in group:
            pattern = rf"(?<!\S){re.escape(member)}(?!\S)"
            if not re.search(pattern, words):
                continue
            for other in group:
                if other == member:
                    continue
                for known in list(variants):
                    swapped = re.sub(pattern, other, known)
                    if swapped not in variants:
                        variants.append(swapped)
            break
    return variants[:MAX_BRAND_VARIANTS]


# ---- option names and colours


def canonical_option_name(name: str) -> str:
    """Merchant option names are free text: 'Colour', 'Color', 'Case size' ... -> our names.

    Returns 'colour', 'size' (case size), 'title' (Shopify's 'Default Title' option) or the
    normalised name itself for anything else ('strap size', 'material').
    """
    normalized = normalize_text(name)
    words = set(normalized.split())
    if normalized == "title":
        return "title"
    if "colour" in normalized or "color" in normalized:
        return "colour"
    if words & {"size", "diameter"} and not words & {"strap", "band", "wrist"}:
        return "size"
    return normalized


def is_default_title(name: str, value: str) -> bool:
    """True for Shopify's placeholder option on products without real variants."""
    return canonical_option_name(name) == "title" and normalize_text(value) == "default title"


def known_colour(word: str) -> str | None:
    """A word from a customer message: 'kala' -> 'black'. None if it is not a colour word."""
    return get_lexicon().colour_for(normalize_text(word))


def colour_of_value(value: str) -> str | None:
    """A merchant option value: 'Matte Black' -> 'black'. Unknown colours keep their own name.

    'Navy' -> 'navy'. 'Black/Blue' -> 'black' (the first known colour wins). Empty -> None.
    """
    normalized = normalize_text(value)
    if not normalized:
        return None
    lexicon = get_lexicon()
    known = lexicon.colour_for(normalized)
    if known:
        return known
    for token in normalized.split():
        known = lexicon.colour_for(token)
        if known:
            return known
    return normalized


def colours_of(options: dict[str, str]) -> list[str]:
    """Colours in a variant's options {'Colour': 'Black', 'Case size': '40mm'} -> ['black']."""
    colours: list[str] = []
    for name, value in options.items():
        if canonical_option_name(name) != "colour":
            continue
        colour = colour_of_value(value)
        if colour:
            colours.append(colour)
    return colours


# ---- sizes


def parse_mm(text: str) -> int | None:
    """'40mm', '44 MM', 'Case 38.5mm' -> 40, 44, 38. None if no plausible case size."""
    match = _MM.search(text.lower())
    if not match:
        return None
    size = int(match.group(1))
    return size if MIN_CASE_MM <= size <= MAX_CASE_MM else None
