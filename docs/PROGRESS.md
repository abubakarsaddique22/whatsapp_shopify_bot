# PROGRESS

## Status at a glance
| Phase | What | Status |
|-------|------|--------|
| 0 | Client questions (blueprint 2.6) | NOT DONE (still pending, lots left) |
| 1 | Foundation (alembic, ruff, pytest, /healthz, /readyz) | DONE, tag phase-1-done |
| 2 | Shopify read-only gateway | DONE, tag phase-2-done |
| 3 | Product tools, search, `app.cli ask` | DONE, tag phase-3-done |
| 4 | WhatsApp text (webhook, inbox/outbox, worker, /dev/simulate) | ALMOST DONE: manual live test works both ways (2026-10-14); only the remaining Step 6 checks, commit and tag are left, see below |
| 5 | Language detection + response-language control | IN PROGRESS: Steps 1 to 5 done, Step 6 next. Eval: language 92.8% (target 95%), golden set still needs native review |
| 6 to 14 | Voice, images, RAG, LangGraph, sync, security, evals, AWS, acceptance | NOT STARTED |

## Current phase
Phase 5: Language detection and response-language control. Steps 1 to 5 are done (full pytest: 505 passed, 9 skipped on 2026-10-14). Next: Step 6 (debug reply via /dev/simulate, baseline row, commit + tag). Open: router language match is 92.8% vs the 95% target (model calls short Urdu-grammar questions with English nouns 'mixed'), and the 69 golden cases need a native speaker.

## Phase 0: Client questions (NOT DONE)
- Do the client questions from the blueprint, section 2.6. Items marked A4 in the language policy table (6.1) also need client confirmation (Urdu-script text and Urdu/Punjabi voice get Roman Urdu replies).

## Phase 1, 2, 3: DONE
- Phase 1: alembic, ruff, pytest pass; /healthz and /readyz OK; tag phase-1-done
- Phase 2: Shopify read-only gateway; unit, ruff, mypy, shopify-ping and live tests pass; tag phase-2-done
- Phase 3: product tools, search, app.cli ask; tag phase-3-done

### Phase 3 steps (all done)
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

## Phase 4: WhatsApp text (ALMOST DONE)
Steps 1 to 5 are built and tested locally with a FakeSender (full pytest 505 passed on 2026-10-14, incl. webhook, worker, unsupported-type reply and duplicate-wamid tests; Postgres must be running: `docker compose up -d`). The WhatsApp access token and other variables are now in .env. Manual live test done on 2026-10-14: a message sent from WhatsApp arrives at the bot, and a reply from the bot reaches WhatsApp (both directions work). Still open: the photo/unsupported-type check, the duplicate-payload check, then commit and tag.

### Phase 4 steps
- [x] Step 1: tables + migration (db/tables.py, migrations/versions/7b2e5f1a9c63_whatsapp_text.py): conversations, whatsapp_inbox, messages, whatsapp_outbox
- [x] Step 2: payloads.py (parse Meta webhook, build outbound text) + real payload fixture + unit tests
- [x] Step 3: client.py + sender.py (RealSender via Graph API, FakeSender for local and tests)
- [x] Step 4: repositories + api/whatsapp_webhook.py (GET verify, POST signature check, dedupe by wamid)
- [x] Step 5: worker.py + api/dev.py (/dev/simulate) + main.py routers. Tests: tests/integration/test_whatsapp_webhook.py, test_worker_sender.py (tests/e2e_local is an empty folder; the blueprint does not list it for Phase 4)
- [ ] Step 6: live test on the Meta test number (text in and out works manually; photo reply and duplicate-payload checks not yet confirmed), update PROGRESS.md (done), commit + tag phase-4-done (not yet)

### Phase 4: what is still left (come back after Phase 5)
1. Sending now works in the manual test (earlier error 131031 no longer blocks it). Support ticket no longer needed unless the error comes back.
2. Live round trip on the test number: text message in and reply out works (manual). Still to confirm: send a photo and get the polite unsupported-type reply; same signed payload twice gives one reply.
3. Real credentials are in .env and the real sender path works in the manual test.
4. Nothing else to write for Phase 4: every file in the blueprint's Phase 4 list exists and has tests (payloads, client, sender, whatsapp_webhook, dev, worker, factories, integration and unit tests). media.py / media_upload.py are empty on purpose (Phases 6 and 7).
5. Meta business verification (not started).
6. Update this file, then git commit "phase 4: whatsapp text" and tag phase-4-done.

## Phase 5: Language detection and response-language control (IN PROGRESS)
Started while Phase 4 Step 6 is blocked. Tick each step when its checks pass.
- [x] Step 1: language/lexicon.py + language/detect.py (script hint + lexicon scoring). Tests: tests/unit/language/test_detect.py. Done: ruff, mypy and `uv run pytest tests\unit\language` (47 passed on this PC). More Roman Urdu words were added to config/roman_urdu_lexicon.yaml; lexicon.py itself unchanged.
- [x] Step 2: language/policy.py (reply-language table 6.1, sticky language, cheap output-language check from 6.2 layer 4). Tests: tests/unit/language/test_policy.py. Done: `uv run pytest tests\unit\language` (96 passed), mypy clean. Rules: two messages in a row switch the language; an unknown message changes nothing but breaks the run; LANGUAGE_STICKY_MESSAGES setting not used yet (SWITCH_AFTER = 2 in policy.py).
- [x] Step 3: integrations/llm/{base, chat_factory, fake_llm}.py. Tests: tests/unit/llm/ (test_fake_llm.py, test_chat_factory.py). Done: pytest, ruff, mypy pass. LLM_PROVIDER=groq via langchain-openai (OpenAI-compatible base_url); empty/unknown provider = ConfigError; fake LLM in all unit tests, real Groq only in the eval job (Step 5); Ollama only later as judge/fallback. Needs in .env: LLM_PROVIDER, LLM_API_KEY, ROUTER_MODEL (GROQ_BASE_URL still to verify against Groq docs).
- [x] Step 4: agent/router.py + prompts/router.md (structured RouterOutput, one repair). Tests: tests/unit/agent/test_router.py. Done: pytest, ruff, mypy pass. route_intent(text, llm, last_language, current_product_title): JSON extracted from the answer, validated, one repair with the problem shown to the model, then fallback intent=unclear (also on provider failure); max_price checked against price_parser (Latin script only, parser wins; known limit: a range like '5000 se 10000' takes the first number). Prompt now has the intent list, output schema, language hint and full-JSON examples. Note: _unclear() writes the None fields out because mypy does not read Field(None, ...) in domain/models.py.
- [x] Step 5: evals/router_golden.jsonl + app.cli eval-router. Tests T08, T09. Done: agent/router_eval.py (scoring + report), `eval-router` command in cli.py (with --limit), evals/router_golden.jsonl (69 cases, all synthetic-starter), tests/unit/agent/test_router_eval.py (T08, T09). Eval runs on Ollama (gpt-oss:120b-cloud, same family as the Groq router model) because Groq hit 429 rate limits: `uv run python -m app.cli eval-router` (default ollama; `--provider groq` for Groq; settings OLLAMA_BASE_URL, OLLAMA_API_KEY, EVAL_ROUTER_MODEL; chat_factory.get_eval_chat_model). Router prompt got Roman Urdu rule + 3 examples. Result: see Baselines measured. Still to do: native-speaker review of the golden set; get language match to 95% (5 of 6 wrong cases are roman_ur called mixed)
- [ ] Step 6: debug reply via /dev/simulate, record first baseline in "Baselines measured", commit + tag phase-5-done (WhatsApp live check after Phase 4 Step 6)

## Not started yet
Phase 6 voice | 7 images | 8 RAG | 9 LangGraph | 10 sync | 11 security + handoff | 12 evals | 13 AWS | 14 acceptance

## Already written (stable code, will not change)
- core/settings.py, core/logging.py (JSON + redaction + correlation id), core/errors.py,
  core/ids.py, core/security.py (signatures, scope audit), db/session.py, db/tables.py,
  api/health.py, domain/models.py (Lang, Intent, RouterOutput, BotResponse, ToolResult,
  ConversationState), main.py

- Phase 3 files: agent/rules.py, agent/tools.py, catalog/*. Phase 5 files: language/*, agent/router.py, agent/router_eval.py, prompts/router.md. prompts/composer.md is written; agent/composer.py is still empty.

- Phase 2 files: domain/catalog.py, integrations/shopify/{auth,client,queries,mapper,gateway}.py, tests/unit/shopify/test_mapper.py, tests/live/test_dev_store.py, scripts/debug_products.py. Not written on purpose: scopes.py (audit lives in core/security.py), fake_gateway.py and evals/fixtures/store.yaml (no mock store).

## Decisions (ADR index)
- 0001 Architecture overview (see Blueprint v2.0)

## Baselines measured
| Date | What | Result |
|------|------|--------|
| 2026-10-14 | Router eval, gpt-oss:120b-cloud via Ollama, 69 golden cases (all synthetic-starter, NOT a real baseline until native review) | language 92.8% (target 95%), intent 98.6%, colour 100%, max_price 100%, fallbacks 0. By class language/intent: en 100/100, mixed 100/100, pa 100/100, ur 100/100, roman_ur 83.9/96.8. Word-check only: 84.1%. 6 wrong: ru-05, ru-12, ru-22, mx-04, mx-06 (roman_ur called mixed), mx-01 (availability called colors_sizes). Earlier Groq run hit 429 (1 fallback), so it is not used |

## Open problems / last error
- Meta: earlier every send failed with 131031 (Business Account locked). Manual test on 2026-10-14 shows sending works now. Watch for it coming back.
- Router: language match 92.8% < 95% target. The model calls short roman_ur questions with English nouns 'mixed'. Options: more prompt examples, or a native speaker decides whether such labels are right. mx-01 'black wali available hai?' (availability vs colors_sizes) is a fair label dispute. policy.py gives 'mixed' and 'roman_ur' different reply languages, so the difference matters.
- ADRs for Phase 5 decisions are not written yet (Groq via OpenAI-compatible, language switch after 2 messages, Roman Urdu reply to Urdu script). README.md is outdated (still says only Phase 1).
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
| Meta developer account, app, test number | Done: app ws-bot, test number +1 555 650 6823, token and variables in .env. Inbound webhooks arrive and sends go out (manual test 2026-10-14). Earlier 131031 block no longer seen |
| Meta business verification | Not started |
| LLM / speech / embedding keys | Groq key available (for the router and eval); put LLM_PROVIDER, LLM_API_KEY, ROUTER_MODEL in .env. Add the Groq key to GitHub secrets for the eval job. Speech and embedding keys not yet |
| AWS account and domain | Not yet |
