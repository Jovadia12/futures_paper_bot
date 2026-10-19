"""Operational alerts: structured log, audit table, optional outbound webhook."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import Settings
from .logging_setup import log, redact, redact_obj
from .store import Store

logger = logging.getLogger("v25bot.alerts")

LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR, "critical": logging.CRITICAL}


class AlertManager:
    def __init__(self, settings: Settings, store: Store, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self.store = store
        self._recent: dict[str, float] = {}
        self._transport = transport
        self.sent: list[dict] = []  # in-memory history (tests, /status)
        self._tasks: set[asyncio.Task] = set()

    def emit(self, level: str, kind: str, message: str, dedupe_seconds: float = 60.0, **data) -> None:
        key = f"{level}:{kind}:{message}"
        now = time.monotonic()
        if dedupe_seconds and now - self._recent.get(key, -1e9) < dedupe_seconds:
            return
        self._recent[key] = now
        message = redact(message)
        data = redact_obj(data)
        log(logger, LEVELS.get(level, logging.WARNING), message, alert=kind, **data)
        self.store.audit(level.upper(), kind, message, data)
        record = {"level": level, "kind": kind, "message": message, "data": data}
        self.sent.append(record)
        del self.sent[:-200]
        if self.s.alert_webhook_url and level in ("warning", "error", "critical"):
            try:
                task = asyncio.get_running_loop().create_task(self._post(record))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            except RuntimeError:
                pass  # no running loop (sync context); already logged and audited

    async def _post(self, record: dict) -> None:
        text = f"[V25 {self.s.mode}] {record['level'].upper()} {record['kind']}: {record['message']}"
        fmt = self.s.alert_webhook_format
        body = {"text": text} if fmt == "slack" else {"content": text} if fmt == "discord" else record | {"text": text}
        try:
            async with httpx.AsyncClient(timeout=5.0, transport=self._transport) as client:
                await client.post(self.s.alert_webhook_url.get_secret_value(), json=body)
        except httpx.HTTPError as exc:
            log(logger, logging.ERROR, "alert webhook delivery failed", error=type(exc).__name__)
