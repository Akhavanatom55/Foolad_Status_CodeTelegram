from __future__ import annotations

import asyncio
import logging

from bot import build_bot
from config import Settings
from health import start_health_server

logger = logging.getLogger("exam-monitor")


def main() -> None:
    # Belmo API services expose PORT. The lightweight HTTP server keeps the
    # service reachable while the Bale polling process runs in the foreground.
    settings = Settings.from_env()
    settings.ensure_storage_dir()
    start_health_server(settings.port)

    app = asyncio.run(build_bot(settings))
    logger.info("Starting Bale polling process")
    app.run()


if __name__ == "__main__":
    main()
