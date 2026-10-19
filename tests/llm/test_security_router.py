"""Phase 12, step 4: the REAL router against evals/adversarial.jsonl (security gate 100 %).

Marked `llm`: it needs a real model, so everyday runs and CI skip it (`pytest -m "not llm"`).
Run it in the evaluation job, or by hand:

    uv run pytest -m llm tests\\llm\\test_security_router.py

Uses the Ollama eval model (docs/PROGRESS.md, model rule). Never loosen this test to make it pass:
a miss is an attack the router does not recognise. Fix the router prompt (prompts/router.md) or
add the case to the pipeline tests, and write the decision in docs/threat_model.md.
"""

import pytest

from app.core.errors import ConfigError
from app.evals import security
from app.integrations.llm.chat_factory import get_eval_chat_model

pytestmark = pytest.mark.llm

ROWS = security.load_rows()


@pytest.fixture(scope="module")
def llm():
    try:
        return get_eval_chat_model()
    except ConfigError as exc:
        pytest.skip(str(exc))


async def test_the_real_router_recognises_every_attack(llm) -> None:
    customers = security.customer_rows(ROWS)

    results = await security.run_router(customers, llm, delay=1.0)

    errors = [r.row.id for r in results if r.fell_back and not r.ok]
    if errors:  # a provider failure is not a model result: do not call it a pass or a miss
        pytest.skip(f"provider failed for {errors}: run again")
    wrong = [f"{r.row.id}: {r.got.intent.value}" for r in results if r.wrong]
    assert wrong == [], "\n" + security.format_report(results, [])


def test_the_scanner_flags_every_store_text_it_should() -> None:
    docs = security.scan_documents(security.document_rows(ROWS))

    assert security.gate_met([], docs), security.format_report([], docs)
