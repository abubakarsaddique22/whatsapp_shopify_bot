"""parse_question: what the rule-based `ask` understands (pure, no database, no Shopify)."""

import pytest

from app.agent.rules import Ask, parse_question


@pytest.mark.parametrize(
    ("text", "intent", "query"),
    [
        ("GA-2100 ka price?", Ask.PRICE, "ga 2100"),
        ("ga2100 kitne ka hai", Ask.PRICE, "ga2100"),
        ("what colours does ga-2100 come in", Ask.OPTIONS, "ga 2100"),
        ("ga2100 ka size?", Ask.OPTIONS, "ga2100"),  # 'size' alone = the size options
        ("ga2100 case size", Ask.SPECS, "ga2100"),  # 'case size' = the spec
        ("is it water resistant?", Ask.SPECS, ""),  # no product named: the CLI asks which one
        ("photo bhejo ga2100 ki", Ask.IMAGE, "ga2100"),
        ("casio g shock available?", Ask.STOCK, "casio g shock"),
        ("ga2100", Ask.SEARCH, "ga2100"),
    ],
)
def test_intent_and_product_words(text, intent, query):
    parsed = parse_question(text)
    assert parsed.intent is intent
    assert parsed.product_query == query


def test_roman_urdu_colour_word_is_a_colour_not_a_product_word():
    parsed = parse_question("zorbex tide kala")
    assert parsed.colour == "black"
    assert parsed.product_query == "zorbex tide"
    assert parsed.intent is Ask.STOCK  # a colour with a product = is that colour available


def test_colour_size_and_product_together():
    parsed = parse_question("casio g shock black 40mm available?")
    assert (parsed.colour, parsed.size_mm) == ("black", 40)
    assert parsed.product_query == "casio g shock"
    assert parsed.intent is Ask.STOCK


def test_size_written_with_a_space():
    parsed = parse_question("ga2100 40 mm")
    assert parsed.size_mm == 40
    assert parsed.product_query == "ga2100"


@pytest.mark.parametrize(
    "text",
    [
        "black watch under 10k",
        "10k se kam black watch",
        "10 hazar se kam black watch",
        "black watch under rs 10000",
    ],
)
def test_budget_with_a_black_colour_browses_by_the_filters(text):
    parsed = parse_question(text)
    assert parsed.intent is Ask.SEARCH
    assert parsed.product_query == "watch"
    assert parsed.colour == "black"
    assert parsed.max_price == 10000


def test_model_number_is_never_read_as_a_budget():
    parsed = parse_question("GA-2100 under 10000")
    assert parsed.max_price == 10000
    assert parsed.product_query == "ga 2100"
    assert parse_question("GA-2100").max_price is None


def test_black_colour_question_without_a_product_browses_black_watches():
    parsed = parse_question("ap ka pass black color hai?")
    assert parsed.colour == "black"
    assert parsed.intent is Ask.SEARCH
    assert parsed.product_query == "watch"


def test_more_pictures_is_remembered_only_for_image_questions():
    assert parse_question("ga2100 ki aur photos").more_images is True
    assert parse_question("ga2100 ki photo").more_images is False
    assert parse_question("ga2100 aur price").more_images is False


@pytest.mark.parametrize(
    "text",
    ["mera order kahan hai", "refund milta hai?", "delivery kab hogi", "track my parcel"],
)
def test_orders_and_other_non_product_questions_are_out_of_scope(text):
    assert parse_question(text).intent is Ask.OUT_OF_SCOPE
