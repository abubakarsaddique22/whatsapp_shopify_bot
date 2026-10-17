"""Phase 9, step 5: the composer. Facts in, one validated BotResponse out (Blueprint 8.3, 8.4).

    response = await compose(llm, text=..., intent=..., language=..., reply_lang=...,
                             results=state["tool_results"], history=conv.history,
                             show_exact_stock=False, feedback=state["validation_errors"])

What happens:
1. prompts/composer.md is the system message ({target_language}, {show_exact_stock} filled in).
2. The user message holds ONLY data, each part inside its own tag: <history>, <customer_message>,
   <tool_results> and <policy_passages> (best MAX_PASSAGES chunks, with their source ids). A tag
   written inside the data is removed, so a product title or a policy text cannot close a block.
3. The answer must be one JSON object that passes BotResponse. If it does not, the model gets ONE
   repair attempt that includes the problem (same rule as the router).
4. After parsing, the code, not the model, sets the fields that must be true: intent, language and
   source_ids. The validator (step 6) checks the rest: numbers, URLs, colours, ids, language.
5. `feedback` is the validator's message from a rejected reply: the retry (MAX_COMPOSE_RETRIES)
   gets it in <validator_feedback>.

Provider failure or two invalid answers: the function returns None and the graph sends the
templated safe_fallback. Nothing here runs a tool, sends a message or touches the database.
"""

import json
import logging
import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agent.state import HISTORY_WINDOW
from app.core.errors import ConfigError, ProviderError
from app.domain.models import BotResponse, Intent, Lang, ToolResult
from app.integrations.llm.base import ChatModel
from app.language.policy import ReplyLang

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "composer.md"
TOOL_POLICY_NAME = "search_policy"  # same value as agent.tools.TOOL_POLICY
TOOL_HANDOFF_NAME = "create_handoff"  # a handoff record is never a fact for the customer

MAX_PASSAGES = 4  # Blueprint 7: pass the best 3 or 4 chunks
MAX_PASSAGE_CHARS = 1200
MAX_HISTORY_CHARS = 300
MAX_REPAIR_ECHO_CHARS = 1000

# {target_language} in the prompt. The two plain codes are explained in the prompt itself.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "en",
    "roman_ur": "roman_ur",
    "mixed": "mixed (simple Roman Urdu, keep product words in English)",
    "ur": "ur (Urdu script)",
    "en_roman_ur": "en_roman_ur (one short English sentence, then one short Roman Urdu sentence)",
}

_DATA_TAGS = re.compile(
    r"</?\s*(?:customer_message|history|tool_results|policy_passages|validator_feedback)\s*>",
    re.IGNORECASE,
)


@lru_cache
def _template() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read {PROMPT_PATH.name}") from exc


def _clean(text: str) -> str:
    """Remove our own data tags from data, so no text can end a block early."""
    return _DATA_TAGS.sub("", text)


def system_prompt(reply_lang: ReplyLang, show_exact_stock: bool) -> str:
    # .replace and not .format: the answer format has braces.
    return (
        _template()
        .replace("{target_language}", LANGUAGE_NAMES[reply_lang])
        .replace("{show_exact_stock}", "yes" if show_exact_stock else "no")
    )


def _facts_and_passages(results: Sequence[ToolResult]) -> tuple[list[dict[str, Any]], list[dict]]:
    """Split tool results into <tool_results> items and <policy_passages> items."""
    facts: list[dict[str, Any]] = []
    passages: list[dict[str, Any]] = []
    for result in results:
        if result.tool == TOOL_HANDOFF_NAME:
            continue
        if result.tool == TOOL_POLICY_NAME and result.ok and isinstance(result.data, dict):
            if result.data.get("abstain") or not result.data.get("passages"):
                facts.append(
                    {
                        "tool": result.tool,
                        "ok": True,
                        "source_id": result.source_id,
                        "note": "no approved passage found: the policy answer is not confirmed",
                    }
                )
            for p in result.data.get("passages", [])[:MAX_PASSAGES]:
                passages.append(
                    {
                        "source_id": p["source_id"],
                        "heading": p.get("heading"),
                        "text": str(p.get("text", ""))[:MAX_PASSAGE_CHARS],
                        "url": p.get("url"),
                    }
                )
            continue
        facts.append(
            {
                "tool": result.tool,
                "ok": result.ok,
                "error_code": result.error_code,
                "source_id": result.source_id,
                "data": result.data,
            }
        )
    return facts, passages[:MAX_PASSAGES]


def _history_block(history: Sequence[dict[str, str]]) -> str:
    lines = [
        f"{item.get('role', '?')}: {str(item.get('text', ''))[:MAX_HISTORY_CHARS]}"
        for item in history[-HISTORY_WINDOW:]
    ]
    return "\n".join(lines) if lines else "(none)"


def build_user_message(
    text: str,
    results: Sequence[ToolResult],
    history: Sequence[dict[str, str]] = (),
    feedback: Sequence[str] = (),
) -> str:
    facts, passages = _facts_and_passages(results)
    # Clean each piece of DATA first, then wrap it in our own tags (which must stay).
    history_text = _clean(_history_block(history))
    customer_text = _clean(text)
    facts_json = _clean(json.dumps(facts, ensure_ascii=False, default=str))
    passages_json = _clean(json.dumps(passages, ensure_ascii=False, default=str))
    parts = [
        f"<history>\n{history_text}\n</history>",
        f"<customer_message>\n{customer_text}\n</customer_message>",
        f"<tool_results>\n{facts_json}\n</tool_results>",
        f"<policy_passages>\n{passages_json}\n</policy_passages>",
    ]
    message = "\n".join(parts)
    if feedback:
        problems = _clean("\n".join(f"- {item}" for item in feedback))
        message += f"\n<validator_feedback>\n{problems}\n</validator_feedback>"
    return message


def _parse(answer: str) -> BotResponse:
    """Take the JSON object out of the answer (a fence or a sentence around it is fine)."""
    start, end = answer.find("{"), answer.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object found in the answer")
    return BotResponse.model_validate(json.loads(answer[start : end + 1]))


def _problem(exc: ValueError) -> str:
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


def _source_ids(results: Sequence[ToolResult]) -> list[str]:
    """Audit ids of the tool calls and policy chunks that were shown to the model."""
    ids: list[str] = []
    for result in results:
        if result.tool == TOOL_HANDOFF_NAME:
            continue
        ids.append(result.source_id)
        if result.tool == TOOL_POLICY_NAME and isinstance(result.data, dict):
            ids.extend(p["source_id"] for p in result.data.get("passages", [])[:MAX_PASSAGES])
    return list(dict.fromkeys(ids))


async def compose(
    llm: ChatModel,
    *,
    text: str,
    intent: Intent,
    language: Lang,
    reply_lang: ReplyLang,
    results: Sequence[ToolResult],
    history: Sequence[dict[str, str]] = (),
    show_exact_stock: bool = False,
    feedback: Sequence[str] = (),
) -> BotResponse | None:
    """One BotResponse, or None when the provider failed or the answer stayed invalid."""
    system = system_prompt(reply_lang, show_exact_stock)
    user = build_user_message(text, results, history, feedback)

    prompt = user
    for attempt in (1, 2):
        try:
            answer = await llm.complete(system, prompt)
        except ProviderError as exc:
            detail = exc.detail or {}
            # Only the kind of failure is logged, never the message, the answer or the key.
            log.warning(
                "composer_provider_error attempt=%s reason=%s error_type=%s status=%s",
                attempt,
                exc.message,
                detail.get("error_type"),
                detail.get("status_code"),
            )
            return None
        try:
            parsed = _parse(answer)
        except ValueError as exc:
            log.warning("composer_invalid_output", extra={"attempt": attempt})
            prompt = _repair_prompt(user, answer, exc)
            continue
        return parsed.model_copy(
            update={
                "intent": intent,  # the router decided it, the model must not change it
                "detected_language": language,
                "source_ids": _source_ids(results),
            }
        )
    return None
