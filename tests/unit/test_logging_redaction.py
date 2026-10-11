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


def test_json_line_has_correlation_id_and_is_redacted():
    handler_filter = RedactionFilter()
    record = logging.LogRecord("t", logging.INFO, "", 0, "user 923001234567 asked", (), None)
    handler_filter.filter(record)
    line = json.loads(JsonFormatter().format(record))
    assert "correlation_id" in line
    assert "923001234567" not in line["event"]
