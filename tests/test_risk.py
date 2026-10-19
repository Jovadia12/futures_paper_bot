"""Independent risk engine: every limit is evaluated from broker state."""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from conftest import T0, make_settings
from v25bot.brokers.base import AccountSnapshot, Order, OrderRole, OrderStatus, OrderType, Position, Side
from v25bot.clock import NY
from v25bot.contracts import Contract
from v25bot.market_data import Quote
from v25bot.risk import RiskContext, RiskEngine, TradeIntent

Z6 = Contract("MNQ", 12, 2026)


def ctx(now=T0, **kw) -> RiskContext:
    base = dict(
        now=now, broker_connected=True,
        account=AccountSnapshot("A", 50_000.0, 0.0, 0.0, now),
        positions=[], open_orders=[],
        quote=Quote("MNQZ6", last=20000.0, ts=now),
        contract=Z6, trades_today=0, realized_today_from_fills=0.0,
        peak_eod_balance=50_000.0, intraday_peak_equity=50_000.0,
    )
    base.update(kw)
    return RiskContext(**base)


def intent(**kw) -> TradeIntent:
    base = dict(side=Side.BUY, qty=1, stop=19980.0, target=20060.0, signal_price=20000.0)
    base.update(kw)
    return TradeIntent(**base)


@pytest.fixture
def risk(tmp_path):
    return RiskEngine(make_settings(tmp_path))


def test_approves_valid_trade_and_reports_risk(risk):
    d = risk.evaluate(intent(), ctx())
    assert d.approved and d.qty == 1
    # est fill 20000 + 2 ticks = 20000.5; (20.5 pts * $2) + $8 reserve = $49
    assert d.metrics["risk_per_contract"] == 49.0


def test_risk_uses_live_market_not_signal_price(risk):
    # Market has moved 1.5 points above the signal: risk grows with the real fill.
    d = risk.evaluate(intent(), ctx(quote=Quote("MNQZ6", last=20001.5, ts=T0)))
    assert d.metrics["risk_per_contract"] == 52.0


def test_max_contracts_reject(risk):
    d = risk.evaluate(intent(qty=3), ctx())
    assert not d.approved and "MAX_CONTRACTS" in d.reasons[0]


def test_max_contracts_reduce_mode(tmp_path):
    r = RiskEngine(make_settings(tmp_path, risk_sizing_mode="reduce"))
    d = r.evaluate(intent(qty=5), ctx())
    assert d.approved and d.qty == 2 and "reduced" in d.reasons[0]


def test_per_trade_risk_reject_and_reduce(tmp_path):
    r = RiskEngine(make_settings(tmp_path, max_contracts=10))
    d = r.evaluate(intent(qty=5), ctx())  # 5 * 49 = 245 > 200
    assert not d.approved and "MAX_TRADE_RISK_USD" in d.reasons[0]
    r2 = RiskEngine(make_settings(tmp_path, max_contracts=10, risk_sizing_mode="reduce"))
    d2 = r2.evaluate(intent(qty=5), ctx())
    assert d2.approved and d2.qty == 4  # floor(200 / 49)


def test_single_contract_over_risk_rejected_even_in_reduce_mode(tmp_path):
    r = RiskEngine(make_settings(tmp_path, risk_sizing_mode="reduce"))
    d = r.evaluate(intent(stop=19880.0), ctx())  # 120.5 pts -> $249/contract
    assert not d.approved


def test_daily_loss_limit_pre_trade(risk):
    # -400 so far; this trade risks 49 -> worst case -449, still inside -450.
    acct = AccountSnapshot("A", 49_600.0, -400.0, 0.0, T0)
    assert risk.evaluate(intent(), ctx(account=acct, realized_today_from_fills=-400.0)).approved
    # -410 so far -> worst case -459 breaches the limit.
    acct = AccountSnapshot("A", 49_590.0, -410.0, 0.0, T0)
    d = risk.evaluate(intent(), ctx(account=acct, realized_today_from_fills=-410.0))
    assert not d.approved and "daily loss" in d.reasons[0]


def test_daily_loss_breach_requests_day_halt(risk):
    acct = AccountSnapshot("A", 49_540.0, -460.0, 0.0, T0)
    d = risk.evaluate(intent(), ctx(account=acct, realized_today_from_fills=-460.0))
    assert not d.approved and d.halt_scope == "day"


def test_fill_derived_loss_wins_when_worse_than_broker_report(risk):
    # Broker says flat for the day, but reconstructed fills show -440.
    d = risk.evaluate(intent(), ctx(realized_today_from_fills=-440.0))
    assert not d.approved and "daily loss" in d.reasons[0]


def test_trailing_eod_drawdown_floor(risk):
    assert risk.drawdown_floor(50_000, 50_000) == 48_000
    assert risk.drawdown_floor(51_000, 51_000) == 49_000
    assert risk.drawdown_floor(53_000, 53_000) == 50_000  # capped at starting balance
    acct = AccountSnapshot("A", 49_420.0, 0.0, 0.0, T0)
    d = risk.evaluate(intent(), ctx(account=acct, peak_eod_balance=51_000.0))
    # floor 49,000 + 400 buffer: equity 49,420 is 420 above floor -> breach? no (420 > 400) but trade pushes below
    assert not d.approved and "drawdown" in d.reasons[0]


def test_drawdown_breach_requests_account_halt(risk):
    acct = AccountSnapshot("A", 48_350.0, 0.0, 0.0, T0)
    d = risk.evaluate(intent(), ctx(account=acct))
    assert not d.approved and d.halt_scope == "account"


def test_intraday_trailing_mode_uses_peak_equity(tmp_path):
    r = RiskEngine(make_settings(tmp_path, drawdown_mode="intraday_trailing"))
    assert r.drawdown_floor(50_000, 50_900) == 48_900
    s = RiskEngine(make_settings(tmp_path, drawdown_mode="static"))
    assert s.drawdown_floor(52_000, 53_000) == 48_000


def test_max_trades_per_day(risk):
    d = risk.evaluate(intent(), ctx(trades_today=2))
    assert not d.approved and "max trades" in d.reasons[0]


@pytest.mark.parametrize("hh,mm,ok", [(8, 29, False), (8, 30, True), (14, 29, True), (14, 30, False), (16, 0, False)])
def test_ny_session_window_boundaries(risk, hh, mm, ok):
    now = datetime(2026, 10, 20, hh, mm, tzinfo=NY)
    d = risk.evaluate(intent(), ctx(now=now, account=AccountSnapshot("A", 50_000, 0, 0, now),
                                    quote=Quote("MNQZ6", last=20000.0, ts=now)))
    assert d.approved is ok
    if not ok:
        assert "entry window" in d.reasons[0]


def test_weekend_and_holiday_rejected(tmp_path):
    sat = datetime(2026, 10, 24, 10, 0, tzinfo=NY)
    r = RiskEngine(make_settings(tmp_path, holidays=(date(2026, 11, 26),)))
    assert not r.evaluate(intent(), ctx(now=sat, account=AccountSnapshot("A", 50_000, 0, 0, sat),
                                        quote=Quote("MNQZ6", 20000.0, sat))).approved
    thx = datetime(2026, 11, 26, 10, 0, tzinfo=NY)
    d = r.evaluate(intent(), ctx(now=thx, account=AccountSnapshot("A", 50_000, 0, 0, thx),
                                 quote=Quote("MNQZ6", 20000.0, thx)))
    assert not d.approved and "holiday" in d.reasons[0]


def test_must_be_flat_outside_holding_window(risk):
    assert not risk.must_be_flat(datetime(2026, 10, 20, 14, 29, tzinfo=NY))
    assert risk.must_be_flat(datetime(2026, 10, 20, 14, 30, tzinfo=NY))
    assert risk.must_be_flat(datetime(2026, 10, 20, 20, 0, tzinfo=NY))
    assert risk.must_be_flat(datetime(2026, 10, 24, 10, 0, tzinfo=NY))  # Saturday


def test_stale_price_rejected(risk):
    q = Quote("MNQZ6", last=20000.0, ts=T0 - timedelta(seconds=120))
    d = risk.evaluate(intent(), ctx(quote=q))
    assert not d.approved and "stale price" in d.reasons[0]


def test_missing_price_rejected(risk):
    assert not risk.evaluate(intent(), ctx(quote=None)).approved


@pytest.mark.parametrize("kw,match", [
    ({"broker_connected": False}, "disconnected"),
    ({"account": None}, "cannot verify"),
    ({"positions": None}, "cannot verify"),
    ({"open_orders": None}, "cannot verify"),
    ({"account": AccountSnapshot("A", 50_000, 0, 0, T0, verified=False)}, "not verified"),
    ({"account": AccountSnapshot("A", 50_000, 0, 0, T0 - timedelta(seconds=30))}, "old"),
])
def test_unverifiable_account_state_rejected(risk, kw, match):
    d = risk.evaluate(intent(), ctx(**kw))
    assert not d.approved and match in d.reasons[0]


def test_existing_position_or_working_order_rejected(risk):
    d = risk.evaluate(intent(), ctx(positions=[Position("MNQZ6", 1, 20000.0)]))
    assert not d.approved and "position" in d.reasons[0]
    order = Order("1", "MNQZ6", Side.SELL, OrderType.STOP, 1, OrderRole.STOP_LOSS, OrderStatus.WORKING, T0, T0,
                  stop_price=19980.0)
    assert not risk.evaluate(intent(), ctx(open_orders=[order])).approved


def test_halt_blocks_trading(risk):
    d = risk.evaluate(intent(), ctx(halt={"active": True, "scope": "manual", "reason": "test"}))
    assert not d.approved and "halted" in d.reasons[0]


def test_price_deviation_from_signal_rejected(risk):
    d = risk.evaluate(intent(), ctx(quote=Quote("MNQZ6", last=20003.0, ts=T0)))  # 12 ticks
    assert not d.approved and "ticks from signal" in d.reasons[0]


def test_market_already_through_stop_rejected(risk):
    d = risk.evaluate(intent(stop=19999.0, signal_price=19999.5),
                      ctx(quote=Quote("MNQZ6", last=19998.75, ts=T0)))
    assert not d.approved and "bracket invalid" in d.reasons[0]


def test_off_tick_prices_rejected(risk):
    assert not risk.evaluate(intent(stop=19980.1), ctx()).approved


def test_short_trade(risk):
    d = risk.evaluate(intent(side=Side.SELL, stop=20020.0, target=19940.0), ctx())
    assert d.approved and d.metrics["est_fill"] == 19999.5


def test_tradingview_equity_is_not_an_input(risk):
    # The intent carries no equity; only broker state is consulted. A rich broker
    # account approves; a broke one rejects, whatever TradingView thinks.
    rich = ctx()
    broke = replace(rich, account=AccountSnapshot("A", 48_100.0, 0.0, 0.0, T0))
    assert risk.evaluate(intent(), rich).approved
    assert not risk.evaluate(intent(), broke).approved
