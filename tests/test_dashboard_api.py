"""Dashboard service against the real engine app: accuracy, auth, read-only, security."""

import csv
import io
import re
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr

from conftest import ADMIN, ADMIN_TOKEN, CONTRACT, T0, WEBHOOK_TOKEN, bar_payload, entry_payload, make_settings
from v25bot.api import create_app
from v25bot.clock import NY, FakeClock
from v25bot.dashboard.app import create_dashboard_app, to_csv
from v25bot.dashboard.config import DashboardSettings
from v25bot.dashboard.engine_client import READ_ONLY_PATHS, ReadOnlyEngineClient
from v25bot.dashboard import analytics as an

PASSWORD = "correct-horse-battery"
HOOK = f"/webhook/tradingview/{WEBHOOK_TOKEN}"


class Stack:
    """Real engine app + dashboard app, joined by an in-process transport, on one fake clock."""

    def __init__(self, tmp_path, engine_up=True, engine_kw=None, **dash_kw):
        self.clock = FakeClock(T0)
        self.settings = make_settings(tmp_path, **(engine_kw or {}))
        self.missing_paths: set[str] = set()  # simulate an older engine without these endpoints
        self.engine = TestClient(create_app(self.settings, clock=self.clock, start_monitor=False))
        self.engine.__enter__()
        self.engine_up = engine_up
        self.engine_calls: list[tuple[str, str]] = []

        def forward(request: httpx.Request) -> httpx.Response:
            self.engine_calls.append((request.method, request.url.path))
            if not self.engine_up:
                raise httpx.ConnectError("engine down")
            if request.url.path in self.missing_paths:
                return httpx.Response(404, json={"detail": "Not Found"})
            r = self.engine.request(request.method, request.url.path, params=dict(request.url.params),
                                    headers={k: v for k, v in request.headers.items() if k.lower() == "authorization"})
            return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

        ds = DashboardSettings(engine_url="http://engine", admin_token=SecretStr(ADMIN_TOKEN),
                               password=SecretStr(PASSWORD), history_cache_seconds=0, **dash_kw)
        self.app = create_dashboard_app(ds, engine_settings=self.settings, transport=httpx.MockTransport(forward),
                                        clock=self.clock)
        self.dash = TestClient(self.app, base_url="http://127.0.0.1:8080")
        self.dash.__enter__()

    def login(self, client=None):
        c = client or self.dash
        r = c.post("/api/login", json={"password": PASSWORD})
        assert r.status_code == 200, r.text
        return r

    def trade(self, **kw):
        r = self.engine.post(HOOK, json=entry_payload(self.clock, **kw))
        assert r.status_code == 200, r.text
        return r.json()

    def bar(self, o, hi, lo, c):
        self.clock.advance(60)
        self.engine.post(HOOK, json=bar_payload(self.clock, o, hi, lo, c))

    def close(self):
        self.dash.__exit__(None, None, None)
        self.engine.__exit__(None, None, None)


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.close()


# ----------------------------------------------------------------------------- auth
def test_pages_and_api_require_login(stack):
    r = stack.dash.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    for path in ("/api/overview", "/api/trades", "/api/trades.csv", "/api/analytics", "/api/activity"):
        assert stack.dash.get(path).status_code == 401, path
    assert stack.dash.get("/login").status_code == 200
    assert stack.dash.get("/static/app.js").status_code == 200  # code only, no data
    assert stack.engine_calls == []  # nothing reached the engine before login


def test_login_cookie_flags_and_logout(stack):
    r = stack.login()
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" not in cookie  # plain http on localhost
    assert stack.dash.get("/api/overview").status_code == 200
    assert stack.dash.get("/", follow_redirects=False).status_code == 200
    stack.dash.post("/api/logout")
    assert stack.dash.get("/api/overview").status_code == 401


def test_cookie_is_secure_over_tailscale_https(stack):
    remote = TestClient(stack.app, base_url="https://v25-pc.tailnet-abc.ts.net")
    with remote:
        cookie = stack.login(remote).headers["set-cookie"]
        assert "Secure" in cookie
        assert remote.get("/api/overview").status_code == 200


def test_wrong_password_and_lockout(stack):
    for _ in range(5):
        assert stack.dash.post("/api/login", json={"password": "nope-nope-nope"}).status_code == 401
    r = stack.dash.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_login_disabled_without_password(tmp_path):
    ds = DashboardSettings(admin_token=SecretStr(ADMIN_TOKEN), password=None)
    with TestClient(create_dashboard_app(ds, engine_settings=make_settings(tmp_path)), base_url="http://127.0.0.1") as c:
        assert c.post("/api/login", json={"password": "anything-at-all"}).status_code == 503


def test_short_password_rejected():
    with pytest.raises(Exception):
        DashboardSettings(password=SecretStr("short")).validate()


def test_unknown_host_rejected_dns_rebinding_guard(stack):
    evil = TestClient(stack.app, base_url="http://attacker.example")
    assert evil.get("/login").status_code == 400
    assert evil.post("/api/login", json={"password": PASSWORD}).status_code == 400


def test_cross_origin_login_refused(stack):
    r = stack.dash.post("/api/login", json={"password": PASSWORD}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = stack.dash.post("/api/login", json={"password": PASSWORD}, headers={"Origin": "https://pc.tail1.ts.net"})
    assert ok.status_code == 200


def test_security_headers(stack):
    r = stack.dash.get("/login")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe" not in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    stack.login()
    assert stack.dash.get("/api/overview").headers["cache-control"] == "no-store"


# ----------------------------------------------------------------------------- read-only
def test_only_session_routes_accept_non_get(stack):
    mutating = {r.path for r in stack.app.routes if isinstance(r, APIRoute) and set(r.methods) - {"GET", "HEAD"}}
    assert mutating == {"/api/login", "/api/logout"}


def test_engine_client_is_get_only_and_allowlisted():
    client = ReadOnlyEngineClient("http://engine", "x" * 32)
    assert not hasattr(client, "post")
    assert {"/admin/kill", "/admin/resume", "/admin/flatten", "/admin/reconcile", "/paper/quote"}.isdisjoint(READ_ONLY_PATHS)
    import asyncio

    with pytest.raises(PermissionError):
        asyncio.run(client.get("/admin/kill"))


def test_dashboard_only_ever_sends_get_to_engine(stack):
    stack.trade()
    stack.login()
    for path in ("/api/overview", "/api/trades", "/api/analytics?range=all", "/api/activity", "/api/trades.csv"):
        assert stack.dash.get(path).status_code == 200
    assert stack.engine_calls and {m for m, _ in stack.engine_calls} == {"GET"}
    assert {p for _, p in stack.engine_calls} <= READ_ONLY_PATHS


def test_static_assets_have_no_inline_scripts_or_styles():
    static = Path(__file__).resolve().parents[1] / "v25bot" / "dashboard" / "static"
    for page in ("index.html", "login.html"):
        text = (static / page).read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), page  # every script is external
        assert "style=" not in text and "<style" not in text and "onclick" not in text.lower()
    assert "style=" not in (static / "app.js").read_text()
    for lib in ("preact.module.js", "hooks.module.js", "htm.module.js", "chart.umd.min.js"):
        assert (static / "vendor" / lib).stat().st_size > 500
    assert "http" not in (static / "index.html").read_text().split("<body>")[1]  # no CDN at runtime


# ----------------------------------------------------------------------------- accuracy
def test_empty_account_shows_no_fabricated_metrics(stack):
    stack.login()
    m = stack.dash.get("/api/overview").json()["metrics"]
    assert m["balance"] == 50_000 and m["equity"] == 50_000 and m["total_trades"] == 0
    for key in ("win_rate", "profit_factor", "avg_win", "avg_loss", "largest_win", "largest_loss"):
        assert m[key] is None, key
    assert m["total_realized_pnl"] == 0 and m["max_drawdown"] == 0


def test_overview_matches_engine_state_exactly(stack):
    stack.trade()                                   # long 1 @ 20000.5, stop 19980, target 20060
    stack.bar(20001.0, 20061.0, 19995.0, 20050.0)   # target hit: +59.5 pts
    stack.clock.advance(120)
    stack.trade(side="sell", price=20050.0)         # short 1 open
    stack.bar(20049.0, 20052.0, 20040.0, 20044.0)
    stack.login()
    ov = stack.dash.get("/api/overview").json()
    status = stack.engine.get("/status", headers=ADMIN).json()
    m = ov["metrics"]
    assert m["balance"] == status["account"]["balance"]
    assert m["equity"] == status["account"]["equity"]
    assert m["unrealized_pnl"] == status["account"]["open_pnl"]
    assert m["today_realized_pnl"] == status["account"]["realized_pnl_today"]
    assert m["trades_today"] == status["trades_today"] == 2
    assert m["total_trades"] == 1 and m["open_trades"] == 1 and m["win_rate"] == 100.0
    assert m["total_realized_pnl"] == pytest.approx(59.5 * 2 - 3.0)
    assert m["starting_balance"] == 50_000 and m["pending_orders"] == len(status["open_orders"]) == 2
    [pos] = ov["positions"]
    assert pos["direction"] == "short" and pos["contracts"] == 1 and pos["avg_price"] == 20049.5
    assert pos["stop_price"] == 20070.0 and pos["target_price"] == 19990.0 and pos["stop_qty"] == 1
    assert pos["unrealized_pnl"] == status["account"]["open_pnl"]  # same mark as the broker
    assert pos["duration_seconds"] == 60


def test_trades_endpoint_filters_and_csv(stack):
    stack.trade()
    stack.bar(20001.0, 20061.0, 19995.0, 20050.0)
    stack.clock.advance(120)
    stack.trade(side="sell", price=20050.0)
    stack.bar(20055.0, 20075.0, 20049.0, 20072.0)   # short stopped out
    stack.login()
    allt = stack.dash.get("/api/trades").json()
    assert [t["exit_reason"] for t in allt["trades"]] == ["Stop loss", "Take profit"]  # newest first
    assert allt["summary"]["wins"] == 1 and allt["summary"]["losses"] == 1
    assert [t["side"] for t in stack.dash.get("/api/trades?side=short").json()["trades"]] == ["short"]
    assert len(stack.dash.get("/api/trades?outcome=win").json()["trades"]) == 1
    assert len(stack.dash.get("/api/trades?q=take").json()["trades"]) == 1
    assert stack.dash.get("/api/trades?range=bogus").status_code == 422
    r = stack.dash.get("/api/trades.csv?range=all")
    assert r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 2 and rows[0]["exit_reason"] == "Take profit" and rows[0]["entry_time_et"].startswith("2026-10-20 10:")


def test_csv_formula_injection_guard():
    t = an.Trade(id="=HYPERLINK(1)", symbol="MNQZ6", side="long", qty=1, entry_time="2026-10-20T14:00:00+00:00",
                 entry_price=1.0, entry_order_id="e", exit_reason="+cmd", net_pnl=-5.0, status="closed")
    row = list(csv.reader(io.StringIO(to_csv([t]))))[1]
    assert row[-1] == "'=HYPERLINK(1)" and row[12] == "'+cmd" and row[11] == "-5.0"  # numbers untouched


def test_analytics_endpoint(stack):
    stack.trade()
    stack.bar(20001.0, 20061.0, 19995.0, 20050.0)
    stack.login()
    a = stack.dash.get("/api/analytics?range=today").json()
    assert a["summary"]["total_trades"] == 1
    assert a["equity"][0]["v"] == 50_000 and a["equity"][-1]["v"] == pytest.approx(50_116.0)
    assert len(a["daily"]) == 1 and a["daily"][0]["pnl"] == pytest.approx(116.0)
    assert [d["day"] for d in a["weekday"]] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert a["by_symbol"][0]["symbol"] == CONTRACT


def test_activity_endpoint_shows_signals_fills_and_rejections(stack):
    stack.trade()
    stack.engine.post("/admin/flatten", headers=ADMIN, json={})
    stack.clock.advance(120)  # a new bar -> a new event id
    r = stack.engine.post(HOOK, json=entry_payload(stack.clock, qty=5))
    assert r.status_code == 409 and "MAX_CONTRACTS" in r.json()["reasons"][0]
    stack.login()
    items = stack.dash.get("/api/activity").json()["items"]
    cats = [i["category"] for i in items]
    assert "signal" in cats and "entry" in cats and "risk" in cats


# ----------------------------------------------------------------------------- health
def test_health_distinguishes_engine_from_tradingview(stack):
    stack.login()
    h = stack.dash.get("/api/overview").json()["health"]
    assert h["engine"]["online"] and h["checks"]["broker_connected"]
    assert h["tradingview"]["state"] == "never"  # engine healthy, but no TradingView data yet
    stack.bar(20000.0, 20001.0, 19999.0, 20000.0)
    assert stack.dash.get("/api/overview").json()["health"]["tradingview"]["state"] == "receiving"
    stack.clock.advance(600)  # data is now 10 minutes old during market hours
    h = stack.dash.get("/api/overview").json()["health"]
    assert h["tradingview"]["state"] == "silent" and h["tradingview"]["last_seen"] is not None


def test_tradingview_quiet_when_market_closed_is_not_an_alarm(stack):
    stack.bar(20000.0, 20001.0, 19999.0, 20000.0)
    stack.clock.set(datetime(2026, 10, 24, 12, 0, tzinfo=NY))  # Saturday
    stack.login()
    assert stack.dash.get("/api/overview").json()["health"]["tradingview"]["state"] == "market_closed"


def test_health_reports_mode_risk_and_halt(stack):
    stack.engine.post("/admin/kill", headers=ADMIN, json={"reason": "drill"})
    stack.login()
    h = stack.dash.get("/api/overview").json()["health"]
    assert h["mode"] == {"mode": "PAPER_ONLY", "broker": "paper", "paper_only": True, "live_enabled": False}
    assert h["risk"]["halt"]["scope"] == "kill_switch" and h["overall"] == "degraded"
    assert h["risk"]["max_trades_per_day"] == 2 and h["risk"]["daily_loss_limit"] == 450


def test_engine_down_shows_offline_not_stale_numbers(tmp_path):
    s = Stack(tmp_path)
    try:
        s.trade()
        s.login()
        assert s.dash.get("/api/overview").json()["metrics"]["equity"] is not None
        s.engine_up = False
        ov = s.dash.get("/api/overview").json()
        assert ov["metrics"] is None and ov["positions"] == []
        assert ov["health"]["overall"] == "offline" and not ov["health"]["engine"]["online"]
        assert ov["health"]["engine"]["observed_uptime_seconds"] is None
        assert s.dash.get("/api/trades").status_code == 503
        s.engine_up = True
        assert s.dash.get("/api/overview").json()["metrics"]["equity"] is not None
    finally:
        s.close()


def test_wrong_admin_token_reported(tmp_path):
    s = Stack(tmp_path)
    try:
        s.app.state.client._token = "x" * 32
        s.login()
        ov = s.dash.get("/api/overview").json()
        assert ov["metrics"] is None and "401" in ov["health"]["error"]
        assert ov["health"]["engine"]["online"]  # /health is public: engine itself is up
    finally:
        s.close()


def test_every_referenced_static_asset_exists():
    import json as _json

    static = Path(__file__).resolve().parents[1] / "v25bot" / "dashboard" / "static"
    refs = set()
    for page in ("index.html", "login.html"):
        refs |= set(re.findall(r'(?:href|src)="/static/([^"]+)"', (static / page).read_text()))
    refs |= {i["src"].removeprefix("/static/") for i in _json.loads((static / "manifest.json").read_text())["icons"]}
    assert refs and all((static / r).is_file() for r in refs), sorted(r for r in refs if not (static / r).is_file())


# ----------------------------------------------------------------------------- V26 signals
def _v26_stack(tmp_path):
    return Stack(tmp_path, engine_kw={"allowed_strategy_versions": ("V26-webhook.1",),
                                      "allowed_bot_ids": ("V26-MNQ-1",)})


def test_signals_page_shows_pipeline_state_and_exact_reasons(tmp_path):
    from v26_payloads import diag_item, v26_bar, v26_entry

    s = _v26_stack(tmp_path)
    try:
        ok = v26_entry(s.clock)
        assert s.engine.post(HOOK, json=ok).json()["accepted"]
        items = [diag_item("htf_fvg", 1, 60, 19970.0, 19990.0, 1), diag_item("tap", 1, 60, 19970.0, 19990.0, 1),
                 diag_item("armed", 1, 60, 19970.0, 19990.0, 1), diag_item("opp_selected", 1, 1, 20000.0, 20005.0, 1),
                 diag_item("inversion", 1, 60, 20000.0, 20005.0, 1), diag_item("entry", 1, 60, 19994.0, 20040.0, 1)]
        s.engine.post(HOOK, json=v26_bar(s.clock, 20001.0, 20007.0, 20000.0, 20006.0, items=items,
                                         bar_time=ok["bar_time"], long_armed=True))
        s.engine.post("/admin/flatten", headers=ADMIN, json={})
        s.clock.advance(120)
        bad = v26_entry(s.clock, target_index=0)  # 1.08R
        assert s.engine.post(HOOK, json=bad).status_code == 422
        s.login()
        r = s.dash.get("/api/signals?range=all").json()
        funnel = {x["key"]: x["count"] for x in r["funnel"]}
        assert funnel == {"htf_fvg": 1, "tap": 1, "armed": 1, "opp_selected": 1, "inversion": 1, "entry": 1,
                          "alert_received": 2, "order_accepted": 1, "order_rejected": 1}
        assert any("below the 2R minimum" in x["reason"] and x["kind"] == "order_rejected" for x in r["reasons"])
        state = r["state"][0]
        assert state["bot_id"] == "V26-MNQ-1" and state["long"]["armed"] and state["long"]["opp_hi"] == 20005.0
        assert {z["tf"] for z in state["zones_tf"]} == {5, 10, 15, 30, 45, 60, 120, 240}
        assert {i["source"] for i in r["timeline"]} == {"engine", "TradingView"}
        assert {p for _, p in s.engine_calls} <= READ_ONLY_PATHS
        assert s.dash.get("/api/signals?range=bogus").status_code == 422
    finally:
        s.close()


def test_signals_page_on_engine_without_diagnostics(stack):
    stack.missing_paths.add("/admin/diagnostics")
    stack.login()
    r = stack.dash.get("/api/signals").json()
    assert r["available"] is False and "V26" in r["reason"]
    assert stack.dash.get("/api/overview").status_code == 200  # rest of the dashboard unaffected


def test_signals_require_login_and_report_engine_down(tmp_path):
    s = Stack(tmp_path)
    try:
        assert s.dash.get("/api/signals").status_code == 401
        s.login()
        s.engine_up = False
        assert s.dash.get("/api/signals").status_code == 503
    finally:
        s.close()
