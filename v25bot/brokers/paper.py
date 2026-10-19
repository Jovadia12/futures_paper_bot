"""Paper execution simulator.

Behaves like a broker with server-side order management:

* Market, limit, and stop (stop-market) orders.
* OSO brackets: the entry fills first, then a stop-loss and a take-profit are
  activated for exactly the filled quantity (partial entry fills grow them).
* OCO: a fill on one exit reduces the sibling; a complete exit cancels it.
  Exit orders are reduce-only and can never flip the position.
* Fills walk a synthetic book: ``paper_book_depth`` contracts per price level,
  plus ``paper_slippage_ticks`` adverse slippage on market and stop orders.
  Large orders therefore partially fill at progressively worse prices.
* Stops gapped through fill at the worse market price, not the stop price.
* Limits need a trade-through by default: a touch is not a fill.
* Per-contract commission on every fill.
* Rejections (invalid prices, no market data, injected faults) and client
  disconnects. Server-side orders keep working while the client is
  disconnected, as they would at a real broker.
* State persists to SQLite and survives restarts.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import replace
from datetime import datetime

from ..clock import Clock, trading_day
from ..config import Settings
from ..contracts import spec_for_contract_name
from ..market_data import Quote, QuoteBook, bar_path
from ..store import Store
from .base import (
    AccountSnapshot,
    BracketRequest,
    BracketResult,
    Broker,
    BrokerDisconnected,
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

STATE_KEY = "paper_broker_state"
EPS = 1e-9


def _on_tick(price: float, tick: float) -> bool:
    return abs(price / tick - round(price / tick)) < 1e-6


class PaperBroker(Broker):
    name = "paper"
    is_paper = True

    def __init__(self, settings: Settings, clock: Clock, quotes: QuoteBook, store: Store | None = None):
        super().__init__()
        self.s = settings
        self.clock = clock
        self.quotes = quotes
        self.store = store
        self.account_id = "PAPER-1"
        self._connected = False
        self._outage = False
        self._seq = 0
        self.balance = settings.account_start_balance
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self._reject_rules: list[tuple[OrderRole | None, str]] = []
        self._pending_fills: list[Fill] = []
        self._load()

    # ---------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if self._outage:
            raise BrokerDisconnected("paper broker outage (simulated)")
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        self._save()

    def is_connected(self) -> bool:
        return self._connected

    def simulate_disconnect(self) -> None:
        """Client link down; reconnect attempts fail until ``simulate_reconnect``."""
        self._outage = True
        self._connected = False

    def simulate_reconnect(self) -> None:
        """End the outage; the engine's monitor reconnects on its next cycle."""
        self._outage = False

    def inject_reject(self, role: OrderRole | None, reason: str = "SimulatedReject", count: int = 1) -> None:
        """Reject the next ``count`` orders with ``role`` (None = any role)."""
        for _ in range(count):
            self._reject_rules.append((role, reason))

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerDisconnected("paper broker disconnected")

    # ---------------------------------------------------------------- queries
    async def get_account(self) -> AccountSnapshot:
        self._require_connected()
        now = self.clock.now()
        day = trading_day(now, self.s.trading_day_roll_hour)
        realized = sum(
            f.realized_pnl - f.commission
            for f in self.fills
            if trading_day(f.ts, self.s.trading_day_roll_hour) == day
        )
        open_pnl, verified = 0.0, True
        for pos in self.positions.values():
            if pos.qty == 0:
                continue
            quote = self.quotes.get(pos.symbol)
            if quote is None:
                verified = False
                continue
            spec = spec_for_contract_name(pos.symbol)
            mark = quote.sell_price() if pos.qty > 0 else quote.buy_price()
            open_pnl += (mark - pos.avg_price) * pos.qty * spec.point_value
        return AccountSnapshot(
            account_id=self.account_id,
            balance=round(self.balance, 2),
            realized_pnl_today=round(realized, 2),
            open_pnl=round(open_pnl, 2),
            fetched_at=now,
            verified=verified,
            source="paper",
        )

    async def get_positions(self) -> list[Position]:
        self._require_connected()
        return [replace(p) for p in self.positions.values() if p.qty != 0]

    async def get_orders(self, open_only: bool = True) -> list[Order]:
        self._require_connected()
        return [replace(o) for o in self.orders.values() if (o.status.is_open or not open_only)]

    async def get_fills(self) -> list[Fill]:
        self._require_connected()
        return [replace(f) for f in self.fills]

    # ---------------------------------------------------------------- orders
    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:07d}"

    def _new_order(self, **kw) -> Order:
        now = self.clock.now()
        order = Order(id=self._next_id("PO"), created_at=now, updated_at=now, **kw)
        self.orders[order.id] = order
        return order

    def _take_reject(self, role: OrderRole) -> str | None:
        for i, (r, reason) in enumerate(self._reject_rules):
            if r is None or r == role:
                del self._reject_rules[i]
                return reason
        return None

    def _reject(self, order: Order, reason: str) -> None:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        order.updated_at = self.clock.now()

    def _fresh_quote(self, symbol: str) -> Quote:
        quote = self.quotes.get(symbol)
        if quote is None:
            raise OrderRejected(f"no market data for {symbol}")
        age = quote.age(self.clock.now())
        if age > self.s.max_quote_age_seconds:
            raise OrderRejected(f"market data for {symbol} is stale ({age:.0f}s)")
        return quote

    async def place_bracket(self, req: BracketRequest) -> BracketResult:
        self._require_connected()
        spec = spec_for_contract_name(req.symbol)
        entry = self._new_order(
            symbol=req.symbol, side=req.side, type=req.entry_type, qty=req.qty,
            role=OrderRole.ENTRY, status=OrderStatus.WORKING, client_id=req.client_id,
            limit_price=req.entry_limit,
        )
        try:
            if not isinstance(req.qty, int) or req.qty < 1:
                raise OrderRejected("InvalidQuantity")
            for price in (req.stop_price, req.target_price):
                if not math.isfinite(price) or price <= 0 or not _on_tick(price, spec.tick_size):
                    raise OrderRejected(f"InvalidPrice: {price} is not on the {spec.tick_size} tick grid")
            quote = self._fresh_quote(req.symbol)
            ref = quote.buy_price() if req.side is Side.BUY else quote.sell_price()
            if req.side is Side.BUY and not (req.stop_price < ref < req.target_price):
                raise OrderRejected(f"InvalidPrice: long bracket needs stop < {ref} < target")
            if req.side is Side.SELL and not (req.target_price < ref < req.stop_price):
                raise OrderRejected(f"InvalidPrice: short bracket needs target < {ref} < stop")
            injected = self._take_reject(OrderRole.ENTRY)
            if injected:
                raise OrderRejected(injected)
        except OrderRejected as exc:
            self._reject(entry, str(exc))
            self._save()
            raise

        group = f"OCO-{entry.id}"
        stop = self._new_order(
            symbol=req.symbol, side=req.side.opposite, type=OrderType.STOP, qty=0,
            role=OrderRole.STOP_LOSS, status=OrderStatus.PENDING, stop_price=req.stop_price,
            parent_id=entry.id, oco_group=group, client_id=req.client_id,
        )
        target = self._new_order(
            symbol=req.symbol, side=req.side.opposite, type=OrderType.LIMIT, qty=0,
            role=OrderRole.TAKE_PROFIT, status=OrderStatus.PENDING, limit_price=req.target_price,
            parent_id=entry.id, oco_group=group, client_id=req.client_id,
        )
        if entry.type is OrderType.MARKET:
            self._fill_marketable(entry, quote, ref)
        else:
            self._evaluate_order(entry, quote)
        self._save()
        await self._dispatch()
        return BracketResult(replace(entry), replace(stop), replace(target))

    async def cancel_order(self, order_id: str) -> None:
        self._require_connected()
        order = self.orders.get(order_id)
        if order is None:
            raise BrokerError(f"unknown order {order_id}")
        if order.status.is_open:
            order.status = OrderStatus.CANCELLED
            order.updated_at = self.clock.now()
            self._save()

    async def flatten(self, symbol: str) -> list[Order]:
        self._require_connected()
        for order in self.orders.values():
            if order.symbol == symbol and order.status.is_open:
                order.status = OrderStatus.CANCELLED
                order.updated_at = self.clock.now()
        created: list[Order] = []
        pos = self.positions.get(symbol)
        if pos and pos.qty != 0:
            side = Side.SELL if pos.qty > 0 else Side.BUY
            order = self._new_order(
                symbol=symbol, side=side, type=OrderType.MARKET, qty=abs(pos.qty),
                role=OrderRole.FLATTEN, status=OrderStatus.WORKING,
            )
            injected = self._take_reject(OrderRole.FLATTEN)
            quote = self.quotes.get(symbol)
            if injected or quote is None:
                self._reject(order, injected or "no market data to liquidate against")
                self._save()
                raise OrderRejected(order.reject_reason or "flatten rejected")
            ref = quote.buy_price() if side is Side.BUY else quote.sell_price()
            self._fill_marketable(order, quote, ref)
            created.append(replace(order))
        self._save()
        await self._dispatch()
        return created

    # ---------------------------------------------------------------- price events
    async def on_quote(self, quote: Quote) -> None:
        """Process a price update. Runs even while the client is disconnected."""
        self.quotes.update(quote)
        working = [
            o for o in self.orders.values()
            if o.symbol == quote.symbol and o.status in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED)
        ]
        # Stops before limits: the conservative ordering within one price event.
        working.sort(key=lambda o: (o.type is not OrderType.STOP, o.created_at, o.id))
        for order in working:
            if order.status in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED):
                self._evaluate_order(order, quote)
        if working:
            self._save()
        await self._dispatch()

    async def on_bar(self, symbol: str, o: float, h: float, low: float, c: float, ts: datetime) -> None:
        pos = self.positions.get(symbol)
        sign = 0 if pos is None or pos.qty == 0 else (1 if pos.qty > 0 else -1)
        for price in bar_path(o, h, low, c, sign):
            await self.on_quote(Quote(symbol=symbol, last=price, ts=ts, source="bar"))

    def _evaluate_order(self, order: Order, quote: Quote) -> None:
        tick = spec_for_contract_name(order.symbol).tick_size
        if order.type is OrderType.STOP:
            if order.side is Side.BUY:
                px = quote.ask if quote.ask is not None else quote.last
                if px + EPS >= order.stop_price:
                    self._fill_marketable(order, quote, max(order.stop_price, px))
            else:
                px = quote.bid if quote.bid is not None else quote.last
                if px - EPS <= order.stop_price:
                    self._fill_marketable(order, quote, min(order.stop_price, px))
        elif order.type is OrderType.LIMIT:
            through = tick if self.s.paper_limit_trade_through else 0.0
            if order.side is Side.SELL:
                px = quote.bid if quote.bid is not None else quote.last
                hit = px + EPS >= order.limit_price + through
            else:
                px = quote.ask if quote.ask is not None else quote.last
                hit = px - EPS <= order.limit_price - through
            if hit:
                qty = min(order.remaining, self.s.paper_book_depth)
                self._apply_fill(order, qty, order.limit_price)
        elif order.type is OrderType.MARKET:
            ref = quote.buy_price() if order.side is Side.BUY else quote.sell_price()
            self._fill_marketable(order, quote, ref)

    def _fill_marketable(self, order: Order, quote: Quote, ref: float) -> None:
        tick = spec_for_contract_name(order.symbol).tick_size
        level = 0
        while order.remaining > 0 and order.status in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED):
            chunk = min(order.remaining, self.s.paper_book_depth)
            price = ref + order.side.sign * (self.s.paper_slippage_ticks + level) * tick
            price = round(round(price / tick) * tick, 10)
            filled = self._apply_fill(order, chunk, price)
            if filled == 0:
                break
            level += 1

    def _apply_fill(self, order: Order, qty: int, price: float) -> int:
        spec = spec_for_contract_name(order.symbol)
        pos = self.positions.get(order.symbol) or Position(order.symbol, 0, 0.0)
        if order.role in (OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT, OrderRole.FLATTEN):
            # Reduce-only: never flip or increase the position.
            if pos.qty == 0 or (pos.qty > 0) == (order.side is Side.BUY):
                self._cancel_order_obj(order)
                return 0
            qty = min(qty, abs(pos.qty))
        if qty <= 0:
            return 0
        now = self.clock.now()
        signed = qty * order.side.sign
        realized = 0.0
        if pos.qty != 0 and (pos.qty > 0) != (signed > 0):
            closing = min(abs(pos.qty), qty)
            realized = (price - pos.avg_price) * closing * (1 if pos.qty > 0 else -1) * spec.point_value
        new_qty = pos.qty + signed
        if new_qty == 0:
            avg = 0.0
        elif pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            avg = (pos.avg_price * abs(pos.qty) + price * qty) / abs(new_qty)
        elif (pos.qty > 0) != (new_qty > 0):
            avg = price  # flipped through zero
        else:
            avg = pos.avg_price
        self.positions[order.symbol] = Position(order.symbol, new_qty, round(avg, 10))

        commission = round(qty * self.s.paper_commission_per_contract, 2)
        self.balance += realized - commission
        prev_filled = order.filled_qty
        order.filled_qty += qty
        order.avg_fill_price = (
            price if prev_filled == 0
            else (order.avg_fill_price * prev_filled + price * qty) / order.filled_qty
        )
        order.status = OrderStatus.FILLED if order.remaining == 0 else OrderStatus.PARTIALLY_FILLED
        order.updated_at = now
        fill = Fill(
            id=self._next_id("PF"), order_id=order.id, symbol=order.symbol, side=order.side,
            qty=qty, price=price, commission=commission, ts=now, role=order.role,
            realized_pnl=round(realized, 2),
        )
        self.fills.append(fill)
        self._pending_fills.append(fill)

        if order.role is OrderRole.ENTRY:
            self._activate_children(order, qty)
        if order.oco_group and order.role in (OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT):
            for sib in self._group(order.oco_group):
                if sib.id != order.id and sib.status.is_open:
                    sib.qty = max(sib.filled_qty, sib.qty - qty)
                    if sib.remaining == 0:
                        self._cancel_order_obj(sib)
        if new_qty == 0:
            for other in self.orders.values():
                if (other.symbol == order.symbol and other.status.is_open
                        and other.role in (OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT)):
                    self._cancel_order_obj(other)
        return qty

    def _activate_children(self, entry: Order, qty: int) -> None:
        quote = self.quotes.get(entry.symbol)
        for child in self.orders.values():
            if child.parent_id != entry.id or not child.status.is_open:
                continue
            child.qty += qty
            child.updated_at = self.clock.now()
            if child.status is OrderStatus.PENDING:
                reason = self._take_reject(child.role)
                if reason is None and child.type is OrderType.STOP and quote is not None:
                    # A stop already through the market is rejected, as at a real exchange.
                    if child.side is Side.SELL and quote.sell_price() <= child.stop_price:
                        reason = "InvalidPrice: sell stop at or above market"
                    if child.side is Side.BUY and quote.buy_price() >= child.stop_price:
                        reason = "InvalidPrice: buy stop at or below market"
                if reason:
                    self._reject(child, reason)
                else:
                    child.status = OrderStatus.WORKING

    def _group(self, group: str) -> list[Order]:
        return [o for o in self.orders.values() if o.oco_group == group]

    def _cancel_order_obj(self, order: Order) -> None:
        if order.status.is_open:
            order.status = OrderStatus.CANCELLED
            order.updated_at = self.clock.now()

    async def _dispatch(self) -> None:
        fills, self._pending_fills = self._pending_fills, []
        for fill in fills:
            for cb in self._fill_listeners:
                result = cb(replace(fill))
                if inspect.isawaitable(result):
                    await result

    # ---------------------------------------------------------------- persistence
    def _save(self) -> None:
        if self.store is None:
            return
        self.store.put(STATE_KEY, {
            "seq": self._seq,
            "balance": self.balance,
            "positions": [p.to_dict() for p in self.positions.values()],
            "orders": [o.to_dict() for o in self.orders.values()],
            "fills": [f.to_dict() for f in self.fills],
        })

    def _load(self) -> None:
        if self.store is None:
            return
        state = self.store.get(STATE_KEY)
        if not state:
            return
        self._seq = state["seq"]
        self.balance = state["balance"]
        self.positions = {p["symbol"]: Position(**p) for p in state["positions"]}
        for d in state["orders"]:
            d = dict(d)
            d["side"], d["type"] = Side(d["side"]), OrderType(d["type"])
            d["role"], d["status"] = OrderRole(d["role"]), OrderStatus(d["status"])
            d["created_at"] = datetime.fromisoformat(d["created_at"])
            d["updated_at"] = datetime.fromisoformat(d["updated_at"])
            self.orders[d["id"]] = Order(**d)
        for d in state["fills"]:
            d = dict(d)
            d["side"], d["role"] = Side(d["side"]), OrderRole(d["role"])
            d["ts"] = datetime.fromisoformat(d["ts"])
            self.fills.append(Fill(**d))

