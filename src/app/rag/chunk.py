"""Phase 8, step 2: cut a cleaned document into chunks (Blueprint 7.5).

    chunks = chunk_document("policy_refund", "Refund policy", clean_text(policy.body))
    chunks[0].heading_path   # "Refund policy > Returns"
    chunks[0].text           # the words under that heading

Two ways of cutting:

- Policies and pages (every doc_type except "faq"): one chunk per heading section. A section that
  is longer than MAX_CHUNK_TOKENS is cut into several chunks, line by line (a line is a paragraph
  or a list item), then by sentence, and only at the very end by words. When a section is cut,
  the last line of a chunk is repeated at the start of the next one if it is short (OVERLAP_TOKENS).
  Text before the first heading belongs to the document title. A heading with no text under it
  makes no chunk.
- FAQ: one question and its answer per chunk, written as "Q: ...\\nA: ...". A question is a
  heading or a line that ends with "?" (or the Urdu question mark), or a "Q:" line. If the page
  has no such line, every heading is taken as a question. If the page has no structure at all, it
  is cut like a normal page.

`heading_path` is the title followed by the headings above the section: "Refund policy > Returns".
The mapper writes every heading level as "## " today, so the path has one heading level. If the
mapper ever keeps the level ("#", "##", "###"), the path becomes nested without a change here.

The text of a chunk does not contain its heading. When a chunk is embedded or searched, join
heading_path and text (rag/index.py), so the section name helps the search.

Sizes are tokens estimated from words (about 1.3 tokens per word). It is a guess without a
tokenizer, good enough for chunk sizes. Phase 8 tunes MAX_CHUNK_TOKENS with the golden set.
"""

import re

from pydantic import BaseModel, Field

from app.rag.clean import content_hash

# MiniLM (paraphrase-multilingual-MiniLM-L12-v2) reads only 128 tokens and silently cuts the rest.
# A chunk can be up to OVERLAP_TOKENS bigger than MAX_CHUNK_TOKENS, so 100 + 20 stays under 128.
# With a bigger model (bge-m3 reads 8192) these two numbers can go up again.
MAX_CHUNK_TOKENS = 100
OVERLAP_TOKENS = 20
MAX_QUESTION_CHARS = 200  # a longer line that ends with "?" is a sentence, not a question

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?\u061f\u06d4])\s+")
_QUESTION_PREFIX = re.compile(r"^q(?:uestion)?\s*[:.)]\s*(.+)$", re.IGNORECASE)
_ANSWER_PREFIX = re.compile(r"^a(?:nswer)?\s*:\s*", re.IGNORECASE)


class Chunk(BaseModel):
    ordinal: int = Field(ge=0)  # position in the document, from 0
    heading_path: str | None
    text: str
    token_count: int = Field(ge=1)
    content_hash: str  # of heading_path + text: same words under the same heading = same hash


def estimate_tokens(text: str) -> int:
    """About 1.3 tokens per word, rounded up (integer maths, so no float surprises)."""
    return max(1, (len(text.split()) * 13 + 9) // 10)


def chunk_document(
    doc_type: str,
    title: str,
    body: str,
    *,
    max_tokens: int = MAX_CHUNK_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[Chunk]:
    """Chunks of an already cleaned text (clean_text). Empty text gives an empty list."""
    title = title.strip()
    pieces: list[tuple[str | None, str]] = []
    if doc_type == "faq":
        pieces = _faq_pieces(title, body, max_tokens)
    if not pieces:
        pieces = _heading_pieces(title, body, max_tokens, overlap_tokens)
    return [_make_chunk(i, path, text) for i, (path, text) in enumerate(pieces)]


def _make_chunk(ordinal: int, heading_path: str | None, text: str) -> Chunk:
    return Chunk(
        ordinal=ordinal,
        heading_path=heading_path,
        text=text,
        token_count=estimate_tokens(text),
        content_hash=content_hash(f"{heading_path or ''}\n{text}"),
    )


# ---- policies and pages: one chunk per heading section


def _sections(title: str, body: str) -> list[tuple[str | None, list[str]]]:
    """[(heading_path, lines)] in reading order. Empty sections are left out."""
    sections: list[tuple[str | None, list[str]]] = []
    stack: dict[int, str] = {}  # heading level -> heading text, for the headings above us
    path: str | None = title or None
    lines: list[str] = []
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        match = _HEADING.match(line)
        if not match:
            lines.append(line)
            continue
        if lines:
            sections.append((path, lines))
            lines = []
        level = len(match.group(1))
        stack = {lvl: text for lvl, text in stack.items() if lvl < level}
        stack[level] = match.group(2)
        parts: list[str] = [title] if title else []
        for lvl in sorted(stack):
            if not parts or parts[-1] != stack[lvl]:  # "FAQ" page with a "FAQ" heading: once
                parts.append(stack[lvl])
        path = " > ".join(parts) or None
    if lines:
        sections.append((path, lines))
    return sections


def _split_long_line(line: str, max_tokens: int) -> list[str]:
    """The line itself if it fits; else its sentences; a sentence that is too long goes by words."""
    if estimate_tokens(line) <= max_tokens:
        return [line]
    pieces: list[str] = []
    words_per_piece = max(1, max_tokens * 10 // 13)
    for sentence in _SENTENCE_END.split(line):
        if estimate_tokens(sentence) <= max_tokens:
            pieces.append(sentence)
            continue
        words = sentence.split()
        for start in range(0, len(words), words_per_piece):
            pieces.append(" ".join(words[start : start + words_per_piece]))
    return pieces


def _pack(units: list[str], max_tokens: int, overlap_tokens: int) -> list[str]:
    """Join units with newlines into chunks of up to max_tokens.

    After a chunk is closed, its last unit starts the next one if that unit is short. So a
    chunk can be up to overlap_tokens bigger than max_tokens.
    """
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for unit in units:
        size = estimate_tokens(unit)
        if current and used + size > max_tokens:
            chunks.append("\n".join(current))
            tail = current[-1]
            tail_size = estimate_tokens(tail)
            current, used = ([tail], tail_size) if tail_size <= overlap_tokens else ([], 0)
        current.append(unit)
        used += size
    if current:
        chunks.append("\n".join(current))
    return chunks


def _heading_pieces(
    title: str, body: str, max_tokens: int, overlap_tokens: int
) -> list[tuple[str | None, str]]:
    pieces: list[tuple[str | None, str]] = []
    for path, lines in _sections(title, body):
        units = [part for line in lines for part in _split_long_line(line, max_tokens)]
        pieces.extend((path, text) for text in _pack(units, max_tokens, overlap_tokens))
    return pieces


# ---- FAQ: one question and answer per chunk


def _as_question(line: str) -> str | None:
    """The question if the line is one: "Q: ..." or a short line ending with ? / the Urdu mark."""
    prefixed = _QUESTION_PREFIX.match(line)
    if prefixed:
        return prefixed.group(1).strip()
    if len(line) <= MAX_QUESTION_CHARS and line.endswith(("?", "\u061f")):
        return line
    return None


def _faq_pairs(body: str) -> list[tuple[str, list[str]]]:
    """[(question, answer lines)] for every question that has an answer."""
    entries: list[tuple[bool, str]] = []  # (is a heading, text without the "## ")
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        match = _HEADING.match(line)
        entries.append((bool(match), match.group(2) if match else line))

    has_questions = any(_as_question(text) for _, text in entries)
    pairs: list[tuple[str, list[str]]] = []
    question: str | None = None
    answer: list[str] = []
    for is_heading, text in entries:
        found = _as_question(text) if has_questions else (text if is_heading else None)
        if found:
            if question and answer:
                pairs.append((question, answer))
            question, answer = found, []
        elif is_heading and has_questions:
            continue  # a section label between questions: not part of any answer
        elif question is not None:
            line = text if answer else _ANSWER_PREFIX.sub("", text, count=1)
            if line:  # a lone "A:" leaves nothing
                answer.append(line)
    if question and answer:
        pairs.append((question, answer))
    return pairs


def _faq_pieces(title: str, body: str, max_tokens: int) -> list[tuple[str | None, str]]:
    path = title or None
    pieces: list[tuple[str | None, str]] = []
    for question, answer in _faq_pairs(body):
        prefix = f"Q: {question}\nA: "
        units = [part for line in answer for part in _split_long_line(line, max_tokens)]
        # An answer that is too long is cut, and every piece repeats the question.
        budget = max(20, max_tokens - estimate_tokens(prefix))
        for piece in _pack(units, budget, 0):
            pieces.append((path, prefix + piece))
    return pieces
