#!/usr/bin/env python3
"""
converter.py
============
هسته‌ی اصلی تبدیل فایل اکسل سوالات چهارگزینه‌ای فارسی به فرمت Moodle XML.

این ماژول هم به‌صورت مستقل (CLI) قابل اجراست و هم توسط ربات بله
(bot.py) به‌عنوان یک import استفاده می‌شود، بدون هیچ تغییری در منطق اصلی
تبدیل که قبلاً روی آن کار شده بود.

نکته: نسبت به نسخه‌ی اولیه، دو باگ اصلاح شده است (در پایین فایل CHANGELOG
توضیح داده شده):
  1) متغیر `feedback` قبل از استفاده مقداردهی نشده بود و باعث کرش در
     همان اولین سوال معتبر می‌شد؛ اکنون از ستون feedback (در صورت شناسایی)
     استخراج می‌شود.
  2) در `_detect_header_row_and_mapping` مقایسه‌ی بهترین ردیف سربرگ از
     متغیر اشتباه (`score` باقیمانده از حلقه‌ی داخلی) استفاده می‌کرد؛
     اصلاح شد تا از `total_score` (مجموع امتیاز تمام ستون‌های شناسایی‌شده
     در آن ردیف) استفاده کند.
"""
from __future__ import annotations

import argparse
import base64
import logging
import mimetypes
import posixpath
import re
import sys
import tempfile
import types
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Tuple, List, Dict, Optional


# =========================================================
# Compatibility shim (harmless here; script does not use openpyxl)
# =========================================================
def _install_compat_shim() -> None:
    if "converter" not in sys.modules:
        pkg = types.ModuleType("converter")
        pkg.__path__ = []  # type: ignore[attr-defined]
        sys.modules["converter"] = pkg

    if "converter.issues" not in sys.modules:
        mod = types.ModuleType("converter.issues")
        mod.default_log_path = str(Path(tempfile.gettempdir()) / "openpyxl.log")
        sys.modules["converter.issues"] = mod
        sys.modules["converter"].issues = mod  # type: ignore[attr-defined]


_install_compat_shim()

try:
    from rapidfuzz import fuzz as _rf_fuzz  # type: ignore
except Exception:
    _rf_fuzz = None

# =========================================================
# Defaults / supported Excel formats
# =========================================================
# These OOXML workbook formats share the same ZIP/XML structure used by the
# built-in workbook reader below. Legacy .xls (BIFF) is intentionally not
# accepted by this lightweight reader.
SUPPORTED_EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xltx", ".xltm"}
DEFAULT_INPUT_XLSX = Path("input.xlsx")


# =========================================================
# Logging
# =========================================================
class ColoredFormatter(logging.Formatter):
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if record.levelno >= logging.ERROR:
            color = self.RED
        elif record.levelno == logging.WARNING:
            color = self.YELLOW
        elif record.levelno == logging.INFO:
            color = self.GREEN
        elif record.levelno == logging.DEBUG:
            color = self.CYAN
        else:
            color = self.RESET
        return f"{color}{msg}{self.RESET}"


def configure_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColoredFormatter("%(levelname)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)


# =========================================================
# Data model
# =========================================================
class ExcelReadError(Exception):
    pass


@dataclass
class ImageOptimizeOptions:
    enabled: bool = True
    max_width: int = 800
    max_height: int = 600
    jpeg_quality: int = 80


@dataclass
class EmbeddedImage:
    filename: str
    mime_type: str
    data: bytes
    source_path: str


# =========================================================
# Text normalization and aliases
# =========================================================
_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ENGLISH_DIGITS = "0123456789"

_TRANSLATION_TABLE = str.maketrans({
    "ي": "ی",
    "ك": "ک",
    "ۀ": "ه",
    "ؤ": "و",
    "إ": "ا",
    "أ": "ا",
    "آ": "ا",
    "‌": " ",
    "\u200f": " ",
    "\u202a": " ",
    "\u202b": " ",
    "\u202c": " ",
    "\u202d": " ",
    "\u202e": " ",
})
for p, e in zip(_PERSIAN_DIGITS, _ENGLISH_DIGITS):
    _TRANSLATION_TABLE[ord(p)] = e

COLUMN_ALIASES: Dict[str, List[str]] = {
    "question": [
        "سوال", "سؤال", "متن سوال", "متن سؤال", "شرح سوال", "شرح سؤال",
        "شرح سوال با توجه هر سرفصل", "سوالات دوره", "question", "question text", "stem",
        "سوالات"  # Added based on feedback
    ],
    "option1": [
        "گزینه 1", "گزینه1", "شرح گزینه 1", "شرح گزینه1",
        "answer 1", "answer1", "option 1", "option1", "choice 1", "choice1"
    ],
    "option2": [
        "گزینه 2", "گزینه2", "شرح گزینه 2", "شرح گزینه2",
        "answer 2", "answer2", "option 2", "option2", "choice 2", "choice2"
    ],
    "option3": [
        "گزینه 3", "گزینه3", "شرح گزینه 3", "شرح گزینه3",
        "answer 3", "answer3", "option 3", "option3", "choice 3", "choice3"
    ],
    "option4": [
        "گزینه 4", "گزینه4", "شرح گزینه 4", "شرح گزینه4",
        "answer 4", "answer4", "option 4", "option4", "choice 4", "choice4"
    ],
    "correct": [
        "پاسخ صحیح", "پاسخ", "جواب صحیح", "جواب", "گزینه صحیح", "شماره گزینه صحیح",
        "correct", "correct answer", "answer key", "key"
    ],
    "feedback": ["توضیح", "توضیحات", "شرح", "explanation", "feedback"],
}


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().translate(_TRANSLATION_TABLE)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _similarity(a: str, b: str) -> int:
    a_n = normalize_text(a)
    b_n = normalize_text(b)
    if not a_n or not b_n:
        return 0
    if a_n == b_n:
        return 100
    a_c = a_n.replace(" ", "")
    b_c = b_n.replace(" ", "")
    if a_c == b_c:
        return 100

    score = 0
    # Prefer exact or substring matches
    if a_n in b_n or b_n in a_n or a_c in b_c or b_c in a_c:
        score += 35

    if _rf_fuzz is not None:
        # Use rapidfuzz if available for better performance and accuracy
        score += int(_rf_fuzz.ratio(a_n, b_n) * 0.65)
    else:
        # Fallback to difflib if rapidfuzz is not installed
        from difflib import SequenceMatcher
        score += int(SequenceMatcher(None, a_n, b_n).ratio() * 65)
    return min(score, 100)


def _best_alias_match(header_value: Any, aliases: List[str]) -> int:
    header = normalize_text(header_value)
    if not header:
        return 0
    return max(_similarity(header, alias) for alias in aliases)


def clean_question_text(text: Optional[str]) -> str:
    if text is None:
        return ""
    text = str(text).strip().replace("\r", "\n")
    # Remove leading question numbers/markers
    text = re.sub(r"^\s*(?:سوال|سؤال)?\s*\d+\s*[\.\)\-:،]\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^\s*\d+\s*[\.\)\-:،]\s*", "", text).strip()
    return text


# =========================================================
# Fuzzy detection of answer-order-sensitive questions
# =========================================================
# Some question banks contain meta-options such as «همه موارد»، «هیچکدام»
# or combined choices such as «گزینه الف و ب». Moodle must NOT shuffle the
# answers for those questions, because changing their order changes the
# meaning of the option labels. Detection is intentionally conservative: a
# fuzzy score is used for known phrases, while combined-letter options require
# two distinct option labels plus a connector/punctuation pattern.
_NO_OPTION_PHRASES = (
    "هیچکدام", "هیچ کدام", "هیچ یک", "هیچیک", "هیچ یک از موارد",
    "none of the above", "none of these",
)
_ALL_OPTION_PHRASES = (
    "همه موارد", "همه ی موارد", "همه موارد بالا", "تمام موارد",
    "تمام موارد بالا", "کلیه موارد", "همه گزینه ها", "همه گزینهها",
    "all of the above", "all of these",
)
_OPTION_LETTER_ALIASES = {
    "الف": "a", "a": "a",
    "ب": "b", "b": "b",
    "ج": "c", "جیم": "c", "c": "c",
    "د": "d", "دال": "d", "d": "d",
}


def _fuzzy_phrase_match(text: str, phrases: Tuple[str, ...], threshold: int = 82) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    compact = normalized.replace(" ", "")

    for phrase in phrases:
        phrase_n = normalize_text(phrase)
        if phrase_n in normalized or phrase_n.replace(" ", "") in compact:
            return True
        if _rf_fuzz is not None:
            # Do not use partial_ratio here: short ordinary options such as
            # «گزینه الف» can otherwise score spuriously high against phrases
            # like «همه موارد». Ratio-based fuzzy matching remains tolerant
            # of small spelling variations without broad false positives.
            if _rf_fuzz.ratio(normalized, phrase_n) >= threshold:
                return True
        else:
            from difflib import SequenceMatcher
            if SequenceMatcher(None, normalized, phrase_n).ratio() * 100 >= threshold:
                return True
    return False


def _has_combined_option_letters(text: str) -> bool:
    """Detect compound answer labels such as «الف و ب», «گزینه 1 و 2»."""
    normalized = normalize_text(text)
    if not normalized:
        return False

    # The important pattern is a pair/list of option labels separated by a
    # connector or punctuation. Support both Persian letters and numeric
    # labels, with or without the word «گزینه».
    label = r"(?:گزینه\s*)?(?:الف|ب|ج|د|a|b|c|d|جیم|دال|[1-4])"
    connector = r"(?:و|یا|[,،/\+&])"
    pair_pattern = rf"{label}\s*{connector}\s*{label}"
    if re.search(pair_pattern, normalized, flags=re.IGNORECASE):
        return True

    # Also catch list-style combinations such as «الف، ب، ج» or
    # «گزینه 1، 2، 3». Require at least two labels to avoid normal text.
    labels = re.findall(r"(?:^|\s|گزینه\s*)(الف|ب|ج|د|a|b|c|d|جیم|دال|[1-4])(?=$|\s|[,،/\+&])", normalized, flags=re.IGNORECASE)
    if len(set(labels)) >= 2 and re.search(r"[,،/\+&]", normalized):
        return True

    return False


def _contains_meta_option_phrase(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False

    # Explicit phrases whose order/meaning depends on the labels.
    if _fuzzy_phrase_match(normalized, _NO_OPTION_PHRASES):
        return True
    if _fuzzy_phrase_match(normalized, _ALL_OPTION_PHRASES):
        return True

    # More variants commonly found in Persian question banks.
    compact = normalized.replace(" ", "")
    extra_phrases = (
        "هیچکدامازموارد", "هیچکدامازگزینهها", "هیچکدامازگزینههادرستنیست",
        "همهمواردصحیحاست", "همهموارددرستاست", "همهمواردبالا",
        "هیچکدامموارد", "تماممواردبالا", "کلیهمواردبالا",
        "هیچیکازموارد", "هیچیکازگزینهها",
    )
    return any(phrase in compact for phrase in extra_phrases)

def should_disable_shuffle(options: List[str]) -> bool:
    """Return True when the answer order must be preserved for this question."""
    for option in options:
        if not option:
            continue
        if _contains_meta_option_phrase(option):
            return True
        if _has_combined_option_letters(option):
            return True
    return False


# =========================================================
# XLSX parsing without openpyxl
# =========================================================
_CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _resolve_zip_target(base_dir: str, target: str) -> str:
    """Resolve an OOXML relationship Target to a normalized ZIP member path."""
    target = target.replace("\\", "/")
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target)).lstrip("./")


def _parse_relationships(zf: zipfile.ZipFile, rels_path: str) -> Dict[str, str]:
    try:
        raw = _read_zip_text(zf, rels_path)
    except KeyError:
        return {}
    root = ET.fromstring(raw)
    rels: Dict[str, str] = {}
    for rel in list(root):
        rid = rel.attrib.get("Id", "")
        target = rel.attrib.get("Target", "")
        if rid and target:
            rels[rid] = target
    return rels


def _extract_images_from_sheet(zf: zipfile.ZipFile, sheet_path: str) -> Dict[Tuple[int, int], List[EmbeddedImage]]:
    """Return images anchored to sheet cells as (zero_based_row, zero_based_col)."""
    result: Dict[Tuple[int, int], List[EmbeddedImage]] = {}
    sheet_dir = posixpath.dirname(sheet_path)
    sheet_name = posixpath.basename(sheet_path)
    sheet_rels = posixpath.join(sheet_dir, "_rels", sheet_name + ".rels")
    sheet_rels_map = _parse_relationships(zf, sheet_rels)
    if not sheet_rels_map:
        return result

    try:
        sheet_raw = _read_zip_text(zf, sheet_path)
        sheet_root = ET.fromstring(sheet_raw)
    except (KeyError, ET.ParseError):
        return result

    ns = _xml_ns(sheet_root)
    nsf = {"m": ns} if ns else {}
    drawing_node = sheet_root.find(".//m:drawing", nsf) if ns else sheet_root.find(".//drawing")
    if drawing_node is None:
        return result

    rid_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    rid = drawing_node.attrib.get(rid_attr, "")
    drawing_target = sheet_rels_map.get(rid)
    if not drawing_target:
        return result

    drawing_path = _resolve_zip_target(sheet_dir, drawing_target)
    drawing_dir = posixpath.dirname(drawing_path)
    drawing_name = posixpath.basename(drawing_path)
    drawing_rels_path = posixpath.join(drawing_dir, "_rels", drawing_name + ".rels")
    drawing_rels = _parse_relationships(zf, drawing_rels_path)
    if not drawing_rels:
        return result

    try:
        drawing_raw = _read_zip_text(zf, drawing_path)
        drawing_root = ET.fromstring(drawing_raw)
    except (KeyError, ET.ParseError):
        return result

    xdr_ns = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    xdr = {"xdr": xdr_ns, "a": a_ns}

    anchors = drawing_root.findall("xdr:twoCellAnchor", xdr) + drawing_root.findall("xdr:oneCellAnchor", xdr)
    for anchor in anchors:
        frm = anchor.find("xdr:from", xdr)
        if frm is None:
            continue
        row_el = frm.find("xdr:row", xdr)
        col_el = frm.find("xdr:col", xdr)
        if row_el is None or col_el is None or row_el.text is None or col_el.text is None:
            continue
        try:
            row_idx = int(row_el.text)
            col_idx = int(col_el.text)
        except ValueError:
            continue

        for blip in anchor.findall(".//a:blip", xdr):
            embed = blip.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed", "")
            media_target = drawing_rels.get(embed)
            if not media_target:
                continue
            media_path = _resolve_zip_target(drawing_dir, media_target)
            try:
                data = zf.read(media_path)
            except KeyError:
                logging.warning("تصویر %s در فایل Excel پیدا نشد.", media_path)
                continue
            ext = Path(media_path).suffix.lower().lstrip(".") or "png"
            mime = mimetypes.types_map.get("." + ext, "application/octet-stream")
            filename = Path(media_path).name or f"image_{row_idx+1}_{col_idx+1}.{ext}"
            result.setdefault((row_idx, col_idx), []).append(EmbeddedImage(filename, mime, data, media_path))

    return result


def _collect_workbook_images(zf: zipfile.ZipFile, all_sheets: List[Tuple[str, str]]) -> Dict[str, Dict[Tuple[int, int], List[EmbeddedImage]]]:
    images_by_sheet: Dict[str, Dict[Tuple[int, int], List[EmbeddedImage]]] = {}
    for _, sheet_path in all_sheets:
        imgs = _extract_images_from_sheet(zf, sheet_path)
        if imgs:
            images_by_sheet[sheet_path] = imgs
    return images_by_sheet


def _col_to_index(col_letters: str) -> int:
    n = 0
    for ch in col_letters:
        if "A" <= ch <= "Z":
            n = n * 26 + (ord(ch) - 64)
    return n - 1


def _cell_ref_to_index(ref: str) -> Tuple[int, int]:
    m = _CELL_REF_RE.match(ref.upper())
    if not m:
        return 0, 0  # Return default if no match
    return int(m.group(2)) - 1, _col_to_index(m.group(1))


def _read_zip_text(zf: zipfile.ZipFile, member: str) -> str:
    with zf.open(member) as fp:
        return fp.read().decode("utf-8", errors="replace")


def _xml_ns(root: ET.Element) -> str:
    if root.tag.startswith("{") and "}" in root.tag:
        return root.tag[1: root.tag.index("}")]
    return ""


def _parse_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    try:
        raw = _read_zip_text(zf, "xl/sharedStrings.xml")
    except KeyError:
        return []

    root = ET.fromstring(raw)
    ns = _xml_ns(root)
    nsf = {"m": ns} if ns else {}

    strings: List[str] = []
    if ns:
        si_nodes = root.findall(".//m:si", nsf)
        t_query = ".//m:t"
    else:
        si_nodes = root.findall(".//si")
        t_query = ".//t"

    for si in si_nodes:
        parts: List[str] = []
        for t in si.findall(t_query, nsf if ns else {}):
            if t.text:
                parts.append(t.text)
        strings.append("".join(parts))
    return strings


def _parse_workbook_sheets(zf: zipfile.ZipFile) -> List[Tuple[str, str]]:
    workbook_raw = _read_zip_text(zf, "xl/workbook.xml")
    workbook_root = ET.fromstring(workbook_raw)
    wb_ns = _xml_ns(workbook_root)
    wb_nsf = {"m": wb_ns} if wb_ns else {}

    rels_raw = _read_zip_text(zf, "xl/_rels/workbook.xml.rels")
    rels_root = ET.fromstring(rels_raw)
    rels_ns = _xml_ns(rels_root)
    rels_nsf = {"r": rels_ns} if rels_ns else {}

    rid_to_target: Dict[str, str] = {}
    if rels_ns:
        rel_nodes = rels_root.findall(".//r:Relationship", rels_nsf)
    else:
        rel_nodes = rels_root.findall(".//Relationship")
    for rel in rel_nodes:
        rid = rel.attrib.get("Id", "")
        target = rel.attrib.get("Target", "")
        if rid and target:
            # Ensure target path is correct for zip access.
            # (fix) Relationship targets can be either relative to xl/ (e.g.
            # "worksheets/sheet1.xml") or package-root-absolute (e.g.
            # "/xl/worksheets/sheet1.xml", which several writers, including
            # openpyxl, emit). The previous logic always prefixed with "xl/",
            # which produced a bogus "xl/xl/worksheets/sheet1.xml" for the
            # absolute form and made every worksheet unreadable.
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = "xl/" + target
            rid_to_target[rid] = target

    sheets: List[Tuple[str, str]] = []
    if wb_ns:
        sheet_nodes = workbook_root.findall(".//m:sheets/m:sheet", wb_nsf)
    else:
        sheet_nodes = workbook_root.findall(".//sheet")

    for sheet in sheet_nodes:
        name = sheet.attrib.get("name", "")
        rid = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        target = rid_to_target.get(rid, "")
        if not target:
            continue
        sheets.append((name, target))

    if not sheets:
        raise ExcelReadError("No worksheets found in workbook.xml")
    return sheets


def _parse_cell_value(c: ET.Element, nsf: Dict[str, str], shared_strings: List[str]) -> Any:
    cell_type = c.attrib.get("t", "")
    if cell_type == "s":  # Shared string
        v = c.find("./m:v", nsf) if nsf else c.find("./v")
        if v is not None and v.text is not None:
            try:
                idx = int(v.text)
                return shared_strings[idx] if 0 <= idx < len(shared_strings) else ""
            except Exception:
                return ""
        return ""
    if cell_type == "inlineStr":  # Inline string
        parts = c.findall(".//m:t", nsf) if nsf else c.findall(".//t")
        return "".join(t.text or "" for t in parts)

    v = c.find("./m:v", nsf) if nsf else c.find("./v")
    if v is not None and v.text is not None:
        return v.text
    # Check for rich text (is)
    is_node = c.find("./m:is", nsf) if nsf else c.find("./is")
    if is_node is not None:
        parts = is_node.findall(".//m:t", nsf) if nsf else is_node.findall(".//t")
        return "".join(t.text or "" for t in parts)
    return ""


def _read_sheet_rows(zf: zipfile.ZipFile, sheet_path: str, shared_strings: List[str]) -> List[List[Any]]:
    raw = _read_zip_text(zf, sheet_path)
    root = ET.fromstring(raw)
    ns = _xml_ns(root)
    nsf = {"m": ns} if ns else {}

    rows: List[List[Any]] = []
    if ns:
        row_nodes = root.findall(".//m:sheetData/m:row", nsf)
        cell_query = "./m:c"
    else:
        row_nodes = root.findall(".//sheetData/row")
        cell_query = "./c"

    for row_node in row_nodes:
        row_index_str = row_node.attrib.get("r")
        row_index = int(row_index_str) if row_index_str else len(rows) + 1

        # Fill empty rows up to the current row_index (1-based to 0-based)
        while len(rows) < row_index - 1:
            rows.append([])

        row_values: Dict[int, Any] = {}
        max_col = -1
        for c in row_node.findall(cell_query, nsf if ns else {}):
            ref = c.attrib.get("r", "")
            if not ref:
                continue
            _, col_idx = _cell_ref_to_index(ref)
            max_col = max(max_col, col_idx)
            row_values[col_idx] = _parse_cell_value(c, nsf, shared_strings)

        if max_col < 0:  # If row has no cells
            rows.append([])
            continue

        # Create a list for the current row, filling missing columns with None
        row_list = [None] * (max_col + 1)
        for idx, value in row_values.items():
            if 0 <= idx <= max_col:
                row_list[idx] = value
        rows.append(row_list)

    return rows


def _detect_header_row_and_mapping(rows: List[List[Any]]) -> Tuple[int, Optional[Dict[str, int]]]:
    best_row_idx = -1
    best_mapping: Dict[str, int] = {}
    best_score = -1
    scan_limit = min(len(rows), 20)  # Scan up to 20 rows for headers

    for r_idx in range(scan_limit):
        row = rows[r_idx]
        mapping: Dict[str, int] = {}
        used_cols: set[int] = set()
        total_score = 0

        # Try to match each required column alias
        for key, aliases in COLUMN_ALIASES.items():
            col_best = -1
            col_best_score = 0
            for c_idx, cell in enumerate(row):
                if c_idx in used_cols:  # Don't reuse columns
                    continue
                score = _best_alias_match(cell, aliases)
                if score > col_best_score:
                    col_best_score = score
                    col_best = c_idx
            # A column is considered a match if score is reasonably high (e.g., >= 70)
            if col_best_score >= 70 and col_best >= 0:
                mapping[key] = col_best
                used_cols.add(col_best)
                total_score += col_best_score

        # Check if essential columns are found
        required_columns = {"question", "option1", "option2", "option3", "option4"}
        found_required = required_columns.intersection(mapping)

        # A strong match for required columns boosts the score
        # We need at least 4 options and the question text to consider it a valid header row.
        # NOTE (fix): compare against total_score (sum of matched column scores for this
        # row), not the leftover `score` from the innermost loop above.
        if len(found_required) >= len(required_columns) and total_score > best_score:
            best_score = total_score
            best_row_idx = r_idx
            best_mapping = mapping

    if best_row_idx < 0 or not {"question", "option1", "option2", "option3", "option4"}.issubset(best_mapping):
        return -1, None  # No suitable header row found with all required columns

    return best_row_idx, best_mapping


def _first_non_empty(values: List[Any]) -> str:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _resolve_correct_index(correct_value: Any, options: List[str]) -> int:
    text = normalize_text(correct_value)
    if not text:
        return 1  # Default to option 1 if no correct answer is specified

    # Try to find a number (1-4) in the text
    m = re.search(r"(?<!\d)([1-4])(?!\d)", text)
    if m:
        return int(m.group(1))

    # Try to match "گزینه X" or "option X"
    for i in range(1, 5):
        if f"گزینه {i}" in text or f"option {i}" in text or f"choice {i}" in text:
            return i

    # Try to match the text of the correct option itself (fuzzy match)
    best_match_idx = -1
    best_match_score = 0
    for idx, opt in enumerate(options, start=1):
        opt_n = normalize_text(opt)
        score = _similarity(text, opt_n)
        if score > best_match_score:
            best_match_score = score
            best_match_idx = idx

    if best_match_idx != -1 and best_match_score >= 80:  # Require a high similarity for text match
        return best_match_idx

    return 1  # Fallback


# =========================================================
# Read questions
# =========================================================
def read_questions_from_workbook(zf: zipfile.ZipFile, all_sheets: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    shared_strings = _parse_shared_strings(zf)
    images_by_sheet = _collect_workbook_images(zf, all_sheets)

    all_questions: List[Dict[str, Any]] = []

    for sheet_name, sheet_path in all_sheets:
        logging.info("📄 در حال بررسی شیت: '%s'", sheet_name)

        try:
            rows = _read_sheet_rows(zf, sheet_path, shared_strings)
            sheet_images = images_by_sheet.get(sheet_path, {})
            if not rows:
                logging.warning("شیت '%s' خالی است. به شیت بعدی می‌رویم.", sheet_name)
                continue

            header_row_idx, mapping = _detect_header_row_and_mapping(rows)

            if mapping is None:
                logging.warning("در شیت '%s'، ردیف سربرگ با ستون‌های مورد نیاز پیدا نشد. به شیت بعدی می‌رویم.",
                                sheet_name)
                continue

            logging.info("🧭 تشخیص ستون‌ها در شیت '%s' در ردیف %d انجام شد:", sheet_name, header_row_idx + 1)
            for key in ["question", "option1", "option2", "option3", "option4", "correct", "feedback"]:
                if key in mapping:
                    col = mapping[key] + 1
                    logging.info("   %s -> ستون %d (%s)", key, col, rows[header_row_idx][mapping[key]])
                else:
                    logging.info("   %s -> پیدا نشد", key)

            questions_in_sheet: List[Dict[str, Any]] = []
            skipped = 0

            for excel_row_num, row in enumerate(rows[header_row_idx + 1:], start=header_row_idx + 2):
                if not any(v is not None and str(v).strip() != "" for v in row):
                    continue  # Skip entirely empty rows

                q_text = ""
                if "question" in mapping and mapping["question"] < len(row):
                    q_text = _first_non_empty([row[mapping["question"]]])

                # If primary question column is empty, try to derive question text from first non-empty cell in row
                if not q_text:
                    q_text = _first_non_empty(row)

                option_values: List[str] = []
                for opt_key in ["option1", "option2", "option3", "option4"]:
                    idx = mapping.get(opt_key)
                    val = ""
                    if idx is not None and idx < len(row):
                        val = "" if row[idx] is None else str(row[idx]).strip()
                    option_values.append(val)

                question_cell_images = []
                question_col_for_row = mapping.get("question")
                if question_col_for_row is not None:
                    question_cell_images = sheet_images.get((excel_row_num - 1, question_col_for_row), [])
                option_image_count = 0
                for opt_key in ["option1", "option2", "option3", "option4"]:
                    opt_col = mapping.get(opt_key)
                    if opt_col is not None:
                        option_image_count += len(sheet_images.get((excel_row_num - 1, opt_col), []))

                non_empty_count = sum(1 for x in option_values if x)
                effective_option_count = non_empty_count + option_image_count
                if (not q_text and not question_cell_images) or effective_option_count < 2:
                    skipped += 1
                    logging.warning(
                        "Row %d (Sheet '%s') skipped: question/options not enough data. question=%r options=%r",
                        excel_row_num, sheet_name, q_text, option_values
                    )
                    continue

                # Filter out empty options before determining correct index and creating choices
                valid_options = [
                    opt for idx, opt in enumerate(option_values, start=1)
                    if opt or (mapping.get(f"option{idx}") is not None and sheet_images.get((excel_row_num - 1, mapping.get(f"option{idx}")), []))
                ]
                if len(valid_options) < 2:
                    skipped += 1
                    logging.warning(
                        "Row %d (Sheet '%s') skipped: Not enough valid options after filtering empty ones.",
                        excel_row_num, sheet_name
                    )
                    continue

                correct_raw = ""
                if "correct" in mapping and mapping["correct"] < len(row):
                    correct_raw = row[mapping["correct"]]
                correct_index = _resolve_correct_index(correct_raw,
                                                       option_values)  # Use original option_values for index resolution

                # (fix) extract the feedback/explanation text for this row, if the column
                # was detected; previously this value was never computed and referencing
                # `feedback` below crashed on the first valid question.
                feedback = ""
                if "feedback" in mapping and mapping["feedback"] < len(row):
                    feedback = _first_non_empty([row[mapping["feedback"]]])

                # Ensure the correct_index points to an existing option in the *original* list of 4
                # and then map it to the filtered valid_options
                correct_col = mapping.get(f"option{correct_index}")
                correct_has_image = bool(correct_col is not None and sheet_images.get((excel_row_num - 1, correct_col), []))
                if correct_index > len(option_values) or (not option_values[correct_index - 1] and not correct_has_image):
                    # If the designated correct option is empty or invalid, find the first non-empty option
                    # and make it the correct one.
                    found_valid_correct = False
                    for idx, opt in enumerate(option_values, start=1):
                        opt_col = mapping.get(f"option{idx}")
                        opt_has_image = bool(opt_col is not None and sheet_images.get((excel_row_num - 1, opt_col), []))
                        if opt or opt_has_image:
                            correct_index = idx
                            found_valid_correct = True
                            break
                    if not found_valid_correct:
                        skipped += 1
                        logging.warning(
                            "Row %d (Sheet '%s') skipped: No valid correct option found among non-empty options.",
                            excel_row_num, sheet_name
                        )
                        continue

                # Images anchored to the question / option cells. Excel anchors are
                # zero-based while the worksheet rows/columns here are zero-based too.
                question_col = mapping.get("question")
                question_images = list(sheet_images.get((excel_row_num - 1, question_col), [])) if question_col is not None else []

                choices = []
                for i, opt_text in enumerate(option_values, start=1):
                    if not opt_text:
                        # An image-only answer cell is still a valid answer.
                        opt_col = mapping.get(f"option{i}")
                        opt_images = list(sheet_images.get((excel_row_num - 1, opt_col), [])) if opt_col is not None else []
                        if opt_images:
                            choices.append({
                                "text": "",
                                "is_correct": i == correct_index,
                                "images": opt_images,
                            })
                        continue
                    opt_col = mapping.get(f"option{i}")
                    opt_images = list(sheet_images.get((excel_row_num - 1, opt_col), [])) if opt_col is not None else []
                    choices.append({
                        "text": opt_text,
                        "is_correct": i == correct_index,
                        "images": opt_images,
                    })

                # Assign fraction after collecting all choices based on the new correct_index
                for ch in choices:
                    ch["fraction"] = 100 if ch["is_correct"] else 0

                has_images = bool(question_images) or any(ch.get("images") for ch in choices)

                questions_in_sheet.append({
                    "name": f"{len(all_questions) + len(questions_in_sheet) + 1}.",  # Global numbering
                    "title": f"{len(all_questions) + len(questions_in_sheet) + 1}.",
                    "question_text": clean_question_text(q_text),
                    "feedback": feedback,
                    "choices": choices,
                    "images": question_images,
                    "has_images": has_images,
                    "shuffle_answers": not should_disable_shuffle(valid_options),
                    "row_num": excel_row_num,
                    "source_sheet": sheet_name,
                })

            if questions_in_sheet:
                logging.info("✅ در شیت '%s'، %d سوال معتبر پیدا شد (skipped=%d)", sheet_name, len(questions_in_sheet),
                             skipped)
                all_questions.extend(questions_in_sheet)
                # As per request, "در تمام شیتها دنبال اون شیت مورد نظری که هستیم باشی"
                # This implies finding THE sheet with questions, not combining all.
                # So, once questions are found in a sheet, we stop.
                break
            else:
                logging.warning("در شیت '%s' هیچ سوال معتبری پیدا نشد. به شیت بعدی می‌رویم.", sheet_name)

        except ExcelReadError as exc:
            logging.warning("خطا در خواندن شیت '%s': %s. به شیت بعدی می‌رویم.", sheet_name, exc)
        except Exception as exc:
            logging.exception("خطای غیرمنتظره در پردازش شیت '%s': %s. به شیت بعدی می‌رویم.", sheet_name, exc)

    if not all_questions:
        raise ExcelReadError("No valid questions found in any sheet that meets the criteria.")

    return all_questions


# =========================================================
# Moodle XML
# =========================================================
def _sub_text(parent: ET.Element, tag: str, text: str = "", **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib=attrs)
    t = ET.SubElement(el, "text")
    t.text = text or ""
    return el


def build_moodle_xml(questions: List[Dict[str, Any]]) -> str:
    quiz = ET.Element("quiz")

    total_questions_count = len(questions)
    if total_questions_count == 0:
        return ""  # No questions to build XML for

    # Determine grade per question based on total questions count
    if total_questions_count >= 20:
        grade_per_question = 1.0
    else:
        # Sum of grades should be 20.0
        grade_per_question = 20.0 / total_questions_count

    # Format to 6 decimal places for Moodle consistency, but ensure it's a string
    grade_str = f"{grade_per_question:.6f}".rstrip('0').rstrip('.')

    for i, q in enumerate(questions, start=1):
        q_el = ET.SubElement(quiz, "question", {"type": "multichoice"})
        _sub_text(q_el, "name", q.get("name") or f"{i}.")

        qt = ET.SubElement(q_el, "questiontext", {"format": "html"})
        q_images = q.get("images", []) or []
        q_text_html = q.get("question_text", "") or ""
        q_file_refs = []
        for idx, image in enumerate(q_images, start=1):
            filename = f"question_{i}_{idx}_{Path(image.filename).name}"
            q_text_html += f'<br><img src="@@PLUGINFILE@@/{filename}" alt="سؤال" />'
            q_file_refs.append((filename, image))
        ET.SubElement(qt, "text").text = q_text_html
        for filename, image in q_file_refs:
            ET.SubElement(qt, "file", {
                "name": filename, "path": "/", "encoding": "base64"
            }).text = base64.b64encode(image.data).decode("ascii")

        gf = ET.SubElement(q_el, "generalfeedback", {"format": "html"})
        ET.SubElement(gf, "text").text = q.get("feedback", "") or ""

        ET.SubElement(q_el, "defaultgrade").text = grade_str  # Apply calculated grade
        ET.SubElement(q_el, "penalty").text = "0.0"  # Penalty set to 0 as requested
        ET.SubElement(q_el, "hidden").text = "0"
        ET.SubElement(q_el, "single").text = "true"  # "true" for single answer, "false" for multiple
        # Normally Moodle should shuffle answers. For questions containing
        # meta-options (e.g. «همه موارد»، «هیچکدام»، «الف و ب») preserve the
        # original order because shuffling changes the meaning of the option
        # labels.
        shuffle_answers = q.get("shuffle_answers", True)
        ET.SubElement(q_el, "shuffleanswers").text = "true" if shuffle_answers else "false"
        ET.SubElement(q_el, "answernumbering").text = "abc"  # numbering style
        ET.SubElement(q_el, "showstandardinstruction").text = "0"

        for ch in q.get("choices", []):
            # fraction for choices is 100 for correct, 0 for incorrect
            ans = ET.SubElement(q_el, "answer", {"fraction": str(ch.get("fraction", 0)), "format": "html"})
            answer_html = ch.get("text", "") or ""
            answer_file_refs = []
            for idx, image in enumerate(ch.get("images", []) or [], start=1):
                filename = f"question_{i}_answer_{len(q_file_refs)+1}_{idx}_{Path(image.filename).name}"
                answer_html += f'<br><img src="@@PLUGINFILE@@/{filename}" alt="گزینه" />'
                answer_file_refs.append((filename, image))
            ET.SubElement(ans, "text").text = answer_html
            for filename, image in answer_file_refs:
                ET.SubElement(ans, "file", {
                    "name": filename, "path": "/", "encoding": "base64"
                }).text = base64.b64encode(image.data).decode("ascii")
            fb = ET.SubElement(ans, "feedback", {"format": "html"})
            ET.SubElement(fb, "text").text = ""

    try:
        ET.indent(quiz, space="  ")  # type: ignore[attr-defined]
    except Exception:
        pass  # Older Python versions might not have ET.indent

    return ET.tostring(quiz, encoding="utf-8", xml_declaration=True).decode("utf-8")


# =========================================================
# Convert pipeline
# =========================================================
def convert(input_xlsx: Path, output_xml: Path) -> Tuple[Path, int, int]:
    if not input_xlsx.exists():
        raise FileNotFoundError(f"Input file not found: {input_xlsx}")

    if input_xlsx.suffix.lower() not in SUPPORTED_EXCEL_EXTENSIONS:
        raise ExcelReadError(
            "فرمت فایل اکسل پشتیبانی نمی‌شود. فرمت‌های پشتیبانی‌شده: "
            + ", ".join(sorted(SUPPORTED_EXCEL_EXTENSIONS))
        )

    logging.info("📂 فایل ورودی : %s", input_xlsx)
    logging.info("=" * 70)

    questions_found: List[Dict[str, Any]] = []
    try:
        with zipfile.ZipFile(input_xlsx, "r") as zf:
            all_sheets = _parse_workbook_sheets(zf)
            questions_found = read_questions_from_workbook(zf, all_sheets)
    except zipfile.BadZipFile as exc:
        raise ExcelReadError(f"Invalid xlsx file (zip error): {exc}") from exc
    except KeyError as exc:
        raise ExcelReadError(f"Missing workbook component inside xlsx: {exc}") from exc
    except ET.ParseError as exc:
        raise ExcelReadError(f"XML parse error inside xlsx: {exc}") from exc
    except Exception as exc:
        logging.exception("❌ Unexpected error during XLSX reading: %s", exc)
        raise ExcelReadError(f"Cannot read workbook: {exc}") from exc

    if not questions_found:
        raise ExcelReadError("No valid questions found in any sheet. Conversion aborted.")

    logging.info("✅ %d سوال نهایی برای تبدیل انتخاب شد.", len(questions_found))
    logging.info("🔨 در حال ساخت XML مودل...")
    xml_content = build_moodle_xml(questions_found)

    output_xml.parent.mkdir(parents=True, exist_ok=True)
    output_xml.write_text(xml_content, encoding="utf-8")

    logging.info("✅ تبدیل با موفقیت انجام شد")
    image_question_count = sum(1 for q in questions_found if q.get("has_images"))
    logging.info("✅ تعداد سوالات: %d", len(questions_found))
    logging.info("🖼️ سوالات دارای تصویر: %d", image_question_count)
    logging.info("✅ فایل خروجی: %s", output_xml)
    return output_xml, len(questions_found), image_question_count


# =========================================================
# CLI
# =========================================================
def _get_user_input(prompt: str, default: Optional[str] = None) -> str:
    if default:
        value = input(f"{prompt} [{default}]: ").strip()
        return value if value else default
    return input(f"{prompt}: ").strip()


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a Persian multiple-choice Excel question bank to Moodle XML.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  %(prog)s
  %(prog)s -i
  %(prog)s input.xlsx
  %(prog)s input.xlsx (output XML will be named input.xml)
        """,
    )
    parser.add_argument("input_xlsx", type=Path, nargs="?", default=None,
                        help="Path to the input Excel (XLSX) file.")
    # output_xml argument is kept for potential future use or to avoid breaking existing calls,
    # but its value will be overridden to match the input XLSX name.
    parser.add_argument("output_xml", type=Path, nargs="?", default=None,
                        help=argparse.SUPPRESS)  # Hide from help for now
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="Run in interactive mode to prompt for input file.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging for more detailed output.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.verbose)

    input_xlsx: Path
    output_xml: Path

    if args.interactive:
        try:
            print("\n" + "=" * 60)
            print("🔧 راه‌اندازی تعاملی - تبدیل Excel به Moodle XML")
            print("=" * 60)

            # Use a more generic default for interactive mode if DEFAULT_INPUT_XLSX doesn't exist
            input_path_default = str(DEFAULT_INPUT_XLSX) if DEFAULT_INPUT_XLSX.exists() else ""
            input_path_str = _get_user_input("📂 مسیر فایل Excel ورودی", input_path_default)
            input_xlsx = Path(input_path_str)
            if not input_xlsx.exists():
                raise FileNotFoundError(f"فایل ورودی پیدا نشد: {input_xlsx}")

            # Output XML name always derived from input XLSX name
            output_xml = input_xlsx.with_suffix(".xml")

            print("=" * 60)
            print(f"✅ فایل ورودی: {input_xlsx}")
            print(f"✅ فایل خروجی: {output_xml} (نام فایل خروجی بر اساس فایل ورودی تعیین شد)")
            print(f"✅ شیت: تمامی شیت‌ها برای سوالات معتبر بررسی خواهند شد.")
            print("=" * 60 + "\n")

        except KeyboardInterrupt:
            print("\n❌ لغو شد توسط کاربر.")
            return 1
        except Exception as exc:
            logging.error("خطا در راه‌اندازی تعاملی: %s", exc)
            return 1
    else:  # Non-interactive mode
        if args.input_xlsx is None:
            input_xlsx = DEFAULT_INPUT_XLSX
            if not input_xlsx.exists():
                logging.error(
                    "❌ فایل ورودی پیش‌فرض (%s) پیدا نشد. لطفا مسیر فایل را مشخص کنید یا از حالت تعاملی (-i) استفاده کنید.",
                    input_xlsx)
                return 1
            logging.info("📌 استفاده از فایل ورودی پیش‌فرض: %s", input_xlsx)
        else:
            input_xlsx = args.input_xlsx

        # Output XML name always derived from input XLSX name
        output_xml = input_xlsx.with_suffix(".xml")

    if not input_xlsx.exists():
        logging.error("❌ فایل ورودی پیدا نشد: %s", input_xlsx)
        return 1

    try:
        convert(input_xlsx=input_xlsx, output_xml=output_xml)
        return 0
    except FileNotFoundError as exc:
        logging.error("❌ %s", exc)
        return 1
    except ExcelReadError as exc:
        logging.error("❌ Excel read error: %s", exc)
        return 1
    except Exception as exc:
        logging.exception("❌ Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
