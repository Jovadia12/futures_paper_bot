"""Tradovate REST adapter (DISABLED BY DEFAULT, UNVERIFIED AGAINST REAL SERVERS).

Written against Tradovate's public API documentation only:
  POST /auth/accesstokenrequest        GET  /auth/renewaccesstoken
  GET  /account/list                   POST /cashBalance/getcashbalancesnapshot
  GET  /position/list                  GET  /contract/item?id=  GET /contract/find?name=
  GET  /order/list                     GET  /orderVersion/deps?masterid=
  GET  /fill/list                      POST /order/placeoso  (entry + OCO stop/target)
  POST /order/cancelorder              POST /order/liquidateposition

Eligibility (Tradovate support article "Tradovate API Access"): an active API
Access add-on on a live, funded Tradovate account is required. Prop-firm and
evaluation accounts, including Lucid, are not eligible. This adapter therefore
refuses to construct unless TRADOVATE_ACCOUNT_IS_PROP_OR_EVALUATION=false.
It also refuses whenever PAPER_ONLY=true, and refuses TRADOVATE_ENV=live unless
every live gate in ``Settings.live_gate_failures`` passes.

It never solves or bypasses CAPTCHA / "p-ticket" penalty challenges. A CAPTCHA
challenge stops the adapter and asks a human to log in through Tradovate.
Every order is sent with ``isAutomated: true``, as Tradovate requires for
non-manual orders.

This code has NOT been exercised against Tradovate servers. Treat it as a
starting point that needs a verified end-to-end demo test before any use.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from ..clock import Clock
from ..config import Settings
from .base import (
    AccountSnapshot,
    BracketRequest,
    BracketResult,
    Broker,
    BrokerDisconnected,
    BrokerError,
    Fill,
    Order,
    OrderRejected,
    OrderRole,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TradingDisabled,
)

BASE_URLS = {
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1",
}

_STATUS = {
    "PendingNew": OrderStatus.WORKING, "Working": OrderStatus.WORKING,
    "Suspended": OrderStatus.PENDING, "Filled": OrderStatus.FILLED,
    "Canceled": OrderStatus.CANCELLED, "Expired": OrderStatus.CANCELLED,
    "Rejected": OrderStatus.REJECTED, "Completed": OrderStatus.FILLED,
}
_TYPES = {"Market": OrderType.MARKET, "Limit": OrderType.LIMIT, "Stop": OrderType.STOP}


def permission_failures(s: Settings) -> list[str]:
    reasons: list[str] = []
    if s.paper_only:
        reasons.append("PAPER_ONLY=true: broker execution is disabled")
    if not s.tradovate_enabled:
        reasons.append("TRADOVATE_ENABLED is not true")
    if s.tradovate_account_is_prop_or_evaluation:
        reasons.append(
            "account declared prop/evaluation: Tradovate does not grant API access to prop-firm or "
            "evaluation accounts (e.g. Lucid)"
        )
    if s.tradovate_env == "live" and not s.live_allowed:
        reasons.append("live environment requires all live gates: " + "; ".join(s.live_gate_failures()))
    missing = [
        name for name, value in (
            ("TRADOVATE_USERNAME", s.tradovate_username), ("TRADOVATE_PASSWORD", s.tradovate_password),
            ("TRADOVATE_CID", s.tradovate_cid), ("TRADOVATE_SECRET", s.tradovate_secret),
            ("TRADOVATE_APP_ID", s.tradovate_app_id), ("TRADOVATE_DEVICE_ID", s.tradovate_device_id),
            ("TRADOVATE_ACCOUNT_SPEC", s.tradovate_account_spec),
        ) if not value
    ]
    if missing:
        reasons.append("missing credentials: " + ", ".join(missing))
    return reasons


class TradovateBroker(Broker):
    name = "tradovate"
    is_paper = False

    def __init__(self, settings: Settings, clock: Clock, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__()
        failures = permission_failures(settings)
        if failures:
            raise TradingDisabled("Tradovate adapter disabled: " + " | ".join(failures))
        self.s = settings
        self.clock = clock
        self.base = BASE_URLS[settings.tradovate_env]
        self._http = httpx.AsyncClient(base_url=self.base, timeout=5.0, transport=transport)
        self._token: str | None = None
        self._token_expiry: datetime | None = None
        self._account_id: int | None = None
        self._connected = False
        self._contract_names: dict[int, str] = {}
        self._contract_ids: dict[str, int] = {}

    # ---------------------------------------------------------------- transport
    async def _request(self, method: str, path: str, *, json: dict | None = None,
                       params: dict | None = None, auth: bool = True) -> object:
        headers = {"Accept": "application/json"}
        if auth:
            await self._ensure_token()
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = await self._http.request(method, path, json=json, params=params, headers=headers)
        except httpx.HTTPError as exc:
            self._connected = False
            raise BrokerDisconnected(f"Tradovate {path}: {type(exc).__name__}") from None
        if resp.status_code in (401, 403):
            self._connected = False
            raise BrokerDisconnected(f"Tradovate {path}: HTTP {resp.status_code} (not authorised)")
        if resp.status_code == 429:
            raise BrokerError(f"Tradovate {path}: rate limited (HTTP 429)")
        if resp.status_code >= 400:
            raise BrokerError(f"Tradovate {path}: HTTP {resp.status_code}")
        self._connected = True
        return resp.json() if resp.content else None

    async def _ensure_token(self) -> None:
        now = self.clock.now()
        if self._token and self._token_expiry and now < self._token_expiry - timedelta(minutes=10):
            return
        if self._token and self._token_expiry and now < self._token_expiry:
            data = await self._request_noauth_renew()
            if data:
                return
        await self._authenticate()

    async def _request_noauth_renew(self) -> bool:
        try:
            resp = await self._http.get(
                "/auth/renewaccesstoken", headers={"Authorization": f"Bearer {self._token}"}
            )
        except httpx.HTTPError:
            return False
        if resp.status_code != 200:
            return False
        data = resp.json()
        if "accessToken" not in data:
            return False
        self._store_token(data)
        return True

    def _store_token(self, data: dict) -> None:
        self._token = data["accessToken"]
        expiry = data.get("expirationTime")
        try:
            self._token_expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00")) if expiry else None
        except (AttributeError, ValueError):
            self._token_expiry = None
        if self._token_expiry is None:
            self._token_expiry = self.clock.now() + timedelta(minutes=60)

    async def _authenticate(self) -> None:
        s = self.s
        body = {
            "name": s.tradovate_username.get_secret_value(),
            "password": s.tradovate_password.get_secret_value(),
            "appId": s.tradovate_app_id,
            "appVersion": s.tradovate_app_version,
            "cid": s.tradovate_cid.get_secret_value(),
            "sec": s.tradovate_secret.get_secret_value(),
            "deviceId": s.tradovate_device_id,
        }
        for _ in range(2):
            data = await self._request("POST", "/auth/accesstokenrequest", json=body, auth=False)
            if not isinstance(data, dict):
                raise BrokerError("Tradovate auth: unexpected response")
            if data.get("p-captcha"):
                self._connected = False
                raise TradingDisabled(
                    "Tradovate requires a CAPTCHA. Log in once through Tradovate's own app; this "
                    "adapter will not attempt to solve or bypass it."
                )
            if "p-ticket" in data:
                wait = float(data.get("p-time", 0))
                if wait > 30:
                    raise BrokerError(f"Tradovate auth penalty: retry after {wait:.0f}s")
                await asyncio.sleep(wait)
                body = body | {"p-ticket": data["p-ticket"]}
                continue
            if "errorText" in data:
                raise BrokerError("Tradovate auth rejected: " + str(data["errorText"])[:200])
            if "accessToken" not in data:
                raise BrokerError("Tradovate auth: no access token in response")
            self._store_token(data)
            return
        raise BrokerError("Tradovate auth: penalty challenge not cleared")

    # ---------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        await self._authenticate()
        accounts = await self._request("GET", "/account/list")
        match = [a for a in accounts or [] if a.get("name") == self.s.tradovate_account_spec]
        if len(match) != 1:
            raise BrokerError("configured TRADOVATE_ACCOUNT_SPEC not found exactly once in /account/list")
        if not match[0].get("active", True):
            raise BrokerError("Tradovate account is not active")
        self._account_id = int(match[0]["id"])
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        await self._http.aclose()

    def is_connected(self) -> bool:
        return self._connected and self._account_id is not None

    def _acct(self) -> int:
        if self._account_id is None:
            raise BrokerDisconnected("Tradovate account not resolved; connect() first")
        return self._account_id

    async def _contract_name(self, contract_id: int) -> str:
        if contract_id not in self._contract_names:
            data = await self._request("GET", "/contract/item", params={"id": contract_id})
            self._contract_names[contract_id] = data["name"]
            self._contract_ids[data["name"]] = contract_id
        return self._contract_names[contract_id]

    async def _contract_id(self, name: str) -> int:
        if name not in self._contract_ids:
            data = await self._request("GET", "/contract/find", params={"name": name})
            if not data or "id" not in data:
                raise BrokerError(f"Tradovate contract {name} not found")
            self._contract_ids[name] = int(data["id"])
            self._contract_names[int(data["id"])] = name
        return self._contract_ids[name]

    # ---------------------------------------------------------------- queries
    async def get_account(self) -> AccountSnapshot:
        data = await self._request("POST", "/cashBalance/getcashbalancesnapshot", json={"accountId": self._acct()})
        required = ("totalCashValue", "realizedPnL", "openPnL")
        verified = isinstance(data, dict) and all(isinstance(data.get(k), (int, float)) for k in required)
        data = data if isinstance(data, dict) else {}
        return AccountSnapshot(
            account_id=str(self._acct()),
            balance=float(data.get("totalCashValue") or 0.0),
            realized_pnl_today=float(data.get("realizedPnL") or 0.0),
            open_pnl=float(data.get("openPnL") or 0.0),
            fetched_at=self.clock.now(),
            verified=verified,
            source="tradovate",
        )

    async def get_positions(self) -> list[Position]:
        rows = await self._request("GET", "/position/list") or []
        out = []
        for row in rows:
            if row.get("accountId") != self._acct() or not row.get("netPos"):
                continue
            name = await self._contract_name(int(row["contractId"]))
            out.append(Position(name, int(row["netPos"]), float(row.get("netPrice") or 0.0)))
        return out

    async def get_orders(self, open_only: bool = True) -> list[Order]:
        rows = await self._request("GET", "/order/list") or []
        out = []
        for row in rows:
            if row.get("accountId") != self._acct():
                continue
            status = _STATUS.get(row.get("ordStatus", ""), OrderStatus.PENDING)
            if open_only and not status.is_open:
                continue
            versions = await self._request("GET", "/orderVersion/deps", params={"masterid": row["id"]}) or []
            version = versions[-1] if versions else {}
            otype = _TYPES.get(version.get("orderType", ""), OrderType.MARKET)
            role = {OrderType.STOP: OrderRole.STOP_LOSS, OrderType.LIMIT: OrderRole.TAKE_PROFIT}.get(otype, OrderRole.OTHER)
            ts = _parse_ts(row.get("timestamp")) or self.clock.now()
            out.append(Order(
                id=str(row["id"]), symbol=await self._contract_name(int(row["contractId"])),
                side=Side.BUY if row.get("action") == "Buy" else Side.SELL, type=otype,
                qty=int(version.get("orderQty") or 0), role=role, status=status,
                created_at=ts, updated_at=ts, limit_price=version.get("price"),
                stop_price=version.get("stopPrice"),
            ))
        return out

    async def get_fills(self) -> list[Fill]:
        rows = await self._request("GET", "/fill/list") or []
        out = []
        for row in rows:
            if not row.get("active", True):
                continue
            out.append(Fill(
                id=str(row["id"]), order_id=str(row.get("orderId")),
                symbol=await self._contract_name(int(row["contractId"])),
                side=Side.BUY if row.get("action") == "Buy" else Side.SELL,
                qty=int(row["qty"]), price=float(row["price"]), commission=0.0,
                ts=_parse_ts(row.get("timestamp")) or self.clock.now(),
            ))
        return out

    # ---------------------------------------------------------------- orders
    async def place_bracket(self, req: BracketRequest) -> BracketResult:
        if req.entry_type is not OrderType.MARKET:
            raise OrderRejected("only market entries are supported by this adapter")
        exit_action = "Sell" if req.side is Side.BUY else "Buy"
        body = {
            "accountSpec": self.s.tradovate_account_spec,
            "accountId": self._acct(),
            "action": "Buy" if req.side is Side.BUY else "Sell",
            "symbol": req.symbol,
            "orderQty": req.qty,
            "orderType": "Market",
            "isAutomated": True,
            "bracket1": {"action": exit_action, "orderType": "Stop", "stopPrice": req.stop_price},
            "bracket2": {"action": exit_action, "orderType": "Limit", "price": req.target_price},
        }
        data = await self._request("POST", "/order/placeoso", json=body)
        if not isinstance(data, dict) or data.get("failureReason") or "orderId" not in data:
            reason = (data or {}).get("failureText") or (data or {}).get("failureReason") or "unknown"
            raise OrderRejected(f"Tradovate rejected OSO: {str(reason)[:200]}")
        now = self.clock.now()

        def mk(oid, role, otype, side, **kw) -> Order:
            return Order(id=str(oid), symbol=req.symbol, side=side, type=otype, qty=req.qty, role=role,
                         status=OrderStatus.WORKING, created_at=now, updated_at=now,
                         client_id=req.client_id, **kw)

        entry = mk(data["orderId"], OrderRole.ENTRY, OrderType.MARKET, req.side)
        stop = mk(data["oso1Id"], OrderRole.STOP_LOSS, OrderType.STOP, req.side.opposite,
                  stop_price=req.stop_price, parent_id=entry.id) if data.get("oso1Id") else None
        target = mk(data["oso2Id"], OrderRole.TAKE_PROFIT, OrderType.LIMIT, req.side.opposite,
                    limit_price=req.target_price, parent_id=entry.id) if data.get("oso2Id") else None
        return BracketResult(entry, stop, target, extra={"raw_keys": sorted(data)})

    async def cancel_order(self, order_id: str) -> None:
        data = await self._request("POST", "/order/cancelorder", json={"orderId": int(order_id), "isAutomated": True})
        if isinstance(data, dict) and data.get("failureReason"):
            raise BrokerError(f"cancel failed: {str(data.get('failureText') or data['failureReason'])[:200]}")

    async def flatten(self, symbol: str) -> list[Order]:
        for order in await self.get_orders(open_only=True):
            if order.symbol == symbol:
                await self.cancel_order(order.id)
        contract_id = await self._contract_id(symbol)
        data = await self._request(
            "POST", "/order/liquidateposition",
            json={"accountId": self._acct(), "contractId": contract_id, "admin": False},
        )
        if isinstance(data, dict) and data.get("failureReason"):
            raise OrderRejected(f"liquidation rejected: {str(data.get('failureText') or data['failureReason'])[:200]}")
        return []


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
