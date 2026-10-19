"""Dashboard analytics: trade reconstruction and statistics from recorded fills."""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import CONTRACT, T0
from v25bot.clock import NY
from v25bot.dashboard import analytics as an

PV = 2.0  # MNQ point value


def fill(fid, order_id, side, qty, price, minute, role, commission=None):
    ts = (T0 + timedelta(minutes=minute)).astimezone(timezone.utc).isoformat()
    return {"id": fid, "order_id": order_id, "symbol": CONTRACT, "side": side, "qty": qty, "price": price,
            "commission": 1.5 * qty if commission is None else commission, "ts": ts, "role": role, "realized_pnl": 0}


def orders_for(entry_id, stop, target, event_id="evt-1"):
    return [
        {"id": entry_id, "role": "entry", "event_id": event_id},
        {"id": f"{entry_id}s", "role": "stop_loss", "parent_id": entry_id, "stop_price": stop},
        {"id": f"{entry_id}t", "role": "take_profit", "parent_id": entry_id, "limit_price": target},
    ]


def test_long_winner_with_stop_and_target():
    fills = [fill("F1", "E1", "buy", 1, 20000.5, 0, "entry"), fill("F2", "E1t", "sell", 1, 20060.0, 30, "take_profit")]
    [t] = an.build_trades(fills, orders_for("E1", 19980.0, 20060.0))
    assert (t.side, t.qty, t.entry_price, t.exit_price) == ("long", 1, 20000.5, 20060.0)
    assert (t.stop_price, t.target_price, t.event_id) == (19980.0, 20060.0, "evt-1")
    assert t.gross_pnl == pytest.approx(59.5 * PV)
    assert t.commission == 3.0 and t.net_pnl == pytest.approx(119.0 - 3.0)
    assert t.exit_reason == "Take profit" and t.duration_seconds == 1800 and t.status == "closed"


def test_short_loser_stop():
    fills = [fill("F1", "E1", "sell", 2, 20000.0, 0, "entry"), fill("F2", "E1s", "buy", 2, 20020.5, 5, "stop_loss")]
    [t] = an.build_trades(fills, orders_for("E1", 20020.0, 19940.0))
    assert t.side == "short" and t.net_pnl == pytest.approx(-20.5 * 2 * PV - 6.0)
    assert t.exit_reason == "Stop loss"


def test_partial_entry_and_partial_exits_make_one_trade():
    fills = [
        fill("F1", "E1", "buy", 2, 20000.0, 0, "entry"), fill("F2", "E1", "buy", 1, 20000.25, 0, "entry"),
        fill("F3", "E1t", "sell", 2, 20060.0, 10, "take_profit"), fill("F4", "E1s", "sell", 1, 19979.5, 20, "stop_loss"),
    ]
    [t] = an.build_trades(fills, orders_for("E1", 19980.0, 20060.0))
    avg = (20000.0 * 2 + 20000.25) / 3
    assert t.qty == 3 and t.entry_price == pytest.approx(avg)
    expected = ((20060.0 - avg) * 2 + (19979.5 - avg) * 1) * PV - 1.5 * 6
    assert t.net_pnl == pytest.approx(expected, abs=0.01)
    assert t.exit_reason == "Stop loss"  # the fill that closed the position


def test_open_trade_is_reported_open():
    trades = an.build_trades([fill("F1", "E1", "buy", 1, 20000.5, 0, "entry")], orders_for("E1", 19980.0, 20060.0))
    assert len(trades) == 1 and trades[0].status == "open" and trades[0].net_pnl is None
    assert trades[0].stop_price == 19980.0


def test_flatten_reason_matched_from_audit():
    fills = [fill("F1", "E1", "buy", 1, 20000.5, 0, "entry"), fill("F2", "X1", "sell", 1, 20004.0, 270, "flatten")]
    exit_ts = (T0 + timedelta(minutes=270)).astimezone(timezone.utc)
    audit = [{"kind": "flatten", "ts": (exit_ts + timedelta(seconds=1)).isoformat(),
              "message": "flatten (NY session cutoff) complete for ['MNQZ6']"}]
    [t] = an.build_trades(fills, [], audit)
    assert t.exit_reason == "Flatten: NY session cutoff"
    [t2] = an.build_trades(fills, [], [])
    assert t2.exit_reason == "Flatten"


def test_fill_that_flips_position_closes_and_opens():
    fills = [fill("F1", "E1", "buy", 1, 20000.0, 0, "entry"), fill("F2", "E2", "sell", 3, 20010.0, 5, "entry")]
    trades = an.build_trades(fills, [])
    assert [t.status for t in trades] == ["closed", "open"]
    assert trades[0].gross_pnl == pytest.approx(10 * PV)
    assert trades[1].side == "short" and trades[1].qty == 2


def test_summary_statistics():
    trades = [an.Trade(id=str(i), symbol=CONTRACT, side="long", qty=1, entry_time="t", entry_price=1, entry_order_id="e",
                       net_pnl=v, status="closed", exit_time=f"2026-10-20T15:{i:02d}:00+00:00") for i, v in enumerate([100, -50, 30, -25, 0])]
    s = an.summarize(trades)
    assert s["total_trades"] == 5 and s["wins"] == 2 and s["losses"] == 2 and s["breakeven"] == 1
    assert s["win_rate"] == 40.0 and s["profit_factor"] == round(130 / 75, 2)
    assert (s["avg_win"], s["avg_loss"], s["largest_win"], s["largest_loss"]) == (65.0, -37.5, 100, -50)
    assert s["net_pnl"] == 55


def test_summary_without_data_returns_none_not_zero():
    s = an.summarize([])
    assert s["win_rate"] is None and s["profit_factor"] is None and s["avg_win"] is None and s["total_trades"] == 0
    only_wins = [an.Trade(id="1", symbol=CONTRACT, side="long", qty=1, entry_time="t", entry_price=1, entry_order_id="e",
                          net_pnl=10, status="closed", exit_time="2026-10-20T15:00:00+00:00")]
    s2 = an.summarize(only_wins)
    assert s2["profit_factor"] is None and s2["profit_factor_infinite"] is True


def test_drawdown_stats():
    points = [{"equity": v} for v in (50100, 50300, 50050, 50200, 49900)]
    dd = an.drawdown_stats(points, 50000, 50000)
    assert dd["max_drawdown"] == 400 and dd["peak_equity"] == 50300
    assert dd["current_drawdown"] == 300 and dd["max_drawdown_pct"] == pytest.approx(400 / 50300 * 100, abs=0.01)


def test_distribution_never_straddles_zero():
    bins = an.distribution([-90, -40, -5, 12, 55, 160])
    assert sum(b["count"] for b in bins) == 6
    assert all(not (b["from"] < 0 < b["to"]) for b in bins)
    assert all((b["sign"] == "loss") == (b["to"] <= 0) for b in bins)


def test_range_filters_use_eastern_time():
    now = datetime(2026, 10, 20, 10, 0, tzinfo=NY)
    mk = lambda i, dt: an.Trade(id=str(i), symbol=CONTRACT, side="long", qty=1, entry_time=dt.isoformat(),  # noqa: E731
                                entry_price=1, entry_order_id="e", net_pnl=1, status="closed", exit_time=dt.isoformat())
    trades = [mk(1, datetime(2026, 10, 20, 9, 45, tzinfo=NY)), mk(2, datetime(2026, 10, 19, 23, 30, tzinfo=NY)),
              mk(3, datetime(2026, 10, 10, 11, 0, tzinfo=NY)), mk(4, datetime(2026, 8, 1, 11, 0, tzinfo=NY))]
    ids = lambda r: [t.id for t in an.filter_trades(trades, range_key=r, now=now)]  # noqa: E731
    assert ids("today") == ["1"]
    assert ids("7d") == ["1", "2"]
    assert ids("30d") == ["1", "2", "3"]
    assert ids("all") == ["1", "2", "3", "4"]


def test_analytics_range_baseline_includes_earlier_pnl():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=NY)
    mk = lambda i, dt, v: an.Trade(id=str(i), symbol=CONTRACT, side="long", qty=1, entry_time=dt.isoformat(),  # noqa: E731
                                   entry_price=1, entry_order_id="e", net_pnl=v, status="closed", exit_time=dt.isoformat())
    trades = [mk(1, datetime(2026, 10, 1, 10, tzinfo=NY), 200), mk(2, datetime(2026, 10, 20, 10, tzinfo=NY), -50)]
    a = an.analytics(trades, "today", now, 50000, 50150)
    assert a["equity"][0]["v"] == 50200 and a["equity"][-1]["v"] == 50150
    assert a["cumulative"][-1]["v"] == -50 and a["drawdown"][-1]["v"] == -50
    assert a["summary"]["total_trades"] == 1


def test_activity_classification():
    events = [
        {"event_id": "a", "kind": "entry", "status": "accepted", "received_at": "2026-10-20T14:00:00+00:00",
         "result": {"qty": 1, "contract": CONTRACT, "risk": {}}},
        {"event_id": "b", "kind": "entry", "status": "rejected", "received_at": "2026-10-20T14:01:00+00:00",
         "result": {"reasons": ["max trades per day reached (2/2)"], "risk": {"contract": CONTRACT}}},
        {"event_id": "c", "kind": "entry", "status": "rejected", "received_at": "2026-10-20T14:02:00+00:00",
         "result": {"reasons": ["V25 entries are only valid from a 1-minute chart"]}},
        {"event_id": "d", "kind": "entry", "status": "rejected", "received_at": "2026-10-20T14:03:00+00:00",
         "result": {"reasons": ["broker rejected order: InvalidPrice"], "risk": {"qty": 1}}},
    ]
    fills = [fill("F1", "E1", "buy", 1, 20000.5, 1, "entry"), fill("F2", "S1", "sell", 1, 19979.5, 2, "stop_loss")]
    audit = [{"ts": "2026-10-20T14:05:00+00:00", "level": "CRITICAL", "kind": "trading_halted", "message": "halted"},
             {"ts": "2026-10-20T14:06:00+00:00", "level": "ERROR", "kind": "broker_disconnected", "message": "lost"},
             {"ts": "2026-10-20T14:07:00+00:00", "level": "WARNING", "kind": "entry_rejected", "message": "dup"}]
    items = an.activity(events, fills, audit)
    cats = {i["ref"] or i["title"]: i["category"] for i in items}
    assert cats["a"] == "signal" and cats["b"] == "risk" and cats["c"] == "rejected" and cats["d"] == "rejected"
    assert cats["F1"] == "entry" and cats["F2"] == "stop"
    assert cats["Trading halted"] == "halt" and cats["Broker disconnected"] == "error"
    assert not any(i["title"] == "Entry rejected" for i in items)  # already shown via the webhook event
    assert [i["ts"] for i in items] == sorted((i["ts"] for i in items), reverse=True)


@pytest.mark.parametrize("dt,is_open", [
    (datetime(2026, 10, 20, 10, 0, tzinfo=NY), True),    # Tue morning
    (datetime(2026, 10, 20, 17, 30, tzinfo=NY), False),  # daily maintenance break
    (datetime(2026, 10, 23, 17, 0, tzinfo=NY), False),   # Friday close
    (datetime(2026, 10, 24, 12, 0, tzinfo=NY), False),   # Saturday
    (datetime(2026, 10, 25, 17, 59, tzinfo=NY), False),  # Sunday before open
    (datetime(2026, 10, 25, 18, 0, tzinfo=NY), True),    # Sunday open
])
def test_cme_hours(dt, is_open):
    assert an.cme_equity_futures_open(dt) is is_open
