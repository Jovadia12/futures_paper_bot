"""docker-compose.staging.yml: a separate V26 paper stack that cannot disturb production."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = (ROOT / "docker-compose.staging.yml").read_text()
PROD = (ROOT / "docker-compose.yml").read_text()


def _service(text: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S)", text, re.S | re.M)
    assert match, f"service {name} not found"
    return match.group(1)


def _env(block: str, key: str) -> str:
    match = re.search(rf'^\s+{key}: "?([^"\n]*)"?$', block, re.M)
    assert match, f"{key} not set"
    return match.group(1)


ENGINE, DASH = _service(STAGING, "engine"), _service(STAGING, "dashboard")


def test_own_project_name_so_production_is_never_recreated():
    assert re.search(r"^name: v25-staging$", STAGING, re.M)
    assert not re.search(r"^name:", PROD, re.M)


def test_own_ports_on_loopback_only():
    assert re.findall(r"^\s+ports: \[(.*)\]$", ENGINE, re.M) == ['"127.0.0.1:8001:8000"']
    assert re.findall(r"^\s+ports: \[(.*)\]$", DASH, re.M) == ['"127.0.0.1:8081:8080"']
    assert "caddy" not in STAGING


def test_own_database_volume():
    assert "staging-data:/data" in ENGINE and "engine-data" not in STAGING
    assert "volumes:" not in DASH and "docker.sock" not in STAGING


def test_paper_only_and_v26_only():
    for block in (ENGINE, DASH):
        assert _env(block, "PAPER_ONLY") == "true" and _env(block, "BROKER") == "paper"
        assert _env(block, "LIVE_TRADING_ENABLED") == "false" and _env(block, "TRADOVATE_ENABLED") == "false"
    assert _env(ENGINE, "ALLOWED_STRATEGY_VERSIONS") == "V26-webhook.1"
    assert _env(ENGINE, "ALLOWED_BOT_IDS") == "V26-MNQ-1"
    assert float(_env(ENGINE, "MIN_REWARD_RISK")) >= 2.0


def test_same_proxy_trust_and_risk_source_as_production():
    prod_engine = _service(PROD, "engine")
    assert _env(ENGINE, "TRUSTED_PROXIES") == _env(prod_engine, "TRUSTED_PROXIES")
    assert "env_file: .env" in ENGINE  # identical risk limits
    for key in ("MAX_CONTRACTS", "MAX_TRADE_RISK_USD", "COST_RESERVE_PER_CONTRACT_USD", "RISK_ENTRY_SLIPPAGE_TICKS"):
        assert key not in STAGING  # never overridden in staging


def test_dashboard_hardened_and_without_webhook_token():
    assert "read_only: true" in DASH and "cap_drop: [ALL]" in DASH and "no-new-privileges:true" in DASH
    assert "secrets: [admin_token, dashboard_password]" in DASH and _env(DASH, "WEBHOOK_TOKEN") == ""


def test_production_compose_still_v25_only():
    env_example = (ROOT / ".env.example").read_text()
    assert "ALLOWED_STRATEGY_VERSIONS=V25-webhook.1\n" in env_example
    assert "ALLOWED_BOT_IDS=V25-MNQ-1\n" in env_example
    assert "ALLOWED_STRATEGY_VERSIONS" not in _service(PROD, "engine")
