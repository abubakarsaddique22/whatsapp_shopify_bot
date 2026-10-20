"""Phase 5, step 4: the router. One customer message in, one validated RouterOutput out.

    out = await route_intent(text, llm, last_language=state.language,
                             current_product_title=title)

What happens (Blueprint 6.2 layer 3, 8.2, 8.4):
1. normalize_input + detect_language give a cheap language hint; the hint goes into the prompt.
2. The model gets prompts/router.md and the message inside <customer_message> tags.
3. The answer must be one JSON object that passes RouterOutput. If it does not, the model gets
   ONE repair attempt that includes the validation problem.
4. Still invalid, or the provider failed: fall back to intent = unclear, needs_clarification =
   True. The bot then asks the customer politely; it never guesses and never crashes.
   Exception (Phase 12 failure drill llm_down): when the REAL provider is down
   (ProviderUnavailable), `route_intent_checked` also says so, and the graph tells the customer
   honestly that there is a technical problem and hands over to the team, instead of asking for
   more detail as if the message was unclear.
5. max_price is checked against the deterministic price parser (Latin script only).

This function never executes a tool. The permission gate decides what an intent may do.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from app.core.errors import ConfigError, ProviderError, ProviderUnavailable
from app.domain.models import Intent, Lang, RouterOutput
from app.integrations.llm.base import ChatModel
from app.language.detect import detect_language, normalize_input
from app.language.price_parser import parse_price

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "router.md"
CLOSING_TAG = "</customer_message>"
MAX_REPAIR_ECHO_CHARS = 1000  # how much of the bad answer is shown back to the model


@lru_cache
def _prompt_template() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read {PROMPT_PATH.name}") from exc


def _system_prompt(current_product_title: str | None, last_language: Lang, hint: str) -> str:
    # .replace and not .format: the prompt is full of JSON braces.
    return (
        _prompt_template()
        .replace("{current_product_title}", current_product_title or "none")
        .replace("{last_language}", last_language.value)
        .replace("{language_hint}", hint)
    )


def _unclear(language: Lang) -> RouterOutput:
    # The None fields are written out because mypy does not see the defaults of
    # `Field(None, ...)` in domain/models.py (it only understands `Field(default=None, ...)`).
    return RouterOutput(
        language=language,
        intent=Intent.UNCLEAR,
        product_query=None,
        color=None,
        size_mm=None,
        max_price=None,
        kb_query_en=None,
        needs_clarification=True,
        confidence=0.0,
    )


def _parse(answer: str) -> RouterOutput:
    """Take the JSON object out of the answer (a fence or a sentence around it is fine)."""
    start, end = answer.find("{"), answer.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object found in the answer")
    return RouterOutput.model_validate(json.loads(answer[start : end + 1]))


def _problem(exc: ValueError) -> str:
    """What was wrong, in one line, for the repair prompt (a pydantic error is a ValueError)."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
    return str(exc)


def _repair_prompt(user: str, answer: str, exc: ValueError) -> str:
    return (
        f"{user}\n\nYour previous answer was not valid.\n"
        f"Previous answer:\n{answer[:MAX_REPAIR_ECHO_CHARS]}\n"
        f"Problem: {_problem(exc)}\n"
        "Return ONLY the corrected JSON object."
    )


async def _ask(llm: ChatModel, system: str, user: str) -> RouterOutput | None:
    """Ask, validate, and allow one repair. None means: give up (the caller falls back)."""
    prompt = user
    for attempt in (1, 2):
        try:
            answer = await llm.complete(system, prompt)
        except ProviderError as exc:
            detail = exc.detail or {}
            # Only the kind of failure is logged (never the message, the answer or the key).
            log.warning(
                "router_provider_error attempt=%s reason=%s error_type=%s status=%s",
                attempt,
                exc.message,
                detail.get("error_type"),
                detail.get("status_code"),
            )
            if isinstance(exc, ProviderUnavailable):
                raise  # route_intent_checked reports it: the provider is down, not the message
            return None
        try:
            return _parse(answer)
        except ValueError as exc:
            log.warning("router_invalid_output", extra={"attempt": attempt})
            prompt = _repair_prompt(user, answer, exc)
    return None


def _check_price(out: RouterOutput, text: str) -> RouterOutput:
    """The parser is exact ('das hazar' = 10000), so its answer wins over the model's.

    No price in the message means the model invented one: drop it.
    """
    price = parse_price(text)
    if out.max_price == price:
        return out
    log.info("router_price_corrected")
    return out.model_copy(update={"max_price": price})


async def route_intent_checked(
    text: str,
    llm: ChatModel,
    *,
    last_language: Lang = Lang.UNKNOWN,
    current_product_title: str | None = None,
) -> tuple[RouterOutput, bool]:
    """(router output, provider_down). provider_down is True only when the real provider could
    not answer (ProviderUnavailable); the output is then the 'unclear' fallback."""
    clean = normalize_input(text)
    hint = detect_language(clean)
    if not clean:
        return _unclear(hint.language), False

    system = _system_prompt(
        current_product_title, last_language, f"{hint.language.value} (script: {hint.script})"
    )
    user = f"<customer_message>\n{clean.replace(CLOSING_TAG, '')}\n{CLOSING_TAG}"

    try:
        out = await _ask(llm, system, user)
    except ProviderUnavailable:
        return _unclear(hint.language), True
    if out is None:
        return _unclear(hint.language), False
    if hint.script == "latin":  # the parser does not read Urdu-script numbers
        out = _check_price(out, clean)
    return out, False


async def route_intent(
    text: str,
    llm: ChatModel,
    *,
    last_language: Lang = Lang.UNKNOWN,
    current_product_title: str | None = None,
) -> RouterOutput:
    """The router output only (evals, the debug reply, the language step)."""
    out, _ = await route_intent_checked(
        text, llm, last_language=last_language, current_product_title=current_product_title
    )
    return out
