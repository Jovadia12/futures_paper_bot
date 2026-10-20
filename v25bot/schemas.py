"""Webhook payload schemas.

``v25.webhook.v1`` is emitted by ``pine/V25_webhook.pine`` and ``v26.webhook.v1``
by ``pine/V26_webhook.pine``. Both share the same event shapes; V26 entries also
carry the engine limits the script sized with (``caps``) and the setup that
produced them (``setup``), and V26 bar events may carry ``diag``. Unknown fields
are rejected (``extra="forbid"``) so a payload from a different or modified
script cannot be half-understood. The legacy prototype payload is still accepted
when ALLOW_LEGACY_SCHEMA=true and is translated into an ``EntryEvent``.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

SCHEMA_V1 = "v25.webhook.v1"
SCHEMA_V26 = "v26.webhook.v1"
SCHEMA_STRATEGY = {SCHEMA_V1: "V25", SCHEMA_V26: "V26"}
V26_HTF_MINUTES = (5, 10, 15, 30, 45, 60, 120, 240)
V25_SIZING_MODES = ("V22 Baseline", "Prop Conservative", "legacy")
V26_SIZING_MODES = ("Engine limits", "Engine limits + prop budget")

EventId = Annotated[str, Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9_\-:.|!]+$")]
BotId = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_\-]+$")]
SymbolStr = Annotated[str, Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_:!]+$")]
Price = Annotated[float, Field(gt=0, lt=1_000_000)]
EpochMs = Annotated[int, Field(gt=0)]


def _finite(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("must be finite")
    return v


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["v25.webhook.v1", "v26.webhook.v1"] = Field(alias="schema")
    strategy: Literal["V25", "V26"]
    strategy_version: Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.\-]+$")]
    bot_id: BotId
    event_id: EventId
    symbol: SymbolStr
    contract: SymbolStr | None = None
    timeframe: Annotated[str, Field(max_length=8)]
    bar_time: int = Field(gt=0)
    sent_at: int = Field(gt=0, description="TradingView timenow, epoch milliseconds")
    passphrase: Annotated[str, Field(max_length=200)] = ""

    @model_validator(mode="after")
    def _schema_matches_strategy(self):
        if SCHEMA_STRATEGY[self.schema_] != self.strategy:
            raise ValueError(f"schema {self.schema_} is not valid for strategy {self.strategy}")
        return self

    @property
    def is_v26(self) -> bool:
        return self.schema_ == SCHEMA_V26


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ----------------------------------------------------------------------------- V26 entry detail
class Caps(_Strict):
    """Engine limits the Pine script sized the trade with (inputs in section 7)."""

    max_contracts: int = Field(ge=1, le=100)
    max_trade_risk: float = Field(gt=0)
    cost_reserve: float = Field(ge=0)
    slippage_ticks: int = Field(ge=0, le=100)


class Candle(_Strict):
    t: EpochMs
    h: Price
    l: Price  # noqa: E741 - matches the Pine payload

    @model_validator(mode="after")
    def _hl(self) -> "Candle":
        if self.h < self.l:
            raise ValueError("candle high below low")
        return self


class FvgInfo(_Strict):
    """The inverted opposing 1-minute FVG with its three original candles."""

    c1: Candle
    c2: Candle
    c3: Candle
    top: Price
    bottom: Price


class Candidate(_Strict):
    p: Price
    t: EpochMs


class Setup(_Strict):
    variant: Literal["core", "filtered"]
    htf_tf: int
    zone_lo: Price
    zone_hi: Price
    zone_time: EpochMs
    tap_time: EpochMs
    fvg: FvgInfo
    stop_anchor: Literal["structure", "boundary"]
    stop_buffer_ticks: int = Field(ge=0, le=1000)
    min_gap_ticks: int = Field(ge=1, le=1000)
    target_time: EpochMs
    target_rr: float = Field(gt=0, lt=1000)
    min_rr: float = Field(ge=2.0, lt=1000)
    max_rr: float = Field(ge=0, lt=1000)
    candidates: list[Candidate] = Field(min_length=1, max_length=5)

    @field_validator("htf_tf")
    @classmethod
    def _tf(cls, v: int) -> int:
        if v not in V26_HTF_MINUTES:
            raise ValueError(f"htf_tf must be one of {V26_HTF_MINUTES}")
        return v


# ----------------------------------------------------------------------------- V26 bar diagnostics
DIAG_KINDS = ("htf_fvg", "htf_invalid", "tap", "armed", "not_armed", "disarm", "opp_selected",
              "inversion", "entry", "blocked", "sweep")
DiagPrice = Annotated[float, Field(gt=0, lt=1_000_000)]


class DiagItem(_Strict):
    k: Literal["htf_fvg", "htf_invalid", "tap", "armed", "not_armed", "disarm", "opp_selected",
               "inversion", "entry", "blocked", "sweep"]
    dir: Literal[-1, 1]
    tf: int | None = Field(default=None, ge=1, le=240)
    lo: DiagPrice | None = None
    hi: DiagPrice | None = None
    t: EpochMs | None = None
    msg: Annotated[str, Field(max_length=160)] = ""


class DiagSetup(_Strict):
    armed: bool
    tf: int | None = Field(default=None, ge=1, le=240)
    zone_lo: DiagPrice | None = None
    zone_hi: DiagPrice | None = None
    tap_time: EpochMs | None = None
    opp_lo: DiagPrice | None = None
    opp_hi: DiagPrice | None = None
    opp_time: EpochMs | None = None


class Diag(_Strict):
    variant: Literal["core", "filtered"]
    zones_bull: int = Field(ge=0, le=10_000)
    zones_bear: int = Field(ge=0, le=10_000)
    zones_tf: Annotated[str, Field(max_length=200, pattern=r"^[0-9:/,]*$")]
    long: DiagSetup
    short: DiagSetup
    items: list[DiagItem] = Field(default_factory=list, max_length=20)


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
    sizing_mode: Literal["V22 Baseline", "Prop Conservative", "legacy", "Engine limits", "Engine limits + prop budget"]
    comment: Annotated[str, Field(max_length=60)] = ""
    caps: Caps | None = None
    setup: Setup | None = None

    _fin = field_validator("price", "est_fill", "stop", "target", "tick_size", "point_value")(_finite)

    @model_validator(mode="after")
    def _bracket(self) -> "EntryEvent":
        if self.is_v26:
            if self.caps is None or self.setup is None:
                raise ValueError("V26 entries must include caps and setup")
            if self.sizing_mode not in V26_SIZING_MODES:
                raise ValueError(f"V26 sizing_mode must be one of {V26_SIZING_MODES}")
        else:
            if self.caps is not None or self.setup is not None:
                raise ValueError("caps and setup are only valid in v26.webhook.v1 entries")
            if self.sizing_mode not in V25_SIZING_MODES:
                raise ValueError(f"V25 sizing_mode must be one of {V25_SIZING_MODES}")
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
    diag: Diag | None = None

    @model_validator(mode="after")
    def _ohlc(self) -> "BarEvent":
        if self.diag is not None and not self.is_v26:
            raise ValueError("diag is only valid in v26.webhook.v1 bar events")
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
