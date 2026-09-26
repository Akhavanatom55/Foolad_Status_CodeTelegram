from __future__ import annotations

import asyncio
import os
import socket
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger("exam-monitor.moodle")


class MoodleError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        function: str | None = None,
        elapsed: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.function = function
        self.elapsed = elapsed


class MoodleClient:
    """Small, defensive Moodle REST client.

    The bot does not need core_webservice_get_site_info to operate.  Health now
    uses the plugin's lightweight local_exammonitor_ping function so a failure
    in the larger core site-info response cannot falsely report the integration
    as down.
    """

    RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})

    def __init__(
        self,
        base_url: str,
        token: str,
        request_timeout: int = 60,
        upload_timeout: int = 600,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.request_timeout = max(5, int(request_timeout))
        self.upload_timeout = max(30, int(upload_timeout))
        # Cap retries; 504 from Cloudflare often needs one quick retry after origin recovers.
        self.max_retries = max(0, min(int(max_retries), 3))

    @property
    def server_endpoint(self) -> str:
        return f"{self.base_url}/webservice/rest/server.php"

    @property
    def upload_endpoint(self) -> str:
        return f"{self.base_url}/webservice/upload.php"

    @staticmethod
    def _retryable_network(exc: BaseException) -> bool:
        return isinstance(
            exc,
            (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
                asyncio.TimeoutError,
            ),
        )

    @classmethod
    def _is_retryable_status(cls, status: int) -> bool:
        return status in cls.RETRYABLE_HTTP_STATUSES

    @staticmethod
    def _diagnostic_headers(response: aiohttp.ClientResponse) -> str:
        values = []
        for name in (
            "server",
            "cf-ray",
            "cf-cache-status",
            "cf-error-type",
            "cf-error-origin",
            "retry-after",
        ):
            value = response.headers.get(name)
            if value:
                values.append(f"{name}={value}")
        return " | ".join(values)

    @staticmethod
    def _compact_error(status: int, raw: str) -> str:
        body = raw[:1800].replace("\x00", " ")
        lowered = raw.lower()
        if status in {502, 503, 504} and "cdn-cgi/" in lowered:
            return (
                f"HTTP {status} from Moodle: Cloudflare/reverse-proxy returned a gateway error "
                "before Moodle produced a REST response. "
                f"Response preview: {body}"
            )
        return f"HTTP {status} from Moodle: {body}"

    def _connector(self) -> aiohttp.TCPConnector:
        """Optionally force IPv4 when LMS_FORCE_IPV4=1 (helps broken IPv6 routes)."""
        import os
        force_v4 = os.getenv("LMS_FORCE_IPV4", "").strip().lower() in {"1", "true", "yes", "on"}
        if force_v4:
            return aiohttp.TCPConnector(family=socket.AF_INET, ssl=True)
        return aiohttp.TCPConnector(ssl=True)


    async def call(self, function: str, **params: Any) -> Any:
        payload = {
            "wstoken": self.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
        }
        payload.update(params)
        timeout = aiohttp.ClientTimeout(
            total=self.request_timeout,
            connect=min(20, self.request_timeout),
            sock_connect=min(20, self.request_timeout),
            sock_read=self.request_timeout,
        )

        last_exc: Exception | None = None
        headers = {
            "User-Agent": "ExamMonitorBot/2.2",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        }

        async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=self._connector()) as session:
            for attempt in range(self.max_retries + 1):
                started = time.monotonic()
                try:
                    async with session.post(self.server_endpoint, data=payload) as response:
                        raw = await response.text()
                        elapsed = time.monotonic() - started
                        logger.info(
                            "Moodle API function=%s status=%s elapsed=%.2fs attempt=%d/%d",
                            function,
                            response.status,
                            elapsed,
                            attempt + 1,
                            self.max_retries + 1,
                        )

                        if response.status >= 400:
                            details = self._diagnostic_headers(response)
                            message = self._compact_error(response.status, raw)
                            if details:
                                message += f" | headers: {details}"
                            err = MoodleError(
                                message,
                                status=response.status,
                                function=function,
                                elapsed=elapsed,
                            )
                            if attempt < self.max_retries and self._is_retryable_status(response.status):
                                last_exc = err
                                wait = 2.0 * (attempt + 1)
                                logger.warning(
                                    "Retrying Moodle function=%s after HTTP %s in %.1fs",
                                    function,
                                    response.status,
                                    wait,
                                )
                                await asyncio.sleep(wait)
                                continue
                            raise err

                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise MoodleError(
                                f"Invalid JSON from Moodle function {function}: {raw[:1200]}",
                                status=response.status,
                                function=function,
                                elapsed=elapsed,
                            ) from exc

                    if isinstance(data, dict) and "exception" in data:
                        raise MoodleError(
                            f"{data.get('errorcode', 'moodle_error')}: "
                            f"{data.get('message', 'Moodle returned an error')}",
                            status=200,
                            function=function,
                            elapsed=elapsed,
                        )
                    return data

                except MoodleError as exc:
                    last_exc = exc
                    logger.error(
                        "Moodle API failed function=%s attempt=%d/%d status=%s elapsed=%s: %s",
                        function,
                        attempt + 1,
                        self.max_retries + 1,
                        exc.status,
                        f"{exc.elapsed:.2f}s" if exc.elapsed is not None else "-",
                        exc,
                    )
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    last_exc = exc
                    elapsed = time.monotonic() - started
                    logger.warning(
                        "Moodle network failure function=%s attempt=%d/%d elapsed=%.2fs: %s",
                        function,
                        attempt + 1,
                        self.max_retries + 1,
                        elapsed,
                        exc,
                    )
                    if attempt < self.max_retries and self._retryable_network(exc):
                        await asyncio.sleep(2.0 * (attempt + 1))
                        continue
                    raise MoodleError(
                        f"Network/timeout error calling Moodle function {function}: {exc}",
                        function=function,
                        elapsed=elapsed,
                    ) from exc

        raise MoodleError(str(last_exc) if last_exc else "Moodle request failed", function=function)

    async def health(self) -> dict[str, Any]:
        """Use the plugin's tiny ping, not the heavier core site-info function.

        A 504 here means Cloudflare/reverse-proxy never got a response from
        PHP-FPM/Moodle — not a bad Bale token. The plugin must be installed,
        the WS token must belong to the Exam Monitor service, and the origin
        must answer within the proxy timeout (~15s on many Cloudflare setups).
        """
        result = await self.call("local_exammonitor_ping")
        if not isinstance(result, dict) or not result.get("ok"):
            raise MoodleError(
                f"Unexpected health response from Moodle: {result!r}",
                function="local_exammonitor_ping",
            )
        return result

    async def site_info(self) -> dict[str, Any]:
        """Optional diagnostic; not used as the bot's health check."""
        return await self.call("core_webservice_get_site_info")

    async def get_status(self, date_start: int, date_end: int, mode: str) -> dict[str, Any]:
        return await self.call(
            "local_exammonitor_get_status",
            date_start=date_start,
            date_end=date_end,
            datemode=mode,
        )

    async def find_course(self, shortname: str) -> dict[str, Any]:
        return await self.call("local_exammonitor_find_course", shortname=shortname)

    async def get_unused_draft_itemid(self) -> int:
        result = await self.call("core_files_get_unused_draft_itemid")
        if not isinstance(result, dict) or "itemid" not in result:
            raise MoodleError(
                f"Unexpected draft item response: {result!r}",
                function="core_files_get_unused_draft_itemid",
            )
        return int(result["itemid"])

    async def upload_to_draft(self, file_path: Path, draft_itemid: int, filename: str) -> dict[str, Any]:
        if not file_path.is_file():
            raise MoodleError(f"Upload source file not found: {file_path}")
        timeout = aiohttp.ClientTimeout(
            total=self.upload_timeout,
            connect=min(60, self.upload_timeout),
            sock_connect=min(60, self.upload_timeout),
            sock_read=self.upload_timeout,
        )
        last_exc: Exception | None = None
        headers = {"User-Agent": "ExamMonitorBot/2.2", "Accept": "application/json"}

        for attempt in range(self.max_retries + 1):
            try:
                data = aiohttp.FormData()
                data.add_field("token", self.token)
                data.add_field("itemid", str(draft_itemid))
                data.add_field("filepath", "/")
                with file_path.open("rb") as fp:
                    data.add_field(
                        "file_1",
                        fp,
                        filename=filename,
                        content_type="application/xml",
                    )
                    async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=self._connector()) as session:
                        async with session.post(self.upload_endpoint, data=data) as response:
                            raw = await response.text()
                            if response.status >= 400:
                                details = self._diagnostic_headers(response)
                                message = f"Upload HTTP {response.status}: {raw[:1400]}"
                                if details:
                                    message += f" | headers: {details}"
                                err = MoodleError(
                                    message,
                                    status=response.status,
                                    function="/webservice/upload.php",
                                )
                                if attempt < self.max_retries and self._is_retryable_status(response.status):
                                    last_exc = err
                                    await asyncio.sleep(2.0 * (attempt + 1))
                                    continue
                                raise err
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError as exc:
                                raise MoodleError(
                                    f"Invalid JSON from Moodle upload: {raw[:1200]}",
                                    status=response.status,
                                    function="/webservice/upload.php",
                                ) from exc

                if isinstance(payload, list) and payload and isinstance(payload[0], dict) and payload[0].get("error"):
                    raise MoodleError(str(payload[0]), function="/webservice/upload.php")
                if not isinstance(payload, list):
                    raise MoodleError(
                        f"Unexpected upload response: {payload!r}",
                        function="/webservice/upload.php",
                    )
                return {"itemid": draft_itemid, "files": payload}
            except (aiohttp.ClientError, asyncio.TimeoutError, MoodleError) as exc:
                last_exc = exc
                if attempt < self.max_retries and isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError)):
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                raise
        raise MoodleError(str(last_exc) if last_exc else "Moodle upload failed")

    async def import_xml_to_quiz(
        self,
        *,
        quizid: int,
        course_shortname: str,
        draft_itemid: int,
        filename: str,
    ) -> dict[str, Any]:
        return await self.call(
            "local_exammonitor_import_xml",
            quizid=quizid,
            course_shortname=course_shortname,
            draftitemid=draft_itemid,
            filename=filename,
        )
