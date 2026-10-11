# WhatsApp + Shopify AI Support Bot (read-only)

Chhota, safe bot: customer WhatsApp par text ya voice bhejta hai, bot Shopify ke live data
ya approved documents se jawab deta hai. Ye kuch change nahi kar sakta (read-only).

Design: `Shopify_WhatsApp_AI_Support_Bot_Master_Blueprint_v2.pdf` (Blueprint v2.0).

## Is zip mein kya hai
Phase 1 ka mukammal, tested skeleton. Jo code kabhi change nahi hota wo pehle se likha hua hai:

| File | Kaam |
|------|------|
| `src/app/core/settings.py` | .env se typed settings, secrets `SecretStr` mein |
| `src/app/core/logging.py` | JSON logs, secrets/phone redaction, correlation id |
| `src/app/core/errors.py` | Saari custom exceptions + FastAPI handlers |
| `src/app/core/ids.py` | Correlation id, phone hash |
| `src/app/core/security.py` | WhatsApp/Shopify signature check, Shopify scope audit |
| `src/app/db/` | Async DB session, `Base` for migrations |
| `src/app/api/health.py` | `/healthz` aur `/readyz` |
| `src/app/domain/models.py` | Pydantic models: Lang, Intent, RouterOutput, BotResponse, ToolResult, ConversationState |
| `src/app/main.py` | FastAPI app + correlation middleware |
| `prompts/`, `config/` | Router/composer prompts, Roman Urdu lexicon, mappings |

Baqi folders (`integrations/`, `catalog/`, `rag/`, `agent/` ...) abhi khali hain. Inhe
phase by phase bharenge (Phase 2 se).

## Windows (cmd) mein chalane ke steps
```
REM 1) uv install (ek baar)
winget install --id=astral-sh.uv -e

REM 2) project folder mein
uv sync
copy .env.example .env

REM 3) database
docker compose up -d db

REM 4) tests
uv run pytest

REM 5) server
uv run uvicorn app.main:app --reload --app-dir src
```
Phir browser mein kholein: http://localhost:8000/docs aur http://localhost:8000/healthz

VS Code: folder open karein, extensions install karein (popup aayega), `Ctrl+Shift+P` ->
"Python: Select Interpreter" -> `.venv`.

## Qaide
- `.env` kabhi Git ya chat mein na dalein.
- Ek waqt mein ek phase. Har phase ke baad `docs/PROGRESS.md` update + git commit.
- Pehle development store use karein, client ka real store baad mein.
