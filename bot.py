from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from bale import Bot, CallbackQuery, Message
from dotenv import load_dotenv

from calendar_utils import (
    day_bounds_timestamps,
    expected_quiz_timestamps,
    jalali_label,
    now_local,
    parse_date_input,
    today_gregorian,
)
from config import Settings
from db import Database
from keyboards import admin_panel, add_questions_button, confirm_course, schedule_menu
from moodle import MoodleClient, MoodleError
from network_diagnostic import NetworkDiagnostic, render_result, render_summary
from rules import CheckResult, build_checks
from converter import convert, ExcelReadError

_SCHEDULE_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

HELP_TEXT = (
    "/admin — پنل مدیریت\n"
    "/diag — تست کامل سرور (شبکه تا LMS)\n"
    "/pinglms — تست سریع Moodle REST\n"
    "/status — بررسی آزمون‌های امروز\n"
    "/status YYYY/MM/DD — بررسی یک تاریخ\n"
    "/cancel — لغو عملیات جاری\n"
    "/id — نمایش شناسه کاربر و گروه\n\n"
    "از پنل /admin می‌توانید گزارش امروز/فردا/یک تاریخ دلخواه را بگیرید، آخرین "
    "گزارش و تاریخچه را ببینید، تست کامل سرور و اتصال LMS را انجام دهید و یک ساعت مشخص برای "
    "اعلام خودکار روزانهٔ وضعیت آزمون‌ها در همین گروه تنظیم کنید."
)


class _ChatProxy:
    """Minimal message-like adapter so run_status()/send() can target a chat
    directly (used for scheduled broadcasts, where there is no incoming
    Message to reply to)."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self.chat_id = chat_id

    async def reply(self, text: str, **kwargs: Any):
        return await self._bot.send_message(self.chat_id, text, **kwargs)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("exam-monitor")


class ExamMonitorBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_storage_dir()
        self.db = Database(settings.db_path)
        self.moodle = MoodleClient(
            settings.lms_url,
            settings.lms_token,
            settings.request_timeout,
            settings.upload_timeout,
            settings.max_retries,
        )
        self.bot = Bot(settings.bale_token)
        self._schedule_running: set[int] = set()
        self._server_diagnostic_running: set[int] = set()

    async def initialize(self) -> None:
        await self.db.initialize()

    def _allowed_group(self, chat_id: int) -> bool:
        return not self.settings.allowed_group_ids or chat_id in self.settings.allowed_group_ids

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids

    def _is_private_chat(self, message_or_chat) -> bool:
        """True when the conversation is a 1:1 private chat (not a group)."""
        chat = getattr(message_or_chat, "chat", message_or_chat)
        chat_type = str(getattr(chat, "type", "") or "").lower()
        if chat_type in {"private", "pv", "user"}:
            return True
        # Bale sometimes omits type; private chats have chat_id == user id.
        return False

    def _can_admin_interact(self, chat_id: int, user_id: int, *, is_private: bool = False) -> bool:
        """Admins may use the bot in allowed groups AND in their private chat with the bot.

        Non-admins never get the panel. Group messages are still restricted to
        ALLOWED_GROUP_IDS (when configured). Private chats are only opened for
        users listed in ADMIN_IDS.
        """
        if not self._is_admin(user_id):
            return False
        if is_private:
            return True
        return self._allowed_group(chat_id)

    async def safe_callback_answer(self, callback: CallbackQuery) -> None:
        try:
            answer = getattr(callback, "answer", None)
            if callable(answer):
                result = answer()
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            pass

    async def send(self, message: Message, text: str, **kwargs: Any) -> None:
        # Keep component failures visible in the logs instead of silently
        # dropping the inline keyboard. python-bale-bot 2.5.0 expects
        # components=InlineKeyboardMarkup(...).
        await message.reply(text, **kwargs)

    async def on_ready(self) -> None:
        logger.info("Bale bot ready as %s", self.bot.user)
        if getattr(self, "_scheduler_task", None) is None:
            self._scheduler_task = asyncio.create_task(self.scheduler_loop())

    async def on_before_ready(self) -> None:
        try:
            await self.bot.delete_webhook()
        except Exception:
            pass

    async def handle_admin_command(self, message: Message) -> None:
        chat_id = int(message.chat_id)
        user_id = int(message.author.id)
        is_private = self._is_private_chat(message) or (chat_id == user_id)
        if not self._can_admin_interact(chat_id, user_id, is_private=is_private):
            return
        await self.send(
            message,
            "🛠 پنل مدیریت بررسی آزمون‌ها\n\n"
            "یکی از دکمه‌ها را بزنید، یا دستور زیر را بفرستید:\n"
            "• /diag  ← تست کامل سرور (شبکه Belmo تا LMS)\n"
            "• /pinglms  ← تست سریع Moodle REST\n\n"
            "اگر دکمه‌ها دیده نشدند حتماً از /diag استفاده کنید.",
            components=admin_panel(),
        )

    async def handle_status_command(self, message: Message, argument: str = "") -> None:
        chat_id = int(message.chat_id)
        user_id = int(message.author.id)
        is_private = self._is_private_chat(message) or (chat_id == user_id)
        if not self._can_admin_interact(chat_id, user_id, is_private=is_private):
            return
        try:
            if argument.strip():
                gdate, jlabel = parse_date_input(argument.strip(), self.settings.lms_timezone)
            else:
                gdate = today_gregorian(self.settings.lms_timezone)
                jlabel = jalali_label(gdate)
            await self.run_status(message, gdate, jlabel, user_id)
        except ValueError as exc:
            await self.send(message, f"⚠️ {exc}")

    async def run_status(self, message: Message, gdate, jlabel: str, user_id: int) -> bool:
        chat_id = int(message.chat_id)
        day_start, day_end = day_bounds_timestamps(gdate, self.settings.lms_timezone)
        expected_open, expected_close = expected_quiz_timestamps(gdate, self.settings.lms_timezone)
        await self.send(message, f"⏳ در حال بررسی آزمون‌های تاریخ {jlabel} ...")
        try:
            payload = await asyncio.wait_for(
                self.moodle.get_status(day_start, day_end, self.settings.status_date_mode),
                timeout=self.settings.operation_timeout,
            )
        except MoodleError as exc:
            logger.exception("Status request failed")
            if getattr(exc, "status", None) in {502, 503, 504}:
                await self.send(
                    message,
                    "❌ LMS از مسیر Web Service پاسخ نداد (Gateway error).\n\n"
                    f"🔧 تابع: {getattr(exc, 'function', 'local_exammonitor_get_status')}\n"
                    f"⏱️ زمان پاسخ: {getattr(exc, 'elapsed', 0) or 0:.1f} ثانیه\n\n"
                    "این پاسخ از Reverse Proxy/Cloudflare یا سرور Moodle برگشته است. "
                    "اگر «تست اتصال LMS» هم خطا داد، باید وب‌سرور/PHP-FPM/Cloudflare خود LMS بررسی شود.\n\n"
                    f"جزئیات: {str(exc)[:1600]}"
                )
            else:
                await self.send(message, f"❌ ارتباط با LMS برقرار نشد یا API خطا داد.\n\nجزئیات: {str(exc)[:1600]}")
            return False
        except Exception as exc:
            logger.exception("Status request failed")
            await self.send(message, f"❌ ارتباط با LMS برقرار نشد یا API خطا داد.\n\nجزئیات: {str(exc)[:1600]}")
            return False

        quizzes = payload.get("quizzes", []) if isinstance(payload, dict) else []
        if not quizzes:
            summary = {"date": jlabel, "quizzes": [], "total": 0, "problematic": 0, "warning": 1}
            await self.db.save_status_run(chat_id, user_id, jlabel, gdate.isoformat(), summary)
            await self.send(message, f"⚠️ برای تاریخ {jlabel} هیچ آزمونی پیدا نشد.\n\nمبنای جستجو: {self.settings.status_date_mode}")
            return True

        total = 0
        problematic = 0
        warning_count = 0
        serialized: list[dict[str, Any]] = []

        header = (
            f"📋 گزارش آزمون‌ها\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 تاریخ: {jlabel}\n"
            f"🕒 بازه مورد انتظار: 14:45 تا 21:00\n"
            f"⏱️ مدت مورد انتظار: 45 دقیقه\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        await self.send(message, header)

        for exam in quizzes:
            total += 1
            checks, _ = build_checks(
                exam,
                day_start=day_start,
                expected_open=expected_open,
                expected_close=expected_close,
                safety_general=self.settings.safety_general_patterns,
                safety_special=self.settings.safety_special_patterns,
                tz_name=self.settings.lms_timezone,
            )
            if any(c.icon == "❌" for c in checks):
                problematic += 1
            if any(c.icon == "⚠️" for c in checks):
                warning_count += 1

            serialized_exam = {**exam, "checks": [c.__dict__ for c in checks]}
            serialized.append(serialized_exam)

            text = self.format_exam_report(exam, checks)
            kwargs: dict[str, Any] = {}
            if int(exam.get("question_count") or 0) == 0:
                kwargs["components"] = add_questions_button(int(exam["quizid"]))
            await self.send(message, text, **kwargs)

        summary = {
            "date": jlabel,
            "quizzes": serialized,
            "total": total,
            "problematic": problematic,
            "warning": warning_count,
        }
        await self.db.save_status_run(chat_id, user_id, jlabel, gdate.isoformat(), summary)
        await self.send(
            message,
            f"🏁 جمع‌بندی\n\n"
            f"📚 تعداد آزمون‌ها: {total}\n"
            f"❌ آزمون‌های دارای خطا: {problematic}\n"
            f"⚠️ آزمون‌های دارای هشدار: {warning_count}\n\n"
            f"گزارش در ربات ذخیره شد.",
        )
        return True

    async def scheduler_loop(self) -> None:
        """Background loop: every ~30s, checks each group's configured
        broadcast time and fires the daily status report when it's due."""
        logger.info("Daily-schedule broadcaster started")
        while True:
            try:
                await self.run_scheduled_broadcasts()
            except Exception:
                logger.exception("Scheduled broadcast tick failed")
            await asyncio.sleep(30)

    async def run_scheduled_broadcasts(self) -> None:
        now = now_local(self.settings.lms_timezone)
        today_str = now.date().isoformat()
        for sched in await self.db.get_enabled_schedules():
            chat_id = int(sched["chat_id"])
            if not self._allowed_group(chat_id):
                continue
            if sched.get("last_run_date") == today_str:
                continue
            if now.hour == int(sched["hour"]) and now.minute == int(sched["minute"]):
                # Do not mark success before the broadcast completes: otherwise a
                # transient LMS/Bale failure would silently suppress the day's report.
                # The in-memory guard prevents duplicate concurrent runs in the same process.
                if chat_id in self._schedule_running:
                    continue
                self._schedule_running.add(chat_id)
                try:
                    await self.broadcast_status(chat_id)
                    await self.db.mark_schedule_run(chat_id, today_str)
                except Exception:
                    logger.exception("Scheduled broadcast failed for chat %s", chat_id)
                finally:
                    self._schedule_running.discard(chat_id)

    async def broadcast_status(self, chat_id: int) -> None:
        gdate = today_gregorian(self.settings.lms_timezone)
        jlabel = jalali_label(gdate)
        admin_id = next(iter(self.settings.admin_ids), 0)
        await self.bot.send_message(chat_id, f"⏰ اعلام خودکار وضعیت آزمون‌های امروز ({jlabel})")
        proxy = _ChatProxy(self.bot, chat_id)
        ok = await self.run_status(proxy, gdate, jlabel, admin_id)
        if not ok:
            raise RuntimeError("Scheduled status report failed; schedule will be retried.")

    def format_exam_report(self, exam: dict[str, Any], checks: list[CheckResult]) -> str:
        lines = [
            f"📝 {exam.get('name','بدون نام')}",
            f"📚 درس: {exam.get('course_fullname','—')}",
            f"🔑 Shortname: {exam.get('course_shortname','—')}",
            f"🔢 Quiz ID: {exam.get('quizid','—')}",
            "━━━━━━━━━━━━━━━━━━",
        ]
        for c in checks:
            detail = f" — {c.detail}" if c.detail else ""
            lines.append(f"{c.icon} {c.label}: {c.value}{detail}")
        lines.append(
            f"🔗 {exam.get('url','') or 'لینک آزمون در دسترس نیست'}"
        )
        return "\n".join(lines)


    async def _run_quick_lms_health(self, msg) -> None:
        try:
            info = await self.moodle.health()
            db_state = "✅" if info.get("db_ok") else "❌"
            await self.send(
                msg,
                "✅ تست سریع LMS موفق بود.\n\n"
                f"🧩 Plugin: {info.get('plugin', 'local_exammonitor')}\n"
                f"🔢 Version: {info.get('version', '—')} ({info.get('version_code', '—')})\n"
                f"🗄️ دیتابیس: {db_state}\n"
                f"🕒 Server time: {info.get('server_time', '—')}\n\n"
                "اگر این OK است ولی گزارش تاریخ 504 می‌دهد، از /diag برای تست شبکه استفاده کنید.",
            )
        except MoodleError as exc:
            if getattr(exc, "status", None) in {502, 503, 504}:
                await self.send(
                    msg,
                    "❌ تست سریع LMS با Gateway Timeout شکست خورد.\n\n"
                    "از سرور ربات به Moodle پشت Cloudflare نمی‌رسیم.\n"
                    f"⏱️ {getattr(exc, 'elapsed', 0) or 0:.1f}s\n\n"
                    "الان /diag را بزنید تا لایه دقیق مشخص شود.\n\n"
                    f"جزئیات: {str(exc)[:1000]}",
                )
            else:
                await self.send(msg, f"❌ تست سریع LMS ناموفق بود.\n\n{str(exc)[:1600]}")
        except Exception as exc:
            logger.exception("LMS quick health failed")
            await self.send(msg, f"❌ تست سریع LMS ناموفق بود.\n\n{type(exc).__name__}: {exc}")

    async def _run_server_diagnostic(self, msg) -> None:
        chat_id = int(getattr(msg, "chat_id", 0) or 0)
        if chat_id in self._server_diagnostic_running:
            await self.send(msg, "⏳ تست سرور دیگری در حال اجراست. کمی صبر کنید.")
            return
        self._server_diagnostic_running.add(chat_id)
        try:
            await self.send(
                msg,
                "🧪 تست کامل سرور شروع شد.\n\n"
                "از داخل همان سروری که ربات روی آن است این موارد بررسی می‌شود:\n"
                "1) پیکربندی\n"
                "2) DNS و IPv4/IPv6\n"
                "3) TCP/443\n"
                "4) TLS/SSL + SNI\n"
                "5) IP خروجی سرور ربات\n"
                "6) HTTPS خود LMS\n"
                "7) Moodle REST ping\n"
                "8) تکرار ping برای پایداری\n\n"
                "⏳ ممکن است ۱ تا ۲ دقیقه طول بکشد...",
            )
            diagnostic = NetworkDiagnostic(self.settings.lms_url, self.settings.lms_token)
            results = await diagnostic.run()
            for result in results:
                await self.send(msg, render_result(result))
                await asyncio.sleep(0.35)
            await self.send(
                msg,
                render_summary(results)
                + "\n\n📌 همه تست‌ها از داخل سرور ربات انجام شده‌اند.\n"
                "خروجی کامل را برای تشخیص مسیر شبکه بفرستید.",
            )
        except Exception as exc:
            logger.exception("Server diagnostic failed")
            await self.send(msg, f"❌ اجرای تست سرور با خطای داخلی روبه‌رو شد.\n\n{type(exc).__name__}: {exc}")
        finally:
            self._server_diagnostic_running.discard(chat_id)

    async def on_callback(self, callback: CallbackQuery) -> None:
        await self.safe_callback_answer(callback)
        user = callback.from_user or getattr(callback, "user", None)
        if user is None or not self._is_admin(int(user.id)):
            return
        msg = callback.message
        if msg is None:
            return
        chat_id = int(msg.chat_id)
        user_id = int(user.id)
        is_private = self._is_private_chat(msg) or (chat_id == user_id)
        if not self._can_admin_interact(chat_id, user_id, is_private=is_private):
            return
        data = str(callback.data or "")
        try:
            if data == "status:today":
                gdate = today_gregorian(self.settings.lms_timezone)
                await self.run_status(msg, gdate, jalali_label(gdate), int(user.id))
                return
            if data == "status:tomorrow":
                gdate = today_gregorian(self.settings.lms_timezone) + timedelta(days=1)
                await self.run_status(msg, gdate, jalali_label(gdate), int(user.id))
                return
            if data == "status:date":
                await self.db.set_flow(chat_id, int(user.id), "waiting_status_date", {})
                await self.send(msg, "🗓 تاریخ را به‌صورت ۱۴۰۵/۰۷/۰۳ ارسال کنید. برای لغو /cancel")
                return
            if data == "misc:help":
                await self.send(msg, HELP_TEXT)
                return
            if data == "sched:menu":
                schedule = await self.db.get_schedule(chat_id)
                await self.send(
                    msg,
                    "⏰ اعلام خودکار روزانه\n\n"
                    "با فعال کردن این گزینه، ربات هر روز در همان ساعتی که مشخص می‌کنید "
                    "به‌طور خودکار گزارش کامل وضعیت آزمون‌های همان روز را در همین گروه ارسال می‌کند.",
                    components=schedule_menu(schedule),
                )
                return
            if data == "sched:back":
                await self.send(msg, "🛠 پنل مدیریت بررسی آزمون‌ها\n\nیکی از گزینه‌های زیر را انتخاب کنید:", components=admin_panel())
                return
            if data == "sched:set":
                await self.db.set_flow(chat_id, int(user.id), "waiting_schedule_time", {})
                await self.send(msg, "🕐 ساعت اعلام خودکار روزانه را به‌صورت HH:MM ارسال کنید (مثلاً 08:30). برای لغو /cancel")
                return
            if data == "sched:disable":
                await self.db.disable_schedule(chat_id)
                await self.send(msg, "🔕 اعلام خودکار روزانه برای این گروه غیرفعال شد.", components=schedule_menu(None))
                return
            if data == "sched:status":
                schedule = await self.db.get_schedule(chat_id)
                if not schedule or not schedule.get("enabled"):
                    await self.send(msg, "ℹ️ اعلام خودکار برای این گروه فعال نیست.", components=schedule_menu(schedule))
                else:
                    last_run = schedule.get("last_run_date") or "—"
                    await self.send(
                        msg,
                        f"✅ اعلام خودکار فعال است.\n🕐 ساعت: {schedule['hour']:02d}:{schedule['minute']:02d}\n📅 آخرین اجرا: {last_run}",
                        components=schedule_menu(schedule),
                    )
                return
            if data == "status:last":
                last = await self.db.get_last_status_run(chat_id)
                if not last:
                    await self.send(msg, "ℹ️ هنوز گزارشی ثبت نشده است.")
                else:
                    s = last["summary"]
                    await self.send(msg, f"🕘 آخرین گزارش: {last['jalali_date']}\n📚 آزمون‌ها: {s.get('total',0)}\n❌ خطاها: {s.get('problematic',0)}\n⚠️ هشدارها: {s.get('warning',0)}")
                return
            if data == "status:issues":
                last = await self.db.get_last_status_run(chat_id)
                if not last:
                    await self.send(msg, "ℹ️ هنوز گزارشی ثبت نشده است.")
                    return
                issues = [e for e in last["summary"].get("quizzes", []) if any(c.get("icon") in {"❌","⚠️"} for c in e.get("checks", []))]
                if not issues:
                    await self.send(msg, "✅ در آخرین گزارش، آزمون مسئله‌داری ثبت نشده است.")
                    return
                await self.send(msg, f"🛠 آزمون‌های دارای مشکل در تاریخ {last['jalali_date']}:")
                for e in issues:
                    problems = [c for c in e.get("checks", []) if c.get("icon") in {"❌","⚠️"}]
                    text = f"📝 {e.get('name','—')}\n" + "\n".join(f"{c['icon']} {c['label']}: {c['value']}" for c in problems)
                    if int(e.get("question_count") or 0) == 0:
                        await self.send(msg, text, components=add_questions_button(int(e["quizid"])))
                    else:
                        await self.send(msg, text)
                return
            if data == "status:history":
                runs = await self.db.get_recent_status_runs(chat_id, 10)
                if not runs:
                    await self.send(msg, "ℹ️ هنوز گزارشی ثبت نشده است.")
                    return
                lines = ["📜 تاریخچه ۱۰ گزارش اخیر", "━━━━━━━━━━━━━━━━━━"]
                for run in runs:
                    s = run["summary"]
                    lines.append(f"📅 {run['jalali_date']} — 📚 {s.get('total', 0)} آزمون — ❌ {s.get('problematic', 0)} — ⚠️ {s.get('warning', 0)}")
                await self.send(msg, "\n".join(lines))
                return
            if data == "lms:health":
                await self._run_quick_lms_health(msg)
                return

            if data == "server:diagnostic":
                await self._run_server_diagnostic(msg)
                return
            if data == "flow:cancel":
                await self.db.clear_flow(chat_id, int(user.id))
                await self.send(msg, "✅ عملیات لغو شد.")
                return
            if data.startswith("addq:"):
                quiz_id = int(data.split(":", 1)[1])
                await self.db.set_flow(chat_id, int(user.id), "waiting_course_shortname", {"quizid": quiz_id})
                await self.send(msg, "🔑 نام کوتاه (Shortname) درس را ارسال کنید:")
                return
            if data.startswith("addq_confirm:"):
                quiz_id = int(data.split(":", 1)[1])
                flow = await self.db.get_flow(chat_id, int(user.id))
                if not flow or flow[0] != "waiting_course_confirm":
                    await self.send(msg, "⚠️ این درخواست منقضی شده است. دوباره از گزارش روی «اضافه کردن سوالات» بزنید.")
                    return
                data_flow = flow[1]
                if int(data_flow.get("quizid", -1)) != quiz_id:
                    await self.send(msg, "⚠️ تطابق درخواست تأیید نشد.")
                    return
                await self.db.set_flow(chat_id, int(user.id), "waiting_excel", data_flow)
                await self.send(msg, f"✅ تأیید شد.\n\nحالا فایل Excel سوالات درس «{data_flow.get('course_fullname','—')}» را به‌صورت Document ارسال کنید.\n\nفقط فایل Excel معتبر پذیرفته می‌شود.")
                return
        except Exception as exc:
            logger.exception("Callback handling failed")
            await self.send(msg, f"❌ خطایی هنگام اجرای عملیات رخ داد.\n{str(exc)[:1200]}")

    async def handle_flow_message(self, message: Message, flow: tuple[str, dict[str, Any]]) -> bool:
        chat_id = int(message.chat_id)
        user_id = int(message.author.id)
        step, data = flow
        if step == "waiting_status_date":
            text = (message.content or "").strip()
            try:
                gdate, jlabel = parse_date_input(text, self.settings.lms_timezone)
            except ValueError as exc:
                await self.send(message, f"⚠️ {exc}")
                return True
            await self.db.clear_flow(chat_id, user_id)
            await self.run_status(message, gdate, jlabel, user_id)
            return True

        if step == "waiting_schedule_time":
            text = (message.content or "").strip()
            m = _SCHEDULE_TIME_RE.match(text)
            if not m:
                await self.send(message, "⚠️ فرمت نامعتبر است. ساعت را به‌صورت HH:MM ارسال کنید (مثلاً 08:30).")
                return True
            hour, minute = int(m.group(1)), int(m.group(2))
            await self.db.set_schedule(chat_id, hour, minute)
            await self.db.clear_flow(chat_id, user_id)
            await self.send(
                message,
                f"✅ اعلام خودکار روزانه فعال شد.\n🕐 هر روز ساعت {hour:02d}:{minute:02d} گزارش وضعیت آزمون‌های همان روز در این گروه ارسال می‌شود.",
                components=schedule_menu({"hour": hour, "minute": minute, "enabled": True}),
            )
            return True

        if step == "waiting_course_shortname":
            shortname = (message.content or "").strip()
            if len(shortname) > 255:
                await self.send(message, "⚠️ Shortname بیش از حد طولانی است.")
                return True
            if not shortname or shortname.startswith("/"):
                return True
            try:
                course = await self.moodle.find_course(shortname)
            except Exception as exc:
                await self.send(message, f"❌ درس پیدا نشد یا API خطا داد.\n{str(exc)[:1000]}")
                return True
            if not course.get("found"):
                await self.send(message, "❌ درسی با این Shortname پیدا نشد. لطفاً Shortname دقیق را ارسال کنید.")
                return True
            if not course.get("quizids") or int(data.get("quizid", -1)) not in [int(x) for x in course.get("quizids", [])]:
                await self.send(message, "❌ آزمون انتخاب‌شده مربوط به این درس نیست. Shortname را درست وارد کنید.")
                return True
            next_data = {**data, "course_shortname": shortname, "course_id": int(course["id"]), "course_fullname": course.get("fullname", "—")}
            await self.db.set_flow(chat_id, user_id, "waiting_course_confirm", next_data)
            await self.send(message, f"🔎 شما می‌خواهید برای درس:\n\n📚 {course.get('fullname','—')}\n🔑 {shortname}\n\nسوالات آزمون اضافه شود؟", components=confirm_course(int(data["quizid"])))
            return True

        if step == "waiting_excel":
            document = getattr(message, "document", None)
            if document is None:
                await self.send(message, "⚠️ لطفاً فایل Excel را به‌صورت Document ارسال کنید.")
                return True
            filename = getattr(document, "file_name", None) or "questions.xlsx"
            if not filename.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
                await self.send(message, "⚠️ این فایل Excel پشتیبانی‌شده نیست. فقط xlsx/xlsm/xltx/xltm ارسال کنید.")
                return True
            size = int(getattr(document, "file_size", 0) or 0)
            if size > self.settings.max_excel_file_mb * 1024 * 1024:
                await self.send(message, f"❌ حجم فایل بیشتر از {self.settings.max_excel_file_mb} MB است.")
                return True

            progress = await message.reply("⏳ فایل دریافت شد. در حال تبدیل Excel → Moodle XML ...")
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    xlsx_path = root / filename
                    xml_path = root / (Path(filename).stem + ".xml")
                    with xlsx_path.open("wb") as fp:
                        await document.save_to_memory(fp)
                    try:
                        _, qcount, image_count = convert(xlsx_path, xml_path)
                    except (ExcelReadError, FileNotFoundError) as exc:
                        await self.send(message, f"❌ تبدیل فایل ناموفق بود.\n{str(exc)[:1500]}")
                        return True
                    await self.send(message, f"✅ تبدیل انجام شد.\n\n📊 تعداد سوالات: {qcount}\n🖼️ سوالات دارای تصویر: {image_count}")

                    draftid = await self.moodle.get_unused_draft_itemid()
                    safe_filename = xml_path.name.encode("ascii", "ignore").decode("ascii") or "questions.xml"
                    await self.moodle.upload_to_draft(xml_path, draftid, safe_filename)
                    await self.send(message, "⏳ فایل XML به LMS منتقل شد. در حال وارد کردن به بانک سوالات و افزودن به Quiz ...")
                    result = await asyncio.wait_for(
                        self.moodle.import_xml_to_quiz(
                            quizid=int(data["quizid"]),
                            course_shortname=str(data["course_shortname"]),
                            draft_itemid=draftid,
                            filename=safe_filename,
                        ),
                        timeout=self.settings.operation_timeout,
                    )
                    await self.db.clear_flow(chat_id, user_id)
                    await self.send(message, f"✅ عملیات کامل شد.\n\n📚 درس: {data.get('course_fullname','—')}\n📝 Quiz ID: {data.get('quizid')}\n❓ سوالات واردشده: {result.get('imported_count', qcount)}\n➕ سوالات اضافه‌شده به آزمون: {result.get('added_count', qcount)}\n🗂 بانک سوال: {result.get('category_name','—')}\n🔀 Shuffle Questions: {'فعال' if result.get('shuffle_questions_enabled') else 'غیرفعال'}")
            except MoodleError as exc:
                logger.exception("Moodle import failed")
                await self.send(message, f"❌ عملیات Moodle ناموفق بود.\n\n{str(exc)[:1800]}")
            except Exception as exc:
                logger.exception("Excel add-questions flow failed")
                await self.send(message, f"❌ خطای غیرمنتظره در فرآیند افزودن سوالات.\n\n{str(exc)[:1800]}")
            finally:
                try:
                    await progress.delete()
                except Exception:
                    pass
            return True
        return False

    async def on_message(self, message: Message) -> None:
        try:
            chat_id = int(message.chat_id)
            user_id = int(message.author.id)
            chat_type = str(getattr(message.chat, "type", "") or "").lower()
            is_group = chat_type in {"group", "supergroup"}
            is_private = (not is_group) and (
                self._is_private_chat(message) or chat_id == user_id or chat_type in {"", "private", "pv", "user"}
            )

            # Groups: only allowed group IDs (or all if list empty).
            # Private: only admins may interact; everyone else is ignored.
            if is_group:
                if not self._allowed_group(chat_id):
                    return
            else:
                if not self._is_admin(user_id):
                    return

            text = (message.content or "").strip()
            if text == "/start":
                if is_private and self._is_admin(user_id):
                    await self.send(
                        message,
                        "سلام 👋\n"
                        "شما به‌عنوان ادمین شناخته شدید.\n\n"
                        "از /admin پنل مدیریت را باز کنید یا از دکمه‌های زیر استفاده کنید.",
                        components=admin_panel(),
                    )
                elif is_private:
                    await self.send(
                        message,
                        "سلام 👋\nاین ربات برای کنترل آزمون‌های LMS است.\n\n"
                        "ادمین‌ها می‌توانند در چت خصوصی یا در گروه مجاز از /admin و /status استفاده کنند.",
                    )
                else:
                    await self.send(
                        message,
                        "سلام 👋\nاین ربات برای کنترل آزمون‌های LMS است.\n\n"
                        "ادمین‌ها می‌توانند از /admin و /status استفاده کنند.",
                    )
                return
            if text == "/help":
                await self.send(message, HELP_TEXT)
                return
            if text == "/id":
                await self.send(message, f"👤 User ID: {user_id}\n💬 Chat ID: {chat_id}")
                return
            if text == "/admin":
                await self.handle_admin_command(message)
                return
            if text in {"/diag", "/servertest", "/testdiagnostic", "/server"}:
                # Text-command fallback when inline buttons are not visible in Bale client
                if not self._is_admin(user_id):
                    return
                await self._run_server_diagnostic(message)
                return
            if text in {"/pinglms", "/lmshealth"}:
                if not self._is_admin(user_id):
                    return
                await self._run_quick_lms_health(message)
                return
            if text == "/status" or text.startswith("/status "):
                await self.handle_status_command(message, text[len("/status"):].strip())
                return
            if text == "/cancel":
                if self._is_admin(user_id):
                    await self.db.clear_flow(chat_id, user_id)
                    await self.send(message, "✅ عملیات جاری لغو شد.")
                return

            if self._is_admin(user_id):
                flow = await self.db.get_flow(chat_id, user_id)
                if flow and await self.handle_flow_message(message, flow):
                    return

            # In group chats, normal files/messages are intentionally ignored unless an admin is in a guided flow.
            return
        except Exception:
            logger.exception("Unhandled on_message error")

    def run(self) -> None:
        self.bot.event(self.on_before_ready)
        self.bot.event(self.on_ready)
        self.bot.event(self.on_message)
        self.bot.event(self.on_callback)
        self.bot.run()


async def build_bot(settings: Settings | None = None) -> ExamMonitorBot:
    settings = settings or Settings.from_env()
    app = ExamMonitorBot(settings)
    await app.initialize()
    return app
