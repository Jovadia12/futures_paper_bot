"""Deployment config: the engine is reachable only from this PC, stays paper-only,
and the IP allowlist works through a Cloudflare Tunnel on Docker Desktop."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import T0, WEBHOOK_TOKEN, entry_payload, make_settings
from v25bot.api import create_app
from v25bot.clock import FakeClock

COMPOSE = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
URL = f"/webhook/tradingview/{WEBHOOK_TOKEN}"
TRADINGVIEW_IP = "52.89.214.238"


def _service(name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S)", COMPOSE, re.S | re.M)
    assert match, f"service {name} not found"
    return match.group(1)


def _env(block: str, key: str) -> str:
    match = re.search(rf'^\s+{key}: "?([^"\n]*)"?$', block, re.M)
    assert match, f"{key} not set"
    return match.group(1)


ENGINE = _service("engine")
TRUSTED_PROXIES = tuple(_env(ENGINE, "TRUSTED_PROXIES").split(","))


def test_engine_published_on_loopback_only():
    ports = re.findall(r"^\s+ports: \[(.*)\]$", ENGINE, re.M)
    assert ports == ['"127.0.0.1:8000:8000"']


def test_compose_forces_paper_only():
    assert _env(ENGINE, "PAPER_ONLY") == "true"
    assert _env(ENGINE, "BROKER") == "paper"
    assert _env(ENGINE, "LIVE_TRADING_ENABLED") == "false"
    assert _env(ENGINE, "TRADOVATE_ENABLED") == "false"


def test_caddy_is_opt_in():
    assert 'profiles: ["caddy"]' in _service("caddy")


@pytest.fixture
def tunnel(tmp_path):
    """The engine as configured by docker-compose.yml, behind Docker Desktop + cloudflared."""
    clients = []

    def factory(peer: str) -> TestClient:
        settings = make_settings(tmp_path / f"t{len(clients)}", enforce_ip_allowlist=True,
                                 trust_proxy_headers=True, trusted_proxies=TRUSTED_PROXIES)
        clock = FakeClock(T0)
        client = TestClient(create_app(settings, clock=clock, start_monitor=False), client=(peer, 40000))
        client.__enter__()
        client.clock = clock
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


# Docker Desktop shows published-port traffic as its VM forwarder or a bridge gateway.
@pytest.mark.parametrize("peer", ["192.168.65.1", "172.18.0.1", "127.0.0.1"])
def test_tradingview_through_cloudflare_tunnel_accepted(tunnel, peer):
    c = tunnel(peer)
    payload = entry_payload(c.clock)
    r = c.post(URL, json=payload, headers={"x-forwarded-for": TRADINGVIEW_IP})
    assert r.status_code == 200 and r.json()["accepted"], r.text
    # Deduplication still applies through the tunnel.
    dup = c.post(URL, json=payload, headers={"x-forwarded-for": TRADINGVIEW_IP})
    assert dup.json()["duplicate"] is True


def test_forged_forwarded_for_rejected(tunnel):
    # Attacker claims to be TradingView; Cloudflare appends the attacker's real IP.
    c = tunnel("192.168.65.1")
    r = c.post(URL, json=entry_payload(c.clock), headers={"x-forwarded-for": f"{TRADINGVIEW_IP}, 198.51.100.7"})
    assert r.status_code == 403


def test_missing_forwarded_for_rejected(tunnel):
    c = tunnel("192.168.65.1")
    assert c.post(URL, json=entry_payload(c.clock)).status_code == 403


def test_untrusted_peer_cannot_claim_tradingview(tunnel):
    c = tunnel("10.0.0.5")
    r = c.post(URL, json=entry_payload(c.clock), headers={"x-forwarded-for": TRADINGVIEW_IP})
    assert r.status_code == 403


def test_bad_token_rejected_through_tunnel(tunnel):
    c = tunnel("192.168.65.1")
    r = c.post("/webhook/tradingview/" + "x" * 32, json=entry_payload(c.clock),
               headers={"x-forwarded-for": TRADINGVIEW_IP})
    assert r.status_code == 403


def test_health_reachable_through_tunnel(tunnel):
    body = tunnel("192.168.65.1").get("/health").json()
    assert body["status"] == "ok" and body["paper_only"] is True and body["live_enabled"] is False
