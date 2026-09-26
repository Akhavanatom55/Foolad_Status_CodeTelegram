from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _markup(rows: list[tuple[str, str]], columns: int = 2) -> InlineKeyboardMarkup:
    columns = max(1, int(columns))
    keyboard: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for label, data in rows:
        current_row.append(InlineKeyboardButton(text=label, callback_data=data))
        if len(current_row) == columns:
            keyboard.append(current_row)
            current_row = []
    if current_row:
        keyboard.append(current_row)
    return InlineKeyboardMarkup(keyboard)


def admin_panel() -> InlineKeyboardMarkup:
    return _markup([
        ("بررسی تاریخ امروز 📅", "status:today"),
        ("بررسی فردا ⏭", "status:tomorrow"),
        ("بررسی تاریخ دیگر 🗓", "status:date"),
        ("آخرین گزارش 🕘", "status:last"),
        ("آزمون‌های مشکل‌دار 🛠", "status:issues"),
        ("تاریخچه گزارش‌ها 📜", "status:history"),
        ("تست اتصال LMS 🔌", "lms:health"),
        ("اعلام خودکار روزانه ⏰", "sched:menu"),
        ("راهنما ❔", "misc:help"),
    ], columns=2)


def schedule_menu(schedule: dict | None) -> InlineKeyboardMarkup:
    rows: list[tuple[str, str]] = []
    if schedule and schedule.get("enabled"):
        rows.append((f"فعال — هر روز ساعت {schedule['hour']:02d}:{schedule['minute']:02d} ✅", "sched:status"))
        rows.append(("تغییر ساعت 🕐", "sched:set"))
        rows.append(("غیرفعال کردن 🔕", "sched:disable"))
    else:
        rows.append(("تنظیم ساعت اعلام خودکار ⏰", "sched:set"))
    rows.append(("بازگشت به پنل 🔙", "sched:back"))
    return _markup(rows, columns=1)


def confirm_course(quiz_id: int) -> InlineKeyboardMarkup:
    return _markup([
        ("بله، همین درس ✅", f"addq_confirm:{quiz_id}"),
        ("لغو ❌", "flow:cancel"),
    ], columns=2)


def add_questions_button(quiz_id: int) -> InlineKeyboardMarkup:
    return _markup([("اضافه کردن سوالات ➕", f"addq:{quiz_id}")], columns=1)
