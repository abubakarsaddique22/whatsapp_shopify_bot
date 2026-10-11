"""Command line helpers. Run from the project folder:

    uv run python -m app.cli shopify-ping
    uv run python -m app.cli import-catalog
"""

import argparse
import asyncio

from app.catalog.importer import sync_mirror
from app.core.errors import AppError
from app.db.session import get_engine, get_sessionmaker
from app.integrations.shopify import gateway
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


async def import_catalog() -> None:
    """Copy the visible products of the real store into the Postgres mirror (re-runnable)."""
    products = await gateway.list_products()  # hidden products never come back from the gateway
    try:
        async with get_sessionmaker()() as session, session.begin():  # commit, or roll back all
            report = await sync_mirror(session, products)
    finally:
        await get_engine().dispose()
    print(f"Visible products in Shopify: {report.total}")
    print(f"  added:     {report.added}")
    print(f"  updated:   {report.updated}")
    print(f"  unchanged: {report.unchanged}")
    print(f"  hidden:    {report.hidden}")
    if report.skipped_hiding:
        print("WARNING: Shopify returned 0 products, so nothing was hidden in the mirror.")


COMMANDS = {"shopify-ping": shopify_ping, "import-catalog": import_catalog}


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
