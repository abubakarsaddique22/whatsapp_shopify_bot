"""Phase 7, step 4: the image_request branch, run as a plain function (Blueprint Phase 7 card).

    replies = await run_image_flow(session, conversation_id, router_output, text)

What happens:
1. Which product? The router's product_query is searched. With no product_query, the product of
   the conversation so far (state.current_product_id) is used, so "aur pictures?" works.
2. Several close matches: ONE clarifying question that names 2 or 3 of them. Nothing is guessed.
   No match: an honest message. After MAX_CLARIFICATIONS questions in a row the bot stops asking
   the same thing and asks for the exact model name.
3. One product: the picture tool picks the pictures (colour asked: that colour's picture). One
   picture by default, up to 3 when the customer asks for more. A picture URL always comes from
   the tool, never from the LLM.
4. No picture: "no picture" plus the product link (T23).

Each picture becomes one reply with kind='image'. Its `body` is the plain-text fallback (name and
link): the sender uses it when the picture cannot be delivered (T24).

Nothing is sent here. The worker queues the replies in the outbox and the sender sends them.
The caller owns the transaction (commit / rollback).
"""

import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import tool_get_product_image, tool_search_products
from app.catalog.images import NO_IMAGE_MESSAGE
from app.db import repositories as repo
from app.domain.models import RouterOutput

MAX_CLARIFICATIONS = 2  # questions in a row (Blueprint: 2 clarifications in a row)
MAX_NAMES = 3  # product names listed in a clarifying question
MORE_IMAGES = 3  # how many pictures "more pictures" gives
MORE_WORDS = frozenset(
    {"pictures", "photos", "images", "aur", "more", "tasveerein", "tasveeren", "tasaweer"}
)

ASK_WHICH = "Which watch would you like to see? Please tell me its name or model."
ASK_EXACT = "I still cannot tell which product you mean. Please send the exact model name."
NOT_FOUND = "I could not find that product. Could you check the name or model?"
UNAVAILABLE = "Sorry, I could not look up the pictures right now. Please try again in a moment."


@dataclass(frozen=True)
class Reply:
    """One message to queue in the outbox. A text reply only needs `body`."""

    body: str
    kind: str = "text"  # text | image
    media_url: str | None = None  # image only: the Shopify picture URL
    caption: str | None = None  # image only


def wants_more(text: str) -> bool:
    """True for 'aur pictures hain?', 'more photos'. 'photo bhejo' is just one picture."""
    return bool(MORE_WORDS & set(re.findall(r"[a-z]+", text.lower())))


def clarifying_question(titles: list[str]) -> str:
    names = titles[:MAX_NAMES]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    return f"Which one do you mean: {listed}?"


def caption_for(title: str, colour: str | None, colour_image_found: bool | None) -> str:
    """The product name. If the colour asked for has no picture of its own, say so honestly."""
    if colour and colour_image_found is False:
        return f"{title} (I do not have a {colour} picture, this is the main picture)"
    return title


async def _ask(
    session: AsyncSession, conversation_id: int, text: str, candidate_ids: list[str] | None = None
) -> list[Reply]:
    """Send a clarifying question and count it, or stop asking after MAX_CLARIFICATIONS."""
    state = await repo.load_state(session, conversation_id)
    if state.clarification_count >= MAX_CLARIFICATIONS:
        return [Reply(ASK_EXACT)]
    state.clarification_count += 1
    state.candidate_product_ids = candidate_ids or []
    await repo.save_state(session, conversation_id, state)
    return [Reply(text)]


async def run_image_flow(
    session: AsyncSession, conversation_id: int, out: RouterOutput, text: str
) -> list[Reply]:
    state = await repo.load_state(session, conversation_id)

    # 1. which product
    if out.product_query:
        found = await tool_search_products(session, out.product_query)
        if not found.ok or not isinstance(found.data, dict):
            return [Reply(UNAVAILABLE)]
        candidates = found.data["candidates"]
        if found.data["kind"] == "none":
            return [Reply(NOT_FOUND)]
        if found.data["kind"] == "ambiguous":
            titles = [c["title"] for c in candidates[:MAX_NAMES]]
            ids = [c["product_id"] for c in candidates]
            return await _ask(session, conversation_id, clarifying_question(titles), ids)
        product_id = candidates[0]["product_id"]
    elif state.current_product_id:
        product_id = state.current_product_id
    else:
        return await _ask(session, conversation_id, ASK_WHICH)

    # 2. the pictures
    max_images = MORE_IMAGES if wants_more(text) else 1
    result = await tool_get_product_image(
        session, product_id, colour=out.color, max_images=max_images
    )
    if not result.ok or not isinstance(result.data, dict):
        state.current_product_id = None  # a hidden or unknown product must not stay "current"
        await repo.save_state(session, conversation_id, state)
        return [Reply(NOT_FOUND)]

    data = result.data
    state.current_product_id = product_id
    state.clarification_count = 0
    state.candidate_product_ids = []
    await repo.save_state(session, conversation_id, state)

    title, link = data["title"], data["product_url"]
    fallback = f"{title}: {link}"
    if not data["images"]:  # T23: honest message and the product link
        return [Reply(f"{NO_IMAGE_MESSAGE}\n{fallback}")]

    caption = caption_for(title, data.get("colour"), data.get("colour_image_found"))
    return [
        Reply(fallback, kind="image", media_url=image["url"], caption=caption)
        for image in data["images"]
    ]
