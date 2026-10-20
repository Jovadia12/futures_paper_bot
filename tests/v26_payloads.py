"""Builders for V26 webhook payloads exactly as pine/V26_webhook.pine formats them.

The geometry is one hand-checked long setup; shorts are its mirror image around
20000, so every short check exercises the opposite boundaries and extremes.
"""

from __future__ import annotations

import copy

from conftest import ms

TICK, PV = 0.25, 2.0
MIRROR = 40_000.0

# LONG: bearish opposing FVG made of three 1m candles (t offsets in minutes before the entry bar).
#   c1 high 20010 low 20005 | c2 high 20006 low 19990 | c3 high 20000 low 19994
#   gap top = c1 low 20005, gap bottom = c3 high 20000 (20 ticks)
#   V26 stop = candle-3 low 19994. Deliberately different from the lowest low of all
#   three candles (19990, candle 2) and from the FVG boundary (20000).
LONG_CANDLES = [(-3, 20010.0, 20005.0), (-2, 20006.0, 19990.0), (-1, 20000.0, 19994.0)]
LONG_CLOSE = 20006.0                       # closes above the gap top 20005: inversion
LONG_LEVELS = [20020.0, 20040.0, 20060.0]  # untapped swing highs, nearest first


def _m(p: float) -> float:
    return MIRROR - p


def v26_common(clock, event: str, kind: str | None = None, **kw) -> dict:
    now = ms(clock.now())
    bar_time = kw.pop("bar_time", now - 60_000)
    d = {
        "schema": "v26.webhook.v1", "strategy": "V26", "strategy_version": "V26-webhook.1",
        "bot_id": "V26-MNQ-1", "event": event,
        "event_id": f"V26-V26-MNQ-1-{bar_time}-{kind or event}",
        "symbol": "CME_MINI:MNQ1!", "contract": "MNQZ2026", "timeframe": "1",
        "bar_time": bar_time, "sent_at": now, "passphrase": "",
    }
    d.update(kw)
    return d


def v26_entry(clock, side: str = "buy", target_index: int | None = None,
              levels: list[float] | None = None, caps: dict | None = None,
              tamper=None, **kw) -> dict:
    """A consistent V26 entry. ``target_index`` picks a level (default: nearest with R >= 2)."""
    long = side == "buy"
    sign = 1 if long else -1
    d = v26_common(clock, "entry", f"entry-{side}", **kw)
    bar_time = d["bar_time"]
    candles = []
    for off, h, low in LONG_CANDLES:
        if long:
            candles.append({"t": bar_time + off * 60_000, "h": h, "l": low})
        else:
            candles.append({"t": bar_time + off * 60_000, "h": _m(low), "l": _m(h)})
    c1, c2, c3 = candles
    top, bottom = (c1["l"], c3["h"]) if long else (c3["l"], c1["h"])
    close = LONG_CLOSE if long else _m(LONG_CLOSE)
    caps = caps or {"max_contracts": 2, "max_trade_risk": 200, "cost_reserve": 8, "slippage_ticks": 2}
    est_fill = close + sign * caps["slippage_ticks"] * TICK
    stop = c3["l"] if long else c3["h"]  # candle 3's wick: low for a long, high for a short
    risk = (est_fill - stop) * sign
    lv = levels if levels is not None else (LONG_LEVELS if long else [_m(p) for p in LONG_LEVELS])
    cands = [{"p": p, "t": bar_time - (30 + i) * 60_000} for i, p in enumerate(lv)]
    if target_index is None:
        target_index = next(i for i, c in enumerate(cands) if (c["p"] - est_fill) * sign / risk >= 2.0)
    tgt = cands[target_index]
    rr = (tgt["p"] - est_fill) * sign / risk
    rpc = risk * PV + caps["cost_reserve"]
    qty = min(caps["max_contracts"], int(caps["max_trade_risk"] // rpc))
    d.update({
        "side": side, "qty": max(qty, 1), "order_type": "market", "price": close, "est_fill": est_fill,
        "stop": stop, "target": tgt["p"], "tick_size": TICK, "point_value": PV,
        "risk_per_contract": round(rpc, 2), "planned_risk": round(max(qty, 1) * rpc, 2),
        "sizing_mode": "Engine limits", "comment": "V26 Bull IFVG" if long else "V26 Bear IFVG",
        "caps": caps,
        "setup": {
            "variant": "core", "htf_tf": 60,
            "zone_lo": 19970.0 if long else _m(19990.0), "zone_hi": 19990.0 if long else _m(19970.0),
            "zone_time": bar_time - 120 * 60_000, "tap_time": bar_time - 10 * 60_000,
            "fvg": {"c1": c1, "c2": c2, "c3": c3, "top": top, "bottom": bottom},
            "stop_anchor": "candle3_wick", "min_gap_ticks": 2,
            "target_time": tgt["t"], "target_rr": round(rr, 4), "min_rr": 2, "max_rr": 12,
            "candidates": cands,
        },
    })
    if tamper:
        d = copy.deepcopy(d)
        tamper(d)
    return d


def diag_item(k: str, dir: int = 1, tf: int | None = 60, lo=None, hi=None, t=None, msg: str = "") -> dict:
    return {"k": k, "dir": dir, "tf": tf, "lo": lo, "hi": hi, "t": t, "msg": msg}


def v26_bar(clock, o, h, low, c, items=None, bar_time=None, long_armed=False, **kw) -> dict:
    bar_time = bar_time if bar_time is not None else ms(clock.now()) - 60_000
    d = v26_common(clock, "bar", bar_time=bar_time, **kw)
    long_state = {"armed": False}
    if long_armed:
        long_state = {"armed": True, "tf": 60, "zone_lo": 19990.0, "zone_hi": 20000.0, "tap_time": bar_time,
                      "opp_lo": 20000.0, "opp_hi": 20005.0, "opp_time": bar_time - 60_000}
    d.update({
        "open": o, "high": h, "low": low, "close": c, "volume": 100,
        "diag": {"variant": "core", "zones_bull": 3, "zones_bear": 2,
                 "zones_tf": "5:1/0,10:0/0,15:1/1,30:0/0,45:0/0,60:1/1,120:0/0,240:0/0",
                 "long": long_state, "short": {"armed": False}, "items": items or []},
    })
    return d
