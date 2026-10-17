"""Phase 9: the time budget of one message (Blueprint 3.7 limits). Pure functions, no database.

When the budget of a turn is used up, no NEW composer call starts: compose_node returns no reply
(so the graph sends the safe templated reply plus a handoff) and a validator rejection is not
retried. A model call that is already running is bounded by LLM_TIMEOUT_SECONDS, not by this.
"""

from app.agent.nodes import ERR_BUDGET, Deps, compose_node, route_after_validate
from app.agent.state import new_turn, over_budget
from app.domain.models import BotResponse, ConversationState, Intent, Lang
from app.integrations.llm.fake_llm import FakeLLM


def conv() -> ConversationState:
    return ConversationState(conversation_id="c1", sender_hash="h1")


def test_a_turn_without_a_budget_is_never_over() -> None:
    assert over_budget(new_turn(1, "x", conv())) is False


def test_a_budget_of_zero_is_over_at_once() -> None:
    assert over_budget(new_turn(1, "x", conv(), budget_seconds=0)) is True


def test_a_long_budget_is_not_over() -> None:
    assert over_budget(new_turn(1, "x", conv(), budget_seconds=60)) is False


def test_the_deadline_is_always_set_so_nothing_is_left_over_from_an_earlier_turn() -> None:
    assert "deadline" in new_turn(1, "x", conv())
    assert "deadline" in new_turn(1, "x", conv(), budget_seconds=5)


async def test_compose_node_makes_no_model_call_when_the_time_is_used_up() -> None:
    llm = FakeLLM([])
    state = new_turn(1, "price?", conv(), budget_seconds=0)

    update = await compose_node(state, Deps(session=None, llm=llm))  # type: ignore[arg-type]

    assert update["response"] is None  # the graph then sends safe_fallback
    assert update["compose_attempts"] == 1
    assert ERR_BUDGET in update["tool_errors"]
    assert llm.calls == []


def rejected_once(budget_seconds: float):
    """A turn whose first composed reply was rejected by the validator."""
    state = new_turn(1, "price?", conv(), budget_seconds=budget_seconds)
    state["response"] = BotResponse(
        detected_language=Lang.EN, intent=Intent.PRICE, response_text="The price is Rs 1."
    )
    state["validation_errors"] = ["price: 1 is not a price from the price tool"]
    state["compose_attempts"] = 1
    return state


def test_a_rejected_reply_is_retried_when_there_is_time() -> None:
    assert route_after_validate(rejected_once(60)) == "retry"


def test_a_rejected_reply_is_not_retried_when_the_time_is_used_up() -> None:
    assert route_after_validate(rejected_once(0)) == "safe_fallback"
