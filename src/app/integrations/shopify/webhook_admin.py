"""Create, fix and list the Shopify webhook subscriptions of the bot (Phase 10, step 3).

Registering a webhook does not change any store data and needs no write scope: the topic scopes
read_products and read_inventory are enough. It is still a GraphQL mutation, so this module is
the ONLY place that passes allow_mutation=True to the read-only client, and only with the three
fixed mutations below.

ensure_subscriptions() is idempotent (safe to run any number of times):
  - topic has no subscription          -> create it
  - subscription points to another URL -> update it (the tunnel URL changed)
  - subscription already correct       -> leave it
  - a second subscription, same topic  -> delete it (it would only cause duplicate deliveries)
Subscriptions of topics we do not use are never touched.
"""

import logging
from typing import Any, Literal

from pydantic import BaseModel

from app.core.errors import ConfigError, ShopifyError
from app.core.settings import get_settings
from app.integrations.shopify.client import graphql
from app.sync.webhooks import SUPPORTED_TOPICS

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhook/shopify"  # the route in api/shopify_webhook.py

LIST_SUBSCRIPTIONS = """
query WebhookSubscriptions {
  webhookSubscriptions(first: 100) { nodes { id topic uri } }
}
"""

CREATE_SUBSCRIPTION = """
mutation WebhookCreate(
  $topic: WebhookSubscriptionTopic!
  $webhookSubscription: WebhookSubscriptionInput!
) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhookSubscription) {
    webhookSubscription { id topic uri }
    userErrors { field message }
  }
}
"""

UPDATE_SUBSCRIPTION = """
mutation WebhookUpdate($id: ID!, $webhookSubscription: WebhookSubscriptionInput!) {
  webhookSubscriptionUpdate(id: $id, webhookSubscription: $webhookSubscription) {
    webhookSubscription { id topic uri }
    userErrors { field message }
  }
}
"""

DELETE_SUBSCRIPTION = """
mutation WebhookDelete($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors { field message }
  }
}
"""


class Subscription(BaseModel):
    id: str
    topic: str  # GraphQL enum name, e.g. PRODUCTS_UPDATE
    uri: str


class Change(BaseModel):
    topic: str  # header style, e.g. products/update
    action: Literal["created", "updated", "unchanged", "deleted_duplicate"]
    subscription_id: str


def topic_enum(topic: str) -> str:
    """'products/update' -> 'PRODUCTS_UPDATE' (the GraphQL name of the topic)."""
    return topic.upper().replace("/", "_")


def callback_uri() -> str:
    """The full URL Shopify must call, built from SHOPIFY_WEBHOOK_BASE_URL."""
    base = get_settings().shopify_webhook_base_url.strip().rstrip("/")
    base = base.removesuffix(WEBHOOK_PATH)  # in case the whole path was pasted into .env
    if not base:
        raise ConfigError("SHOPIFY_WEBHOOK_BASE_URL is empty (put your public https URL in .env)")
    if not base.startswith("https://"):
        raise ConfigError("SHOPIFY_WEBHOOK_BASE_URL must start with https:// (Shopify needs it)")
    return base + WEBHOOK_PATH


def _raise_on_user_errors(payload: dict[str, Any] | None, what: str) -> dict[str, Any]:
    """Shopify answers 200 even when a mutation is refused: the reason is in userErrors."""
    payload = payload or {}
    errors = payload.get("userErrors") or []
    if errors:
        text = "; ".join(str(e.get("message", e)) for e in errors)[:300]
        hint = ""
        if "specified topic" in text:
            hint = " (check that the app has the scopes read_products and read_inventory)"
        raise ShopifyError(f"Could not {what}: {text}{hint}")
    return payload


async def list_subscriptions() -> list[Subscription]:
    """The webhook subscriptions this app has in the shop right now."""
    data = await graphql(LIST_SUBSCRIPTIONS)
    nodes = (data.get("webhookSubscriptions") or {}).get("nodes") or []
    return [
        Subscription(id=node["id"], topic=node["topic"], uri=node.get("uri") or "")
        for node in nodes
        if node.get("id") and node.get("topic")
    ]


async def _create(topic: str, uri: str) -> str:
    data = await graphql(
        CREATE_SUBSCRIPTION,
        {"topic": topic_enum(topic), "webhookSubscription": {"uri": uri, "format": "JSON"}},
        allow_mutation=True,
    )
    payload = _raise_on_user_errors(data.get("webhookSubscriptionCreate"), f"create {topic}")
    return str((payload.get("webhookSubscription") or {}).get("id", ""))


async def _update(subscription_id: str, topic: str, uri: str) -> None:
    data = await graphql(
        UPDATE_SUBSCRIPTION,
        {"id": subscription_id, "webhookSubscription": {"uri": uri, "format": "JSON"}},
        allow_mutation=True,
    )
    _raise_on_user_errors(data.get("webhookSubscriptionUpdate"), f"update {topic}")


async def _delete(subscription_id: str, topic: str) -> None:
    data = await graphql(DELETE_SUBSCRIPTION, {"id": subscription_id}, allow_mutation=True)
    _raise_on_user_errors(data.get("webhookSubscriptionDelete"), f"delete duplicate {topic}")


async def ensure_subscriptions(uri: str | None = None) -> list[Change]:
    """Make Shopify's subscriptions match SUPPORTED_TOPICS and the callback URL.

    Returns what was done for every topic. Running it twice in a row changes nothing the
    second time (all 'unchanged').
    """
    target = uri or callback_uri()
    existing = await list_subscriptions()
    changes: list[Change] = []

    for topic in sorted(SUPPORTED_TOPICS):
        mine = [s for s in existing if s.topic == topic_enum(topic)]
        mine.sort(key=lambda s: s.uri != target)  # a subscription that is already correct first

        if not mine:
            new_id = await _create(topic, target)
            changes.append(Change(topic=topic, action="created", subscription_id=new_id))
            continue

        keep, extras = mine[0], mine[1:]
        if keep.uri == target:
            changes.append(Change(topic=topic, action="unchanged", subscription_id=keep.id))
        else:
            await _update(keep.id, topic, target)
            changes.append(Change(topic=topic, action="updated", subscription_id=keep.id))

        for extra in extras:
            await _delete(extra.id, topic)
            changes.append(
                Change(topic=topic, action="deleted_duplicate", subscription_id=extra.id)
            )

    log.info("shopify_webhooks_ensured", extra={"changes": len(changes)})
    return changes
