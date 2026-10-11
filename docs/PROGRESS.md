# PROGRESS

## Current phase
Phase 3: Local product-query service and validated tools (Postgres mirror, search, price/stock/specs tools, app.cli ask). Status: not started.

## Completed phases
- Phase 0: (do the client questions from the blueprint, section 2.6)
- Phase 1: done (alembic, ruff, pytest pass; /healthz and /readyz OK; tag phase-1-done)
- Phase 2: done (Shopify read-only gateway; unit, ruff, mypy, shopify-ping and live tests pass; tag phase-2-done)

## Already written (stable code, will not change)
- core/settings.py, core/logging.py (JSON + redaction + correlation id), core/errors.py,
  core/ids.py, core/security.py (signatures, scope audit), db/session.py, db/tables.py,
  api/health.py, domain/models.py (Lang, Intent, RouterOutput, BotResponse, ToolResult,
  ConversationState), main.py

- Phase 2 files: domain/catalog.py, integrations/shopify/{auth,client,queries,mapper,gateway}.py, tests/unit/shopify/test_mapper.py, tests/live/test_dev_store.py, scripts/debug_products.py. Not written on purpose: scopes.py (audit lives in core/security.py), fake_gateway.py and evals/fixtures/store.yaml (no mock store).

## Still to write, step by step
Phase 3 product tools | 4 WhatsApp text | 5 language | 6 voice | 7 images |
8 RAG | 9 LangGraph | 10 sync | 11 security + handoff | 12 evals | 13 AWS | 14 acceptance

## Decisions (ADR index)
- 0001 Architecture overview (see Blueprint v2.0)

## Baselines measured
| Date | What | Result |
|------|------|--------|

## Open problems / last error
- Shopify returns onlineStoreUrl = null for every product while the store is in Private mode (password page). Private mode must be OFF for the visibility rule (ACTIVE and onlineStoreUrl not null) to see products. Live tests pass with it off.
- config/specs_mapping.yaml still has placeholder metafield names (custom.*); replace with the client's real names.
- Refund policy comes from shopPolicies; returnPolicyProfiles (API 2026-10) not checked yet.

## Parked ideas (do not build now)
-

## External accounts and status
| Item | Status |
|------|--------|
| Shopify Partner / Dev Dashboard app | Done: client credentials, read-only scopes pass audit |
| Dev store with synthetic data | 2 active products, policy pages created; Private mode off |
| Meta developer account, app, test number | |
| Meta business verification | |
| LLM / speech / embedding keys | |
| AWS account and domain | |
