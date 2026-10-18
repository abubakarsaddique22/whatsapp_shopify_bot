"""Phase 9, step 3: the first nodes of the graph (Blueprint 3.7, Figure 3) as plain functions.

    normalize_input_node(state)            clean and cap the text
    detect_language_node(state)            cheap script + Roman Urdu hint (the router decides)
    route_intent_node(state, deps)         router call, sticky language, reply language
    gate_node(state)                       permission gate picks the branch (gate.choose_branch)
    run_tools_node(state, deps)            run the tools the intent allows, through the gate
    route_after_tools(state)               'clarify' (no single product), 'pictures' (found), or
                                           'compose'

A node reads the GraphState and returns ONLY the keys it changes (a dict). Nodes that touch the
database or the model take a `Deps` object as second argument; graph.py (step 7) binds it.

Rules this file keeps:
* Every tool call goes through gate() first. A denied call is not run: it becomes the error code
  "permission_denied" in `tool_errors` and a warning in the log (no customer text in the log).
* At most MAX_TOOL_CALLS_PER_TURN tool calls per message: no loop is possible.
* Product ids come only from tool results: a search result fills `candidate_product_ids`, and the
  chosen product becomes `current_product_id`. The model never supplies an id.
* Nothing is saved to the database here. The conversation state is changed on a COPY and handed
  back in the result; the last node of the graph (step 7/8) saves it once.
* Tools run one after another, not in parallel: they share one AsyncSession, which does not allow
  two queries at the same time, and the second call needs the product the first one found.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.composer import compose
from app.agent.gate import POLICY_DOC_TYPE, TOOL_HANDOFF, choose_branch, gate
from app.agent.image_flow import wants_more
from app.agent.router import route_intent
from app.agent.state import (
    MAX_CLARIFICATIONS,
    MAX_COMPOSE_RETRIES,
    MAX_INPUT_CHARS,
    MAX_TOOL_CALLS_PER_TURN,
    GraphState,
    over_budget,
)
from app.agent.templates import render
from app.agent.tools import (
    TOOL_IMAGE,
    TOOL_OPTIONS,
    TOOL_POLICY,
    TOOL_PRICE,
    TOOL_SEARCH,
    TOOL_SPECS,
    TOOL_STOCK,
    TOOLS,
)
from app.agent.validator import ValidationContext, validate
from app.core.errors import PermissionDenied
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.domain.models import BotResponse, ConversationState, Intent, RouterOutput, ToolResult
from app.integrations.embeddings.base import Embedder
from app.integrations.llm.base import ChatModel
from app.language.detect import detect_language, normalize_input
from app.language.policy import next_language, reply_language

log = logging.getLogger(__name__)

# Intents that look up ONE product first, and the tool that answers the question afterwards.
PRODUCT_TOOL: dict[Intent, str | None] = {
    Intent.PRODUCT_SEARCH: None,  # the search itself is the answer
    Intent.PRICE: TOOL_PRICE,
    Intent.AVAILABILITY: TOOL_STOCK,
    Intent.COLORS_SIZES: TOOL_OPTIONS,
    Intent.SPECS: TOOL_SPECS,
    Intent.IMAGE_REQUEST: TOOL_IMAGE,
}

# error codes this file adds to GraphState.tool_errors (tool error codes are added as they are)
ERR_NO_ROUTER = "no_router"
ERR_NO_PRODUCT = "no_product"  # no product named and none in the conversation
ERR_DENIED = "permission_denied"
ERR_CAP = "tool_cap_reached"
ERR_COMPOSER = "composer_failed"  # provider failed or two invalid answers: safe_fallback
ERR_BUDGET = "turn_budget_exceeded"  # the time budget of the turn is used up: safe_fallback

MAX_NAMES = 3  # product names listed in one clarifying question
MAX_OPTIONS = 8  # colours / options of ONE product listed in one clarifying question


@dataclass
class Deps:
    """What the nodes need from outside. Tests pass a FakeLLM and a FakeEmbedder."""

    session: AsyncSession
    llm: ChatModel
    embedder: Embedder | None = None  # None: search_policy uses the configured embedder
    urdu_script_replies: bool = False
    composer_llm: ChatModel | None = None  # COMPOSER_MODEL; None: the router's model is used


# ---- the cheap nodes


async def normalize_input_node(state: GraphState) -> dict[str, Any]:
    return {"text": normalize_input(state.get("text", ""), MAX_INPUT_CHARS)}


async def detect_language_node(state: GraphState) -> dict[str, Any]:
    hint = detect_language(state.get("text", ""))
    return {"language_hint": hint.language.value}


async def route_intent_node(state: GraphState, deps: Deps) -> dict[str, Any]:
    """Router call, then the sticky language and the reply language (same rules as step 5)."""
    conv = state["conversation"].model_copy(deep=True)
    text = state.get("text", "")
    previous = conv.last_router_output.language if conv.last_router_output else None

    title = None
    if conv.current_product_id:
        basics = await repo.product_basics(deps.session, conv.current_product_id)
        title = basics.title if basics else None

    out = await route_intent(
        text, deps.llm, last_language=conv.language, current_product_title=title
    )
    language = next_language(conv.language, previous, out.language, text)
    conv.language = language
    conv.last_router_output = out
    return {
        "router": out,
        "conversation": conv,
        "reply_language": reply_language(language, urdu_script_replies=deps.urdu_script_replies),
    }


async def gate_node(state: GraphState) -> dict[str, Any]:
    """Pick the branch. An abuse / injection message is a security event (logged, never shown)."""
    out = state.get("router")
    if out is None:
        return {"branch": "clarify"}
    update: dict[str, Any] = {"branch": choose_branch(out, state["conversation"])}
    if out.intent is Intent.ABUSE_OR_INJECTION:
        update["security_event"] = "abuse_or_injection"
        log.warning("security_event", extra={"event": "abuse_or_injection"})
    return update


# ---- the tool nodes


class _ToolRun:
    """The tool calls of ONE turn: gate first, cap, collect results, keep ids honest."""

    def __init__(self, deps: Deps, intent: Intent, conv: ConversationState, calls_made: int):
        self.deps = deps
        self.intent = intent
        self.conv = conv
        self.calls_made = calls_made
        self.results: list[ToolResult] = []
        self.errors: list[str] = []

    async def call(
        self, tool: str, extra: dict[str, Any] | None = None, **args: Any
    ) -> ToolResult | None:
        if self.calls_made >= MAX_TOOL_CALLS_PER_TURN:
            self.errors.append(ERR_CAP)
            return None
        try:
            gate(self.intent, tool, args, self.conv)
        except PermissionDenied:
            self.errors.append(ERR_DENIED)
            log.warning("gate_denied", extra={"tool": tool, "intent": self.intent.value})
            return None
        self.calls_made += 1
        result = await TOOLS[tool](self.deps.session, **args, **(extra or {}))
        self.results.append(result)
        if not result.ok and result.error_code:
            self.errors.append(result.error_code)
        return result


def _remember_search(conv: ConversationState, result: ToolResult) -> str | None:
    """Keep the ids a search returned. One clear match becomes the current product."""
    if not result.ok or not isinstance(result.data, dict):
        return None
    candidates = result.data.get("candidates") or []
    conv.candidate_product_ids = [c["product_id"] for c in candidates]
    if result.data.get("kind") == "one" and candidates:
        conv.current_product_id = candidates[0]["product_id"]
        return str(conv.current_product_id)
    return None


async def _product_flow(run: _ToolRun, router: RouterOutput, text: str) -> None:
    """Find ONE product (a search, or the one already discussed), then ask the answer tool."""
    conv = run.conv
    product_id: str | None = None

    if router.product_query:
        # Colour and size go to the answer tool, which can say "not offered". Only a plain
        # product search uses them as filters.
        filters: dict[str, Any] = {}
        if run.intent is Intent.PRODUCT_SEARCH:
            filters = {
                "colour": router.color,
                "size_mm": router.size_mm,
                "max_price": router.max_price,
            }
        found = await run.call(TOOL_SEARCH, query=router.product_query, **filters)
        if found is None:
            return
        product_id = _remember_search(conv, found)
    elif conv.current_product_id and run.intent is not Intent.PRODUCT_SEARCH:
        product_id = conv.current_product_id
    else:
        run.errors.append(ERR_NO_PRODUCT)
        return

    tool = PRODUCT_TOOL[run.intent]
    if tool is None or product_id is None:  # plain search, or ambiguous / none / failed search
        return

    args: dict[str, Any] = {"product_id": product_id}
    if tool in (TOOL_PRICE, TOOL_STOCK):
        args.update(colour=router.color, size_mm=router.size_mm)
    elif tool == TOOL_IMAGE:
        args.update(colour=router.color, max_images=3 if wants_more(text) else 1)

    result = await run.call(tool, **args)
    if result is not None and result.error_code == "not_found":
        conv.current_product_id = None  # a hidden or unknown product must not stay "current"


async def _policy_flow(run: _ToolRun, router: RouterOutput, text: str) -> None:
    await run.call(
        TOOL_POLICY,
        extra={"original_query": text, "embedder": run.deps.embedder},
        query_en=router.kb_query_en or "",
        doc_type=POLICY_DOC_TYPE.get(run.intent),
    )


async def run_tools_node(state: GraphState, deps: Deps) -> dict[str, Any]:
    """Run the tools the router's intent allows (product_info, image_request, policy_rag)."""
    router = state.get("router")
    if router is None:
        return {"tool_errors": [ERR_NO_ROUTER]}

    conv = state["conversation"].model_copy(deep=True)
    run = _ToolRun(deps, router.intent, conv, state.get("tool_calls_made", 0))
    text = state.get("text", "")

    if router.intent in POLICY_DOC_TYPE:
        await _policy_flow(run, router, text)
    elif router.intent in PRODUCT_TOOL:
        await _product_flow(run, router, text)

    return {
        "tool_results": [*state.get("tool_results", []), *run.results],
        "tool_calls_made": run.calls_made,
        "tool_errors": [*state.get("tool_errors", []), *run.errors],
        "conversation": conv,
    }


# Figure 3 names three tool nodes; they share one implementation, the intent decides the plan.
async def product_info_node(state: GraphState, deps: Deps) -> dict[str, Any]:
    return await run_tools_node(state, deps)


async def image_request_node(state: GraphState, deps: Deps) -> dict[str, Any]:
    return await run_tools_node(state, deps)


async def policy_rag_node(state: GraphState, deps: Deps) -> dict[str, Any]:
    return await run_tools_node(state, deps)


def _ambiguous_variants(state: GraphState) -> tuple[str, list[str]] | None:
    """(product title, variant names) when price or stock found ONE product with several variants.

    The customer did not say which colour / option, so the tool cannot give one price or stock
    state. That is a question for the customer, not a text for the composer to write.
    """
    for result in state.get("tool_results", []):
        if result.tool in (TOOL_PRICE, TOOL_STOCK) and isinstance(result.data, dict):
            if result.data.get("match") == "ambiguous":
                variants = result.data.get("variants") or []
                names = [str(v["title"]) for v in variants if v.get("title")]
                if names:
                    return str(result.data.get("title", "")), names
    return None


def _pictures_found(state: GraphState) -> bool:
    """True when this is a picture request and the picture tool found at least one picture."""
    router = state.get("router")
    if router is None or router.intent is not Intent.IMAGE_REQUEST:
        return False
    for result in state.get("tool_results", []):
        if result.tool == TOOL_IMAGE and result.ok and isinstance(result.data, dict):
            return bool(result.data.get("images"))
    return False


def route_after_tools(state: GraphState) -> Literal["clarify", "pictures", "compose"]:
    """Several products, no product or none found: ask. Pictures found: send them. Else compose.

    Pictures found means the pictures ARE the answer: no composer call and no validator, because
    nothing the model writes would be sent anyway.
    """
    if ERR_NO_PRODUCT in state.get("tool_errors", []):
        return "clarify"
    for result in state.get("tool_results", []):
        if result.tool == TOOL_SEARCH and isinstance(result.data, dict):
            if result.data.get("kind") in ("ambiguous", "none"):
                return "clarify"
    if _ambiguous_variants(state) is not None:
        return "clarify"
    if _pictures_found(state):
        return "pictures"
    return "compose"


async def pictures_node(state: GraphState) -> dict[str, Any]:
    """The picture tool found pictures. No model call: a template line for the history.

    The customer gets the pictures with their captions (graph.to_replies). Every picture URL comes
    from the tool result, never from a model.
    """
    title = ""
    for result in state.get("tool_results", []):
        if result.tool == TOOL_IMAGE and isinstance(result.data, dict):
            title = str(result.data.get("title", ""))
    source_ids = [r.source_id for r in state.get("tool_results", []) if r.tool != TOOL_HANDOFF]
    response = _reply(state, "pictures_sent", title=title)
    return {"response": response.model_copy(update={"source_ids": source_ids})}


# ---- compose (step 5)


async def compose_node(state: GraphState, deps: Deps) -> dict[str, Any]:
    """The model writes the reply from tool results only. None response -> safe_fallback."""
    router = state.get("router")
    conv = state["conversation"]
    attempts = state.get("compose_attempts", 0) + 1
    if over_budget(state):  # no new model call once the time budget of the turn is used up
        log.warning("turn_budget_exceeded", extra={"attempt": attempts})
        return {
            "response": None,
            "compose_attempts": attempts,
            "tool_errors": [*state.get("tool_errors", []), ERR_BUDGET],
        }
    response = await compose(
        deps.composer_llm or deps.llm,
        text=state.get("text", ""),
        intent=router.intent if router else Intent.UNCLEAR,
        language=router.language if router else conv.language,
        reply_lang=state.get("reply_language", "en"),
        results=state.get("tool_results", []),
        history=conv.history,
        show_exact_stock=get_settings().show_exact_stock,
        feedback=state.get("validation_errors", []),
    )
    update: dict[str, Any] = {"response": response, "compose_attempts": attempts}
    if response is None:
        update["tool_errors"] = [*state.get("tool_errors", []), ERR_COMPOSER]
    return update


# ---- validate (step 6)


def _secret_values(settings: Settings) -> list[str]:
    """Literal secrets the validator looks for in a reply (placeholders are skipped)."""
    secrets = [
        settings.llm_api_key,
        settings.whatsapp_access_token,
        settings.whatsapp_app_secret,
        settings.whatsapp_verify_token,
        settings.shopify_client_secret,
        settings.shopify_offline_token,
        settings.admin_api_key,
        settings.phone_hash_pepper,
    ]
    values = [s.get_secret_value() for s in secrets]
    return [v for v in values if len(v) >= 8 and not v.startswith("change-me")]


async def validate_node(state: GraphState) -> dict[str, Any]:
    """Check the composed reply. Problems go back to the composer for the one retry."""
    response = state.get("response")
    if response is None:
        return {"validation_errors": []}
    router = state.get("router")
    settings = get_settings()
    problems = validate(
        response,
        ValidationContext(
            reply_lang=state.get("reply_language", "en"),
            intent=router.intent if router else Intent.UNCLEAR,
            results=state.get("tool_results", []),
            customer_text=state.get("text", ""),
            store_domain=settings.shopify_shop_domain,
            secrets=_secret_values(settings),
        ),
    )
    if problems:  # only the codes are logged, never the reply (it can hold customer data)
        log.warning(
            "validator_rejected",
            extra={
                "codes": sorted({p.split(":", 1)[0] for p in problems}),
                "attempt": state.get("compose_attempts", 0),
            },
        )
    return {"validation_errors": problems}


def route_after_validate(state: GraphState) -> Literal["done", "retry", "safe_fallback"]:
    """No reply or a second rejection: safe_fallback. First rejection: compose once more."""
    if state.get("response") is None:
        return "safe_fallback"
    if not state.get("validation_errors"):
        return "done"
    if over_budget(state):  # a retry is another model call: not when the time is used up
        return "safe_fallback"
    if state.get("compose_attempts", 0) <= MAX_COMPOSE_RETRIES:
        return "retry"
    return "safe_fallback"


async def safe_fallback_node(state: GraphState) -> dict[str, Any]:
    """Composer failed or the validator rejected twice: templated reply plus a handoff (8.8)."""
    reason = "composer_failed" if state.get("response") is None else "validator_rejected"
    update = await handoff_node(state, reason=reason, kind="safe_fallback")
    update["used_fallback"] = True
    return update


# ---- templated nodes (step 4): clarify, refuse, handoff. No model call.


def tool_create_handoff_stub(reason: str, *, urgency: str = "normal") -> ToolResult:
    """Phase 9 stub: no database row and no staff message. Phase 11 replaces it (handoff/)."""
    return ToolResult(
        tool=TOOL_HANDOFF,
        ok=True,
        data={"status": "stub", "reason": reason, "urgency": urgency},
        source_id=f"{TOOL_HANDOFF}:{uuid.uuid4().hex[:12]}",
        fetched_at=datetime.now(UTC),
    )


def _reply(state: GraphState, kind: str, **values: str) -> BotResponse:
    out = state.get("router")
    return BotResponse(
        detected_language=state["conversation"].language,
        intent=out.intent if out else Intent.UNCLEAR,
        response_text=render(kind, state.get("reply_language", "en"), **values),
    )


def _clarified(
    state: GraphState, conv: ConversationState, kind: str, **values: str
) -> dict[str, Any]:
    conv.clarification_count += 1
    response = _reply(state, kind, **values).model_copy(update={"needs_clarification": True})
    return {"response": response, "conversation": conv}


async def clarify_node(state: GraphState) -> dict[str, Any]:
    """One short question. After MAX_CLARIFICATIONS in a row a person takes over (8.6)."""
    conv = state["conversation"].model_copy(deep=True)
    if conv.clarification_count >= MAX_CLARIFICATIONS:
        return await handoff_node(state, reason="repeated_clarification")

    variants = _ambiguous_variants(state)
    if variants is not None:  # one product, several colours / options: list them
        title, options = variants
        listed = ", ".join(dict.fromkeys(options[:MAX_OPTIONS]))
        return _clarified(state, conv, "ask_which_variant", title=title, options=listed)

    names: list[str] = []
    for result in state.get("tool_results", []):  # names come from the search result only
        if result.tool == TOOL_SEARCH and isinstance(result.data, dict):
            if result.data.get("kind") == "none":
                return _clarified(state, conv, "not_found")
            names = [c["title"] for c in result.data.get("candidates", [])[:MAX_NAMES]]
    if names:
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
        return _clarified(state, conv, "ask_which_product", names=listed)
    if ERR_NO_PRODUCT in state.get("tool_errors", []):
        return _clarified(state, conv, "ask_product_name")
    return _clarified(state, conv, "ask_detail")


# Which template refuse_node sends. Anything not listed here is a private_data_request.
REFUSAL_KIND: dict[Intent, str] = {
    Intent.ABUSE_OR_INJECTION: "abuse_or_injection",
    Intent.OUT_OF_SCOPE: "out_of_scope",
}


async def refuse_node(state: GraphState) -> dict[str, Any]:
    """private_data_request, abuse_or_injection, out_of_scope. No tool runs, no model call.

    A person is only OFFERED (private data). The text is a template in the reply language.
    """
    out = state.get("router")
    kind = REFUSAL_KIND.get(out.intent, "private_data") if out else "private_data"
    return {"response": _reply(state, kind)}


async def handoff_node(
    state: GraphState, reason: str | None = None, kind: str = "handoff_created"
) -> dict[str, Any]:
    """Record a handoff (stub), pause the bot for the customer, tell the customer.

    This is a system action, not a model-chosen tool, so it does not go through gate(): the
    third unclear message has intent 'unclear', which has no allowed tools.
    """
    conv = state["conversation"].model_copy(deep=True)
    out = state.get("router")
    reason = reason or (out.intent.value if out else "unknown")
    conv.needs_handoff = True
    conv.handoff_reason = reason
    conv.bot_paused_until = datetime.now(UTC) + timedelta(hours=get_settings().handoff_pause_hours)
    response = _reply(state, kind).model_copy(update={"needs_handoff": True})
    return {
        "response": response,
        "conversation": conv,
        "handoff_reason": reason,
        "tool_results": [*state.get("tool_results", []), tool_create_handoff_stub(reason)],
    }
