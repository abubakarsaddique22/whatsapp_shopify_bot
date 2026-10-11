# PROGRESS

## Current phase
Phase 1: Foundation. Status: skeleton delivered, run the tests locally (see README).

## Completed phases
- Phase 0: (do the client questions from the blueprint, section 2.6)
- Phase 1: (mark done after tests pass + git tag phase-1-done)

## Already written (stable code, will not change)
- core/settings.py, core/logging.py (JSON + redaction + correlation id), core/errors.py,
  core/ids.py, core/security.py (signatures, scope audit), db/session.py, db/tables.py,
  api/health.py, domain/models.py (Lang, Intent, RouterOutput, BotResponse, ToolResult,
  ConversationState), main.py

## Still to write, step by step
Phase 2 Shopify client | 3 product tools | 4 WhatsApp text | 5 language | 6 voice | 7 images |
8 RAG | 9 LangGraph | 10 sync | 11 security + handoff | 12 evals | 13 AWS | 14 acceptance

## Decisions (ADR index)
- 0001 Architecture overview (see Blueprint v2.0)

## Baselines measured
| Date | What | Result |
|------|------|--------|

## Open problems / last error
-

## Parked ideas (do not build now)
-

## External accounts and status
| Item | Status |
|------|--------|
| Shopify Partner / Dev Dashboard app | |
| Dev store with synthetic data | |
| Meta developer account, app, test number | |
| Meta business verification | |
| LLM / speech / embedding keys | |
| AWS account and domain | |
