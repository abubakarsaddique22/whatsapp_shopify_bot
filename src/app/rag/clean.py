"""Phase 8, step 2: make the text of a policy or page ready for chunking (Blueprint 7.5).

    text = clean_text(policy.body)
    content_hash(text)

The Shopify gateway already turns merchant HTML into text (integrations/shopify/mapper.py,
`html_to_text`: no scripts, styles or hidden parts, headings written as "## Heading"). This module
does NOT repeat that work. If a text still contains HTML tags it sends it through the same
function, so there is one HTML cleaner in the project, and then it makes the text steady:

- Unicode NFKC (a no-break space becomes a space, full-width digits become normal digits);
- zero-width space, word joiner, byte order mark and soft hyphen are dropped. U+200C and U+200D
  stay on purpose: Urdu and Persian spelling uses them inside words;
- control characters are dropped, line breaks become "\\n", runs of spaces become one space;
- no more than one empty line in a row, no spaces at the ends of lines.

The result is the same when it runs twice, so a text that did not change always gets the same
hash. That is what lets ingest skip it (Blueprint 7.6).
"""

import hashlib
import re
import unicodedata

from app.integrations.shopify.mapper import html_to_text

# "<br>", "</p>", "<h2 class=...>": a letter right after "<". "less than <5 days" is not a tag.
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")

# None = delete the character. U+2028 and U+2029 are line separators.
_TRANSLATE: dict[int, str | None] = {
    0x200B: None,  # zero-width space
    0x2060: None,  # word joiner
    0xFEFF: None,  # byte order mark
    0x00AD: None,  # soft hyphen
    0x2028: "\n",
    0x2029: "\n",
}


def clean_text(raw: str | None) -> str:
    """Steady, plain text. Empty input gives an empty string."""
    if not raw:
        return ""
    text = html_to_text(raw) if _HTML_TAG.search(raw) else raw
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def content_hash(text: str) -> str:
    """SHA-256 of the text, as 64 hex characters. Used for documents and for chunks."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
