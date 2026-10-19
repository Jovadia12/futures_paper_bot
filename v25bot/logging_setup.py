"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from typing import Any

_REDACTED = "***REDACTED***"
_lock = threading.Lock()
_secrets: set[str] = set()

# Patterns that look like credentials even when the exact value is unknown.
_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(
        r"(?i)(\"?(?:password|passphrase|secret|sec|token|accessToken|mdAccessToken|"
        r"api[_-]?key|authorization)\"?\s*[:=]\s*\"?)([^\"\s,}&]+)"
    ),
    re.compile(r"(/webhook(?:/tradingview)?/)[^/\s?\"']+"),
    re.compile(r"(/paper/mark/)[^/\s?\"']+"),
]


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        with _lock:
            _secrets.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, _REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + _REDACTED, text)
    return text


def redact_obj(obj: Any) -> Any:
    """Redact secrets inside nested dict/list structures (for logs and storage)."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in {
                "passphrase", "password", "secret", "sec", "token", "accesstoken",
                "mdaccesstoken", "cid", "authorization", "webhook_token", "admin_token",
            }:
                out[key] = _REDACTED if value not in (None, "") else value
            else:
                out[key] = redact_obj(value)
        return out
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


_factory_installed = False


def install_redacting_record_factory() -> None:
    """Redact every log record at creation, so no handler (ours, uvicorn's, httpx's,
    pytest's) ever sees a raw secret."""
    global _factory_installed
    if _factory_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # malformed format args: leave untouched
            return record
        record.msg, record.args = redact(message), None
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if extra:
            payload["data"] = redact_obj(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


def setup_logging(level: str = "INFO") -> None:
    install_redacting_record_factory()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn's access log contains the raw URL path (which holds the webhook
    # token). We log requests ourselves with the route template instead.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = [handler]
        logging.getLogger(name).propagate = False
    logging.getLogger("httpx").setLevel("WARNING")


def log(logger: logging.Logger, level: int, msg: str, **data: Any) -> None:
    logger.log(level, msg, extra={"data": data} if data else None)
