from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("exam-monitor.config")


def _csv_ints(value: str) -> FrozenSet[int]:
    out: set[int] = set()
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.add(int(item))
        except ValueError:
            raise RuntimeError(f"Invalid integer in list environment variable: {item!r}")
    return frozenset(out)


def _csv_strings(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in (value or "").split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    bale_token: str
    bale_api_base: str
    admin_ids: FrozenSet[int]
    allowed_group_ids: FrozenSet[int]
    lms_url: str
    lms_token: str
    lms_timezone: str
    status_date_mode: str
    db_path: str
    port: int
    max_excel_file_mb: int
    request_timeout: int
    upload_timeout: int
    operation_timeout: int
    max_retries: int
    safety_general_patterns: tuple[str, ...]
    safety_special_patterns: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        bale_token = os.getenv("BALE_TOKEN", "").strip()
        if not bale_token:
            raise RuntimeError("BALE_TOKEN is required")

        lms_url = os.getenv("LMS_URL", "").strip().rstrip("/")
        if not lms_url:
            raise RuntimeError("LMS_URL is required")
        if not lms_url.lower().startswith(("http://", "https://")):
            raise RuntimeError(
                f"LMS_URL must start with http:// or https:// (got {lms_url!r}). "
                "Without a scheme, requests to Moodle fail with a confusing "
                "'InvalidURL' error that just prints the bare host name."
            )

        lms_token = os.getenv("LMS_TOKEN", "").strip()
        if not lms_token:
            raise RuntimeError("LMS_TOKEN is required")

        admin_ids = _csv_ints(os.getenv("ADMIN_IDS", ""))
        if not admin_ids:
            raise RuntimeError("ADMIN_IDS must contain at least one numeric Telegram/Bale user id")

        status_date_mode = os.getenv("STATUS_DATE_MODE", "open_time").strip().lower()
        if status_date_mode not in {"open_time", "created_time", "either"}:
            raise RuntimeError("STATUS_DATE_MODE must be open_time, created_time, or either")

        db_path = os.getenv("DB_PATH", "data/exam_monitor.db").strip()

        return cls(
            bale_token=bale_token,
            bale_api_base=os.getenv("BALE_API_BASE", "https://tapi.bale.ai").rstrip("/"),
            admin_ids=admin_ids,
            allowed_group_ids=_csv_ints(os.getenv("ALLOWED_GROUP_IDS", "")),
            lms_url=lms_url,
            lms_token=lms_token,
            lms_timezone=os.getenv("LMS_TIMEZONE", "Asia/Tehran").strip(),
            status_date_mode=status_date_mode,
            db_path=db_path,
            port=int(os.getenv("PORT", "8080")),
            max_excel_file_mb=int(os.getenv("MAX_EXCEL_FILE_MB", "20")),
            request_timeout=int(os.getenv("LMS_REQUEST_TIMEOUT", "75")),
            upload_timeout=int(os.getenv("LMS_UPLOAD_TIMEOUT", "600")),
            operation_timeout=int(os.getenv("LMS_OPERATION_TIMEOUT", "300")),
            max_retries=int(os.getenv("LMS_MAX_RETRIES", "2")),
            safety_general_patterns=_csv_strings(os.getenv("SAFETY_GENERAL_PATTERNS", "ایمنی عمومی")) or ("ایمنی عمومی",),
            safety_special_patterns=_csv_strings(os.getenv("SAFETY_SPECIAL_PATTERNS", "ایمنی تخصصی")) or ("ایمنی تخصصی",),
        )

    def ensure_storage_dir(self) -> None:
        """Make sure the directory that will hold the sqlite db actually exists
        and is writable, falling back to a temp directory instead of crashing
        the whole bot when it isn't.

        On Belmo (and most similar PaaS platforms) the deployed app directory
        is mounted read-only at runtime; only a dedicated persistent
        volume/disk (if you attach one) or the OS temp directory is writable.
        `Path.mkdir(..., exist_ok=True)` does NOT raise if the directory
        already exists even when the filesystem is read-only, so we also do a
        real write test to catch that case.
        """
        target_dir = Path(self.db_path).parent

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            probe = target_dir / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            return
        except OSError as exc:
            fallback_dir = Path(tempfile.gettempdir()) / "exam-monitor"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            fallback_path = str(fallback_dir / Path(self.db_path).name)

            logger.warning(
                "DB_PATH directory '%s' is not writable (%s). Falling back to "
                "'%s' for now so the bot can still start, but anything stored "
                "there (report history, duplicate-import protection) will be "
                "lost on the next restart or redeploy. To fix this "
                "permanently, attach a persistent volume/disk to this Belmo "
                "service and set the DB_PATH environment variable to a file "
                "path inside it.",
                target_dir,
                exc,
                fallback_path,
            )
            # Settings is a frozen dataclass; update db_path in place so every
            # caller (main.py and bot.py) that reads settings.db_path after
            # this call picks up the writable fallback location.
            object.__setattr__(self, "db_path", fallback_path)
