"""Compare two TradingView Strategy Tester "List of trades" exports (e.g. V25 vs V26).

    python tools/compare_tv_trades.py V25_trades.csv V26_trades.csv [more.csv ...]

Export each from TradingView: Strategy Tester -> List of trades -> Export (download icon),
using the same chart, symbol, timeframe and date range for every script.

Everything printed is a HISTORICAL SIMULATION by TradingView's broker emulator on past bars.
It is not live or paper performance and says nothing certain about future results.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path


def _col(header: list[str], *must: str, avoid: tuple[str, ...] = ()) -> int | None:
    for i, name in enumerate(header):
        low = name.lower()
        if all(m in low for m in must) and not any(a in low for a in avoid):
            return i
    return None


def _num(text: str) -> float:
    return float(text.replace(",", "").replace("−", "-").replace("$", "").strip() or 0)


def load(path: Path) -> list[float]:
    """Net P&L (USD) per closed trade, in export order of trade number."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    trade = _col(header, "trade #")
    kind = _col(header, "type")
    pnl = (_col(header, "net p&l", "usd", avoid=("cumulative", "%"))
           or _col(header, "profit", "usd", avoid=("cumulative", "%")))
    if None in (trade, kind, pnl):
        raise SystemExit(f"{path}: not a TradingView 'List of trades' export (columns: {header})")
    out: dict[int, float] = {}
    for r in rows[1:]:
        if len(r) > max(trade, kind, pnl) and r[kind].lower().startswith("exit"):
            out[int(_num(r[trade]))] = _num(r[pnl])
    return [out[k] for k in sorted(out)]


def summarize(pnls: list[float]) -> dict:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    equity = peak = max_dd = 0.0
    streak = worst = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        streak = streak + 1 if p < 0 else 0
        worst = max(worst, streak)
    return {
        "trades": len(pnls), "wins": len(wins), "losses": len(losses),
        "win_rate_%": round(100 * len(wins) / len(pnls), 1) if pnls else None,
        "net_pnl": round(sum(pnls), 2),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "max_drawdown": round(max_dd, 2), "longest_losing_streak": worst,
    }


def main(paths: list[str]) -> None:
    if len(paths) < 2:
        raise SystemExit(__doc__)
    results = {Path(p).stem: summarize(load(Path(p))) for p in paths}
    keys = list(next(iter(results.values())))
    width = max(len(n) for n in results) + 2
    print("HISTORICAL SIMULATION (TradingView Strategy Tester) - not live or paper results\n")
    print("metric".ljust(24) + "".join(n.rjust(max(width, 14)) for n in results))
    for k in keys:
        print(k.ljust(24) + "".join(str(r[k] if r[k] is not None else "-").rjust(max(width, 14)) for r in results.values()))


if __name__ == "__main__":
    main(sys.argv[1:])
