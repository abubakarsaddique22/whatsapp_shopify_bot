"""Command line helpers. Run from the project folder:

    uv run python -m app.cli shopify-ping
    uv run python -m app.cli import-catalog
    uv run python -m app.cli ask "GA-2100 ka price?"
    uv run python -m app.cli ask "is it water resistant" --debug
"""

import argparse
import asyncio
import json

from app.agent import rules
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


async def ask(question: str, *, debug: bool = False) -> None:
    """Answer one customer question with the rule-based reader and the live tools (no LLM).

    Reads the mirror and calls Shopify for price and stock. `--debug` also prints every tool
    result, so the numbers can be compared with the Shopify admin.
    """
    try:
        async with get_sessionmaker()() as session:
            reply = await rules.answer(session, question)
    finally:
        await get_engine().dispose()
    print(reply.text)
    if debug:
        parsed = reply.parsed
        print(
            f"\n[understood] intent={parsed.intent.value} product={parsed.product_query!r} "
            f"colour={parsed.colour} size_mm={parsed.size_mm} max_price={parsed.max_price}"
        )
        for result in reply.results:
            print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))


COMMANDS = {"shopify-ping": shopify_ping, "import-catalog": import_catalog}


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    parser.add_argument("command", choices=[*sorted(COMMANDS), "ask"])
    parser.add_argument("question", nargs="*", help="for 'ask': the customer's sentence")
    parser.add_argument("--debug", action="store_true", help="for 'ask': print the tool results")
    args = parser.parse_args()
    question = " ".join(args.question).strip()
    if args.command == "ask" and not question:
        parser.error('ask needs a question, for example: ask "GA-2100 ka price?"')
    try:
        if args.command == "ask":
            asyncio.run(ask(question, debug=args.debug))
        else:
            asyncio.run(COMMANDS[args.command]())
    except AppError as exc:
        print(f"ERROR ({exc.code}): {exc.message}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
