"""Try product search on the REAL mirror (read-only). Run import-catalog first.

    set PYTHONPATH=src
    uv run python scripts\\search_probe.py "ga2100"
    uv run python scripts\\search_probe.py "black casio" --max-price 10000
    uv run python scripts\\search_probe.py "g shock" --colour kala --size 44
"""

import argparse
import asyncio
import sys

from app.catalog.search import search_products
from app.db.session import get_engine, get_sessionmaker


async def main(args: argparse.Namespace) -> None:
    try:
        async with get_sessionmaker()() as session:
            result = await search_products(
                session,
                args.query,
                colour=args.colour,
                size_mm=args.size,
                max_price=args.max_price,
            )
    finally:
        await get_engine().dispose()
    print(f"kind={result.kind.value}  name_matches={result.name_matches}  colour={result.colour}")
    for c in result.candidates:
        print(f"  {c.score:.2f}  {c.title}  [{c.vendor or '-'}]  colours={c.colours}")
        print(f"        variants={len(c.variant_ids)}  colour_verified={c.colour_verified}")
        print(f"        {c.url}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--colour")
    parser.add_argument("--size", type=int)
    parser.add_argument("--max-price", type=int)
    asyncio.run(main(parser.parse_args()))
