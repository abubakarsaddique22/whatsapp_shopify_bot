"""GraphQL queries (read-only: `query` only, never `mutation`; client.py also refuses mutations).

Shopify rejects a single query whose requested cost is above 1000 points. A product page costs
about 1 + variants*3 + metafields + media per product (1 + 20*3 + 25 + 5 = 91), so the page
size below keeps it near 730.
Ask only for fields we use: smaller queries are faster and get throttled less.
"""

PRODUCTS_PAGE_SIZE = 8
VARIANTS_PER_PRODUCT = 20  # watches have few variants; the mapper logs a warning if there are more

_VARIANT_FIELDS = """
  id title sku price compareAtPrice availableForSale inventoryQuantity inventoryPolicy
  selectedOptions { name value }
  image { url }
  inventoryItem { tracked }
"""

_PRODUCT_FIELDS = f"""
  id title handle status onlineStoreUrl vendor productType tags updatedAt descriptionHtml
  options {{ name values }}
  media(first: 5) {{ nodes {{ ... on MediaImage {{ id alt image {{ url width height }} }} }} }}
  metafields(first: 25) {{ nodes {{ namespace key type value }} }}
  variants(first: {VARIANTS_PER_PRODUCT}) {{
    pageInfo {{ hasNextPage }}
    nodes {{ {_VARIANT_FIELDS} }}
  }}
"""

PRODUCTS_PAGE = f"""
query ProductsPage($first: Int!, $after: String, $q: String) {{
  shop {{ currencyCode }}
  products(first: $first, after: $after, query: $q) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ {_PRODUCT_FIELDS} }}
  }}
}}
"""

PRODUCT_BY_ID = f"""
query ProductById($id: ID!) {{
  shop {{ currencyCode }}
  product(id: $id) {{ {_PRODUCT_FIELDS} }}
}}
"""

# Live price and stock for one variant. `product` is needed for the customer-visible rule.
VARIANT_BY_ID = f"""
query VariantById($id: ID!) {{
  shop {{ currencyCode }}
  productVariant(id: $id) {{
    {_VARIANT_FIELDS}
    product {{ id status onlineStoreUrl }}
  }}
}}
"""

POLICIES = """
query Policies {
  shop { shopPolicies { type title body url } }
}
"""

PAGES = """
query Pages($first: Int!, $after: String) {
  pages(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id title handle body isPublished }
  }
}
"""

PAGES_PAGE_SIZE = 50
