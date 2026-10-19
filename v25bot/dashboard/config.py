"""Dashboard settings (environment variables; secrets also accept NAME_FILE)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic import SecretStr

from ..config import ConfigError, _bool, _env, _float, _int, _list, _secret


@dataclass(frozen=True)
class DashboardSettings:
    engine_url: str = "http://127.0.0.1:8000"
    admin_token: SecretStr | None = None
    password: SecretStr | None = None
    host: str = "127.0.0.1"
    port: int = 8080
    session_hours: float = 12.0
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "*.ts.net")
    refresh_seconds: float = 4.0
    tv_live_seconds: float = 150.0
    history_cache_seconds: float = 5.0
    in_docker: bool = False
    max_login_failures: int = 5
    lockout_seconds: float = 300.0

    @classmethod
    def from_env(cls) -> "DashboardSettings":
        s = cls(
            engine_url=(_env("ENGINE_URL", "http://127.0.0.1:8000") or "").rstrip("/"),
            admin_token=_secret("ADMIN_TOKEN"),
            password=_secret("DASHBOARD_PASSWORD"),
            host=_env("DASHBOARD_HOST", "127.0.0.1") or "127.0.0.1",
            port=_int("DASHBOARD_PORT", 8080),
            session_hours=_float("DASHBOARD_SESSION_HOURS", 12.0),
            allowed_hosts=_list("DASHBOARD_ALLOWED_HOSTS", ("127.0.0.1", "localhost", "*.ts.net")),
            refresh_seconds=_float("DASHBOARD_REFRESH_SECONDS", 4.0),
            tv_live_seconds=_float("DASHBOARD_TV_LIVE_SECONDS", 150.0),
            in_docker=_bool("DASHBOARD_IN_DOCKER", False) or os.path.exists("/.dockerenv"),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.password is not None and len(self.password.get_secret_value()) < 12:
            raise ConfigError("DASHBOARD_PASSWORD must be at least 12 characters")
        if not 3 <= self.refresh_seconds <= 30:
            raise ConfigError("DASHBOARD_REFRESH_SECONDS must be between 3 and 30")
        if "*" in self.allowed_hosts:
            raise ConfigError("DASHBOARD_ALLOWED_HOSTS must not contain a bare '*'")
