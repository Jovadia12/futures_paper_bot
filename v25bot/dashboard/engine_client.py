"""Read-only client for the engine's existing HTTP API.

The dashboard never touches the engine's database or process. It can only
issue GET requests, and only to the paths below. Any other method or path is
refused here, before a request is built, so the dashboard cannot kill,
resume, flatten, reconcile, or place orders even though it holds the admin
bearer token that the engine requires for its status endpoints.
"""

from __future__ import annotations

import asyncio
import time

import httpx

READ_ONLY_PATHS = frozenset({
    "/health", "/status", "/admin/fills", "/admin/orders", "/admin/events", "/admin/audit",
    "/admin/diagnostics",
})


class EngineUnavailable(RuntimeError):
    pass


class EngineEndpointMissing(EngineUnavailable):
    """The engine answered 404: it is older than the endpoint (e.g. no V26 diagnostics yet)."""


class ReadOnlyEngineClient:
    def __init__(self, base_url: str, admin_token: str | None, *, cache_seconds: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 4.0):
        self._token = admin_token
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._cache: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.cache_seconds = cache_seconds

    async def close(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: dict | None = None, *, auth: bool = True) -> object:
        if path not in READ_ONLY_PATHS:
            raise PermissionError(f"dashboard engine client is read-only; {path!r} is not allowed")
        headers = {"Accept": "application/json"}
        if auth:
            if not self._token:
                raise EngineUnavailable("ADMIN_TOKEN is not configured for the dashboard")
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = await self._http.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"engine unreachable ({type(exc).__name__})") from None
        if resp.status_code == 401:
            raise EngineUnavailable("engine rejected the dashboard's admin token (HTTP 401)")
        if resp.status_code == 404:
            raise EngineEndpointMissing(f"engine has no {path} endpoint (HTTP 404)")
        if resp.status_code >= 400:
            raise EngineUnavailable(f"engine returned HTTP {resp.status_code} for {path}")
        return resp.json()

    async def cached(self, path: str, params: dict | None = None, max_age: float | None = None) -> object:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        max_age = self.cache_seconds if max_age is None else max_age
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < max_age:
                return hit[1]
            value = await self.get(path, params)
            self._cache[key] = (time.monotonic(), value)
            return value

    # ------------------------------------------------------------ typed helpers
    async def health(self) -> dict:
        return await self.get("/health", auth=False)  # public engine endpoint

    async def status(self) -> dict:
        return await self.get("/status")

    async def fills(self) -> list[dict]:
        return await self.cached("/admin/fills")

    async def orders(self) -> list[dict]:
        return await self.cached("/admin/orders")

    async def events(self) -> list[dict]:
        return await self.cached("/admin/events", {"limit": 500})

    async def audit(self) -> list[dict]:
        return await self.cached("/admin/audit", {"limit": 1000})

    async def diagnostics(self, since_ms: int | None = None) -> dict:
        params = {"limit": 500} | ({"since": since_ms} if since_ms else {})
        return await self.cached("/admin/diagnostics", params)
