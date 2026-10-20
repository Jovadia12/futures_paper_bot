"""Execution engine: turns validated V25 events into protected broker orders."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from . import contracts, v26_integrity
from .alerts import AlertManager
from .brokers.base import (
    AccountSnapshot,
    BracketRequest,
    Broker,
    BrokerError,
    Fill,
    Order,
    OrderRejected,
    OrderRole,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from .clock import Clock, trading_day
from .config import Settings
from .logging_setup import log
from .market_data import Quote, QuoteBook
from .pnl import net_positions, realized_by_day
from .risk import RiskContext, RiskEngine, TradeIntent
from .schemas import BarEvent, EntryEvent, ExitEvent, FlattenEvent, HeartbeatEvent
from .store import Store

logger = logging.getLogger("v25bot.engine")

MANUAL_RESUME_SCOPES = {"account", "protection", "manual", "kill_switch", "reconcile"}


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class ExecutionEngine:
    def __init__(self, settings: Settings, store: Store, broker: Broker, quotes: QuoteBook,
                 clock: Clock, alerts: AlertManager):
        self.s = settings
        self.store = store
        self.broker = broker
        self.quotes = quotes
        self.clock = clock
        self.alerts = alerts
        self.risk = RiskEngine(settings)
        self.lock = asyncio.Lock()
        self._last_entry_fill: datetime | None = None
        self._was_connected = True
        self._reconnect_delay = 0.0
        self._next_reconnect = 0.0
        broker.add_fill_listener(self._on_fill)

    # ================================================================= state
    def day(self, now: datetime | None = None) -> str:
        return trading_day(now or self.clock.now(), self.s.trading_day_roll_hour).isoformat()

    def halt_state(self) -> dict | None:
        if self.s.kill_switch_file and os.path.exists(self.s.kill_switch_file):
            return {"active": True, "scope": "kill_switch", "reason": f"kill switch file {self.s.kill_switch_file} present"}
        halt = self.store.get("halt")
        if halt and halt.get("active") and halt.get("scope") == "day" and halt.get("trading_day") != self.day():
            self.store.put("halt", {"active": False, "cleared_reason": "new trading day", "previous": halt})
            self.alerts.emit("info", "halt_cleared", "daily halt cleared for new trading day")
            return None
        return halt if halt and halt.get("active") else None

    def set_halt(self, scope: str, reason: str) -> None:
        current = self.store.get("halt") or {}
        # Never downgrade a manual-resume halt to a self-clearing daily halt.
        if current.get("active") and current.get("scope") in MANUAL_RESUME_SCOPES and scope == "day":
            return
        if current.get("active") and current.get("scope") == scope and current.get("reason") == reason:
            return
        self.store.put("halt", {
            "active": True, "scope": scope, "reason": reason,
            "since": self.clock.now().isoformat(), "trading_day": self.day(),
        })
        self.alerts.emit("critical" if scope in MANUAL_RESUME_SCOPES else "error", "trading_halted",
                         f"trading halted [{scope}]: {reason}")

    @property
    def needs_reconcile(self) -> bool:
        return bool(self.store.get("needs_reconcile", True))

    def trades_today(self) -> int:
        return sum(1 for o in self.store.orders(self.day(), "entry") if o.get("status") != "rejected")

    def realized_today_from_fills(self) -> float:
        return realized_by_day(self.store.fills(), self.s.trading_day_roll_hour).get(
            trading_day(self.clock.now(), self.s.trading_day_roll_hour), 0.0
        )

    def _roll_day(self, account: AccountSnapshot) -> None:
        today = self.day()
        last = self.store.get("last_trading_day")
        peak = self.store.get("peak_eod_balance", self.s.account_start_balance)
        if last != today:
            if last is not None:
                # First balance observed in a new day == prior session's closing balance (flat overnight).
                peak = max(peak, account.balance)
                self.store.put("peak_eod_balance", peak)
                self.store.audit("INFO", "day_roll", f"trading day {last} -> {today}", {"peak_eod_balance": peak})
            self.store.put("last_trading_day", today)
            self.store.put("intraday_peak_equity", account.equity)
        else:
            self.store.put("intraday_peak_equity", max(self.store.get("intraday_peak_equity", account.equity), account.equity))

    async def _snapshot(self) -> tuple[AccountSnapshot | None, list[Position] | None, list[Order] | None]:
        if not self.broker.is_connected():
            return None, None, None
        try:
            account = await self.broker.get_account()
            positions = await self.broker.get_positions()
            orders = await self.broker.get_orders(open_only=True)
        except BrokerError as exc:
            self.alerts.emit("error", "broker_state_unavailable", f"cannot read broker state: {exc}")
            return None, None, None
        self._roll_day(account)
        return account, positions, orders

    # ================================================================= fills
    async def _on_fill(self, fill: Fill) -> None:
        self.store.add_fill(fill.to_dict(), trading_day(fill.ts, self.s.trading_day_roll_hour).isoformat())
        if fill.role is OrderRole.ENTRY:
            self._last_entry_fill = fill.ts
        log(logger, logging.INFO, "fill", **fill.to_dict())

    async def sync_fills(self) -> int:
        added = 0
        for fill in await self.broker.get_fills():
            if self.store.add_fill(fill.to_dict(), trading_day(fill.ts, self.s.trading_day_roll_hour).isoformat()):
                added += 1
        return added

    def _record_orders(self, orders: list[Order | None], event_id: str | None = None) -> None:
        for order in orders:
            if order is not None:
                self.store.upsert_order(order.to_dict(), self.day(order.created_at), event_id)

    # ================================================================= events
    async def handle_entry(self, evt: EntryEvent) -> tuple[int, dict]:
        async with self.lock:
            code, result = await self._handle_entry(evt)
        if evt.is_v26:
            self._record_entry_outcome(evt, result)
        return code, result

    async def _handle_entry(self, evt: EntryEvent) -> tuple[int, dict]:
        now = self.clock.now()
        base: dict[str, Any] = {"event_id": evt.event_id, "paper_only": self.s.paper_only, "broker": self.broker.name}

        def rejected(code: int, *reasons: str, **extra) -> tuple[int, dict]:
            result = base | {"accepted": False, "status": "rejected", "reasons": list(reasons)} | extra
            self.alerts.emit("warning", "entry_rejected", f"entry {evt.event_id} rejected: {'; '.join(reasons)}",
                             dedupe_seconds=0)
            return code, result

        if evt.strategy_version == "legacy":
            if not self.s.allow_legacy_schema:
                return rejected(422, "legacy payload schema disabled")
        elif self.s.allowed_strategy_versions and evt.strategy_version not in self.s.allowed_strategy_versions:
            return rejected(422, f"strategy_version {evt.strategy_version!r} not in ALLOWED_STRATEGY_VERSIONS")
        if self.s.allowed_bot_ids and evt.bot_id not in self.s.allowed_bot_ids and evt.bot_id != "legacy":
            return rejected(422, f"bot_id {evt.bot_id!r} not allowlisted")
        if evt.timeframe != "1":
            return rejected(422, f"{evt.strategy} entries are only valid from a 1-minute chart (got {evt.timeframe!r})")

        try:
            res = contracts.resolve(
                evt.symbol, now, allowed_root=self.s.symbol_root, roll_days=self.s.roll_days_before_expiry,
                min_days_to_expiry=self.s.min_days_to_expiry, chart_contract=evt.contract,
                allow_unverified_roll_window=self.s.allow_unverified_continuous_in_roll_window,
            )
        except contracts.ContractError as exc:
            return rejected(409, f"contract resolution failed: {exc}")
        contract = res.contract
        base["contract"] = contract.name
        if res.warnings:
            base["warnings"] = list(res.warnings)
            for w in res.warnings:
                self.alerts.emit("warning", "contract_roll", w)
        spec = contract.spec
        if abs(evt.tick_size - spec.tick_size) > 1e-9 or abs(evt.point_value - spec.point_value) > 1e-9:
            return rejected(422, f"alert tick/point value {evt.tick_size}/{evt.point_value} do not match "
                                 f"{contract.root} {spec.tick_size}/{spec.point_value}: wrong chart symbol?")
        if evt.is_v26:
            mismatch = v26_integrity.caps_mismatch(evt, self.s)
            if mismatch:
                return rejected(422, *mismatch)
            broken = v26_integrity.check(evt, self.s)
            if broken:
                return rejected(422, *(f"V26 rule check: {why}" for why in broken))

        # Paper only: the signal's close is the freshest known price. Live adapters
        # must use a real market-data feed, so the signal never stands in for one there.
        if self.broker.is_paper:
            self.quotes.update(Quote(contract.name, last=evt.price, ts=min(now, _from_ms(evt.sent_at)), source="signal"))
        # The signal bar's own OHLC happened before this entry; never replay it against the new position.
        bar_key = f"last_bar_time:{contract.name}"
        self.store.put(bar_key, max(self.store.get(bar_key, 0), evt.bar_time))

        if self.needs_reconcile:
            await self._reconcile()
        account, positions, orders = await self._snapshot()
        if account is not None:
            try:
                await self.sync_fills()
            except BrokerError:
                account = None
        ctx = RiskContext(
            now=now,
            broker_connected=self.broker.is_connected() and not self.needs_reconcile,
            account=account, positions=positions, open_orders=orders,
            quote=self.quotes.get(contract.name), contract=contract,
            trades_today=self.trades_today(),
            realized_today_from_fills=self.realized_today_from_fills(),
            peak_eod_balance=self.store.get("peak_eod_balance", self.s.account_start_balance),
            intraday_peak_equity=self.store.get("intraday_peak_equity", self.s.account_start_balance),
            halt=self.halt_state(),
        )
        intent = TradeIntent(Side(evt.side), evt.qty, evt.stop, evt.target, evt.price)
        decision = self.risk.evaluate(intent, ctx)
        base["risk"] = decision.metrics
        if decision.halt_scope:
            self.set_halt(decision.halt_scope, decision.halt_reason or "risk breach")
        if not decision.approved:
            return rejected(409, *decision.reasons)
        if decision.reasons:
            base["adjustments"] = decision.reasons

        req = BracketRequest(
            client_id=evt.event_id[:64], symbol=contract.name, side=intent.side, qty=decision.qty,
            stop_price=evt.stop, target_price=evt.target,
        )
        try:
            result = await self.broker.place_bracket(req)
        except OrderRejected as exc:
            self.store.upsert_order({"id": f"rej-{evt.event_id}"[:80], "symbol": contract.name, "role": "entry",
                                     "status": "rejected", "reject_reason": str(exc)}, self.day(), evt.event_id)
            return rejected(409, f"broker rejected order: {exc}")
        except BrokerError as exc:
            self.store.put("needs_reconcile", True)
            self.alerts.emit("error", "order_outcome_unknown",
                             f"order placement for {evt.event_id} failed ({exc}); reconciling before further entries")
            return 503, base | {"accepted": False, "status": "error", "reasons": [f"broker error: {exc}"]}

        self._record_orders([result.entry, result.stop, result.target], evt.event_id)
        self.store.put("active_contract", contract.name)
        protection = await self._check_protection(grace=0.0 if self.broker.is_paper else self.s.protection_grace_seconds)
        if not self.broker.is_paper:
            asyncio.get_running_loop().create_task(self._delayed_protection_check())
        fills = [f.to_dict() for f in await self.broker.get_fills() if f.order_id == result.entry.id]
        status = "accepted" if protection["ok"] else "liquidated_unprotected"
        out = base | {
            "accepted": protection["ok"], "status": status, "qty": decision.qty,
            "orders": {"entry": result.entry.id, "stop": result.stop.id if result.stop else None,
                       "target": result.target.id if result.target else None},
            "fills": fills, "protection": protection,
        }
        self.alerts.emit("info" if protection["ok"] else "critical", "entry_" + status,
                         f"{evt.side} {decision.qty} {contract.name} stop {evt.stop} target {evt.target}: {status}",
                         dedupe_seconds=0)
        return 200, out

    async def _delayed_protection_check(self) -> None:
        await asyncio.sleep(self.s.protection_grace_seconds + 0.5)
        async with self.lock:
            await self._check_protection(grace=self.s.protection_grace_seconds)

    async def handle_exit(self, evt: ExitEvent) -> tuple[int, dict]:
        async with self.lock:
            positions = await self._positions_or_none()
            open_qty = {p.symbol: p.qty for p in positions or []}
            msg = (f"TradingView reports {evt.strategy} trade closed at {evt.exit_price} ({evt.exit_id or 'exit'}); "
                   f"broker positions: {open_qty or 'flat'}")
            if not open_qty:
                self.alerts.emit("info", "tv_exit", msg, dedupe_seconds=0)
                return 200, {"event_id": evt.event_id, "status": "noted", "broker_flat": True}
            if self.s.tv_exit_action == "flatten":
                self.alerts.emit("warning", "tv_exit_flatten", msg + " -> flattening to stay in sync", dedupe_seconds=0)
                ok = await self._flatten_all("TradingView exit event")
                return 200, {"event_id": evt.event_id, "status": "flattened" if ok else "flatten_failed"}
            self.alerts.emit("warning", "tv_exit_divergence", msg + " (TV_EXIT_ACTION=log)", dedupe_seconds=0)
            return 200, {"event_id": evt.event_id, "status": "noted", "broker_flat": False}

    async def handle_flatten(self, evt: FlattenEvent) -> tuple[int, dict]:
        async with self.lock:
            ok = await self._flatten_all(f"TradingView flatten: {evt.reason}")
            return 200, {"event_id": evt.event_id, "status": "flattened" if ok else "flatten_failed", "reason": evt.reason}

    async def handle_bar(self, evt: BarEvent) -> tuple[int, dict]:
        now = self.clock.now()
        try:
            res = contracts.resolve(
                evt.symbol, now, allowed_root=self.s.symbol_root, roll_days=self.s.roll_days_before_expiry,
                min_days_to_expiry=0, chart_contract=evt.contract,
                allow_unverified_roll_window=True,
            )
        except contracts.ContractError as exc:
            return 409, {"status": "ignored", "reason": str(exc)}
        symbol = res.contract.name
        # Diagnostics first: the entry on this bar already advanced last_bar_time, and the
        # bar event's diagnostics describe that same bar. They are idempotent per bar.
        if evt.diag is not None:
            self._record_diag(evt)
        key = f"last_bar_time:{symbol}"
        if evt.bar_time <= self.store.get(key, 0):
            return 200, {"status": "ignored", "reason": "bar not newer than last processed bar"}
        self.store.put(key, evt.bar_time)
        self.store.put("tv_last_seen", now.isoformat())
        ts = min(now, _from_ms(evt.sent_at))
        if not self.broker.is_paper:
            # A real broker's risk checks need a licensed market-data feed, never chart bars.
            return 200, {"status": "noted", "contract": symbol}
        async with self.lock:
            await self.broker.on_bar(symbol, evt.open, evt.high, evt.low, evt.close, ts)
        return 200, {"status": "processed", "contract": symbol}

    # ================================================================= V26 diagnostics
    def _diag_allowed(self, evt) -> bool:
        return ((not self.s.allowed_strategy_versions or evt.strategy_version in self.s.allowed_strategy_versions)
                and (not self.s.allowed_bot_ids or evt.bot_id in self.s.allowed_bot_ids))

    def _record_diag(self, evt: BarEvent) -> None:
        if not self._diag_allowed(evt):
            return
        d = evt.diag
        rows = [(i, {"kind": it.k, "dir": it.dir, "tf": it.tf, "lo": it.lo, "hi": it.hi, "t": it.t, "msg": it.msg})
                for i, it in enumerate(d.items)]
        self.store.add_diagnostics(evt.bot_id, evt.strategy_version, evt.bar_time, rows)
        states = self.store.get("strategy_state", {})
        if evt.bar_time >= (states.get(evt.bot_id) or {}).get("bar_time", 0):
            states[evt.bot_id] = {
                "bar_time": evt.bar_time, "received_at": self.clock.now().isoformat(),
                "strategy_version": evt.strategy_version, "variant": d.variant,
                "zones_bull": d.zones_bull, "zones_bear": d.zones_bear, "zones_tf": d.zones_tf,
                "long": d.long.model_dump(), "short": d.short.model_dump(), "close": evt.close,
            }
            self.store.put("strategy_state", states)
        if evt.bar_time % (60 * 60_000) == 0:  # hourly
            self.store.prune_diagnostics(evt.bar_time - self.s.diagnostics_retention_days * 86_400_000)

    def _record_entry_outcome(self, evt: EntryEvent, result: dict) -> None:
        """Engine-side rows next to the script's own: the alert arrived, and what the engine did with it."""
        side = 1 if evt.side == "buy" else -1
        accepted = bool(result.get("accepted"))
        reasons = "; ".join(result.get("reasons") or []) or result.get("status", "")
        st = evt.setup
        rows = [
            (1000, {"kind": "alert_received", "dir": side, "tf": st.htf_tf, "lo": evt.stop, "hi": evt.target,
                    "t": evt.bar_time, "msg": f"{evt.side} {evt.qty} @ {evt.price}"}),
            (1001, {"kind": "order_accepted" if accepted else "order_rejected", "dir": side, "tf": st.htf_tf,
                    "lo": evt.stop, "hi": evt.target, "t": evt.bar_time,
                    "msg": (f"qty {result.get('qty')}" if accepted else reasons)[:500]}),
        ]
        self.store.add_diagnostics(evt.bot_id, evt.strategy_version, evt.bar_time, rows)

    def diagnostics(self, limit: int = 200, kind: str | None = None, since_ms: int | None = None) -> dict:
        return {
            "items": self.store.recent_diagnostics(limit, kind, since_ms),
            "counts": self.store.diagnostic_counts(since_ms),
            "strategy_state": self.store.get("strategy_state", {}),
        }

    async def handle_heartbeat(self, evt: HeartbeatEvent) -> tuple[int, dict]:
        self.store.put("tv_last_seen", self.clock.now().isoformat())
        return 200, {"status": "ok"}

    async def push_quote(self, quote: Quote) -> None:
        """Paper only: drive the simulator with a price from a feed you are licensed to use."""
        if not self.broker.is_paper:
            raise BrokerError("push_quote is only available for the paper broker")
        async with self.lock:
            await self.broker.on_quote(quote)

    # ================================================================= protection
    async def _positions_or_none(self) -> list[Position] | None:
        try:
            return await self.broker.get_positions()
        except BrokerError:
            return None

    async def _check_protection(self, grace: float) -> dict:
        try:
            positions = await self.broker.get_positions()
            orders = await self.broker.get_orders(open_only=True)
        except BrokerError as exc:
            return {"ok": False, "checked": False, "reason": f"cannot read broker state: {exc}"}
        now = self.clock.now()
        problems = []
        for pos in positions:
            if pos.qty == 0:
                continue
            need = Side.SELL if pos.qty > 0 else Side.BUY
            covered = sum(
                o.remaining for o in orders
                if o.symbol == pos.symbol and o.type is OrderType.STOP and o.side is need
                and o.status in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED)
            )
            if covered >= abs(pos.qty):
                continue
            if grace and self._last_entry_fill and (now - self._last_entry_fill).total_seconds() < grace:
                continue
            problems.append({"symbol": pos.symbol, "position": pos.qty, "stop_coverage": covered})
        if not problems:
            return {"ok": True, "checked": True}
        for p in problems:
            self.alerts.emit("critical", "unprotected_position",
                             f"position {p['position']} {p['symbol']} has stop coverage {p['stop_coverage']}: "
                             "liquidating and halting", dedupe_seconds=0)
        self.set_halt("protection", "entry filled without full broker-side stop protection")
        flattened = await self._flatten_all("unprotected position")
        return {"ok": False, "checked": True, "problems": problems, "liquidated": flattened}

    # ================================================================= flatten / kill
    async def flatten_all(self, reason: str) -> bool:
        async with self.lock:
            return await self._flatten_all(reason)

    async def _flatten_all(self, reason: str) -> bool:
        symbols: set[str] = set()
        try:
            symbols |= {p.symbol for p in await self.broker.get_positions() if p.qty != 0}
            symbols |= {o.symbol for o in await self.broker.get_orders(open_only=True)}
        except BrokerError as exc:
            self.alerts.emit("critical", "flatten_failed",
                             f"FLATTEN ({reason}) cannot read broker state: {exc}. MANUAL INTERVENTION REQUIRED",
                             dedupe_seconds=0)
            self.set_halt("account", f"flatten could not verify broker state ({reason})")
            return False
        ok = True
        for symbol in sorted(symbols):
            ok = await self._flatten_symbol(symbol, reason) and ok
        if symbols:
            self.alerts.emit("warning" if ok else "critical", "flatten",
                             f"flatten ({reason}) {'complete' if ok else 'FAILED'} for {sorted(symbols)}", dedupe_seconds=0)
        return ok

    async def _flatten_symbol(self, symbol: str, reason: str) -> bool:
        last_exc: Exception | None = None
        for attempt in range(max(1, self.s.flatten_retry_attempts)):
            try:
                created = await self.broker.flatten(symbol)
                self._record_orders(created)
                # Wait for the broker to report flat before re-sending, so a slow
                # liquidation is never doubled into an opposite position.
                for _ in range(1 if self.broker.is_paper else 10):
                    remaining = [p for p in await self.broker.get_positions() if p.symbol == symbol and p.qty != 0]
                    working = [o for o in await self.broker.get_orders(open_only=True) if o.symbol == symbol]
                    if not remaining and not working:
                        return True
                    if not self.broker.is_paper:
                        await asyncio.sleep(0.5)
                last_exc = BrokerError(f"after flatten: position {remaining}, open orders {len(working)}")
            except BrokerError as exc:
                last_exc = exc
            await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))
        self.alerts.emit("critical", "flatten_failed",
                         f"FLATTEN {symbol} FAILED after {self.s.flatten_retry_attempts} attempts ({reason}): "
                         f"{last_exc}. MANUAL INTERVENTION REQUIRED", dedupe_seconds=0)
        self.set_halt("account", f"flatten failed for {symbol}")
        return False

    async def engage_kill_switch(self, reason: str) -> dict:
        async with self.lock:
            self.store.put("halt", {"active": True, "scope": "kill_switch", "reason": reason,
                                    "since": self.clock.now().isoformat(), "trading_day": self.day()})
            self.alerts.emit("critical", "kill_switch", f"KILL SWITCH engaged: {reason}", dedupe_seconds=0)
            ok = await self._flatten_all(f"kill switch: {reason}")
            return {"kill_switch": True, "flattened": ok}

    async def resume(self, actor: str) -> tuple[bool, str]:
        async with self.lock:
            if self.s.kill_switch_file and os.path.exists(self.s.kill_switch_file):
                return False, f"remove kill switch file {self.s.kill_switch_file} first"
            if not self.broker.is_connected():
                return False, "broker disconnected"
            rec = await self._reconcile()
            if not rec.get("ok"):
                return False, f"reconciliation failed: {rec.get('issues') or rec.get('reason')}"
            protection = await self._check_protection(grace=0.0)
            if not protection["ok"]:
                return False, "positions are not fully protected"
            previous = self.store.get("halt")
            self.store.put("halt", {"active": False, "cleared_by": actor, "cleared_at": self.clock.now().isoformat(),
                                    "previous": previous})
            self.alerts.emit("warning", "resume", f"trading resumed by {actor}", dedupe_seconds=0)
            return True, "resumed"

    # ================================================================= reconciliation
    async def reconcile(self) -> dict:
        async with self.lock:
            return await self._reconcile()

    async def _reconcile(self) -> dict:
        if not self.broker.is_connected():
            self.store.put("needs_reconcile", True)
            return {"ok": False, "reason": "broker disconnected"}
        try:
            positions = await self.broker.get_positions()
            orders = await self.broker.get_orders(open_only=True)
            await self.sync_fills()
        except BrokerError as exc:
            self.store.put("needs_reconcile", True)
            return {"ok": False, "reason": f"broker state unavailable: {exc}"}
        issues = []
        broker_pos = {p.symbol: p.qty for p in positions if p.qty != 0}
        fill_pos = net_positions(self.store.fills())
        if broker_pos != fill_pos:
            issues.append(f"position mismatch: broker {broker_pos} vs fills {fill_pos}")
        known = {o["id"] for o in self.store.orders()}
        unknown = [o.id for o in orders if o.id not in known]
        if unknown:
            issues.append(f"unknown working orders at broker: {unknown}")
        for order in orders:
            if order.id in known:
                self.store.upsert_order(order.to_dict(), self.day(order.created_at))
        # Cancel exit orders orphaned by a flat position.
        for order in orders:
            if (order.role in (OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT) and order.status is not OrderStatus.PENDING
                    and broker_pos.get(order.symbol, 0) == 0):
                try:
                    await self.broker.cancel_order(order.id)
                    self.alerts.emit("warning", "orphan_order", f"cancelled orphaned exit order {order.id}")
                except BrokerError as exc:
                    issues.append(f"cannot cancel orphan {order.id}: {exc}")
        if issues:
            self.set_halt("reconcile", "; ".join(issues))
            self.store.put("needs_reconcile", False)
            return {"ok": False, "issues": issues}
        self.store.put("needs_reconcile", False)
        self.store.put("last_reconcile", self.clock.now().isoformat())
        return {"ok": True, "positions": broker_pos, "open_orders": len(orders)}

    # ================================================================= supervision
    async def supervise(self, do_reconcile: bool) -> dict:
        """One monitor cycle. Returns a summary for tests and /status."""
        async with self.lock:
            now = self.clock.now()
            summary: dict[str, Any] = {}
            if self.s.kill_switch_file and os.path.exists(self.s.kill_switch_file):
                halt = self.store.get("halt") or {}
                if not (halt.get("active") and halt.get("scope") == "kill_switch"):
                    self.store.put("halt", {"active": True, "scope": "kill_switch", "reason": "kill switch file",
                                            "since": now.isoformat(), "trading_day": self.day()})
                    self.alerts.emit("critical", "kill_switch", "KILL SWITCH file detected", dedupe_seconds=0)
                    await self._flatten_all("kill switch file")
                summary["kill_switch"] = True

            connected = self.broker.is_connected()
            if not connected:
                if self._was_connected:
                    self.alerts.emit("error", "broker_disconnected",
                                     "broker connection lost: new entries blocked; broker-side stops remain working")
                self.store.put("needs_reconcile", True)
                # Real brokers penalise rapid re-login, so back off up to 60 s between attempts.
                if self.broker.is_paper or time.monotonic() >= self._next_reconnect:
                    try:
                        await self.broker.connect()
                        connected = self.broker.is_connected()
                    except BrokerError:
                        connected = False
                    if not connected and not self.broker.is_paper:
                        self._reconnect_delay = min(60.0, max(1.0, self._reconnect_delay * 2))
                        self._next_reconnect = time.monotonic() + self._reconnect_delay
                if connected:
                    self._reconnect_delay = 0.0
                if connected:
                    self.alerts.emit("warning", "broker_reconnected", "broker reconnected; reconciling")
                    do_reconcile = True
            self._was_connected = connected
            summary["connected"] = connected
            if not connected:
                return summary

            if do_reconcile or self.needs_reconcile:
                summary["reconcile"] = await self._reconcile()

            account, positions, _ = await self._snapshot()
            if account is None:
                return summary
            open_positions = [p for p in positions or [] if p.qty != 0]
            breach = self.risk.breach(
                account, self.realized_today_from_fills(),
                self.store.get("peak_eod_balance", self.s.account_start_balance),
                self.store.get("intraday_peak_equity", account.equity),
            )
            if breach:
                self.set_halt(*breach)
                if open_positions:
                    await self._flatten_all(f"risk breach: {breach[1]}")
                summary["breach"] = breach

            if self.risk.must_be_flat(now) and open_positions:
                self.alerts.emit("warning", "session_flatten",
                                 f"outside NY holding window (cutoff {self.s.flatten_time:%H:%M}): flattening",
                                 dedupe_seconds=0)
                await self._flatten_all("NY session cutoff")
                summary["session_flatten"] = True

            for pos in open_positions:
                quote = self.quotes.get(pos.symbol)
                if quote is None or quote.age(now) > self.s.max_quote_age_seconds:
                    self.alerts.emit("warning", "stale_price",
                                     f"price for open position {pos.symbol} is stale; relying on broker-side stop")
                    summary["stale_price"] = True

            if self.s.tv_heartbeat_max_age_seconds > 0 and self.risk.in_entry_window(now):
                seen = self.store.get("tv_last_seen")
                age = (now - datetime.fromisoformat(seen)).total_seconds() if seen else None
                if age is None or age > self.s.tv_heartbeat_max_age_seconds:
                    self.alerts.emit("warning", "tradingview_silent",
                                     f"no TradingView bar/heartbeat for {age if age is not None else 'ever'}s")

            summary["protection"] = await self._check_protection(grace=self.s.protection_grace_seconds)
            return summary

    async def status(self) -> dict:
        account, positions, orders = await self._snapshot()
        return {
            "mode": self.s.mode,
            "paper_only": self.s.paper_only,
            "live_enabled": self.s.live_allowed,
            "broker": self.broker.name,
            "broker_connected": self.broker.is_connected(),
            "needs_reconcile": self.needs_reconcile,
            "halt": self.halt_state(),
            "trading_day": self.day(),
            "trades_today": self.trades_today(),
            "account": account.to_dict() if account else None,
            "positions": [p.to_dict() for p in positions or []],
            "open_orders": [o.to_dict() for o in orders or []],
            "peak_eod_balance": self.store.get("peak_eod_balance", self.s.account_start_balance),
            "drawdown_floor": self.risk.drawdown_floor(
                self.store.get("peak_eod_balance", self.s.account_start_balance),
                self.store.get("intraday_peak_equity", self.s.account_start_balance),
            ),
            "quotes": {k: {"last": q.last, "bid": q.bid, "ask": q.ask, "ts": q.ts.isoformat(), "source": q.source}
                       for k, q in self.quotes.all().items()},
            "last_reconcile": self.store.get("last_reconcile"),
            "tv_last_seen": self.store.get("tv_last_seen"),
            "strategy_state": self.store.get("strategy_state", {}),
        }
