"""MNQ contract specification and continuous-symbol resolution.

CME Micro E-mini Nasdaq-100 (MNQ):
* Quarterly months: H (Mar), M (Jun), U (Sep), Z (Dec).
* Last trading day: third Friday of the contract month, trading halts 09:30 ET.
* Tick 0.25 index points = $0.50; point value $2.00.
* Liquidity rolls to the next quarter about 8 calendar days before expiry
  (the Thursday of the prior week), which is the default roll date here.

Accepted inputs: ``CME_MINI:MNQ1!``, ``MNQ1!``, ``MNQ2!``, ``MNQ``,
``MNQZ2026``, ``MNQZ26``, ``MNQZ6``, ``CME_MINI:MNQZ2026``.
Output contract names use Tradovate's convention (``MNQZ6``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .clock import NY

QUARTERLY = {3: "H", 6: "M", 9: "U", 12: "Z"}
CODE_TO_MONTH = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                 "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


@dataclass(frozen=True)
class InstrumentSpec:
    root: str
    tick_size: float
    point_value: float
    months: tuple[int, ...]
    last_trade_time_et: time


SPECS = {
    "MNQ": InstrumentSpec("MNQ", 0.25, 2.0, (3, 6, 9, 12), time(9, 30)),
}


class ContractError(ValueError):
    """Symbol cannot be resolved to a tradable contract."""


@dataclass(frozen=True)
class Contract:
    root: str
    month: int
    year: int

    @property
    def spec(self) -> InstrumentSpec:
        return SPECS[self.root]

    @property
    def code(self) -> str:
        return QUARTERLY[self.month]

    @property
    def name(self) -> str:
        """Tradovate-style name, e.g. ``MNQZ6``."""
        return f"{self.root}{self.code}{self.year % 10}"

    @property
    def tradingview_name(self) -> str:
        """TradingView-style name, e.g. ``MNQZ2026``."""
        return f"{self.root}{self.code}{self.year}"

    @property
    def expiry(self) -> date:
        return third_friday(self.year, self.month)

    @property
    def last_trade(self) -> datetime:
        return datetime.combine(self.expiry, self.spec.last_trade_time_et, tzinfo=NY)

    def roll_date(self, roll_days: int) -> date:
        return self.expiry - timedelta(days=roll_days)

    def next(self) -> "Contract":
        months = self.spec.months
        idx = months.index(self.month)
        if idx + 1 < len(months):
            return Contract(self.root, months[idx + 1], self.year)
        return Contract(self.root, months[0], self.year + 1)


@dataclass(frozen=True)
class Resolution:
    contract: Contract
    requested: str
    continuous: bool
    source: str  # "explicit" | "chart_contract" | "roll_schedule"
    warnings: tuple[str, ...] = ()


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7  # Friday == 4
    return first + timedelta(days=offset + 14)


_SYMBOL_RE = re.compile(
    r"^(?:(?P<exchange>[A-Z_]+):)?(?P<root>" + "|".join(sorted(SPECS, key=len, reverse=True)) + r")"
    r"(?:(?P<cont>[1-9])!|(?P<code>[FGHJKMNQUVXZ])(?P<year>\d{4}|\d{2}|\d))?$"
)


def _expand_year(digits: str, today: date) -> int:
    if len(digits) == 4:
        return int(digits)
    if len(digits) == 2:
        return 2000 + int(digits)
    year = today.year - today.year % 10 + int(digits)
    if year < today.year - 1:
        year += 10
    return year


def front_contract(root: str, today: date, roll_days: int, offset: int = 0) -> Contract:
    """Contract a continuous ``ROOT{offset+1}!`` should trade on ``today`` (NY date)."""
    spec = SPECS[root]
    month = next((m for m in spec.months if m >= today.month), None)
    contract = Contract(root, month, today.year) if month else Contract(root, spec.months[0], today.year + 1)
    while today >= contract.roll_date(roll_days):
        contract = contract.next()
    for _ in range(offset):
        contract = contract.next()
    return contract


def parse_symbol(symbol: str, today: date) -> tuple[str, Contract | None, int | None]:
    """Return (root, explicit contract or None, continuous offset or None)."""
    m = _SYMBOL_RE.match(symbol.strip().upper())
    if not m:
        raise ContractError(f"unrecognised or unsupported symbol {symbol!r} (roots: {sorted(SPECS)})")
    root = m.group("root")
    if m.group("code"):
        month = CODE_TO_MONTH[m.group("code")]
        if month not in SPECS[root].months:
            raise ContractError(f"{symbol!r}: {root} has no {m.group('code')} contract month")
        return root, Contract(root, month, _expand_year(m.group("year"), today)), None
    cont = int(m.group("cont")) if m.group("cont") else 1
    return root, None, cont - 1


def resolve(
    symbol: str,
    now: datetime,
    *,
    allowed_root: str = "MNQ",
    roll_days: int = 8,
    min_days_to_expiry: int = 2,
    chart_contract: str | None = None,
    allow_unverified_roll_window: bool = False,
) -> Resolution:
    """Resolve a TradingView symbol to the contract that must actually be traded.

    ``chart_contract`` is TradingView's ``syminfo.current_contract`` for a
    continuous chart. When present, it wins over the calendar roll schedule,
    because the strategy's stop/target prices come from that contract's data.
    """
    today = now.astimezone(NY).date()
    root, explicit, offset = parse_symbol(symbol, today)
    if root != allowed_root:
        raise ContractError(f"symbol root {root} not allowlisted (allowed: {allowed_root})")
    warnings: list[str] = []

    if explicit is not None:
        contract, continuous, source = explicit, False, "explicit"
    else:
        continuous = True
        scheduled = front_contract(root, today, roll_days, offset or 0)
        if chart_contract:
            croot, ccontract, _ = parse_symbol(chart_contract, today)
            if croot != root or ccontract is None:
                raise ContractError(f"chart contract {chart_contract!r} does not match {symbol!r}")
            contract, source = ccontract, "chart_contract"
            if ccontract != scheduled and offset == 0:
                warnings.append(
                    f"TradingView continuous chart is on {ccontract.name}; roll schedule says "
                    f"{scheduled.name}. Trading {ccontract.name} so chart prices match."
                )
        else:
            contract, source = scheduled, "roll_schedule"
            # Between the previous contract's roll date and its expiry, a
            # continuous chart may still show the old contract's prices.
            previous = _previous(scheduled)
            in_window = previous.roll_date(roll_days) <= today <= previous.expiry
            if in_window and not allow_unverified_roll_window:
                raise ContractError(
                    f"{symbol} is in the {previous.name}->{scheduled.name} roll window and the "
                    "alert did not include the chart's underlying contract; refusing because chart "
                    "prices may belong to a different contract month. Use an explicit contract chart "
                    "or the V25 webhook script, which sends syminfo.current_contract."
                )

    if now >= contract.last_trade:
        raise ContractError(f"{contract.name} expired at {contract.last_trade.isoformat()}")
    if (contract.expiry - today).days < min_days_to_expiry:
        raise ContractError(
            f"{contract.name} expires {contract.expiry.isoformat()}, within the "
            f"{min_days_to_expiry}-day minimum; switch the chart to {contract.next().tradingview_name}"
        )
    return Resolution(contract, symbol, continuous, source, tuple(warnings))


def _previous(contract: Contract) -> Contract:
    months = contract.spec.months
    idx = months.index(contract.month)
    if idx > 0:
        return Contract(contract.root, months[idx - 1], contract.year)
    return Contract(contract.root, months[-1], contract.year - 1)


def spec_for_contract_name(name: str) -> InstrumentSpec:
    for root, spec in SPECS.items():
        if name.upper().startswith(root):
            return spec
    raise ContractError(f"no spec for {name!r}")
