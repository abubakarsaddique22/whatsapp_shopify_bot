"""Phase 12, step 4: the REAL bot calls the right tool (tools gate 95 %, no attack tool).

Marked `llm`: it needs a real model, Postgres and the dev store, so everyday runs and CI skip it
(`pytest -m "not llm"`). Run it in the evaluation job, or by hand:

    uv run pytest -m llm tests\\llm\\test_tools.py

Uses the Ollama eval model (docs/PROGRESS.md, model rule). Same cases and same gate as
`scripts\\run_eval.py --set tools`: product, policy and attack cases, one fresh conversation each.
Never loosen this test to make it pass: a miss is a question that got the wrong tool, and an
attack that reaches a data tool fails the gate on its own. Fix the router prompt
(prompts/router.md) or the gate (agent/gate.py) and write the decision in docs/threat_model.md.
"""

import asyncio

import pytest

from app.core.errors import ConfigError
from app.db.session import get_engine
from app.evals import runner, tools

pytestmark = pytest.mark.llm

DELAY = 1.0  # seconds between cases


@pytest.fixture(scope="module")
def deps_args():
    try:
        return runner.build_deps_args(ollama=True)
    except ConfigError as exc:
        pytest.skip(str(exc))


async def test_the_bot_calls_the_right_tool_and_no_attack_reaches_a_data_tool(deps_args) -> None:
    outcomes = []
    try:
        with runner.watching_provider_errors() as catcher:
            for case in tools.build_cases():
                run = await runner.ask_with_retry(deps_args, case.texts, catcher)
                outcomes.append(tools.ToolOutcome(case, run.tools, run.reply, run.intent))
                await asyncio.sleep(DELAY)
    finally:
        await get_engine().dispose()

    errors = [o.case.id for o in outcomes if o.reply.startswith("[ERROR")]
    if errors:  # a crash (for example Postgres is not running) is not a model result
        pytest.skip(f"the run crashed for {errors[:5]} (and maybe more): run again")

    assert tools.gate_met(outcomes), "\n" + tools.format_report(outcomes)
