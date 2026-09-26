from __future__ import annotations

import logging
import threading
from aiohttp import web

logger = logging.getLogger("exam-monitor.health")


def _health_app() -> web.Application:
    app = web.Application()

    async def root(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "service": "exam-monitor-telegram",
            }
        )

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    return app


def _run_server(port: int) -> None:
    logger.info("Health server listening on 0.0.0.0:%s", port)
    web.run_app(
        _health_app(),
        host="0.0.0.0",
        port=port,
        handle_signals=False,
        access_log=logger,
    )


def start_health_server(port: int) -> threading.Thread:
    thread = threading.Thread(
        target=_run_server,
        args=(port,),
        name="health-server",
        daemon=True,
    )
    thread.start()
    return thread
