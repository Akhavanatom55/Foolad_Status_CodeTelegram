from __future__ import annotations

import logging

from telegram import Update

from bot import build_application
from config import Settings
from health import start_health_server

logger = logging.getLogger("exam-monitor")


def main() -> None:
    settings = Settings.from_env()
    settings.ensure_storage_dir()
    start_health_server(settings.port)

    application = build_application(settings)
    logger.info("Starting Telegram polling process")
    # run_polling() manages its own event loop (do NOT wrap this in
    # asyncio.run()) and already deletes any leftover webhook for us before
    # the first getUpdates call.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
