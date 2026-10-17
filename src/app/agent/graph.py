"""Phase 9, step 7: the bounded LangGraph graph of Blueprint Figure 3.

    graph = build_graph(deps, checkpointer)                   # compile once per worker
    state = await run_turn(graph, conversation_id, text, conversation,
                           source="text", correlation_id=cid)
    replies = to_replies(state)                                # what the outbox gets

Shape (no cycle except the ONE composer retry, so no loop is possible):

    normalize_input -> detect_language -> route_intent -> permission_gate -> <branch>
    product_info | image_request | policy_rag -> (clarify | pictures | compose)
    clarify | handoff | refuse | pictures -> finish
    compose -> validate -> done: finish | retry: compose (once) | safe_fallback -> finish
    finish saves the ConversationState once and ends the turn.

Checkpointer: open_checkpointer() gives the LangGraph Postgres saver (thread = the conversation).
It keeps the state of every step of every turn, which is what the traces and a crash look at. The
customer's conversation (language, current product, pause) is still the `conversations.state`
row that finish_node saves, so a lost checkpoint never loses the conversation.

LangSmith: the trace id of a turn IS derived from the correlation id (turn_config), and the
correlation id is also in the trace metadata, so a log line and a trace find each other. Tracing is
switched on by LANGSMITH_TRACING in .env; no phone number goes into the metadata.
"""

import asyncio
import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from app.agent.image_flow import Reply
from app.agent.nodes import (
    Deps,
    clarify_node,
    compose_node,
    detect_language_node,
    gate_node,
    handoff_node,
    image_request_node,
    normalize_input_node,
    pictures_node,
    policy_rag_node,
    product_info_node,
    refuse_node,
    route_after_tools,
    route_after_validate,
    route_intent_node,
    safe_fallback_node,
    validate_node,
)
from app.agent.state import BRANCHES, HISTORY_WINDOW, GraphState, Source, new_turn
from app.agent.templates import render
from app.agent.tools import TOOL_IMAGE
from app.core.settings import get_settings
from app.db import repositories as repo
from app.domain.models import ConversationState, Intent
from app.language.policy import ReplyLang

log = logging.getLogger(__name__)

MAX_GRAPH_STEPS = 25  # LangGraph recursion limit: a safety net on top of the caps in state.py
MAX_HISTORY_TEXT = 300  # characters kept per history line
TRACE_NAMESPACE = "wa-shopify-bot"


# ---- the graph


def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver | None = None) -> Any:
    """Compile the graph. `deps` (session, models) is bound into the nodes that need it."""

    async def route_intent(state: GraphState) -> dict[str, Any]:
        return await route_intent_node(state, deps)

    async def product_info(state: GraphState) -> dict[str, Any]:
        return await product_info_node(state, deps)

    async def image_request(state: GraphState) -> dict[str, Any]:
        return await image_request_node(state, deps)

    async def policy_rag(state: GraphState) -> dict[str, Any]:
        return await policy_rag_node(state, deps)

    async def compose(state: GraphState) -> dict[str, Any]:
        return await compose_node(state, deps)

    async def handoff(state: GraphState) -> dict[str, Any]:
        return await handoff_node(state)

    async def finish(state: GraphState) -> dict[str, Any]:
        return await finish_node(state, deps)

    graph = StateGraph(GraphState)
    graph.add_node("normalize_input", normalize_input_node)
    graph.add_node("detect_language", detect_language_node)
    graph.add_node("route_intent", route_intent)
    graph.add_node("permission_gate", gate_node)
    graph.add_node("product_info", product_info)
    graph.add_node("image_request", image_request)
    graph.add_node("policy_rag", policy_rag)
    graph.add_node("clarify", clarify_node)
    graph.add_node("handoff", handoff)
    graph.add_node("refuse", refuse_node)
    graph.add_node("pictures", pictures_node)
    graph.add_node("compose", compose)
    graph.add_node("validate", validate_node)
    graph.add_node("safe_fallback", safe_fallback_node)
    graph.add_node("finish", finish)

    graph.add_edge(START, "normalize_input")
    graph.add_edge("normalize_input", "detect_language")
    graph.add_edge("detect_language", "route_intent")
    graph.add_edge("route_intent", "permission_gate")
    graph.add_conditional_edges(
        "permission_gate", lambda s: s.get("branch") or "clarify", {b: b for b in BRANCHES}
    )
    for tool_node in ("product_info", "image_request", "policy_rag"):
        graph.add_conditional_edges(
            tool_node,
            route_after_tools,
            {"clarify": "clarify", "pictures": "pictures", "compose": "compose"},
        )
    graph.add_edge("compose", "validate")
    graph.add_conditional_edges(
        "validate",
        route_after_validate,
        {"done": "finish", "retry": "compose", "safe_fallback": "safe_fallback"},
    )
    for templated in ("clarify", "handoff", "refuse", "safe_fallback", "pictures"):
        graph.add_edge(templated, "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


async def finish_node(state: GraphState, deps: Deps) -> dict[str, Any]:
    """The last node: update the conversation and save it ONCE (the caller commits).

    A reply that is not a clarification ends the 'questions in a row' count. The history keeps
    the last HISTORY_WINDOW exchanges for the composer. Tool results are not stored.
    """
    conv = state["conversation"].model_copy(deep=True)
    response = state.get("response")
    if response is not None:
        if not response.needs_clarification and not response.needs_handoff:
            conv.clarification_count = 0
        lines = [
            {"role": "customer", "text": state.get("text", "")[:MAX_HISTORY_TEXT]},
            {"role": "bot", "text": response.response_text[:MAX_HISTORY_TEXT]},
        ]
        conv.history = [*conv.history, *lines][-2 * HISTORY_WINDOW :]
    conv.tool_results = []
    await repo.save_state(deps.session, state["conversation_id"], conv)
    return {"conversation": conv}


# ---- one turn


def trace_id(correlation_id: str | None) -> uuid.UUID | None:
    """The LangSmith run id of a turn: the correlation id itself if it is a UUID, else derived."""
    if not correlation_id:
        return None
    try:
        return uuid.UUID(correlation_id)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"{TRACE_NAMESPACE}:{correlation_id}")


def turn_config(conversation_id: int, correlation_id: str | None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "configurable": {"thread_id": f"conversation-{conversation_id}"},
        "recursion_limit": MAX_GRAPH_STEPS,
        "run_name": "whatsapp_turn",
        "tags": [TRACE_NAMESPACE],
        "metadata": {"conversation_id": conversation_id, "correlation_id": correlation_id},
    }
    run_id = trace_id(correlation_id)
    if run_id is not None:
        config["run_id"] = run_id
    return config


async def run_turn(
    graph: Any,
    conversation_id: int,
    text: str,
    conversation: ConversationState,
    *,
    source: Source = "text",
    correlation_id: str | None = None,
    budget_seconds: float | None = None,
) -> GraphState:
    """One customer message in, the final GraphState of the turn out.

    `budget_seconds` is the time budget of the turn; None takes TURN_BUDGET_SECONDS from the
    settings. When it runs out, no new composer call starts (nodes.compose_node).
    """
    budget = get_settings().turn_budget_seconds if budget_seconds is None else budget_seconds
    start = new_turn(
        conversation_id,
        text,
        conversation,
        source=source,
        correlation_id=correlation_id,
        budget_seconds=budget,
    )
    final: GraphState = await graph.ainvoke(start, turn_config(conversation_id, correlation_id))
    return final


# ---- from the final state to outbox replies


def picture_caption(
    title: str, colour: str | None, colour_image_found: bool | None, lang: ReplyLang
) -> str:
    """The caption under a picture: the product name, and the colour when it is the right one.

    A colour was asked for but has no picture of its own: say so, in the reply language, instead
    of letting the customer think the main picture is that colour.
    """
    if colour and colour_image_found is False:
        return render("picture_colour_missing", lang, title=title, colour=colour)
    if colour and colour_image_found:
        return f"{title} ({colour.title()})"
    return title


def _picture_replies(state: GraphState) -> list[Reply]:
    """Pictures come from the tool result (never from the model), like in Phase 7."""
    router = state.get("router")
    if state.get("used_fallback") or router is None or router.intent is not Intent.IMAGE_REQUEST:
        return []
    for result in state.get("tool_results", []):
        if result.tool != TOOL_IMAGE or not result.ok or not isinstance(result.data, dict):
            continue
        data = result.data
        title, link = data["title"], data["product_url"]
        caption = picture_caption(
            title,
            data.get("colour"),
            data.get("colour_image_found"),
            state.get("reply_language", "en"),
        )
        return [
            Reply(f"{title}: {link}", kind="image", media_url=image["url"], caption=caption)
            for image in data.get("images", [])
        ]
    return []


def to_replies(state: GraphState) -> list[Reply]:
    """Replies for the outbox: the pictures of an image request, otherwise the validated text."""
    pictures = _picture_replies(state)
    if pictures:
        return pictures
    response = state.get("response")
    if response is None:
        raise RuntimeError("the graph ended without a reply")  # cannot happen: see the edges
    return [Reply(response.response_text)]


# ---- the Postgres checkpointer


def checkpoint_url() -> str:
    """psycopg wants a plain postgresql:// URL, the app uses postgresql+asyncpg://."""
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def open_checkpointer(url: str | None = None) -> AsyncIterator[AsyncPostgresSaver]:
    """The LangGraph Postgres saver. setup() creates its tables on the first run (re-runnable)."""
    async with AsyncPostgresSaver.from_conn_string(url or checkpoint_url()) as saver:
        await saver.setup()
        yield saver


def use_selector_event_loop() -> None:
    """Windows only: psycopg async cannot run on the default Proactor loop.

    Call it BEFORE asyncio.run() in every program that opens the checkpointer (worker, cli chat).
    asyncpg (the app's own database driver) works on both loops.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
