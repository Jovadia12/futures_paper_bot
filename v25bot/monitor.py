"""Background supervision loop: reconciliation, connectivity, cutoff, protection."""

from __future__ import annotations

import asyncio
import logging
import time

from .engine import ExecutionEngine
from .logging_setup import log

logger = logging.getLogger("v25bot.monitor")


class Monitor:
    def __init__(self, engine: ExecutionEngine):
        self.engine = engine
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_cycle: float | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="v25-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        s = self.engine.s
        last_reconcile = 0.0
        while not self._stop.is_set():
            started = time.monotonic()
            do_reconcile = started - last_reconcile >= s.reconcile_interval_seconds
            try:
                summary = await self.engine.supervise(do_reconcile)
                if do_reconcile and summary.get("connected"):
                    last_reconcile = started
                self.last_cycle = time.time()
                self.last_error = None
            except Exception as exc:  # the loop must never die silently
                self.last_error = type(exc).__name__
                log(logger, logging.ERROR, "monitor cycle failed", error=repr(exc))
                self.engine.alerts.emit("error", "monitor_error", f"monitor cycle failed: {type(exc).__name__}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=s.monitor_interval_seconds)
            except asyncio.TimeoutError:
                pass
