"""Engine-side checks that a V26 entry follows the V26 rules (docs/V26_STRATEGY.md).

The Pine script decides the trade; these checks re-derive the parts that can be
verified from the payload alone and reject anything inconsistent, so a modified
script, a mis-set input or a corrupted alert cannot reach the paper broker:

* the engine limits the script sized with equal the engine's own settings;
* the opposing 1-minute FVG is a real three-candle gap, in candle order, all
  candles closed before the entry bar, at least ``min_gap_ticks`` wide;
* the entry close inverted that gap;
* the stop is exactly candle 3's wick of the inverted gap (low for a long,
  high for a short; candle 3 = last candle of the gap's original formation);
* the target is a confirmed swing level from before the entry, offers at least
  MIN_REWARD_RISK, and is the nearest eligible candidate;
* the declared risk per contract matches the engine's own formula.
"""

from __future__ import annotations

from .config import Settings
from .schemas import EntryEvent

EPS = 1e-6


def _on_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)


def caps_mismatch(evt: EntryEvent, s: Settings) -> list[str]:
    caps = evt.caps
    pairs = (
        ("max_contracts", caps.max_contracts, s.max_contracts, "MAX_CONTRACTS"),
        ("max_trade_risk", caps.max_trade_risk, s.max_trade_risk_usd, "MAX_TRADE_RISK_USD"),
        ("cost_reserve", caps.cost_reserve, s.cost_reserve_per_contract_usd, "COST_RESERVE_PER_CONTRACT_USD"),
        ("slippage_ticks", caps.slippage_ticks, s.risk_entry_slippage_ticks, "RISK_ENTRY_SLIPPAGE_TICKS"),
    )
    return [
        f"Pine input {name}={pine:g} does not match engine {env}={engine:g}: set section 7 of the "
        "V26 inputs to the engine's values and recreate the alert"
        for name, pine, engine, env in pairs
        if abs(float(pine) - float(engine)) > EPS
    ]


def check(evt: EntryEvent, s: Settings) -> list[str]:
    """Return the reasons the entry breaks a V26 rule (empty when it is consistent)."""
    st, g = evt.setup, evt.setup.fvg
    tick = evt.tick_size
    long = evt.side == "buy"
    why: list[str] = []

    # --- HTF zone and tap
    if not st.zone_lo < st.zone_hi:
        why.append(f"HTF zone {st.zone_lo}-{st.zone_hi} is empty")
    if not st.zone_time < st.tap_time <= evt.bar_time:
        why.append("HTF zone must form before the tap, and the tap cannot be after the entry bar")

    # --- the opposing 1m FVG: three closed candles in order
    if not g.c1.t < g.c2.t < g.c3.t < evt.bar_time:
        why.append("FVG candles must be in order c1 < c2 < c3 and closed before the entry bar")
    if long:  # bearish gap: candle-3 high below candle-1 low
        exp_top, exp_bottom, kind = g.c1.l, g.c3.h, "bearish"
    else:     # bullish gap: candle-3 low above candle-1 high
        exp_top, exp_bottom, kind = g.c3.l, g.c1.h, "bullish"
    if abs(g.top - exp_top) > EPS or abs(g.bottom - exp_bottom) > EPS:
        why.append(f"{kind} FVG boundaries {g.bottom}-{g.top} do not match its candles "
                   f"(expected {exp_bottom}-{exp_top})")
    if g.top - g.bottom < st.min_gap_ticks * tick - EPS:
        why.append(f"FVG {g.bottom}-{g.top} is smaller than {st.min_gap_ticks} ticks")

    # --- confirmed inversion by the entry bar's close
    if long and not evt.price > g.top:
        why.append(f"close {evt.price} did not invert the bearish FVG (top {g.top})")
    if not long and not evt.price < g.bottom:
        why.append(f"close {evt.price} did not invert the bullish FVG (bottom {g.bottom})")

    # --- stop: candle 3 of the inverted gap's original formation (low for a long, high for a short).
    # Not the extreme of all three candles, not the gap boundary, no buffer.
    anchor = g.c3.l if long else g.c3.h
    exp_stop = _on_tick(anchor, tick)
    if abs(evt.stop - exp_stop) > EPS:
        why.append(f"stop {evt.stop} is not candle 3's {'low' if long else 'high'} {anchor} "
                   f"of the inverted {kind} FVG")

    # --- fill estimate and risk per contract (same formula as the risk engine)
    sign = 1 if long else -1
    exp_fill = evt.price + sign * evt.caps.slippage_ticks * tick
    if abs(evt.est_fill - exp_fill) > EPS:
        why.append(f"est_fill {evt.est_fill} is not close {sign * evt.caps.slippage_ticks:+d} ticks ({exp_fill})")
    risk = (exp_fill - evt.stop) * sign
    if risk <= 0:
        why.append("stop is not beyond the estimated fill")
        return why
    exp_rpc = risk * evt.point_value + evt.caps.cost_reserve
    if abs(evt.risk_per_contract - exp_rpc) > 0.0051:
        why.append(f"risk_per_contract {evt.risk_per_contract} does not match {exp_rpc:.2f}")

    # --- liquidity target: confirmed before entry, >= MIN_REWARD_RISK, nearest eligible
    min_rr = max(st.min_rr, s.min_reward_risk)
    rr = (evt.target - exp_fill) * sign / risk
    if abs(rr - st.target_rr) > 1e-3:
        why.append(f"target_rr {st.target_rr} does not match {rr:.4f}")
    if rr < min_rr - 1e-9:
        why.append(f"target {evt.target} offers {rr:.2f}R, below the {min_rr:g}R minimum")
    if st.max_rr > 0 and rr > st.max_rr + 1e-9:
        why.append(f"target {evt.target} offers {rr:.2f}R, above the {st.max_rr:g}R maximum")
    if not st.target_time < evt.bar_time:
        why.append("target swing level must be confirmed before the entry bar")

    cands = st.candidates
    dist = [(c.p - exp_fill) * sign for c in cands]
    if any(d <= 0 for d in dist) or any(b <= a for a, b in zip(dist, dist[1:])):
        why.append("candidates must lie beyond the fill, nearest first")
        return why
    if any(c.t >= evt.bar_time for c in cands):
        why.append("candidate swing levels must be confirmed before the entry bar")

    def eligible(d: float) -> bool:
        r = d / risk
        return r >= min_rr - 1e-9 and (st.max_rr <= 0 or r <= st.max_rr + 1e-9)

    first = next((c for c, d in zip(cands, dist) if eligible(d)), None)
    listed = any(abs(c.p - evt.target) <= EPS for c in cands)
    if first is not None:
        if abs(first.p - evt.target) > EPS or first.t != st.target_time:
            why.append(f"target {evt.target} is not the nearest eligible swing level ({first.p})")
    elif not listed and (len(cands) < 5 or (evt.target - cands[-1].p) * sign <= 0):
        # Only the 5 nearest levels are listed; an unlisted target must lie beyond all of them.
        why.append(f"target {evt.target} is not among the listed swing levels")
    return why
