"""Broker abstraction shared by the paper simulator and real adapters."""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Awaitable, Callable


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderStatus(str, Enum):
    PENDING = "pending"          # accepted, waiting for parent fill (OSO child)
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.PENDING, OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED)


class OrderRole(str, Enum):
    ENTRY = "entry"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    FLATTEN = "flatten"
    OTHER = "other"


@dataclass
class Order:
    id: str
    symbol: str
    side: Side
    type: OrderType
    qty: int
    role: OrderRole
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    limit_price: float | None = None
    stop_price: float | None = None
    filled_qty: int = 0
    avg_fill_price: float | None = None
    parent_id: str | None = None
    oco_group: str | None = None
    client_id: str | None = None
    reject_reason: str | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.qty - self.filled_qty)

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("side", "type", "role", "status"):
            d[key] = d[key].value
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        return d


@dataclass
class Fill:
    id: str
    order_id: str
    symbol: str
    side: Side
    qty: int
    price: float
    commission: float
    ts: datetime
    role: OrderRole = OrderRole.OTHER
    realized_pnl: float = 0.0  # gross P&L realised by this fill (before commission)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        d["role"] = self.role.value
        d["ts"] = self.ts.isoformat()
        return d


@dataclass
class Position:
    symbol: str
    qty: int  # signed: >0 long, <0 short
    avg_price: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AccountSnapshot:
    account_id: str
    balance: float            # cash balance including realised P&L and commissions
    realized_pnl_today: float  # net of commissions, broker trading day
    open_pnl: float
    fetched_at: datetime
    verified: bool = True
    source: str = "broker"

    @property
    def equity(self) -> float:
        return self.balance + self.open_pnl

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fetched_at"] = self.fetched_at.isoformat()
        d["equity"] = self.equity
        return d


@dataclass
class BracketRequest:
    client_id: str
    symbol: str
    side: Side
    qty: int
    stop_price: float
    target_price: float
    entry_type: OrderType = OrderType.MARKET
    entry_limit: float | None = None


@dataclass
class BracketResult:
    entry: Order
    stop: Order | None
    target: Order | None
    extra: dict = field(default_factory=dict)


class BrokerError(RuntimeError):
    pass


class BrokerDisconnected(BrokerError):
    pass


class OrderRejected(BrokerError):
    pass


class TradingDisabled(BrokerError):
    """Adapter is not permitted to run with the current configuration."""


FillListener = Callable[[Fill], Awaitable[None] | None]


class Broker(abc.ABC):
    name: str = "base"
    is_paper: bool = True

    def __init__(self) -> None:
        self._fill_listeners: list[FillListener] = []

    def add_fill_listener(self, cb: FillListener) -> None:
        self._fill_listeners.append(cb)

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    @abc.abstractmethod
    async def get_account(self) -> AccountSnapshot: ...

    @abc.abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    async def get_orders(self, open_only: bool = True) -> list[Order]: ...

    @abc.abstractmethod
    async def get_fills(self) -> list[Fill]: ...

    @abc.abstractmethod
    async def place_bracket(self, req: BracketRequest) -> BracketResult: ...

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abc.abstractmethod
    async def flatten(self, symbol: str) -> list[Order]:
        """Cancel every open order for ``symbol`` and close any position at market."""
