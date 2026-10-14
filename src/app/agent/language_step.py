"""Phase 5, step 6: the language step for ONE customer message (Blueprint 6.1, 6.2, 8.2).

    result = await run_language_step(session, conversation_id, text, llm)
    reply = format_debug_reply(result)      # development only

What happens:
1. Load the conversation state (state.language = sticky language, state.last_router_output =
   what the router said about the PREVIOUS customer message).
2. route_intent() classifies this message (language + intent + fields).
3. policy.next_language() decides the sticky language after this message (an explicit request
   wins; two messages in a row switch the language; an unknown message changes nothing).
4. policy.reply_language() turns the sticky language into the language of the bot's reply.
5. The new state is saved. The caller owns the transaction (commit / rollback).

Nothing here sends a message and nothing here executes a tool. Phase 9 (LangGraph) will call
the same functions from its nodes; the debug reply is removed or stays behind DEBUG_REPLY.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.router import route_intent
from app.db import repositories as repo
from app.domain.models import Lang, RouterOutput
from app.integrations.llm.base import ChatModel
from app.language.policy import ReplyLang, next_language, reply_language


@dataclass(frozen=True)
class LanguageResult:
    router: RouterOutput  # what the router found in THIS message
    conversation_language: Lang  # sticky language after this message
    reply_language: ReplyLang  # language the bot must answer in


async def run_language_step(
    session: AsyncSession,
    conversation_id: int,
    text: str,
    llm: ChatModel,
    *,
    urdu_script_replies: bool = False,
) -> LanguageResult:
    state = await repo.load_state(session, conversation_id)
    previous = state.last_router_output.language if state.last_router_output else None

    out = await route_intent(text, llm, last_language=state.language)
    language = next_language(state.language, previous, out.language, text)

    state.language = language
    state.last_router_output = out
    await repo.save_state(session, conversation_id, state)

    return LanguageResult(
        router=out,
        conversation_language=language,
        reply_language=reply_language(language, urdu_script_replies=urdu_script_replies),
    )


def format_debug_reply(result: LanguageResult) -> str:
    """The development-only reply: what the bot understood. Never shown in production."""
    out = result.router
    lines = [
        f"[debug] language={out.language.value} | intent={out.intent.value} "
        f"| conversation={result.conversation_language.value} "
        f"| reply_in={result.reply_language} | confidence={out.confidence:.2f}"
    ]
    fields = []
    if out.product_query:
        fields.append(f"product={out.product_query}")
    if out.color:
        fields.append(f"color={out.color}")
    if out.size_mm is not None:
        fields.append(f"size_mm={out.size_mm}")
    if out.max_price is not None:
        fields.append(f"max_price={out.max_price}")
    if out.kb_query_en:
        fields.append(f"kb={out.kb_query_en}")
    if out.needs_clarification:
        fields.append("needs_clarification")
    if fields:
        lines.append(" | ".join(fields))
    return "\n".join(lines)
