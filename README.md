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
| 5 | Language detection, router, reply-language policy, debug reply | Done (router language match 92.8%, target 95%) |
| 6 | Voice: fake STT, Groq Whisper adapter, media download + ffmpeg, voice gate, `/dev/simulate` audio, bake-off script | Steps 1 se 5 done (tests pass). Step 6 (bake-off, live voice-note check) phone aur token ka intezar |
| 7 | Product images: image resolver, JPEG conversion (AVIF/WebP), WhatsApp image bhejna (link ya upload), image flow (`image_request`), text-link fallback | Steps 1 se 5 done (T05, T23, T24 pass, fakes ke saath). Asli phone par images ka live check phone aur token ka intezar |
| 8 | RAG: policies/FAQ ko chunk karna, local embeddings (sentence-transformers, `paraphrase-multilingual-MiniLM-L12-v2`; code ka default `BAAI/bge-m3` hai, uska score abhi nahi naapa), pgvector index, hybrid retrieval, `rag-ingest` / `rag-search` / `rag-eval` | Done, tag phase-8-done (baseline 2026-10-16: hit rate 97.5%, abstention 14/20). Real store par `rag-ingest` / `rag-search` ka manual check baqi |
| 9 | LangGraph workflow: state, permission gate, tools, clarify/refuse/handoff templates, composer, validator, graph + Postgres checkpointer, worker, `app.cli chat` | Done, tag phase-9-done (full pytest/ruff/mypy pass, asli model se 12 messages ka check 2026-10-17). Phone par 10-turn check aur LangSmith traces phone ka intezar |
| 10 | Shopify sync: webhook endpoint (HMAC), webhook registration, sync worker (handlers), selective re-embedding, reconciliation jobs (delta / full / policy), monitor + alarms + dead letter | Done (tests pass, 2026-10-17: T16, T17, T18, out-of-order, retry, missed-event repair). Commit + tag phase-10-done haath se |
| 11 | Security, privacy, handoff: card redaction, injection scan (English, Roman Urdu, Urdu), rate limits + daily budget, handoff (pause, resume, notifier), kill switch, admin API (default band), retention + `delete_customer.py`, adversarial tests T19 se T21 | Steps 1 se 9 done. Step 10 (adversarial tests, `docs/threat_model.md`, `docs/security_checklist.md`) likha gaya, tests chalane baqi; phir commit + tag phase-11-done haath se |
| 12 se 14 | evals, AWS, acceptance | Phase 12 shuru (Steps 1 se 3), baqi nahi |

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

## Qaide
- `.env` kabhi Git ya chat mein na dalein.
- WhatsApp ka temporary token 24 ghante mein expire hota hai (HTTP 401, code 190): naya token
  `.env` mein daal kar worker dobara chalayein.
- Ek waqt mein ek phase. Har phase ke baad `docs/PROGRESS.md` update + git commit + tag.
- Pehle development store use karein, client ka real store baad mein.
