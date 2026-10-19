"""Independent pre-trade risk engine.

Every number here comes from the broker (account snapshot, positions, open
orders, fills) and the live quote. TradingView's simulated equity is never
used. The alert's ``qty``/``stop``/``target`` are the strategy's *request*;
this engine decides whether that request is allowed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .brokers.base import AccountSnapshot, Order, Position, Side
from .clock import NY
from .config import Settings
from .contracts import Contract
from .market_data import Quote


@dataclass
class TradeIntent:
    side: Side
    qty: int
    stop: float
    target: float
    signal_price: float


@dataclass
class RiskContext:
    now: datetime
    broker_connected: bool
    account: AccountSnapshot | None
    positions: list[Position] | None
    open_orders: list[Order] | None
    quote: Quote | None
    contract: Contract
    trades_today: int
    realized_today_from_fills: float
    peak_eod_balance: float
    intraday_peak_equity: float
    halt: dict | None = None


@dataclass
class RiskDecision:
    approved: bool
    qty: int
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    halt_scope: str | None = None  # "day" | "account"
    halt_reason: str | None = None


class RiskEngine:
    def __init__(self, settings: Settings):
        self.s = settings

    # ------------------------------------------------------------------ session
    def is_trading_date(self, now: datetime) -> bool:
        ny = now.astimezone(NY)
        return ny.weekday() < 5 and ny.date() not in self.s.holidays

    def in_entry_window(self, now: datetime) -> bool:
        ny = now.astimezone(NY)
        return self.is_trading_date(now) and self.s.entry_window_start <= ny.time() < self.s.entry_window_end

    def must_be_flat(self, now: datetime) -> bool:
        """True outside the holding window [ENTRY_WINDOW_START, FLATTEN_TIME) on trading dates.

        V25 never holds overnight, so any position outside the window is flattened.
        """
        ny = now.astimezone(NY)
        holding = self.is_trading_date(now) and self.s.entry_window_start <= ny.time() < self.s.flatten_time
        return not holding

    # ------------------------------------------------------------------ limits
    def drawdown_floor(self, peak_eod_balance: float, intraday_peak_equity: float) -> float:
        start, dd = self.s.account_start_balance, self.s.max_drawdown_usd
        if self.s.drawdown_mode == "static":
            return start - dd
        peak = peak_eod_balance
        if self.s.drawdown_mode == "intraday_trailing":
            peak = max(peak, intraday_peak_equity)
        return min(start, peak - dd)

    @staticmethod
    def day_pnl(account: AccountSnapshot, realized_from_fills: float) -> float:
        """Conservative day P&L: the worse of broker-reported and fill-derived realised P&L, plus open P&L."""
        return min(account.realized_pnl_today, realized_from_fills) + account.open_pnl

    def breach(self, account: AccountSnapshot, realized_from_fills: float,
               peak_eod_balance: float, intraday_peak_equity: float) -> tuple[str, str] | None:
        """Halt condition from current broker state, independent of any new trade."""
        day_pnl = self.day_pnl(account, realized_from_fills)
        if day_pnl <= -self.s.daily_loss_limit_usd:
            return "day", f"daily loss limit reached: day P&L {day_pnl:.2f} <= -{self.s.daily_loss_limit_usd:.2f}"
        floor = self.drawdown_floor(peak_eod_balance, intraday_peak_equity)
        room = account.equity - floor
        if room <= self.s.drawdown_safety_buffer_usd:
            return "account", (
                f"drawdown limit: equity {account.equity:.2f} is {room:.2f} above floor {floor:.2f} "
                f"(safety buffer {self.s.drawdown_safety_buffer_usd:.2f})"
            )
        return None

    # ------------------------------------------------------------------ pre-trade
    def evaluate(self, intent: TradeIntent, ctx: RiskContext) -> RiskDecision:
        s = self.s
        spec = ctx.contract.spec
        reasons: list[str] = []
        metrics: dict = {"contract": ctx.contract.name, "requested_qty": intent.qty}

        def reject(*why: str, **halt) -> RiskDecision:
            return RiskDecision(False, 0, reasons + list(why), metrics, **halt)

        # 1. Hard stops: halts, connectivity, verifiable state.
        if ctx.halt and ctx.halt.get("active"):
            return reject(f"trading halted ({ctx.halt.get('scope')}): {ctx.halt.get('reason')}")
        if not ctx.broker_connected:
            return reject("broker disconnected: account state cannot be verified")
        if ctx.account is None or ctx.positions is None or ctx.open_orders is None:
            return reject("broker account state unavailable: cannot verify account")
        if not ctx.account.verified:
            return reject("broker account snapshot not verified")
        age = (ctx.now - ctx.account.fetched_at).total_seconds()
        if age > s.account_state_max_age_seconds:
            return reject(f"broker account snapshot is {age:.1f}s old")

        # 2. Session.
        if not self.is_trading_date(ctx.now):
            return reject("not a trading day (weekend or configured holiday)")
        if not self.in_entry_window(ctx.now):
            ny = ctx.now.astimezone(NY)
            return reject(
                f"outside NY entry window {s.entry_window_start:%H:%M}-{s.entry_window_end:%H:%M} "
                f"(now {ny:%H:%M:%S} NY)"
            )

        # 3. Market data.
        if ctx.quote is None:
            return reject("no market price available")
        qage = ctx.quote.age(ctx.now)
        if qage > s.max_quote_age_seconds:
            return reject(f"stale price: {qage:.0f}s old (max {s.max_quote_age_seconds:.0f}s)")

        # 4. Exposure: V25 runs pyramiding=0, so require flat and no working orders.
        if any(p.qty != 0 for p in ctx.positions):
            return reject("existing open position at broker (pyramiding disabled)")
        if any(o.symbol == ctx.contract.name for o in ctx.open_orders):
            return reject("working orders already exist for this contract")
        if ctx.trades_today >= s.max_trades_per_day:
            return reject(f"max trades per day reached ({ctx.trades_today}/{s.max_trades_per_day})")

        # 5. Price sanity against the live market, not the signal.
        tick = spec.tick_size
        ref = ctx.quote.buy_price() if intent.side is Side.BUY else ctx.quote.sell_price()
        est_fill = ref + intent.side.sign * s.risk_entry_slippage_ticks * tick
        deviation = abs(ref - intent.signal_price) / tick
        metrics.update(market_ref=ref, est_fill=est_fill, deviation_ticks=round(deviation, 2))
        for name, price in (("stop", intent.stop), ("target", intent.target)):
            if not math.isfinite(price) or abs(price / tick - round(price / tick)) > 1e-6:
                return reject(f"{name} {price} is not on the {tick} tick grid")
        if deviation > s.max_entry_deviation_ticks:
            return reject(
                f"market {ref} is {deviation:.0f} ticks from signal price {intent.signal_price} "
                f"(max {s.max_entry_deviation_ticks})"
            )
        # The protective stop must sit beyond the side of the book it would trigger on,
        # otherwise it is rejected or fills instantly and the entry is unprotected.
        exit_side_px = ctx.quote.sell_price() if intent.side is Side.BUY else ctx.quote.buy_price()
        if intent.side is Side.BUY and not (intent.stop < min(est_fill, exit_side_px) and est_fill < intent.target):
            return reject(f"long bracket invalid at market {exit_side_px}: need stop {intent.stop} < market "
                          f"and fill {est_fill} < target {intent.target}")
        if intent.side is Side.SELL and not (intent.stop > max(est_fill, exit_side_px) and est_fill > intent.target):
            return reject(f"short bracket invalid at market {exit_side_px}: need stop {intent.stop} > market "
                          f"and fill {est_fill} > target {intent.target}")

        # 6. Size and per-trade risk.
        per_contract = abs(est_fill - intent.stop) * spec.point_value + s.cost_reserve_per_contract_usd
        qty = intent.qty
        if qty < 1:
            return reject("quantity must be >= 1")
        if qty > s.max_contracts:
            if s.risk_sizing_mode == "reduce":
                reasons.append(f"qty reduced {qty} -> {s.max_contracts} (MAX_CONTRACTS)")
                qty = s.max_contracts
            else:
                return reject(f"qty {qty} exceeds MAX_CONTRACTS {s.max_contracts}")
        if qty * per_contract > s.max_trade_risk_usd:
            fit = int(s.max_trade_risk_usd // per_contract)
            if s.risk_sizing_mode == "reduce" and fit >= 1:
                reasons.append(f"qty reduced {qty} -> {fit} (MAX_TRADE_RISK_USD)")
                qty = fit
            else:
                return reject(
                    f"trade risk {qty * per_contract:.2f} exceeds MAX_TRADE_RISK_USD {s.max_trade_risk_usd:.2f} "
                    f"({per_contract:.2f}/contract)"
                )
        trade_risk = qty * per_contract
        metrics.update(qty=qty, risk_per_contract=round(per_contract, 2), trade_risk=round(trade_risk, 2))

        # 7. Account limits from broker state.
        breach = self.breach(ctx.account, ctx.realized_today_from_fills,
                             ctx.peak_eod_balance, ctx.intraday_peak_equity)
        if breach:
            scope, why = breach
            return reject(why, halt_scope=scope, halt_reason=why)
        day_pnl = self.day_pnl(ctx.account, ctx.realized_today_from_fills)
        floor = self.drawdown_floor(ctx.peak_eod_balance, ctx.intraday_peak_equity)
        metrics.update(day_pnl=round(day_pnl, 2), equity=ctx.account.equity, drawdown_floor=floor)
        if day_pnl - trade_risk < -s.daily_loss_limit_usd:
            return reject(
                f"trade risk {trade_risk:.2f} would breach the daily loss limit "
                f"(day P&L {day_pnl:.2f}, limit -{s.daily_loss_limit_usd:.2f})"
            )
        if ctx.account.equity - trade_risk < floor + s.drawdown_safety_buffer_usd:
            return reject(
                f"trade risk {trade_risk:.2f} would breach drawdown floor {floor:.2f} + buffer "
                f"{s.drawdown_safety_buffer_usd:.2f} (equity {ctx.account.equity:.2f})"
            )
        return RiskDecision(True, qty, reasons, metrics)
