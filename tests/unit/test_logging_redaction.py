import json
import logging

from app.core.logging import JsonFormatter, RedactionFilter, redact


def test_redacts_tokens_and_phone():
    assert "shpat_abcdef123456" not in redact("token shpat_abcdef123456 here")
    assert "923001234567" not in redact("from 923001234567 today")
    assert "Bearer [REDACTED]" in redact("Authorization: Bearer abc.def.ghi")
    assert "sk-abcdefghijkl" not in redact("key sk-abcdefghijkl")


def test_normal_text_untouched():
    assert redact("price is Rs 12,500") == "price is Rs 12,500"


def test_a_shopify_id_is_kept_so_logs_can_be_debugged():
    gid = "gid://shopify/Product/9876543210123"
    assert redact(f"variants truncated for {gid}") == f"variants truncated for {gid}"
    assert redact("item gid://shopify/InventoryItem/12345678901234") == (
        "item gid://shopify/InventoryItem/12345678901234"
    )


def test_a_phone_number_next_to_a_shopify_id_is_still_redacted():
    text = redact("gid://shopify/Product/9876543210123 asked by 923001234567")
    assert "gid://shopify/Product/9876543210123" in text
    assert "923001234567" not in text


def test_a_nul_character_in_the_input_cannot_fake_a_placeholder():
    assert redact("x \x000\x00 y") == "x 0 y"


def test_card_numbers_are_removed_from_logs_completely():
    # the phone pattern alone used to leave the last digits of a spaced card number
    assert redact("card 4111 1111 1111 1111 ok") == "card [CARD] ok"
    assert redact("card 4111111111111111 ok") == "card [CARD] ok"
    assert "1111" not in redact("paid with 4111-1111-1111-1111")


def test_a_card_next_to_a_shopify_id_and_a_phone_number():
    text = redact("gid://shopify/Product/9876543210123 card 4111 1111 1111 1111 from 923001234567")
    assert "gid://shopify/Product/9876543210123" in text
    assert "[CARD]" in text
    assert "[PHONE]" in text
    assert "4111" not in text
    assert "923001234567" not in text


def test_json_line_has_correlation_id_and_is_redacted():
    handler_filter = RedactionFilter()
    record = logging.LogRecord("t", logging.INFO, "", 0, "user 923001234567 asked", (), None)
    handler_filter.filter(record)
    line = json.loads(JsonFormatter().format(record))
    assert "correlation_id" in line
    assert "923001234567" not in line["event"]
