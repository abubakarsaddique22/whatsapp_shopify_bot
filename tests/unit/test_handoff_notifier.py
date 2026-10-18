"""Phase 11, step 6: handoff/notifier.py without a database.

The text staff read, and every channel (Slack, email, WhatsApp staff, log) with the network
replaced: respx for Slack, a fake smtplib for email, a fake send_text for WhatsApp.
"""

import smtplib
from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.errors import ConfigError, NotificationError, WhatsAppError
from app.core.settings import Settings
from app.handoff import notifier
from app.handoff.notifier import Notification

SLACK_URL = "https://hooks.slack.com/services/T000/B000/SECRETSECRET"
WHEN = datetime(2026, 10, 17, 9, 15, tzinfo=UTC)


def make_settings(**values) -> Settings:
    """Settings without reading .env or validating, so a test sets only what it needs."""
    return Settings.model_construct(**values)


def note(**changes) -> Notification:
    base = {"subject": "[Handoff #7] human_request", "body": "Customer wants a person."}
    return Notification(**{**base, **changes})


# ---- the text


def test_the_notification_has_the_reason_the_summary_and_only_the_end_of_the_number():
    n = notifier.build_handoff_notification(
        7,
        "human_request",
        "normal",
        "Reason: human_request\nCustomer said: hello",
        WHEN,
        "923001234567",
    )

    assert n.subject == "[Handoff #7] human_request"
    assert n.urgent is False
    assert "Handoff #7 | urgency: normal | reason: human_request" in n.body
    assert "number ending 4567" in n.body
    assert "923001234567" not in n.body and "3001234" not in n.body
    assert "Waiting since: 2026-10-17 09:15 UTC" in n.body
    assert "Customer said: hello" in n.body


def test_a_high_urgency_handoff_is_marked_urgent_in_the_subject():
    n = notifier.build_handoff_notification(8, "complaint", "high", "s", WHEN, "923001234567")
    assert n.urgent is True
    assert n.subject == "[Handoff #8] URGENT complaint"


def test_a_card_number_in_the_summary_is_masked_again():
    n = notifier.build_handoff_notification(
        9, "x", "normal", "Customer said: 4111 1111 1111 1111", WHEN, "923001234567"
    )
    assert "4111" not in n.body and "[CARD]" in n.body


def test_a_line_break_in_the_reason_cannot_split_the_subject():
    n = notifier.build_handoff_notification(
        10, "x\r\nBcc: evil@example.com", "normal", "s", WHEN, "923001234567"
    )
    assert "\n" not in n.subject and "\r" not in n.subject


def test_a_missing_time_and_an_odd_number_do_not_break_the_text():
    n = notifier.build_handoff_notification(11, "x", "normal", "s", None, "")
    assert "Waiting since: just now" in n.body
    assert "number ending ????" in n.body


def test_a_very_long_summary_is_cut():
    n = notifier.build_handoff_notification(12, "x", "normal", "a" * 10_000, WHEN, "92300")
    assert len(n.body) <= notifier.MAX_BODY_CHARS


# ---- slack


@respx.mock
async def test_slack_gets_the_message_with_the_subject_in_bold():
    route = respx.post(SLACK_URL).mock(return_value=httpx.Response(200, text="ok"))
    settings = make_settings(handoff_channel="slack", handoff_target=SLACK_URL)

    await notifier.send(note(), settings)

    assert route.call_count == 1
    text = route.calls.last.request.read().decode()
    assert "*[Handoff #7] human_request*" in text and "Customer wants a person." in text


@respx.mock
async def test_slack_text_cannot_ping_the_channel_or_make_a_link():
    route = respx.post(SLACK_URL).mock(return_value=httpx.Response(200, text="ok"))
    settings = make_settings(handoff_channel="slack", handoff_target=SLACK_URL)

    await notifier.send(note(body="<!channel> see <https://evil.example|click> & more"), settings)

    sent = route.calls.last.request.read().decode()
    assert "<!channel>" not in sent and "<https" not in sent
    assert "&lt;!channel&gt;" in sent and "&amp; more" in sent


@respx.mock
async def test_an_urgent_slack_message_gets_a_marker():
    route = respx.post(SLACK_URL).mock(return_value=httpx.Response(200, text="ok"))
    settings = make_settings(handoff_channel="slack", handoff_target=SLACK_URL)

    await notifier.send(note(urgent=True), settings)

    assert ":rotating_light:" in route.calls.last.request.read().decode()


@respx.mock
async def test_a_slack_error_names_the_status_and_never_the_url():
    respx.post(SLACK_URL).mock(return_value=httpx.Response(500, text="boom"))
    settings = make_settings(handoff_channel="slack", handoff_target=SLACK_URL)

    with pytest.raises(NotificationError) as caught:
        await notifier.send(note(), settings)

    assert "HTTP 500" in caught.value.message
    assert "SECRETSECRET" not in caught.value.message


@respx.mock
async def test_slack_unreachable_never_shows_the_url_either():
    respx.post(SLACK_URL).mock(side_effect=httpx.ConnectError(f"cannot connect to {SLACK_URL}"))
    settings = make_settings(handoff_channel="slack", handoff_target=SLACK_URL)

    with pytest.raises(NotificationError) as caught:
        await notifier.send(note(), settings)

    assert "SECRETSECRET" not in caught.value.message and "ConnectError" in caught.value.message


async def test_slack_needs_a_real_webhook_url():
    for target in ("", "https://evil.example/hook", "hooks.slack.com/services/x"):
        settings = make_settings(handoff_channel="slack", handoff_target=target)
        with pytest.raises(ConfigError):
            await notifier.send(note(), settings)


# ---- email


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    failure: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.ssl_context = host, port, timeout, context
        self.calls: list = []
        self.message = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        if FakeSMTP.failure is not None:
            raise FakeSMTP.failure
        self.message = message


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.failure = None
    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


def email_settings(**changes) -> Settings:
    values = {
        "handoff_channel": "email",
        "handoff_target": "staff@shop.example, owner@shop.example",
        "smtp_host": "smtp.shop.example",
        "smtp_port": 587,
        "smtp_user": "bot@shop.example",
        "smtp_password": SecretStr("app-password"),
        "smtp_from": "",
    }
    return make_settings(**{**values, **changes})


async def test_email_goes_out_with_starttls_login_and_the_text(smtp):
    await notifier.send(note(urgent=True), email_settings())

    (client,) = smtp.instances
    assert type(client) is FakeSMTP and (client.host, client.port) == ("smtp.shop.example", 587)
    assert client.calls == ["starttls", ("login", "bot@shop.example", "app-password")]
    message = client.message
    assert message["Subject"] == "[Handoff #7] human_request"
    assert message["From"] == "bot@shop.example"  # SMTP_FROM empty: SMTP_USER
    assert message["To"] == "staff@shop.example, owner@shop.example"
    assert message["X-Priority"] == "1"
    assert "Customer wants a person." in message.get_content()


async def test_port_465_uses_ssl_from_the_start_and_no_starttls(smtp):
    await notifier.send(note(), email_settings(smtp_port=465, smtp_from="alerts@shop.example"))

    (client,) = smtp.instances
    assert type(client) is FakeSMTPSSL
    assert client.calls == [("login", "bot@shop.example", "app-password")]
    assert client.message["From"] == "alerts@shop.example"


async def test_a_normal_email_has_no_priority_header(smtp):
    await notifier.send(note(), email_settings())
    assert smtp.instances[0].message["X-Priority"] is None


async def test_an_smtp_failure_names_the_kind_and_never_the_password(smtp):
    smtp.failure = smtplib.SMTPAuthenticationError(535, b"bad login app-password for bot@shop")

    with pytest.raises(NotificationError) as caught:
        await notifier.send(note(), email_settings())

    assert "SMTPAuthenticationError" in caught.value.message
    assert "app-password" not in caught.value.message and "bot@shop" not in caught.value.message


async def test_a_network_problem_is_a_notification_error_too(smtp):
    smtp.failure = TimeoutError("timed out")
    with pytest.raises(NotificationError):
        await notifier.send(note(), email_settings())


@pytest.mark.parametrize(
    "changes",
    [
        {"handoff_target": ""},
        {"smtp_host": ""},
        {"smtp_user": "", "smtp_from": ""},
    ],
)
async def test_email_without_its_settings_is_a_config_error(smtp, changes):
    with pytest.raises(ConfigError):
        await notifier.send(note(), email_settings(**changes))
    assert smtp.instances == []  # nothing was opened


async def test_a_line_break_in_an_address_is_refused(smtp):
    settings = email_settings(handoff_target="a@shop.example\r\nBcc: evil@example.com")
    with pytest.raises(ConfigError):
        await notifier.send(note(), settings)
    assert smtp.instances == []


# ---- whatsapp staff


async def test_whatsapp_staff_gets_a_text_to_the_digits_of_the_target(monkeypatch):
    calls = []

    async def fake_send_text(to, body):
        calls.append((to, body))
        return "wamid.1"

    monkeypatch.setattr(notifier, "send_text", fake_send_text)
    settings = make_settings(handoff_channel="whatsapp_staff", handoff_target="+92 300 1112233")

    await notifier.send(note(), settings)

    ((to, body),) = calls
    assert to == "923001112233"
    assert "[Handoff #7] human_request" in body and "Customer wants a person." in body


async def test_whatsapp_staff_refused_by_meta_gives_the_code_and_not_the_number(monkeypatch):
    async def refuse(to, body):
        raise WhatsAppError("refused", detail={"status": 400, "meta_code": 131047})

    monkeypatch.setattr(notifier, "send_text", refuse)
    settings = make_settings(handoff_channel="whatsapp_staff", handoff_target="923001112233")

    with pytest.raises(NotificationError) as caught:
        await notifier.send(note(), settings)

    assert "131047" in caught.value.message and "923001112233" not in caught.value.message


async def test_whatsapp_staff_needs_a_number():
    settings = make_settings(handoff_channel="whatsapp_staff", handoff_target="")
    with pytest.raises(ConfigError):
        await notifier.send(note(), settings)


# ---- log


async def test_the_log_channel_writes_the_notification_to_the_log(caplog):
    settings = make_settings(handoff_channel="log", app_env="local")
    with caplog.at_level("INFO", logger=notifier.log.name):
        await notifier.send(note(), settings)
    assert any(record.message == "handoff_notification" for record in caplog.records)


async def test_the_log_channel_is_refused_in_production():
    settings = make_settings(handoff_channel="log", app_env="production")
    with pytest.raises(ConfigError, match="local use only"):
        await notifier.send(note(), settings)


async def test_an_unknown_channel_is_a_config_error():
    with pytest.raises(ConfigError):
        await notifier.send(note(), make_settings(handoff_channel="pigeon"))


# ---- owner alerts


async def test_notify_owner_returns_true_when_it_was_sent(monkeypatch):
    sent = []

    async def fake_send(notification, settings=None):
        sent.append(notification)

    monkeypatch.setattr(notifier, "send", fake_send)

    assert await notifier.notify_owner("Budget at 80 %\nline two", "4111 1111 1111 1111") is True

    (n,) = sent
    assert "\n" not in n.subject and n.urgent is True
    assert "4111" not in n.body


async def test_notify_owner_never_raises(monkeypatch):
    async def broken(notification, settings=None):
        raise NotificationError("slack down")

    monkeypatch.setattr(notifier, "send", broken)
    assert await notifier.notify_owner("alert", "body") is False

    async def worse(notification, settings=None):
        raise RuntimeError("bug")

    monkeypatch.setattr(notifier, "send", worse)
    assert await notifier.notify_owner("alert", "body") is False
