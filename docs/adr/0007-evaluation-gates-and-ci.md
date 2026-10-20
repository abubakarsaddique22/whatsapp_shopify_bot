# ADR 0007: Evaluation gates and CI
Date: 2026-10-20  Status: accepted

Context: Phase 12 needs repeatable gates (Blueprint 11.4, 11.6). Groq's free tier rate-limits
long runs, a paid Groq plan is not there yet, and the GitHub runner has neither Ollama nor the
real Shopify store.
Decision:
- Every push runs `ci.yml`, with the checks that need no LLM: ruff, mypy, pytest
  -m "not llm and not live", the 5 failure drills, the T01-T25 map check, pip-audit, and a
  gitleaks secret scan. No step there may continue on error.
- `eval.yml` (Groq security gate plus a retrieval report) runs only by hand until the paid Groq
  plan exists. The nightly schedule was removed so that it does not fail every night without
  secrets.
- Full evaluation runs (`run_eval.py --all`) happen on the developer PC. Ollama results find
  bugs; the release gate counts only on Groq (ADR 0002). Results are kept in `evals/results/`
  and summarised in `docs/eval_report.md`.
- Eval sets and runs can be copied to LangSmith (datasets and experiments), always masked with
  the log `redact()`. Big eval runs use LANGSMITH_TRACING=false: on 2026-10-20 tracing every
  eval question used up the free monthly trace quota.
Alternatives: run the LLM sets in CI on Groq now (fails on rate limits and needs a paid key);
make retrieval abstention a hard CI gate (it is not met, and the client has not decided yet).
Consequences: CI stays fast and free, and stays green unless code really breaks. The LLM gates
depend on someone running them before a release; docs/eval_report.md shows when they last ran.
