"""Command line helpers. Run from the project folder:

    uv run python -m app.cli shopify-ping
"""

import argparse
import asyncio

from app.core.errors import AppError
from app.integrations.shopify.auth import shop_host
from app.integrations.shopify.client import graphql

PING_QUERY = "{ shop { name currencyCode } productsCount { count } }"


async def shopify_ping() -> None:
    """Smallest possible real call: proves the token, the scopes and the store connection work."""
    data = await graphql(PING_QUERY)  # token + scope audit happen inside
    print(f"Shop:     {data['shop']['name']} ({shop_host()})")
    print(f"Currency: {data['shop']['currencyCode']}")
    print(f"Products: {data['productsCount']['count']}")
    print("Scope audit: OK (read-only scopes only)")


COMMANDS = {"shopify-ping": shopify_ping}


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args()
    try:
        asyncio.run(COMMANDS[args.command]())
    except AppError as exc:
        print(f"ERROR ({exc.code}): {exc.message}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
