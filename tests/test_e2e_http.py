"""End-to-end PAPER drill over real HTTP.

A real uvicorn server on a TCP port, background monitor running, driven with
the exact JSON the V25 webhook Pine script emits. The clock is injected so the
drill can run at any wall-clock time; everything else (sockets, routing, auth,
persistence, simulator, monitor task) is the production code path.

This proves the paper pipeline end to end. It says nothing about a real
broker: no live or broker-sim execution has been verified.
"""

import socket
import threading
import time
from datetime import datetime

import httpx
import uvicorn

from conftest import ADMIN, CONTRACT, WEBHOOK_TOKEN, bar_payload, entry_payload, make_settings
from v25bot.api import create_app
from v25bot.clock import NY, FakeClock


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_paper_end_to_end_over_http(tmp_path):
    clock = FakeClock(datetime(2026, 10, 20, 10, 0, tzinfo=NY))
    settings = make_settings(tmp_path, monitor_interval_seconds=0.1, reconcile_interval_seconds=0.2)
    app = create_app(settings, clock=clock, start_monitor=True)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    hook = f"{base}/webhook/tradingview/{WEBHOOK_TOKEN}"
    try:
        with httpx.Client(timeout=5) as http:
            for _ in range(100):
                try:
                    if http.get(f"{base}/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.05)
            health = http.get(f"{base}/health").json()
            assert health["mode"] == "PAPER_ONLY" and health["broker_connected"]

            def status():
                return http.get(f"{base}/status", headers=ADMIN).json()

            def wait_for(pred, timeout=3.0):
                end = time.time() + timeout
                while time.time() < end:
                    st = status()
                    if pred(st):
                        return st
                    time.sleep(0.05)
                raise AssertionError(f"condition not met; last status: {st}")

            # 1. Unauthenticated request is refused.
            assert http.post(f"{base}/webhook/tradingview/{'x' * 32}", json={}).status_code == 403

            # 2. Long entry -> protected bracket.
            r = http.post(hook, json=entry_payload(clock))
            assert r.status_code == 200 and r.json()["accepted"], r.text
            assert r.json()["protection"]["ok"]
            st = status()
            assert [(p["symbol"], p["qty"]) for p in st["positions"]] == [(CONTRACT, 1)]
            assert {o["role"] for o in st["open_orders"]} == {"stop_loss", "take_profit"}

            # 3. Duplicate delivery -> no second order.
            assert http.post(hook, json=entry_payload(clock)).json()["duplicate"] is True

            # 4. Next 1m bar trades through the target -> OCO exit, profit.
            clock.advance(60)
            assert http.post(hook, json=bar_payload(clock, 20001.0, 20061.0, 19999.0, 20055.0)).json()["status"] == "processed"
            st = wait_for(lambda s: s["positions"] == [])
            assert st["account"]["realized_pnl_today"] > 0
            assert st["open_orders"] == []

            # 5. Short entry, then a bar gaps through the stop -> loss with slippage.
            clock.advance(60)
            r = http.post(hook, json=entry_payload(clock, side="sell", price=20050.0))
            assert r.json()["accepted"], r.text
            clock.advance(60)
            http.post(hook, json=bar_payload(clock, 20060.0, 20075.0, 20058.0, 20072.0))
            st = wait_for(lambda s: s["positions"] == [])
            fills = http.get(f"{base}/admin/fills", headers=ADMIN).json()
            stop_fill = [f for f in fills if f["role"] == "stop_loss"][-1]
            assert stop_fill["price"] > 20070.0  # stop 20070 + gap/slippage

            # 6. Daily trade limit (2) enforced from the engine's own records.
            clock.advance(60)
            r = http.post(hook, json=entry_payload(clock, price=20072.0))
            assert r.status_code == 409 and "max trades" in r.json()["reasons"][0]

            # 7. Next day: entry, then the background monitor flattens at 14:30 NY.
            clock.set(datetime(2026, 10, 21, 14, 0, tzinfo=NY))
            r = http.post(hook, json=entry_payload(clock))
            assert r.json()["accepted"], r.text
            clock.set(datetime(2026, 10, 21, 14, 30, 1, tzinfo=NY))
            http.post(f"{base}/paper/quote", headers=ADMIN, json={"symbol": "MNQ1!", "last": 20003.0})
            wait_for(lambda s: s["positions"] == [] and s["open_orders"] == [])

            # 8. Kill switch blocks trading until an explicit resume.
            clock.set(datetime(2026, 10, 22, 10, 0, tzinfo=NY))
            assert http.post(f"{base}/admin/kill", headers=ADMIN, json={"reason": "drill"}).json()["kill_switch"]
            r = http.post(hook, json=entry_payload(clock))
            assert r.status_code == 409 and "halted" in r.json()["reasons"][0]
            assert http.post(f"{base}/admin/resume", headers=ADMIN, json={"confirm": "RESUME"}).json()["resumed"]
            clock.advance(60)
            assert http.post(hook, json=entry_payload(clock)).json()["accepted"]

            # 9. Simulated broker outage: entries refused, monitor reconnects and reconciles.
            http.post(f"{base}/admin/flatten", headers=ADMIN, json={})
            http.post(f"{base}/paper/simulate", headers=ADMIN, json={"action": "disconnect"})
            clock.advance(60)
            r = http.post(hook, json=entry_payload(clock))
            assert "disconnected" in r.json()["reasons"][0]
            assert http.get(f"{base}/health").json()["broker_connected"] is False
            http.post(f"{base}/paper/simulate", headers=ADMIN, json={"action": "reconnect"})
            wait_for(lambda s: s["broker_connected"] and not s["needs_reconcile"])

            kinds = {a["kind"] for a in status()["recent_alerts"]} | {
                a["kind"] for a in http.get(f"{base}/admin/audit?limit=500", headers=ADMIN).json()}
            assert {"broker_disconnected", "broker_reconnected", "kill_switch", "session_flatten"} <= kinds
    finally:
        server.should_exit = True
        thread.join(timeout=5)
