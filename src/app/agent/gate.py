"""Phase 9, step 2: the permission gate (Blueprint 8.5). Pure Python, outside the LLM.

    gate(intent, tool, args, state)            # raises PermissionDenied, returns None when allowed
    choose_branch(router_output, state)        # which node of Figure 3 runs after the router

The model only says WHAT the customer wants (the intent). This file decides which tools may run
for that intent and with which arguments. The model cannot add a tool and cannot invent an id:

* a tool must be on the list for the intent (ALLOWED_TOOLS, one entry for EVERY intent);
* a product id must come from an earlier tool result (state.candidate_product_ids) or be the
  product the conversation is already about (state.current_product_id, which also came from a
  tool result);
* limits: search_products at most 5 results, pictures at most 3, search_policy only with the
  doc_type that belongs to the intent.

A variant id is not checked here: the price, stock and options tools already refuse a variant
that does not belong to the product (error_code not_found).

Differences from the blueprint table 8.5, on purpose:
* tool names are the ones in agent/tools.py (get_colors_sizes, check_stock, get_price, get_specs,
  get_product_image), not the blueprint's get_variant_options / check_variant / get_product_specs;
* colors_sizes and specs may also call search_products, because the FIRST message of a
  conversation ("GA-2100 ke colours?") has no candidate product yet. The id rule still holds.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.agent.state import HANDOFF_AFTER_CLARIFICATIONS, Branch
from app.agent.tools import (
    TOOL_IMAGE,
    TOOL_OPTIONS,
    TOOL_POLICY,
    TOOL_PRICE,
    TOOL_SEARCH,
    TOOL_SPECS,
    TOOL_STOCK,
)
from app.core.errors import PermissionDenied
from app.domain.models import ConversationState, Intent, RouterOutput

TOOL_HANDOFF = "create_handoff"  # written in step 4 (stub); the gate already knows its name

MAX_SEARCH_LIMIT = 5
MAX_IMAGES = 3

ALLOWED_TOOLS: dict[Intent, frozenset[str]] = {
    Intent.PRODUCT_SEARCH: frozenset({TOOL_SEARCH}),
    Intent.PRICE: frozenset({TOOL_SEARCH, TOOL_OPTIONS, TOOL_PRICE}),
    Intent.AVAILABILITY: frozenset({TOOL_SEARCH, TOOL_OPTIONS, TOOL_STOCK}),
    Intent.COLORS_SIZES: frozenset({TOOL_SEARCH, TOOL_OPTIONS}),
    Intent.SPECS: frozenset({TOOL_SEARCH, TOOL_SPECS}),
    Intent.IMAGE_REQUEST: frozenset({TOOL_SEARCH, TOOL_IMAGE}),
    Intent.POLICY_REFUND: frozenset({TOOL_POLICY}),
    Intent.POLICY_SHIPPING: frozenset({TOOL_POLICY}),
    Intent.POLICY_PRIVACY: frozenset({TOOL_POLICY}),
    Intent.FAQ: frozenset({TOOL_POLICY}),
    Intent.HUMAN_REQUEST: frozenset({TOOL_HANDOFF}),
    Intent.COMPLAINT: frozenset({TOOL_HANDOFF}),
    Intent.PRIVATE_DATA_REQUEST: frozenset({TOOL_HANDOFF}),  # no data tools at all
    Intent.ABUSE_OR_INJECTION: frozenset(),  # nothing runs; the caller logs a security event
    Intent.GREETING: frozenset(),
    Intent.OUT_OF_SCOPE: frozenset(),
    Intent.UNCLEAR: frozenset(),
}

# search_policy only runs with the doc_type of the intent (a refund question reads refund text).
POLICY_DOC_TYPE: dict[Intent, str] = {
    Intent.POLICY_REFUND: "policy_refund",
    Intent.POLICY_SHIPPING: "policy_shipping",
    Intent.POLICY_PRIVACY: "policy_privacy",
    Intent.FAQ: "faq",
}

# Which node runs after the router. Every intent has an entry (a unit test fails otherwise).
# UNCLEAR is handled in choose_branch(): clarify, or handoff after repeated failures.
INTENT_BRANCH: dict[Intent, Branch] = {
    Intent.PRODUCT_SEARCH: "product_info",
    Intent.PRICE: "product_info",
    Intent.AVAILABILITY: "product_info",
    Intent.COLORS_SIZES: "product_info",
    Intent.SPECS: "product_info",
    Intent.IMAGE_REQUEST: "image_request",
    Intent.POLICY_REFUND: "policy_rag",
    Intent.POLICY_SHIPPING: "policy_rag",
    Intent.POLICY_PRIVACY: "policy_rag",
    Intent.FAQ: "policy_rag",
    Intent.HUMAN_REQUEST: "handoff",
    Intent.COMPLAINT: "handoff",
    Intent.PRIVATE_DATA_REQUEST: "refuse",
    Intent.ABUSE_OR_INJECTION: "refuse",
    Intent.GREETING: "compose",
    Intent.OUT_OF_SCOPE: "refuse",  # the out_of_scope template, no model call
    Intent.UNCLEAR: "clarify",
}


def known_product_ids(state: ConversationState) -> set[str]:
    """Ids a tool call may use: earlier search results and the product being discussed."""
    ids = set(state.candidate_product_ids)
    if state.current_product_id:
        ids.add(state.current_product_id)
    return ids


def _values(args: BaseModel | Mapping[str, Any] | None) -> dict[str, Any]:
    if args is None:
        return {}
    if isinstance(args, BaseModel):
        return args.model_dump()
    return dict(args)


def gate(
    intent: Intent,
    tool: str,
    args: BaseModel | Mapping[str, Any] | None,
    state: ConversationState,
) -> None:
    """Return None when the call may run, raise PermissionDenied when it may not."""
    allowed = ALLOWED_TOOLS.get(intent, frozenset())
    if tool not in allowed:
        raise PermissionDenied(f"tool not allowed for this intent: {tool}")

    values = _values(args)

    if "product_id" in values:
        pid = values["product_id"]
        if not isinstance(pid, str) or pid not in known_product_ids(state):
            raise PermissionDenied("product id not from a previous tool result")

    if tool == TOOL_SEARCH:
        limit = values.get("limit")
        if limit is not None and not (isinstance(limit, int) and 1 <= limit <= MAX_SEARCH_LIMIT):
            raise PermissionDenied("search limit too high")

    if tool == TOOL_IMAGE:
        wanted = values.get("max_images", 1)
        if not (isinstance(wanted, int) and 1 <= wanted <= MAX_IMAGES):
            raise PermissionDenied("too many images requested")

    if tool == TOOL_POLICY:
        if values.get("doc_type") != POLICY_DOC_TYPE.get(intent):
            raise PermissionDenied("doc_type does not match the intent")


def choose_branch(out: RouterOutput, state: ConversationState) -> Branch:
    """The node that runs after the router (Figure 3).

    An unclear message first gets a clarifying question. When the customer was already asked
    twice, the third unclear message goes to a person instead (Blueprint 8.6).
    """
    if out.intent is Intent.UNCLEAR and state.clarification_count + 1 >= (
        HANDOFF_AFTER_CLARIFICATIONS
    ):
        return "handoff"
    return INTENT_BRANCH[out.intent]
