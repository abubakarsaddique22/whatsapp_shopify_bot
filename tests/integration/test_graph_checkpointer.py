"""Phase 9: the graph with the REAL LangGraph Postgres checkpointer (Blueprint Phase 9, step 9).

Troubleshooting entry of the blueprint: "State not persisted (checkpointer setup)". This test
proves two things on the real database:

1. After a turn the checkpointer holds the final state of the conversation thread (the graph
   ended, nothing is waiting, the reply is in the saved state).
2. A second turn on the SAME thread starts clean: tool results and call counts of the first turn
   do not leak into it (new_turn() sets every per-turn field).

Everything else is fake (tools, model), like in test_graph_flow.py, and repo.save_state is
replaced, so only the checkpoint tables are written. The thread is deleted at the end.
Needs: docker compose up -d db (the checkpointer creates its own tables on the first run).

The test is a plain (sync) function on purpose: on Windows psycopg async cannot run on the
default Proactor event loop, so it starts its own loop after use_selector_event_loop().
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import nodes
from app.agent.graph import build_graph, open_checkpointer, run_turn, use_selector_event_loop
from app.agent.nodes import Deps
from app.agent.tools import TOOL_PRICE, TOOL_SEARCH, TOOLS
from app.core.settings import Settings
from app.db import repositories as repo
from app.domain.models import ConversationState, ToolResult
from app.integrations.llm.fake_llm import FakeLLM

CONVERSATION_ID = 987_654_321  # far away from real conversation ids
THREAD = {"configurable": {"thread_id": f"conversation-{CONVERSATION_ID}"}}
PID = "gid://shopify/Product/9401"
VID = "gid://shopify/ProductVariant/9401001"
PRICE_TEXT = "Ji, Qorvex Reef ki qeemat Rs 9,000 hai."
GREETING_TEXT = "Assalam o alaikum! Aap ko kaunsi watch ki maloomat chahiye?"


def tool_result(tool: str, data: dict[str, Any]) -> ToolResult:
    return ToolResult(
        tool=tool, ok=True, data=data, source_id=f"{tool}:test", fetched_at=datetime.now(UTC)
    )


def router_says(intent: str, **fields: Any) -> str:
    return json.dumps({"language": "roman_ur", "intent": intent, "confidence": 0.9, **fields})


def composer_says(text: str) -> str:
    return json.dumps({"detected_language": "roman_ur", "intent": "price", "response_text": text})


def reply_text(response: Any) -> str:
    """The saved reply is a BotResponse, or a plain dict if the saver hands back dicts."""
    if hasattr(response, "response_text"):
        return response.response_text
    return response["response_text"]


def test_the_turn_is_saved_in_the_postgres_checkpointer_and_the_next_turn_starts_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(session: Any, **kwargs: Any) -> ToolResult:
        candidate = {
            "product_id": PID,
            "title": "Qorvex Reef",
            "colours": [],
            "colour_verified": True,
        }
        return tool_result(TOOL_SEARCH, {"kind": "one", "colour": None, "candidates": [candidate]})

    async def fake_price(session: Any, **kwargs: Any) -> ToolResult:
        price = {"amount": "9000", "currency": "PKR", "display": "Rs 9,000"}
        return tool_result(
            TOOL_PRICE,
            {
                "match": "one",
                "product_id": PID,
                "title": "Qorvex Reef",
                "variant_id": VID,
                "variant_title": "Black / 40mm",
                "colour_unverified": False,
                "price": price,
            },
        )

    async def fake_save(session: Any, conversation_id: int, conversation: ConversationState):
        return None

    async def fake_basics(session: Any, product_id: str):
        return SimpleNamespace(title="Qorvex Reef")

    monkeypatch.setitem(TOOLS, TOOL_SEARCH, fake_search)
    monkeypatch.setitem(TOOLS, TOOL_PRICE, fake_price)
    monkeypatch.setattr(repo, "save_state", fake_save)
    monkeypatch.setattr(repo, "product_basics", fake_basics)
    settings = Settings(_env_file=None, shopify_shop_domain="shop.example.com")  # type: ignore[call-arg]
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)

    async def scenario() -> tuple[Any, Any, Any]:
        conversation = ConversationState(conversation_id="c1", sender_hash="h1")
        async with open_checkpointer() as saver:
            try:
                first_llm = FakeLLM(
                    [router_says("price", product_query="Qorvex Reef"), composer_says(PRICE_TEXT)]
                )
                first_graph = build_graph(Deps(session=None, llm=first_llm), saver)  # type: ignore[arg-type]
                first = await run_turn(
                    first_graph,
                    CONVERSATION_ID,
                    "Qorvex Reef ki price?",
                    conversation,
                    correlation_id="checkpoint-turn-1",
                )
                snapshot = await first_graph.aget_state(THREAD)

                second_llm = FakeLLM([router_says("greeting"), composer_says(GREETING_TEXT)])
                second_graph = build_graph(Deps(session=None, llm=second_llm), saver)  # type: ignore[arg-type]
                second = await run_turn(
                    second_graph,
                    CONVERSATION_ID,
                    "salam",
                    first["conversation"],
                    correlation_id="checkpoint-turn-2",
                )
                return first, snapshot, second
            finally:
                delete_thread = getattr(saver, "adelete_thread", None)
                if delete_thread is not None:
                    await delete_thread(THREAD["configurable"]["thread_id"])

    previous_policy = asyncio.get_event_loop_policy()
    use_selector_event_loop()  # Windows: psycopg needs the selector loop
    try:
        first, snapshot, second = asyncio.run(scenario())
    finally:
        asyncio.set_event_loop_policy(previous_policy)

    # the first turn ran the real flow and ended with the validated reply
    assert first["tool_calls_made"] == 2
    assert reply_text(first["response"]) == PRICE_TEXT
    assert first["used_fallback"] is False

    # the checkpointer kept the final state of that thread: finished, with the reply inside
    assert snapshot.next == ()
    assert reply_text(snapshot.values["response"]) == PRICE_TEXT

    # the second turn on the same thread did not inherit anything from the first
    assert second["tool_calls_made"] == 0
    assert second["tool_results"] == []
    assert second["compose_attempts"] == 1
    assert reply_text(second["response"]) == GREETING_TEXT
