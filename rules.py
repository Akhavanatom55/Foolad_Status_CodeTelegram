from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


def normalize_text(text: str) -> str:
    table = str.maketrans({
        "ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه",
        "‌": " ", "\u200f": " ", "\u200e": " ",
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
    })
    return " ".join(text.translate(table).strip().lower().split())


@dataclass(frozen=True)
class CheckResult:
    icon: str
    label: str
    value: str
    detail: str = ""


def expected_safety_grade(name: str, general_patterns: tuple[str, ...], special_patterns: tuple[str, ...]) -> tuple[str, float | None, str]:
    normalized = normalize_text(name)
    special_hit = any(normalize_text(p) in normalized for p in special_patterns)
    general_hit = any(normalize_text(p) in normalized for p in general_patterns)
    if special_hit and general_hit:
        return "ambiguous", None, "نام آزمون هم‌زمان الگوی ایمنی عمومی و ایمنی تخصصی را دارد"
    if special_hit:
        return "special", 17.0, "ایمنی تخصصی"
    if general_hit:
        return "general", 14.0, "ایمنی عمومی"
    if "ایمنی" in normalized:
        return "ambiguous", None, "نام آزمون شامل «ایمنی» است ولی نوع عمومی/تخصصی مشخص نیست"
    return "none", None, "برای این آزمون کنترل حدنصاب ایمنی اعمال نمی‌شود"


def build_checks(exam: dict, *, day_start: int, expected_open: int, expected_close: int, safety_general: tuple[str, ...], safety_special: tuple[str, ...], tz_name: str = "Asia/Tehran") -> tuple[list[CheckResult], int]:
    checks: list[CheckResult] = []

    qcount = int(exam.get("question_count") or 0)
    if qcount > 0:
        checks.append(CheckResult("✅", "تعداد سوالات", str(qcount), "سوال در Quiz موجود است"))
    else:
        checks.append(CheckResult("❌", "تعداد سوالات", "0", "هیچ سوالی در آزمون نیست؛ می‌توان از دکمه «اضافه کردن سوالات» استفاده کرد"))

    enrolled = int(exam.get("enrolled_count") or 0)
    if enrolled > 0:
        checks.append(CheckResult("✅", "تعداد نفرات", str(enrolled), "تعداد اعضای فعال Course"))
    else:
        checks.append(CheckResult("⚠️", "تعداد نفرات", "0", "هیچ عضو فعال در Course پیدا نشد"))

    mode, expected_pass, reason = expected_safety_grade(exam.get("name", ""), safety_general, safety_special)
    actual_pass = exam.get("grade_pass")
    def pass_text(value):
        if value is None:
            return "تنظیم نشده"
        try:
            value = float(value)
        except (TypeError, ValueError):
            return str(value)
        return "تنظیم نشده" if value < 0 else f"{value:g}"

    if mode in {"general", "special"}:
        if actual_pass is not None and abs(float(actual_pass) - float(expected_pass)) < 1e-9:
            checks.append(CheckResult("✅", "نمره قبولی", f"{actual_pass:g}", f"برای {reason} مقدار {expected_pass:g} صحیح است"))
        else:
            actual_text = pass_text(actual_pass)
            checks.append(CheckResult("❌", "نمره قبولی", actual_text, f"برای {reason} باید {expected_pass:g} باشد"))
    elif mode == "ambiguous":
        actual_text = pass_text(actual_pass)
        checks.append(CheckResult("⚠️", "نمره قبولی", actual_text, reason))
    else:
        actual_text = pass_text(actual_pass)
        checks.append(CheckResult("✅", "نمره قبولی", actual_text, "این آزمون مشمول قاعده حدنصاب ایمنی نیست"))

    shuffle = exam.get("shuffle_questions")
    if qcount == 0:
        checks.append(CheckResult("⚠️", "بهم‌ریختن سوالات", "قابل اتکا نیست", "آزمون سوال ندارد؛ پس از اضافه شدن سوالات دوباره بررسی کنید"))
    elif shuffle is True:
        checks.append(CheckResult("✅", "بهم‌ریختن سوالات", "فعال", "تمام بخش‌های مرتبط با سوالات فعال هستند"))
    elif shuffle is False:
        checks.append(CheckResult("❌", "بهم‌ریختن سوالات", "غیرفعال", "حداقل یک بخش آزمون Shuffle Questions ندارد"))
    else:
        checks.append(CheckResult("⚠️", "بهم‌ریختن سوالات", "نامشخص", "Moodle مقدار وضعیت را برنگرداند"))

    timelimit = exam.get("timelimit")
    if timelimit is not None and int(timelimit) == 45 * 60:
        checks.append(CheckResult("✅", "مدت آزمون", "45 دقیقه", "زمان محدودیت صحیح است"))
    else:
        actual = "تنظیم نشده" if timelimit in (None, 0) else f"{int(timelimit) // 60} دقیقه"
        checks.append(CheckResult("❌", "مدت آزمون", actual, "باید دقیقاً 45 دقیقه باشد"))

    def local_dt(value: int | None) -> str:
        if not value:
            return "تنظیم نشده"
        return datetime.fromtimestamp(int(value), ZoneInfo(tz_name)).strftime("%Y/%m/%d %H:%M")

    timeopen = exam.get("timeopen")
    if timeopen is not None and int(timeopen) == int(expected_open):
        checks.append(CheckResult("✅", "شروع آزمون", "14:45", "زمان شروع صحیح است"))
    else:
        checks.append(CheckResult("❌", "شروع آزمون", local_dt(timeopen), "باید همان روز ساعت 14:45 باشد"))

    timeclose = exam.get("timeclose")
    if timeclose is not None and int(timeclose) == int(expected_close):
        checks.append(CheckResult("✅", "پایان آزمون", "21:00", "زمان پایان صحیح است"))
    else:
        checks.append(CheckResult("❌", "پایان آزمون", local_dt(timeclose), "باید همان روز ساعت 21:00 باشد"))

    overrides = int(exam.get("override_count") or 0)
    if overrides > 0:
        checks.append(CheckResult("⚠️", "Overrideها", str(overrides), "برای بعضی کاربران/گروه‌ها Override ثبت شده و ممکن است زمان یا محدودیت متفاوت باشد"))

    problems = sum(1 for c in checks if c.icon in {"❌", "⚠️"})
    return checks, problems
