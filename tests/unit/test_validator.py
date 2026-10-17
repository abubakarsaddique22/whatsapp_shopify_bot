"""Phase 9, step 6: the output validator (Blueprint 8.7) and the retry route."""

from datetime import UTC, datetime

from app.agent.nodes import route_after_validate
from app.agent.validator import ValidationContext, validate
from app.domain.models import BotResponse, ImageSelection, Intent, Lang, ToolResult

PID = "gid://shopify/Product/111"
VID = "gid://shopify/ProductVariant/222"
STORE = "store.example"
IMAGE = "https://cdn.example/ga2100.jpg"
POLICY_URL = "https://store.example/policies/refund-policy"


def _tool(tool: str, data: dict, source_id: str = "t:1") -> ToolResult:
    return ToolResult(
        tool=tool, ok=True, data=data, error_code=None, source_id=source_id,
        fetched_at=datetime.now(UTC),
    )  # fmt: skip


PRICE = _tool(
    "get_price",
    {
        "match": "one",
        "product_id": PID,
        "title": "Casio GA-2100 Black",
        "variant_id": VID,
        "price": {
            "amount": "12500.00",
            "currency": "PKR",
            "display": "Rs 12,500",
            "compare_at_display": None,
            "on_sale": False,
        },
    },
)
IMAGES = _tool(
    "get_product_image",
    {"product_id": PID, "title": "Casio GA-2100 Black", "images": [{"url": IMAGE}]},
)
POLICY = _tool(
    "search_policy",
    {
        "abstain": False,
        "passages": [
            {
                "source_id": "chunk:1",
                "heading": "Returns",
                "text": "You can return an item within 7 days. Delivery costs Rs 250.",
                "url": POLICY_URL,
            }
        ],
    },
)


def _ctx(*results: ToolResult, lang="en", intent=Intent.PRICE, text="price?", secrets=()):
    return ValidationContext(
        reply_lang=lang, intent=intent, results=list(results), customer_text=text,
        store_domain=STORE, secrets=secrets,
    )  # fmt: skip


def _reply(text: str, **kwargs) -> BotResponse:
    return BotResponse(detected_language=Lang.EN, intent=Intent.PRICE, response_text=text, **kwargs)


def _codes(problems: list[str]) -> set[str]:
    return {p.split(":", 1)[0] for p in problems}


# ---- a good reply


def test_good_reply_passes():
    reply = _reply("The Casio GA-2100 Black costs Rs 12,500.", product_ids=[PID])
    assert validate(reply, _ctx(PRICE)) == []


def test_policy_numbers_and_fee_are_allowed():
    reply = _reply("You can return it within 7 days. Delivery costs Rs 250.")
    assert validate(reply, _ctx(POLICY, intent=Intent.POLICY_REFUND)) == []


# ---- numbers and prices


def test_invented_price_is_rejected():
    problems = validate(_reply("It costs Rs 13,000."), _ctx(PRICE))
    assert _codes(problems) == {"price"}
    assert "13000" in problems[0]


def test_price_without_a_price_tool_is_rejected():
    assert "price" in _codes(validate(_reply("It costs Rs 12,500."), _ctx()))


def test_invented_number_is_rejected():
    problems = validate(_reply("It is water resistant to 200 m."), _ctx(PRICE))
    assert "numbers" in _codes(problems)


def test_a_number_from_the_customer_is_allowed():
    reply = _reply("Yes, I can look at 44 mm models.")
    assert validate(reply, _ctx(PRICE, text="do you have 44mm?")) == []


# ---- urls


def test_store_link_and_tool_urls_are_allowed():
    ok = _reply(f"Here it is: {IMAGE} and https://store.example/products/ga-2100")
    assert validate(ok, _ctx(IMAGES)) == []


def test_policy_source_url_is_allowed():
    reply = _reply(f"Details: {POLICY_URL}")
    assert validate(reply, _ctx(POLICY, intent=Intent.POLICY_REFUND)) == []


def test_foreign_link_is_rejected():
    reply = _reply("Pay here: https://evil.example/pay")
    assert "url" in _codes(validate(reply, _ctx(PRICE)))


# ---- colours


def test_colour_from_the_title_is_allowed_other_colour_is_not():
    assert validate(_reply("It is the black one."), _ctx(PRICE)) == []
    assert "colour" in _codes(validate(_reply("It comes in red."), _ctx(PRICE)))


def test_colour_the_customer_asked_may_be_named_when_not_offered():
    none = _tool(
        "get_price",
        {"match": "none", "reason": "colour_not_offered", "colours_offered": ["black"]},
    )
    reply = _reply("Sorry, we do not have it in red. We have black.")
    assert validate(reply, _ctx(none, text="red colour price?")) == []


# ---- ids


def test_unknown_product_id_is_rejected():
    reply = _reply("Here you go.", product_ids=["gid://shopify/Product/999"])
    assert "id" in _codes(validate(reply, _ctx(PRICE)))


def test_image_must_come_from_a_tool():
    good = ImageSelection(product_id=PID, image_url=IMAGE)
    bad = ImageSelection(product_id=PID, image_url="https://cdn.example/other.jpg")
    assert validate(_reply("Here.", image=good), _ctx(IMAGES)) == []
    assert "id" in _codes(validate(_reply("Here.", image=bad), _ctx(IMAGES)))


# ---- language, length


def test_wrong_language_is_rejected():
    english = "Thank you, the watch is available and we will send it today."
    assert "language" in _codes(validate(_reply(english), _ctx(PRICE, lang="roman_ur")))


def test_too_long_and_too_many_questions_are_rejected():
    long_reply = _reply(" ".join(["This is a short sentence."] * 7))
    assert "length" in _codes(validate(long_reply, _ctx()))
    assert "length" in _codes(validate(_reply("Which one? Which colour?"), _ctx()))


# ---- leaks


def test_tool_name_gid_and_phone_are_leaks():
    for text in (
        "I used search_products to check.",
        f"The id is {PID}.",
        "Call 0300 1234567 now.",
        "Contact me at someone@example.com.",
    ):
        assert "leak" in _codes(validate(_reply(text), _ctx(PRICE))), text


def test_secret_value_is_a_leak_and_is_not_echoed():
    reply = _reply("The key is sk-livesecret123456.")
    problems = validate(reply, _ctx(secrets=("sk-livesecret123456",)))
    assert "leak" in _codes(problems)
    assert all("sk-livesecret" not in p for p in problems)


def test_copying_the_prompt_is_a_leak():
    text = "Never state a price, stock level, colour, size, dimension, delivery time"
    assert "leak" in _codes(validate(_reply(text), _ctx()))


# ---- private data


def test_private_data_reply_has_no_order_data():
    ctx = _ctx(intent=Intent.PRIVATE_DATA_REQUEST, text="where is my order")
    assert "private_data" in _codes(validate(_reply("Your order 12345 is on the way."), ctx))
    assert validate(_reply("Our team can help you with that."), ctx) == []


# ---- retry route (one retry, then safe_fallback)


def _state(response, errors, attempts):
    return {"response": response, "validation_errors": errors, "compose_attempts": attempts}


def test_route_after_validate():
    reply = _reply("Hello.")
    assert route_after_validate(_state(reply, [], 1)) == "done"
    assert route_after_validate(_state(reply, ["price: x"], 1)) == "retry"
    assert route_after_validate(_state(reply, ["price: x"], 2)) == "safe_fallback"
    assert route_after_validate(_state(None, [], 1)) == "safe_fallback"
