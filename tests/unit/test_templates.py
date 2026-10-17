import pytest

from app.agent.templates import load_templates, render

LANGS = ("en", "roman_ur", "en_roman_ur")
KINDS = {
    "ask_which_product",
    "ask_product_name",
    "not_found",
    "ask_detail",
    "voice_unclear",
    "private_data",
    "abuse_or_injection",
    "out_of_scope",
    "handoff_created",
    "paused_ack",
    "safe_fallback",
    "shopify_unavailable",
    "pictures_sent",
    "picture_colour_missing",
    "ask_which_variant",
}


def test_every_kind_has_every_language():
    templates = load_templates()
    assert KINDS <= set(templates)
    for kind in KINDS:
        assert set(LANGS) <= set(templates[kind]), kind


@pytest.mark.parametrize("lang", ["mixed", "ur"])
def test_fallback_languages_render(lang):
    assert render("safe_fallback", lang)


def test_no_template_leaks_internals():
    for per_lang in load_templates().values():
        for text in per_lang.values():
            assert "create_handoff" not in text
            assert "gid://" not in text
            assert len(text) <= 300


def test_ask_which_product_inserts_names():
    assert "A or B" in render("ask_which_product", "en", names="A or B")


def test_picture_templates_insert_title_and_colour():
    for lang in LANGS:
        assert "GA-2100" in render("pictures_sent", lang, title="GA-2100")
        caption = render("picture_colour_missing", lang, title="GA-2100", colour="black")
        assert "GA-2100" in caption and "black" in caption
