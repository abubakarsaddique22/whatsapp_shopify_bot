"""Phase 9, step 1: the state that travels through the conversation graph (Blueprint 3.7, Fig 3).

Two kinds of state, kept apart on purpose:

* `ConversationState` (domain/models.py) lives in the database between customer messages
  (repo.load_state / repo.save_state): sticky language, current product, candidate product ids,
  clarification count, handoff pause. Nothing about it changes in Phase 9.
* `GraphState` (this file) is ONE turn: one customer message goes in, one reply comes out. Every
  node reads it and returns only the keys it changes. `new_turn()` builds the starting state, so
  no value from an earlier turn can leak into this one.

Nodes of Figure 3 and the `branch` the permission gate picks after route_intent:

    normalize_input -> detect_language -> route_intent -> permission_gate -> <branch>
    product_info | image_request | policy_rag   run allowed tools, collect ToolResult objects
    clarify | handoff | refuse                  templated replies, no model call
    compose                                      the model writes the reply from tool results
    compose -> validate -> (one retry) -> safe_fallback

The caps below keep the graph bounded: there is no loop without a counter.
"""

import time
from typing import Literal, TypedDict

from app.domain.models import BotResponse, ConversationState, RouterOutput, ToolResult
from app.language.policy import ReplyLang

# What the customer's message was: typed, or the transcript of a voice note (Phase 6).
Source = Literal["text", "voice"]

# The branch chosen by the permission gate (gate.py, step 2). One name per node after the gate.
Branch = Literal[
    "product_info",
    "image_request",
    "policy_rag",
    "clarify",
    "handoff",
    "refuse",
    "compose",
]
BRANCHES: tuple[str, ...] = (
    "product_info",
    "image_request",
    "policy_rag",
    "clarify",
    "handoff",
    "refuse",
    "compose",
)

# ---- caps (Blueprint 8.4, 8.6, 8.7, 8.8)
MAX_INPUT_CHARS = 1000  # normalize_input cuts a longer message
MAX_TOOL_CALLS_PER_TURN = 4  # no tool loop: at most this many tool calls for one message
MAX_COMPOSE_RETRIES = 1  # validator rejects once -> one retry with its message, then safe_fallback
MAX_CLARIFICATIONS = 2  # questions in a row; image_flow.py uses the same number
HANDOFF_AFTER_CLARIFICATIONS = 3  # a third failed clarification goes to a person (8.6)
HISTORY_WINDOW = 6  # messages of history the composer sees


class GraphState(TypedDict, total=False):
    # --- input (set by new_turn)
    conversation_id: int
    text: str  # the customer's message (or voice transcript), cleaned by normalize_input
    source: Source
    correlation_id: str | None  # also the LangSmith trace id link (step 7)
    deadline: float  # time.monotonic() value after which no new composer call starts

    # --- loaded at the start of the turn
    conversation: ConversationState

    # --- language and routing
    language_hint: str | None  # cheap hint from language/detect.py (the router decides)
    router: RouterOutput | None
    reply_language: ReplyLang
    branch: Branch | None

    # --- tools
    tool_results: list[ToolResult]
    tool_calls_made: int
    tool_errors: list[str]  # error codes, for example "shopify_unavailable"

    # --- composing and validating
    response: BotResponse | None
    compose_attempts: int
    validation_errors: list[str]  # why the validator rejected the last reply
    used_fallback: bool  # True when a templated safe reply was sent instead of a composed one

    # --- security
    security_event: str | None  # for example "injection_suspected"; logged, never shown
    handoff_reason: str | None  # why a person is needed (human_request, complaint, ...)


def new_turn(
    conversation_id: int,
    text: str,
    conversation: ConversationState,
    *,
    source: Source = "text",
    correlation_id: str | None = None,
    budget_seconds: float | None = None,
) -> GraphState:
    """The starting state of one turn. Every per-turn field is set, so nothing is left over.

    `budget_seconds` is the time budget of the turn (Blueprint 3.7): None means no deadline.
    """
    state = GraphState(
        conversation_id=conversation_id,
        text=text,
        source=source,
        correlation_id=correlation_id,
        conversation=conversation,
        language_hint=None,
        router=None,
        reply_language="en",
        branch=None,
        tool_results=[],
        tool_calls_made=0,
        tool_errors=[],
        response=None,
        compose_attempts=0,
        validation_errors=[],
        used_fallback=False,
        security_event=None,
        handoff_reason=None,
        deadline=float("inf") if budget_seconds is None else time.monotonic() + budget_seconds,
    )
    return state


def over_budget(state: GraphState) -> bool:
    """True when the turn has used up its time budget (no deadline: never)."""
    deadline = state.get("deadline")
    return deadline is not None and time.monotonic() >= deadline
