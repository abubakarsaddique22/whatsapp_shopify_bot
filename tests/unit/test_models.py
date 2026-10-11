import pytest
from pydantic import ValidationError

from app.domain.models import BotResponse, Intent, Lang, RouterOutput


def test_router_output_valid():
    r = RouterOutput(language=Lang.ROMAN_UR, intent=Intent.PRICE, confidence=0.9, max_price=10000)
    assert r.intent is Intent.PRICE


def test_router_output_rejects_bad_confidence():
    with pytest.raises(ValidationError):
        RouterOutput(language=Lang.EN, intent=Intent.PRICE, confidence=1.5)


def test_bot_response_rejects_empty_text():
    with pytest.raises(ValidationError):
        BotResponse(detected_language=Lang.EN, intent=Intent.GREETING, response_text="   ")
