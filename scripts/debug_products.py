"""Debug helper: shows why products are (not) customer-visible. Read-only.

set PYTHONPATH=src
uv run python scripts\\debug_products.py
"""

import asyncio

from app.integrations.shopify.client import graphql

QUERY = """
{
  shop { url }
  products(first: 20) {
    nodes { id title handle status onlineStoreUrl onlineStorePreviewUrl publishedAt }
  }
}
"""


async def main() -> None:
    data = await graphql(QUERY)
    print(f"shop url: {data['shop']['url']}")
    for p in data["products"]["nodes"]:
        visible = p["status"] == "ACTIVE" and bool(p["onlineStoreUrl"])
        print(f"{p['title']!r}")
        print(f"  status:           {p['status']}")
        print(f"  publishedAt:      {p['publishedAt']}")
        print(f"  onlineStoreUrl:   {p['onlineStoreUrl']}")
        print(f"  previewUrl:       {p['onlineStorePreviewUrl']}")
        print(f"  visible to bot:   {visible}")


if __name__ == "__main__":
    asyncio.run(main())
