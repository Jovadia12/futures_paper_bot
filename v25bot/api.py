"""FastAPI application: TradingView webhook, admin API, legacy-compatible routes."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from . import __version__, contracts
from .alerts import AlertManager
from .brokers.base import Broker, OrderRole
from .clock import Clock
from .config import Settings
from .engine import ExecutionEngine
from .logging_setup import install_redacting_record_factory, log, redact_obj, register_secret
from .market_data import Quote, QuoteBook
from .monitor import Monitor
from .schemas import BarEvent, EntryEvent, ExitEvent, FlattenEvent, HeartbeatEvent, LegacySignal, V1Adapter
from .store import Store

logger = logging.getLogger("v25bot.api")


def build_broker(settings: Settings, clock: Clock, quotes: QuoteBook, store: Store) -> Broker:
    if settings.broker == "paper":
        from .brokers.paper import PaperBroker

        return PaperBroker(settings, clock, quotes, store)
    from .brokers.tradovate import TradovateBroker  # raises TradingDisabled unless every gate passes

    return TradovateBroker(settings, clock)


def _in_networks(ip: str, networks: tuple[str, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in networks:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


class KillBody(BaseModel):
    reason: str = Field(default="manual kill switch", max_length=200)


class ResumeBody(BaseModel):
    confirm: Literal["RESUME"]
    actor: str = Field(default="admin", max_length=60)


class FlattenBody(BaseModel):
    reason: str = Field(default="manual emergency flatten", max_length=200)


class QuoteBody(BaseModel):
    symbol: str = Field(min_length=2, max_length=40)
    last: float = Field(gt=0)
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)


class SimulateBody(BaseModel):
    action: Literal["disconnect", "reconnect", "reject_next"]
    role: Literal["entry", "stop_loss", "take_profit", "flatten", "any"] = "any"
    reason: str = Field(default="SimulatedReject", max_length=80)


def create_app(settings: Settings | None = None, *, clock: Clock | None = None,
               broker: Broker | None = None, start_monitor: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate_safety()
    clock = clock or Clock()
    for secret in settings.secret_values():
        register_secret(secret)
    install_redacting_record_factory()
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain full URLs

    store = Store(settings.db_path)
    quotes = QuoteBook()
    alerts = AlertManager(settings, store)
    broker = broker or build_broker(settings, clock, quotes, store)
    engine = ExecutionEngine(settings, store, broker, quotes, clock, alerts)
    monitor = Monitor(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log(logger, logging.INFO, "starting V25 execution engine", version=__version__, mode=settings.mode,
            broker=broker.name, paper_only=settings.paper_only, live_enabled=settings.live_allowed)
        if settings.webhook_token is None:
            alerts.emit("error", "config", "WEBHOOK_TOKEN not set: webhook endpoints are disabled")
        if settings.admin_token is None:
            alerts.emit("warning", "config", "ADMIN_TOKEN not set: admin endpoints are disabled")
        if not settings.enforce_ip_allowlist:
            alerts.emit("warning", "config", "ENFORCE_IP_ALLOWLIST=false: enable it for internet-facing deployments")
        try:
            await broker.connect()
            await engine.reconcile()
        except Exception as exc:
            alerts.emit("error", "broker_connect_failed", f"broker connect failed at startup: {exc}")
        if start_monitor:
            monitor.start()
        yield
        if start_monitor:
            await monitor.stop()
        await broker.close()
        store.close()

    app = FastAPI(title="V25 execution engine", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if settings.paper_only else None, redoc_url=None)
    app.state.settings, app.state.engine, app.state.store = settings, engine, store
    app.state.broker, app.state.monitor, app.state.alerts, app.state.quotes = broker, monitor, alerts, quotes

    # ------------------------------------------------------------------ helpers
    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else ""
        if settings.trust_proxy_headers and _in_networks(peer, settings.trusted_proxies):
            chain = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
            for hop in reversed(chain):
                if not _in_networks(hop, settings.trusted_proxies):
                    return hop
        return peer

    def check_webhook_token(token: str) -> None:
        expected = settings.webhook_token
        if expected is None:
            raise HTTPException(503, "webhook disabled: WEBHOOK_TOKEN not configured")
        if not hmac.compare_digest(token.encode(), expected.get_secret_value().encode()):
            raise HTTPException(403, "Forbidden")

    def require_admin(authorization: str = Header(default="")) -> None:
        expected = settings.admin_token
        if expected is None:
            raise HTTPException(503, "admin API disabled: ADMIN_TOKEN not configured")
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value.encode(), expected.get_secret_value().encode()):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    async def read_body(request: Request) -> bytes:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            raise HTTPException(413, "payload too large")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > settings.max_body_bytes:
                raise HTTPException(413, "payload too large")
        return body

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        log(logger, logging.INFO, "request", method=request.method,
            route=getattr(route, "path", "unmatched"), status=response.status_code,
            ms=round((time.perf_counter() - started) * 1000, 1), client=client_ip(request))
        return response

    # ------------------------------------------------------------------ webhook
    async def webhook(token: str, request: Request) -> JSONResponse:
        check_webhook_token(token)
        ip = client_ip(request)
        if settings.enforce_ip_allowlist and not _in_networks(ip, settings.ip_allowlist):
            alerts.emit("warning", "webhook_ip_blocked", f"webhook from non-allowlisted IP {ip}")
            raise HTTPException(403, "Forbidden")
        raw = await read_body(request)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "body is not valid JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(422, "body must be a JSON object")

        try:
            if "schema" in data:
                evt = V1Adapter.validate_python(data)
            elif settings.allow_legacy_schema:
                evt = LegacySignal.model_validate(data).to_entry()
            else:
                raise HTTPException(422, "missing 'schema' (legacy payloads disabled)")
        except ValidationError as exc:
            errors = [{"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"]} for e in exc.errors()[:10]]
            raise HTTPException(422, {"error": "schema validation failed", "errors": errors}) from None

        if settings.webhook_passphrase is not None:
            if not hmac.compare_digest(evt.passphrase.encode(), settings.webhook_passphrase.get_secret_value().encode()):
                raise HTTPException(403, "Forbidden")

        now = clock.now()
        age = (now - datetime.fromtimestamp(evt.sent_at / 1000, tz=timezone.utc)).total_seconds()
        if age > settings.max_signal_age_seconds:
            raise HTTPException(422, f"stale signal: {age:.0f}s old (max {settings.max_signal_age_seconds:.0f}s)")
        if -age > settings.max_future_skew_seconds:
            raise HTTPException(422, f"signal timestamp {-age:.0f}s in the future")

        if isinstance(evt, BarEvent):
            code, result = await engine.handle_bar(evt)
            return JSONResponse(result, status_code=code)
        if isinstance(evt, HeartbeatEvent):
            code, result = await engine.handle_heartbeat(evt)
            return JSONResponse(result, status_code=code)

        sha = hashlib.sha256(raw).hexdigest()
        if not store.claim_event(evt.event_id, evt.event, sha, redact_obj(data), ip):
            existing = store.get_event(evt.event_id)
            if existing["payload_sha256"] != sha:
                alerts.emit("warning", "event_id_conflict", f"event_id {evt.event_id} reused with a different payload")
                return JSONResponse({"duplicate": True, "accepted": False, "status": "conflict",
                                     "reason": "event_id reused with a different payload"}, status_code=409)
            if existing["status"] == "processing":
                return JSONResponse({"duplicate": True, "accepted": False, "status": "in_progress"}, status_code=409)
            return JSONResponse({"duplicate": True, "result": json.loads(existing["result_json"])}, status_code=200)

        try:
            if isinstance(evt, EntryEvent):
                code, result = await engine.handle_entry(evt)
            elif isinstance(evt, ExitEvent):
                code, result = await engine.handle_exit(evt)
            elif isinstance(evt, FlattenEvent):
                code, result = await engine.handle_flatten(evt)
            else:  # pragma: no cover - discriminated union is exhaustive
                code, result = 422, {"status": "rejected", "reasons": ["unsupported event"]}
        except Exception as exc:
            store.finish_event(evt.event_id, "error", {"status": "error", "error": type(exc).__name__})
            alerts.emit("critical", "webhook_handler_error", f"handler error for {evt.event_id}: {type(exc).__name__}")
            log(logger, logging.ERROR, "handler error", error=repr(exc))
            raise HTTPException(500, "internal error") from None
        store.finish_event(evt.event_id, result.get("status", "unknown"), result)
        return JSONResponse(result, status_code=code)

    app.add_api_route("/webhook/tradingview/{token}", webhook, methods=["POST"], include_in_schema=False)
    app.add_api_route("/webhook/{token}", webhook, methods=["POST"], include_in_schema=False)

    # ------------------------------------------------------------------ public
    @app.get("/health")
    async def health() -> dict:
        halt = engine.halt_state()
        monitor_ok = (not start_monitor) or (monitor.last_cycle is not None and time.time() - monitor.last_cycle < 30)
        return {
            "status": "ok", "mode": settings.mode, "paper_only": settings.paper_only,
            "live_enabled": settings.live_allowed, "broker": broker.name,
            "broker_connected": broker.is_connected(), "halted": bool(halt), "monitor_ok": monitor_ok,
            "version": __version__,
        }

    # ------------------------------------------------------------------ admin
    admin = [Depends(require_admin)]

    @app.get("/status", dependencies=admin)
    async def status() -> dict:
        return await engine.status() | {
            "monitor": {"last_cycle": monitor.last_cycle, "last_error": monitor.last_error},
            "recent_alerts": alerts.sent[-20:],
        }

    @app.post("/admin/kill", dependencies=admin)
    async def kill(body: KillBody) -> dict:
        return await engine.engage_kill_switch(body.reason)

    @app.post("/admin/resume", dependencies=admin)
    async def resume(body: ResumeBody) -> JSONResponse:
        ok, msg = await engine.resume(body.actor)
        return JSONResponse({"resumed": ok, "message": msg}, status_code=200 if ok else 409)

    @app.post("/admin/flatten", dependencies=admin)
    async def flatten(body: FlattenBody) -> dict:
        ok = await engine.flatten_all(body.reason)
        return {"flattened": ok}

    @app.post("/admin/reconcile", dependencies=admin)
    async def reconcile() -> dict:
        return await engine.reconcile()

    @app.get("/admin/events", dependencies=admin)
    async def events(limit: int = 50) -> list[dict]:
        return store.recent_events(min(max(limit, 1), 500))

    @app.get("/admin/orders", dependencies=admin)
    async def orders(trading_day: str | None = None) -> list[dict]:
        return store.orders(trading_day)

    @app.get("/admin/fills", dependencies=admin)
    async def fills(trading_day: str | None = None) -> list[dict]:
        return store.fills(trading_day)

    @app.get("/admin/diagnostics", dependencies=admin)
    async def diagnostics(limit: int = 200, kind: str | None = None, since: int | None = None) -> dict:
        """V26 strategy diagnostics: the script's per-bar record plus the engine's outcome for each entry."""
        return engine.diagnostics(min(max(limit, 1), 2000), kind, since)

    @app.get("/admin/audit", dependencies=admin)
    async def audit(limit: int = 100) -> list[dict]:
        return store.recent_audit(min(max(limit, 1), 1000))

    # ------------------------------------------------------------------ paper
    def resolve_symbol(symbol: str) -> str:
        try:
            return contracts.resolve(symbol, clock.now(), allowed_root=settings.symbol_root,
                                     roll_days=settings.roll_days_before_expiry, min_days_to_expiry=0,
                                     allow_unverified_roll_window=True).contract.name
        except contracts.ContractError as exc:
            raise HTTPException(422, str(exc)) from None

    def require_paper() -> None:
        # Injected prices must never reach the risk engine of a real broker.
        if not broker.is_paper:
            raise HTTPException(403, "paper-only endpoint")

    @app.post("/paper/quote", dependencies=admin)
    async def paper_quote(body: QuoteBody) -> dict:
        require_paper()
        symbol = resolve_symbol(body.symbol)
        await engine.push_quote(Quote(symbol, last=body.last, bid=body.bid, ask=body.ask, ts=clock.now(), source="api"))
        return {"symbol": symbol, "accepted": True}

    @app.post("/paper/mark/{token}")
    async def paper_mark(token: str, price: float) -> dict:
        """Legacy prototype route: mark the active contract at ``price``."""
        check_webhook_token(token)
        require_paper()
        if price <= 0:
            raise HTTPException(422, "price must be > 0")
        symbol = store.get("active_contract") or resolve_symbol(settings.symbol_root)
        await engine.push_quote(Quote(symbol, last=price, ts=clock.now(), source="mark"))
        positions = [p.to_dict() for p in await broker.get_positions()] if broker.is_connected() else None
        return {"symbol": symbol, "price": price, "positions": positions}

    @app.post("/paper/simulate", dependencies=admin)
    async def paper_simulate(body: SimulateBody) -> dict:
        require_paper()
        if body.action == "disconnect":
            broker.simulate_disconnect()
        elif body.action == "reconnect":
            broker.simulate_reconnect()
        else:
            broker.inject_reject(None if body.role == "any" else OrderRole(body.role), body.reason)
        return {"ok": True, "action": body.action}

    return app
