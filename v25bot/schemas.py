"""Webhook payload schemas.

``v25.webhook.v1`` is emitted by ``pine/V25_webhook.pine``. Unknown fields are
rejected (``extra="forbid"``) so a payload from a different or modified script
cannot be half-understood. The legacy prototype payload is still accepted
when ALLOW_LEGACY_SCHEMA=true and is translated into an ``EntryEvent``.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

SCHEMA_V1 = "v25.webhook.v1"

EventId = Annotated[str, Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9_\-:.|!]+$")]
BotId = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_\-]+$")]
SymbolStr = Annotated[str, Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_:!]+$")]
Price = Annotated[float, Field(gt=0, lt=1_000_000)]


def _finite(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("must be finite")
    return v


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["v25.webhook.v1"] = Field(alias="schema")
    strategy: Literal["V25"]
    strategy_version: Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.\-]+$")]
    bot_id: BotId
    event_id: EventId
    symbol: SymbolStr
    contract: SymbolStr | None = None
    timeframe: Annotated[str, Field(max_length=8)]
    bar_time: int = Field(gt=0)
    sent_at: int = Field(gt=0, description="TradingView timenow, epoch milliseconds")
    passphrase: Annotated[str, Field(max_length=200)] = ""


class EntryEvent(_Base):
    event: Literal["entry"]
    side: Literal["buy", "sell"]
    qty: int = Field(ge=1, le=100)
    order_type: Literal["market"]
    price: Price
    est_fill: Price
    stop: Price
    target: Price
    tick_size: float = Field(gt=0)
    point_value: float = Field(gt=0)
    risk_per_contract: float = Field(ge=0)
    planned_risk: float = Field(ge=0)
    sizing_mode: Literal["V22 Baseline", "Prop Conservative", "legacy"]
    comment: Annotated[str, Field(max_length=60)] = ""

    _fin = field_validator("price", "est_fill", "stop", "target", "tick_size", "point_value")(_finite)

    @model_validator(mode="after")
    def _bracket(self) -> "EntryEvent":
        if self.side == "buy" and not (self.stop < self.price < self.target):
            raise ValueError("long entry requires stop < price < target")
        if self.side == "sell" and not (self.target < self.price < self.stop):
            raise ValueError("short entry requires target < price < stop")
        return self


class ExitEvent(_Base):
    event: Literal["exit"]
    exit_price: Price
    entry_id: Annotated[str, Field(max_length=40)] = ""
    exit_id: Annotated[str, Field(max_length=40)] = ""
    trade_qty: int = Field(ge=0, le=100, default=0)
    profit: float = 0.0
    closed_trades: int = Field(ge=0, default=0)


class FlattenEvent(_Base):
    event: Literal["flatten"]
    reason: Literal["session_flat", "prop_risk_halt", "manual"]


class BarEvent(_Base):
    event: Literal["bar"]
    open: Price
    high: Price
    low: Price
    close: Price
    volume: float = Field(ge=0, default=0)

    @model_validator(mode="after")
    def _ohlc(self) -> "BarEvent":
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError("inconsistent OHLC")
        return self


class HeartbeatEvent(_Base):
    event: Literal["heartbeat"]
    price: Price | None = None


V1Event = Annotated[
    Union[EntryEvent, ExitEvent, FlattenEvent, BarEvent, HeartbeatEvent],
    Field(discriminator="event"),
]
V1Adapter: TypeAdapter = TypeAdapter(V1Event)


class LegacySignal(BaseModel):
    """Payload accepted by the original v25_paper_bot prototype."""

    model_config = ConfigDict(extra="forbid")

    event_id: EventId
    symbol: SymbolStr
    action: Literal["buy", "sell"]
    qty: int = Field(ge=1, le=100)
    entry: Price
    stop: Price
    target: Price
    timestamp: int = Field(gt=0, description="epoch seconds")

    def to_entry(self) -> EntryEvent:
        return EntryEvent.model_validate({
            "schema": SCHEMA_V1, "strategy": "V25", "strategy_version": "legacy",
            "bot_id": "legacy", "event": "entry", "event_id": self.event_id, "symbol": self.symbol,
            "timeframe": "1", "bar_time": self.timestamp * 1000, "sent_at": self.timestamp * 1000,
            "side": self.action, "qty": self.qty, "order_type": "market", "price": self.entry,
            "est_fill": self.entry, "stop": self.stop, "target": self.target, "tick_size": 0.25,
            "point_value": 2.0, "risk_per_contract": 0, "planned_risk": 0, "sizing_mode": "legacy",
        })
