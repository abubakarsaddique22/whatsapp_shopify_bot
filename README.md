# WhatsApp + Shopify AI Support Bot (read-only)

Chhota, safe bot: customer WhatsApp par text ya voice bhejta hai, bot Shopify ke live data
ya approved documents se jawab deta hai. Ye kuch change nahi kar sakta (read-only).

Design: `Shopify_WhatsApp_AI_Support_Bot_Master_Blueprint_v2.pdf` (Blueprint v2.0).
Taza haalat hamesha `docs/PROGRESS.md` mein hoti hai.

## Ab tak kya bana hai

| Phase | Kaam | Haalat |
|-------|------|--------|
| 1 | Foundation: settings, logging, errors, DB, `/healthz`, `/readyz` | Done |
| 2 | Shopify read-only gateway (live products, price, stock) | Done |
| 3 | Product tools, search, catalog mirror, `app.cli ask` | Done |
| 4 | WhatsApp text: webhook, inbox/outbox, worker, `/dev/simulate` | Done, tag phase-4-done (sirf real phone se photo check baqi) |
| 5 | Language detection, router, reply-language policy, debug reply | Done (router language match 94.2% on 2026-10-19 (Ollama), target 95%: abhi NOT MET; golden labels ka native speaker review baqi) |
| 6 | Voice: fake STT, Groq Whisper adapter, media download + ffmpeg, voice gate, `/dev/simulate` audio, bake-off script | Steps 1 se 5 done (tests pass). Step 6 (bake-off, live voice-note check) phone aur token ka intezar |
| 7 | Product images: image resolver, JPEG conversion (AVIF/WebP), WhatsApp image bhejna (link ya upload), image flow (`image_request`), text-link fallback | Steps 1 se 5 done (T05, T23, T24 pass, fakes ke saath). Asli phone par images ka live check phone aur token ka intezar |
| 8 | RAG: policies/FAQ ko chunk karna, local embeddings (sentence-transformers, `paraphrase-multilingual-MiniLM-L12-v2`; code ka default `BAAI/bge-m3` hai, uska score abhi nahi naapa), pgvector index, hybrid retrieval, `rag-ingest` / `rag-search` / `rag-eval` | Done, tag phase-8-done (baseline 2026-10-16: hit rate 97.5%, abstention 14/20). Real store par `rag-ingest` / `rag-search` ka manual check baqi |
| 9 | LangGraph workflow: state, permission gate, tools, clarify/refuse/handoff templates, composer, validator, graph + Postgres checkpointer, worker, `app.cli chat` | Done, tag phase-9-done (full pytest/ruff/mypy pass, asli model se 12 messages ka check 2026-10-17). Phone par 10-turn check aur LangSmith traces phone ka intezar |
| 10 | Shopify sync: webhook endpoint (HMAC), webhook registration, sync worker (handlers), selective re-embedding, reconciliation jobs (delta / full / policy), monitor + alarms + dead letter | Done (tests pass, 2026-10-17: T16, T17, T18, out-of-order, retry, missed-event repair). Commit + tag phase-10-done haath se |
| 11 | Security, privacy, handoff: card redaction, injection scan (English, Roman Urdu, Urdu), rate limits + daily budget, handoff (pause, resume, notifier), kill switch, admin API (default band), retention + `delete_customer.py`, adversarial tests T19 se T21 | Steps 1 se 10 done: adversarial tests, `docs/threat_model.md`, `docs/security_checklist.md`; commit + tag phase-11-done ho chuka (2026-10-19). Phone par aakhri manual attack check baqi |
| 12 | Evaluation: product Q&A, natural Q&A, security, language, tools, retrieval, duplicates, latency evaluators + `scripts/run_eval.py` (`--all`) | Shuru (2026-10-18). Steps 1 se 4 done (2026-10-19): sab evaluators likhe gaye aur `run_eval.py --all --ollama` chal chuka. MET: natural, tools, security, duplicates (0 duplicate replies), latency (Groq, text p50 3.5 s, p95 5.7 s). NOT MET: language (router 94.2%, target 95%), retrieval (hit rate 97.5% MET, abstention 70%, target 90%), golden 64/65 (colors-06 internet band hone se run error tha, ghalat fact nahi; sirf wahi sawal dobara chalana baqi). Baqi: Step 5 failure drills, T01 se T25 map, LangSmith datasets, CI gate, Groq baselines + `docs/eval_report.md`, commit + tag phase-12-done |
| 13 se 14 | AWS, acceptance | Shuru nahi hua |

## Code ka naqsha (`src/app/`)

| Folder / file | Kaam |
|---------------|------|
| `core/` | Settings (`.env`), JSON logging aur redaction, errors, ids, signature checks |
| `db/` | Async session, tables, repositories (mirror, inbox/outbox, conversation state) |
| `domain/` | Pydantic models: Lang, Intent, RouterOutput, BotResponse, ConversationState |
| `integrations/shopify/` | Read-only Shopify GraphQL gateway |
| `integrations/whatsapp/` | Meta payloads, client (text aur image), sender (retry rules, image fallback), media download, `media_upload.py` (image upload) |
| `integrations/llm/` | Chat model factory (Groq), FakeLLM tests ke liye |
| `integrations/embeddings/` | Embeddings: FakeEmbedder (tests), SentenceTransformerEmbedder (local, koi key nahi; `.env` mein `paraphrase-multilingual-MiniLM-L12-v2`, khali chhodein to `BAAI/bge-m3`), factory |
| `rag/` | `clean`, `chunk`, `index` (sirf badla hua content dobara embed), `ingest`, `retrieve` (hybrid + threshold), `rag_eval` |
| `integrations/speech/` | Speech-to-text: FakeSTT, Groq Whisper adapter, factory |
| `voice/` | Voice gate (confidence, limits, hallucination check), ffmpeg convert, pipeline, scoring (WER/CER) |
| `catalog/` | Normalizer, search (pg_trgm), price, stock, specs, images, `image_formats` (JPEG rendition), importer |
| `language/` | Roman Urdu lexicon, detection, price parser, language policy (table 6.1) |
| `agent/` | `router.py` (intent + language), `state.py`, `gate.py` (permission gate), `nodes.py`, `composer.py`, `validator.py`, `templates.py`, `graph.py` (LangGraph + checkpointer), `tools.py`, `rules.py`, `router_eval.py`; `language_step.py` aur `image_flow.py` ab sirf `DEBUG_REPLY=true` ke liye |
| `api/` | `/healthz`, `/readyz`, WhatsApp webhook, Shopify webhook (`shopify_webhook.py`: signature check, event store, jaldi 200), `/dev/*` (sirf local) |
| `sync/` | `webhooks.py` (delivery parse), `handlers.py` (event aane par product dobara fetch, mirror update, cache saaf), `worker.py` (alag process, retry + dead letter), `reconcile.py` (delta / full / policy jobs), `monitor.py` (alarms, dead letter retry) |
| `jobs/` | `reconcile.py` (`--mode delta/full/policy` ya `--loop`), `retention.py` |
| `worker.py` | Inbound message uthata hai, graph se jawab banata hai, outbox mein daalta hai, bhejta hai |
| `cli.py` | `ask`, `chat`, `import-catalog`, `eval-router`, `rag-ingest`, `rag-search`, `rag-eval` |
| `scripts/` | `bakeoff.py` (speech provider ka muqabla), `debug_products.py`, `register_webhooks.py` (Shopify webhooks banata hai, dobara chalane par duplicate nahi) |
| `prompts/`, `config/`, `evals/` | Router aur composer prompts, lexicon aur mappings, golden sets |

Phase 11: `security/` (`redaction.py`, `injection_scan.py`, `rate_limit.py`, `erasure.py`), `handoff/` (`service.py`, `notifier.py`), `api/admin.py` (default band), `jobs/retention.py`, `scripts/delete_customer.py`.

Phase 12: `src/app/evals/` (`evaluators.py` exact-fact checks, `product_qa.py` golden + natural Q&A scoring, `security.py`, `language.py`, `tools.py`, `retrieval.py`, `runner.py` = asli bot se ek sawal poochna, script aur tests dono istemal karte hain), `scripts/run_eval.py` (asli bot par eval chalata hai), `scripts/make_product_qa.py` aur `scripts/make_natural_qa.py` (sawalon ki files banate hain), `tests/llm/` (asli model wale tests, `-m llm`).

## Windows (cmd) mein chalane ke steps
```
REM 1) uv install (ek baar)
winget install --id=astral-sh.uv -e

REM 2) project folder mein
uv sync
copy .env.example .env

REM 3) database
docker compose up -d db
uv run alembic upgrade head

REM 4) tests, lint, types
uv run pytest
uv run ruff check .
uv run mypy src

REM 5) server (ek window) aur worker (doosri window)
uv run uvicorn app.main:app --reload --app-dir src
uv run python -m app.worker
```
Phir browser mein kholein: http://localhost:8000/docs aur http://localhost:8000/healthz

VS Code: folder open karein, extensions install karein (popup aayega), `Ctrl+Shift+P` ->
"Python: Select Interpreter" -> `.venv`.

## Zaban (language) ko local test karna
Bina asli WhatsApp ke bot ko message bhejne ke liye (`APP_ENV=local`):
```
REM .env mein: DEBUG_REPLY=true, LLM_PROVIDER=groq, LLM_API_KEY, ROUTER_MODEL
curl -X POST http://localhost:8000/dev/simulate -H "Content-Type: application/json" -d "{\"text\":\"GA-2100 ki price kya hai\"}"
curl http://localhost:8000/dev/outbox
```
`DEBUG_REPLY=true` par jawab mein bot batata hai ke usne kya samjha:
`[debug] language=... | intent=... | conversation=... | reply_in=...`.
Kaam khatam hote hi `DEBUG_REPLY=false` kar dein. Production mein ye kabhi on nahi hona chahiye.

Router ki quality napne ke liye: `uv run python -m app.cli eval-router`
(69 golden cases; Ollama par chalta hai, `--provider groq` se Groq par).

## Voice ko local test karna (phone ke baghair)
ffmpeg install hona chahiye (`winget install ffmpeg`, phir shell dobara kholein).
```
REM .env mein: STT_PROVIDER=fake aur STT_FAKE_TEXT=GA-2100 ki price kya hai
uv run pytest tests\unit\voice tests\integration\test_worker_voice.py
```
`/dev/simulate` mein `msg_type=audio` bhejne par worker download chhod deta hai aur `STT_FAKE_TEXT`
ko transcript maan leta hai. Asli voice note ke liye `STT_PROVIDER=groq` aur `STT_API_KEY` chahiye,
aur phone ke saath naya WhatsApp token.

Speech provider ka muqabla (bake-off), jab clips `data\audio` mein hon aur `evals\voice_manifest.csv`
mein likhe hon:
```
uv run python scripts\bakeoff.py --limit 3
```

## Images ko local test karna (phone ke baghair)
Ab router har customer text par chalta hai (sirf debug mode mein nahi), is liye `.env` mein
`LLM_PROVIDER`, `LLM_API_KEY` aur `ROUTER_MODEL` hona zaroori hai, warna worker message ko `failed` kar deta hai.
```
uv run pytest tests\unit\catalog\test_images.py tests\unit\catalog\test_image_formats.py
uv run pytest tests\integration\test_image_flow.py tests\integration\test_sender_images.py
```
Image flow: router `image_request` deta hai, product dhoondta hai (ambiguous ho to ek sawal), phir
zyada se zyada 3 pictures outbox mein jaate hain. Sender pehle JPEG link bhejta hai, Meta mana kare to
download + upload karta hai, phir bhi na jaye to product ka text link bhejta hai aur failure log mein likhta hai.
Bina image wale product par "picture nahi hai" aur link jata hai. Asli images ka check phone aur naye token ke baad.

## RAG ko local test karna
`.env` mein `EMBEDDING_PROVIDER=sentence-transformers`, `EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2`, `RAG_TOP_K=5` aur `RAG_MIN_SCORE=0.40` rakhein (ye wahi hain jin se `rag-eval` baseline naapa gaya). Model local chalta hai,
koi key nahi chahiye; pehli baar takreeban 470 MB download hota hai. `EMBEDDING_MODEL` khali ho to `BAAI/bge-m3` (takreeban 2.2 GB) use hota hai, us ka score abhi nahi naapa. Ollama sirf `eval-router` ke liye hai, embeddings ke liye nahi.
```
uv run alembic upgrade head
uv run python -m app.cli rag-ingest
uv run python -m app.cli rag-search "refund kitne din mein milta hai?" --en "refund time"
uv run python -m app.cli rag-eval
uv run pytest tests\unit\rag
```
Kisi document mein jo cheez nahi, uska sawal poochein: bot ko "not confirmed" kehna chahiye (ABSTAIN), andaza nahi lagana.

## Poora bot terminal mein chalana (Phase 9, phone ke baghair)
`.env` mein `LLM_PROVIDER`, `LLM_API_KEY`, `ROUTER_MODEL` aur `COMPOSER_MODEL` zaroori hain (`TURN_BUDGET_SECONDS` default 45).
```
uv run python -m app.cli chat --debug
```
Ye asli model, database aur dev store se chalta hai, WhatsApp par kuch nahi jata. `/reset` conversation saaf karta hai, `/quit` band.
Pictures milne par composer aur validator ko chhod kar seedha template caption jata hai (colour na mile to caption wahi batata hai).
Tests: `uv run pytest tests\unit\agent tests\integration\test_graph_flow.py tests\integration\test_graph_checkpointer.py tests\integration\test_image_flow.py`

## Shopify sync ko local test karna (Phase 10)
Webhook sirf batata hai ke kuch badla; handler hamesha Shopify se object dobara fetch karta hai, isliye purana event naya data overwrite nahi kar sakta.
`.env` mein `SHOPIFY_WEBHOOK_BASE_URL` (ngrok ka static domain, bina path ke) rakhein.
```
REM tests (Postgres chalna chahiye: docker compose up -d db)
uv run pytest tests\unit\test_sync_webhooks.py tests\unit\test_webhook_admin.py tests\unit\test_reconcile.py tests\unit\test_monitor.py
uv run pytest tests\integration\test_sync_events.py tests\integration\test_sync_handlers.py tests\integration\test_sync_repair.py tests\integration\test_reconcile.py tests\integration\test_monitor.py

REM live: server, tunnel, webhooks register, sync worker (4 alag windows)
uv run uvicorn app.main:app --reload --app-dir src
ngrok http --url=<static domain> 8000
uv run python scripts\register_webhooks.py
uv run python -m app.sync.worker
```
Phir dev store mein product ka title, price ya stock badlein; 10 se 20 second baad mirror mein nazar aana chahiye.

Ghalat ya missed webhook ki marammat (server band tha, tunnel band tha):
```
uv run python -m app.jobs.reconcile --mode delta     REM jo products Shopify ne edit kiye
uv run python -m app.jobs.reconcile --mode full      REM raat ko: sab products, delete hue products yahin milte hain
uv run python -m app.jobs.reconcile --mode policy    REM policies/pages ko knowledge base mein (sirf badla hua embed hota hai)
uv run python -m app.jobs.reconcile --loop           REM teeno apne interval par (development)
```
Sehat ka check (alarm ho to exit code 1): `uv run python -m app.sync.monitor`.
Jo event 5 koshishon ke baad bhi fail ho wo dead letter (`failed`) mein jata hai:
```
uv run python -m app.sync.monitor --list-failed
uv run python -m app.sync.monitor --retry-failed              REM sab wapas pending
uv run python -m app.sync.monitor --retry-failed --event-id 42
```
Known limit: bot ka live stock cache sync worker se saaf nahi hota (dono alag process hain). `LIVE_STOCK_CACHE_SECONDS=0` (default) par cache hai hi nahi, to is ka asar nahi; cache on karein to purani cheez `LIVE_STOCK_CACHE_SECONDS` ke baad khud expire hoti hai.

## Security ko test karna (Phase 11)
Sab kuch bina phone ke chalta hai (asli worker, asli Postgres, FakeLLM). `docker compose up -d db` aur `uv run alembic upgrade head` pehle.
```
uv run pytest tests\adversarial
uv run pytest tests\integration\test_worker_phase11.py tests\integration\test_admin_api.py tests\integration\test_retention_and_erasure.py
```
`tests\adversarial` mein: T19 (private data), T20 (injection, secrets), T21 (zehreela documents), rate limit ka wapas theek hona, handoff aur resume, kill switch, log scan, safe defaults. `evals\adversarial.jsonl` mein 68 hamle aur normal jumle hain.

Kill switch (admin API on ho to; `.env` mein `ADMIN_ENDPOINTS_ENABLED=true` aur 24+ character `ADMIN_API_KEY`):
```
curl -X POST http://localhost:8000/admin/kill-switch -H "X-Admin-Key: KEY" -H "Content-Type: application/json" -d "{\"on\": true}"
```
Handoff ka channel check: `uv run python -m app.handoff.notifier --test`. Ek customer ka data mitana: `uv run python scripts\delete_customer.py --wa-id <number>` (bina `--yes` sirf ginti). Retention roz: `uv run python -m app.jobs.retention --dry-run`.
Go-live se pehle `docs/security_checklist.md` ke sab items aur `docs/threat_model.md` ke known gaps dekhein.

## Evaluation chalana (Phase 12)
Eval ka kaam Ollama par chalta hai (`--ollama`, model rule `docs/PROGRESS.md` mein), app khud Groq par. Final baseline (gate ke liye) Groq par ek baar chalana hai: `--ollama` ke baghair, chhote hisson mein (`--limit`, `--kind`, `--ids`), kyunke free Groq tier rate-limit karta hai.
`.env` mein `LLM_*` aur `COMPOSER_MODEL` chahiye, aur Postgres (`docker compose up -d db`); security set ko database nahi chahiye. Kuch WhatsApp par nahi jata.
```
uv run python scripts\run_eval.py --ollama --delay 1                  REM golden: store ke price/stock/colours, har jawab sahi (gate 100%)
uv run python scripts\run_eval.py --set natural --ollama --delay 1    REM ulte seedhe sawal, ghalat fact ek bhi nahi (poochna theek)
uv run python scripts\run_eval.py --set security --ollama --delay 1   REM evals\adversarial.jsonl: router har hamla pehchane (gate 100%)
uv run python scripts\run_eval.py --set language --ollama --delay 1   REM router aur reply ki zubaan (gate 95%)
uv run python scripts\run_eval.py --set tools --ollama --delay 1      REM sahi tool chala? product, policy, attack (gate 95%, koi attack data tool tak na pahunche)
uv run python scripts\run_eval.py --set retrieval                     REM evals\rag_golden.jsonl: hit rate aur abstention (blueprint: 90% har ek); LLM nahi, Postgres aur local embedding model
uv run python scripts\run_eval.py --set duplicates                    REM T16 + chaos: 0 duplicate reply (worker band; asli worker, fake Meta, koi model nahi)
uv run python scripts\run_eval.py --set latency --limit 30 --delay 25 REM webhook se outbox tak, text p50 8 s / p95 15 s (worker band; Groq par, --ollama mana; free tier ke 429 wale runs gine nahi jaate)
uv run python scripts\run_eval.py --all --ollama --delay 3            REM sab evaluators ek saath, aakhir mein SUMMARY (latency --ollama ke saath SKIPPED)
uv run python scripts\run_eval.py --ollama --ids price-03,price-09    REM sirf kuch sawal
uv run pytest tests\unit\test_language_eval.py tests\unit\test_security_eval.py tests\unit\test_tools_eval.py tests\unit\test_retrieval_eval.py
uv run pytest -m llm tests\llm                                         REM asli model chahiye (lamba: takreeban 20 minute), roz ke pytest mein skip hota hai
```
Ab tak ke nateeje (Ollama, 2026-10-19; Groq par final baseline baqi):

| Evaluator | Nateeja | Gate (blueprint 11.4, proposed) |
|-----------|---------|------|
| Golden product Q&A | 64/65 (price 32/32, stock 24/24, colours 8/9); colors-06 provider error (internet band), ghalat fact nahi | 100%: NOT MET, `--ids colors-06` dobara chalana baqi |
| Natural Q&A | 34/42 sahi, 8 ne poochha, ghalat fact 0 | ghalat fact 0: MET |
| Security | router 46/46, store texts 21/22 (adv-048 known gap) | 100%: MET |
| Language | reply 100% (69/69), router 94.2% (ek run mein 95%+ bhi); 4 ghalatiyan sab `roman_ur` ko `mixed` kehna (ru-04, ru-05, mx-04, mx-06) | 95% dono: router wala hissa abhi NOT MET |
| Tools | 190/191 = 99.5% (attack 46/46, policy 79/80, product 65/65); ek ghalti priv-ur-08 ("WhatsApp chat kitne mahine baad delete hoti hai?": router ne out_of_scope kaha) | 95% aur koi attack data tool tak nahi: MET |
| Retrieval (RAG_MIN_SCORE 0.40) | hit rate 97.5% (en 100%, roman_ur 95%), abstention 14/20 = 70%, MRR 0.899 (sirf report) | hit 90% MET, abstention 90% NOT MET |
| Duplicates (T16 + chaos) | 5 scenarios mein duplicate replies 0, duplicate inbox rows 0, koi reply gum nahi; known gap `meta_accepts_then_crash` (Meta ke accept ke baad worker mare to reply dobara jata hai: 2 duplicates) gate mein nahi ginta | 0 duplicate: MET |
| Latency (text, Groq free tier) | 22 achhe runs / 30: p50 3.5 s, p95 5.7 s, max 6.7 s (+2 s poll wait: 5.5 s / 7.7 s); 8 runs gine nahi (7 Groq 429, 1 provider error); voice abhi nahi (0 clips) | p50 8 s aur p95 15 s: MET |

Retrieval mein abstention ki kami 6 aise sawalon se hai jo documents mein nahi hain magar asli sawalon jaise lagte hain (smart watches, installments, straps): koi threshold unhe alag nahi kar sakta, composer ko abstain karna hoga (Phase 9) ya client ke saath gate badalna hoga (Phase 12, Step 10). Retrieval ke `-m llm` test mein abstention ka hissa isi wajah se xfail hai.

Duplicates ka known gap (`meta_accepts_then_crash`): Meta ke paas idempotency key nahi, is liye ye khidki database rule se band nahi hoti; client ke saath faisla baqi (Step 9): abhi crash par reply dobara jata hai (duplicate, gum jawab se behtar), ya at-most-once (bhejne se pehle "sending" mark, crash par dobara nahi, kabhi jawab gum ho sakta hai).
Latency ka baseline Groq free tier par hai (429 wale runs gine nahi jaate); final baseline paid ya chhote hisson mein dobara chalegi (Step 9).

Sawalon ki file store se dobara banane ke liye (jab prices ya stock badlein): `uv run python scripts\make_product_qa.py`, phir file commit karein. Gate ka aakhri nateeja `docs/eval_report.md` mein hoga (abhi likhi nahi).
Security eval sirf router ka intent dekhta hai, jawab ka text nahi; ye aur `adv-048` ka known gap `docs/threat_model.md` mein likha hai. Language eval ka router wala hissa abhi 95% se neeche hai, gate ko pass dikhane ke liye `mixed` ko `roman_ur` na ginein jab tak owner faisla na kare.

## Qaide
- `.env` kabhi Git ya chat mein na dalein.
- WhatsApp ka temporary token 24 ghante mein expire hota hai (HTTP 401, code 190): naya token
  `.env` mein daal kar worker dobara chalayein.
- Ek waqt mein ek phase. Har phase ke baad `docs/PROGRESS.md` update + git commit + tag.
- Pehle development store use karein, client ka real store baad mein.
