"""Shows exactly what the bot sees in the real store (read-only, no secrets printed).

    set PYTHONPATH=src
    uv run python scripts\\dump_catalog.py

Hidden products (draft, unlisted, archived, not on Online Store) must NOT appear here.
"""

import asyncio
import sys

from app.integrations.shopify import gateway


async def main() -> None:
    products = await gateway.list_products()
    print(f"Visible products: {len(products)}\n")
    for p in products:
        print(f"{p.title}  [{p.vendor or '-'}]  images={len(p.images)}")
        print(f"  options: {[(o.name, o.values) for o in p.options]}")
        print(f"  specs:   {p.specs or '-'}")
        for v in p.variants:
            print(
                f"  - {v.title}: {v.price.currency} {v.price.amount} | {v.stock.value}"
                f" | qty={v.inventory_quantity} | variant_image={'yes' if v.image_url else 'no'}"
            )
        print()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    asyncio.run(main())
