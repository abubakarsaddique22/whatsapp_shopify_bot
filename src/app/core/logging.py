"""Structured JSON logging with redaction and a correlation id on every line."""

import json
import logging
import re
import sys
from datetime import UTC, datetime

from app.core.ids import correlation_id_var

# Order matters: more specific patterns first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-~+/=]+", re.I), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:shpat|shpca|shpss|shppa)_[A-Za-z0-9]{8,}"), "[REDACTED_SHOPIFY_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), "[REDACTED_META_TOKEN]"),
    (re.compile(r"sha256=[0-9a-fA-F]{16,}"), "sha256=[REDACTED]"),
    (re.compile(r"(?<![\w.])\+?\d[\d\s\-]{9,14}\d(?![\w.])"), "[PHONE]"),
]

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

# A Shopify id (gid://shopify/Product/1234567890123) has 13 digits and looked like a phone number.
# It is not private (it is the store's own object id) and it is needed to debug, so it is kept.
_SHOPIFY_GID = re.compile(r"gid://shopify/[A-Za-z]+/\d+")
_KEPT = re.compile(r"\x00(\d+)\x00")


def redact(text: str) -> str:
    kept: list[str] = []

    def keep(match: re.Match[str]) -> str:
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    text = _SHOPIFY_GID.sub(keep, text.replace("\x00", ""))  # set aside: the phone pattern
    # cannot touch it (a NUL in the input is dropped first, so it can never fake a placeholder)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return _KEPT.sub(lambda match: kept[int(match.group(1))], text)


class RedactionFilter(logging.Filter):
    """Removes tokens, secrets and phone numbers from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED and isinstance(value, str):
                record.__dict__[key] = redact(value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                data[key] = value
        if record.exc_info:
            data["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(data, default=str, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# Separate logger for attacks / forged requests (also stored in DB later, Phase 11).
security_log = logging.getLogger("security")
