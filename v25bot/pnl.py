"""Realised P&L reconstructed from broker fills (average-cost method)."""

from __future__ import annotations

from datetime import date, datetime

from .clock import trading_day
from .contracts import spec_for_contract_name


def realized_by_day(fills: list[dict], roll_hour: int = 17) -> dict[date, float]:
    """Net realised P&L (after commissions) per trading day, replaying fills in time order."""
    positions: dict[str, tuple[int, float]] = {}
    out: dict[date, float] = {}
    for f in sorted(fills, key=lambda x: (x["ts"], x["id"])):
        pv = spec_for_contract_name(f["symbol"]).point_value
        qty, avg = positions.get(f["symbol"], (0, 0.0))
        signed = f["qty"] if f["side"] == "buy" else -f["qty"]
        realized = 0.0
        if qty != 0 and (qty > 0) != (signed > 0):
            closing = min(abs(qty), abs(signed))
            realized = (f["price"] - avg) * closing * (1 if qty > 0 else -1) * pv
        new_qty = qty + signed
        if new_qty == 0:
            new_avg = 0.0
        elif qty == 0 or (qty > 0) == (signed > 0):
            new_avg = (avg * abs(qty) + f["price"] * abs(signed)) / abs(new_qty)
        elif (qty > 0) != (new_qty > 0):
            new_avg = f["price"]
        else:
            new_avg = avg
        positions[f["symbol"]] = (new_qty, new_avg)
        day = trading_day(datetime.fromisoformat(f["ts"]), roll_hour)
        out[day] = out.get(day, 0.0) + realized - float(f.get("commission", 0.0))
    return out


def net_positions(fills: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in fills:
        out[f["symbol"]] = out.get(f["symbol"], 0) + (f["qty"] if f["side"] == "buy" else -f["qty"])
    return {k: v for k, v in out.items() if v != 0}
