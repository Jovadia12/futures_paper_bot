"""Run with ``python -m v25bot``."""

import os

import uvicorn

from .api import create_app
from .config import Settings
from .logging_setup import setup_logging


def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        access_log=False,
        log_config=None,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
