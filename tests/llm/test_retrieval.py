"""Phase 12, step 4d: real retrieval on the golden questions (evals/rag_golden.jsonl).

Marked `llm` like the other evaluation tests, so everyday runs and CI skip it
(`pytest -m "not llm"`). It uses no LLM, but it needs Postgres (docker compose up -d) and the local
embedding model, which is slow to load and must not slow down every test run.

    uv run pytest -m llm tests\\llm\\test_retrieval.py

The sample documents are loaded in a transaction that is rolled back; the real knowledge base is
not touched. Blueprint 11.4 proposes hit rate AND abstention at 90 % each:

* hit rate: a HARD gate. A miss is a policy question the bot cannot answer. Fix chunking, the
  embedding model or RAG_MIN_SCORE (python -m app.cli rag-eval shows the sweep) and write the
  decision in docs/PROGRESS.md. Never loosen the target to make this pass.
* abstention: the baseline is 70 % because six unanswerable questions look like real ones
  ('smart watches', 'installments', 'watch straps') and no score threshold separates them. Below
  the target this part is an expected failure (xfail) and shows the number, until the composer
  abstains for them (Phase 9) or the client revises the gate (Phase 12, step 10). Delete the
  xfail below the day the abstention gate is decided.
"""

import pytest

from app.core.errors import ConfigError
from app.db.session import get_engine
from app.evals import retrieval
from app.integrations.embeddings.factory import get_eval_embedder

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def embedder():
    try:
        return get_eval_embedder()
    except ConfigError as exc:
        pytest.skip(str(exc))


async def test_retrieval_meets_the_hit_rate_target_and_reports_abstention(embedder) -> None:
    try:
        results = await retrieval.run(embedder)
    except OSError as exc:  # Postgres is not running: not a retrieval result
        pytest.skip(f"database not reachable: {exc}")
    finally:
        await get_engine().dispose()

    report = "\n" + retrieval.format_report(results)
    assert retrieval.hit_gate_met(results), report
    if not retrieval.abstention_gate_met(results):
        pytest.xfail("abstention below the blueprint target (see the docstring)" + report)
