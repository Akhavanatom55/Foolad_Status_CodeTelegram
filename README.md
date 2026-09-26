# رفع مشکل ربات Exam Monitor + پنل ادمین خصوصی

## علت خطای 504

پیام:
`❌ LMS از مسیر Web Service پاسخ نداد (Gateway error). HTTP 504 from Moodle: Cloudflare/reverse-proxy...`

این خطا **از سمت سرور LMS / Cloudflare** است، نه توکن بله و نه باگ منطق ربات:

1. Cloudflare (یا reverse proxy) حدود ۱۵ ثانیه منتظر origin می‌ماند.
2. اگر PHP-FPM / Moodle در آن زمان پاسخ ندهد، HTML خطای 504 برمی‌گردد.
3. حتی تابع سبک `local_exammonitor_ping` اگر پلاگین نصب نباشد یا سرویس وب‌سرویس گیر کند، همین 504 را می‌دهد.
4. تابع قدیمی `get_status` با چند کوئری به‌ازای هر Quiz (به‌خصوص شمارش enrollment) روی سایت‌های شلوغ از timeout رد می‌شد.

**اقدام اجباری روی سرور LMS:**

1. پلاگین `local_exammonitor_1.0.4.zip` را در مسیر `local/exammonitor` نصب/آپدیت کنید.
2. Site administration → Notifications → Upgrade.
3. Web services → External services → سرویس **Exam Monitor Integration** را فعال و توکن را به آن وصل کنید (قابلیت `local/exammonitor:access`).
4. در Cloudflare / Nginx / Apache: Origin timeout را حداقل ۶۰–۱۲۰ ثانیه کنید.
5. PHP-FPM: `request_terminate_timeout` و `max_execution_time` برای مسیر webservice را بالا ببرید.
6. بعد از آپدیت، از ربات «تست اتصال LMS» را بزنید؛ باید نسخه `1.0.4` و `db_ok=true` برگردد.

## باگ پنل ادمین در چت خصوصی (رفع شد)

در `bot.py` قبل از هر دستور چک می‌شد که `chat_id` در `ALLOWED_GROUP_IDS` باشد.
در چت خصوصی `chat_id == user_id` است و معمولاً در لیست گروه نیست → پیام‌های ادمین (از جمله `/admin` و `/start`) بی‌صدا دور انداخته می‌شدند.

**الان:**
- ادمین‌های داخل `ADMIN_IDS` می‌توانند در **چت خصوصی با ربات** `/start` و `/admin` بزنند و پنل را ببینند.
- در گروه فقط گروه‌های مجاز (یا همه اگر لیست خالی باشد) کار می‌کنند.
- کاربران غیر ادمین در چت خصوصی همچنان نادیده گرفته می‌شوند.

## فایل‌های تحویلی

| فایل | توضیح |
|------|--------|
| `local_exammonitor_1.0.4.zip` | پلاگین کامل Moodle — نصب روی LMS |
| `bot_code_changes.zip` | فقط فایل‌های تغییرکرده ربات: `bot.py`, `moodle.py`, `config.py` |

### استقرار ربات (Belmo / GitHub)

فقط این سه فایل را در ریپو جایگزین کنید و دوباره deploy کنید:

- `bot.py`
- `moodle.py`
- `config.py`

تغییرات جزئی در کلاینت Moodle:
- `max_retries` پیش‌فرض ۲ (بازیابی سریع‌تر از 504 موقت)
- پیام خطای تست اتصال راهنمای اقدامات سرور را نشان می‌دهد

### استقرار پلاگین

محتوای zip را در `{moodledata parent}/local/exammonitor` بریزید یا از «Install plugin» آپلود کنید، سپس Upgrade و purge caches.

## متغیرهای محیطی مهم

```
ADMIN_IDS=123456,789012
ALLOWED_GROUP_IDS=-1001234567890
LMS_URL=https://lms.helpsy.ir
LMS_TOKEN=...
LMS_REQUEST_TIMEOUT=75
LMS_MAX_RETRIES=2
LMS_OPERATION_TIMEOUT=300
```

بعد از نصب پلاگین ۱.۰.۴ و باز بودن origin، 504 نباید تکرار شود. اگر همچنان 504 دیدید، مشکل ۱۰۰٪ timeout سمت وب‌سرور/Cloudflare است و باید روی همان سرور بررسی شود.
