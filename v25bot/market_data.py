"""Quote cache with staleness checks.

The paper simulator needs prices. Without a licensed tick feed, prices come
from (a) V25 ``bar`` alerts (1-minute OHLC), (b) the signal's own close price,
or (c) quotes pushed to ``/paper/quote`` by any feed you are entitled to use.
1-minute OHLC is coarse: intrabar ordering is unknown, so bars are expanded
into a conservative path (see ``bar_path``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Quote:
    symbol: str
    last: float
    ts: datetime
    bid: float | None = None
    ask: float | None = None
    source: str = "unknown"

    def buy_price(self) -> float:
        return self.ask if self.ask is not None else self.last

    def sell_price(self) -> float:
        return self.bid if self.bid is not None else self.last

    def age(self, now: datetime) -> float:
        return (now - self.ts).total_seconds()


class StalePrice(RuntimeError):
    pass


class QuoteBook:
    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}

    def update(self, quote: Quote) -> bool:
        """Store ``quote`` unless an equally new or newer one exists. Returns True if stored."""
        current = self._quotes.get(quote.symbol)
        if current is not None and quote.ts < current.ts:
            return False
        self._quotes[quote.symbol] = quote
        return True

    def get(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    def fresh(self, symbol: str, now: datetime, max_age: float) -> Quote:
        quote = self._quotes.get(symbol)
        if quote is None:
            raise StalePrice(f"no price available for {symbol}")
        age = quote.age(now)
        if age > max_age:
            raise StalePrice(f"price for {symbol} is {age:.0f}s old (max {max_age:.0f}s)")
        if age < -5:
            raise StalePrice(f"price for {symbol} is timestamped {-age:.0f}s in the future")
        return quote

    def all(self) -> dict[str, Quote]:
        return dict(self._quotes)


def bar_path(o: float, h: float, low: float, c: float, position_sign: int) -> list[float]:
    """Order a bar's OHLC so the adverse extreme for an open position comes first.

    Long (+1): O → L → H → C. Short (−1): O → H → L → C. Flat: by candle colour.
    If a stop and target both sit inside one bar's range, the stop fills first:
    the conservative assumption.
    """
    if position_sign > 0:
        return [o, low, h, c]
    if position_sign < 0:
        return [o, h, low, c]
    return [o, low, h, c] if c >= o else [o, h, low, c]
