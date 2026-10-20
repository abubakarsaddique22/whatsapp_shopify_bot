"""The checkpoint serializer reads our own types back without a LangGraph warning.

LangGraph warns today, and will refuse in a later version, when a checkpoint holds a type that
is not on its allow list. CHECKPOINT_TYPES is that list; these tests keep it complete.
"""

import enum
import inspect
import logging
from datetime import UTC, datetime

from pydantic import BaseModel

from app.agent.graph import CHECKPOINT_TYPES, checkpoint_serializer, turn_summary
from app.agent.state import new_turn
from app.domain import models
from app.domain.models import (
    BotResponse,
    ConversationState,
    ImageSelection,
    Intent,
    Lang,
    RouterOutput,
    ToolResult,
)

SERDE_LOGGER = "langgraph.checkpoint.serde"


def _router() -> RouterOutput:
    return RouterOutput(
        language=Lang.UR, intent=Intent.PRICE, product_query="Rolex Submariner", confidence=0.9
    )


def _search_result() -> ToolResult:
    return ToolResult(
        tool="search_products",
        ok=True,
        data={"kind": "one", "candidates": [{"title": "Rolex Submariner, 16610"}]},
        source_id="tool:1",
        fetched_at=datetime.now(UTC),
    )


def _full_state() -> dict:
    conversation = ConversationState(
        conversation_id="7",
        sender_hash="abc",
        language=Lang.ROMAN_UR,
        last_router_output=_router(),
        tool_results=[_search_result()],
    )
    response = BotResponse(
        detected_language=Lang.UR,
        intent=Intent.PRICE,
        response_text="Ji, Rolex Submariner ki price Rs 12,999 hai.",
        image=ImageSelection(product_id="gid://shopify/Product/1", image_url="https://x/y.jpg"),
    )
    return {
        "conversation": conversation,
        "router": _router(),
        "tool_results": [_search_result()],
        "response": response,
        "language": Lang.EN,
        "intent": Intent.FAQ,
    }


def test_every_model_class_of_the_domain_is_on_the_checkpoint_list() -> None:
    ours = {
        cls
        for _, cls in inspect.getmembers(models, inspect.isclass)
        if cls.__module__ == models.__name__ and issubclass(cls, BaseModel | enum.Enum)
    }
    assert ours <= set(CHECKPOINT_TYPES), f"add to CHECKPOINT_TYPES: {ours - set(CHECKPOINT_TYPES)}"


def test_a_checkpoint_with_our_types_is_read_back_without_a_warning(caplog) -> None:
    serde = checkpoint_serializer()
    state = _full_state()
    with caplog.at_level(logging.WARNING, logger=SERDE_LOGGER):
        back = serde.loads_typed(serde.dumps_typed(state))
    assert back == state
    assert isinstance(back["conversation"].last_router_output, RouterOutput)
    assert not [r for r in caplog.records if r.name.startswith(SERDE_LOGGER)]


def test_the_default_serializer_still_warns_so_the_test_above_means_something(caplog) -> None:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    with caplog.at_level(logging.WARNING, logger=SERDE_LOGGER):
        serde.loads_typed(serde.dumps_typed({"language": Lang.EN}))
    assert any("unregistered type" in r.getMessage() for r in caplog.records)


def test_the_turn_summary_has_the_route_and_the_tools_but_no_customer_words() -> None:
    conversation = ConversationState(conversation_id="7", sender_hash="abc")
    state = new_turn(7, "رولک سب مرینر کی پرائز کیا ہے؟", conversation, source="voice")
    state.update(
        router=_router(),
        branch="product_info",
        tool_results=[_search_result()],
        tool_errors=["shopify_unavailable"],
        compose_attempts=1,
        used_fallback=True,
    )
    summary = turn_summary(state)
    assert summary["source"] == "voice"
    assert summary["intent"] == "price"
    assert summary["language"] == "ur"
    assert summary["branch"] == "product_info"
    assert summary["tools"] == ["search_products"]
    assert summary["tool_errors"] == ["shopify_unavailable"]
    assert summary["search_kind"] == "one"
    assert summary["search_found"] == 1
    assert summary["used_fallback"] is True
    flat = repr(summary)
    assert "Rolex" not in flat and "Submariner" not in flat and "مرینر" not in flat


def test_the_turn_summary_of_a_turn_without_a_router_result() -> None:
    conversation = ConversationState(conversation_id="7", sender_hash="abc")
    summary = turn_summary(new_turn(7, "hi", conversation))
    assert summary["intent"] is None
    assert summary["tools"] == []
    assert summary["used_fallback"] is False
    assert "search_kind" not in summary
