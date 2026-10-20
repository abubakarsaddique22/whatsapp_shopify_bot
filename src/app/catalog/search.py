"""Product search over the catalogue mirror (Phase 3, step 5; Blueprint 4.9).

Pipeline: normalise the words (aliases, 'ga2100' = 'GA-2100') -> rank visible products with
pg_trgm over title, brand, product type and tags -> keep products that have at least ONE variant
satisfying colour, case size and price together -> decide: one clear match, several close
matches (ask the customer), or none.

Only the mirror is used here, so no price is returned: the price a customer sees always comes
from a live Shopify call (catalog/stock.py -> fetch_live_variant). The mirror price is used only
to apply a max_price filter.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.normalizer import (
    canonical_option_name,
    colour_of_value,
    colours_of,
    expand_aliases,
    known_colour,
    normalize_text,
    parse_mm,
)
from app.db.tables import VariantMirror

MAX_RESULTS = 5  # Blueprint 4.9: at most 5 candidates
CANDIDATE_POOL = 20  # rows ranked in SQL before the variant filters run
MIN_SCORE = 0.35  # below this a product is not a match at all
STRONG_SCORE = 0.5  # a single match must reach this to be treated as 'the' product
LEAD_MARGIN = 0.10  # the best match must lead the second by this much to win outright
MIN_COMPACT_LEN = 3  # 'ga2100' may match glued titles; 'g' alone may not

# The haystack is title + brand + product type + tags, lowercased and reduced to a-z 0-9.
# Score = the better of: trigram word similarity, or 1.0 when the glued query ('ga2100') appears
# inside the glued haystack ('casiogshockga2100'). Only visible products are ever ranked.
_RANK_SQL = text(
    """
    WITH p AS (
        SELECT id, title, url, vendor, tags,
               trim(regexp_replace(
                   lower(concat_ws(' ', title, vendor, product_type, array_to_string(tags, ' '))),
                   '[^a-z0-9]+', ' ', 'g')) AS hay
        FROM products_mirror
        WHERE is_visible
    )
    SELECT id, title, url, vendor, tags,
           GREATEST(
               word_similarity(CAST(:q AS text), hay),
               CASE WHEN CAST(:qc AS text) <> ''
                         AND strpos(replace(hay, ' ', ''), CAST(:qc AS text)) > 0
                    THEN CAST(1.0 AS real) ELSE CAST(0.0 AS real) END
           ) AS score
    FROM p
    ORDER BY score DESC, title
    LIMIT :pool
    """
)


# A customer who types ONE product's full title (copied from the shop) gets that product, even
# when words of the title are colours ('Silver Gold Black Textured') that parse_query would take
# out. Only when no OTHER visible title contains these words: 'zorbex tide' stays ambiguous
# while 'Zorbex Tide Ultra' exists (T13).
_EXACT_SQL = text(
    """
    WITH p AS (
        SELECT id, title, url, vendor, tags,
               trim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) AS t
        FROM products_mirror
        WHERE is_visible
    )
    SELECT id, title, url, vendor, tags, CAST(1.0 AS real) AS score
    FROM p
    WHERE t = CAST(:t AS text)
      AND (SELECT count(*) FROM p AS other
           WHERE strpos(' ' || other.t || ' ', ' ' || CAST(:t AS text) || ' ') > 0) = 1
    """
)


# The customer's whole MESSAGE contains ONE product's full title ('How much is the PP - Nautilus
# Silver Brown - AAA Quality?'). Same safety rule as _EXACT_SQL: no OTHER visible title may contain
# these words, so 'zorbex tide' stays ambiguous while 'Zorbex Tide Ultra' exists (T13). The longest
# title wins when a message holds more than one.
_NAMED_SQL = text(
    """
    WITH p AS (
        SELECT title,
               trim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) AS t
        FROM products_mirror
        WHERE is_visible
    )
    SELECT title, t AS norm
    FROM p
    WHERE t <> ''
      AND strpos(CAST(:msg AS text), ' ' || t || ' ') > 0
      AND (SELECT count(*) FROM p AS other
           WHERE strpos(' ' || other.t || ' ', ' ' || p.t || ' ') > 0) = 1
    ORDER BY length(t) DESC
    """
)

# A title written with a small typo ('RLX - Submairner Rubber - Green Hulk - AAA Quality'): the
# exact match above misses it, and the router then 'corrects' RLX to Rolex and drops 'Rubber Green
# Hulk', so the wrong product was priced (eval nat-typo-price-15, 2026-10-20). pg_trgm
# word_similarity(title, message) = how much of the title is in the message. Same safety rule as
# above: a title that is part of another title never counts ('zorbex tide', T13).
_FUZZY_NAMED_SQL = text(
    """
    WITH p AS (
        SELECT title,
               trim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) AS t
        FROM products_mirror
        WHERE is_visible
    )
    SELECT title, t AS norm, word_similarity(t, CAST(:msg AS text)) AS score
    FROM p
    WHERE t <> ''
      AND (SELECT count(*) FROM p AS other
           WHERE strpos(' ' || other.t || ' ', ' ' || p.t || ' ') > 0) = 1
    ORDER BY score DESC, length(t) DESC
    LIMIT 5
    """
)

MIN_NAMED_WORDS = 2  # a one-word title ('Watch') would be 'found' in almost any message
MIN_NAMED_CHARS = 8
# Measured 2026-10-20 on the real titles: one typo in a full title scores 0.83, the nearest other
# title 0.52; 'zorbex tide' against 'Zorbex Tide Ultra' 0.67; 'rlx branded premium' 0.87 against
# 0.73 for 'AP Branded Premium Box' (stays a question: the lead is too small).
FUZZY_MIN_SCORE = 0.8
FUZZY_LEAD = 0.15  # the best title must lead the next one by this much


async def title_named_in_text(session: AsyncSession | None, message: str) -> str | None:
    """The title of the ONE visible product whose full title is written in `message`, or None.

    Needs no router and no ranking, so a title with colour words ('Silver Brown') or a crowd of
    similar titles (many 'PP - Nautilus ...') cannot hide it. No session (unit tests that use
    fake tools): nothing to look up.
    """
    if session is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", message.lower()).strip()
    if not normalized:
        return None
    rows = (await session.execute(_NAMED_SQL, {"msg": f" {normalized} "})).all()
    rows = [r for r in rows if _long_enough(r.norm)]
    if rows:
        longest = [r for r in rows if len(r.norm) == len(rows[0].norm)]
        return str(longest[0].title) if len(longest) == 1 else None
    return await _title_named_with_a_typo(session, normalized)


def _long_enough(norm: str) -> bool:
    return len(norm.split()) >= MIN_NAMED_WORDS and len(norm) >= MIN_NAMED_CHARS


async def _title_named_with_a_typo(session: AsyncSession, normalized: str) -> str | None:
    """Almost the whole title is in the message, and clearly more than any other title."""
    rows = (await session.execute(_FUZZY_NAMED_SQL, {"msg": f" {normalized} "})).all()
    rows = [r for r in rows if _long_enough(r.norm)]
    if not rows or rows[0].score < FUZZY_MIN_SCORE:
        return None
    if len(rows) > 1 and rows[0].score - rows[1].score < FUZZY_LEAD:
        return None
    return str(rows[0].title)


class MatchKind(StrEnum):
    NONE = "none"  # nothing matched (T14): say so honestly
    ONE = "one"  # one clear product: go on
    AMBIGUOUS = "ambiguous"  # several close matches (T13): ask one clarifying question


@dataclass(frozen=True)
class VariantRow:
    """The few variant facts search needs (built from the mirror, easy to build in tests)."""

    id: str
    title: str
    options: dict[str, str]
    price: Decimal


@dataclass(frozen=True)
class Candidate:
    product_id: str
    title: str
    url: str
    vendor: str
    score: float  # 0..1
    variant_ids: list[str]  # variants that satisfy every filter (all of them if no filter)
    colours: list[str]  # every colour the product comes in, canonical ('black')
    # Only set when a colour was asked for. True: the colour is confirmed (a variant option, or
    # the name/tags say it). False: nothing in the data says which colour it is, so the bot must
    # not claim it. None: no colour filter.
    colour_verified: bool | None = None


@dataclass(frozen=True)
class SearchResult:
    kind: MatchKind
    candidates: list[Candidate]
    name_matches: int  # products matching the words, BEFORE colour, size and price filters
    colour: str | None  # the colour filter that was applied (also when found in the query text)


# ---- pure helpers (no database)


def parse_query(query: str) -> tuple[str, str | None]:
    """'gshock GA-2100 kala' -> ('g shock ga 2100', 'black').

    Applies the alias list, then moves colour words out of the product words: the colour is
    checked against the variants, not against the title.
    """
    words: list[str] = []
    colour: str | None = None
    for word in expand_aliases(query).split():
        found = known_colour(word)
        if found:
            colour = colour or found
        else:
            words.append(word)
    return " ".join(words), colour


def variant_size_mm(variant: VariantRow, product_title: str = "") -> int | None:
    """Case size of a variant: its size option first, then its title, then the product title."""
    for name, value in variant.options.items():
        if canonical_option_name(name) == "size":
            size = parse_mm(value)
            if size:
                return size
    return parse_mm(variant.title) or parse_mm(product_title)


def variant_matches(
    variant: VariantRow,
    *,
    colour: str | None,
    size_mm: int | None,
    max_price: int | None,
    product_title: str = "",
) -> bool:
    """True if this ONE variant satisfies every filter that is set."""
    if colour and colour not in colours_of(variant.options):
        return False
    if size_mm is not None and variant_size_mm(variant, product_title) != size_mm:
        return False
    return not (max_price is not None and variant.price > Decimal(max_price))


class ColourFit(StrEnum):
    CONFIRMED = "confirmed"  # the name or tags say this colour
    UNVERIFIED = "unverified"  # nothing says which colour: do not claim it, do not hide it
    EXCLUDED = "excluded"  # the name or tags name only other colours


def colours_in_text(value: str) -> set[str]:
    """Colours named in a title or tags: 'Casio Black Resin' -> {'black'}."""
    found: set[str] = set()
    for word in normalize_text(value).split():
        colour = known_colour(word)
        if colour:
            found.add(colour)
    return found


def name_colour_fit(wanted: str, name_text: str) -> ColourFit:
    """For a product whose variants have NO colour option: what do its name and tags say?"""
    named = colours_in_text(name_text)
    if wanted in named:
        return ColourFit.CONFIRMED
    return ColourFit.EXCLUDED if named else ColourFit.UNVERIFIED


def classify(candidates: list[Candidate]) -> MatchKind:
    """One clear product, several close ones, or none. `candidates` is sorted best first."""
    if not candidates:
        return MatchKind.NONE
    best = candidates[0]
    if best.score < STRONG_SCORE:
        return MatchKind.AMBIGUOUS  # a weak guess: confirm with the customer
    if len(candidates) == 1 or best.score - candidates[1].score >= LEAD_MARGIN:
        return MatchKind.ONE
    return MatchKind.AMBIGUOUS


# ---- search


async def _exact_title_rows(session: AsyncSession, query: str) -> list[Any]:
    """The one visible product whose whole title is the query, or [] (see _EXACT_SQL)."""
    normalized = re.sub(r"[^a-z0-9]+", " ", query.lower()).strip()
    if not normalized:
        return []
    return list((await session.execute(_EXACT_SQL, {"t": normalized})).all())


async def search_products(
    session: AsyncSession,
    query: str,
    *,
    colour: str | None = None,
    size_mm: int | None = None,
    max_price: int | None = None,
    limit: int = MAX_RESULTS,
) -> SearchResult:
    """Find visible products for a customer's words. Never returns hidden products."""
    limit = max(1, min(limit, MAX_RESULTS))
    named: list[Any] = await _exact_title_rows(session, query)
    if named:
        # The whole title was typed: colour words in it belong to the title, not to a filter.
        wanted = colour_of_value(colour) if colour else None
    else:
        words, colour_in_query = parse_query(query)
        wanted = colour_of_value(colour) if colour else colour_in_query
        if not words:
            return SearchResult(MatchKind.NONE, [], 0, wanted)

        glued = words.replace(" ", "")
        params = {
            "q": words,
            "qc": glued if len(glued) >= MIN_COMPACT_LEN else "",
            "pool": CANDIDATE_POOL,
        }
        rows = (await session.execute(_RANK_SQL, params)).all()
        named = [row for row in rows if row.score >= MIN_SCORE]
        if not named:
            return SearchResult(MatchKind.NONE, [], 0, wanted)

    variant_rows = await session.execute(
        select(
            VariantMirror.id,
            VariantMirror.product_id,
            VariantMirror.title,
            VariantMirror.options,
            VariantMirror.price_amount,
        )
        .where(VariantMirror.product_id.in_([row.id for row in named]))
        .order_by(VariantMirror.title)
    )
    variants: dict[str, list[VariantRow]] = {}
    for vid, product_id, title, options, price in variant_rows.all():
        variants.setdefault(product_id, []).append(VariantRow(vid, title, options or {}, price))

    filtering = wanted is not None or size_mm is not None or max_price is not None
    candidates: list[Candidate] = []
    for row in named:
        own = variants.get(row.id, [])
        colours = sorted({c for v in own for c in colours_of(v.options)})
        variant_colour = wanted
        verified: bool | None = None
        if wanted:
            if colours:  # the merchant lists colours as an option: trust only that
                verified = True
            else:  # no colour option (single-variant product): read the name and tags
                fit = name_colour_fit(wanted, f"{row.title} {' '.join(row.tags or [])}")
                if fit == ColourFit.EXCLUDED:
                    continue
                verified = fit == ColourFit.CONFIRMED
                variant_colour = None  # nothing to check on the variants; size and price still do
        matching = [
            v.id
            for v in own
            if variant_matches(
                v,
                colour=variant_colour,
                size_mm=size_mm,
                max_price=max_price,
                product_title=row.title,
            )
        ]
        if filtering and not matching:
            continue
        candidates.append(
            Candidate(
                product_id=row.id,
                title=row.title,
                url=row.url,
                vendor=row.vendor,
                score=round(float(row.score), 3),
                variant_ids=matching if filtering else [v.id for v in own],
                colours=colours,
                colour_verified=verified,
            )
        )
    candidates.sort(key=lambda c: c.colour_verified is False)  # confirmed first, then by score
    candidates = candidates[:limit]
    return SearchResult(classify(candidates), candidates, len(named), wanted)
