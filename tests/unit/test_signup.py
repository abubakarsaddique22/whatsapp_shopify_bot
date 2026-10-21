"""Embedded Signup page (/signup, P5 Coexistence). Meta is faked with respx: no network, no DB."""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from fastapi import FastAPI
from pydantic import SecretStr

from app.api import signup
from app.core.settings import Settings
from app.integrations.whatsapp.payloads import parse_webhook
from app.main import create_app

KEY = "k" * 30
V = "v26.0"
G = f"https://graph.facebook.com/{V}"
WABA = "102290129340398"
PHONE = "106540352242922"
TOKEN = "EAAB-business-token-secret"


def _settings(**extra: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "signup_key": SecretStr(KEY),
        "meta_app_id": "1725896202285877",
        "signup_config_id": "987654321",
        "meta_graph_version": V,
        "whatsapp_app_secret": SecretStr("app-secret"),
    }
    values.update(extra)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(signup, "get_settings", _settings)
    app = FastAPI()
    app.include_router(signup.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot") as http:
        yield http


def _meta_ok(mock: respx.MockRouter, *, on_biz_app: bool = True) -> dict[str, respx.Route]:
    return {
        "token": mock.get(f"{G}/oauth/access_token").respond(
            200, json={"access_token": TOKEN, "token_type": "bearer"}
        ),
        "subscribe": mock.post(f"{G}/{WABA}/subscribed_apps").respond(200, json={"success": True}),
        "sync": mock.post(f"{G}/{PHONE}/smb_app_data").respond(
            200, json={"messaging_product": "whatsapp", "request_id": "req-1"}
        ),
        "status": mock.get(f"{G}/{PHONE}").respond(
            200,
            json={
                "is_on_biz_app": on_biz_app,
                "platform_type": "CLOUD_API",
                "display_phone_number": "923076390997",
                "id": PHONE,
            },
        ),
    }


def _body(**extra: str) -> dict[str, str]:
    body = {
        "key": KEY,
        "code": "the-code",
        "event": signup.COEXISTENCE_EVENT,
        "waba_id": WABA,
        "phone_number_id": PHONE,
    }
    body.update(extra)
    return body


async def test_wrong_or_missing_key_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/signup")).status_code == 404
    assert (await client.get("/signup", params={"key": "wrong"})).status_code == 404
    assert (await client.post("/signup/complete", json=_body(key="wrong"))).status_code == 404


async def test_page_has_the_coexistence_launch_and_no_secret(client: httpx.AsyncClient) -> None:
    page = await client.get("/signup", params={"key": KEY})
    assert page.status_code == 200
    assert "whatsapp_business_app_onboarding" in page.text
    assert '"987654321"' in page.text  # config id
    assert "connect.facebook.net" in page.text
    assert "app-secret" not in page.text
    assert page.headers["cache-control"] == "no-store"


async def test_without_signup_key_the_router_is_not_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: _settings(signup_key=SecretStr("")))
    paths = {getattr(route, "path", "") for route in create_app().routes}
    assert "/signup" not in paths


def test_short_signup_key_is_refused() -> None:
    with pytest.raises(ValueError, match="SIGNUP_KEY"):
        _settings(signup_key=SecretStr("short"))


async def test_coexistence_runs_every_step_in_order(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=True) as mock:
        routes = _meta_ok(mock)
        response = await client.post("/signup/complete", json=_body())
    report = response.json()
    assert response.status_code == 200
    assert report["ok"] is True
    assert [s["step"] for s in report["steps"]] == [
        "token",
        "subscribe_webhooks",
        "sync_smb_app_state_sync",
        "sync_history",
        "check_status",
    ]
    # the code is exchanged with the app id + secret, server to server
    params = routes["token"].calls[0].request.url.params
    assert params["code"] == "the-code" and params["client_secret"] == "app-secret"
    # contacts first, then history
    bodies = [call.request.content for call in routes["sync"].calls]
    assert b"smb_app_state_sync" in bodies[0] and b'"history"' in bodies[1]
    # the business token is used but never returned to the page
    assert routes["subscribe"].calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in response.text


async def test_number_is_never_registered_for_coexistence(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _meta_ok(mock)
        register = mock.post(f"{G}/{PHONE}/register").respond(200, json={"success": True})
        await client.post("/signup/complete", json=_body())
    assert not register.called


async def test_phone_number_id_is_looked_up_when_the_page_had_none(
    client: httpx.AsyncClient,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        _meta_ok(mock)
        mock.get(f"{G}/{WABA}/phone_numbers").respond(
            200, json={"data": [{"id": PHONE, "display_phone_number": "923076390997"}]}
        )
        report = (await client.post("/signup/complete", json=_body(phone_number_id=""))).json()
    assert report["ok"] is True
    assert report["phone_number_id"] == PHONE


async def test_failed_token_exchange_stops_and_says_why(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{G}/oauth/access_token").respond(
            400, json={"error": {"code": 100, "message": "code expired"}}
        )
        subscribe = mock.post(f"{G}/{WABA}/subscribed_apps").respond(200, json={"success": True})
        report = (await client.post("/signup/complete", json=_body())).json()
    assert report["ok"] is False
    assert report["steps"] == [
        {
            "step": "token",
            "ok": False,
            "note": "token exchange failed (HTTP 400, code 100: code expired)",
        }
    ]
    assert not subscribe.called


async def test_a_failed_sync_is_reported_not_hidden(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _meta_ok(mock)
        mock.post(f"{G}/{PHONE}/smb_app_data").respond(
            400, json={"error": {"code": 2593100, "message": "already synced"}}
        )
        report = (await client.post("/signup/complete", json=_body())).json()
    assert report["ok"] is False
    failed = [s for s in report["steps"] if not s["ok"]]
    assert {s["step"] for s in failed} == {"sync_smb_app_state_sync", "sync_history"}


async def test_normal_cloud_api_finish_is_not_called_coexistence(
    client: httpx.AsyncClient,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _meta_ok(mock, on_biz_app=False)
        report = (await client.post("/signup/complete", json=_body(event="FINISH"))).json()
    assert report["ok"] is False
    assert not routes["sync"].called
    assert any(s["step"] == "coexistence" and not s["ok"] for s in report["steps"])


async def test_bad_input_is_refused_before_any_meta_call(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        token = mock.get(f"{G}/oauth/access_token").respond(200, json={"access_token": TOKEN})
        not_finished = await client.post("/signup/complete", json=_body(event="CANCEL"))
        bad_ids = await client.post("/signup/complete", json=_body(waba_id="1/../x"))
        no_code = await client.post("/signup/complete", json=_body(code=""))
    assert not_finished.status_code == 400
    assert bad_ids.status_code == 400
    assert no_code.status_code == 400
    assert not token.called


async def test_network_error_after_the_token_is_reported(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{G}/oauth/access_token").respond(200, json={"access_token": TOKEN})
        mock.post(f"{G}/{WABA}/subscribed_apps").mock(side_effect=httpx.ConnectError("down"))
        response = await client.post("/signup/complete", json=_body())
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert TOKEN not in response.text


# ---- payloads: Coexistence webhooks must never look like new customer messages


def _change(field: str, value: dict[str, object]) -> dict[str, object]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": WABA, "changes": [{"field": field, "value": value}]}],
    }


OLD_MESSAGE = {
    "from": "16505551234",
    "id": "wamid.OLD",
    "timestamp": "1738796547",
    "type": "image",
    "image": {"id": "24230790383178626", "mime_type": "image/jpeg"},
}


def test_history_media_message_is_not_a_new_message() -> None:
    parsed = parse_webhook(
        _change("history", {"messaging_product": "whatsapp", "messages": [OLD_MESSAGE]})
    )
    assert parsed.messages == []


def test_contacts_sync_and_account_update_give_nothing() -> None:
    contacts = _change("smb_app_state_sync", {"state_sync": [{"type": "contact"}]})
    update = _change("account_update", {"event": "PARTNER_REMOVED"})
    assert parse_webhook(contacts).messages == []
    assert parse_webhook(update).messages == []


def test_a_real_messages_change_still_parses() -> None:
    parsed = parse_webhook(_change("messages", {"messages": [OLD_MESSAGE]}))
    assert [m.wamid for m in parsed.messages] == ["wamid.OLD"]
