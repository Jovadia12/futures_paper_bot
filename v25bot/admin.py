"""Operator CLI (stdlib only, works inside the container).

  python -m v25bot.admin status
  python -m v25bot.admin kill --reason "manual"
  python -m v25bot.admin resume
  python -m v25bot.admin flatten
  python -m v25bot.admin reconcile
  python -m v25bot.admin quote --last 20000.25 [--bid ... --ask ...]
  python -m v25bot.admin test-entry --side buy --price 20000 --stop 19980 --target 20060
  python -m v25bot.admin test-bar --open 20000 --high 20010 --low 19990 --close 20005

Reads ADMIN_TOKEN / WEBHOOK_TOKEN (or *_FILE) from the environment. Tokens are
never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _secret(name: str) -> str:
    path = os.environ.get(f"{name}_FILE")
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"{name} (or {name}_FILE) is not set")
    return value


def _call(base: str, method: str, path: str, body: dict | None = None, admin: bool = True) -> int:
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["Authorization"] = "Bearer " + _secret("ADMIN_TOKEN")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(json.dumps(json.loads(resp.read() or b"null"), indent=2))
            return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}", file=sys.stderr)
        return 1


def _v1(event: str, kind: str, args) -> dict:
    now = int(time.time() * 1000)
    bar_time = now - now % 60_000 - (60_000 if event == "entry" else 0)
    return {
        "schema": "v25.webhook.v1", "strategy": "V25", "strategy_version": "V25-webhook.1",
        "bot_id": args.bot_id, "event": event, "event_id": f"V25-{args.bot_id}-{bar_time}-{kind}-manual{now % 100000}",
        "symbol": args.symbol, "contract": args.contract, "timeframe": "1",
        "bar_time": bar_time, "sent_at": now, "passphrase": os.environ.get("WEBHOOK_PASSPHRASE", ""),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m v25bot.admin")
    p.add_argument("--url", default=os.environ.get("ENGINE_URL", "http://127.0.0.1:8000"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("reconcile")
    k = sub.add_parser("kill")
    k.add_argument("--reason", default="manual kill switch")
    r = sub.add_parser("resume")
    r.add_argument("--actor", default="cli")
    f = sub.add_parser("flatten")
    f.add_argument("--reason", default="manual emergency flatten")
    q = sub.add_parser("quote")
    q.add_argument("--symbol", default="CME_MINI:MNQ1!")
    q.add_argument("--last", type=float, required=True)
    q.add_argument("--bid", type=float)
    q.add_argument("--ask", type=float)
    for name in ("test-entry", "test-bar"):
        t = sub.add_parser(name)
        t.add_argument("--symbol", default="CME_MINI:MNQ1!")
        t.add_argument("--contract", default=None, help="e.g. MNQZ2026 (syminfo.current_contract)")
        t.add_argument("--bot-id", default="V25-MNQ-1")
        if name == "test-entry":
            t.add_argument("--side", choices=["buy", "sell"], default="buy")
            t.add_argument("--qty", type=int, default=1)
            t.add_argument("--price", type=float, required=True)
            t.add_argument("--stop", type=float, required=True)
            t.add_argument("--target", type=float, required=True)
        else:
            for field in ("open", "high", "low", "close"):
                t.add_argument(f"--{field}", type=float, required=True)
    args = p.parse_args(argv)
    base = args.url.rstrip("/")

    if args.cmd == "status":
        return _call(base, "GET", "/status")
    if args.cmd == "reconcile":
        return _call(base, "POST", "/admin/reconcile")
    if args.cmd == "kill":
        return _call(base, "POST", "/admin/kill", {"reason": args.reason})
    if args.cmd == "resume":
        return _call(base, "POST", "/admin/resume", {"confirm": "RESUME", "actor": args.actor})
    if args.cmd == "flatten":
        return _call(base, "POST", "/admin/flatten", {"reason": args.reason})
    if args.cmd == "quote":
        return _call(base, "POST", "/paper/quote", {"symbol": args.symbol, "last": args.last,
                                                    "bid": args.bid, "ask": args.ask})
    token = _secret("WEBHOOK_TOKEN")
    if args.cmd == "test-entry":
        body = _v1("entry", f"entry-{args.side}", args) | {
            "side": args.side, "qty": args.qty, "order_type": "market", "price": args.price,
            "est_fill": args.price, "stop": args.stop, "target": args.target, "tick_size": 0.25,
            "point_value": 2.0, "risk_per_contract": 0, "planned_risk": 0,
            "sizing_mode": "Prop Conservative", "comment": "manual test",
        }
    else:
        body = _v1("bar", "bar", args) | {"open": args.open, "high": args.high, "low": args.low,
                                          "close": args.close, "volume": 0}
    return _call(base, "POST", f"/webhook/tradingview/{token}", body, admin=False)


if __name__ == "__main__":
    sys.exit(main())
