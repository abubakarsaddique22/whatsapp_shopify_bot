# PROGRESS

## Current phase
Phase 4: WhatsApp text (webhook, inbox/outbox, worker, /dev/simulate). Status: in progress, step 1 written (not yet checked). Plan: build and test everything locally with a FakeSender; the live WhatsApp test (step 6) is blocked because Meta returns error 131031 (Business Account locked) on every send from the test WABA.

### Phase 4 steps (one file at a time; run the checks, then report)
- [ ] Step 1: tables + migration (db/tables.py, migrations/versions/7b2e5f1a9c63_whatsapp_text.py): conversations, whatsapp_inbox, messages, whatsapp_outbox
- [ ] Step 2: payloads.py (parse Meta webhook, build outbound text) + real payload fixture + unit tests
- [ ] Step 3: client.py + sender.py (RealSender via Graph API, FakeSender for local and tests)
- [ ] Step 4: repositories + api/whatsapp_webhook.py (GET verify, POST signature check, dedupe by wamid)
- [ ] Step 5: worker.py + api/dev.py (/dev/simulate) + main.py routers + tests/e2e_local
- [ ] Step 6: live test on the Meta test number (blocked by 131031), update PROGRESS.md, commit + tag phase-4-done

### Phase 3 steps (one at a time; run the checks, then report)
- [x] Step 1: price parser (language/price_parser.py, language/lexicon.py) + mirror migration check. Tests: test_price_parser, test_lexicon
- [x] Step 2: normalizer (catalog/normalizer.py): text, aliases, option names, colours, case size. Tests: test_normalizer
- [x] Step 3: price formatter + stock helper (catalog/price.py, catalog/stock.py). Tests: test_price, test_stock (pass)
- [x] Step 4: mirror import (db/repositories.py + `app.cli import-catalog`): real store -> Postgres mirror (tests pass)
- [x] Step 5: search (catalog/search.py): pg_trgm + filters, one match / ambiguous / none. Tests T13, T14 on the real store (pass)
- [x] Step 6: specs + images (catalog/specs.py, catalog/images.py). Test T04 (pass)
- [x] Step 7: tools (agent/tools.py, tools 1 to 6) + tests/integration/test_tools.py. Tests T01-T04, T13-T15 (pass, incl. RUN_LIVE=1 live price/stock)
- [x] Step 8: CLI `ask` (rule-based), ten manual questions vs Shopify admin, update PROGRESS.md, git commit + tag phase-3-done. Done (tag phase-3-done).

Testing rule: no fake store data. Search, tools and CLI run against the real dev store. Only T15 (Shopify down) uses a simulated error.
Store data still to add in Shopify: colour variants (GA-2100: Black/Blue/Silver), low stock (qty 1-3), specs metafields (custom.case_diameter, custom.water_resistance), a Case size option, more images.

## Completed phases
- Phase 0: (do the client questions from the blueprint, section 2.6)
- Phase 1: done (alembic, ruff, pytest pass; /healthz and /readyz OK; tag phase-1-done)
- Phase 2: done (Shopify read-only gateway; unit, ruff, mypy, shopify-ping and live tests pass; tag phase-2-done)
- Phase 3: done (product tools, search, app.cli ask; tag phase-3-done)

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
| Meta developer account, app, test number | Done: app ws-bot, test number +1 555 650 6823, inbound webhooks arrive. BLOCKED: every send fails with 131031 Business Account locked (account shows Approved/Connected). Support ticket to open via Business Support Home |
| Meta business verification | |
| LLM / speech / embedding keys | |
| AWS account and domain | |
