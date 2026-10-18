"""Phase 11, step 9: erase the data of one customer (Blueprint 9.5). Used by the retention job
(idle conversations) and by scripts\\delete_customer.py (a customer asks to be forgotten).

    found = await find_conversation(session, wa_id="923001234567")     # (conversation id, hash)
    counts = await count_conversation_data(session, conversation_id)   # what WOULD go (dry run)
    counts = await erase_conversation(session, conversation_id)        # delete it; caller commits

What one customer's data is, and where it lives:

* `conversations` holds the raw phone number and the saved state (language, history lines).
  Deleting that row removes, through ON DELETE CASCADE, the customer's `messages`, `whatsapp_inbox`,
  `whatsapp_outbox` and `handoffs` rows.
* The LangGraph checkpointer keeps the step history of every turn in `checkpoints`,
  `checkpoint_blobs` and `checkpoint_writes`, under the thread id `conversation-<id>`. These rows
  hold customer text too and have no foreign key, so they are deleted by hand.
* `security_events` has the keyed hash of the sender (never the number) and, for the one-a-day
  budget notice, the conversation id. Both go. The `admin_action` audit rows stay: they hold an
  action and a conversation id, nothing about the person.
* One `customer_erased` event is written afterwards: the conversation id and how many rows went.
  No phone number, no hash, no text.

NOT covered here (nothing in this database): LangSmith traces, if LANGSMITH_TRACING=true, and
WhatsApp itself (the customer's own phone, Meta's copy). Keep tracing off in production, or hide
inputs in LangSmith, if customers can ask to be forgotten.
"""

import logging
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repositories as repo
from app.db.tables import (
    Conversation,
    Handoff,
    Message,
    SecurityEvent,
    WhatsAppInbox,
    WhatsAppOutbox,
)

log = logging.getLogger(__name__)

CHECKPOINT_PREFIX = "conversation-"
# the tables of the LangGraph Postgres saver; all three have a `thread_id` column
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
# security_events kinds whose `ref` is a conversation id (not a document or a message id)
CONVERSATION_REF_KINDS = ("budget_notice",)


def checkpoint_thread_id(conversation_id: int) -> str:
    """The LangGraph thread of a conversation (agent/graph.py turn_config uses this too)."""
    return f"{CHECKPOINT_PREFIX}{conversation_id}"


def rows_changed(result: Any) -> int:
    """How many rows a DELETE or UPDATE touched."""
    return cast("CursorResult[Any]", result).rowcount or 0


# ---- the LangGraph checkpoint tables (they exist only after the worker has run once)


async def checkpoint_tables(session: AsyncSession) -> list[str]:
    found: list[str] = []
    for name in CHECKPOINT_TABLES:
        result = await session.execute(
            text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}
        )
        if result.scalar_one():
            found.append(name)
    return found


async def delete_checkpoints(session: AsyncSession, conversation_ids: Sequence[int]) -> int:
    """Delete the checkpoint rows of these conversations. Returns how many rows went."""
    if not conversation_ids:
        return 0
    threads = [checkpoint_thread_id(cid) for cid in conversation_ids]
    deleted = 0
    for table in await checkpoint_tables(session):  # fixed names from CHECKPOINT_TABLES
        result = await session.execute(
            text(f"DELETE FROM {table} WHERE thread_id = ANY(CAST(:threads AS text[]))"),
            {"threads": threads},
        )
        deleted += rows_changed(result)
    return deleted


async def delete_idle_checkpoints(session: AsyncSession, active_ids: Sequence[int]) -> int:
    """Delete the checkpoints of every conversation that is NOT in active_ids.

    That covers idle conversations and checkpoints whose conversation is already gone. Only
    threads named `conversation-<id>` are touched.
    """
    active = [checkpoint_thread_id(cid) for cid in active_ids]
    deleted = 0
    for table in await checkpoint_tables(session):
        result = await session.execute(
            text(
                f"DELETE FROM {table} WHERE thread_id LIKE :prefix "
                "AND NOT (thread_id = ANY(CAST(:active AS text[])))"
            ),
            {"prefix": f"{CHECKPOINT_PREFIX}%", "active": active},
        )
        deleted += rows_changed(result)
    return deleted


async def _count_checkpoints(session: AsyncSession, conversation_id: int) -> int:
    total = 0
    for table in await checkpoint_tables(session):
        result = await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE thread_id = :thread"),
            {"thread": checkpoint_thread_id(conversation_id)},
        )
        total += int(result.scalar_one())
    return total


# ---- one customer


async def find_conversation(
    session: AsyncSession, *, wa_id: str | None = None, conversation_id: int | None = None
) -> tuple[int, str] | None:
    """(conversation id, wa_id_hash) of the customer, or None. Give the number OR the id."""
    stmt = select(Conversation.id, Conversation.wa_id_hash)
    if wa_id is not None:
        stmt = stmt.where(Conversation.wa_id == wa_id)
    elif conversation_id is not None:
        stmt = stmt.where(Conversation.id == conversation_id)
    else:
        raise ValueError("give wa_id or conversation_id")
    row = (await session.execute(stmt)).one_or_none()
    return None if row is None else (int(row[0]), str(row[1]))


def _own_events(conversation_id: int, wa_hash: str) -> Any:
    """The security_events rows that belong to this customer."""
    return or_(
        SecurityEvent.wa_hash == wa_hash,
        and_(
            SecurityEvent.kind.in_(CONVERSATION_REF_KINDS),
            SecurityEvent.ref == str(conversation_id),
        ),
    )


async def _count(session: AsyncSession, column: Any, *where: Any) -> int:
    result = await session.execute(select(func.count()).select_from(column.class_).where(*where))
    return int(result.scalar_one())


async def count_conversation_data(session: AsyncSession, conversation_id: int) -> dict[str, int]:
    """How many rows of this customer exist now (what an erase would remove). LookupError if
    there is no such conversation."""
    found = await find_conversation(session, conversation_id=conversation_id)
    if found is None:
        raise LookupError(f"conversation {conversation_id} does not exist")
    return {
        "messages": await _count(session, Message.id, Message.conversation_id == conversation_id),
        "inbox": await _count(
            session, WhatsAppInbox.id, WhatsAppInbox.conversation_id == conversation_id
        ),
        "outbox": await _count(
            session, WhatsAppOutbox.id, WhatsAppOutbox.conversation_id == conversation_id
        ),
        "handoffs": await _count(session, Handoff.id, Handoff.conversation_id == conversation_id),
        "security_events": await _count(
            session, SecurityEvent.id, _own_events(conversation_id, found[1])
        ),
        "checkpoints": await _count_checkpoints(session, conversation_id),
    }


async def erase_conversation(session: AsyncSession, conversation_id: int) -> dict[str, int]:
    """Delete everything of one customer, the phone number included. The caller commits.

    Returns how many rows of each kind went. LookupError if there is no such conversation.
    """
    found = await find_conversation(session, conversation_id=conversation_id)
    if found is None:
        raise LookupError(f"conversation {conversation_id} does not exist")
    counts = await count_conversation_data(session, conversation_id)

    await session.execute(delete(SecurityEvent).where(_own_events(conversation_id, found[1])))
    await delete_checkpoints(session, [conversation_id])
    # ON DELETE CASCADE removes messages, inbox, outbox and handoffs with the conversation
    await session.execute(delete(Conversation).where(Conversation.id == conversation_id))

    await repo.add_security_event(
        session, "customer_erased", detail={"conversation_id": conversation_id, **counts}
    )
    log.warning("customer_erased", extra={"conversation_id": conversation_id, **counts})
    return counts
