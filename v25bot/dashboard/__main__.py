"""Run with ``python -m v25bot.dashboard``."""

import uvicorn

from ..logging_setup import setup_logging
from .app import create_dashboard_app
from .config import DashboardSettings


def main() -> None:
    settings = DashboardSettings.from_env()
    setup_logging("INFO")
    uvicorn.run(create_dashboard_app(settings), host=settings.host, port=settings.port,
                access_log=False, log_config=None, proxy_headers=False)


if __name__ == "__main__":
    main()
