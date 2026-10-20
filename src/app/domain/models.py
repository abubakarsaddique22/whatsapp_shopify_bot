"""Shared Pydantic contracts (Blueprint 3.8). Names are the contract between phases."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Lang(StrEnum):
    EN = "en"
    ROMAN_UR = "roman_ur"
    UR = "ur"  # Urdu script
    PA = "pa"  # Punjabi (usually from voice)
    MIXED = "mixed"
    UNKNOWN = "unknown"


class Intent(StrEnum):
    PRODUCT_SEARCH = "product_search"
    PRICE = "price"
    AVAILABILITY = "availability"
    COLORS_SIZES = "colors_sizes"
    SPECS = "specs"
    IMAGE_REQUEST = "image_request"
    POLICY_REFUND = "policy_refund"
    POLICY_SHIPPING = "policy_shipping"
    POLICY_PRIVACY = "policy_privacy"
    FAQ = "faq"
    GREETING = "greeting"
    HUMAN_REQUEST = "human_request"
    COMPLAINT = "complaint"
    PRIVATE_DATA_REQUEST = "private_data_request"  # orders, other customers, payments
    OUT_OF_SCOPE = "out_of_scope"
    ABUSE_OR_INJECTION = "abuse_or_injection"
    UNCLEAR = "unclear"


class RouterOutput(BaseModel):
    language: Lang
    intent: Intent
    product_query: str | None = Field(None, max_length=120)
    color: str | None = Field(None, max_length=40)
    size_mm: int | None = Field(None, ge=10, le=80)
    max_price: int | None = Field(None, ge=0)
    kb_query_en: str | None = Field(None, max_length=200)
    needs_clarification: bool = False
    confidence: float = Field(ge=0.0, le=1.0)


class ImageSelection(BaseModel):
    product_id: str  # Shopify global id, copied from a tool result
    variant_id: str | None = None
    image_url: str  # must come from a tool result, never from the LLM
    caption: str = Field("", max_length=200)


class BotResponse(BaseModel):
    detected_language: Lang
    intent: Intent
    response_text: str = Field(min_length=1, max_length=1000)
    product_ids: list[str] = []
    image: ImageSelection | None = None
    needs_clarification: bool = False
    needs_handoff: bool = False
    source_ids: list[str] = []  # internal only: chunk ids and tool call ids used

    @field_validator("response_text")
    @classmethod
    def no_empty_or_control(cls, v: str) -> str:
        v = "".join(ch for ch in v if ch.isprintable() or ch in "\n\t").strip()
        if not v:
            raise ValueError("empty reply")
        return v


class ToolResult(BaseModel):
    tool: str
    ok: bool
    data: dict | list | None = None
    error_code: str | None = None  # e.g. "shopify_unavailable", "not_found", "hidden"
    source_id: str  # used for audit and by the validator
    fetched_at: datetime


class ConversationState(BaseModel):
    conversation_id: str
    sender_hash: str  # keyed hash of the phone number, never the raw number
    language: Lang = Lang.UNKNOWN
    current_product_id: str | None = None
    current_variant_id: str | None = None
    candidate_product_ids: list[str] = []  # ids returned by earlier tool results
    last_router_output: RouterOutput | None = None
    tool_results: list[ToolResult] = []
    clarification_count: int = 0
    needs_handoff: bool = False
    bot_paused_until: datetime | None = None
    # Phase 11: why a person is needed (set by the handoff node, cleared on resume), and the pause
    # the customer was already told about with ONE 'our team has your request' (then silence)
    handoff_reason: str | None = None
    pause_acked_for: datetime | None = None
    # P8 owner first: the owner answered this customer from the WhatsApp Business app, so the bot
    # stays silent for this customer until then (and sends no notice: every bot message costs)
    owner_active_until: datetime | None = None
    history: list[dict[str, str]] = []  # short rolling window: {"role": ..., "text": ...}
