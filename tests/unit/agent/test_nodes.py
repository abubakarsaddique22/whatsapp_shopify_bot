"""Phase 9, step 3: agent/nodes.py. Fake tools and a FakeLLM: no database, no network.

The tools in the TOOLS registry are replaced by small fakes that record how they were called, so
these tests show WHICH tools a node calls, in which order and with which arguments.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agent import nodes
from app.agent.nodes import (
    ERR_CAP,
    ERR_DENIED,
    ERR_NO_PRODUCT,
    ERR_NO_ROUTER,
    Deps,
    clarify_node,
    detect_language_node,
    gate_node,
    image_request_node,
    normalize_input_node,
    pictures_node,
    policy_rag_node,
    route_after_tools,
    route_intent_node,
    run_tools_node,
)
from app.agent.state import MAX_INPUT_CHARS, MAX_TOOL_CALLS_PER_TURN, new_turn
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
from app.domain.models import ConversationState, Intent, Lang, RouterOutput, ToolResult
from app.integrations.embeddings.fake_embedder import FakeEmbedder
from app.integrations.llm.fake_llm import FakeLLM

PID = "gid://shopify/Product/1"
OTHER = "gid://shopify/Product/2"


# ---- helpers


def conv(**kw) -> ConversationState:
    return ConversationState(conversation_id="c1", sender_hash="h1", **kw)


def router(intent: Intent, **kw) -> RouterOutput:
    return RouterOutput(language=Lang.EN, intent=intent, confidence=0.9, **kw)


def turn(intent: Intent, *, text: str = "hello", conversation=None, **kw):
    state = new_turn(1, text, conversation or conv())
    state["router"] = router(intent, **kw)
    return state


def result(tool: str, data=None, ok: bool = True, error_code: str | None = None) -> ToolResult:
    return ToolResult(
        tool=tool,
        ok=ok,
        data=data,
        error_code=error_code,
        source_id=f"{tool}:test",
        fetched_at=datetime.now(UTC),
    )


def search_one(pid: str = PID) -> dict:
    return {"kind": "one", "candidates": [{"product_id": pid, "title": "GA-2100"}]}


def search_many() -> dict:
    return {
        "kind": "ambiguous",
        "candidates": [
            {"product_id": PID, "title": "GA-2100 Black"},
            {"product_id": OTHER, "title": "GA-2100 Blue"},
        ],
    }


class FakeTools:
    """Replaces entries of the TOOLS registry and records every call as (tool, kwargs)."""

    def __init__(self, monkeypatch) -> None:
        self.monkeypatch = monkeypatch
        self.calls: list[tuple[str, dict]] = []

    def install(self, tool: str, data=None, *, ok: bool = True, error_code: str | None = None):
        async def fake(session, **kwargs):
            self.calls.append((tool, kwargs))
            return result(tool, data, ok, error_code)

        self.monkeypatch.setitem(TOOLS, tool, fake)

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def args(self, tool: str) -> dict:
        return next(kwargs for name, kwargs in self.calls if name == tool)


@pytest.fixture
def tools(monkeypatch) -> FakeTools:
    return FakeTools(monkeypatch)


def deps(answers: list[str] | None = None, embedder=None) -> Deps:
    return Deps(session=None, llm=FakeLLM(answers or []), embedder=embedder)  # type: ignore[arg-type]


ROUTER_JSON = json.dumps(
    {
        "language": "en",
        "intent": "price",
        "product_query": "GA-2100",
        "color": None,
        "size_mm": None,
        "max_price": None,
        "kb_query_en": None,
        "needs_clarification": False,
        "confidence": 0.9,
    }
)


# ---- normalize_input and detect_language


async def test_normalize_input_removes_control_characters_and_caps_the_length():
    state = new_turn(1, "  price\u200b kya\x00 hai?  " + "a" * 3000, conv())
    update = await normalize_input_node(state)
    assert update["text"].startswith("price kya hai?")
    assert "\u200b" not in update["text"] and "\x00" not in update["text"]
    assert len(update["text"]) <= MAX_INPUT_CHARS


async def test_detect_language_gives_a_hint_from_the_script():
    state = new_turn(1, "یہ گھڑی کتنے کی ہے", conv())
    assert (await detect_language_node(state)) == {"language_hint": "ur"}


async def test_detect_language_says_unknown_when_there_is_no_evidence():
    state = new_turn(1, "GA-2100", conv())
    assert (await detect_language_node(state)) == {"language_hint": "unknown"}


# ---- route_intent_node


async def test_route_intent_node_updates_a_copy_of_the_conversation():
    original = conv()
    state = new_turn(1, "GA-2100 price?", original)
    update = await route_intent_node(state, deps([ROUTER_JSON]))

    assert update["router"].intent is Intent.PRICE
    assert update["router"].product_query == "GA-2100"
    assert update["reply_language"] == "en"
    assert update["conversation"].language is Lang.EN
    assert update["conversation"].last_router_output == update["router"]
    assert original.language is Lang.UNKNOWN  # the state that came in was not changed
    assert original.last_router_output is None


async def test_route_intent_node_gives_the_router_the_current_product_title(monkeypatch):
    async def basics(session, product_id):
        return SimpleNamespace(title="Casio Vintage X")

    monkeypatch.setattr(nodes.repo, "product_basics", basics)
    llm = FakeLLM([ROUTER_JSON])
    state = new_turn(1, "price?", conv(current_product_id=PID))
    await route_intent_node(state, Deps(session=None, llm=llm))  # type: ignore[arg-type]
    assert "Casio Vintage X" in llm.calls[0][0]


async def test_route_intent_node_falls_back_to_unclear_when_the_model_fails():
    state = new_turn(1, "GA-2100 price?", conv())
    update = await route_intent_node(state, deps([]))  # FakeLLM has no answers: ProviderError
    assert update["router"].intent is Intent.UNCLEAR
    assert update["router"].needs_clarification is True


# ---- gate_node


@pytest.mark.parametrize(
    ("intent", "branch"),
    [
        (Intent.PRICE, "product_info"),
        (Intent.IMAGE_REQUEST, "image_request"),
        (Intent.FAQ, "policy_rag"),
        (Intent.COMPLAINT, "handoff"),
        (Intent.PRIVATE_DATA_REQUEST, "refuse"),
        (Intent.OUT_OF_SCOPE, "refuse"),
        (Intent.GREETING, "compose"),
        (Intent.UNCLEAR, "clarify"),
    ],
)
async def test_gate_node_picks_the_branch(intent, branch):
    update = await gate_node(turn(intent))
    assert update["branch"] == branch
    assert "security_event" not in update


async def test_gate_node_marks_an_injection_as_a_security_event():
    update = await gate_node(turn(Intent.ABUSE_OR_INJECTION))
    assert update["branch"] == "refuse"
    assert update["security_event"] == "abuse_or_injection"


async def test_gate_node_without_a_router_result_asks_a_question():
    assert (await gate_node(new_turn(1, "x", conv())))["branch"] == "clarify"


# ---- tools: products


async def test_price_question_searches_then_asks_for_the_price(tools):
    tools.install(TOOL_SEARCH, search_one())
    tools.install(TOOL_PRICE, {"match": "one"})
    state = turn(Intent.PRICE, product_query="GA-2100", color="black", size_mm=45)

    update = await run_tools_node(state, deps())

    assert tools.names == [TOOL_SEARCH, TOOL_PRICE]
    # colour and size are NOT search filters for a price question: the price tool answers them
    assert tools.args(TOOL_SEARCH) == {"query": "GA-2100"}
    assert tools.args(TOOL_PRICE) == {"product_id": PID, "colour": "black", "size_mm": 45}
    assert [r.tool for r in update["tool_results"]] == [TOOL_SEARCH, TOOL_PRICE]
    assert update["tool_calls_made"] == 2
    assert update["tool_errors"] == []
    assert update["conversation"].current_product_id == PID
    assert update["conversation"].candidate_product_ids == [PID]
    assert route_after_tools({**state, **update}) == "compose"


async def test_a_plain_product_search_uses_colour_size_and_price_as_filters(tools):
    tools.install(TOOL_SEARCH, search_one())
    state = turn(Intent.PRODUCT_SEARCH, product_query="digital", color="black", max_price=20000)

    await run_tools_node(state, deps())

    assert tools.names == [TOOL_SEARCH]
    assert tools.args(TOOL_SEARCH) == {
        "query": "digital",
        "colour": "black",
        "size_mm": None,
        "max_price": 20000,
    }


@pytest.mark.parametrize(
    ("intent", "tool"),
    [
        (Intent.AVAILABILITY, TOOL_STOCK),
        (Intent.COLORS_SIZES, TOOL_OPTIONS),
        (Intent.SPECS, TOOL_SPECS),
    ],
)
async def test_each_product_intent_asks_its_own_tool(tools, intent, tool):
    tools.install(TOOL_SEARCH, search_one())
    tools.install(tool, {"ok": True})

    await run_tools_node(turn(intent, product_query="GA-2100"), deps())

    assert tools.names == [TOOL_SEARCH, tool]
    assert tools.args(tool)["product_id"] == PID


async def test_a_follow_up_uses_the_current_product_without_a_search(tools):
    tools.install(TOOL_PRICE, {"match": "one"})
    state = turn(Intent.PRICE, color="black", conversation=conv(current_product_id=PID))

    update = await run_tools_node(state, deps())

    assert tools.names == [TOOL_PRICE]
    assert tools.args(TOOL_PRICE)["product_id"] == PID
    assert update["tool_errors"] == []


async def test_no_product_named_and_none_in_the_conversation_means_a_question(tools):
    update = await run_tools_node(turn(Intent.PRICE), deps())

    assert tools.calls == []
    assert update["tool_errors"] == [ERR_NO_PRODUCT]
    assert route_after_tools({**turn(Intent.PRICE), **update}) == "clarify"


async def test_a_product_search_without_words_asks_a_question_even_with_a_current_product(tools):
    state = turn(Intent.PRODUCT_SEARCH, conversation=conv(current_product_id=PID))
    update = await run_tools_node(state, deps())
    assert tools.calls == []
    assert update["tool_errors"] == [ERR_NO_PRODUCT]


async def test_several_matches_stop_at_the_search_and_lead_to_a_question(tools):
    tools.install(TOOL_SEARCH, search_many())
    tools.install(TOOL_PRICE, {"match": "one"})
    state = turn(Intent.PRICE, product_query="GA-2100")

    update = await run_tools_node(state, deps())

    assert tools.names == [TOOL_SEARCH]  # no price is asked for a product nobody chose
    assert update["conversation"].candidate_product_ids == [PID, OTHER]
    assert update["conversation"].current_product_id is None
    assert route_after_tools({**state, **update}) == "clarify"


async def test_no_match_leads_to_a_question(tools):
    tools.install(TOOL_SEARCH, {"kind": "none", "candidates": []})
    state = turn(Intent.PRICE, product_query="zzz")
    update = await run_tools_node(state, deps())
    assert tools.names == [TOOL_SEARCH]
    assert route_after_tools({**state, **update}) == "clarify"


async def test_a_failed_search_is_recorded_and_nothing_else_runs(tools):
    tools.install(TOOL_SEARCH, None, ok=False, error_code="shopify_unavailable")
    tools.install(TOOL_PRICE, {"match": "one"})
    state = turn(Intent.PRICE, product_query="GA-2100")

    update = await run_tools_node(state, deps())

    assert tools.names == [TOOL_SEARCH]
    assert update["tool_errors"] == ["shopify_unavailable"]
    assert route_after_tools({**state, **update}) == "compose"  # the composer says "cannot check"


async def test_an_unknown_product_stops_being_the_current_product(tools):
    tools.install(TOOL_PRICE, None, ok=False, error_code="not_found")
    state = turn(Intent.PRICE, conversation=conv(current_product_id=PID))

    update = await run_tools_node(state, deps())

    assert update["conversation"].current_product_id is None
    assert update["tool_errors"] == ["not_found"]


async def test_the_node_works_on_a_copy_of_the_conversation(tools):
    tools.install(TOOL_SEARCH, search_one())
    tools.install(TOOL_PRICE, {"match": "one"})
    original = conv()
    state = turn(Intent.PRICE, product_query="GA-2100", conversation=original)

    await run_tools_node(state, deps())

    assert original.current_product_id is None
    assert original.candidate_product_ids == []


# ---- tools: pictures


async def test_one_picture_by_default_three_when_the_customer_wants_more(tools):
    tools.install(TOOL_IMAGE, {"images": []})
    one = turn(Intent.IMAGE_REQUEST, text="photo bhejo", conversation=conv(current_product_id=PID))
    more = turn(
        Intent.IMAGE_REQUEST, text="aur pictures hain?", conversation=conv(current_product_id=PID)
    )

    await image_request_node(one, deps())
    await image_request_node(more, deps())

    assert [kwargs["max_images"] for _, kwargs in tools.calls] == [1, 3]


async def test_the_picture_tool_gets_the_colour(tools):
    tools.install(TOOL_IMAGE, {"images": []})
    state = turn(Intent.IMAGE_REQUEST, color="black", conversation=conv(current_product_id=PID))
    await image_request_node(state, deps())
    assert tools.args(TOOL_IMAGE)["colour"] == "black"


# ---- tools: policy


async def test_a_policy_question_calls_search_policy_with_the_doc_type_of_the_intent(tools):
    tools.install(TOOL_POLICY, {"abstain": False, "passages": []})
    embedder = FakeEmbedder()
    state = turn(Intent.POLICY_REFUND, text="paisay wapas kaise hon", kb_query_en="refund policy")

    update = await policy_rag_node(state, deps(embedder=embedder))

    assert tools.names == [TOOL_POLICY]
    sent = tools.args(TOOL_POLICY)
    assert sent["query_en"] == "refund policy"
    assert sent["doc_type"] == "policy_refund"
    assert sent["original_query"] == "paisay wapas kaise hon"
    assert sent["embedder"] is embedder
    assert update["tool_calls_made"] == 1
    assert route_after_tools({**state, **update}) == "compose"


@pytest.mark.parametrize(
    ("intent", "doc_type"),
    [
        (Intent.POLICY_SHIPPING, "policy_shipping"),
        (Intent.POLICY_PRIVACY, "policy_privacy"),
        (Intent.FAQ, "faq"),
    ],
)
async def test_doc_type_follows_the_intent(tools, intent, doc_type):
    tools.install(TOOL_POLICY, {"abstain": True, "passages": []})
    await policy_rag_node(turn(intent, kb_query_en="x"), deps())
    assert tools.args(TOOL_POLICY)["doc_type"] == doc_type


async def test_a_policy_question_without_an_english_query_still_searches(tools):
    tools.install(TOOL_POLICY, {"abstain": False, "passages": []})
    await policy_rag_node(turn(Intent.POLICY_REFUND, text="refund?"), deps())
    sent = tools.args(TOOL_POLICY)
    assert sent["query_en"] == ""  # the tool then uses the customer's own words
    assert sent["original_query"] == "refund?"


async def test_an_abstention_goes_to_the_composer_not_to_a_question(tools):
    tools.install(TOOL_POLICY, {"abstain": True, "passages": []})
    state = turn(Intent.POLICY_SHIPPING, kb_query_en="delivery to mars")
    update = await policy_rag_node(state, deps())
    assert route_after_tools({**state, **update}) == "compose"


# ---- intents that never run a tool


@pytest.mark.parametrize(
    "intent",
    [
        Intent.GREETING,
        Intent.OUT_OF_SCOPE,
        Intent.UNCLEAR,
        Intent.ABUSE_OR_INJECTION,
        Intent.PRIVATE_DATA_REQUEST,
        Intent.HUMAN_REQUEST,
        Intent.COMPLAINT,
    ],
)
async def test_these_intents_run_no_tool_in_this_node(tools, intent):
    update = await run_tools_node(
        turn(intent, product_query="GA-2100", conversation=conv(current_product_id=PID)), deps()
    )
    assert tools.calls == []
    assert update["tool_results"] == []
    assert update["tool_calls_made"] == 0


async def test_without_a_router_result_no_tool_runs(tools):
    update = await run_tools_node(new_turn(1, "x", conv()), deps())
    assert tools.calls == []
    assert update == {"tool_errors": [ERR_NO_ROUTER]}


# ---- caps and the gate inside the node


async def test_no_more_than_the_cap_of_tool_calls_per_turn(tools):
    tools.install(TOOL_PRICE, {"match": "one"})
    state = turn(Intent.PRICE, conversation=conv(current_product_id=PID))
    state["tool_calls_made"] = MAX_TOOL_CALLS_PER_TURN

    update = await run_tools_node(state, deps())

    assert tools.calls == []
    assert update["tool_errors"] == [ERR_CAP]
    assert update["tool_calls_made"] == MAX_TOOL_CALLS_PER_TURN


async def test_the_node_keeps_results_and_errors_of_earlier_nodes(tools):
    tools.install(TOOL_PRICE, {"match": "one"})
    earlier = result(TOOL_SEARCH, search_one())
    state = turn(Intent.PRICE, conversation=conv(current_product_id=PID))
    state["tool_results"] = [earlier]
    state["tool_calls_made"] = 1
    state["tool_errors"] = ["something"]

    update = await run_tools_node(state, deps())

    assert [r.tool for r in update["tool_results"]] == [TOOL_SEARCH, TOOL_PRICE]
    assert update["tool_calls_made"] == 2
    assert update["tool_errors"] == ["something"]


async def test_a_denied_call_is_not_run(tools):
    tools.install(TOOL_PRICE, {"match": "one"})
    run = nodes._ToolRun(deps(), Intent.PRIVATE_DATA_REQUEST, conv(current_product_id=PID), 0)

    assert await run.call(TOOL_PRICE, product_id=PID) is None

    assert tools.calls == []
    assert run.errors == [ERR_DENIED]
    assert run.calls_made == 0  # a denied call does not use up the cap


async def test_a_product_id_the_state_does_not_know_is_not_run(tools):
    tools.install(TOOL_PRICE, {"match": "one"})
    run = nodes._ToolRun(deps(), Intent.PRICE, conv(candidate_product_ids=[PID]), 0)

    assert await run.call(TOOL_PRICE, product_id=OTHER) is None

    assert tools.calls == []
    assert run.errors == [ERR_DENIED]


# ---- route_after_tools


def test_route_after_tools_composes_when_one_product_was_found():
    state = new_turn(1, "x", conv())
    state["tool_results"] = [result(TOOL_SEARCH, search_one()), result(TOOL_PRICE, {})]
    assert route_after_tools(state) == "compose"


def test_route_after_tools_composes_when_nothing_ran():
    assert route_after_tools(new_turn(1, "x", conv())) == "compose"


def test_route_after_tools_asks_when_the_search_was_ambiguous():
    state = new_turn(1, "x", conv())
    state["tool_results"] = [result(TOOL_SEARCH, search_many())]
    assert route_after_tools(state) == "clarify"


# ---- pictures: found pictures are the answer, no composer


def image_data(n: int = 1) -> dict:
    return {
        "product_id": PID,
        "title": "GA-2100",
        "product_url": "https://shop.example/products/ga-2100",
        "images": [{"url": f"https://cdn.example/{i}.jpg"} for i in range(n)],
    }


def test_route_after_tools_sends_the_pictures_when_the_tool_found_some():
    state = turn(Intent.IMAGE_REQUEST)
    state["tool_results"] = [result(TOOL_SEARCH, search_one()), result(TOOL_IMAGE, image_data())]
    assert route_after_tools(state) == "pictures"


def test_route_after_tools_composes_when_the_product_has_no_picture():
    state = turn(Intent.IMAGE_REQUEST)
    state["tool_results"] = [result(TOOL_SEARCH, search_one()), result(TOOL_IMAGE, image_data(0))]
    assert route_after_tools(state) == "compose"  # the composer says: no picture, here is the link


def test_route_after_tools_composes_when_the_picture_tool_failed():
    state = turn(Intent.IMAGE_REQUEST)
    state["tool_results"] = [result(TOOL_IMAGE, None, ok=False, error_code="not_found")]
    assert route_after_tools(state) == "compose"


def test_a_picture_result_only_counts_for_an_image_request():
    state = turn(Intent.PRICE)
    state["tool_results"] = [result(TOOL_IMAGE, image_data())]
    assert route_after_tools(state) == "compose"


async def test_pictures_node_writes_a_template_line_and_keeps_the_source_ids():
    state = turn(Intent.IMAGE_REQUEST)
    state["tool_results"] = [result(TOOL_SEARCH, search_one()), result(TOOL_IMAGE, image_data())]

    response = (await pictures_node(state))["response"]

    assert response.intent is Intent.IMAGE_REQUEST
    assert "GA-2100" in response.response_text
    assert response.source_ids == [f"{TOOL_SEARCH}:test", f"{TOOL_IMAGE}:test"]
    assert response.needs_clarification is False and response.needs_handoff is False


# ---- one product with several colours: ask which one, no composer


def several_colours() -> dict:
    names = ["BLACK", "BLUE", "RED", "SKIN"]
    return {
        "match": "ambiguous",
        "product_id": PID,
        "title": "Casio MTP-1374D Classic",
        "variants": [{"variant_id": f"v{n}", "title": name} for n, name in enumerate(names)],
    }


@pytest.mark.parametrize("tool", [TOOL_PRICE, TOOL_STOCK])
def test_route_after_tools_asks_when_price_or_stock_needs_a_colour(tool):
    state = turn(Intent.PRICE)
    state["tool_results"] = [result(TOOL_SEARCH, search_one()), result(tool, several_colours())]
    assert route_after_tools(state) == "clarify"


async def test_clarify_node_lists_the_colours_of_the_one_product():
    state = turn(Intent.PRICE)
    state["reply_language"] = "roman_ur"
    found = result(TOOL_SEARCH, search_one())
    state["tool_results"] = [found, result(TOOL_PRICE, several_colours())]

    update = await clarify_node(state)

    response = update["response"]
    assert "Casio MTP-1374D Classic" in response.response_text
    assert "BLACK, BLUE, RED, SKIN" in response.response_text
    assert response.needs_clarification is True
    assert update["conversation"].clarification_count == 1


async def test_clarify_node_lists_at_most_the_option_cap():
    data = several_colours()
    data["variants"] = [{"variant_id": f"v{n}", "title": f"C{n}"} for n in range(30)]
    state = turn(Intent.PRICE)
    state["tool_results"] = [result(TOOL_PRICE, data)]

    update = await clarify_node(state)

    text = update["response"].response_text
    assert "C7" in text and "C8" not in text  # the first MAX_OPTIONS (8) options, not all 30
