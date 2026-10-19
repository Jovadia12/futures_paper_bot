"""Trade reconstruction and performance statistics.

Everything is derived from records the engine persisted: broker fills, orders,
webhook events, and the audit log. Nothing is estimated or filled in. When a
value cannot be computed from data it is returned as ``None`` and the UI
shows a dash.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from ..clock import NY
from ..contracts import ContractError, spec_for_contract_name

RANGES = ("today", "7d", "30d", "all")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _point_value(symbol: str) -> float | None:
    try:
        return spec_for_contract_name(symbol).point_value
    except ContractError:
        return None


@dataclass
class Trade:
    id: str
    symbol: str
    side: str  # "long" | "short"
    qty: int
    entry_time: str
    entry_price: float
    entry_order_id: str
    exit_time: str | None = None
    exit_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    gross_pnl: float = 0.0
    commission: float = 0.0
    net_pnl: float | None = None
    exit_reason: str | None = None
    duration_seconds: float | None = None
    event_id: str | None = None
    status: str = "open"  # "open" | "closed"
    fills: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_ROLE_REASON = {"stop_loss": "Stop loss", "take_profit": "Take profit", "flatten": "Flatten", "entry": "Reversal"}
_FLATTEN_RE = re.compile(r"flatten \((.+)\) (?:complete|FAILED)")


def _flatten_reason(exit_time: datetime, audit: list[dict]) -> str | None:
    best: tuple[float, str] | None = None
    for row in audit:
        if row.get("kind") != "flatten":
            continue
        ts = parse_ts(row.get("ts"))
        m = _FLATTEN_RE.search(row.get("message", ""))
        if ts is None or m is None:
            continue
        gap = abs((ts - exit_time).total_seconds())
        if gap <= 10 and (best is None or gap < best[0]):
            best = (gap, m.group(1))
    return best[1] if best else None


def build_trades(fills: list[dict], orders: list[dict], audit: list[dict] | None = None) -> list[Trade]:
    """Replay fills (time order) into round-trip trades: flat -> position -> flat."""
    audit = audit or []
    by_parent: dict[str, dict[str, dict]] = defaultdict(dict)
    order_event: dict[str, str] = {}
    for o in orders:
        if o.get("parent_id"):
            by_parent[o["parent_id"]][o.get("role", "")] = o
        if o.get("id") and o.get("event_id"):
            order_event[o["id"]] = o["event_id"]

    trades: list[Trade] = []
    state: dict[str, dict] = {}

    def open_trade(f: dict, signed: int, price: float) -> dict:
        return {"qty": 0, "avg": 0.0, "max_qty": 0, "exit_qty": 0, "exit_value": 0.0, "last_exit_role": None,
                "trade": Trade(id=f"T-{f['id']}", symbol=f["symbol"], side="long" if signed > 0 else "short",
                               qty=0, entry_time=f["ts"], entry_price=price, entry_order_id=f["order_id"],
                               event_id=order_event.get(f["order_id"]))}

    def close_trade(st: dict, f: dict) -> None:
        t: Trade = st["trade"]
        exit_dt, entry_dt = parse_ts(f["ts"]), parse_ts(t.entry_time)
        t.exit_time = f["ts"]
        t.exit_price = round(st["exit_value"] / st["exit_qty"], 6)
        t.status = "closed"
        t.gross_pnl = round(t.gross_pnl, 2)
        t.commission = round(t.commission, 2)
        t.net_pnl = round(t.gross_pnl - t.commission, 2)
        if entry_dt and exit_dt:
            t.duration_seconds = (exit_dt - entry_dt).total_seconds()
        role = st["last_exit_role"] or ""
        reason = _ROLE_REASON.get(role, role.replace("_", " ").title() or "Exit")
        if role == "flatten" and exit_dt:
            detail = _flatten_reason(exit_dt, audit)
            if detail:
                reason = f"Flatten: {detail}"
        t.exit_reason = reason
        trades.append(t)

    for f in sorted(fills, key=lambda x: (str(x.get("ts")), str(x.get("id")))):
        symbol = f["symbol"]
        pv = _point_value(symbol)
        if pv is None:
            continue
        price = float(f["price"])
        sign = 1 if f["side"] == "buy" else -1
        remaining = int(f["qty"])
        commission = float(f.get("commission") or 0.0)
        while remaining > 0:
            st = state.get(symbol)
            if st is None or st["qty"] == 0:
                st = state[symbol] = open_trade(f, sign, price)
            t: Trade = st["trade"]
            if f["id"] not in t.fills:
                t.fills.append(f["id"])
            t.commission += commission
            commission = 0.0  # charge a fill's commission once, to the trade it touches first
            if st["qty"] == 0 or (st["qty"] > 0) == (sign > 0):
                new_qty = st["qty"] + sign * remaining
                st["avg"] = (st["avg"] * abs(st["qty"]) + price * remaining) / abs(new_qty)
                st["qty"] = new_qty
                st["max_qty"] = max(st["max_qty"], abs(new_qty))
                t.entry_price = round(st["avg"], 6)
                t.qty = st["max_qty"]
                remaining = 0
            else:
                closing = min(abs(st["qty"]), remaining)
                t.gross_pnl += (price - st["avg"]) * closing * (1 if st["qty"] > 0 else -1) * pv
                st["exit_qty"] += closing
                st["exit_value"] += price * closing
                st["qty"] += sign * closing
                st["last_exit_role"] = f.get("role")
                remaining -= closing
                if st["qty"] == 0:
                    close_trade(st, f)
    for st in state.values():
        if st["qty"] != 0:
            t = st["trade"]
            t.commission = round(t.commission, 2)
            t.gross_pnl = round(t.gross_pnl, 2)
            trades.append(t)
    for t in trades:
        children = by_parent.get(t.entry_order_id, {})
        if "stop_loss" in children:
            t.stop_price = children["stop_loss"].get("stop_price")
        if "take_profit" in children:
            t.target_price = children["take_profit"].get("limit_price")
    trades.sort(key=lambda t: t.entry_time)
    return trades


def realized_net_all_fills(fills: list[dict]) -> float:
    """Realised P&L minus commissions over every fill, including open trades' entry costs."""
    total = 0.0
    for t in build_trades(fills, []):
        total += t.gross_pnl - t.commission
    return round(total, 2)


# --------------------------------------------------------------------- filtering
def range_start(range_key: str, now: datetime) -> datetime | None:
    now_ny = now.astimezone(NY)
    if range_key == "today":
        return now_ny.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    if range_key == "7d":
        return now - timedelta(days=7)
    if range_key == "30d":
        return now - timedelta(days=30)
    return None


def closed_in_range(trades: list[Trade], range_key: str, now: datetime) -> list[Trade]:
    start = range_start(range_key, now)
    out = []
    for t in trades:
        if t.status != "closed":
            continue
        exit_dt = parse_ts(t.exit_time)
        if start is None or (exit_dt and exit_dt >= start):
            out.append(t)
    return out


def filter_trades(trades: list[Trade], *, range_key: str = "all", now: datetime, q: str = "",
                  side: str = "", reason: str = "", outcome: str = "") -> list[Trade]:
    start = range_start(range_key, now)
    q = q.strip().lower()
    out = []
    for t in trades:
        ref = parse_ts(t.exit_time or t.entry_time)
        if start is not None and (ref is None or ref < start):
            continue
        if side and t.side != side:
            continue
        if reason and (t.exit_reason or "").split(":")[0].lower() != reason.lower():
            continue
        if outcome == "win" and not (t.net_pnl is not None and t.net_pnl > 0):
            continue
        if outcome == "loss" and not (t.net_pnl is not None and t.net_pnl < 0):
            continue
        if outcome == "open" and t.status != "open":
            continue
        if q and q not in " ".join(str(v) for v in (t.symbol, t.side, t.exit_reason, t.id, t.event_id)).lower():
            continue
        out.append(t)
    return out


# --------------------------------------------------------------------- statistics
def summarize(trades: list[Trade]) -> dict:
    closed = [t for t in trades if t.status == "closed" and t.net_pnl is not None]
    wins = [t.net_pnl for t in closed if t.net_pnl > 0]
    losses = [t.net_pnl for t in closed if t.net_pnl < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    if losses:
        pf = round(gross_win / abs(gross_loss), 2)
    else:
        pf = None
    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(closed) - len(wins) - len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "profit_factor": pf,
        "profit_factor_infinite": bool(wins) and not losses,
        "gross_profit": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(gross_win + gross_loss, 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else None,
        "largest_win": round(max(wins), 2) if wins else None,
        "largest_loss": round(min(losses), 2) if losses else None,
        "commissions": round(sum(t.commission for t in closed), 2),
    }


def equity_points(trades: list[Trade], start_balance: float) -> list[dict]:
    """Closed-trade equity curve: one point per trade exit."""
    points, equity = [], start_balance
    for t in sorted((t for t in trades if t.status == "closed"), key=lambda t: t.exit_time or ""):
        equity += t.net_pnl or 0.0
        points.append({"t": t.exit_time, "equity": round(equity, 2), "pnl": t.net_pnl, "trade_id": t.id})
    return points


def drawdown_stats(points: list[dict], start_balance: float, current_equity: float | None) -> dict:
    series = [start_balance] + [p["equity"] for p in points]
    if current_equity is not None:
        series.append(current_equity)
    peak, max_dd, max_dd_pct = series[0], 0.0, 0.0
    for value in series:
        peak = max(peak, value)
        dd = peak - value
        if dd > max_dd:
            max_dd, max_dd_pct = dd, (dd / peak * 100 if peak else 0.0)
    current = (peak - series[-1]) if series else 0.0
    return {
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "current_drawdown": round(current, 2),
        "current_drawdown_pct": round(current / peak * 100, 2) if peak else None,
        "peak_equity": round(peak, 2),
    }


def analytics(trades: list[Trade], range_key: str, now: datetime, start_balance: float,
              current_equity: float | None) -> dict:
    """Series for every chart, scoped to ``range_key`` (by trade exit time)."""
    all_closed = sorted((t for t in trades if t.status == "closed"), key=lambda t: t.exit_time or "")
    scoped = closed_in_range(all_closed, range_key, now)
    start = range_start(range_key, now)
    base = start_balance + sum(
        t.net_pnl or 0.0 for t in all_closed if start is not None and parse_ts(t.exit_time) < start)

    equity, cumulative, drawdown = [], [], []
    eq, peak = base, base
    if scoped:
        equity.append({"t": scoped[0].entry_time, "v": round(base, 2)})
        cumulative.append({"t": scoped[0].entry_time, "v": 0.0})
        drawdown.append({"t": scoped[0].entry_time, "v": 0.0})
    for t in scoped:
        eq += t.net_pnl or 0.0
        peak = max(peak, eq)
        equity.append({"t": t.exit_time, "v": round(eq, 2)})
        cumulative.append({"t": t.exit_time, "v": round(eq - base, 2)})
        drawdown.append({"t": t.exit_time, "v": round(-(peak - eq), 2)})

    daily: dict[str, dict] = {}
    for t in scoped:
        day = parse_ts(t.exit_time).astimezone(NY).date().isoformat()
        d = daily.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0, "wins": 0})
        d["pnl"] += t.net_pnl or 0.0
        d["trades"] += 1
        d["wins"] += 1 if (t.net_pnl or 0) > 0 else 0
    for d in daily.values():
        d["pnl"] = round(d["pnl"], 2)

    weekday = []
    for i, name in enumerate(WEEKDAYS[:5]):
        sel = [t for t in scoped if parse_ts(t.exit_time).astimezone(NY).weekday() == i]
        wins = sum(1 for t in sel if (t.net_pnl or 0) > 0)
        weekday.append({"day": name, "pnl": round(sum(t.net_pnl or 0 for t in sel), 2), "trades": len(sel),
                        "win_rate": round(wins / len(sel) * 100, 1) if sel else None})

    by_symbol = []
    for symbol in sorted({t.symbol for t in scoped}):
        sel = [t for t in scoped if t.symbol == symbol]
        by_symbol.append({"symbol": symbol, **summarize(sel)})

    return {
        "range": range_key,
        "summary": summarize(scoped),
        "equity": equity,
        "cumulative": cumulative,
        "drawdown": drawdown,
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "distribution": distribution([t.net_pnl for t in scoped if t.net_pnl is not None]),
        "weekday": weekday,
        "by_symbol": by_symbol,
        "current_equity": current_equity,
    }


def distribution(values: list[float], max_bins: int = 12) -> list[dict]:
    """Histogram of trade P&L with a bin edge at exactly zero, so bars never straddle win/loss."""
    if not values:
        return []
    lo, hi = min(values + [0.0]), max(values + [0.0])
    span = hi - lo
    if span == 0:
        return [{"from": 0.0, "to": 0.0, "count": len(values), "sign": "flat"}]
    raw = span / max_bins
    mag = 10 ** math.floor(math.log10(raw))
    width = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / width) * width
    bins = []
    edge = start
    while edge < hi or not bins:
        bins.append({"from": round(edge, 2), "to": round(edge + width, 2), "count": 0,
                     "sign": "loss" if edge + width <= 0 else "win"})  # zero is always an edge
        edge += width
    for v in values:
        bins[min(int((v - start) // width), len(bins) - 1)]["count"] += 1
    return bins


# --------------------------------------------------------------------- activity
_AUDIT_CATEGORY = {
    "trading_halted": "halt", "kill_switch": "halt", "halt_cleared": "system", "resume": "system",
    "flatten_failed": "error", "unprotected_position": "error", "monitor_error": "error",
    "webhook_handler_error": "error", "broker_state_unavailable": "error", "order_outcome_unknown": "error",
    "broker_disconnected": "error", "broker_reconnected": "system", "broker_connect_failed": "error",
    "session_flatten": "exit", "flatten": "exit", "stale_price": "warning", "tradingview_silent": "warning",
    "webhook_ip_blocked": "warning", "event_id_conflict": "warning", "contract_roll": "system",
    "tv_exit": "signal", "tv_exit_flatten": "exit", "tv_exit_divergence": "warning", "orphan_order": "system",
    "config": "system", "day_roll": "system",
}
# Already represented by webhook-event or fill rows.
_AUDIT_SKIP = {"entry_rejected", "entry_accepted", "entry_liquidated_unprotected"}


def activity(events: list[dict], fills: list[dict], audit: list[dict], limit: int = 300) -> list[dict]:
    items: list[dict] = []
    for e in events:
        result = e.get("result") or {}
        kind, status = e.get("kind"), e.get("status")
        reasons = result.get("reasons") or []
        if kind == "entry":
            if status in ("accepted",):
                cat, title = "signal", "Entry signal accepted"
            elif status == "liquidated_unprotected":
                cat, title = "error", "Entry filled without protection: liquidated"
            elif status == "error":
                cat, title = "error", "Entry signal failed (broker error)"
            elif reasons and str(reasons[0]).startswith("broker rejected"):
                cat, title = "rejected", "Order rejected by broker"
            elif status == "rejected" and "risk" in result:
                # The engine attaches "risk" exactly when its risk engine evaluated the signal.
                cat, title = "risk", "Risk-management rejection"
            elif status == "rejected":
                cat, title = "rejected", "Signal rejected"
            else:
                cat, title = "signal", f"Entry signal {status}"
            detail = "; ".join(str(r) for r in reasons) if reasons else (
                f"{result.get('qty', '')} {result.get('contract', '')}".strip() or None)
        elif kind == "exit":
            cat, title, detail = "signal", "TradingView exit signal", result.get("status")
        elif kind == "flatten":
            cat, title, detail = "signal", "TradingView flatten signal", result.get("reason")
        else:
            cat, title, detail = "signal", f"{kind} signal", status
        items.append({"ts": e.get("received_at"), "category": cat, "title": title, "detail": detail,
                      "source": "webhook", "ref": e.get("event_id")})
    for f in fills:
        role = f.get("role")
        cat, title = {
            "entry": ("entry", "Entry filled"), "stop_loss": ("stop", "Stop-loss triggered"),
            "take_profit": ("target", "Take-profit filled"), "flatten": ("exit", "Position flattened"),
        }.get(role, ("exit", "Fill"))
        items.append({"ts": f.get("ts"), "category": cat, "title": title,
                      "detail": f"{f['side'].upper()} {f['qty']} {f['symbol']} @ {f['price']}",
                      "source": "broker", "ref": f.get("id")})
    for a in audit:
        kind = a.get("kind", "")
        if kind in _AUDIT_SKIP:
            continue
        level = (a.get("level") or "").upper()
        cat = _AUDIT_CATEGORY.get(kind) or ("error" if level in ("ERROR", "CRITICAL") else "system")
        items.append({"ts": a.get("ts"), "category": cat, "title": kind.replace("_", " ").capitalize(),
                      "detail": a.get("message"), "source": "engine", "level": level, "ref": None})
    items.sort(key=lambda i: parse_ts(i["ts"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items[:limit]


# --------------------------------------------------------------------- market hours
def cme_equity_futures_open(now: datetime) -> bool:
    """CME Globex equity-index hours: Sun 18:00 – Fri 17:00 ET, daily break 17:00–18:00 ET."""
    ny = now.astimezone(NY)
    wd, h = ny.weekday(), ny.hour
    if wd == 5:
        return False
    if wd == 6:
        return h >= 18
    if wd == 4:
        return h < 17
    return not (h == 17)
