"""HTTP layer: authentication, schema validation, replay protection, legacy routes."""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from conftest import ADMIN, ADMIN_TOKEN, CONTRACT, T0, WEBHOOK_TOKEN, bar_payload, entry_payload, make_settings
from v25bot.api import create_app
from v25bot.clock import FakeClock

URL = f"/webhook/tradingview/{WEBHOOK_TOKEN}"


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def factory(client_addr=("203.0.113.9", 50000), **overrides):
        clock = FakeClock(T0)
        app = create_app(make_settings(tmp_path / f"c{len(clients)}", **overrides), clock=clock, start_monitor=False)
        client = TestClient(app, client=client_addr)
        client.__enter__()
        client.clock = clock
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def test_health_keeps_legacy_fields_and_reports_paper_only(client):
    body = client.get("/health").json()
    assert body["mode"] == "PAPER_ONLY" and body["live_enabled"] is False and body["paper_only"] is True
    assert body["broker"] == "paper"


def test_valid_entry_accepted(client):
    r = client.post(URL, json=entry_payload(client.clock))
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] and r.json()["contract"] == CONTRACT


def test_wrong_token_forbidden_and_nothing_persisted(client):
    r = client.post("/webhook/tradingview/" + "x" * 32, json=entry_payload(client.clock))
    assert r.status_code == 403
    assert client.get("/admin/events", headers=ADMIN).json() == []


def test_webhook_disabled_without_token(make_client):
    c = make_client(webhook_token=None)
    assert c.post(URL, json=entry_payload(c.clock)).status_code == 503


def test_duplicate_webhook_returns_original_result_and_places_one_order(client):
    payload = entry_payload(client.clock)
    first = client.post(URL, json=payload)
    second = client.post(URL, json=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["result"]["orders"] == first.json()["orders"]
    status = client.get("/status", headers=ADMIN).json()
    assert [p["qty"] for p in status["positions"]] == [1]
    assert status["trades_today"] == 1


def test_duplicate_of_rejected_event_stays_rejected(client):
    payload = entry_payload(client.clock, qty=5)  # exceeds MAX_CONTRACTS
    first = client.post(URL, json=payload)
    assert first.status_code == 409
    second = client.post(URL, json=payload)
    assert second.json()["duplicate"] and second.json()["result"]["accepted"] is False


def test_event_id_reuse_with_different_payload_is_conflict(client):
    payload = entry_payload(client.clock)
    client.post(URL, json=payload)
    tampered = dict(payload, qty=2)
    r = client.post(URL, json=tampered)
    assert r.status_code == 409 and r.json()["status"] == "conflict"


def test_replayed_old_signal_rejected(client):
    payload = entry_payload(client.clock)
    client.clock.advance(91)
    r = client.post(URL, json=payload)
    assert r.status_code == 422 and "stale signal" in r.text


def test_future_dated_signal_rejected(client):
    payload = entry_payload(client.clock)
    payload["sent_at"] += 60_000
    assert client.post(URL, json=payload).status_code == 422


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(unexpected="x"),                 # unknown field
    lambda d: d.pop("stop"),                            # missing stop
    lambda d: d.update(stop=20010.0),                   # stop on wrong side
    lambda d: d.update(qty=0),
    lambda d: d.update(side="hold"),
    lambda d: d.update(schema="v25.webhook.v0"),
    lambda d: d.update(event_id="bad id with spaces"),
    lambda d: d.update(price=float("inf")),
])
def test_schema_violations_rejected(client, mutate):
    payload = entry_payload(client.clock)
    mutate(payload)
    r = client.post(URL, content=json.dumps(payload, allow_nan=True), headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_non_json_and_oversized_bodies_rejected(client):
    assert client.post(URL, content=b"not json", headers={"content-type": "text/plain"}).status_code == 422
    assert client.post(URL, content=b"[1,2]").status_code == 422
    big = json.dumps(entry_payload(client.clock) | {"comment": "x" * 9000})
    assert client.post(URL, content=big).status_code == 413


def test_text_plain_json_body_accepted(client):
    # TradingView sends text/plain when the message is not detected as JSON.
    r = client.post(URL, content=json.dumps(entry_payload(client.clock)), headers={"content-type": "text/plain"})
    assert r.status_code == 200


def test_passphrase_enforced_when_configured(make_client):
    c = make_client(webhook_passphrase=SecretStr("correct horse battery"))
    assert c.post(URL, json=entry_payload(c.clock)).status_code == 403
    assert c.post(URL, json=entry_payload(c.clock, passphrase="correct horse battery")).status_code == 200


def test_ip_allowlist(make_client):
    c = make_client(enforce_ip_allowlist=True)
    assert c.post(URL, json=entry_payload(c.clock)).status_code == 403  # 203.0.113.9 is not TradingView
    direct = make_client(client_addr=("52.89.214.238", 40000), enforce_ip_allowlist=True)
    assert direct.post(URL, json=entry_payload(direct.clock)).status_code == 200
    # Behind a trusted reverse proxy (e.g. Caddy on 172.18.0.2), the forwarded client IP is used.
    proxied = make_client(client_addr=("172.18.0.2", 40000), enforce_ip_allowlist=True,
                          trust_proxy_headers=True, trusted_proxies=("172.16.0.0/12",))
    r = proxied.post(URL, json=entry_payload(proxied.clock), headers={"x-forwarded-for": "52.89.214.238"})
    assert r.status_code == 200
    r = proxied.post(URL, json=entry_payload(proxied.clock, qty=2), headers={"x-forwarded-for": "198.51.100.1"})
    assert r.status_code == 403


def test_spoofed_forwarded_for_ignored_without_trusted_proxy(make_client):
    c = make_client(enforce_ip_allowlist=True, trust_proxy_headers=False)
    r = c.post(URL, json=entry_payload(c.clock), headers={"x-forwarded-for": "52.89.214.238"})
    assert r.status_code == 403


def test_legacy_prototype_payload_still_works(client):
    legacy = {"event_id": "test-legacy-0001", "symbol": "MNQ", "action": "buy", "qty": 1,
              "entry": 20000, "stop": 19975, "target": 20075, "timestamp": int(client.clock.ts())}
    r = client.post(f"/webhook/{WEBHOOK_TOKEN}", json=legacy)
    assert r.status_code == 200 and r.json()["accepted"]
    dup = client.post(f"/webhook/{WEBHOOK_TOKEN}", json=legacy)
    assert dup.json()["duplicate"] is True


def test_legacy_payload_disabled(make_client):
    c = make_client(allow_legacy_schema=False)
    legacy = {"event_id": "test-legacy-0002", "symbol": "MNQ", "action": "buy", "qty": 1,
              "entry": 20000, "stop": 19975, "target": 20075, "timestamp": int(c.clock.ts())}
    assert c.post(URL, json=legacy).status_code == 422


def test_legacy_paper_mark_route(client):
    client.post(URL, json=entry_payload(client.clock))
    r = client.post(f"/paper/mark/{WEBHOOK_TOKEN}", params={"price": 20061})
    assert r.status_code == 200 and r.json()["positions"] == []
    assert client.post("/paper/mark/" + "x" * 32, params={"price": 1}).status_code == 403


def test_bar_events_drive_paper_simulation(client):
    client.post(URL, json=entry_payload(client.clock))
    r = client.post(URL, json=bar_payload(client.clock, 20000.0, 20005.0, 19975.0, 19990.0))
    assert r.json()["status"] == "processed"
    status = client.get("/status", headers=ADMIN).json()
    assert status["positions"] == [] and status["account"]["realized_pnl_today"] < 0
    # Same bar again: ignored (monotonic bar time).
    again = client.post(URL, json=bar_payload(client.clock, 20000.0, 20005.0, 19975.0, 19990.0))
    assert again.json()["status"] == "ignored"


def test_admin_endpoints_require_bearer_token(client):
    assert client.get("/status").status_code == 401
    assert client.get("/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/admin/kill", json={}).status_code == 401
    assert client.get("/status", headers=ADMIN).status_code == 200


def test_admin_kill_resume_flow(client):
    client.post(URL, json=entry_payload(client.clock))
    assert client.post("/admin/kill", json={"reason": "drill"}, headers=ADMIN).json()["flattened"]
    assert client.get("/health").json()["halted"] is True
    client.clock.advance(60)
    r = client.post(URL, json=entry_payload(client.clock))
    assert r.status_code == 409 and "halted" in r.json()["reasons"][0]
    assert client.post("/admin/resume", json={"confirm": "nope"}, headers=ADMIN).status_code == 422
    assert client.post("/admin/resume", json={"confirm": "RESUME"}, headers=ADMIN).json()["resumed"]


def test_paper_simulate_controls(client):
    assert client.post("/paper/simulate", json={"action": "reject_next", "role": "entry"}, headers=ADMIN).json()["ok"]
    r = client.post(URL, json=entry_payload(client.clock))
    assert r.status_code == 409 and "broker rejected" in r.json()["reasons"][0]
    client.post("/paper/simulate", json={"action": "disconnect"}, headers=ADMIN)
    client.clock.advance(60)
    r = client.post(URL, json=entry_payload(client.clock))
    assert "disconnected" in r.json()["reasons"][0]


def test_secrets_never_logged_or_stored(make_client, caplog):
    c = make_client(webhook_passphrase=SecretStr("super-secret-passphrase"))
    caplog.set_level(logging.DEBUG)
    c.post(URL, json=entry_payload(c.clock, passphrase="super-secret-passphrase"))
    c.post("/webhook/tradingview/" + "x" * 32, json={})
    text = caplog.text + json.dumps(c.get("/admin/events", headers=ADMIN).json())
    text += json.dumps(c.get("/admin/audit", headers=ADMIN).json()) + json.dumps(c.get("/status", headers=ADMIN).json())
    stored = c.app.state.store._conn.execute("SELECT payload_json FROM events").fetchall()
    text += json.dumps([r[0] for r in stored])
    for secret in (WEBHOOK_TOKEN, ADMIN_TOKEN, "super-secret-passphrase"):
        assert secret not in text


def test_docs_and_errors_do_not_echo_secrets(client):
    r = client.post(URL, json={"schema": "v25.webhook.v1", "passphrase": "leak-me-please"})
    assert r.status_code == 422 and "leak-me-please" not in r.text


def test_paper_price_endpoints_refused_for_real_broker(tmp_path):
    from v25bot.brokers.paper import PaperBroker
    from v25bot.market_data import QuoteBook
    from v25bot.store import Store

    settings = make_settings(tmp_path)
    clock = FakeClock(T0)
    fake_real = PaperBroker(settings, clock, QuoteBook(), Store(str(tmp_path / "other.db")))
    fake_real.is_paper = False
    app = create_app(settings, clock=clock, broker=fake_real, start_monitor=False)
    with TestClient(app) as c:
        assert c.post("/paper/quote", headers=ADMIN, json={"symbol": "MNQ1!", "last": 20000}).status_code == 403
        assert c.post(f"/paper/mark/{WEBHOOK_TOKEN}", params={"price": 20000}).status_code == 403
        assert c.post("/paper/simulate", headers=ADMIN, json={"action": "disconnect"}).status_code == 403
