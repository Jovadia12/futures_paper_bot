"""V26 pipeline: schema, engine-limit agreement, stop/target rule checks, risk, paper fills,
duplicates and diagnostics. V25 behaviour is covered, unchanged, by the other test files."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import ADMIN, CONTRACT, T0, WEBHOOK_TOKEN, entry_payload, make_settings, ms
from v25bot.api import create_app
from v25bot.brokers.base import OrderRole, OrderStatus
from v25bot.clock import FakeClock
from v25bot.config import Settings
from v25bot.schemas import BarEvent, EntryEvent, V1Adapter
from v26_payloads import diag_item, v26_bar, v26_entry

STAGING = dict(allowed_strategy_versions=("V26-webhook.1",), allowed_bot_ids=("V26-MNQ-1",))
URL = f"/webhook/tradingview/{WEBHOOK_TOKEN}"


@pytest.fixture
def s26(harness):
    return harness(**STAGING)


def enter(h, **kw):
    return h.run(h.engine.handle_entry(EntryEvent.model_validate(v26_entry(h.clock, **kw))))


def bar(h, *ohlc, **kw):
    return h.run(h.engine.handle_bar(BarEvent.model_validate(v26_bar(h.clock, *ohlc, **kw))))


def reasons(result):
    return " | ".join(result["reasons"])


# ============================================================================ schema
def test_v26_entry_and_bar_validate():
    clock = FakeClock(T0)
    assert V1Adapter.validate_python(v26_entry(clock)).is_v26
    assert V1Adapter.validate_python(v26_bar(clock, 1, 2, 1, 2)).diag.zones_bull == 3


@pytest.mark.parametrize("tamper,match", [
    (lambda d: d.update(strategy="V25"), "not valid for strategy"),
    (lambda d: d.pop("setup"), "must include caps and setup"),
    (lambda d: d.pop("caps"), "must include caps and setup"),
    (lambda d: d.update(sizing_mode="Prop Conservative"), "V26 sizing_mode"),
    (lambda d: d["setup"].update(htf_tf=3), "htf_tf"),
    (lambda d: d["setup"].update(min_rr=1.5), "min_rr"),
    (lambda d: d["setup"].update(stop_anchor="structure"), "stop_anchor"),   # earlier draft's alerts
    (lambda d: d["setup"].update(stop_anchor="boundary"), "stop_anchor"),
    (lambda d: d["setup"].update(stop_buffer_ticks=0), "Extra inputs"),
    (lambda d: d["setup"].update(extra=1), "Extra inputs"),
    (lambda d: d["setup"]["fvg"]["c2"].update(h=1.0, l=2.0), "high below low"),
])
def test_v26_entry_schema_rejections(tamper, match):
    with pytest.raises(ValidationError, match=match):
        V1Adapter.validate_python(v26_entry(FakeClock(T0), tamper=tamper))


def test_v25_payload_cannot_carry_v26_fields():
    clock = FakeClock(T0)
    bad = entry_payload(clock) | {"caps": v26_entry(clock)["caps"]}
    with pytest.raises(ValidationError, match="only valid in v26"):
        V1Adapter.validate_python(bad)
    bad = entry_payload(clock) | {"schema": "v26.webhook.v1"}
    with pytest.raises(ValidationError, match="not valid for strategy"):
        V1Adapter.validate_python(bad)


def test_bar_diag_limits():
    clock = FakeClock(T0)
    with pytest.raises(ValidationError, match="at most 20"):
        V1Adapter.validate_python(v26_bar(clock, 1, 2, 1, 2, items=[diag_item("tap")] * 21))
    with pytest.raises(ValidationError):
        V1Adapter.validate_python(v26_bar(clock, 1, 2, 1, 2, items=[diag_item("made_up")]))


def test_min_reward_risk_cannot_be_lowered():
    with pytest.raises(ValidationError, match="2.0"):
        Settings(min_reward_risk=1.5)


# ============================================================================ deployment gating
def test_production_defaults_reject_v26(h):
    """The running V25 bot keeps rejecting V26 until ALLOWED_STRATEGY_VERSIONS is changed."""
    code, r = enter(h)
    assert code == 422 and "not in ALLOWED_STRATEGY_VERSIONS" in reasons(r)
    assert h.positions() == []


def test_v25_bot_id_allowlist_rejects_v26(harness):
    h = harness(allowed_strategy_versions=("V25-webhook.1", "V26-webhook.1"), allowed_bot_ids=("V25-MNQ-1",))
    code, r = enter(h)
    assert code == 422 and "not allowlisted" in reasons(r)


# ============================================================================ accepted entries
@pytest.mark.parametrize("side,stop,target", [
    ("buy", 19994.0, 20040.0),   # candle-3 low; 20020 is only 1.08R, 20040 is 2.68R
    ("sell", 20006.0, 19960.0),  # candle-3 high; mirror image
])
def test_v26_entry_accepted_with_candle3_stop_and_liquidity_target(s26, side, stop, target):
    code, r = enter(s26, side=side)
    assert code == 200 and r["accepted"], r
    assert r["qty"] == 2
    orders = {o.role: o for o in s26.open_orders()}
    assert orders[OrderRole.STOP_LOSS].stop_price == stop
    assert orders[OrderRole.TAKE_PROFIT].limit_price == target
    assert r["risk"]["trade_risk"] <= 200


def test_example_distinguishes_candle3_from_other_stop_definitions():
    """The fixture is built so the three possible readings give three different prices."""
    fvg = v26_entry(FakeClock(T0))["setup"]["fvg"]
    lowest = min(fvg[c]["l"] for c in ("c1", "c2", "c3"))
    assert (fvg["c3"]["l"], lowest, fvg["bottom"]) == (19994.0, 19990.0, 20000.0)
    fvg = v26_entry(FakeClock(T0), side="sell")["setup"]["fvg"]
    highest = max(fvg[c]["h"] for c in ("c1", "c2", "c3"))
    assert (fvg["c3"]["h"], highest, fvg["top"]) == (20006.0, 20010.0, 20000.0)


# ============================================================================ caps agreement
@pytest.mark.parametrize("caps,env", [
    ({"max_contracts": 3, "max_trade_risk": 200, "cost_reserve": 8, "slippage_ticks": 2}, "MAX_CONTRACTS"),
    ({"max_contracts": 2, "max_trade_risk": 300, "cost_reserve": 8, "slippage_ticks": 2}, "MAX_TRADE_RISK_USD"),
    ({"max_contracts": 2, "max_trade_risk": 200, "cost_reserve": 4, "slippage_ticks": 2},
     "COST_RESERVE_PER_CONTRACT_USD"),
    ({"max_contracts": 2, "max_trade_risk": 200, "cost_reserve": 8, "slippage_ticks": 1}, "RISK_ENTRY_SLIPPAGE_TICKS"),
])
def test_caps_must_equal_engine_limits(s26, caps, env):
    code, r = enter(s26, caps=caps)
    assert code == 422 and env in reasons(r) and "does not match engine" in reasons(r)
    assert s26.positions() == []


def test_one_contract_over_risk_cap_rejected(harness):
    """Engine and Pine both at a $30 cap: one contract risks $33, so no trade (never 'at least 1')."""
    h = harness(**STAGING, max_trade_risk_usd=30.0)
    caps = {"max_contracts": 2, "max_trade_risk": 30, "cost_reserve": 8, "slippage_ticks": 2}
    code, r = enter(h, caps=caps)
    assert code == 409 and "exceeds MAX_TRADE_RISK_USD 30.00 (33.00/contract)" in reasons(r)
    assert h.positions() == []


def test_qty_above_max_contracts_rejected(s26):
    code, r = enter(s26, tamper=lambda d: d.update(qty=3, planned_risk=99.0))
    assert code == 409 and "exceeds MAX_CONTRACTS 2" in reasons(r)


# ============================================================================ stop rules
@pytest.mark.parametrize("side,wrong,what", [
    ("buy", 19990.0, "lowest low of all three candles (candle 2)"),
    ("buy", 20000.0, "FVG boundary (candle-3 high)"),
    ("buy", 19993.75, "candle-3 low widened by a tick"),
    ("buy", 19994.25, "candle-3 low tightened by a tick"),
    ("sell", 20010.0, "highest high of all three candles (candle 2)"),
    ("sell", 20000.0, "FVG boundary (candle-3 low)"),
    ("sell", 20006.25, "candle-3 high widened by a tick"),
])
def test_any_other_stop_is_rejected(s26, side, wrong, what):
    code, r = enter(s26, side=side, tamper=lambda d: d.update(stop=wrong))
    assert code == 422, what
    want = "is not candle 3's low 19994.0 of the inverted bearish FVG" if side == "buy" \
        else "is not candle 3's high 20006.0 of the inverted bullish FVG"
    assert f"V26 rule check: stop {wrong} {want}" in reasons(r), what


def test_candle3_is_the_formation_candle_not_the_inverting_candle(s26):
    """The inverting bar is the entry bar; a 'candle 3' at or after it is refused."""
    def c3_is_entry_bar(d):
        d["setup"]["fvg"]["c3"]["t"] = d["bar_time"]
    code, r = enter(s26, tamper=c3_is_entry_bar)
    assert code == 422 and "closed before the entry bar" in reasons(r)


def test_candle3_must_be_the_gap_forming_candle(s26):
    """Candle 3 is identified by the gap itself: its high is the bearish gap's bottom."""
    def relabel(d):
        f = d["setup"]["fvg"]
        f["c2"], f["c3"] = dict(f["c3"], t=f["c2"]["t"]), dict(f["c2"], t=f["c3"]["t"])
        d["stop"] = f["c3"]["l"]  # stop on the relabelled 'candle 3' (really candle 2)
    code, r = enter(s26, tamper=relabel)
    assert code == 422 and "do not match its candles" in reasons(r)


# ============================================================================ FVG candle indexing
def _swap_c1_c3_times(d):
    f = d["setup"]["fvg"]
    f["c1"]["t"], f["c3"]["t"] = f["c3"]["t"], f["c1"]["t"]


@pytest.mark.parametrize("tamper,match", [
    (_swap_c1_c3_times, "candles must be in order"),
    (lambda d: d["setup"]["fvg"]["c3"].update(t=d["bar_time"]), "closed before the entry bar"),
    (lambda d: d["setup"]["fvg"].update(top=20004.0), "do not match its candles"),
    (lambda d: d["setup"]["fvg"].update(bottom=19994.0), "do not match its candles"),
    (lambda d: d["setup"].update(min_gap_ticks=40), "smaller than 40 ticks"),
    (lambda d: d["setup"].update(tap_time=d["bar_time"] + 60_000), "tap cannot be after"),
])
def test_fvg_geometry_and_indexing(s26, tamper, match):
    code, r = enter(s26, tamper=tamper)
    assert code == 422 and match in reasons(r)


def test_entry_close_must_invert_the_gap(s26):
    def no_inversion(d):
        d.update(price=20005.0, est_fill=20005.5)
    code, r = enter(s26, tamper=no_inversion)
    assert code == 422 and "did not invert the bearish FVG" in reasons(r)


# ============================================================================ targets
def test_insufficient_reward_risk_rejected(s26):
    code, r = enter(s26, target_index=0)  # 20020 = 1.08R with the three-candle stop
    assert code == 422 and "below the 2R minimum" in reasons(r)


def test_engine_minimum_reward_risk_applies(harness):
    h = harness(**STAGING, min_reward_risk=3.0)
    code, r = enter(h)  # 20040 = 2.68R
    assert code == 422 and "below the 3R minimum" in reasons(r)


def test_target_must_be_the_nearest_eligible_level(s26):
    code, r = enter(s26, target_index=2)  # 20060 = 4.28R, but 20040 (2.68R) is nearer and eligible
    assert code == 422 and "not the nearest eligible swing level (20040.0)" in reasons(r)


def test_nearest_eligible_among_many_candidates(s26):
    levels = [20010.0, 20020.0, 20030.0, 20035.0, 20050.0]  # 0.28R 1.08R 1.88R 2.28R 3.48R
    code, r = enter(s26, levels=levels)
    assert code == 200 and r["accepted"], r
    assert {o.role: o for o in s26.open_orders()}[OrderRole.TAKE_PROFIT].limit_price == 20035.0


def test_target_beyond_five_listed_levels(s26):
    """Only the 5 nearest levels are listed; a target beyond all of them is valid if none qualified."""
    def far(d):
        d["setup"]["candidates"] = [{"p": p, "t": d["bar_time"] - 3_600_000}
                                    for p in (20008.0, 20010.0, 20012.0, 20014.0, 20016.0)]
    code, r = enter(s26, tamper=far)
    assert code == 200 and r["accepted"], r


def test_unlisted_target_rejected(s26):
    def unlisted(d):
        d["setup"]["candidates"] = d["setup"]["candidates"][:1]
    code, r = enter(s26, tamper=unlisted)
    assert code == 422 and "not among the listed swing levels" in reasons(r)


@pytest.mark.parametrize("tamper,match", [
    (lambda d: d["setup"].update(target_time=d["bar_time"]), "confirmed before the entry bar"),
    (lambda d: d["setup"].update(target_rr=3.5), "target_rr 3.5 does not match"),
    (lambda d: d["setup"]["candidates"].reverse(), "nearest first"),
    (lambda d: d.update(risk_per_contract=20.0), "risk_per_contract 20.0 does not match 33.00"),
    (lambda d: d.update(est_fill=20006.25), "est_fill 20006.25"),
])
def test_target_and_sizing_consistency(s26, tamper, match):
    code, r = enter(s26, tamper=tamper)
    assert code == 422 and match in reasons(r)


# ============================================================================ paper fills
def test_intrabar_stop_and_target_long_assumes_stop_first(s26):
    code, r = enter(s26)
    assert r["accepted"], r
    s26.clock.advance(60)
    bar(s26, 20006.0, 20045.0, 19990.0, 20044.0)  # touched both 19994 and 20040
    stops = [o for o in s26.run(s26.broker.get_orders(open_only=False)) if o.role is OrderRole.STOP_LOSS]
    targets = [o for o in s26.run(s26.broker.get_orders(open_only=False)) if o.role is OrderRole.TAKE_PROFIT]
    assert stops[0].status is OrderStatus.FILLED and targets[0].status is OrderStatus.CANCELLED
    assert s26.positions() == [] or all(p.qty == 0 for p in s26.positions())


def test_intrabar_stop_and_target_short_assumes_stop_first(s26):
    code, r = enter(s26, side="sell")
    assert r["accepted"], r
    s26.clock.advance(60)
    bar(s26, 19994.0, 20010.0, 19955.0, 19956.0)
    stops = [o for o in s26.run(s26.broker.get_orders(open_only=False)) if o.role is OrderRole.STOP_LOSS]
    assert stops[0].status is OrderStatus.FILLED


def test_target_fill(s26):
    enter(s26)
    s26.clock.advance(60)
    bar(s26, 20007.0, 20041.0, 20005.0, 20040.5)
    targets = [o for o in s26.run(s26.broker.get_orders(open_only=False)) if o.role is OrderRole.TAKE_PROFIT]
    assert targets[0].status is OrderStatus.FILLED


# ============================================================================ diagnostics
def test_bar_diagnostics_stored_once_and_state_updated(s26):
    items = [diag_item("htf_fvg", 1, 60, 19970.0, 19990.0, 1), diag_item("tap", 1, 60, 19970.0, 19990.0, 1),
             diag_item("armed", 1, 60, 19970.0, 19990.0, 1)]
    bt = ms(s26.clock.now()) - 60_000
    bar(s26, 20000.0, 20001.0, 19985.0, 19990.0, items=items, bar_time=bt, long_armed=True)
    bar(s26, 20000.0, 20001.0, 19985.0, 19990.0, items=items, bar_time=bt, long_armed=True)  # resent
    d = s26.engine.diagnostics()
    assert [i["kind"] for i in d["items"]] == ["armed", "tap", "htf_fvg"]
    assert d["counts"] == {"htf_fvg": 1, "tap": 1, "armed": 1}
    state = d["strategy_state"]["V26-MNQ-1"]
    assert state["long"]["armed"] is True and state["zones_bull"] == 3 and state["bar_time"] == bt


def test_diagnostics_of_the_entry_bar_survive_the_entry(s26):
    """The entry advances last_bar_time; the same bar's diagnostics must still be stored."""
    payload = v26_entry(s26.clock)
    s26.run(s26.engine.handle_entry(EntryEvent.model_validate(payload)))
    items = [diag_item("inversion", 1, 60, 20000.0, 20005.0), diag_item("entry", 1, 60, 19994.0, 20040.0)]
    code, r = bar(s26, 20001.0, 20007.0, 20000.0, 20006.0, items=items, bar_time=payload["bar_time"])
    assert r["reason"] == "bar not newer than last processed bar"
    kinds = [i["kind"] for i in s26.engine.diagnostics()["items"]]
    assert {"inversion", "entry", "alert_received", "order_accepted"} <= set(kinds)


def test_entry_rejection_reason_recorded_in_diagnostics(s26):
    enter(s26, target_index=0)
    rows = {i["kind"]: i for i in s26.engine.diagnostics()["items"]}
    assert "below the 2R minimum" in rows["order_rejected"]["msg"]
    assert rows["alert_received"]["msg"] == "buy 2 @ 20006.0"


def test_diagnostics_ignored_for_non_allowlisted_strategy(h):
    bar(h, 20000.0, 20001.0, 19985.0, 19990.0, items=[diag_item("tap")])
    d = h.engine.diagnostics()
    assert d["items"] == [] and d["strategy_state"] == {}


def test_diagnostics_pruned_after_retention(harness):
    h = harness(**STAGING, diagnostics_retention_days=1)
    old = ms(h.clock.now()) - 3 * 86_400_000
    h.store.add_diagnostics("V26-MNQ-1", "V26-webhook.1", old, [(0, {"kind": "tap", "dir": 1})])
    hour = ms(h.clock.now()) // 3_600_000 * 3_600_000
    bar(h, 20000.0, 20001.0, 19985.0, 19990.0, items=[diag_item("tap")], bar_time=hour)
    assert [i["bar_time"] for i in h.engine.diagnostics()["items"]] == [hour]


# ============================================================================ HTTP pipeline
@pytest.fixture
def staging_client(tmp_path):
    clock = FakeClock(T0)
    app = create_app(make_settings(tmp_path, **STAGING), clock=clock, start_monitor=False)
    with TestClient(app, client=("203.0.113.9", 50000)) as c:
        c.clock = clock
        yield c


def test_http_v26_entry_duplicate_and_diagnostics_endpoint(staging_client):
    c = staging_client
    payload = v26_entry(c.clock)
    first = c.post(URL, json=payload)
    assert first.status_code == 200 and first.json()["accepted"], first.text
    dup = c.post(URL, json=payload)
    assert dup.json()["duplicate"] is True
    status = c.get("/status", headers=ADMIN).json()
    assert [p["qty"] for p in status["positions"]] == [2] and status["trades_today"] == 1
    assert status["positions"][0]["symbol"] == CONTRACT

    c.post(URL, json=v26_bar(c.clock, 20001.0, 20007.0, 20000.0, 20006.0, bar_time=payload["bar_time"],
                             items=[diag_item("entry", 1, 60, 19994.0, 20040.0)]))
    d = c.get("/admin/diagnostics", headers=ADMIN).json()
    assert d["counts"]["alert_received"] == 1 and d["counts"]["order_accepted"] == 1 and d["counts"]["entry"] == 1
    assert "V26-MNQ-1" in d["strategy_state"] and "strategy_state" in status
    assert c.get("/admin/diagnostics").status_code == 401


def test_http_v26_body_fits_default_limit(staging_client):
    """A bar alert with the maximum 20 diagnostics items stays below MAX_BODY_BYTES."""
    import json

    long_msg = "target: no untapped swing high with R in range (nearest 12.34R)"
    items = [diag_item("blocked", 1, 240, 21234.75, 21299.25, 1_760_000_000_000, long_msg)] * 20
    body = v26_bar(staging_client.clock, 20000.0, 20001.0, 19985.0, 19990.0, items=items, long_armed=True)
    assert len(json.dumps(body, separators=(",", ":"))) < 8192
    assert staging_client.post(URL, json=body).status_code == 200
