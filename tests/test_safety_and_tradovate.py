"""Safety gates, secret handling, and the (disabled) Tradovate adapter."""

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr

from conftest import T0, make_settings
from v25bot.brokers.base import BracketRequest, BrokerError, OrderRejected, Side, TradingDisabled
from v25bot.brokers.tradovate import TradovateBroker, permission_failures
from v25bot.clock import FakeClock
from v25bot.config import LIVE_ACK_PHRASE, ConfigError, Settings
from v25bot.logging_setup import install_redacting_record_factory, redact, redact_obj, register_secret

CREDS = dict(
    tradovate_username=SecretStr("demo-user"), tradovate_password=SecretStr("hunter2-password"),
    tradovate_app_id="V25", tradovate_cid=SecretStr("1234"), tradovate_secret=SecretStr("api-secret-value"),
    tradovate_device_id="device-1", tradovate_account_spec="DEMO123",
)


def test_defaults_are_paper_only(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("PAPER_", "LIVE_", "BROKER", "TRADOVATE_")):
            monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    assert s.paper_only is True and s.broker == "paper" and s.tradovate_enabled is False
    assert s.mode == "PAPER_ONLY" and not s.live_allowed
    assert "PAPER_ONLY=true" in s.live_gate_failures()


def test_paper_only_forbids_any_broker_adapter(tmp_path):
    with pytest.raises(ConfigError, match="PAPER_ONLY=true permits only BROKER=paper"):
        make_settings(tmp_path, broker="tradovate", tradovate_enabled=True,
                      tradovate_account_is_prop_or_evaluation=False, **CREDS)


def test_prop_or_evaluation_account_refused(tmp_path):
    with pytest.raises(ConfigError, match="prop-firm/evaluation"):
        make_settings(tmp_path, paper_only=False, broker="tradovate", tradovate_enabled=True, **CREDS)


def test_live_env_requires_every_gate(tmp_path):
    with pytest.raises(ConfigError, match="TRADOVATE_ENV=live refused"):
        make_settings(tmp_path, paper_only=False, broker="tradovate", tradovate_enabled=True,
                      tradovate_account_is_prop_or_evaluation=False, tradovate_env="live", **CREDS)
    with pytest.raises(ConfigError, match="LIVE_TRADING_ACK"):
        make_settings(tmp_path, paper_only=False, broker="tradovate", tradovate_enabled=True,
                      tradovate_account_is_prop_or_evaluation=False, tradovate_env="live",
                      live_trading_enabled=True, live_trading_ack="yes", allow_legacy_schema=False, **CREDS)
    ok = make_settings(tmp_path, paper_only=False, broker="tradovate", tradovate_enabled=True,
                       tradovate_account_is_prop_or_evaluation=False, tradovate_env="live",
                       live_trading_enabled=True, live_trading_ack=LIVE_ACK_PHRASE, allow_legacy_schema=False,
                       **CREDS)
    assert ok.live_allowed and ok.mode == "LIVE"


def test_secrets_hidden_in_repr_and_dump(tmp_path):
    s = make_settings(tmp_path, **CREDS)
    text = repr(s) + str(s.model_dump())
    for raw in ("hunter2-password", "api-secret-value", "w" * 32, "a" * 32):
        assert raw not in text


def test_secret_files(tmp_path, monkeypatch):
    secret_file = tmp_path / "webhook_token"
    secret_file.write_text("f" * 40 + "\n")
    monkeypatch.setenv("WEBHOOK_TOKEN_FILE", str(secret_file))
    monkeypatch.setenv("WEBHOOK_TOKEN", "ignored-because-file-wins-xxxxxxxx")
    assert Settings.from_env().webhook_token.get_secret_value() == "f" * 40


def test_short_tokens_rejected(tmp_path):
    with pytest.raises(ConfigError, match="at least 24"):
        make_settings(tmp_path, webhook_token=SecretStr("short"))


def test_redaction_patterns():
    register_secret("my-registered-secret")
    assert "my-registered-secret" not in redact("value my-registered-secret here")
    assert "abc.def.ghi" not in redact("Authorization: Bearer abc.def.ghi123")
    assert "p4ss" not in redact('{"password": "p4ss"}')
    assert "tok123" not in redact("POST /webhook/tradingview/tok123 200")
    assert redact_obj({"passphrase": "x", "nested": {"accessToken": "y"}}) == {
        "passphrase": "***REDACTED***", "nested": {"accessToken": "***REDACTED***"}}


def test_redacting_record_factory(caplog):
    install_redacting_record_factory()
    register_secret("factory-secret-123")
    logging.getLogger("third.party").warning("connecting with %s", "factory-secret-123")
    assert "factory-secret-123" not in caplog.text


# --------------------------------------------------------------------------- Tradovate
def tv_settings(tmp_path, **kw):
    base = dict(paper_only=False, broker="tradovate", tradovate_enabled=True,
                tradovate_account_is_prop_or_evaluation=False, tradovate_env="demo") | CREDS
    base.update(kw)
    return make_settings(tmp_path, **base)


def test_adapter_refuses_under_default_settings(tmp_path):
    s = make_settings(tmp_path)
    with pytest.raises(TradingDisabled) as exc:
        TradovateBroker(s, FakeClock(T0))
    msg = str(exc.value)
    assert "PAPER_ONLY=true" in msg and "TRADOVATE_ENABLED" in msg and "prop" in msg


def test_adapter_lists_missing_credentials_by_name_only(tmp_path):
    s = Settings(paper_only=False, tradovate_enabled=True, tradovate_account_is_prop_or_evaluation=False,
                 tradovate_password=SecretStr("hunter2-password"))
    failures = " ".join(permission_failures(s))
    assert "TRADOVATE_USERNAME" in failures and "hunter2-password" not in failures


class FakeTradovate:
    def __init__(self, auth_response=None):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.auth_response = auth_response or {"accessToken": "access-token-xyz",
                                               "expirationTime": "2026-10-20T16:00:00Z"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path.removeprefix("/v1")
        routes = {
            "/auth/accesstokenrequest": self.auth_response,
            "/account/list": [{"id": 77, "name": "DEMO123", "active": True}],
            "/order/placeoso": {"orderId": 1, "oso1Id": 2, "oso2Id": 3},
            "/cashBalance/getcashbalancesnapshot": {"totalCashValue": 50_000.0, "realizedPnL": -12.5, "openPnL": 4.0},
        }
        return httpx.Response(200, json=routes.get(path, {}))


def test_placeoso_request_shape_and_isautomated(tmp_path):
    fake = FakeTradovate()
    b = TradovateBroker(tv_settings(tmp_path), FakeClock(T0), transport=httpx.MockTransport(fake))
    loop = asyncio.new_event_loop()
    loop.run_until_complete(b.connect())
    res = loop.run_until_complete(b.place_bracket(BracketRequest("cid", "MNQZ6", Side.BUY, 1, 19980.0, 20060.0)))
    assert (res.entry.id, res.stop.id, res.target.id) == ("1", "2", "3")
    _, path, body = next(r for r in fake.requests if r[1].endswith("/order/placeoso"))
    assert body["isAutomated"] is True and body["orderType"] == "Market" and body["accountId"] == 77
    assert body["bracket1"] == {"action": "Sell", "orderType": "Stop", "stopPrice": 19980.0}
    assert body["bracket2"] == {"action": "Sell", "orderType": "Limit", "price": 20060.0}
    acct = loop.run_until_complete(b.get_account())
    assert acct.verified and acct.realized_pnl_today == -12.5
    loop.run_until_complete(b.close())
    loop.close()


def test_captcha_challenge_is_never_bypassed(tmp_path):
    fake = FakeTradovate({"p-ticket": "ticket", "p-time": 0, "p-captcha": True})
    b = TradovateBroker(tv_settings(tmp_path), FakeClock(T0), transport=httpx.MockTransport(fake))
    loop = asyncio.new_event_loop()
    with pytest.raises(TradingDisabled, match="CAPTCHA"):
        loop.run_until_complete(b.connect())
    assert sum(1 for r in fake.requests if r[1].endswith("accesstokenrequest")) == 1
    loop.close()


def test_auth_error_does_not_leak_credentials(tmp_path):
    fake = FakeTradovate({"errorText": "Incorrect username or password"})
    b = TradovateBroker(tv_settings(tmp_path), FakeClock(T0), transport=httpx.MockTransport(fake))
    loop = asyncio.new_event_loop()
    with pytest.raises(BrokerError) as exc:
        loop.run_until_complete(b.connect())
    assert "hunter2-password" not in str(exc.value) and "api-secret-value" not in str(exc.value)
    loop.close()


def test_unverifiable_cash_balance_marked_unverified(tmp_path):
    class Partial(FakeTradovate):
        def __call__(self, request):
            if request.url.path.endswith("getcashbalancesnapshot"):
                return httpx.Response(200, json={"totalCashValue": 50_000.0})
            return super().__call__(request)

    b = TradovateBroker(tv_settings(tmp_path), FakeClock(T0), transport=httpx.MockTransport(Partial()))
    loop = asyncio.new_event_loop()
    loop.run_until_complete(b.connect())
    assert loop.run_until_complete(b.get_account()).verified is False
    loop.close()


def test_rejected_oso_raises(tmp_path):
    class Reject(FakeTradovate):
        def __call__(self, request):
            if request.url.path.endswith("placeoso"):
                return httpx.Response(200, json={"failureReason": "RiskCheck", "failureText": "Exceeds limit"})
            return super().__call__(request)

    b = TradovateBroker(tv_settings(tmp_path), FakeClock(T0), transport=httpx.MockTransport(Reject()))
    loop = asyncio.new_event_loop()
    loop.run_until_complete(b.connect())
    with pytest.raises(OrderRejected, match="Exceeds limit"):
        loop.run_until_complete(b.place_bracket(BracketRequest("c", "MNQZ6", Side.SELL, 1, 20020.0, 19940.0)))
    loop.close()


def test_network_failure_marks_disconnected(tmp_path):
    def boom(request):
        raise httpx.ConnectError("down")

    b = TradovateBroker(tv_settings(tmp_path), FakeClock(T0), transport=httpx.MockTransport(boom))
    loop = asyncio.new_event_loop()
    with pytest.raises(BrokerError):
        loop.run_until_complete(b.connect())
    assert not b.is_connected()
    loop.close()


def test_now_is_utc_aware():
    assert datetime.now(timezone.utc).tzinfo is not None
