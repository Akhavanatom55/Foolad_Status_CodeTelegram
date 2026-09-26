from __future__ import annotations

from bale import InlineKeyboardButton, InlineKeyboardMarkup


def _markup(rows: list[tuple[str, str]], columns: int = 2) -> InlineKeyboardMarkup:
    """Build inline keyboard compatible with python-bale-bot 2.5.x.

    rows: list of (label, callback_data)
    columns: buttons per row (1 or 2)
    """
    markup = InlineKeyboardMarkup()
    columns = max(1, min(int(columns), 3))
    for index, (label, data) in enumerate(rows):
        # bale row numbers are 1-based
        row_no = (index // columns) + 1
        btn = InlineKeyboardButton(text=str(label)[:64], callback_data=str(data)[:64])
        markup.add(btn, row=row_no)
    return markup


def admin_panel() -> InlineKeyboardMarkup:
    # Put diagnostic buttons first so they are never clipped by client UI limits.
    return _markup(
        [
            ("تست سرور", "server:diagnostic"),
            ("تست سريع LMS", "lms:health"),
            ("بررسي امروز", "status:today"),
            ("بررسي فردا", "status:tomorrow"),
            ("بررسي تاريخ ديگر", "status:date"),
            ("آخرين گزارش", "status:last"),
            ("آزمون هاي مشكل دار", "status:issues"),
            ("تاريخچه گزارش ها", "status:history"),
            ("اعلام خودكار روزانه", "sched:menu"),
            ("راهنما", "misc:help"),
        ],
        columns=2,
    )


def schedule_menu(schedule: dict | None) -> InlineKeyboardMarkup:
    rows: list[tuple[str, str]] = []
    if schedule and schedule.get("enabled"):
        rows.append(
            (
                f"فعال {schedule['hour']:02d}:{schedule['minute']:02d}",
                "sched:status",
            )
        )
        rows.append(("تغيير ساعت", "sched:set"))
        rows.append(("غيرفعال كردن", "sched:disable"))
    else:
        rows.append(("تنظيم ساعت اعلام خودكار", "sched:set"))
    rows.append(("بازگشت به پنل", "sched:back"))
    return _markup(rows, columns=1)


def confirm_course(quiz_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            ("بله همين درس", f"addq_confirm:{quiz_id}"),
            ("لغو", "flow:cancel"),
        ],
        columns=2,
    )


def add_questions_button(quiz_id: int) -> InlineKeyboardMarkup:
    return _markup([("اضافه كردن سوالات", f"addq:{quiz_id}")], columns=1)
