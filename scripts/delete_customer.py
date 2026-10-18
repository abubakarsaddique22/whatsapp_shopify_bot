"""Erase everything the bot stores about ONE customer (a request to be forgotten).

    uv run python scripts\\delete_customer.py --wa-id 923001234567           show what would go
    uv run python scripts\\delete_customer.py --wa-id 923001234567 --yes     delete it
    uv run python scripts\\delete_customer.py --conversation-id 12 --yes     same, by conversation

Without --yes nothing is changed: it only counts. With --yes the phone number, the transcript, the
inbox and outbox rows, the handoffs, the LangGraph checkpoints and the security events of this
customer are deleted in ONE transaction, and one `customer_erased` event is written (conversation
id and counts, no phone number).

The number is never printed or logged. What this script cannot reach: LangSmith traces (if tracing
is on), and the copy on the customer's own phone or at Meta. Details: app/security/erasure.py.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.db.session import get_engine, get_sessionmaker  # noqa: E402
from app.security import erasure  # noqa: E402


def digits_only(raw: str) -> str:
    """Meta's wa_id is digits only: accept '+92 300 1234567' too."""
    return "".join(ch for ch in raw if ch.isdigit())


async def main(wa_id: str | None, conversation_id: int | None, confirmed: bool) -> int:
    try:
        async with get_sessionmaker()() as session:
            found = await erasure.find_conversation(
                session,
                wa_id=digits_only(wa_id) if wa_id else None,
                conversation_id=conversation_id,
            )
            if found is None:
                print("No conversation found for that customer. Nothing to do.")
                return 1
            target = found[0]
            if confirmed:
                counts = await erasure.erase_conversation(session, target)
                await session.commit()
                print(f"Erased conversation #{target}:")
            else:
                counts = await erasure.count_conversation_data(session, target)
                print(f"Conversation #{target} has this (nothing deleted, add --yes to delete):")
            for key, value in counts.items():
                print(f"  {key}: {value}")
            print("  conversation row with the phone number: 1")
            return 0
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Erase one customer's data.")
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--wa-id", help="the customer's WhatsApp number (digits, + and spaces ok)")
    who.add_argument("--conversation-id", type=int, help="the conversation id instead")
    parser.add_argument("--yes", action="store_true", help="really delete (default: only count)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.wa_id, args.conversation_id, args.yes)))
