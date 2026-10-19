"""Execution engine: protection, flatten, kill switch, cutoff, disconnects, reconciliation."""

from datetime import datetime

import pytest

from conftest import CONTRACT, T0
from v25bot.brokers.base import Fill, OrderRole, OrderStatus, Side
from v25bot.clock import NY


def test_entry_places_protected_bracket(h):
    code, res = h.entry()
    assert code == 200 and res["accepted"] and res["status"] == "accepted"
    assert res["contract"] == CONTRACT
    assert res["protection"]["ok"]
    assert [(p.symbol, p.qty) for p in h.positions()] == [(CONTRACT, 1)]
    stops = [o for o in h.open_orders() if o.role is OrderRole.STOP_LOSS]
    assert stops and stops[0].stop_price == 19980.0 and stops[0].qty == 1
    assert h.engine.trades_today() == 1


def test_stop_rejected_after_fill_triggers_liquidation_and_halt(h):
    h.broker.inject_reject(OrderRole.STOP_LOSS, "InvalidPrice")
    code, res = h.entry()
    assert not res["accepted"] and res["status"] == "liquidated_unprotected"
    assert res["protection"]["liquidated"] is True
    assert h.positions() == []
    halt = h.engine.halt_state()
    assert halt["scope"] == "protection"
    assert "unprotected_position" in h.alert_kinds()
    # Further trading is blocked until an explicit resume.
    h.clock.advance(120)
    code, res = h.entry(price=20000.0)
    assert not res["accepted"] and "halted" in res["reasons"][0]


def test_resume_after_protection_halt(h):
    h.broker.inject_reject(OrderRole.STOP_LOSS)
    h.entry()
    ok, msg = h.run(h.engine.resume("tester"))
    assert ok, msg
    assert h.engine.halt_state() is None
    h.clock.advance(120)
    code, res = h.entry()
    assert res["accepted"]


def test_failed_liquidation_raises_critical_alert_and_account_halt(h):
    h.broker.inject_reject(OrderRole.STOP_LOSS)
    h.broker.inject_reject(OrderRole.FLATTEN, "NoLiquidity", count=5)
    code, res = h.entry()
    assert res["protection"]["liquidated"] is False
    assert "flatten_failed" in h.alert_kinds()
    assert h.engine.halt_state()["scope"] in ("account", "protection")
    assert h.positions()[0].qty == 1  # still open: manual intervention flagged


def test_partial_entry_fills_are_fully_protected(harness):
    h = harness(paper_book_depth=1, max_contracts=2)
    code, res = h.entry(qty=2, stop=19990.0)
    assert res["accepted"] and len(res["fills"]) == 2
    assert [f["price"] for f in res["fills"]] == [20000.5, 20000.75]
    stop = next(o for o in h.open_orders() if o.role is OrderRole.STOP_LOSS)
    assert stop.qty == 2


def test_broker_rejected_entry_is_reported_not_counted(h):
    h.broker.inject_reject(OrderRole.ENTRY, "InsufficientMargin")
    code, res = h.entry()
    assert code == 409 and "InsufficientMargin" in res["reasons"][0]
    assert h.positions() == [] and h.engine.trades_today() == 0


def test_disconnect_rejects_entries_then_reconnect_reconciles(h):
    h.broker.simulate_disconnect()
    code, res = h.entry()
    assert not res["accepted"] and "disconnected" in res["reasons"][0]
    summary = h.supervise()
    assert summary["connected"] is False
    assert "broker_disconnected" in h.alert_kinds()
    h.broker.simulate_reconnect()
    summary = h.supervise()
    assert summary["connected"] and summary["reconcile"]["ok"]
    assert "broker_reconnected" in h.alert_kinds()
    h.clock.advance(60)
    assert h.entry()[1]["accepted"]


def test_stop_executes_during_disconnect_and_is_reconciled(h):
    h.entry()
    h.broker.simulate_disconnect()
    h.quote(19979.0)  # broker-side stop still protects us
    h.broker.simulate_reconnect()
    summary = h.supervise()
    assert summary["reconcile"]["ok"]
    assert h.positions() == []
    assert any(f["role"] == "stop_loss" for f in h.store.fills())


def test_session_cutoff_flattens_open_position(h):
    h.entry()
    h.clock.set(datetime(2026, 10, 20, 14, 29, 59, tzinfo=NY))
    h.quote(20005.0)
    assert h.supervise().get("session_flatten") is None
    h.clock.set(datetime(2026, 10, 20, 14, 30, 0, tzinfo=NY))
    h.quote(20006.0)
    assert h.supervise()["session_flatten"] is True
    assert h.positions() == [] and h.open_orders() == []


def test_entry_after_cutoff_rejected(h):
    h.clock.set(datetime(2026, 10, 20, 14, 31, tzinfo=NY))
    code, res = h.entry()
    assert not res["accepted"] and "entry window" in res["reasons"][0]


def test_daily_loss_breach_halts_for_the_day_and_clears_next_day(harness):
    h = harness(max_trades_per_day=5, daily_loss_limit_usd=100)
    assert h.entry()[1]["accepted"]  # planned worst case -49 fits inside -100
    h.quote(19900.0)  # market gaps 80 points through the stop: about -205 realised
    summary = h.supervise()
    assert summary["breach"][0] == "day"
    assert h.engine.halt_state()["scope"] == "day"
    h.clock.advance(120)
    assert "halted" in h.entry()[1]["reasons"][0]
    h.clock.set(datetime(2026, 10, 21, 10, 0, tzinfo=NY))
    assert h.engine.halt_state() is None
    assert h.entry()[1]["accepted"]


def test_drawdown_breach_requires_manual_resume(harness):
    h = harness(max_drawdown_usd=100, drawdown_safety_buffer_usd=10, daily_loss_limit_usd=1000)
    assert h.entry()[1]["accepted"]  # 50,000 - 49 stays above floor 49,900 + 10
    h.quote(19900.0)  # gap through the stop
    summary = h.supervise()
    assert summary["breach"][0] == "account"
    h.clock.set(datetime(2026, 10, 21, 10, 0, tzinfo=NY))
    assert h.engine.halt_state()["scope"] == "account"  # does not clear on a new day


def test_kill_switch_flattens_and_persists_across_restart(h):
    h.entry()
    res = h.run(h.engine.engage_kill_switch("test"))
    assert res["flattened"] and h.positions() == []
    h.restart()
    assert h.engine.halt_state()["scope"] == "kill_switch"
    h.clock.advance(60)
    assert "halted" in h.entry()[1]["reasons"][0]


def test_kill_switch_file(h):
    h.entry()
    open(h.settings.kill_switch_file, "w").close()
    summary = h.supervise()
    assert summary["kill_switch"] and h.positions() == []
    ok, msg = h.run(h.engine.resume("tester"))
    assert not ok and "kill switch file" in msg
    import os

    os.remove(h.settings.kill_switch_file)
    assert h.run(h.engine.resume("tester"))[0]


def test_emergency_flatten(h):
    h.entry()
    assert h.run(h.engine.flatten_all("manual"))
    assert h.positions() == [] and h.open_orders() == []


def test_tradingview_flatten_event(h):
    h.entry()
    code, res = h.flatten_event("session_flat")
    assert res["status"] == "flattened" and h.positions() == []


def test_tradingview_exit_event_syncs_broker(h):
    h.entry()
    code, res = h.exit_event()
    assert res["status"] == "flattened" and h.positions() == []


def test_tradingview_exit_event_log_mode(harness):
    h = harness(tv_exit_action="log")
    h.entry()
    code, res = h.exit_event()
    assert res["status"] == "noted" and h.positions()[0].qty == 1
    assert "tv_exit_divergence" in h.alert_kinds()


def test_max_trades_per_day_counts_engine_entries(h):
    for i in range(2):
        assert h.entry()[1]["accepted"]
        h.run(h.engine.flatten_all("next"))
        h.clock.advance(120)
    code, res = h.entry()
    assert "max trades" in res["reasons"][0]


def test_signal_bar_is_not_replayed_against_new_position(h):
    bar_time = int(T0.timestamp() * 1000) - 60_000
    h.entry(bar_time=bar_time)
    # The signal bar itself had a low below the stop; arriving late it must be ignored.
    code, res = h.bar(20000.0, 20010.0, 19970.0, 20000.0, bar_time=bar_time)
    assert res["status"] == "ignored"
    assert h.positions()[0].qty == 1


def test_next_bar_drives_paper_fills(h):
    h.entry()
    code, res = h.bar(20000.0, 20061.0, 19995.0, 20050.0)
    assert res["status"] == "processed" and h.positions() == []
    assert h.account().realized_pnl_today > 0


def test_stale_price_alert_with_open_position(h):
    h.entry()
    h.clock.advance(h.settings.max_quote_age_seconds + 5)
    summary = h.supervise()
    assert summary["stale_price"] and "stale_price" in h.alert_kinds()


def test_stale_price_blocks_new_entries(h):
    h.quote(20000.0)
    h.clock.advance(200)
    # Signal timestamped 200s after the last quote seeds a fresh price in paper mode,
    # so make the signal itself old relative to the clock to prove staleness is checked.
    from conftest import entry_payload
    from v25bot.schemas import EntryEvent

    d = entry_payload(h.clock)
    d["sent_at"] -= 150_000
    code, res = h.run(h.engine.handle_entry(EntryEvent.model_validate(d)))
    assert "stale price" in res["reasons"][0]


def test_reconcile_detects_position_mismatch(h):
    h.entry()
    # Corrupt the engine's fill record to simulate a fill the engine never saw.
    h.store.add_fill(Fill("ghost", "x", CONTRACT, Side.BUY, 1, 20000.0, 0.0, h.clock.now(), OrderRole.ENTRY).to_dict(),
                     h.engine.day())
    res = h.run(h.engine.reconcile())
    assert not res["ok"] and "mismatch" in res["issues"][0]
    assert h.engine.halt_state()["scope"] == "reconcile"


def test_reconcile_flags_unknown_broker_orders(h):
    h.entry()
    h.store._conn.execute("DELETE FROM orders")
    res = h.run(h.engine.reconcile())
    assert not res["ok"] and "unknown working orders" in " ".join(res["issues"])


def test_protection_monitor_catches_cancelled_stop(h):
    h.entry()
    stop = next(o for o in h.open_orders() if o.role is OrderRole.STOP_LOSS)
    h.run(h.broker.cancel_order(stop.id))  # someone cancels the stop at the broker
    h.clock.advance(10)
    summary = h.supervise()
    assert summary["protection"]["ok"] is False
    assert h.positions() == [] and h.engine.halt_state()["scope"] == "protection"


def test_risk_comes_from_broker_not_tradingview(h):
    # The alert claims tiny planned risk, but the stop is 120 points away at the broker's price.
    code, res = h.entry(stop=19880.0, planned_risk=1.0, risk_per_contract=1.0)
    assert not res["accepted"] and "MAX_TRADE_RISK_USD" in res["reasons"][0]


def test_strategy_version_pinning(h):
    code, res = h.entry(strategy_version="V99-hacked")
    assert code == 422 and "strategy_version" in res["reasons"][0]


def test_wrong_instrument_point_value_rejected(h):
    code, res = h.entry(point_value=20.0)  # an NQ chart, not MNQ
    assert code == 422 and "wrong chart" in res["reasons"][0]


def test_non_one_minute_chart_rejected(h):
    code, res = h.entry(timeframe="5")
    assert code == 422


def test_roll_window_entry_uses_chart_contract(harness):
    h = harness(start=datetime(2026, 12, 14, 10, 0, tzinfo=NY))
    code, res = h.entry(contract="MNQH2027")
    assert res["accepted"] and res["contract"] == "MNQH7"


def test_roll_window_entry_without_chart_contract_rejected(harness):
    h = harness(start=datetime(2026, 12, 14, 10, 0, tzinfo=NY))
    code, res = h.entry(contract=None)
    assert code == 409 and "roll window" in res["reasons"][0]


def test_restart_preserves_position_and_protection(h):
    h.entry()
    h.restart()
    summary = h.supervise(reconcile=True)
    assert summary["reconcile"]["ok"] and summary["protection"]["ok"]
    assert h.positions()[0].qty == 1


@pytest.mark.parametrize("side,stop,target", [("sell", 20020.0, 19940.0)])
def test_short_entry(h, side, stop, target):
    code, res = h.entry(side=side, stop=stop, target=target)
    assert res["accepted"] and h.positions()[0].qty == -1
    stop_order = next(o for o in h.open_orders() if o.role is OrderRole.STOP_LOSS)
    assert stop_order.side is Side.BUY and stop_order.status is OrderStatus.WORKING


def test_background_monitor_runs_and_enforces_cutoff(h):
    import asyncio

    from v25bot.monitor import Monitor

    h.entry()
    h.clock.set(datetime(2026, 10, 20, 14, 30, 5, tzinfo=NY))
    h.quote(20004.0)

    async def go():
        m = Monitor(h.engine)
        m.start()
        await asyncio.sleep(0.1)
        await m.stop()
        return m

    m = h.run(go())
    assert m.last_cycle is not None and m.last_error is None
    assert h.positions() == []


def test_real_broker_never_uses_tradingview_or_signal_prices(h):
    """For a non-paper broker, chart bars and signal prices are not market data."""
    from v25bot.brokers.base import BrokerError

    h.broker.is_paper = False  # stand-in for a real adapter
    code, res = h.bar(20000.0, 20010.0, 19990.0, 20005.0)
    assert res["status"] == "noted" and h.quotes.get(CONTRACT) is None
    code, res = h.entry()
    assert not res["accepted"] and "no market price" in res["reasons"][0]
    with pytest.raises(BrokerError):
        h.quote(20000.0)
