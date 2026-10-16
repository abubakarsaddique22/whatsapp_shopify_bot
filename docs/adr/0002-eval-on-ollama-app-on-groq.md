# ADR 0002: Eval on Ollama, app on Groq
Date: 2026-10-15  Status: accepted

Context: Student budget. Groq rate limits (429) spoiled eval baselines; the eval is a batch job.
Decision: All eval/judge work uses Ollama (`get_eval_chat_model`). Router, composer and STT in
the app use Groq (OpenAI-compatible API, `openai/gpt-oss-120b`, Whisper large-v3 / turbo).
Alternatives: Groq for everything (rate limits, cost); OpenAI later (needs a new ADR).
Consequences: Eval is free and retry-friendly. Eval results on Ollama may differ slightly from
Groq, so the golden set is run once on Groq before go-live and after model/prompt changes.
