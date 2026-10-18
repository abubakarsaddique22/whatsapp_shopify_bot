"""Phase 8, step 3: bring the store's approved texts into the knowledge base (Blueprint 7.2, 7.6).

    docs = await fetch_sources()                      # policies and pages from Shopify
    async with session_factory() as session, session.begin():
        report = await ingest_documents(session, get_embedder(), docs)

What one run does for every document (a "source", named by source_key):

1. Clean the text (rag/clean.py) and cut it into chunks (rag/chunk.py). A document with no
   text is the same as a missing one.
2. Scan the text for injection (security/injection_scan.py). A flagged document is NOT indexed
   and is listed in the report. Its previous version stays active.
3. Compare hashes. Same hash and same embedding model: nothing to do ("unchanged").
4. Changed or new: embed only the chunks whose hash is new (rag/index.py reuses the vector of
   a chunk it already has), deactivate the old version, store the new one as version + 1, keep
   the last few old versions for audit.
5. After the loop: an active source that is no longer in the store is deactivated and its chunks
   are deleted.

Safety stops:
- Embedding happens BEFORE anything is written, so a provider failure changes nothing.
- If the store returned no document at all, nothing is removed (an API problem must not wipe the
  knowledge base). The report says `skipped_removal`.
- The caller owns the transaction (commit or roll back), like db/repositories.py.

The document hash covers the doc type, title, link, language and cleaned text, so a changed
official link makes a new version too. Its chunks are all reused, so it costs no embedding.
"""

import logging
import re
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConfigError
from app.db import repositories as repo
from app.domain.catalog import Page, Policy
from app.integrations.embeddings.base import Embedder
from app.integrations.shopify import gateway
from app.integrations.shopify.auth import shop_host
from app.rag import index
from app.rag.chunk import chunk_document
from app.rag.clean import clean_text, content_hash
from app.rag.index import SourceDocument
from app.security.injection_scan import Scanner, scan_text

log = logging.getLogger(__name__)

# Shopify policy type -> our doc_type (Blueprint 7.4). Other policies (terms, legal notice,
# contact information) are kept as plain "page" documents.
POLICY_DOC_TYPES = {
    "REFUND_POLICY": "policy_refund",
    "SHIPPING_POLICY": "policy_shipping",
    "PRIVACY_POLICY": "policy_privacy",
}
OTHER_DOC_TYPE = "page"

# Sources this module manages. Removal only looks at keys with these prefixes.
MANAGED_PREFIXES: tuple[str, ...] = ("policy:", "page:")

_FAQ_WORDS = re.compile(r"\b(?:faq|faqs|frequently asked|sawal)\b", re.IGNORECASE)


class FlaggedDocument(BaseModel):
    source_key: str
    findings: list[str]


class IngestReport(BaseModel):
    total: int = 0  # documents given to the run
    added: int = 0  # new sources
    updated: int = 0  # new version of a known source
    unchanged: int = 0
    removed: int = 0  # sources that left the store
    skipped_empty: int = 0  # no text, so nothing to index
    flagged: list[FlaggedDocument] = Field(default_factory=list)
    chunks_embedded: int = 0  # sent to the embedding provider
    chunks_reused: int = 0  # vector copied from an identical stored chunk
    skipped_removal: bool = False  # the store gave no document at all, so nothing was removed


# ---- sources


def policy_source(policy: Policy) -> SourceDocument:
    kind = policy.type.strip().upper()
    return SourceDocument(
        source_key=f"policy:{kind}",
        doc_type=POLICY_DOC_TYPES.get(kind, OTHER_DOC_TYPE),
        source="shopify_policy",
        title=policy.title.strip() or kind.replace("_", " ").capitalize(),
        source_url=policy.url,
        body=policy.body,
    )


def page_source(page: Page, host: str | None) -> SourceDocument:
    """A page is a FAQ when its title or handle says so; any other page is a plain page."""
    label = f"{page.title} {page.handle.replace('-', ' ')}"
    return SourceDocument(
        source_key=f"page:{page.id}",
        doc_type="faq" if _FAQ_WORDS.search(label) else OTHER_DOC_TYPE,
        source="shopify_page",
        title=page.title.strip(),
        source_url=f"https://{host}/pages/{page.handle}" if host and page.handle else None,
        body=page.body,
    )


def build_sources(
    policies: Sequence[Policy], pages: Sequence[Page], host: str | None
) -> list[SourceDocument]:
    return [policy_source(p) for p in policies] + [page_source(p, host) for p in pages]


async def fetch_sources() -> list[SourceDocument]:
    """Policies and pages through the Shopify gateway. A Shopify error stops the run early."""
    policies = await gateway.list_policies()
    pages = await gateway.list_pages()
    try:
        host: str | None = shop_host()
    except ConfigError:
        host = None
    return build_sources(policies, pages, host)


def _doc_type_for_stem(stem: str) -> str:
    if stem.startswith("policy_"):
        return stem
    return "faq" if stem == "faq" else OTHER_DOC_TYPE


def load_sources_from_dir(folder: Path) -> list[SourceDocument]:
    """Documents from a folder of .md files (local development and the golden-set corpus).

    The file name gives the doc_type: policy_refund.md -> policy_refund, faq.md -> faq, any
    other name -> page. A first line "# Title" is the title; the rest is the text.
    """
    files = sorted(folder.glob("*.md"))
    if not files:
        raise ConfigError(f"No .md files found in {folder}")
    docs: list[SourceDocument] = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        title = path.stem.replace("_", " ").capitalize()
        body_lines = lines
        for number, line in enumerate(lines):
            if line.strip():
                if line.startswith("# "):
                    title = line[2:].strip()
                    body_lines = lines[number + 1 :]
                break
        docs.append(
            SourceDocument(
                source_key=f"file:{path.stem}",
                doc_type=_doc_type_for_stem(path.stem),
                source="local_file",
                title=title,
                body="\n".join(body_lines),
            )
        )
    return docs


# ---- ingest


def document_hash(doc: SourceDocument, cleaned: str) -> str:
    """Changes when the text, title, doc type, link or language changes."""
    parts = [doc.doc_type, doc.title, doc.source_url or "", doc.language, cleaned]
    return content_hash("\n".join(parts))


async def ingest_documents(
    session: AsyncSession,
    embedder: Embedder,
    docs: Sequence[SourceDocument],
    *,
    scanner: Scanner = scan_text,
    managed_prefixes: tuple[str, ...] = MANAGED_PREFIXES,
) -> IngestReport:
    """Make the knowledge base match `docs`. Does not commit."""
    report = IngestReport(total=len(docs))
    active = await index.active_documents(session)
    seen: set[str] = set()

    for doc in docs:
        cleaned = clean_text(doc.body)
        chunks = chunk_document(doc.doc_type, doc.title, cleaned) if cleaned else []
        if not chunks:
            report.skipped_empty += 1
            continue
        seen.add(doc.source_key)  # flagged documents count too: their old version stays

        # The title goes into every chunk, so it is scanned with the text.
        findings = scanner(f"{doc.title}\n{cleaned}")
        if findings:
            report.flagged.append(FlaggedDocument(source_key=doc.source_key, findings=findings))
            log.warning(
                "rag_ingest_flagged", extra={"source_key": doc.source_key, "findings": findings}
            )
            # One row per version of the document, not one per sync run (Blueprint 9.4).
            await repo.add_security_event_once(
                session,
                "document_flagged",
                ref=doc.source_key,
                detail={"findings": findings, "document_hash": document_hash(doc, cleaned)},
            )
            continue

        digest = document_hash(doc, cleaned)
        state = active.get(doc.source_key)
        if state and state.content_hash == digest and state.embedding_models == {embedder.model}:
            report.unchanged += 1
            continue

        # Embed first: if the provider fails, nothing has been written yet.
        vectors, embedded, reused = await index.embed_chunks(session, embedder, chunks)
        if state:
            await index.deactivate_document(session, state.id)  # one active version per source
        version = await index.next_version(session, doc.source_key)
        doc_id = await index.insert_document(session, doc, version=version, content_hash=digest)
        await index.insert_chunks(session, doc_id, chunks, vectors, embedder.model)
        await index.prune_versions(session, doc.source_key)

        if state:
            report.updated += 1
        else:
            report.added += 1
        report.chunks_embedded += embedded
        report.chunks_reused += reused

    stale = [key for key in active if key.startswith(managed_prefixes) and key not in seen]
    if stale and not seen:
        report.skipped_removal = True  # the store gave nothing: do not wipe the knowledge base
        log.warning("rag_ingest_skipped_removal", extra={"stale": len(stale)})
    else:
        for key in stale:
            await index.deactivate_document(session, active[key].id)
            await index.delete_chunks(session, active[key].id)
            report.removed += 1

    log.info(
        "rag_ingest_done",
        extra={
            "added": report.added,
            "updated": report.updated,
            "unchanged": report.unchanged,
            "removed": report.removed,
            "flagged": len(report.flagged),
            "chunks_embedded": report.chunks_embedded,
            "chunks_reused": report.chunks_reused,
        },
    )
    return report
