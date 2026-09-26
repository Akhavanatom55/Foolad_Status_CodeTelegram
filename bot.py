from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
from rules import CheckResult, build_checks
from converter import convert, ExcelReadError

_SCHEDULE_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

HELP_TEXT = (
    "/admin — پنل مدیریت\n"
    "/status — بررسی آزمون‌های امروز\n"
    "/status YYYY/MM/DD — بررسی یک تاریخ\n"
    "/cancel — لغو عملیات جاری\n"
    "/id — نمایش شناسه کاربر و گروه\n\n"
    "از پنل /admin می‌توانید گزارش امروز/فردا/یک تاریخ دلخواه را بگیرید، آخرین "
    "گزارش و تاریخچه را ببینید، اتصال LMS را تست کنید و یک ساعت مشخص برای "
    "اعلام خودکار روزانهٔ وضعیت آزمون‌های همین گروه تنظیم کنید."
)


class _ReplyTarget:
    """Minimal message-like adapter so run_status()/send() can target either a
    real incoming message/callback (reply in-place) or a bare chat_id (used
    for scheduled broadcasts, where there is no incoming Update to reply to).
    Keeping this indirection means the rest of the bot's logic below — which
    was written against python-bale-bot's `message.reply()` — barely had to
    change when ported to python-telegram-bot.
    """

    def __init__(self, bot, chat_id: int, message: Message | None = None) -> None:
        self._bot = bot
        self.chat_id = chat_id
        self._message = message

    async def reply(self, text: str, *, components: InlineKeyboardMarkup | None = None):
        if self._message is not None:
            return await self._message.reply_text(text, reply_markup=components)
        return await self._bot.send_message(chat_id=self.chat_id, text=text, reply_markup=components)


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
        # Filled in by build_application() once the Application (and its
        # bot client) exists — ExamMonitorBot is constructed first so that
        # ApplicationBuilder().post_init(...) has something to call back into.
        self.application: Application | None = None
        self._schedule_running: set[int] = set()
        self._scheduler_task: asyncio.Task | None = None

    # ---------------------------------------------------------------- utils

    def _target(self, chat_id: int, message: Message | None = None) -> _ReplyTarget:
        return _ReplyTarget(self.application.bot, chat_id, message)

    def _allowed_group(self, chat_id: int) -> bool:
        return not self.settings.allowed_group_ids or chat_id in self.settings.allowed_group_ids

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids

    def _is_private_chat(self, chat) -> bool:
        """True when the conversation is a 1:1 private chat (not a group)."""
        chat_type = str(getattr(chat, "type", "") or "").lower()
        return chat_type == "private"

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

    def _gate(self, update: Update) -> bool:
        """Top-level access gate applied to every incoming update, mirroring
        the original on_message() filter: groups need ALLOWED_GROUP_IDS,
        private chats need the sender to be an admin. Individual commands
        (/admin, /status) additionally re-check via _can_admin_interact().
        """
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return False
        chat_type = str(getattr(chat, "type", "") or "").lower()
        is_group = chat_type in {"group", "supergroup"}
        if is_group:
            return self._allowed_group(chat.id)
        return self._is_admin(user.id)

    async def send(self, target: _ReplyTarget, text: str, **kwargs: Any) -> None:
        await target.reply(text, **kwargs)

    async def on_post_init(self, application: Application) -> None:
        """Runs once, inside the running event loop, after Application.initialize()
        and before polling starts. Replaces on_ready()/on_before_ready() and the
        old `await app.initialize()` call in main.py.

        Note: python-telegram-bot's run_polling() already deletes any webhook
        for you as part of its own bootstrap, so there is no need for an
        explicit delete_webhook() call here (unlike the Bale version).
        """
        await self.db.initialize()
        me = await application.bot.get_me()
        logger.info("Telegram bot ready as %s", me.username)
        self._scheduler_task = asyncio.create_task(self.scheduler_loop())

    # ------------------------------------------------------------ commands

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._gate(update):
            return
        chat = update.effective_chat
        user = update.effective_user
        is_private = self._is_private_chat(chat) or (chat.id == user.id)
        target = self._target(chat.id, update.effective_message)
        if is_private and self._is_admin(user.id):
            await self.send(
                target,
                "سلام 👋\n"
                "شما به‌عنوان ادمین شناخته شدید.\n\n"
                "از /admin پنل مدیریت را باز کنید یا از دکمه‌های زیر استفاده کنید.",
                components=admin_panel(),
            )
        elif is_private:
            await self.send(
                target,
                "سلام 👋\nاین ربات برای کنترل آزمون‌های LMS است.\n\n"
                "ادمین‌ها می‌توانند در چت خصوصی یا در گروه مجاز از /admin و /status استفاده کنند.",
            )
        else:
            await self.send(
                target,
                "سلام 👋\nاین ربات برای کنترل آزمون‌های LMS است.\n\n"
                "ادمین‌ها می‌توانند از /admin و /status استفاده کنند.",
            )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._gate(update):
            return
        target = self._target(update.effective_chat.id, update.effective_message)
        await self.send(target, HELP_TEXT)

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._gate(update):
            return
        target = self._target(update.effective_chat.id, update.effective_message)
        await self.send(
            target,
            f"👤 User ID: {update.effective_user.id}\n💬 Chat ID: {update.effective_chat.id}",
        )

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._gate(update):
            return
        chat = update.effective_chat
        user = update.effective_user
        if self._is_admin(user.id):
            await self.db.clear_flow(chat.id, user.id)
            target = self._target(chat.id, update.effective_message)
            await self.send(target, "✅ عملیات جاری لغو شد.")

    async def cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._gate(update):
            return
        await self.handle_admin_command(update)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._gate(update):
            return
        argument = " ".join(context.args) if context.args else ""
        await self.handle_status_command(update, argument)

    async def handle_admin_command(self, update: Update) -> None:
        chat = update.effective_chat
        user = update.effective_user
        is_private = self._is_private_chat(chat) or (chat.id == user.id)
        if not self._can_admin_interact(chat.id, user.id, is_private=is_private):
            return
        target = self._target(chat.id, update.effective_message)
        await self.send(target, "🛠 پنل مدیریت بررسی آزمون‌ها\n\nیکی از گزینه‌های زیر را انتخاب کنید:", components=admin_panel())

    async def handle_status_command(self, update: Update, argument: str = "") -> None:
        chat = update.effective_chat
        user = update.effective_user
        is_private = self._is_private_chat(chat) or (chat.id == user.id)
        if not self._can_admin_interact(chat.id, user.id, is_private=is_private):
            return
        target = self._target(chat.id, update.effective_message)
        try:
            if argument.strip():
                gdate, jlabel = parse_date_input(argument.strip(), self.settings.lms_timezone)
            else:
                gdate = today_gregorian(self.settings.lms_timezone)
                jlabel = jalali_label(gdate)
            await self.run_status(target, gdate, jlabel, user.id)
        except ValueError as exc:
            await self.send(target, f"⚠️ {exc}")

    # --------------------------------------------------------------- status

    async def run_status(self, target: _ReplyTarget, gdate, jlabel: str, user_id: int) -> bool:
        chat_id = int(target.chat_id)
        day_start, day_end = day_bounds_timestamps(gdate, self.settings.lms_timezone)
        expected_open, expected_close = expected_quiz_timestamps(gdate, self.settings.lms_timezone)
        await self.send(target, f"⏳ در حال بررسی آزمون‌های تاریخ {jlabel} ...")
        try:
            payload = await asyncio.wait_for(
                self.moodle.get_status(day_start, day_end, self.settings.status_date_mode),
                timeout=self.settings.operation_timeout,
            )
        except MoodleError as exc:
            logger.exception("Status request failed")
            if getattr(exc, "status", None) in {502, 503, 504}:
                await self.send(
                    target,
                    "❌ LMS از مسیر Web Service پاسخ نداد (Gateway error).\n\n"
                    f"🔧 تابع: {getattr(exc, 'function', 'local_exammonitor_get_status')}\n"
                    f"⏱️ زمان پاسخ: {getattr(exc, 'elapsed', 0) or 0:.1f} ثانیه\n\n"
                    "این پاسخ از Reverse Proxy/Cloudflare یا سرور Moodle برگشته است. "
                    "اگر «تست اتصال LMS» هم خطا داد، باید وب‌سرور/PHP-FPM/Cloudflare خود LMS بررسی شود.\n\n"
                    f"جزئیات: {str(exc)[:1600]}"
                )
            else:
                await self.send(target, f"❌ ارتباط با LMS برقرار نشد یا API خطا داد.\n\nجزئیات: {str(exc)[:1600]}")
            return False
        except Exception as exc:
            logger.exception("Status request failed")
            await self.send(target, f"❌ ارتباط با LMS برقرار نشد یا API خطا داد.\n\nجزئیات: {str(exc)[:1600]}")
            return False

        quizzes = payload.get("quizzes", []) if isinstance(payload, dict) else []
        if not quizzes:
            summary = {"date": jlabel, "quizzes": [], "total": 0, "problematic": 0, "warning": 1}
            await self.db.save_status_run(chat_id, user_id, jlabel, gdate.isoformat(), summary)
            await self.send(target, f"⚠️ برای تاریخ {jlabel} هیچ آزمونی پیدا نشد.\n\nمبنای جستجو: {self.settings.status_date_mode}")
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
        await self.send(target, header)

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
            await self.send(target, text, **kwargs)

        summary = {
            "date": jlabel,
            "quizzes": serialized,
            "total": total,
            "problematic": problematic,
            "warning": warning_count,
        }
        await self.db.save_status_run(chat_id, user_id, jlabel, gdate.isoformat(), summary)
        await self.send(
            target,
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
                # transient LMS/Telegram failure would silently suppress the day's report.
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
        await self.application.bot.send_message(chat_id=chat_id, text=f"⏰ اعلام خودکار وضعیت آزمون‌های امروز ({jlabel})")
        target = self._target(chat_id)
        ok = await self.run_status(target, gdate, jlabel, admin_id)
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

    # -------------------------------------------------------------- callback

    async def _safe_callback_answer(self, callback: CallbackQuery) -> None:
        try:
            await callback.answer()
        except Exception:
            # Most commonly "query is too old"; harmless, just skip the
            # loading-spinner dismissal instead of crashing the handler.
            logger.debug("callback.answer() failed", exc_info=True)

    async def on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        callback = update.callback_query
        await self._safe_callback_answer(callback)
        user = callback.from_user
        if user is None or not self._is_admin(int(user.id)):
            return
        msg = callback.message
        if msg is None:
            return
        chat_id = int(msg.chat_id)
        user_id = int(user.id)
        is_private = self._is_private_chat(msg.chat) or (chat_id == user_id)
        if not self._can_admin_interact(chat_id, user_id, is_private=is_private):
            return
        target = self._target(chat_id, msg)
        data = str(callback.data or "")
        try:
            if data == "status:today":
                gdate = today_gregorian(self.settings.lms_timezone)
                await self.run_status(target, gdate, jalali_label(gdate), user_id)
                return
            if data == "status:tomorrow":
                gdate = today_gregorian(self.settings.lms_timezone) + timedelta(days=1)
                await self.run_status(target, gdate, jalali_label(gdate), user_id)
                return
            if data == "status:date":
                await self.db.set_flow(chat_id, user_id, "waiting_status_date", {})
                await self.send(target, "🗓 تاریخ را به‌صورت ۱۴۰۵/۰۷/۰۳ ارسال کنید. برای لغو /cancel")
                return
            if data == "misc:help":
                await self.send(target, HELP_TEXT)
                return
            if data == "sched:menu":
                schedule = await self.db.get_schedule(chat_id)
                await self.send(
                    target,
                    "⏰ اعلام خودکار روزانه\n\n"
                    "با فعال کردن این گزینه، ربات هر روز در همان ساعتی که مشخص می‌کنید "
                    "به‌طور خودکار گزارش کامل وضعیت آزمون‌های همان روز را در همین گروه ارسال می‌کند.",
                    components=schedule_menu(schedule),
                )
                return
            if data == "sched:back":
                await self.send(target, "🛠 پنل مدیریت بررسی آزمون‌ها\n\nیکی از گزینه‌های زیر را انتخاب کنید:", components=admin_panel())
                return
            if data == "sched:set":
                await self.db.set_flow(chat_id, user_id, "waiting_schedule_time", {})
                await self.send(target, "🕐 ساعت اعلام خودکار روزانه را به‌صورت HH:MM ارسال کنید (مثلاً 08:30). برای لغو /cancel")
                return
            if data == "sched:disable":
                await self.db.disable_schedule(chat_id)
                await self.send(target, "🔕 اعلام خودکار روزانه برای این گروه غیرفعال شد.", components=schedule_menu(None))
                return
            if data == "sched:status":
                schedule = await self.db.get_schedule(chat_id)
                if not schedule or not schedule.get("enabled"):
                    await self.send(target, "ℹ️ اعلام خودکار برای این گروه فعال نیست.", components=schedule_menu(schedule))
                else:
                    last_run = schedule.get("last_run_date") or "—"
                    await self.send(
                        target,
                        f"✅ اعلام خودکار فعال است.\n🕐 ساعت: {schedule['hour']:02d}:{schedule['minute']:02d}\n📅 آخرین اجرا: {last_run}",
                        components=schedule_menu(schedule),
                    )
                return
            if data == "status:last":
                last = await self.db.get_last_status_run(chat_id)
                if not last:
                    await self.send(target, "ℹ️ هنوز گزارشی ثبت نشده است.")
                else:
                    s = last["summary"]
                    await self.send(target, f"🕘 آخرین گزارش: {last['jalali_date']}\n📚 آزمون‌ها: {s.get('total',0)}\n❌ خطاها: {s.get('problematic',0)}\n⚠️ هشدارها: {s.get('warning',0)}")
                return
            if data == "status:issues":
                last = await self.db.get_last_status_run(chat_id)
                if not last:
                    await self.send(target, "ℹ️ هنوز گزارشی ثبت نشده است.")
                    return
                issues = [e for e in last["summary"].get("quizzes", []) if any(c.get("icon") in {"❌","⚠️"} for c in e.get("checks", []))]
                if not issues:
                    await self.send(target, "✅ در آخرین گزارش، آزمون مسئله‌داری ثبت نشده است.")
                    return
                await self.send(target, f"🛠 آزمون‌های دارای مشکل در تاریخ {last['jalali_date']}:")
                for e in issues:
                    problems = [c for c in e.get("checks", []) if c.get("icon") in {"❌","⚠️"}]
                    text = f"📝 {e.get('name','—')}\n" + "\n".join(f"{c['icon']} {c['label']}: {c['value']}" for c in problems)
                    if int(e.get("question_count") or 0) == 0:
                        await self.send(target, text, components=add_questions_button(int(e["quizid"])))
                    else:
                        await self.send(target, text)
                return
            if data == "status:history":
                runs = await self.db.get_recent_status_runs(chat_id, 10)
                if not runs:
                    await self.send(target, "ℹ️ هنوز گزارشی ثبت نشده است.")
                    return
                lines = ["📜 تاریخچه ۱۰ گزارش اخیر", "━━━━━━━━━━━━━━━━━━"]
                for run in runs:
                    s = run["summary"]
                    lines.append(f"📅 {run['jalali_date']} — 📚 {s.get('total', 0)} آزمون — ❌ {s.get('problematic', 0)} — ⚠️ {s.get('warning', 0)}")
                await self.send(target, "\n".join(lines))
                return
            if data == "lms:health":
                try:
                    info = await self.moodle.health()
                    db_state = "✅" if info.get("db_ok") else "❌"
                    await self.send(
                        target,
                        "✅ اتصال واقعی LMS برقرار است.\n\n"
                        f"🧩 Plugin: {info.get('plugin', 'local_exammonitor')}\n"
                        f"🔢 Version: {info.get('version', '—')} ({info.get('version_code', '—')})\n"
                        f"🗄️ دیتابیس Moodle: {db_state}\n"
                        f"🕒 Server time: {info.get('server_time', '—')}\n\n"
                        "این تست مستقیماً Web Service پلاگین Exam Monitor را بررسی می‌کند و "
                        "به core_webservice_get_site_info وابسته نیست."
                    )
                except MoodleError as exc:
                    if getattr(exc, "status", None) in {502, 503, 504}:
                        await self.send(
                            target,
                            "❌ Moodle از پشت Reverse Proxy/Cloudflare در دسترس نیست.\n\n"
                            "🔧 این خطا از خود مسیر LMS است، نه از توکن ربات.\n"
                            f"⏱️ زمان پاسخ: {getattr(exc, 'elapsed', 0) or 0:.1f} ثانیه\n\n"
                            "اقدامات لازم روی سرور LMS:\n"
                            "1) پلاگین local_exammonitor نسخه ۱.۰.۴ را نصب/آپدیت کنید و Web Service را پاک‌سازی کنید.\n"
                            "2) در Cloudflare/Nginx/Apache timeout مبدا را حداقل ۶۰–۱۲۰ ثانیه کنید.\n"
                            "3) PHP-FPM request_terminate_timeout و max_execution_time را برای webservice بالا ببرید.\n"
                            "4) توکن وب‌سرویس باید متعلق به سرویس Exam Monitor باشد و قابلیت local/exammonitor:access داشته باشد.\n\n"
                            f"جزئیات: {str(exc)[:1200]}"
                        )
                    else:
                        await self.send(target, f"❌ تست Web Service LMS ناموفق بود.\n\n{str(exc)[:1600]}")
                except Exception as exc:
                    logger.exception("LMS health check failed")
                    await self.send(target, f"❌ تست Web Service LMS ناموفق بود.\n\n{str(exc)[:1600]}")
                return
            if data == "flow:cancel":
                await self.db.clear_flow(chat_id, user_id)
                await self.send(target, "✅ عملیات لغو شد.")
                return
            if data.startswith("addq:"):
                quiz_id = int(data.split(":", 1)[1])
                await self.db.set_flow(chat_id, user_id, "waiting_course_shortname", {"quizid": quiz_id})
                await self.send(target, "🔑 نام کوتاه (Shortname) درس را ارسال کنید:")
                return
            if data.startswith("addq_confirm:"):
                quiz_id = int(data.split(":", 1)[1])
                flow = await self.db.get_flow(chat_id, user_id)
                if not flow or flow[0] != "waiting_course_confirm":
                    await self.send(target, "⚠️ این درخواست منقضی شده است. دوباره از گزارش روی «اضافه کردن سوالات» بزنید.")
                    return
                data_flow = flow[1]
                if int(data_flow.get("quizid", -1)) != quiz_id:
                    await self.send(target, "⚠️ تطابق درخواست تأیید نشد.")
                    return
                await self.db.set_flow(chat_id, user_id, "waiting_excel", data_flow)
                await self.send(target, f"✅ تأیید شد.\n\nحالا فایل Excel سوالات درس «{data_flow.get('course_fullname','—')}» را به‌صورت Document ارسال کنید.\n\nفقط فایل Excel معتبر پذیرفته می‌شود.")
                return
        except Exception as exc:
            logger.exception("Callback handling failed")
            await self.send(target, f"❌ خطایی هنگام اجرای عملیات رخ داد.\n{str(exc)[:1200]}")

    # --------------------------------------------------------- flow message

    async def handle_flow_message(self, update: Update, flow: tuple[str, dict[str, Any]]) -> bool:
        message = update.effective_message
        chat_id = int(update.effective_chat.id)
        user_id = int(update.effective_user.id)
        target = self._target(chat_id, message)
        step, data = flow
        if step == "waiting_status_date":
            text = (message.text or message.caption or "").strip()
            try:
                gdate, jlabel = parse_date_input(text, self.settings.lms_timezone)
            except ValueError as exc:
                await self.send(target, f"⚠️ {exc}")
                return True
            await self.db.clear_flow(chat_id, user_id)
            await self.run_status(target, gdate, jlabel, user_id)
            return True

        if step == "waiting_schedule_time":
            text = (message.text or "").strip()
            m = _SCHEDULE_TIME_RE.match(text)
            if not m:
                await self.send(target, "⚠️ فرمت نامعتبر است. ساعت را به‌صورت HH:MM ارسال کنید (مثلاً 08:30).")
                return True
            hour, minute = int(m.group(1)), int(m.group(2))
            await self.db.set_schedule(chat_id, hour, minute)
            await self.db.clear_flow(chat_id, user_id)
            await self.send(
                target,
                f"✅ اعلام خودکار روزانه فعال شد.\n🕐 هر روز ساعت {hour:02d}:{minute:02d} گزارش وضعیت آزمون‌های همان روز در این گروه ارسال می‌شود.",
                components=schedule_menu({"hour": hour, "minute": minute, "enabled": True}),
            )
            return True

        if step == "waiting_course_shortname":
            shortname = (message.text or message.caption or "").strip()
            if len(shortname) > 255:
                await self.send(target, "⚠️ Shortname بیش از حد طولانی است.")
                return True
            if not shortname or shortname.startswith("/"):
                return True
            try:
                course = await self.moodle.find_course(shortname)
            except Exception as exc:
                await self.send(target, f"❌ درس پیدا نشد یا API خطا داد.\n{str(exc)[:1000]}")
                return True
            if not course.get("found"):
                await self.send(target, "❌ درسی با این Shortname پیدا نشد. لطفاً Shortname دقیق را ارسال کنید.")
                return True
            if not course.get("quizids") or int(data.get("quizid", -1)) not in [int(x) for x in course.get("quizids", [])]:
                await self.send(target, "❌ آزمون انتخاب‌شده مربوط به این درس نیست. Shortname را درست وارد کنید.")
                return True
            next_data = {**data, "course_shortname": shortname, "course_id": int(course["id"]), "course_fullname": course.get("fullname", "—")}
            await self.db.set_flow(chat_id, user_id, "waiting_course_confirm", next_data)
            await self.send(target, f"🔎 شما می‌خواهید برای درس:\n\n📚 {course.get('fullname','—')}\n🔑 {shortname}\n\nسوالات آزمون اضافه شود؟", components=confirm_course(int(data["quizid"])))
            return True

        if step == "waiting_excel":
            document = message.document
            if document is None:
                await self.send(target, "⚠️ لطفاً فایل Excel را به‌صورت Document ارسال کنید.")
                return True
            filename = document.file_name or "questions.xlsx"
            if not filename.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
                await self.send(target, "⚠️ این فایل Excel پشتیبانی‌شده نیست. فقط xlsx/xlsm/xltx/xltm ارسال کنید.")
                return True
            size = int(document.file_size or 0)
            if size > self.settings.max_excel_file_mb * 1024 * 1024:
                await self.send(target, f"❌ حجم فایل بیشتر از {self.settings.max_excel_file_mb} MB است.")
                return True

            progress = await message.reply_text("⏳ فایل دریافت شد. در حال تبدیل Excel → Moodle XML ...")
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    xlsx_path = root / filename
                    xml_path = root / (Path(filename).stem + ".xml")
                    tg_file = await document.get_file()
                    await tg_file.download_to_drive(xlsx_path)
                    try:
                        _, qcount, image_count = convert(xlsx_path, xml_path)
                    except (ExcelReadError, FileNotFoundError) as exc:
                        await self.send(target, f"❌ تبدیل فایل ناموفق بود.\n{str(exc)[:1500]}")
                        return True
                    await self.send(target, f"✅ تبدیل انجام شد.\n\n📊 تعداد سوالات: {qcount}\n🖼️ سوالات دارای تصویر: {image_count}")

                    draftid = await self.moodle.get_unused_draft_itemid()
                    safe_filename = xml_path.name.encode("ascii", "ignore").decode("ascii") or "questions.xml"
                    await self.moodle.upload_to_draft(xml_path, draftid, safe_filename)
                    await self.send(target, "⏳ فایل XML به LMS منتقل شد. در حال وارد کردن به بانک سوالات و افزودن به Quiz ...")
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
                    await self.send(target, f"✅ عملیات کامل شد.\n\n📚 درس: {data.get('course_fullname','—')}\n📝 Quiz ID: {data.get('quizid')}\n❓ سوالات واردشده: {result.get('imported_count', qcount)}\n➕ سوالات اضافه‌شده به آزمون: {result.get('added_count', qcount)}\n🗂 بانک سوال: {result.get('category_name','—')}\n🔀 Shuffle Questions: {'فعال' if result.get('shuffle_questions_enabled') else 'غیرفعال'}")
            except MoodleError as exc:
                logger.exception("Moodle import failed")
                await self.send(target, f"❌ عملیات Moodle ناموفق بود.\n\n{str(exc)[:1800]}")
            except Exception as exc:
                logger.exception("Excel add-questions flow failed")
                await self.send(target, f"❌ خطای غیرمنتظره در فرآیند افزودن سوالات.\n\n{str(exc)[:1800]}")
            finally:
                try:
                    await progress.delete()
                except Exception:
                    pass
            return True
        return False

    async def on_flow_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Catch-all for non-command text/document messages: continues a
        guided flow (waiting_status_date, waiting_excel, ...) for admins.
        Anything without an active flow is silently ignored — same as the
        Bale version's on_message() fallthrough."""
        if not self._gate(update):
            return
        user = update.effective_user
        if not self._is_admin(user.id):
            return
        chat = update.effective_chat
        flow = await self.db.get_flow(chat.id, user.id)
        if flow:
            await self.handle_flow_message(update, flow)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Unhandled error while processing an update", exc_info=context.error)


def build_application(settings: Settings | None = None) -> Application:
    settings = settings or Settings.from_env()
    exam_bot = ExamMonitorBot(settings)

    builder: ApplicationBuilder = Application.builder().token(settings.telegram_token)
    if settings.telegram_api_base:
        builder = builder.base_url(settings.telegram_api_base)
    application = builder.post_init(exam_bot.on_post_init).build()
    exam_bot.application = application

    application.add_handler(CommandHandler("start", exam_bot.cmd_start))
    application.add_handler(CommandHandler("help", exam_bot.cmd_help))
    application.add_handler(CommandHandler("id", exam_bot.cmd_id))
    application.add_handler(CommandHandler("admin", exam_bot.cmd_admin))
    application.add_handler(CommandHandler("status", exam_bot.cmd_status))
    application.add_handler(CommandHandler("cancel", exam_bot.cmd_cancel))
    application.add_handler(CallbackQueryHandler(exam_bot.on_callback_query))
    application.add_handler(MessageHandler(~filters.COMMAND, exam_bot.on_flow_message))
    application.add_error_handler(exam_bot.on_error)

    return application
