"""Phase 11, step 6: tell staff about a handoff (and the owner about an alert).

    n = build_handoff_notification(handoff_id, reason, urgency, summary, created_at, wa_id)
    await send(n)                              # through HANDOFF_CHANNEL, raises NotificationError

    result = await notify_pending(session)     # every open handoff nobody was told about yet
    result.sent, result.failed                 # the caller commits

    ok = await notify_owner("Budget at 80 %", "...")   # an alert; never raises, returns True/False

    uv run python -m app.handoff.notifier           # tell staff about pending handoffs now
    uv run python -m app.handoff.notifier --test    # send one test message: is the channel set up?

Channels (HANDOFF_CHANNEL, HANDOFF_TARGET):

    email           target = one or more addresses, comma separated. Needs SMTP_HOST, SMTP_PORT
                    (587 = STARTTLS, 465 = SSL), SMTP_USER, SMTP_PASSWORD, SMTP_FROM. Standard
                    library only (smtplib in a thread), no new dependency.
    slack           target = a Slack incoming-webhook URL (https://hooks.slack.com/...). It is a
                    secret: it is never logged and never put in an error message.
    whatsapp_staff  target = the staff number (digits, country code). Meta only allows a free text
                    to a number that wrote to the bot in the last 24 hours, so this channel works
                    only for a staff phone that is in a conversation with the bot.
    log             prints the notification to the log. Local development only: in
                    APP_ENV=production it is refused, because nobody would read it.

What this module does and does not do (Blueprint 8.6):

* handoff/service.py writes the `handoffs` row; this module tells a person. The row keeps the
  result: `notified_at` when staff were told, `notify_error` when that failed. A handoff is
  NEVER lost because the notification failed: the row stays open and the next call to
  `notify_pending` tries again.
* The text holds the summary from the service (reason, language, product, the last customer
  lines) and the LAST FOUR digits of the customer's number, enough to find the chat in the
  WhatsApp Business app, not the whole number. Card-like numbers are masked once more here.
* Slack gets `&`, `<` and `>` escaped, so customer text cannot ping @channel or make a link.
  A subject or an address never carries a line break, so a customer cannot add mail headers.
* One call to the channel, no retry loop inside: a failed handoff stays pending and the next
  pass (the worker after every handoff, or the CLI from a scheduler) tries again.
* `FOR UPDATE SKIP LOCKED`: two workers running `notify_pending` at the same time never tell
  staff twice about the same handoff.

Wiring into the worker (call `notify_pending` after the handoff is committed, so a slow mail
server never delays the customer's reply) is step 7. Nothing calls this module yet.
"""

import argparse
import asyncio
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConfigError, NotificationError, WhatsAppError
from app.core.logging import setup_logging
from app.core.settings import Settings, get_settings
from app.db.session import get_engine, get_sessionmaker
from app.db.tables import Conversation, Handoff
from app.handoff.service import STATUS_OPEN
from app.integrations.whatsapp.client import send_text
from app.security.redaction import redact_cards

log = logging.getLogger(__name__)

SLACK_URL_PREFIX = "https://hooks.slack.com/"
HTTP_TIMEOUT_SECONDS = 10
SMTP_TIMEOUT_SECONDS = 15
SMTP_SSL_PORT = 465
MAX_BODY_CHARS = 3000
MAX_SUBJECT_CHARS = 120
MAX_ERROR_CHARS = 300  # what is kept in handoffs.notify_error
PHONE_TAIL_DIGITS = 4

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Notification:
    subject: str  # one line
    body: str
    urgent: bool = False


@dataclass(frozen=True)
class NotifyResult:
    sent: int
    failed: int


# ---- the text


def _clean(text: str) -> str:
    """Mask card numbers and drop control characters. Line breaks and tabs stay."""
    return _CONTROL_CHARS.sub("", redact_cards(text))


def _one_line(text: str, limit: int = MAX_SUBJECT_CHARS) -> str:
    line = " ".join(_clean(text).split())
    return line if len(line) <= limit else line[: limit - 3] + "..."


def _phone_tail(wa_id: str) -> str:
    digits = "".join(ch for ch in wa_id if ch.isdigit())
    return digits[-PHONE_TAIL_DIGITS:] if digits else "????"


def build_handoff_notification(
    handoff_id: int,
    reason: str,
    urgency: str,
    summary: str,
    created_at: datetime | None,
    wa_id: str,
) -> Notification:
    """The message staff read: who, why, since when, what was said. No full phone number."""
    urgent = urgency == "high"
    since = (
        created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if created_at else "just now"
    )
    body = "\n".join(
        [
            f"Handoff #{handoff_id} | urgency: {urgency} | reason: {reason}",
            f"Customer: number ending {_phone_tail(wa_id)}",
            f"Waiting since: {since}",
            "",
            _clean(summary).strip(),
            "",
            "The bot stays quiet for this customer until the pause ends or staff resume it.",
        ]
    )
    prefix = "URGENT " if urgent else ""
    return Notification(
        subject=_one_line(f"[Handoff #{handoff_id}] {prefix}{reason}"),
        body=body[:MAX_BODY_CHARS],
        urgent=urgent,
    )


# ---- the channels


def _escape_slack(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _send_slack(notification: Notification, settings: Settings) -> None:
    url = settings.handoff_target.strip()
    if not url.startswith(SLACK_URL_PREFIX):
        raise ConfigError(f"HANDOFF_TARGET must be a Slack webhook URL ({SLACK_URL_PREFIX}...)")
    marker = ":rotating_light: " if notification.urgent else ""
    text = f"{marker}*{_escape_slack(notification.subject)}*\n{_escape_slack(notification.body)}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http:
            response = await http.post(url, json={"text": text})
    except httpx.HTTPError as exc:  # the message of an httpx error can hold the URL: not used
        raise NotificationError(f"Could not reach Slack ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise NotificationError(f"Slack refused the message (HTTP {response.status_code})")


def _send_email_blocking(notification: Notification, settings: Settings) -> None:
    recipients = [part.strip() for part in settings.handoff_target.split(",") if part.strip()]
    sender = settings.smtp_from.strip() or settings.smtp_user.strip()
    if not recipients:
        raise ConfigError("Set HANDOFF_TARGET to the address that should get the notification")
    if not settings.smtp_host or not sender:
        raise ConfigError("Set SMTP_HOST and SMTP_USER (or SMTP_FROM) in .env to send email")
    if any(re.search(r"[\r\n]", item) for item in [sender, *recipients]):
        raise ConfigError("An email address cannot contain a line break")

    message = EmailMessage()
    message["Subject"] = _one_line(notification.subject)
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    if notification.urgent:
        message["X-Priority"] = "1"
    message.set_content(notification.body)

    port = settings.smtp_port
    try:
        client: smtplib.SMTP
        if port == SMTP_SSL_PORT:
            client = smtplib.SMTP_SSL(
                settings.smtp_host,
                port,
                timeout=SMTP_TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(settings.smtp_host, port, timeout=SMTP_TIMEOUT_SECONDS)
        with client as smtp:
            if port != SMTP_SSL_PORT:
                smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        # only the kind of failure: the text of an SMTP error can echo the login or the address
        raise NotificationError(f"Could not send the email ({type(exc).__name__})") from None


async def _send_email(notification: Notification, settings: Settings) -> None:
    await asyncio.to_thread(_send_email_blocking, notification, settings)


async def _send_whatsapp_staff(notification: Notification, settings: Settings) -> None:
    target = "".join(ch for ch in settings.handoff_target if ch.isdigit())
    if not target:
        raise ConfigError("Set HANDOFF_TARGET to the staff WhatsApp number (digits only)")
    try:
        await send_text(target, f"{notification.subject}\n{notification.body}"[:MAX_BODY_CHARS])
    except WhatsAppError as exc:
        # Meta's code is in .detail; the staff number is never put in the message
        meta_code = exc.detail.get("meta_code")
        raise NotificationError(
            f"WhatsApp refused the staff message (meta code {meta_code})"
        ) from None


def _send_log(notification: Notification, settings: Settings) -> None:
    if settings.app_env == "production":
        raise ConfigError("HANDOFF_CHANNEL=log is for local use only: choose email or slack")
    log.info(
        "handoff_notification",
        extra={
            "notification_subject": notification.subject,
            "notification_body": notification.body,
        },
    )


async def send(notification: Notification, settings: Settings | None = None) -> None:
    """Send one notification through HANDOFF_CHANNEL. Raises NotificationError or ConfigError."""
    chosen = settings or get_settings()
    channel = chosen.handoff_channel
    if channel == "slack":
        await _send_slack(notification, chosen)
    elif channel == "email":
        await _send_email(notification, chosen)
    elif channel == "whatsapp_staff":
        await _send_whatsapp_staff(notification, chosen)
    elif channel == "log":
        _send_log(notification, chosen)
    else:  # the settings validator makes this unreachable; kept so a new channel is not forgotten
        raise ConfigError(f"Unknown HANDOFF_CHANNEL: {channel}")


async def notify_owner(
    subject: str, body: str, *, urgent: bool = True, settings: Settings | None = None
) -> bool:
    """Tell staff or the owner about something that is not a handoff (budget alarm, flagged
    document, sync alarm). Never raises: an alert that fails must not break what raised it.
    """
    notification = Notification(_one_line(subject), _clean(body)[:MAX_BODY_CHARS], urgent)
    try:
        await send(notification, settings)
    except AppError as exc:
        log.warning("owner_alert_failed", extra={"code": exc.code, "detail": exc.message})
        return False
    except Exception:
        log.exception("owner_alert_failed")
        return False
    return True


# ---- the pending handoffs


async def notify_pending(
    session: AsyncSession, settings: Settings | None = None, *, limit: int = 20
) -> NotifyResult:
    """Tell staff about every open handoff that nobody was told about yet. The caller commits.

    Success sets `notified_at` and clears `notify_error`. A failure keeps `notified_at` empty and
    writes the reason (short, no secrets) to `notify_error`, so the next call tries again. A
    handoff that was resolved before anyone was told is skipped: staff do not need that ping.
    """
    chosen = settings or get_settings()
    result = await session.execute(
        select(Handoff, Conversation.wa_id)
        .join(Conversation, Conversation.id == Handoff.conversation_id)
        .where(Handoff.status == STATUS_OPEN, Handoff.notified_at.is_(None))
        .order_by(Handoff.id)
        .limit(limit)
        .with_for_update(skip_locked=True, of=Handoff)
    )
    sent = failed = 0
    for handoff, wa_id in result.all():
        notification = build_handoff_notification(
            handoff.id, handoff.reason, handoff.urgency, handoff.summary, handoff.created_at, wa_id
        )
        try:
            await send(notification, chosen)
        except AppError as exc:
            handoff.notify_error = exc.message[:MAX_ERROR_CHARS]
            error_code = exc.code
        except Exception as exc:  # one broken handoff must not stop the others
            handoff.notify_error = f"unexpected error ({type(exc).__name__})"
            error_code = "unexpected"
        else:
            handoff.notified_at = datetime.now(UTC)
            handoff.notify_error = None
            sent += 1
            log.info("handoff_notified", extra={"handoff_id": handoff.id})
            continue
        failed += 1
        log.warning(
            "handoff_notify_failed", extra={"handoff_id": handoff.id, "code": error_code}
        )
    await session.flush()
    return NotifyResult(sent, failed)


# ---- entry point


async def main(*, test: bool) -> int:
    settings = get_settings()
    setup_logging(settings.log_level)
    if test:
        try:
            await send(
                Notification(
                    "[Handoff test] notification channel works",
                    "This is a test from `python -m app.handoff.notifier --test`.\n"
                    "No customer is waiting.",
                ),
                settings,
            )
        except AppError as exc:
            print(f"ERROR: {exc.message}")
            return 1
        print(f"Test notification sent through '{settings.handoff_channel}'.")
        return 0
    try:
        async with get_sessionmaker()() as session, session.begin():
            result = await notify_pending(session, settings)
    except AppError as exc:
        print(f"ERROR: {exc.message}")
        return 1
    finally:
        await get_engine().dispose()
    print(f"Staff told about {result.sent} handoff(s); {result.failed} failed (will be retried).")
    return 1 if result.failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tell staff about pending handoffs.")
    parser.add_argument("--test", action="store_true", help="send one test message and stop")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(test=args.test)))
