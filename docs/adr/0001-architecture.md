# ADR 0001: Architecture overview
Date: 2026-10-11  Status: accepted

Context: Read-only WhatsApp support bot for a Shopify watch store.
Decision: Follow Blueprint v2.0: FastAPI + PostgreSQL inbox/outbox + small LangGraph
workflow + live Shopify tools + pgvector RAG. Deterministic permission gate outside the LLM.
Alternatives: Autonomous agent loop, ChromaDB, SQS/Redis (rejected for now, see Blueprint 3.4).
Consequences: Simple to test and run; extra services only when measured load needs them.
