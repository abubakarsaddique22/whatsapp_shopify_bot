"""Phase 9, step 1: agent/state.py (the state of one graph turn). No database, no network."""

from typing import get_args

from app.agent import state
from app.agent.state import BRANCHES, Branch, GraphState, new_turn
from app.domain.models import ConversationState, Lang


def make_conversation() -> ConversationState:
    return ConversationState(conversation_id="c1", sender_hash="h1")


def test_new_turn_sets_input_and_clean_defaults():
    conv = make_conversation()
    turn = new_turn(7, "GA-2100 ki price kya hai", conv, correlation_id="abc")

    assert turn["conversation_id"] == 7
    assert turn["text"] == "GA-2100 ki price kya hai"
    assert turn["source"] == "text"
    assert turn["correlation_id"] == "abc"
    assert turn["conversation"] is conv
    assert turn["router"] is None
    assert turn["branch"] is None
    assert turn["response"] is None
    assert turn["tool_results"] == []
    assert turn["tool_calls_made"] == 0
    assert turn["compose_attempts"] == 0
    assert turn["validation_errors"] == []
    assert turn["used_fallback"] is False
    assert turn["security_event"] is None


def test_a_voice_transcript_keeps_its_source_flag():
    turn = new_turn(1, "price kya hai", make_conversation(), source="voice")
    assert turn["source"] == "voice"


def test_every_turn_gets_its_own_lists():
    first = new_turn(1, "a", make_conversation())
    second = new_turn(2, "b", make_conversation())
    first["tool_results"].append("x")  # type: ignore[arg-type]
    first["validation_errors"].append("y")
    assert second["tool_results"] == []
    assert second["validation_errors"] == []


def test_new_turn_sets_every_per_turn_field():
    """A field added to GraphState but forgotten in new_turn would leak between turns."""
    turn = new_turn(1, "hi", make_conversation())
    assert set(turn) == set(GraphState.__annotations__)


def test_branches_match_the_branch_type():
    assert set(get_args(Branch)) == set(BRANCHES)
    assert len(BRANCHES) == len(set(BRANCHES))


def test_figure_3_branches_are_all_present():
    assert set(BRANCHES) == {
        "product_info",
        "image_request",
        "policy_rag",
        "clarify",
        "handoff",
        "refuse",
        "compose",
    }


def test_caps_are_small_positive_numbers():
    assert state.MAX_COMPOSE_RETRIES == 1  # blueprint 8.7: one retry, then safe fallback
    assert state.MAX_CLARIFICATIONS == 2
    assert state.HANDOFF_AFTER_CLARIFICATIONS == state.MAX_CLARIFICATIONS + 1
    assert 1 <= state.MAX_TOOL_CALLS_PER_TURN <= 10
    assert state.MAX_INPUT_CHARS > 0
    assert state.HISTORY_WINDOW > 0


def test_the_conversation_state_default_language_is_unknown():
    assert make_conversation().language is Lang.UNKNOWN
