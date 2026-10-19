from __future__ import annotations

import asyncio
import warnings
from datetime import datetime

import pytest
from pydantic import SecretStr

warnings.filterwarnings("ignore", category=DeprecationWarning)

from v25bot.alerts import AlertManager  # noqa: E402
from v25bot.brokers.paper import PaperBroker  # noqa: E402
from v25bot.clock import NY, FakeClock  # noqa: E402
from v25bot.config import Settings  # noqa: E402
from v25bot.engine import ExecutionEngine  # noqa: E402
from v25bot.market_data import Quote, QuoteBook  # noqa: E402
from v25bot.schemas import BarEvent, EntryEvent, ExitEvent, FlattenEvent  # noqa: E402
from v25bot.store import Store  # noqa: E402

WEBHOOK_TOKEN = "w" * 32
ADMIN_TOKEN = "a" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
# Tuesday 2026-10-20 10:00 New York: inside the V25 entry window, front month MNQZ6.
T0 = datetime(2026, 10, 20, 10, 0, tzinfo=NY)
CONTRACT = "MNQZ6"


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        webhook_token=SecretStr(WEBHOOK_TOKEN),
        admin_token=SecretStr(ADMIN_TOKEN),
        db_path=str(tmp_path / "test.db"),
        kill_switch_file=str(tmp_path / "KILL_SWITCH"),
        flatten_retry_attempts=2,
    )
    base.update(overrides)
    settings = Settings(**base)
    settings.validate_safety()
    return settings


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def common(clock, event: str, kind: str | None = None, **kw) -> dict:
    now = ms(clock.now())
    bar_time = kw.pop("bar_time", now - 60_000)
    d = {
        "schema": "v25.webhook.v1", "strategy": "V25", "strategy_version": "V25-webhook.1",
        "bot_id": "V25-MNQ-1", "event": event,
        "event_id": f"V25-V25-MNQ-1-{bar_time}-{kind or event}",
        "symbol": "CME_MINI:MNQ1!", "contract": "MNQZ2026", "timeframe": "1",
        "bar_time": bar_time, "sent_at": now, "passphrase": "",
    }
    d.update(kw)
    return d


def entry_payload(clock, side="buy", qty=1, price=20000.0, stop=None, target=None, **kw) -> dict:
    stop = stop if stop is not None else (price - 20 if side == "buy" else price + 20)
    target = target if target is not None else (price + 60 if side == "buy" else price - 60)
    d = common(clock, "entry", f"entry-{side}", **kw)
    d.update({
        "side": side, "qty": qty, "order_type": "market", "price": price,
        "est_fill": price + (1 if side == "buy" else -1), "stop": stop, "target": target,
        "tick_size": 0.25, "point_value": 2.0, "risk_per_contract": 50.0,
        "planned_risk": 50.0 * qty, "sizing_mode": "Prop Conservative", "comment": "V25 Bull IFVG",
    })
    d.update({k: v for k, v in kw.items() if k in d})
    return d


def bar_payload(clock, o, h, low, c, bar_time=None, **kw) -> dict:
    bar_time = bar_time if bar_time is not None else ms(clock.now())
    d = common(clock, "bar", bar_time=bar_time, **kw)
    d.update({"open": o, "high": h, "low": low, "close": c, "volume": 100})
    return d


class Harness:
    """Engine + paper broker wired together on a dedicated event loop and fake clock."""

    def __init__(self, tmp_path, start: datetime = T0, **overrides):
        self.tmp_path = tmp_path
        self.loop = asyncio.new_event_loop()
        self.settings = make_settings(tmp_path, **overrides)
        self.clock = FakeClock(start)
        self._build()
        self.run(self.broker.connect())
        self.run(self.engine.reconcile())

    def _build(self):
        self.store = Store(self.settings.db_path)
        self.quotes = QuoteBook()
        self.alerts = AlertManager(self.settings, self.store)
        self.broker = PaperBroker(self.settings, self.clock, self.quotes, self.store)
        self.engine = ExecutionEngine(self.settings, self.store, self.broker, self.quotes, self.clock, self.alerts)

    def restart(self):
        """Simulate a process restart on the same database."""
        self.store.close()
        self._build()
        self.run(self.broker.connect())

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def entry(self, **kw):
        return self.run(self.engine.handle_entry(EntryEvent.model_validate(entry_payload(self.clock, **kw))))

    def bar(self, o, h, low, c, **kw):
        return self.run(self.engine.handle_bar(BarEvent.model_validate(bar_payload(self.clock, o, h, low, c, **kw))))

    def exit_event(self, **kw):
        d = common(self.clock, "exit", **kw)
        d.update({"exit_price": 20010.0, "entry_id": "LONG", "exit_id": "LONG EXIT", "trade_qty": 1,
                  "profit": 10.0, "closed_trades": 1})
        return self.run(self.engine.handle_exit(ExitEvent.model_validate(d)))

    def flatten_event(self, reason="session_flat", **kw):
        d = common(self.clock, "flatten", f"flatten-{reason}", reason=reason, **kw)
        return self.run(self.engine.handle_flatten(FlattenEvent.model_validate(d)))

    def quote(self, last, bid=None, ask=None, symbol=CONTRACT):
        self.run(self.engine.push_quote(Quote(symbol, last=last, bid=bid, ask=ask, ts=self.clock.now(), source="test")))

    def supervise(self, reconcile=False):
        return self.run(self.engine.supervise(reconcile))

    def positions(self):
        return self.run(self.broker.get_positions())

    def open_orders(self):
        return self.run(self.broker.get_orders(open_only=True))

    def account(self):
        return self.run(self.broker.get_account())

    def alert_kinds(self):
        return [a["kind"] for a in self.alerts.sent]

    def close(self):
        self.store.close()
        self.loop.close()


@pytest.fixture
def harness(tmp_path):
    made = []

    def factory(**kw) -> Harness:
        h = Harness(tmp_path / f"h{len(made)}", **kw)
        made.append(h)
        return h

    yield factory
    for h in made:
        h.close()


@pytest.fixture
def h(harness) -> Harness:
    return harness()
