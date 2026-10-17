"""Phase 9, step 2: the permission gate (agent/gate.py). No database, no model, no network."""

import pytest
from pydantic import BaseModel

from app.agent.gate import (
    ALLOWED_TOOLS,
    INTENT_BRANCH,
    MAX_IMAGES,
    MAX_SEARCH_LIMIT,
    POLICY_DOC_TYPE,
    TOOL_HANDOFF,
    choose_branch,
    gate,
)
from app.agent.state import BRANCHES, HANDOFF_AFTER_CLARIFICATIONS
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
from app.core.errors import PermissionDenied
from app.domain.models import ConversationState, Intent, Lang, RouterOutput

PID = "gid://shopify/Product/1"
OTHER = "gid://shopify/Product/2"


def conv(**kw) -> ConversationState:
    return ConversationState(conversation_id="c1", sender_hash="h1", **kw)


def router(intent: Intent) -> RouterOutput:
    return RouterOutput(language=Lang.EN, intent=intent, confidence=0.9)


# ---- the tables are complete


def test_every_intent_has_an_allowed_tools_entry():
    assert set(ALLOWED_TOOLS) == set(Intent)


def test_every_intent_has_a_branch():
    assert set(INTENT_BRANCH) == set(Intent)
    assert set(INTENT_BRANCH.values()) <= set(BRANCHES)


def test_allowed_tools_are_real_tools():
    known = set(TOOLS) | {TOOL_HANDOFF}
    for intent, tools in ALLOWED_TOOLS.items():
        assert tools <= known, intent


def test_policy_intents_have_a_doc_type():
    for intent in (Intent.POLICY_REFUND, Intent.POLICY_SHIPPING, Intent.POLICY_PRIVACY, Intent.FAQ):
        assert ALLOWED_TOOLS[intent] == {TOOL_POLICY}
        assert intent in POLICY_DOC_TYPE


# ---- which tool may run


def test_a_tool_that_is_on_the_list_is_allowed():
    gate(Intent.PRICE, TOOL_SEARCH, {"query": "GA-2100"}, conv())
    gate(Intent.PRICE, TOOL_PRICE, {"product_id": PID}, conv(current_product_id=PID))
    gate(Intent.AVAILABILITY, TOOL_STOCK, {"product_id": PID}, conv(candidate_product_ids=[PID]))
    gate(Intent.SPECS, TOOL_SPECS, {"product_id": PID}, conv(current_product_id=PID))
    gate(Intent.COLORS_SIZES, TOOL_OPTIONS, {"product_id": PID}, conv(current_product_id=PID))


@pytest.mark.parametrize(
    ("intent", "tool"),
    [
        (Intent.PRICE, TOOL_POLICY),
        (Intent.PRICE, TOOL_STOCK),  # price may not call the stock tool
        (Intent.AVAILABILITY, TOOL_PRICE),
        (Intent.POLICY_REFUND, TOOL_PRICE),
        (Intent.POLICY_REFUND, TOOL_SEARCH),
        (Intent.IMAGE_REQUEST, TOOL_PRICE),
        (Intent.GREETING, TOOL_SEARCH),
        (Intent.UNCLEAR, TOOL_SEARCH),
        (Intent.OUT_OF_SCOPE, TOOL_POLICY),
    ],
)
def test_a_tool_that_is_not_on_the_list_is_denied(intent, tool):
    with pytest.raises(PermissionDenied):
        gate(intent, tool, {}, conv(current_product_id=PID))


def test_private_data_request_may_never_use_a_data_tool():
    state = conv(current_product_id=PID, candidate_product_ids=[PID])
    for tool in TOOLS:
        with pytest.raises(PermissionDenied):
            gate(Intent.PRIVATE_DATA_REQUEST, tool, {"product_id": PID}, state)
    gate(Intent.PRIVATE_DATA_REQUEST, TOOL_HANDOFF, {}, state)  # only a handoff is possible


def test_abuse_or_injection_may_use_nothing():
    state = conv(current_product_id=PID, candidate_product_ids=[PID])
    for tool in [*TOOLS, TOOL_HANDOFF]:
        with pytest.raises(PermissionDenied):
            gate(Intent.ABUSE_OR_INJECTION, tool, {}, state)


def test_an_unknown_tool_name_is_denied():
    with pytest.raises(PermissionDenied):
        gate(Intent.PRICE, "delete_everything", {}, conv())


# ---- product ids come from tool results


def test_a_product_id_from_an_earlier_search_is_allowed():
    gate(Intent.PRICE, TOOL_PRICE, {"product_id": PID}, conv(candidate_product_ids=[PID, OTHER]))


def test_the_product_being_discussed_is_allowed():
    gate(Intent.PRICE, TOOL_PRICE, {"product_id": PID}, conv(current_product_id=PID))


def test_a_product_id_that_no_tool_returned_is_denied():
    with pytest.raises(PermissionDenied):
        gate(Intent.PRICE, TOOL_PRICE, {"product_id": OTHER}, conv(candidate_product_ids=[PID]))


def test_an_invented_id_is_denied_with_an_empty_state():
    with pytest.raises(PermissionDenied):
        gate(Intent.PRICE, TOOL_PRICE, {"product_id": PID}, conv())


@pytest.mark.parametrize("bad", [None, 123, ["x"]])
def test_a_product_id_of_the_wrong_type_is_denied(bad):
    with pytest.raises(PermissionDenied):
        gate(Intent.PRICE, TOOL_PRICE, {"product_id": bad}, conv(current_product_id=PID))


def test_arguments_can_be_a_pydantic_model():
    class Args(BaseModel):
        product_id: str

    gate(Intent.PRICE, TOOL_PRICE, Args(product_id=PID), conv(current_product_id=PID))
    with pytest.raises(PermissionDenied):
        gate(Intent.PRICE, TOOL_PRICE, Args(product_id=OTHER), conv(current_product_id=PID))


# ---- limits


def test_search_limit_is_at_most_five():
    gate(Intent.PRODUCT_SEARCH, TOOL_SEARCH, {"query": "x", "limit": MAX_SEARCH_LIMIT}, conv())
    for bad in (MAX_SEARCH_LIMIT + 1, 0, -1):
        with pytest.raises(PermissionDenied):
            gate(Intent.PRODUCT_SEARCH, TOOL_SEARCH, {"query": "x", "limit": bad}, conv())


def test_at_most_three_images():
    state = conv(current_product_id=PID)
    gate(Intent.IMAGE_REQUEST, TOOL_IMAGE, {"product_id": PID, "max_images": MAX_IMAGES}, state)
    gate(Intent.IMAGE_REQUEST, TOOL_IMAGE, {"product_id": PID}, state)  # default is one
    for bad in (MAX_IMAGES + 1, 0):
        with pytest.raises(PermissionDenied):
            gate(Intent.IMAGE_REQUEST, TOOL_IMAGE, {"product_id": PID, "max_images": bad}, state)


@pytest.mark.parametrize(("intent", "doc_type"), list(POLICY_DOC_TYPE.items()))
def test_search_policy_needs_the_doc_type_of_the_intent(intent, doc_type):
    gate(intent, TOOL_POLICY, {"query_en": "x", "doc_type": doc_type}, conv())
    with pytest.raises(PermissionDenied):
        gate(intent, TOOL_POLICY, {"query_en": "x", "doc_type": "page"}, conv())
    with pytest.raises(PermissionDenied):
        gate(intent, TOOL_POLICY, {"query_en": "x"}, conv())  # no filter at all


# ---- the branch after the router


@pytest.mark.parametrize("intent", [i for i in Intent if i is not Intent.UNCLEAR])
def test_choose_branch_follows_the_table(intent):
    assert choose_branch(router(intent), conv()) == INTENT_BRANCH[intent]


def test_branches_for_the_main_intents():
    assert choose_branch(router(Intent.PRICE), conv()) == "product_info"
    assert choose_branch(router(Intent.IMAGE_REQUEST), conv()) == "image_request"
    assert choose_branch(router(Intent.POLICY_REFUND), conv()) == "policy_rag"
    assert choose_branch(router(Intent.HUMAN_REQUEST), conv()) == "handoff"
    assert choose_branch(router(Intent.COMPLAINT), conv()) == "handoff"
    assert choose_branch(router(Intent.PRIVATE_DATA_REQUEST), conv()) == "refuse"
    assert choose_branch(router(Intent.ABUSE_OR_INJECTION), conv()) == "refuse"
    assert choose_branch(router(Intent.OUT_OF_SCOPE), conv()) == "refuse"
    assert choose_branch(router(Intent.GREETING), conv()) == "compose"


def test_an_unclear_message_gets_a_question_first():
    assert choose_branch(router(Intent.UNCLEAR), conv(clarification_count=0)) == "clarify"
    assert choose_branch(router(Intent.UNCLEAR), conv(clarification_count=1)) == "clarify"


def test_the_third_unclear_message_goes_to_a_person():
    count = HANDOFF_AFTER_CLARIFICATIONS - 1  # two questions were already asked
    assert choose_branch(router(Intent.UNCLEAR), conv(clarification_count=count)) == "handoff"
    assert choose_branch(router(Intent.UNCLEAR), conv(clarification_count=count + 3)) == "handoff"


def test_a_clear_message_is_not_handed_off_because_of_old_clarifications():
    assert choose_branch(router(Intent.PRICE), conv(clarification_count=5)) == "product_info"
