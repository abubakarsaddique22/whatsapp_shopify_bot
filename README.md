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
| 8 | RAG: policies/FAQ ko chunk karna, local embeddings (sentence-transformers, `BAAI/bge-m3`), pgvector index, hybrid retrieval, `rag-ingest` / `rag-search` / `rag-eval` | Steps 1 se 7 bane (tests pass). Step 8 (baseline, commit + tag) baqi |
| 9 se 14 | LangGraph, sync, security, evals, AWS, acceptance | Shuru nahi hue |

## Code ka naqsha (`src/app/`)

| Folder / file | Kaam |
|---------------|------|
| `core/` | Settings (`.env`), JSON logging aur redaction, errors, ids, signature checks |
| `db/` | Async session, tables, repositories (mirror, inbox/outbox, conversation state) |
| `domain/` | Pydantic models: Lang, Intent, RouterOutput, BotResponse, ConversationState |
| `integrations/shopify/` | Read-only Shopify GraphQL gateway |
| `integrations/whatsapp/` | Meta payloads, client (text aur image), sender (retry rules, image fallback), media download, `media_upload.py` (image upload) |
| `integrations/llm/` | Chat model factory (Groq), FakeLLM tests ke liye |
| `integrations/embeddings/` | Embeddings: FakeEmbedder (tests), SentenceTransformerEmbedder (local `BAAI/bge-m3`, koi key nahi), factory |
| `rag/` | `clean`, `chunk`, `index` (sirf badla hua content dobara embed), `ingest`, `retrieve` (hybrid + threshold), `rag_eval` |
| `integrations/speech/` | Speech-to-text: FakeSTT, Groq Whisper adapter, factory |
| `voice/` | Voice gate (confidence, limits, hallucination check), ffmpeg convert, pipeline, scoring (WER/CER) |
| `catalog/` | Normalizer, search (pg_trgm), price, stock, specs, images, `image_formats` (JPEG rendition), importer |
| `language/` | Roman Urdu lexicon, detection, price parser, language policy (table 6.1) |
| `agent/` | `router.py` (intent + language), `language_step.py`, `image_flow.py` (image_request ka jawab), `tools.py`, `rules.py`, `router_eval.py` |
| `api/` | `/healthz`, `/readyz`, WhatsApp webhook, `/dev/*` (sirf local) |
| `worker.py` | Inbound message uthata hai, jawab outbox mein daalta hai, bhejta hai |
| `cli.py` | `ask`, `import-catalog`, `eval-router`, `rag-ingest`, `rag-search`, `rag-eval` |
| `scripts/` | `bakeoff.py` (speech provider ka muqabla), `debug_products.py` |
| `prompts/`, `config/`, `evals/` | Router aur composer prompts, lexicon aur mappings, golden sets |

Khali (baad ke phases ke liye): `sync/`, `security/`, `handoff/`, `jobs/`,
`agent/graph.py`, `agent/composer.py`.

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
`.env` mein `EMBEDDING_PROVIDER=sentence-transformers` aur `EMBEDDING_MODEL=BAAI/bge-m3` rakhein. Model local chalta hai,
koi key nahi chahiye; pehli baar takreeban 2.2 GB download hota hai. Ollama sirf `eval-router` ke liye hai, embeddings ke liye nahi.
```
uv run alembic upgrade head
uv run python -m app.cli rag-ingest
uv run python -m app.cli rag-search "refund kitne din mein milta hai?" --en "refund time"
uv run python -m app.cli rag-eval
uv run pytest tests\unit\rag
```
Kisi document mein jo cheez nahi, uska sawal poochein: bot ko "not confirmed" kehna chahiye (ABSTAIN), andaza nahi lagana.

## Qaide
- `.env` kabhi Git ya chat mein na dalein.
- WhatsApp ka temporary token 24 ghante mein expire hota hai (HTTP 401, code 190): naya token
  `.env` mein daal kar worker dobara chalayein.
- Ek waqt mein ek phase. Har phase ke baad `docs/PROGRESS.md` update + git commit + tag.
- Pehle development store use karein, client ka real store baad mein.
