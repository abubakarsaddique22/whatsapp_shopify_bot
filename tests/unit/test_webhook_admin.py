"""Unit tests for integrations/shopify/webhook_admin.py (Phase 10, step 3).

A small fake Shopify keeps the subscriptions in memory, so the tests check what really matters:
the first run creates, a second run changes nothing, a new tunnel URL updates (no duplicates),
and a refused request is reported. No network.
"""

from typing import Any

import pytest

from app.core.errors import ConfigError, ShopifyError
from app.core.settings import Settings
from app.integrations.shopify import webhook_admin as wa
from app.integrations.shopify.client import graphql
from app.sync.webhooks import SUPPORTED_TOPICS

URL = "https://abc123.example-tunnel.app"
TARGET = URL + "/webhook/shopify"
OLD_TARGET = "https://old-tunnel.example-tunnel.app/webhook/shopify"


class FakeShopify:
    """Stands in for client.graphql(): answers the four webhook operations from memory."""

    def __init__(self, existing: list[dict[str, str]] | None = None) -> None:
        self.subs: dict[str, dict[str, str]] = {s["id"]: dict(s) for s in existing or []}
        self.mutations = 0
        self.refuse_topic: str | None = None
        self._next = 100  # far from the ids the tests put in by hand, so they never collide

    async def __call__(
        self, query: str, variables: dict[str, Any] | None = None, *, allow_mutation: bool = False
    ) -> dict[str, Any]:
        variables = variables or {}
        if "webhookSubscriptionCreate(" in query:
            assert allow_mutation, "a mutation must be explicitly allowed"
            self.mutations += 1
            if variables["topic"] == self.refuse_topic:
                message = "You cannot create a webhook subscription with the specified topic"
                return {
                    "webhookSubscriptionCreate": {
                        "webhookSubscription": None,
                        "userErrors": [{"field": ["webhookSubscription"], "message": message}],
                    }
                }
            sub_id = f"gid://shopify/WebhookSubscription/{self._next}"
            self._next += 1
            created = {
                "id": sub_id,
                "topic": variables["topic"],
                "uri": variables["webhookSubscription"]["uri"],
            }
            self.subs[sub_id] = created
            return {"webhookSubscriptionCreate": {"webhookSubscription": created, "userErrors": []}}
        if "webhookSubscriptionUpdate(" in query:
            assert allow_mutation, "a mutation must be explicitly allowed"
            self.mutations += 1
            self.subs[variables["id"]]["uri"] = variables["webhookSubscription"]["uri"]
            return {"webhookSubscriptionUpdate": {"userErrors": []}}
        if "webhookSubscriptionDelete(" in query:
            assert allow_mutation, "a mutation must be explicitly allowed"
            self.mutations += 1
            self.subs.pop(variables["id"])
            return {"webhookSubscriptionDelete": {"userErrors": []}}
        assert not allow_mutation, "listing must stay a plain read-only query"
        return {"webhookSubscriptions": {"nodes": list(self.subs.values())}}


def sub(sub_id: int, topic: str, uri: str) -> dict[str, str]:
    return {"id": f"gid://shopify/WebhookSubscription/{sub_id}", "topic": topic, "uri": uri}


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wa, "get_settings", lambda: Settings(shopify_webhook_base_url=URL))


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeShopify) -> FakeShopify:
    monkeypatch.setattr(wa, "graphql", fake)
    return fake


# ---- ensure_subscriptions


async def test_first_run_creates_one_subscription_per_topic(
    monkeypatch: pytest.MonkeyPatch, settings: None
) -> None:
    fake = install(monkeypatch, FakeShopify())
    changes = await wa.ensure_subscriptions()
    assert {c.action for c in changes} == {"created"}
    assert {c.topic for c in changes} == set(SUPPORTED_TOPICS)
    assert len(fake.subs) == len(SUPPORTED_TOPICS)
    assert {s["uri"] for s in fake.subs.values()} == {TARGET}


async def test_second_run_changes_nothing(monkeypatch: pytest.MonkeyPatch, settings: None) -> None:
    fake = install(monkeypatch, FakeShopify())
    await wa.ensure_subscriptions()
    mutations_after_first_run = fake.mutations
    changes = await wa.ensure_subscriptions()
    assert {c.action for c in changes} == {"unchanged"}
    assert fake.mutations == mutations_after_first_run
    assert len(fake.subs) == len(SUPPORTED_TOPICS)  # registering twice never duplicates


async def test_new_tunnel_url_updates_the_subscriptions(
    monkeypatch: pytest.MonkeyPatch, settings: None
) -> None:
    fake = install(
        monkeypatch,
        FakeShopify([sub(1, "PRODUCTS_UPDATE", OLD_TARGET), sub(2, "PRODUCTS_CREATE", TARGET)]),
    )
    changes = {c.topic: c.action for c in await wa.ensure_subscriptions()}
    assert changes["products/update"] == "updated"
    assert changes["products/create"] == "unchanged"
    assert changes["products/delete"] == "created"
    assert changes["inventory_levels/update"] == "created"
    assert {s["uri"] for s in fake.subs.values()} == {TARGET}
    assert len(fake.subs) == len(SUPPORTED_TOPICS)


async def test_duplicate_subscription_of_one_topic_is_deleted(
    monkeypatch: pytest.MonkeyPatch, settings: None
) -> None:
    fake = install(
        monkeypatch,
        FakeShopify([sub(1, "PRODUCTS_UPDATE", OLD_TARGET), sub(2, "PRODUCTS_UPDATE", TARGET)]),
    )
    changes = await wa.ensure_subscriptions()
    update_changes = [c for c in changes if c.topic == "products/update"]
    assert sorted(c.action for c in update_changes) == ["deleted_duplicate", "unchanged"]
    remaining = [s for s in fake.subs.values() if s["topic"] == "PRODUCTS_UPDATE"]
    assert [s["uri"] for s in remaining] == [TARGET]  # the correct one is the one that stays


async def test_subscription_of_another_topic_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, settings: None
) -> None:
    fake = install(monkeypatch, FakeShopify([sub(9, "SHOP_UPDATE", OLD_TARGET)]))
    await wa.ensure_subscriptions()
    assert fake.subs["gid://shopify/WebhookSubscription/9"]["uri"] == OLD_TARGET


async def test_refused_subscription_raises_with_a_hint(
    monkeypatch: pytest.MonkeyPatch, settings: None
) -> None:
    fake = install(monkeypatch, FakeShopify())
    fake.refuse_topic = "INVENTORY_LEVELS_UPDATE"
    with pytest.raises(ShopifyError) as error:
        await wa.ensure_subscriptions()
    assert "read_inventory" in error.value.message


async def test_list_subscriptions_reads_what_shopify_has(
    monkeypatch: pytest.MonkeyPatch, settings: None
) -> None:
    install(monkeypatch, FakeShopify([sub(1, "PRODUCTS_UPDATE", TARGET)]))
    found = await wa.list_subscriptions()
    assert [(s.topic, s.uri) for s in found] == [("PRODUCTS_UPDATE", TARGET)]


# ---- callback_uri


@pytest.mark.parametrize(
    "configured",
    [URL, URL + "/", URL + "/webhook/shopify", "  " + URL + "  "],
)
def test_callback_uri_adds_the_path_once(monkeypatch: pytest.MonkeyPatch, configured: str) -> None:
    monkeypatch.setattr(wa, "get_settings", lambda: Settings(shopify_webhook_base_url=configured))
    assert wa.callback_uri() == TARGET


def test_callback_uri_needs_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wa, "get_settings", lambda: Settings(shopify_webhook_base_url=""))
    with pytest.raises(ConfigError):
        wa.callback_uri()


def test_callback_uri_must_be_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wa, "get_settings", lambda: Settings(shopify_webhook_base_url="http://abc.example.com")
    )
    with pytest.raises(ConfigError):
        wa.callback_uri()


# ---- the read-only rule of the client is still on


async def test_the_client_still_refuses_mutations_by_default() -> None:
    with pytest.raises(ShopifyError):
        await graphql("mutation { x }")
