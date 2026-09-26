from __future__ import annotations

import asyncio
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp


@dataclass
class DiagnosticResult:
    number: int
    name: str
    ok: bool | None
    elapsed: float | None = None
    details: list[str] = field(default_factory=list)


class NetworkDiagnostic:
    """Run layered network tests from the same process/container as the bot.

    The goal is to distinguish:
      - DNS failures
      - broken IPv4/IPv6 routes
      - TCP/443 failures
      - TLS/SNI/certificate failures
      - HTTP/Cloudflare gateway failures
      - Moodle REST/Web Service failures
      - unexpected outbound egress behaviour on the PaaS

    No secret token value is ever included in the returned report.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        parsed = urlsplit(self.base_url)
        self.hostname = parsed.hostname or ""
        self.port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        self.scheme = parsed.scheme.lower()

    @staticmethod
    def _fmt_elapsed(started: float) -> float:
        return round(time.monotonic() - started, 2)

    @staticmethod
    def _header_summary(headers: aiohttp.typedefs.LooseHeaders | Any) -> list[str]:
        wanted = (
            "server",
            "cf-ray",
            "cf-cache-status",
            "cf-error-type",
            "cf-error-origin",
            "retry-after",
            "location",
            "content-type",
        )
        out: list[str] = []
        for name in wanted:
            value = headers.get(name)
            if value:
                out.append(f"{name}={value}")
        return out

    async def _resolve(self) -> list[tuple[int, str]]:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            self.hostname,
            self.port,
            type=socket.SOCK_STREAM,
        )
        found: list[tuple[int, str]] = []
        seen: set[tuple[int, str]] = set()
        for family, _socktype, _proto, _canonname, sockaddr in infos:
            ip = sockaddr[0]
            key = (family, ip)
            if key not in seen:
                seen.add(key)
                found.append(key)
        return found

    async def _tcp_test(self, family: int, ip: str) -> tuple[bool, str, float]:
        started = time.monotonic()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=ip,
                    port=self.port,
                    family=family,
                ),
                timeout=8,
            )
            _ = reader
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True, "TCP connection established", self._fmt_elapsed(started)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}", self._fmt_elapsed(started)

    async def _tls_test(self, family: int, ip: str) -> tuple[bool, str, float]:
        started = time.monotonic()
        context = ssl.create_default_context()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=ip,
                    port=self.port,
                    family=family,
                    ssl=context,
                    server_hostname=self.hostname,
                ),
                timeout=12,
            )
            _ = reader
            ssl_obj = writer.get_extra_info("ssl_object")
            cipher = ssl_obj.cipher()[0] if ssl_obj and ssl_obj.cipher() else "-"
            peercert = ssl_obj.getpeercert() if ssl_obj else None
            subject = "-"
            if peercert:
                for subject_part in peercert.get("subject", ()):
                    for key, value in subject_part:
                        if key == "commonName":
                            subject = value
                            break
                    if subject != "-":
                        break
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return (
                True,
                f"TLS handshake OK; cipher={cipher}; certificate_cn={subject}",
                self._fmt_elapsed(started),
            )
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}", self._fmt_elapsed(started)

    async def run(self) -> list[DiagnosticResult]:
        results: list[DiagnosticResult] = []

        # 1. Configuration sanity.
        started = time.monotonic()
        config_details = [
            f"LMS URL: {self.base_url}",
            f"Hostname: {self.hostname or '—'}",
            f"Scheme: {self.scheme or '—'}",
            f"Token configured: {'YES' if self.token else 'NO'}",
            f"Force IPv4: {'YES' if __import__('os').getenv('LMS_FORCE_IPV4','').strip().lower() in {'1','true','yes','on'} else 'NO'}",
        ]
        config_ok = bool(self.hostname and self.scheme == "https" and self.token)
        results.append(
            DiagnosticResult(
                1,
                "پیکربندی مقصد LMS",
                config_ok,
                self._fmt_elapsed(started),
                config_details,
            )
        )

        # 2. DNS.
        resolved: list[tuple[int, str]] = []
        started = time.monotonic()
        if not self.hostname:
            results.append(
                DiagnosticResult(2, "DNS", False, self._fmt_elapsed(started), ["Hostname is empty"])
            )
        else:
            try:
                all_resolved = await self._resolve()
                all_ipv4 = [ip for family, ip in all_resolved if family == socket.AF_INET]
                all_ipv6 = [ip for family, ip in all_resolved if family == socket.AF_INET6]

                # Cloudflare can publish several anycast addresses. Testing every
                # address can make a diagnostic very slow when a whole address
                # family is unreachable, so we test up to two per family.
                resolved = []
                resolved.extend((socket.AF_INET, ip) for ip in all_ipv4[:2])
                resolved.extend((socket.AF_INET6, ip) for ip in all_ipv6[:2])

                addresses = [
                    f"{'IPv4' if family == socket.AF_INET else 'IPv6'}: {ip}"
                    for family, ip in all_resolved
                ]
                details = [
                    f"Resolved addresses: {len(addresses)}",
                    f"IPv4: {', '.join(all_ipv4) if all_ipv4 else 'NONE'}",
                    f"IPv6: {', '.join(all_ipv6) if all_ipv6 else 'NONE'}",
                    f"Addresses selected for TCP/TLS tests: {len(resolved)} (max 2 per family)",
                ]
                details.extend(addresses[:8])
                results.append(
                    DiagnosticResult(2, "DNS", bool(all_resolved), self._fmt_elapsed(started), details)
                )
            except Exception as exc:
                results.append(
                    DiagnosticResult(
                        2,
                        "DNS",
                        False,
                        self._fmt_elapsed(started),
                        [f"{type(exc).__name__}: {exc}"],
                    )
                )

        # 3 + 4. TCP and TLS per resolved address.
        if resolved:
            tcp_details: list[str] = []
            tls_details: list[str] = []
            tcp_ok_count = 0
            tls_ok_count = 0
            tcp_started = time.monotonic()
            for family, ip in resolved:
                ok, detail, elapsed = await self._tcp_test(family, ip)
                if ok:
                    tcp_ok_count += 1
                label = "IPv4" if family == socket.AF_INET else "IPv6"
                tcp_details.append(f"{label} {ip}: {'✅' if ok else '❌'} {detail} ({elapsed:.2f}s)")
            results.append(
                DiagnosticResult(
                    3,
                    "TCP روی پورت 443",
                    tcp_ok_count > 0,
                    self._fmt_elapsed(tcp_started),
                    tcp_details,
                )
            )

            tls_started = time.monotonic()
            for family, ip in resolved:
                ok, detail, elapsed = await self._tls_test(family, ip)
                if ok:
                    tls_ok_count += 1
                label = "IPv4" if family == socket.AF_INET else "IPv6"
                tls_details.append(f"{label} {ip}: {'✅' if ok else '❌'} {detail} ({elapsed:.2f}s)")
            results.append(
                DiagnosticResult(
                    4,
                    "TLS/SSL + SNI",
                    tls_ok_count > 0,
                    self._fmt_elapsed(tls_started),
                    tls_details,
                )
            )
        else:
            results.append(DiagnosticResult(3, "TCP روی پورت 443", False, None, ["Skipped: DNS returned no address"]))
            results.append(DiagnosticResult(4, "TLS/SSL + SNI", False, None, ["Skipped: DNS returned no address"]))

        # 5. Public egress IP, using two independent providers.
        started = time.monotonic()
        egress_details: list[str] = []
        egress_ip: str | None = None
        providers = (
            ("ipify", "https://api.ipify.org?format=json"),
            ("ifconfig.me", "https://ifconfig.me/ip"),
        )
        timeout = aiohttp.ClientTimeout(total=10, connect=5, sock_connect=5, sock_read=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for provider, url in providers:
                    try:
                        async with session.get(url, headers={"User-Agent": "ExamMonitorServerDiagnostic/1.0"}) as response:
                            raw = (await response.text()).strip()
                            if response.status == 200 and raw:
                                if "ipify" in provider:
                                    try:
                                        data = await self._parse_json(raw)
                                        candidate = str(data.get("ip", "")).strip()
                                    except Exception:
                                        candidate = ""
                                else:
                                    candidate = raw.splitlines()[0].strip()
                                if candidate:
                                    egress_ip = candidate
                                    egress_details.append(f"Public outbound IP: {candidate} (source={provider})")
                                    break
                            egress_details.append(f"{provider}: HTTP {response.status}")
                    except Exception as exc:
                        egress_details.append(f"{provider}: {type(exc).__name__}: {exc}")
        except Exception as exc:
            egress_details.append(f"Client setup failed: {type(exc).__name__}: {exc}")
        results.append(
            DiagnosticResult(
                5,
                "IP خروجی عمومی Belmo",
                egress_ip is not None,
                self._fmt_elapsed(started),
                egress_details,
            )
        )

        # 6. Normal HTTPS request to the LMS homepage.
        started = time.monotonic()
        details: list[str] = []
        http_ok = False
        try:
            timeout = aiohttp.ClientTimeout(total=25, connect=8, sock_connect=8, sock_read=20)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "ExamMonitorServerDiagnostic/1.0", "Accept": "text/html,application/json,*/*"},
            ) as session:
                async with session.get(self.base_url, allow_redirects=False) as response:
                    raw = await response.text(errors="replace")
                    http_ok = 200 <= response.status < 400
                    details.append(f"HTTP status: {response.status}")
                    details.extend(self._header_summary(response.headers))
                    if raw:
                        preview = " ".join(raw[:400].split())
                        details.append(f"Body preview: {preview[:400]}")
                    if response.status in {502, 503, 504}:
                        details.append("Gateway error received before a normal Moodle response.")
        except Exception as exc:
            details.append(f"{type(exc).__name__}: {exc}")
        results.append(
            DiagnosticResult(6, "HTTPS به خود سایت LMS", http_ok, self._fmt_elapsed(started), details)
        )

        # 7. Real Moodle REST ping with the same token the bot uses.
        started = time.monotonic()
        details = []
        rest_ok = False
        ping_url = f"{self.base_url}/webservice/rest/server.php"
        if not self.token:
            details.append("Skipped: LMS_TOKEN is not configured")
        else:
            payload = {
                "wstoken": self.token,
                "wsfunction": "local_exammonitor_ping",
                "moodlewsrestformat": "json",
            }
            try:
                timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_connect=10, sock_read=30)
                async with aiohttp.ClientSession(
                    timeout=timeout,
                    headers={
                        "User-Agent": "ExamMonitorServerDiagnostic/1.0",
                        "Accept": "application/json",
                        "Cache-Control": "no-cache",
                    },
                ) as session:
                    async with session.post(ping_url, data=payload, allow_redirects=False) as response:
                        raw = await response.text(errors="replace")
                        details.append(f"HTTP status: {response.status}")
                        details.extend(self._header_summary(response.headers))
                        if response.status == 200:
                            preview = " ".join(raw[:900].split())
                            details.append(f"JSON preview: {preview[:900]}")
                            rest_ok = '"ok"' in raw and '"plugin"' in raw
                        else:
                            preview = " ".join(raw[:1100].split())
                            details.append(f"Response preview: {preview[:1100]}")
                            if response.status in {502, 503, 504}:
                                details.append("This is the exact Web Service path used by the bot.")
            except Exception as exc:
                details.append(f"{type(exc).__name__}: {exc}")
        results.append(
            DiagnosticResult(7, "Moodle REST: local_exammonitor_ping", rest_ok, self._fmt_elapsed(started), details)
        )

        # 8. Second REST ping to measure consistency after the first call.
        started = time.monotonic()
        details = []
        repeat_ok = False
        if not self.token:
            details.append("Skipped: LMS_TOKEN is not configured")
        else:
            payload = {
                "wstoken": self.token,
                "wsfunction": "local_exammonitor_ping",
                "moodlewsrestformat": "json",
            }
            try:
                timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_connect=10, sock_read=30)
                async with aiohttp.ClientSession(
                    timeout=timeout,
                    headers={
                        "User-Agent": "ExamMonitorServerDiagnostic/1.0",
                        "Accept": "application/json",
                        "Cache-Control": "no-cache",
                    },
                ) as session:
                    async with session.post(ping_url, data=payload, allow_redirects=False) as response:
                        raw = await response.text(errors="replace")
                        details.append(f"HTTP status: {response.status}")
                        details.extend(self._header_summary(response.headers))
                        if response.status == 200:
                            repeat_ok = '"ok"' in raw and '"plugin"' in raw
                            details.append("Second consecutive ping also succeeded.")
                        else:
                            details.append(f"Response preview: {' '.join(raw[:900].split())[:900]}")
            except Exception as exc:
                details.append(f"{type(exc).__name__}: {exc}")
        results.append(
            DiagnosticResult(8, "تکرار REST ping برای پایداری اتصال", repeat_ok, self._fmt_elapsed(started), details)
        )

        return results

    @staticmethod
    async def _parse_json(raw: str) -> dict[str, Any]:
        import json

        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value


def render_result(result: DiagnosticResult) -> str:
    if result.ok is True:
        state = "✅ موفق"
    elif result.ok is False:
        state = "❌ ناموفق"
    else:
        state = "ℹ️ انجام نشد"

    lines = [
        f"🧪 تست {result.number}: {result.name}",
        f"وضعیت: {state}",
    ]
    if result.elapsed is not None:
        lines.append(f"⏱️ زمان: {result.elapsed:.2f} ثانیه")
    for detail in result.details:
        lines.append(f"• {detail}")
    return "\n".join(lines)


def render_summary(results: list[DiagnosticResult]) -> str:
    passed = sum(1 for r in results if r.ok is True)
    failed = sum(1 for r in results if r.ok is False)
    skipped = sum(1 for r in results if r.ok is None)

    by_num = {r.number: r for r in results}

    lines = [
        "📊 جمع‌بندی تست سرور",
        "━━━━━━━━━━━━━━━━━━",
        f"✅ موفق: {passed}",
        f"❌ ناموفق: {failed}",
        f"ℹ️ انجام‌نشده: {skipped}",
        "",
        "ترتیب تست‌ها:",
    ]
    for result in results:
        icon = "✅" if result.ok is True else "❌" if result.ok is False else "ℹ️"
        timing = f" — {result.elapsed:.2f}s" if result.elapsed is not None else ""
        lines.append(f"{icon} {result.number}. {result.name}{timing}")

    lines.extend(["", "🧭 تشخیص پیشنهادی:"])

    def failed(n: int) -> bool:
        r = by_num.get(n)
        return bool(r and r.ok is False)

    def ok(n: int) -> bool:
        r = by_num.get(n)
        return bool(r and r.ok is True)

    if failed(1):
        lines.append("• پیکربندی LMS ناقص است (LMS_URL باید https://... باشد و LMS_TOKEN ست شود).")
    elif failed(2):
        lines.append("• DNS از سرور ربات resolve نمی‌شود — مشکل DNS/شبکه خروجی Belmo.")
    elif failed(3):
        lines.append("• TCP/443 به IPهای LMS برقرار نمی‌شود — فایروال یا مسیر شبکه مسدود است.")
    elif failed(4):
        lines.append("• TLS/SNI شکست خورده — گواهی یا مسیر HTTPS مشکل دارد.")
    elif failed(6) and ok(4):
        lines.append("• HTTPS به دامنه 504/خطا می‌دهد ولی TLS به IP OK بود → معمولاً Cloudflare/WAF یا origin timeout.")
    elif failed(7) or failed(8):
        if ok(6):
            lines.append("• سایت باز می‌شود ولی REST ping شکست می‌خورد → توکن/سرویس/وب‌سرویس را دوباره چک کنید.")
        else:
            lines.append("• REST ping از Belmo به LMS نمی‌رسد (همان مسیر 504).")
            lines.append("• IP خروجی تست ۵ را در Cloudflare/Firewall Allowlist کنید.")
            lines.append("• یا Cloudflare را DNS only کنید و دوباره تست بزنید.")
            lines.append("• یا ربات را روی سرور نزدیک LMS / همان سرور LMS با Docker اجرا کنید.")
    elif ok(7) and ok(8):
        lines.append("• اتصال Belmo→LMS سالم است. اگر get_status هنوز 504 می‌دهد، timeout سرور/PHP را برای همان تابع بالا ببرید.")
    else:
        lines.append("• جزئیات تست‌های ناموفق را بفرستید تا لایه دقیق مشخص شود.")

    # Egress IP hint
    egress = by_num.get(5)
    if egress and egress.details:
        for d in egress.details:
            if "IP" in d or "ip" in d.lower() or any(ch.isdigit() for ch in d):
                lines.append(f"• برای Allowlist از خروجی تست ۵ استفاده کنید: {d}")
                break

    return "\n".join(lines)
