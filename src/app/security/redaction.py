"""Phase 11: remove card-like numbers from text BEFORE it is stored or logged.

    redact_cards("my card is 4111 1111 1111 1111")   # "my card is [CARD]"
    CARD_PLACEHOLDER in text                         # True: the customer sent card details

The bot never needs payment data (Blueprint 8.8, A10). repositories.add_inbox and add_message
call this, so a card number never reaches the database, the model or the logs. The placeholder
is also the signal for the worker: when it is in the text, the customer is asked not to share
card details here.

What counts as a card number (narrow on purpose, so phone numbers and order numbers stay):
  - four groups of four digits (4111 1111 1111 1111 or 4111-1111-1111-1111): always;
  - an American Express style 4-6-5 grouping, or 13 to 19 digits written together:
    only when the Luhn check passes (every real card number passes it).
Urdu-script digits are handled too, because Python reads them as digits.

Known limit: a card number typed with other separators (dots, dashes in odd places) is not
found. The model never sees such text as a card anyway, and the validator blocks digit-heavy
replies, so this is a small gap, not a hole.
"""

import re

CARD_PLACEHOLDER = "[CARD]"

_FOUR_GROUPS = re.compile(r"\d{4}([ -])\d{4}\1\d{4}\1\d{4}")

_CANDIDATE = re.compile(
    r"(?<![\w.])(?:"
    r"\d{4}([ -])\d{4}\1\d{4}\1\d{4}"  # 4111 1111 1111 1111
    r"|\d{4}[ -]\d{6}[ -]\d{5}"  # American Express style: 3782 822463 10005
    r"|\d{13,19}"  # digits written together
    r")(?!\w)(?![.,]\d)"
)


def _luhn_ok(digits: str) -> bool:
    """The standard card checksum: double every second digit from the right."""
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _mask(match: re.Match[str]) -> str:
    found = match.group(0)
    if _FOUR_GROUPS.fullmatch(found) or _luhn_ok(re.sub(r"\D", "", found)):
        return CARD_PLACEHOLDER
    return found


def redact_cards(text: str) -> str:
    """Replace every card-like number in the text with [CARD]."""
    return _CANDIDATE.sub(_mask, text)
