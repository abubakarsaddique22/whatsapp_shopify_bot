# ADR 0006: Local embeddings, vectors in Postgres (pgvector)
Date: 2026-10-16  Status: accepted (re-checked 2026-10-20 after the torch upgrade)

Context: RAG over the store's policy pages and FAQ (Phase 8). The budget is small, the document
set is small (tens of pages), and customer questions must not be sent to another paid API just
to be searched.
Decision:
- Embeddings run locally with sentence-transformers, model
  `paraphrase-multilingual-MiniLM-L12-v2` (384 numbers, multilingual). The code default when
  EMBEDDING_MODEL is empty is `BAAI/bge-m3`, which has not been scored yet.
- The vectors live in the same Postgres as everything else, with the pgvector extension: table
  `kb_chunks`, column `vector(1536)`. A shorter vector is padded with zeros, so a later model
  change does not change the table.
- Search: cosine similarity, top `RAG_TOP_K` (5), with a `RAG_MIN_SCORE` (0.40) cut-off for
  abstaining. A Roman Urdu question is also searched with the router's short English
  `kb_query_en`, because MiniLM keeps English and Roman Urdu far apart (2026-10-20 smoke check:
  en ~ roman_ur 0.13 for the same question).
Alternatives: Gemini or OpenAI embeddings (cost, data leaves the server, a key to manage);
Ollama embeddings (another service to run in production); a separate vector database such as
Chroma or Qdrant (an extra service and backup for a small corpus; ADR 0001).
Consequences: No embedding cost and one database to back up. The model is downloaded once and
loaded into RAM on the server (Phase 13 sizing). Baseline 2026-10-16: hit 97.5%, MRR 0.899,
abstention 70%; the same numbers after the torch 2.13 / sentence-transformers 5.6 upgrade
(`scripts/embedding_smoke.py` checks the real model). The 70% abstention is a known gap: see
docs/eval_report.md.
