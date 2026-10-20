"""Runtime configuration.

All settings come from environment variables. A secret may also be supplied as
``NAME_FILE=/run/secrets/name`` (Docker / Kubernetes secrets), which takes
precedence over ``NAME``. Secrets are held in ``SecretStr`` so they never
appear in ``repr()``, logs, or API responses.

Safety defaults: ``PAPER_ONLY=true``, broker ``paper``, Tradovate disabled.
"""

from __future__ import annotations

import os
from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

LIVE_ACK_PHRASE = "I UNDERSTAND THIS SENDS REAL ORDERS TO A REAL ACCOUNT"

# TradingView's published webhook source addresses.
TRADINGVIEW_WEBHOOK_IPS = (
    "52.89.214.238",
    "34.212.75.30",
    "54.218.53.128",
    "52.32.178.7",
)


class ConfigError(RuntimeError):
    """Raised when configuration is invalid or unsafe."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _secret(name: str) -> SecretStr | None:
    path = _env(f"{name}_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError as exc:  # never include file content in the message
            raise ConfigError(f"cannot read secret file for {name}: {exc.strerror}") from None
        return SecretStr(value) if value else None
    value = _env(name)
    return SecretStr(value) if value else None


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {value!r}")


def _float(name: str, default: float) -> float:
    value = _env(name)
    try:
        return float(value) if value is not None else default
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None


def _int(name: str, default: int) -> int:
    value = _env(name)
    try:
        return int(value) if value is not None else default
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def _list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = _env(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _time(name: str, default: str) -> time:
    value = _env(name, default)
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        raise ConfigError(f"{name} must be HH:MM") from None


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    # --- Mode / safety gates -------------------------------------------------
    paper_only: bool = True
    live_trading_enabled: bool = False
    live_trading_ack: str = ""
    broker: Literal["paper", "tradovate"] = "paper"

    # --- Webhook security ----------------------------------------------------
    webhook_token: SecretStr | None = None
    webhook_passphrase: SecretStr | None = None
    admin_token: SecretStr | None = None
    enforce_ip_allowlist: bool = False
    ip_allowlist: tuple[str, ...] = TRADINGVIEW_WEBHOOK_IPS
    trust_proxy_headers: bool = False
    trusted_proxies: tuple[str, ...] = ("127.0.0.1", "::1")
    max_body_bytes: int = 8192
    max_signal_age_seconds: float = 90.0
    max_future_skew_seconds: float = 15.0
    allowed_strategy_versions: tuple[str, ...] = ("V25-webhook.1",)
    allowed_bot_ids: tuple[str, ...] = ()
    allow_legacy_schema: bool = True

    # --- Storage / logging ---------------------------------------------------
    db_path: str = "data/v25bot.db"
    kill_switch_file: str = "data/KILL_SWITCH"
    log_level: str = "INFO"

    # --- Instrument ----------------------------------------------------------
    symbol_root: str = "MNQ"
    roll_days_before_expiry: int = 8
    min_days_to_expiry: int = 2
    allow_unverified_continuous_in_roll_window: bool = False

    # --- Risk engine ---------------------------------------------------------
    account_start_balance: float = 50_000.0
    max_contracts: int = 2
    max_trade_risk_usd: float = 200.0
    daily_loss_limit_usd: float = 450.0
    max_drawdown_usd: float = 2_000.0
    drawdown_mode: Literal["trailing_eod", "static", "intraday_trailing"] = "trailing_eod"
    drawdown_safety_buffer_usd: float = 400.0
    max_trades_per_day: int = 2
    risk_sizing_mode: Literal["reject", "reduce"] = "reject"
    cost_reserve_per_contract_usd: float = 8.0
    risk_entry_slippage_ticks: int = 2
    max_entry_deviation_ticks: int = 8
    # V26 only: minimum reward:risk an entry's liquidity target must offer (never below 2.0).
    min_reward_risk: float = 2.0
    entry_window_start: time = time(8, 30)
    entry_window_end: time = time(14, 30)
    flatten_time: time = time(14, 30)
    trading_day_roll_hour: int = 17
    holidays: tuple[date, ...] = ()
    account_state_max_age_seconds: float = 10.0
    max_quote_age_seconds: float = 90.0

    # --- Execution / supervision ----------------------------------------------
    protection_grace_seconds: float = 3.0
    monitor_interval_seconds: float = 1.0
    reconcile_interval_seconds: float = 5.0
    tv_exit_action: Literal["flatten", "log"] = "flatten"
    tv_heartbeat_max_age_seconds: float = 0.0
    flatten_retry_attempts: int = 3
    diagnostics_retention_days: int = 14

    # --- Paper simulator -----------------------------------------------------
    paper_commission_per_contract: float = 1.50
    paper_slippage_ticks: int = 2
    paper_book_depth: int = 5
    paper_limit_trade_through: bool = True

    # --- Alerts --------------------------------------------------------------
    alert_webhook_url: SecretStr | None = None
    alert_webhook_format: Literal["generic", "slack", "discord"] = "generic"

    # --- Tradovate (disabled by default) -------------------------------------
    tradovate_enabled: bool = False
    tradovate_env: Literal["demo", "live"] = "demo"
    tradovate_account_is_prop_or_evaluation: bool = True
    tradovate_username: SecretStr | None = None
    tradovate_password: SecretStr | None = None
    tradovate_app_id: str = ""
    tradovate_app_version: str = "1.0"
    tradovate_cid: SecretStr | None = None
    tradovate_secret: SecretStr | None = None
    tradovate_device_id: str = ""
    tradovate_account_spec: str = ""

    # ------------------------------------------------------------------------
    @field_validator("max_contracts", "max_trades_per_day", "paper_book_depth")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    @field_validator("min_reward_risk")
    @classmethod
    def _min_rr(cls, v: float) -> float:
        if not v >= 2.0:
            raise ValueError("MIN_REWARD_RISK must be >= 2.0 (the V26 strategy requires at least 2:1)")
        return v

    @field_validator("diagnostics_retention_days")
    @classmethod
    def _retention(cls, v: int) -> int:
        if v < 1:
            raise ValueError("DIAGNOSTICS_RETENTION_DAYS must be >= 1")
        return v

    @classmethod
    def from_env(cls) -> "Settings":
        holidays: list[date] = []
        for item in _list("HOLIDAYS"):
            try:
                holidays.append(date.fromisoformat(item))
            except ValueError:
                raise ConfigError(f"HOLIDAYS entry {item!r} is not YYYY-MM-DD") from None
        try:
            settings = cls(
                paper_only=_bool("PAPER_ONLY", True),
                live_trading_enabled=_bool("LIVE_TRADING_ENABLED", False),
                live_trading_ack=_env("LIVE_TRADING_ACK", "") or "",
                broker=(_env("BROKER", "paper") or "paper").lower(),
                webhook_token=_secret("WEBHOOK_TOKEN"),
                webhook_passphrase=_secret("WEBHOOK_PASSPHRASE"),
                admin_token=_secret("ADMIN_TOKEN"),
                enforce_ip_allowlist=_bool("ENFORCE_IP_ALLOWLIST", False),
                ip_allowlist=_list("IP_ALLOWLIST", TRADINGVIEW_WEBHOOK_IPS),
                trust_proxy_headers=_bool("TRUST_PROXY_HEADERS", False),
                trusted_proxies=_list("TRUSTED_PROXIES", ("127.0.0.1", "::1")),
                max_body_bytes=_int("MAX_BODY_BYTES", 8192),
                max_signal_age_seconds=_float("MAX_SIGNAL_AGE_SECONDS", 90.0),
                max_future_skew_seconds=_float("MAX_FUTURE_SKEW_SECONDS", 15.0),
                allowed_strategy_versions=_list("ALLOWED_STRATEGY_VERSIONS", ("V25-webhook.1",)),
                allowed_bot_ids=_list("ALLOWED_BOT_IDS"),
                allow_legacy_schema=_bool("ALLOW_LEGACY_SCHEMA", True),
                db_path=_env("DB_PATH", "data/v25bot.db"),
                kill_switch_file=_env("KILL_SWITCH_FILE", "data/KILL_SWITCH"),
                log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
                symbol_root=(_env("SYMBOL_ROOT", "MNQ") or "MNQ").upper(),
                roll_days_before_expiry=_int("ROLL_DAYS_BEFORE_EXPIRY", 8),
                min_days_to_expiry=_int("MIN_DAYS_TO_EXPIRY", 2),
                allow_unverified_continuous_in_roll_window=_bool(
                    "ALLOW_UNVERIFIED_CONTINUOUS_IN_ROLL_WINDOW", False
                ),
                account_start_balance=_float("ACCOUNT_START_BALANCE", 50_000.0),
                max_contracts=_int("MAX_CONTRACTS", 2),
                max_trade_risk_usd=_float("MAX_TRADE_RISK_USD", 200.0),
                daily_loss_limit_usd=_float("DAILY_LOSS_LIMIT_USD", 450.0),
                max_drawdown_usd=_float("MAX_DRAWDOWN_USD", 2_000.0),
                drawdown_mode=(_env("DRAWDOWN_MODE", "trailing_eod") or "trailing_eod").lower(),
                drawdown_safety_buffer_usd=_float("DRAWDOWN_SAFETY_BUFFER_USD", 400.0),
                max_trades_per_day=_int("MAX_TRADES_PER_DAY", 2),
                risk_sizing_mode=(_env("RISK_SIZING_MODE", "reject") or "reject").lower(),
                cost_reserve_per_contract_usd=_float("COST_RESERVE_PER_CONTRACT_USD", 8.0),
                risk_entry_slippage_ticks=_int("RISK_ENTRY_SLIPPAGE_TICKS", 2),
                max_entry_deviation_ticks=_int("MAX_ENTRY_DEVIATION_TICKS", 8),
                min_reward_risk=_float("MIN_REWARD_RISK", 2.0),
                entry_window_start=_time("ENTRY_WINDOW_START", "08:30"),
                entry_window_end=_time("ENTRY_WINDOW_END", "14:30"),
                flatten_time=_time("FLATTEN_TIME", "14:30"),
                trading_day_roll_hour=_int("TRADING_DAY_ROLL_HOUR", 17),
                holidays=tuple(holidays),
                account_state_max_age_seconds=_float("ACCOUNT_STATE_MAX_AGE_SECONDS", 10.0),
                max_quote_age_seconds=_float("MAX_QUOTE_AGE_SECONDS", 90.0),
                protection_grace_seconds=_float("PROTECTION_GRACE_SECONDS", 3.0),
                monitor_interval_seconds=_float("MONITOR_INTERVAL_SECONDS", 1.0),
                reconcile_interval_seconds=_float("RECONCILE_INTERVAL_SECONDS", 5.0),
                tv_exit_action=(_env("TV_EXIT_ACTION", "flatten") or "flatten").lower(),
                tv_heartbeat_max_age_seconds=_float("TV_HEARTBEAT_MAX_AGE_SECONDS", 0.0),
                flatten_retry_attempts=_int("FLATTEN_RETRY_ATTEMPTS", 3),
                diagnostics_retention_days=_int("DIAGNOSTICS_RETENTION_DAYS", 14),
                paper_commission_per_contract=_float("PAPER_COMMISSION_PER_CONTRACT", 1.50),
                paper_slippage_ticks=_int("PAPER_SLIPPAGE_TICKS", 2),
                paper_book_depth=_int("PAPER_BOOK_DEPTH", 5),
                paper_limit_trade_through=_bool("PAPER_LIMIT_TRADE_THROUGH", True),
                alert_webhook_url=_secret("ALERT_WEBHOOK_URL"),
                alert_webhook_format=(_env("ALERT_WEBHOOK_FORMAT", "generic") or "generic").lower(),
                tradovate_enabled=_bool("TRADOVATE_ENABLED", False),
                tradovate_env=(_env("TRADOVATE_ENV", "demo") or "demo").lower(),
                tradovate_account_is_prop_or_evaluation=_bool(
                    "TRADOVATE_ACCOUNT_IS_PROP_OR_EVALUATION", True
                ),
                tradovate_username=_secret("TRADOVATE_USERNAME"),
                tradovate_password=_secret("TRADOVATE_PASSWORD"),
                tradovate_app_id=_env("TRADOVATE_APP_ID", "") or "",
                tradovate_app_version=_env("TRADOVATE_APP_VERSION", "1.0") or "1.0",
                tradovate_cid=_secret("TRADOVATE_CID"),
                tradovate_secret=_secret("TRADOVATE_SECRET"),
                tradovate_device_id=_env("TRADOVATE_DEVICE_ID", "") or "",
                tradovate_account_spec=_env("TRADOVATE_ACCOUNT_SPEC", "") or "",
            )
        except ValueError as exc:
            raise ConfigError(f"invalid configuration: {exc}") from None
        settings.validate_safety()
        return settings

    # ------------------------------------------------------------------------
    def live_gate_failures(self) -> list[str]:
        """Every reason live (real-money) execution is not permitted."""
        reasons: list[str] = []
        if self.paper_only:
            reasons.append("PAPER_ONLY=true")
        if not self.live_trading_enabled:
            reasons.append("LIVE_TRADING_ENABLED is not true")
        if self.live_trading_ack != LIVE_ACK_PHRASE:
            reasons.append("LIVE_TRADING_ACK does not match the required acknowledgment phrase")
        if self.broker != "tradovate":
            reasons.append("BROKER is not a live-capable adapter")
        if not self.tradovate_enabled:
            reasons.append("TRADOVATE_ENABLED is not true")
        if self.tradovate_account_is_prop_or_evaluation:
            reasons.append(
                "TRADOVATE_ACCOUNT_IS_PROP_OR_EVALUATION=true: Tradovate states prop/evaluation "
                "accounts are not eligible for API access"
            )
        if self.allow_legacy_schema:
            reasons.append("ALLOW_LEGACY_SCHEMA must be false for live trading")
        return reasons

    @property
    def live_allowed(self) -> bool:
        return not self.live_gate_failures()

    @property
    def mode(self) -> str:
        return "PAPER_ONLY" if self.paper_only else ("LIVE" if self.live_allowed else "BROKER_SIM")

    def validate_safety(self) -> None:
        if self.paper_only and self.broker != "paper":
            raise ConfigError(
                "PAPER_ONLY=true permits only BROKER=paper. Broker execution stays disabled "
                "until PAPER_ONLY=false and every live gate is satisfied."
            )
        if self.broker == "tradovate":
            if not self.tradovate_enabled:
                raise ConfigError("BROKER=tradovate requires TRADOVATE_ENABLED=true")
            if self.tradovate_account_is_prop_or_evaluation:
                raise ConfigError(
                    "Tradovate adapter refused: prop-firm/evaluation accounts (e.g. Lucid) are "
                    "not eligible for Tradovate API access. See docs/TRADOVATE_LUCID.md."
                )
            if self.tradovate_env == "live" and not self.live_allowed:
                raise ConfigError(
                    "TRADOVATE_ENV=live refused: " + "; ".join(self.live_gate_failures())
                )
        if self.entry_window_start >= self.entry_window_end:
            raise ConfigError("ENTRY_WINDOW_START must be before ENTRY_WINDOW_END")
        if self.flatten_time < self.entry_window_end:
            raise ConfigError("FLATTEN_TIME must not be before ENTRY_WINDOW_END")
        for name in ("webhook_token", "admin_token"):
            secret: SecretStr | None = getattr(self, name)
            if secret is not None and len(secret.get_secret_value()) < 24:
                raise ConfigError(f"{name.upper()} must be at least 24 characters")

    def secret_values(self) -> list[str]:
        """Raw secret values, used only to register log redaction."""
        values = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                if raw:
                    values.append(raw)
        return values

