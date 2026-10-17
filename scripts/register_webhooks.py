"""Registers (or repairs) the Shopify webhook subscriptions of the bot. Safe to run many times.

    uv run python scripts\\register_webhooks.py           create / fix the subscriptions
    uv run python scripts\\register_webhooks.py --list    only show what Shopify has now

Needs SHOPIFY_WEBHOOK_BASE_URL in .env: your public https address, no path (the tunnel URL in
development). Run the script again whenever the tunnel URL changes.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.core.errors import AppError  # noqa: E402
from app.integrations.shopify import webhook_admin  # noqa: E402


async def show() -> None:
    subscriptions = await webhook_admin.list_subscriptions()
    if not subscriptions:
        print("Shopify has no webhook subscriptions for this app.")
        return
    for sub in sorted(subscriptions, key=lambda s: s.topic):
        print(f"{sub.topic:26} {sub.uri}")


async def register() -> None:
    target = webhook_admin.callback_uri()
    print(f"Callback URL: {target}")
    for change in await webhook_admin.ensure_subscriptions(target):
        print(f"{change.action:18} {change.topic}")


async def main(list_only: bool) -> int:
    try:
        await (show() if list_only else register())
    except AppError as exc:  # config problem, Shopify refused, Shopify unreachable
        print(f"ERROR: {exc.message}")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Register the Shopify webhooks of the bot.")
    parser.add_argument("--list", action="store_true", help="only list, change nothing")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.list)))
