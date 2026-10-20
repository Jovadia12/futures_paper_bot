"""Dashboard web service: authenticated, read-only, separate from the engine."""

from __future__ import annotations

import asyncio
import csv
import fnmatch
import hashlib
import hmac
import io
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import __version__
from ..clock import NY, Clock
from ..config import ConfigError, Settings
from ..contracts import ContractError, spec_for_contract_name
from ..logging_setup import install_redacting_record_factory, log, register_secret
from . import analytics as an
from .config import DashboardSettings
from .engine_client import EngineEndpointMissing, EngineUnavailable, ReadOnlyEngineClient

logger = logging.getLogger("v25bot.dashboard")
STATIC = Path(__file__).parent / "static"
COOKIE = "v25_session"
MUTATING_ROUTES = {"/api/login", "/api/logout"}  # the only non-GET routes; they touch dashboard sessions only

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
        "object-src 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _age(now: datetime, value) -> float | None:
    ts = an.parse_ts(value)
    return round((now - ts).total_seconds(), 1) if ts else None


def create_dashboard_app(settings: DashboardSettings | None = None, *, engine_settings: Settings | None = None,
                         transport: httpx.AsyncBaseTransport | None = None, clock: Clock | None = None) -> FastAPI:
    settings = settings or DashboardSettings.from_env()
    clock = clock or Clock()
    if engine_settings is None:
        try:
            engine_settings = Settings.from_env()
        except ConfigError as exc:
            log(logger, logging.WARNING, "engine limits unavailable to dashboard", error=str(exc))
            engine_settings = Settings()
    for secret in (settings.admin_token, settings.password):
        if secret is not None:
            register_secret(secret.get_secret_value())
    install_redacting_record_factory()

    client = ReadOnlyEngineClient(
        settings.engine_url, settings.admin_token.get_secret_value() if settings.admin_token else None,
        cache_seconds=settings.history_cache_seconds, transport=transport,
    )
    sessions: dict[str, float] = {}
    failures: list[float] = []
    observed = {"dashboard_started": clock.now(), "engine_up_since": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log(logger, logging.INFO, "dashboard starting", version=__version__, engine_url=settings.engine_url,
            password_configured=settings.password is not None)
        yield
        await client.close()

    app = FastAPI(title="V25 dashboard", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.state.client, app.state.sessions, app.state.settings = client, sessions, settings

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------ auth
    def session_valid(request: Request) -> bool:
        sid = request.cookies.get(COOKIE)
        expiry = sessions.get(sid or "")
        if expiry is None:
            return False
        if time.time() > expiry:
            sessions.pop(sid, None)
            return False
        return True

    def require_session(request: Request) -> None:
        if not session_valid(request):
            raise HTTPException(401, "login required")

    def origin_allowed(request: Request) -> bool:
        origin = request.headers.get("origin")
        if not origin:
            return True  # same-origin fetches from older browsers / curl
        host = urlsplit(origin).hostname or ""
        return any(fnmatch.fnmatch(host, pattern) for pattern in settings.allowed_hosts)

    def secure_request(request: Request) -> bool:
        host = (request.headers.get("host") or "").split(":")[0]
        return (request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
                or host not in ("127.0.0.1", "localhost"))

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request) -> JSONResponse:
        if not origin_allowed(request):
            raise HTTPException(403, "cross-origin login refused")
        if settings.password is None:
            raise HTTPException(503, "DASHBOARD_PASSWORD is not configured")
        now = time.time()
        failures[:] = [t for t in failures if now - t < settings.lockout_seconds]
        if len(failures) >= settings.max_login_failures:
            wait = int(settings.lockout_seconds - (now - failures[0])) + 1
            raise HTTPException(429, f"too many failed attempts; try again in {wait}s",
                                headers={"Retry-After": str(wait)})
        given = hashlib.sha256(body.password.encode()).digest()
        expected = hashlib.sha256(settings.password.get_secret_value().encode()).digest()
        if not hmac.compare_digest(given, expected):
            failures.append(now)
            log(logger, logging.WARNING, "dashboard login failed", failures=len(failures))
            await asyncio.sleep(0.5)
            raise HTTPException(401, "incorrect password")
        failures.clear()
        sid = secrets.token_urlsafe(32)
        max_age = int(settings.session_hours * 3600)
        sessions[sid] = now + max_age
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, sid, max_age=max_age, httponly=True, samesite="strict",
                            secure=secure_request(request), path="/")
        return response

    @app.post("/api/logout")
    async def logout(request: Request) -> JSONResponse:
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    # ------------------------------------------------------------------ pages
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        if not session_valid(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        if session_valid(request):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC / "login.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ------------------------------------------------------------------ data
    async def live() -> tuple[dict | None, dict | None, str | None]:
        """Engine liveness and live state. Never raises; returns the error text instead."""
        now = clock.now()
        try:
            health = await client.health()
        except EngineUnavailable as exc:
            observed["engine_up_since"] = None
            return None, None, str(exc)
        if observed["engine_up_since"] is None:
            observed["engine_up_since"] = now
        try:
            status = await client.status()
        except EngineUnavailable as exc:
            return health, None, str(exc)
        return health, status, None

    async def history() -> tuple[list, list, list, list]:
        return await asyncio.gather(client.fills(), client.orders(), client.audit(), client.events())

    def start_balance(status: dict | None, fills: list) -> tuple[float, float | None]:
        configured = engine_settings.account_start_balance
        account = (status or {}).get("account")
        if not account:
            return configured, None
        implied = round(account["balance"] - an.realized_net_all_fills(fills), 2)
        return implied, configured

    def positions_view(status: dict, trades: list[an.Trade], now: datetime) -> list[dict]:
        out = []
        open_trades = {t.symbol: t for t in trades if t.status == "open"}
        orders = status.get("open_orders") or []
        for p in status.get("positions") or []:
            symbol, qty, avg = p["symbol"], p["qty"], p["avg_price"]
            quote = (status.get("quotes") or {}).get(symbol)
            mark = None
            if quote:
                mark = (quote.get("bid") if qty > 0 else quote.get("ask")) or quote.get("last")
            try:
                pv = spec_for_contract_name(symbol).point_value
            except ContractError:
                pv = None
            unreal = round((mark - avg) * qty * pv, 2) if mark is not None and pv else None
            stops = [o for o in orders if o["symbol"] == symbol and o["role"] == "stop_loss"]
            targets = [o for o in orders if o["symbol"] == symbol and o["role"] == "take_profit"]
            trade = open_trades.get(symbol)
            opened = an.parse_ts(trade.entry_time) if trade else None
            out.append({
                "symbol": symbol, "direction": "long" if qty > 0 else "short", "contracts": abs(qty),
                "avg_price": avg, "current_price": mark,
                "price_ts": quote.get("ts") if quote else None,
                "price_age_seconds": _age(now, quote.get("ts")) if quote else None,
                "price_source": quote.get("source") if quote else None,
                "unrealized_pnl": unreal,
                "stop_price": stops[0].get("stop_price") if stops else None,
                "stop_qty": sum(o["qty"] - o.get("filled_qty", 0) for o in stops) if stops else 0,
                "target_price": targets[0].get("limit_price") if targets else None,
                "opened_at": _iso(opened),
                "duration_seconds": round((now - opened).total_seconds()) if opened else None,
            })
        return out

    def health_view(health: dict | None, status: dict | None, error: str | None, events: list,
                    audit: list, now: datetime, day_pnl: float | None) -> dict:
        es = engine_settings
        online = health is not None
        tv_seen = (status or {}).get("tv_last_seen")
        tv_age = _age(now, tv_seen)
        market_open = an.cme_equity_futures_open(now)
        if tv_age is not None and tv_age <= settings.tv_live_seconds:
            tv_state = "receiving"
        elif not market_open:
            tv_state = "market_closed"
        elif tv_seen is None:
            tv_state = "never"
        else:
            tv_state = "silent"
        signals = [e for e in events if e.get("kind") in ("entry", "exit", "flatten")]
        last_signal = max(signals, key=lambda e: e.get("received_at") or "", default=None)
        quotes = (status or {}).get("quotes") or {}
        newest_quote = max(quotes.items(), key=lambda kv: kv[1].get("ts") or "", default=None)
        halt = (status or {}).get("halt")
        account = (status or {}).get("account") or {}
        floor = (status or {}).get("drawdown_floor")
        equity = account.get("equity")
        room = round(equity - floor, 2) if equity is not None and floor is not None else None
        errors = [a for a in audit if (a.get("level") or "").upper() in ("ERROR", "CRITICAL")][:15]
        checks = {
            "engine_online": online,
            "monitor_ok": bool(health and health.get("monitor_ok")),
            "broker_connected": bool(status and status.get("broker_connected")),
            "reconciled": bool(status) and not status.get("needs_reconcile"),
            "not_halted": bool(status) and not halt,
        }
        return {
            "overall": "healthy" if all(checks.values()) else ("offline" if not online else "degraded"),
            "checks": checks,
            "error": error,
            "engine": {
                "online": online, "version": (health or {}).get("version"),
                "monitor_ok": checks["monitor_ok"],
                "monitor_last_cycle": _iso(datetime.fromtimestamp(status["monitor"]["last_cycle"], tz=timezone.utc))
                if status and (status.get("monitor") or {}).get("last_cycle") else None,
                "monitor_last_error": ((status or {}).get("monitor") or {}).get("last_error"),
                "observed_up_since": _iso(observed["engine_up_since"]),
                "observed_uptime_seconds": round((now - observed["engine_up_since"]).total_seconds())
                if observed["engine_up_since"] else None,
                "dashboard_started": _iso(observed["dashboard_started"]),
            },
            "docker": {
                "dashboard_in_container": settings.in_docker,
                "engine_reachable": online,
                "engine_url": settings.engine_url,
            },
            "tradingview": {
                "state": tv_state, "last_seen": tv_seen, "age_seconds": tv_age,
                "live_threshold_seconds": settings.tv_live_seconds, "market_open": market_open,
                "last_signal": {"ts": last_signal.get("received_at"), "kind": last_signal.get("kind"),
                                "status": last_signal.get("status"), "event_id": last_signal.get("event_id"),
                                "age_seconds": _age(now, last_signal.get("received_at"))} if last_signal else None,
            },
            "market_data": {
                "symbol": newest_quote[0], "ts": newest_quote[1].get("ts"), "source": newest_quote[1].get("source"),
                "last": newest_quote[1].get("last"), "age_seconds": _age(now, newest_quote[1].get("ts")),
            } if newest_quote else None,
            "reconciliation": {
                "last": (status or {}).get("last_reconcile"),
                "age_seconds": _age(now, (status or {}).get("last_reconcile")),
                "needs_reconcile": (status or {}).get("needs_reconcile"),
            },
            "mode": {
                "mode": (status or health or {}).get("mode"), "broker": (status or health or {}).get("broker"),
                "paper_only": (status or health or {}).get("paper_only"),
                "live_enabled": (status or health or {}).get("live_enabled"),
            },
            "risk": {
                "halt": halt,
                "trades_today": (status or {}).get("trades_today"),
                "max_trades_per_day": es.max_trades_per_day,
                "day_pnl": day_pnl,
                "daily_loss_limit": es.daily_loss_limit_usd,
                "drawdown_floor": floor,
                "drawdown_room": room,
                "drawdown_buffer": es.drawdown_safety_buffer_usd,
                "max_contracts": es.max_contracts,
                "max_trade_risk": es.max_trade_risk_usd,
                "entry_window": f"{es.entry_window_start:%H:%M}–{es.entry_window_end:%H:%M} ET",
                "flatten_time": f"{es.flatten_time:%H:%M} ET",
            },
            "errors": errors,
        }

    async def build_overview(now: datetime) -> dict:
        health, status, error = await live()
        base = {"now": _iso(now), "refresh_seconds": settings.refresh_seconds}
        if status is None:
            return base | {"health": health_view(health, None, error, [], [], now, None), "account": None,
                           "metrics": None, "positions": [], "orders": [], "recent": []}
        try:
            fills, orders, audit, events = await history()
        except EngineUnavailable as exc:
            return base | {"health": health_view(health, status, str(exc), [], [], now, None), "account": None,
                           "metrics": None, "positions": [], "orders": [], "recent": []}
        trades = an.build_trades(fills, orders, audit)
        summary = an.summarize(trades)
        account = status.get("account")
        start, configured = start_balance(status, fills)
        equity = account["equity"] if account else None
        dd = an.drawdown_stats(an.equity_points(trades, start), start, equity)
        open_trades = [t for t in trades if t.status == "open"]
        day_pnl = account["realized_pnl_today"] if account else None
        metrics = {
            "balance": account["balance"] if account else None,
            "equity": equity,
            "starting_balance": start,
            "configured_starting_balance": configured,
            "total_realized_pnl": summary["net_pnl"],
            "today_realized_pnl": day_pnl,
            "unrealized_pnl": account["open_pnl"] if account else None,
            "total_return_pct": round((equity - start) / start * 100, 3) if equity is not None and start else None,
            "trades_today": status.get("trades_today"),
            "total_trades": summary["total_trades"],
            "open_trades": len(open_trades),
            **{k: summary[k] for k in ("win_rate", "profit_factor", "profit_factor_infinite", "avg_win", "avg_loss",
                                       "largest_win", "largest_loss", "wins", "losses", "commissions")},
            **dd,
            "open_positions": len(status.get("positions") or []),
            "pending_orders": len(status.get("open_orders") or []),
        }
        return base | {
            "health": health_view(health, status, error, events, audit, now, day_pnl),
            "account": account,
            "metrics": metrics,
            "positions": positions_view(status, trades, now),
            "orders": status.get("open_orders") or [],
            "recent": [i for i in an.activity(events, fills, audit, limit=60) if i["category"] != "system"][:8],
            "equity_spark": an.equity_points(trades, start)[-60:],
        }

    @app.get("/api/overview")
    async def overview(request: Request) -> dict:
        require_session(request)
        return await build_overview(clock.now())

    async def trades_or_503() -> tuple[list[an.Trade], dict | None, list]:
        try:
            fills, orders, audit, _ = await history()
        except EngineUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return an.build_trades(fills, orders, audit), None, fills

    def check_range(range_key: str) -> str:
        if range_key not in an.RANGES:
            raise HTTPException(422, f"range must be one of {an.RANGES}")
        return range_key

    @app.get("/api/trades")
    async def trades(request: Request, range: str = "all", q: str = "", side: str = "", reason: str = "",
                     outcome: str = "") -> dict:
        require_session(request)
        rows, _, _ = await trades_or_503()
        selected = an.filter_trades(rows, range_key=check_range(range), now=clock.now(), q=q[:80], side=side,
                                    reason=reason, outcome=outcome)
        selected.sort(key=lambda t: t.exit_time or t.entry_time, reverse=True)
        return {"trades": [t.to_dict() for t in selected], "summary": an.summarize(selected),
                "reasons": sorted({(t.exit_reason or "").split(":")[0] for t in rows if t.exit_reason})}

    @app.get("/api/trades.csv")
    async def trades_csv(request: Request, range: str = "all", q: str = "", side: str = "", reason: str = "",
                         outcome: str = "") -> Response:
        require_session(request)
        rows, _, _ = await trades_or_503()
        selected = an.filter_trades(rows, range_key=check_range(range), now=clock.now(), q=q[:80], side=side,
                                    reason=reason, outcome=outcome)
        selected.sort(key=lambda t: t.exit_time or t.entry_time)
        return Response(to_csv(selected), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="v25_trades_{range}_{clock.now().astimezone(NY):%Y%m%d}.csv"'})

    @app.get("/api/analytics")
    async def analytics_view(request: Request, range: str = "all") -> dict:
        require_session(request)
        _, status, _ = await live()
        rows, _, fills = await trades_or_503()
        start, _ = start_balance(status, fills)
        equity = ((status or {}).get("account") or {}).get("equity")
        return an.analytics(rows, check_range(range), clock.now(), start, equity)

    @app.get("/api/activity")
    async def activity_view(request: Request, limit: int = 300) -> dict:
        require_session(request)
        try:
            fills, _, audit, events = await history()
        except EngineUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return {"items": an.activity(events, fills, audit, limit=max(1, min(limit, 1000)))}

    @app.get("/api/signals")
    async def signals_view(request: Request, range: str = "today") -> dict:
        require_session(request)
        now = clock.now()
        since = an.range_start(check_range(range), now)
        try:
            diag = await client.diagnostics(int(since.timestamp() * 1000) if since else None)
        except EngineEndpointMissing:
            return {"available": False, "reason": "This engine version has no strategy diagnostics (V26 engines only)."}
        except EngineUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return an.signals(diag, now)

    return app


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_text(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text  # spreadsheet formula injection guard


def _et(value) -> str:
    ts = an.parse_ts(value)
    return ts.astimezone(NY).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def to_csv(trades: list[an.Trade]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["entry_time_et", "exit_time_et", "symbol", "side", "qty", "entry_price", "exit_price",
                "stop_loss", "take_profit", "gross_pnl", "commission", "net_pnl", "exit_reason",
                "duration_seconds", "status", "trade_id"])
    for t in trades:
        w.writerow([_et(t.entry_time), _et(t.exit_time), _csv_text(t.symbol), t.side, t.qty, t.entry_price,
                    t.exit_price, t.stop_price, t.target_price, t.gross_pnl, t.commission, t.net_pnl,
                    _csv_text(t.exit_reason), t.duration_seconds, t.status, _csv_text(t.id)])
    return buf.getvalue()
