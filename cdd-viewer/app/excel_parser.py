"""Parse the CDD worksheet into JSON-ready data.

Sheet conventions this parser relies on:
- The general header is the row containing "Date of birth" / "CI" / "BR" plus the row above it.
  Anything above those two rows is skipped and never copied into the output.
- A company starts at a file number in column A, or at the first row after a yellow separator.
- Column A labels (RO, Shares, DIR1, SH1, Com Sec, ...) open a block; unlabeled rows belong to
  the block above them.
- Data is outdated when its row is filled blue or carries an end / resign date.
"""

from __future__ import annotations

import colorsys
import datetime as dt
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles.colors import COLOR_INDEX
from openpyxl.utils import get_column_letter

from .excel_fields import fill_owners, inline_parts, preview_fill

SCHEMA_VERSION = 1
DEFAULT_SHEET = "CDD"
DEFAULT_OUTDATED_FILLS = frozenset({"00B0F0"})
SEPARATOR_FILLS = frozenset({"FFFF00"})
MIN_COLUMNS = 19

A, B, C, D, E, F, G, H = range(8)
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

DEFAULT_COLUMNS = {
    "dob": 2, "ci": 3, "br": 4, "incorporation": 5,
    "appoint": 8, "resign": 9,
    "transfer_date": 10, "transfer_from": 11, "transfer_to": 12,
    "transfer_shares": 13, "transfer_consideration": 14,
    "dt_bo": 15, "dt_trustee": 16, "dt_date": 17, "dt_shares": 18,
}
HEADER_ALIASES = {
    "date of birth": "dob", "ci": "ci", "br": "br", "incorporation": "incorporation",
    "appoint date": "appoint", "resign date": "resign",
    "transfer date": "transfer_date", "transfer from": "transfer_from", "transfer to": "transfer_to",
    "no of shares": "transfer_shares", "consideration": "transfer_consideration",
    "dt bo": "dt_bo", "dt trustee": "dt_trustee", "dt date": "dt_date", "dt shares": "dt_shares",
}
SEVERITIES = ("error", "warning", "info")
REQUIRED_SHARE_FIELDS = (
    ("no_of_shares", "No. of shares"),
    ("total_no_of_shares", "Total no. of shares"),
    ("consideration", "Consideration"),
    ("total_consideration", "Total consideration"),
    ("currency", "Currency"),
)


class ParseError(Exception):
    """The workbook does not look like a CDD sheet."""


# --------------------------------------------------------------------------- values

_WS_RE = re.compile(r"\s+")
_NUM_TEXT_RE = re.compile(r"^-?\d+(\.\d+)?$")
_EMPTY_MARKS = {"-", "—", "–", "?", "??", "n/a", "na", "nil"}
_ERROR_RE = re.compile(r"^#(REF!|VALUE!|DIV/0!|N/A|NAME\?|NUM!|NULL!)$")


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def clean_text(value: Any) -> str | None:
    if is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = _WS_RE.sub(" ", str(value)).strip()
    return text or None


def as_number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if _NUM_TEXT_RE.match(text):
            number = float(text)
            return int(number) if number.is_integer() else number
    return None


def number_or_text(value: Any) -> int | float | str | None:
    number = as_number(value)
    if number is not None:
        return number
    text = clean_text(value)
    return None if text in _EMPTY_MARKS else text


def id_text(value: Any) -> str | None:
    text = clean_text(value)
    return None if text is None or text.lower() in _EMPTY_MARKS else text


# --------------------------------------------------------------------------- dates

_MONTHS = {name: i for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DMY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]+\s*(\d{4}|\d{2})(?!\d)")
_D_MON_Y_RE = re.compile(r"(?<!\d)(\d{1,2})\s*([A-Za-z]{3,9})\.?,?\s*(\d{4}|\d{2})(?!\d)")
_DATE_MARK_RE = re.compile(r"\(\s*([AR])\s*\)|®", re.I)


@dataclass
class DateResult:
    value: str          # ISO date when ok, otherwise the original text
    ok: bool
    note: str | None = None
    problem: str | None = None  # invalid | unparsed | year_only | serial


def _full_year(text: str) -> int:
    year = int(text)
    if len(text) == 2:
        year += 2000 if year <= dt.date.today().year % 100 + 1 else 1900
    return year


def parse_date(value: Any) -> DateResult | None:
    """Read a date cell; text dates are day/month/year (Hong Kong order)."""
    if is_blank(value):
        return None
    if isinstance(value, dt.datetime):
        return DateResult(value.date().isoformat(), True)
    if isinstance(value, dt.date):
        return DateResult(value.isoformat(), True)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value).is_integer() and 1900 <= value <= 2100:
            return DateResult(str(int(value)), False, problem="year_only")
        if 20000 <= value <= 80000:
            day = dt.date(1899, 12, 30) + dt.timedelta(days=int(value))
            return DateResult(day.isoformat(), True, problem="serial")
        return DateResult(str(value), False, problem="unparsed")
    text = clean_text(value)
    if text.lower() in _EMPTY_MARKS:
        return None
    marks = [(m.group(1) or "").upper() or "®" for m in _DATE_MARK_RE.finditer(text)]
    body = _DATE_MARK_RE.sub(" ", text)
    parsed = None
    match = _DMY_RE.search(body)
    try:
        if match:
            parsed = dt.date(_full_year(match.group(3)), int(match.group(2)), int(match.group(1)))
        else:
            match = _D_MON_Y_RE.search(body)
            if match and match.group(2)[:3].lower() in _MONTHS:
                parsed = dt.date(_full_year(match.group(3)), _MONTHS[match.group(2)[:3].lower()],
                                 int(match.group(1)))
    except ValueError:
        return DateResult(text, False, problem="invalid")
    if parsed is None:
        if re.fullmatch(r"\d{4}", body.strip()):
            return DateResult(body.strip(), False, problem="year_only")
        return DateResult(text, False, problem="unparsed")
    rest = _WS_RE.sub(" ", body[:match.start()] + " " + body[match.end():]).strip(" ,;")
    note = " ".join(marks + ([rest] if rest else [])) or None
    return DateResult(parsed.isoformat(), True, note=note)


# --------------------------------------------------------------------------- labels

_ROLE_TOKEN_RE = re.compile(r"(COM\s?SEC|DIR|DR|SH|CS|SC|MB|BO|DT)\s*(\d+(?:[-.]\d+)*)?")


@dataclass(frozen=True)
class Label:
    kind: str           # COMPANY RO SHARES ROLE STATUS OLD_NAME BUSINESS_NAME BRANCH OTHER
    raw: str            # cell text exactly as typed
    text: str           # whitespace-normalised
    roles: tuple[str, ...] = ()
    company_no: str | None = None
    note: str | None = None

    @property
    def display(self) -> str:
        if self.kind != "ROLE":
            return self.text
        return ", ".join("Com Sec" if role == "CS" else role for role in self.roles)


def role_kind(role: str) -> str:
    match = re.match(r"[A-Z]+", role)
    return match.group(0) if match else role


def role_number(role: str) -> str:
    return role[len(role_kind(role)):]


def parse_label(value: Any) -> Label | None:
    if is_blank(value):
        return None
    raw = str(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = clean_text(value)
        return Label("COMPANY", raw, text, company_no=text)
    text = clean_text(value)
    match = re.match(r"^(\d+)\s*(.*)$", text)
    if match and (not match.group(2) or re.search(r"resign|resgin|dereg", match.group(2), re.I)):
        return Label("COMPANY", raw, text, company_no=match.group(1), note=match.group(2) or None)
    upper = text.upper().replace("!", "1")
    if upper == "RO":
        return Label("RO", raw, text)
    if upper == "SHARES":
        return Label("SHARES", raw, text)
    if upper in ("?", "??"):
        return Label("COMPANY", raw, text)
    if re.match(r"^(RESIGN|RESGIN|DEREG)", upper):
        return Label("STATUS", raw, text, note=text)
    if upper in ("OLD", "OLD NAME", "OLD NAMES"):
        return Label("OLD_NAME", raw, text)
    if upper == "BUSINESS NAME":
        return Label("BUSINESS_NAME", raw, text)
    branch = re.fullmatch(r"(BR\s*\d+)\s*(.*)", text, re.I | re.S)
    if branch:
        return Label("BRANCH", raw, branch.group(1).upper(), note=branch.group(2) or None)
    tokens = list(_ROLE_TOKEN_RE.finditer(upper))
    leftover = re.sub(r"[\s,&/+\-.]", "", _ROLE_TOKEN_RE.sub("", upper))
    if tokens and not leftover:
        number = next((t.group(2) for t in tokens if t.group(2)), None)
        roles: list[str] = []
        for token in tokens:
            name = token.group(1)
            name = "CS" if name.startswith("COM") else ("DIR" if name == "DR" else name)
            role = name + (token.group(2) or number or "")
            if role not in roles:
                roles.append(role)
        return Label("ROLE", raw, text, roles=tuple(roles))
    return Label("OTHER", raw, text)


# --------------------------------------------------------------------------- text kinds

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HKID_RE = re.compile(r"^[A-Z]{1,2}\d{6}\s*\(?[0-9A]\)?$", re.I)
_OTHER_ID_RE = re.compile(r"^(?:(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,12}|\d{17}[\dX]|\d{7}\(\d\))$", re.I)
_PHONE_RE = re.compile(r"^\+?[\d\s\-()]{8,22}$")
_FULL_DATE_RE = re.compile(r"^\d{1,2}\s*[/.\-]\s*\d{1,2}\s*[/.\-]+\s*(\d{4}|\d{2})$")
_ZH_ADDRESS_RE = re.compile(r"[室樓號街道路座村邨苑廈灣園徑巷舖]|香港|九龍|新界|中心|地下")
_EN_ADDRESS_RE = re.compile(
    r"\b(flat|room|rm|unit|floor|block|blk|tower|street|road|rd|building|bldg|estate|house|court|"
    r"centre|center|hong kong|kowloon|new territories|shop|workshop|lane|avenue|drive|path|terrace|"
    r"village|garden|gardens|plaza|mansion|industrial|commercial|wing|phase|g/f|\d+/f)\b", re.I)
# Placeholder values used in the dummy workbook, e.g. "dir1_address", "sh1_tel".
_PLACEHOLDER_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
_PLACEHOLDER_KINDS = (
    ({"address", "adress", "addr"}, "address"),
    ({"email", "mail"}, "email"),
    ({"tel", "phone", "mobile"}, "phone"),
    ({"birth", "dob"}, "dob"),
    ({"chinese"}, "name_zh"),
    ({"id", "passport"}, "id"),
    ({"name"}, "name"),
)
_CORPORATE_RE = re.compile(r"\b(limited|ltd|company|corporation|corp|inc|llc)\b|有限公司", re.I)


def id_document_type(text: str | None) -> str | None:
    """HKID when the number matches; passport only when the cell says so."""
    raw = str(text or "").strip()
    if not raw:
        return None
    compact = re.sub(r"\s+", "", raw)
    if _HKID_RE.match(compact) or _HKID_RE.match(raw):
        return "hkid"
    if re.search(r"passport", raw, re.I):
        return "passport"
    return None


def classify(value: Any) -> str | None:
    """Guess what a free-form person / address cell contains."""
    if is_blank(value):
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return "date"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value).is_integer() and 20_000_000 <= value <= 99_999_999:
            return "phone"
        return "number"
    text = clean_text(value)
    if _PLACEHOLDER_RE.match(text):
        tokens = set(text.split("_"))
        for keys, kind in _PLACEHOLDER_KINDS:
            if tokens & keys:
                return kind
    if "@" in text and _EMAIL_RE.match(text.replace(" ", "")):
        return "email"
    if _HKID_RE.match(text):
        return "id"
    if _FULL_DATE_RE.match(text):
        return "date"
    if _CJK_RE.search(text):
        return "address_zh" if _ZH_ADDRESS_RE.search(text) or re.search(r"\d", text) else "name_zh"
    digits = re.sub(r"\D", "", text)
    if _PHONE_RE.match(text) and 8 <= len(digits) <= 15:
        return "phone"
    if _OTHER_ID_RE.match(text.replace(" ", "")):
        return "id"
    if _EN_ADDRESS_RE.search(text) or (len(text) >= 15 and re.search(r"\d", text) and _LATIN_RE.search(text)):
        return "address"
    return "other"


# --------------------------------------------------------------------------- colours

_THEME_SLOTS = ["dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3",
                "accent4", "accent5", "accent6", "hlink", "folHlink"]
_THEME_INDEX = ["lt1", "dk1", "lt2", "dk2", "accent1", "accent2", "accent3",
                "accent4", "accent5", "accent6", "hlink", "folHlink"]


def _theme_palette(wb) -> dict[str, str]:
    raw = getattr(wb, "loaded_theme", None)
    if not raw:
        return {}
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    try:
        scheme = ET.fromstring(raw).find(".//a:clrScheme", ns)
    except ET.ParseError:
        return {}
    palette: dict[str, str] = {}
    if scheme is None:
        return palette
    for slot in _THEME_SLOTS:
        element = scheme.find(f"a:{slot}", ns)
        if element is None or not len(element):
            continue
        child = element[0]
        value = child.get("val") if child.tag.endswith("srgbClr") else child.get("lastClr")
        if value:
            palette[slot] = value.upper()
    return palette


def _apply_tint(hex_rgb: str, tint: float) -> str:
    r, g, b = (int(hex_rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    light = light * (1 + tint) if tint < 0 else light * (1 - tint) + tint
    r, g, b = colorsys.hls_to_rgb(hue, light, sat)
    return "%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


def _hls(hex_rgb: str | None) -> tuple[float, float, float] | None:
    if not hex_rgb or not re.fullmatch(r"[0-9A-Fa-f]{6}", hex_rgb):
        return None
    r, g, b = (int(hex_rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    return hue * 360, light, sat


def is_red(hex_rgb: str | None) -> bool:
    """Red, light red or dark red fill: marks an inactive company on its file-number cell."""
    hls = _hls(hex_rgb)
    return bool(hls) and (hls[0] <= 15 or hls[0] >= 345) and hls[2] >= 0.5 and 0.25 <= hls[1] <= 0.95


def is_orange(hex_rgb: str | None) -> bool:
    """Orange file-number fill (Excel's standard orange FFC000 and close shades): a normal active company."""
    hls = _hls(hex_rgb)
    return bool(hls) and 35 <= hls[0] <= 55 and hls[2] >= 0.7 and 0.4 <= hls[1] <= 0.75


def color_name(hex_rgb: str | None) -> str | None:
    hls = _hls(hex_rgb)
    if not hls:
        return None
    degrees, light, sat = hls
    if sat < 0.15:
        return "白色" if light > 0.95 else ("黑色" if light < 0.1 else "灰色")
    if degrees < 15 or degrees >= 345:
        base = "紅色"
    elif degrees < 35:
        base = "杏色" if light >= 0.7 else "橙紅色"
    elif degrees < 55:
        return "橙色" if is_orange(hex_rgb) else ("金色" if light < 0.4 else "淺橙色")
    elif degrees < 70:
        base = "黃色"
    elif degrees < 160:
        base = "綠色"
    elif degrees < 200:
        base = "青色"
    elif degrees < 250:
        base = "藍色"
    elif degrees < 290:
        base = "紫色"
    else:
        base = "粉紅色"
    if base == "杏色":
        return base
    if light >= 0.75:
        return "淺" + base
    return "深" + base if light <= 0.3 else base


class FillReader:
    def __init__(self, wb):
        self.palette = _theme_palette(wb)

    def __call__(self, cell) -> str | None:
        fill = getattr(cell, "fill", None)
        if fill is None or fill.patternType != "solid" or fill.fgColor is None:
            return None
        color = fill.fgColor
        if color.type == "rgb" and isinstance(color.rgb, str):
            value = color.rgb.upper()
            return None if value == "00000000" else value[-6:]
        if color.type == "theme" and color.theme is not None and color.theme < len(_THEME_INDEX):
            base = self.palette.get(_THEME_INDEX[color.theme])
            if base:
                return _apply_tint(base, color.tint) if color.tint else base
        if color.type == "indexed" and color.indexed is not None and color.indexed < len(COLOR_INDEX):
            return COLOR_INDEX[color.indexed][-6:]
        return None


# --------------------------------------------------------------------------- rows

@dataclass
class Row:
    n: int
    values: list[Any]
    fills: list[str | None]
    label: Label | None = None

    def v(self, idx: int) -> Any:
        return self.values[idx] if idx < len(self.values) else None

    def blank(self, start: int, stop: int) -> bool:
        return all(is_blank(self.v(i)) for i in range(start, stop))

    @property
    def empty(self) -> bool:
        return all(is_blank(value) for value in self.values)

    def summary(self) -> str:
        parts = [f"{LETTERS[i]}: {clean_text(v)}" for i, v in enumerate(self.values) if not is_blank(v)]
        return "；".join(parts)


def _issue(row: Any, severity: str, code: str, message: str, field: str | None = None) -> dict:
    """field names the form field the problem belongs to, e.g. "party.resign_date" or "ro.to"."""
    return {"row": row, "severity": severity, "code": code, "message": message, "field": field}


def _fmt_num(value: Any) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else str(value)


def _fmt_pct(value: float) -> str:
    return f"{value:.2%}" if 0 <= value <= 1.0001 else _fmt_num(value)


def follows_address_fields(lines) -> bool:
    """True when filled cells start at field 1 and do not skip an earlier field.

    End-filled leftovers such as only field 4, or fields 2–4 with field 1 empty,
    are not an address.
    """
    cells = [str(part or "").strip() for part in list(lines or [])]
    cells = (cells + ["", "", "", ""])[:4]
    seen_empty = False
    for cell in cells:
        if not cell:
            seen_empty = True
        elif seen_empty:
            return False
    return True


def bypasses_address_one(lines) -> bool:
    """True when field 1 is empty and fields 2, 3 and 4 are all filled.

    A finished import never looks like that. Those cells belong in the undefined area.
    """
    cells = [str(part or "").strip() for part in list(lines or [])]
    cells = (cells + ["", "", "", ""])[:4]
    return not cells[0] and all(cells[index] for index in (1, 2, 3))


def split_address(text: str | None) -> list[str]:
    """Split an address into four fields from the end: the last part is field 4."""
    if not text:
        return ["", "", "", ""]
    normalized = text.replace("\n", ",").replace("，", ",")
    parts = [part.strip() for part in normalized.split(",") if part.strip()]
    if len(parts) > 4:
        parts = [", ".join(parts[:-3])] + parts[-3:]
    return [""] * (4 - len(parts)) + parts


# Words and numbers that belong in another column, not in an address.
_ADDRESS_BAD = re.compile(
    r"\b(?:SHARES?|ID|DIR\d*|SH\d+|E-?MAILS?|TELS?|DOB|PASSPORT|COM\s*SEC)\b"
    r"|[A-Z]{1,2}\d{6}\s*\(?[0-9A]\)?",
    re.I,
)


def suspicious_address_words(line: str) -> list[str]:
    return sorted({match.group(0).upper() for match in _ADDRESS_BAD.finditer(line or "")})


# --------------------------------------------------------------------------- company

class _CompanyBuilder:
    def __init__(self, sheet: "_SheetParser", rows: list[Row], separated: bool, issues: list[dict]):
        self.sheet = sheet
        self.cols = sheet.cols
        self.rows = rows
        self.separated = separated
        self.issues: list[dict] = list(issues)
        self.parties: list[dict] = []
        self.block: dict | None = None
        self._block_seen = False
        self._after_subheader = False
        self._status_row: int | None = None
        head = rows[0]
        self.head = head
        self.c: dict[str, Any] = {
            "id": None,
            "company_no": head.label.company_no if head.label else None,
            "row": head.n,
            "label_color": head.fills[A],
            "color_name": None,
            "color_group": None,
            "red_label": False,
            "active": True,
            "status": "active",
            "status_note": None,
            "status_date": None,
            "general": {"name_en": None, "name_zh": None, "ci_no": None, "br_no": None,
                        "incorporation_date": None},
            "former_names": [],
            "business_names": [],
            "branches": [],
            "registered_offices": [],
            "share_capital": [],
            "current_share_capital_row": None,
            "share_capital_missing": [],
            "issued_shares": None,
            "currency": None,
            "holdings_total": None,
            "shareholding_summary": [],
            "directors": [],
            "shareholders": [],
            "company_secretaries": [],
            "other_parties": [],
            "notes": [],
            "issues": self.issues,
        }

    # ----- helpers

    def _address_lines(self, row: Row, text: str | None) -> list[str]:
        lines = split_address(text)
        for index, line in enumerate(lines):
            words = suspicious_address_words(line)
            if not words:
                continue
            self.warn(row, "ADDRESS_SUSPICIOUS",
                      f"資料有誤，請檢查：地址 {index + 1} 含有 {'、'.join(words)}，可能不是地址",
                      field=f"address.line.{index + 1}")
        return lines

    def warn(self, row: Row | int | None, code: str, message: str, severity: str = "warning",
             field: str | None = None) -> None:
        number = row.n if isinstance(row, Row) else row
        self.issues.append(_issue(number, severity, code, message, field))

    def _date(self, row: Row, col: int, what: str, field: str | None = None, end: bool = False) -> str | None:
        """Read a date cell; a resign / end date that is not a real date is an error to be checked."""
        raw = clean_text(row.v(col))
        where = f"{what}（{LETTERS[col]} 欄）「{raw}」"
        bad = "error" if end else "warning"
        result = parse_date(row.v(col))
        if result is None:
            if raw in ("?", "??"):
                self.warn(row, "DATE_UNREADABLE", f"資料有誤，請檢查：{where}不是日期", bad, field)
                return raw
            return None
        if result.problem == "invalid":
            self.warn(row, "DATE_INVALID", f"資料有誤，請檢查：{where}不是有效日期", bad, field)
        elif result.problem == "unparsed":
            self.warn(row, "DATE_UNREADABLE", f"資料有誤，請檢查：{where}不是日期", bad, field)
        elif result.problem == "year_only":
            self.warn(row, "DATE_YEAR_ONLY", f"資料有誤，請檢查：{where}只有年份，不是完整日期", bad, field)
        elif result.problem == "serial":
            self.warn(row, "DATE_AS_NUMBER", f"{where}以數字儲存，已換算為 {result.value}", "info", field)
        if result.note:
            self.warn(row, "DATE_WITH_NOTE", f"{where}夾雜註記，已讀作 {result.value}", "info", field)
        return result.value

    def _note(self, row: Row, code: str, message: str, severity: str = "warning") -> None:
        self.c["notes"].append({"row": row.n, "value": row.summary()})
        self.warn(row, code, message, severity)

    # ----- main flow

    def build(self) -> dict:
        self._head()
        rest = self.rows[1:]
        start = 0
        if rest:
            second = rest[0]
            text = clean_text(second.v(B))
            if (second.label is None or second.label.kind == "STATUS") and text \
                    and classify(second.v(B)) not in ("email", "phone", "id", "date"):
                self.c["general"]["name_zh"] = text
                if second.label:
                    self._status(second.label.note, second)
                start = 1
        for row in rest[start:]:
            self._row(row)
        self._yellow_and_mixed_rows()
        self._finish()
        return self.c

    def _head(self) -> None:
        row, label, general = self.head, self.head.label, self.c["general"]
        general["name_en"] = clean_text(row.v(B))
        general["ci_no"] = id_text(row.v(self.cols["ci"]))
        general["br_no"] = id_text(row.v(self.cols["br"]))
        general["incorporation_date"] = self._date(row, self.cols["incorporation"], "成立日期",
                                                   "general.incorporation_date")
        if label is None:
            self.warn(row, "NO_COMPANY_NO", "公司沒有編號（A 欄空白）", field="company_no")
        elif label.company_no is None:
            self.warn(row, "NO_COMPANY_NO", f"公司編號寫成「{label.text}」，未有正式編號", field="company_no")
        if label is not None and label.note:
            self._status(label.note, row)
            self.warn(row, "STATUS_IN_LABEL", "公司編號和狀態寫在同一格，建議分開", "info", "company_no")
        if not general["name_en"]:
            self.warn(row, "NO_COMPANY_NAME", "公司首行的 B 欄沒有公司名稱", field="general.name_en")
        if not self.separated:
            self.warn(row, "NO_SEPARATOR", "這間公司之前沒有黃色分隔行", "info")

    def _status(self, text: str | None, row: Row) -> None:
        note = clean_text(text)
        if not note:
            return
        if self._status_row is None:
            self._status_row = row.n
        lower = note.lower()
        if "dereg" in lower:
            self.c["status"] = "deregistered"
        elif re.search(r"resign|resgin", lower):
            self.c["status"] = "resigned"
        self.c["status_note"] = note if not self.c["status_note"] else f"{self.c['status_note']}；{note}"
        if not self.c["status_date"]:
            result = parse_date(note)
            if result and result.ok:
                self.c["status_date"] = result.value

    def _row(self, row: Row) -> None:
        for value in row.values:
            if isinstance(value, str) and _ERROR_RE.match(value.strip()):
                self.warn(row, "FORMULA_ERROR", f"儲存格有公式錯誤 {value.strip()}", "error")
                break
        if row.blank(A, H + 1):
            self._note(row, "ONLY_RIGHT_COLUMNS", "這行 A–H 欄空白，只有 I 欄之後有資料", "info")
            return
        after_subheader, self._after_subheader = self._after_subheader, False
        if self._is_subheader(row):
            self.block = None
            self._block_seen = True
            self._after_subheader = True
            if not is_blank(row.v(A)):
                self.warn(row, "SUBHEADER_LABEL", f"sub header 行的 A 欄有「{clean_text(row.v(A))}」，已略過", "info")
            return
        label = row.label
        if label is None:
            if after_subheader and self.block is None and as_number(row.v(B)) is not None:
                self.warn(row, "SHARES_NO_LABEL", "sub header 下一行的 A 欄漏寫「Shares」，已當作股本讀入", "info",
                          "capital.label")
                self._capital_entry(row)
                self.block = {"type": "shares"}
                return
            self._line(row)
            return
        if label.kind != "STATUS":
            self._block_seen = True
        holding = (label.kind == "ROLE" and {role_kind(r) for r in label.roles} == {"SH"}
                   and self._is_holding_row(row, labeled=True))
        if label.kind == "ROLE" and label.display != label.raw:
            self.warn(row, "LABEL_VARIANT", f"A 欄「{label.raw}」已視為 {label.display}", "info",
                      "holding.label" if holding else "party.label")
        kind = label.kind
        if kind == "STATUS":
            self._status(label.note, row)
            text = clean_text(row.v(B))
            if text and not self.c["general"]["name_zh"]:
                self.c["general"]["name_zh"] = text
            elif text:
                self.c["notes"].append({"row": row.n, "value": text})
        elif kind == "RO":
            entry = self._ro_entry(row)
            self.c["registered_offices"].append(entry)
            self.block = {"type": "ro", "entry": entry}
        elif kind == "SHARES":
            self._capital_entry(row)
            self.block = {"type": "shares"}
        elif kind in ("OLD_NAME", "BUSINESS_NAME"):
            self.block = {"type": "names", "key": "former_names" if kind == "OLD_NAME" else "business_names"}
            self._names_line(self.block, row)
        elif kind == "BRANCH":
            self._branch(row)
        elif kind == "ROLE":
            if holding:
                self._sh_holding(label, row)
            else:
                self._new_party(label, row)
        elif kind == "OTHER":
            if row.blank(B, len(row.values)):
                self._note(row, "LABEL_NOTE", f"A 欄有備註「{label.text}」", "info")
            else:
                self.warn(row, "UNKNOWN_LABEL", f"A 欄標籤「{label.text}」未能識別，已原文保留", field="party.label")
                self._new_party(label, row)
        else:
            self._note(row, "UNEXPECTED_ROW", f"公司區塊內出現「{label.text}」，已略過")

    def _line(self, row: Row) -> None:
        block = self.block
        if block is None and not self._block_seen:
            text = clean_text(row.v(B))
            if text and (_CORPORATE_RE.search(text) or classify(row.v(B)) == "name_zh"):
                self.warn(row, "OLD_NAME_NO_LABEL", "公司名稱之後的名稱沒有 Old name 標籤，已當作舊名稱", "info",
                          "general.former_names")
                self._names_line({"type": "names", "key": "former_names"}, row, allow_current_zh=False)
                return
        if block is None:
            self._note(row, "UNASSIGNED_ROW", "這行沒有 A 欄標籤，也不屬於任何區塊")
        elif block["type"] == "party":
            self._party_line(block["party"], row)
        elif block["type"] == "ro":
            self._ro_line(block, row)
        elif block["type"] == "shares":
            if as_number(row.v(B)) is not None:
                self._capital_entry(row)
            else:
                self._note(row, "SHARES_NOTE", "Shares 區塊下有非股數的資料", "info")
        elif block["type"] == "names":
            self._names_line(block, row)
        elif block["type"] == "branch":
            text = clean_text(row.v(B))
            entry = block["entry"]
            if text:
                entry["address"] = f"{entry['address']}, {text}" if entry["address"] else text
                entry["lines"] = self._address_lines(row, entry["address"])

    @staticmethod
    def _is_subheader(row: Row) -> bool:
        text = clean_text(row.v(B))
        return bool(text) and re.sub(r"[^a-z]", "", text.lower()) == "noofshares"

    @staticmethod
    def _is_holding_row(row: Row, labeled: bool) -> bool:
        if as_number(row.v(B)) is None:
            return labeled and is_blank(row.v(B)) and (
                as_number(row.v(C)) is not None or not row.blank(G, H + 1))
        return labeled or as_number(row.v(C)) is not None or not row.blank(G, H + 1)

    # ----- registered office / share capital / names / branches

    def _ro_entry(self, row: Row) -> dict:
        reasons = self.sheet.reasons(row)
        address = clean_text(row.v(B))
        return {
            "address": address,
            "lines": self._address_lines(row, address),
            "from": self._date(row, self.cols["appoint"], "RO 開始日期", "ro.from"),
            "to": self._date(row, self.cols["resign"], "RO 結束日期", "ro.to", end=True),
            "row": row.n,
            "outdated": bool(reasons),
            "outdated_reasons": reasons,
        }

    def _ro_line(self, block: dict, row: Row) -> None:
        text = clean_text(row.v(B))
        if text is None or classify(row.v(B)) in ("id", "phone", "email"):
            self._note(row, "RO_UNEXPECTED_LINE", "RO 區塊下出現非地址資料，可能漏寫了 A 欄標籤")
            self.issues[-1]["field"] = "registered_offices"
            return
        entry = block["entry"]
        has_dates = not row.blank(self.cols["appoint"], self.cols["resign"] + 1)
        if has_dates or self.sheet.is_blue(row) != ("blue" in entry["outdated_reasons"]):
            entry = self._ro_entry(row)
            self.c["registered_offices"].append(entry)
            block["entry"] = entry
        else:
            entry["address"] = f"{entry['address']}, {text}" if entry["address"] else text
            entry["lines"] = self._address_lines(row, entry["address"])

    def _capital_entry(self, row: Row) -> None:
        reasons = self.sheet.reasons(row)
        self.c["share_capital"].append({
            "row": row.n,
            "no_of_shares": number_or_text(row.v(B)),
            "total_no_of_shares": number_or_text(row.v(C)),
            "consideration": number_or_text(row.v(D)),
            "total_consideration": number_or_text(row.v(E)),
            "currency": clean_text(row.v(F)),
            "class": clean_text(row.v(G)),
            "type": clean_text(row.v(H)),
            "date": self._date(row, self.cols["appoint"], "股本日期", "capital.date"),
            "outdated": bool(reasons),
            "outdated_reasons": reasons,
        })

    def _names_line(self, block: dict, row: Row, allow_current_zh: bool = True) -> None:
        reasons = self.sheet.reasons(row)
        for col in (B, C, D):
            text = clean_text(row.v(col))
            if not text:
                continue
            if allow_current_zh and block["key"] == "former_names" and not reasons and _CJK_RE.search(text) \
                    and not self.c["general"]["name_zh"]:
                self.c["general"]["name_zh"] = text
                self.warn(row, "NAME_AFTER_OLD_NAME", "中文名稱放在舊名稱之後", "info", "general.name_zh")
                continue
            item_reasons = reasons + (["former"] if block["key"] == "former_names" else [])
            self.c[block["key"]].append({
                "value": text, "row": row.n,
                "from": self._date(row, self.cols["appoint"], "名稱開始日期", "item"),
                "to": self._date(row, self.cols["resign"], "名稱結束日期", "item", end=True),
                "outdated": bool(item_reasons), "outdated_reasons": item_reasons,
            })

    def _branch(self, row: Row) -> None:
        """Branch rows (BR0001 ...) are current unless blue or given a real end date."""
        reasons = self.sheet.reasons(row)
        note = row.label.note
        entry = {
            "label": row.label.text,
            "address": clean_text(row.v(B)),
            "lines": self._address_lines(row, clean_text(row.v(B))),
            "br_no": id_text(row.v(self.cols["br"])),
            "from": self._date(row, self.cols["appoint"], "分行開始日期", "branch.from"),
            "to": self._date(row, self.cols["resign"], "分行結束日期", "branch.to"),
            "note": note,
            "row": row.n,
            "outdated": bool(reasons),
            "outdated_reasons": reasons,
        }
        if note:
            result = parse_date(note)
            ended = re.search(r"resign|resgin|dereg|cease|close", note, re.I) and result and result.ok
            if ended:
                entry["to"] = entry["to"] or result.value
                entry["outdated"], entry["outdated_reasons"] = True, sorted(set(reasons) | {"end_date"})
            else:
                self.warn(row, "BRANCH_NOTE_UNCLEAR",
                          f"資料有誤，請檢查：A 欄「{row.label.raw.strip()}」附有不清晰的內容，已照常顯示", "warning",
                          "branch.label")
        self.c["branches"].append(entry)
        self.block = {"type": "branch", "entry": entry}

    # ----- parties

    def _new_party(self, label: Label, row: Row, from_holding: bool = False) -> dict:
        reasons = self.sheet.reasons(row)
        party = {
            "label": label.display,
            "label_raw": label.text,
            "roles": list(label.roles) or [label.text],
            "row": row.n,
            "outdated": bool(reasons),
            "outdated_reasons": reasons,
            "name_en": None,
            "name_zh": None,
            "date_of_birth": None,
            "ci_no": None,
            "br_no": None,
            "id_numbers": [],
            "addresses": [],
            "emails": [],
            "phones": [],
            "other": [],
            "appoint_date": None,
            "appoint_date_row": None,
            "resign_date": None,
            "resign_date_row": None,
            "shareholdings": [],
            "current_shares": None,
            "current_holding_row": None,
            "current_consideration": None,
            "current_consideration_source": None,
            "percentage": None,
        }
        self.parties.append(party)
        self.block = {"type": "party", "party": party}
        if not from_holding:
            self._party_head(party, row)
        return party

    def _party_head(self, party: dict, row: Row) -> None:
        name = clean_text(row.v(B))
        if name:
            if _CJK_RE.search(name) and not _LATIN_RE.search(name):
                party["name_zh"] = name
            else:
                party["name_en"] = name
        for col in (C, D, E):
            self._party_cell(party, row, col, head=True)
        for col in (F, G, H):
            self._party_other(party, row, col, head=True)
        party["appoint_date"] = self._date(row, self.cols["appoint"], "委任日期", "party.appoint_date")
        party["resign_date"] = self._date(row, self.cols["resign"], "辭任日期", "party.resign_date", end=True)
        party["appoint_date_row"] = row.n if party["appoint_date"] else None
        party["resign_date_row"] = row.n if party["resign_date"] else None
        self._apply_general_tail(party, row)

    def _party_line(self, party: dict, row: Row) -> None:
        if self._is_holding_row(row, labeled=False):
            self._add_holding(party, row, "")
            return
        address_line = classify(row.v(B)) in ("address", "address_zh")
        self._date(row, self.cols["appoint"], "開始日期", "address.from" if address_line else "item")
        self._date(row, self.cols["resign"], "結束日期", "address.to" if address_line else "item", end=True)
        # A date written right after an address belongs to that address, not to the date of birth.
        address_date = None
        for col in (B, C, D, E):
            if address_line and col in (C, D) and classify(row.v(col)) == "date":
                result = parse_date(row.v(col))
                if result and result.ok:
                    address_date = address_date or result.value
                    continue
            self._party_cell(party, row, col, head=False)
        if address_date:
            for item in party["addresses"]:
                if item["row"] == row.n and not item.get("from"):
                    item["from"] = address_date
        for col in (F, G, H):
            self._party_other(party, row, col, head=False)
        self._apply_general_tail(party, row)

    def _party_cell(self, party: dict, row: Row, col: int, head: bool) -> None:
        value = row.v(col)
        text = clean_text(value)
        if text is None or text.lower() in _EMPTY_MARKS:
            return
        if col == D and re.fullmatch(r"\d{6,8}", text) and not party["ci_no"]:
            party["ci_no"] = text
            return
        if col == E and re.fullmatch(r"\d{8}(?:[-\s]?\d+)*", text) and not party["br_no"]:
            party["br_no"] = text
            return
        kind = classify(value)
        address_lines = None
        if kind in ("address", "address_zh"):
            address_lines = self._address_lines(row, text)
            if not follows_address_fields(address_lines):
                kind = "other"
                address_lines = None
                for issue in self.issues:
                    if issue.get("row") == row.n and str(issue.get("field") or "").startswith("address."):
                        issue["field"] = "undefined"
        if kind in ("date", "dob") and not party["date_of_birth"]:
            result = parse_date(value)
            party["date_of_birth"] = result.value if result and result.ok else text
            if result and result.problem == "invalid":
                self.warn(row, "DATE_INVALID", f"資料有誤，請檢查：出生日期（{LETTERS[col]} 欄）「{text}」不是有效日期",
                          field="party.date_of_birth")
            return
        if kind == "name_zh" and not party["name_zh"]:
            party["name_zh"] = text
            return
        if kind == "name" and not party["name_en"]:
            party["name_en"] = text
            return
        self._party_field(party, row, kind, text, col, head, address_lines)

    def _party_other(self, party: dict, row: Row, col: int, head: bool) -> None:
        text = clean_text(row.v(col))
        if text and text.lower() not in _EMPTY_MARKS:
            self._party_field(party, row, "other", text, col, head, None)

    def _party_field(self, party: dict, row: Row, kind: str, text: str, col: int, head: bool,
                     address_lines: list[str] | None = None) -> None:
        key = {"address": "addresses", "address_zh": "addresses", "id": "id_numbers",
               "email": "emails", "phone": "phones"}.get(kind, "other")
        reasons = list(party["outdated_reasons"]) if head else self.sheet.reasons(row)
        if party["outdated"] and not head:
            reasons.append("block")
        item = {"value": text, "row": row.n, "column": LETTERS[col],
                "outdated": bool(reasons), "outdated_reasons": reasons}
        if key == "id_numbers":
            item["id_type"] = id_document_type(text)
        if key == "addresses":
            item["lines"] = address_lines if address_lines is not None else self._address_lines(row, text)
        if not head:
            start = parse_date(row.v(self.cols["appoint"]))
            end = parse_date(row.v(self.cols["resign"]))
            if start:
                item["from"] = start.value
            if end:
                item["to"] = end.value
        party[key].append(item)

    def _sh_holding(self, label: Label, row: Row) -> None:
        role = label.roles[0]
        block = self.block
        party = block["party"] if block and block["type"] == "party" else None
        if party is None:
            party = next((p for p in reversed(self.parties)
                          if role in p["roles"] or f"DIR{role_number(role)}" in p["roles"]), None)
            if party is None:
                party = self._new_party(label, row, from_holding=True)
                self.warn(row, "SH_WITHOUT_PERSON", f"{label.display} 持股行之前沒有對應的人物資料",
                          field="holding.label")
            self.block = {"type": "party", "party": party}
        else:
            numbers = {role_number(r) for r in party["roles"] if role_kind(r) in ("DIR", "SH")}
            if role not in party["roles"] and role_number(role) not in numbers:
                self.warn(row, "SH_NUMBER_MISMATCH",
                          f"{label.display} 放在 {party['label']} 區塊之下但編號不同，已按位置歸屬，請確認",
                          field="holding.label")
        if role not in party["roles"]:
            party["roles"].append(role)
        self._add_holding(party, row, label.display)

    def _row_transfer(self, row: Row) -> dict | None:
        cols = self.cols
        keys = ("transfer_date", "transfer_from", "transfer_to", "transfer_shares", "transfer_consideration")
        if not any(id_text(row.v(cols[key])) for key in keys):
            return None
        return {
            "date": self._date(row, cols["transfer_date"], "轉讓日期", "holding.transfer"),
            "from": id_text(row.v(cols["transfer_from"])),
            "to": id_text(row.v(cols["transfer_to"])),
            "no_of_shares": number_or_text(row.v(cols["transfer_shares"])),
            "consideration": number_or_text(row.v(cols["transfer_consideration"])),
        }

    def _row_trust(self, row: Row) -> dict | None:
        cols = self.cols
        keys = ("dt_bo", "dt_trustee", "dt_date", "dt_shares")
        if not any(id_text(row.v(cols[key])) for key in keys):
            return None
        return {
            "bo": id_text(row.v(cols["dt_bo"])),
            "trustee": id_text(row.v(cols["dt_trustee"])),
            "date": self._date(row, cols["dt_date"], "DT 日期", "holding.dt"),
            "shares": number_or_text(row.v(cols["dt_shares"])),
        }

    def _apply_general_tail(self, party: dict, row: Row) -> None:
        """Columns I onward keep the general header, including on a name or address row."""
        transfer, trust = self._row_transfer(row), self._row_trust(row)
        if not transfer and not trust:
            return
        reasons = self.sheet.reasons(row)
        if party["outdated"]:
            reasons = reasons + ["block"]
        party["shareholdings"].append({
            "label": "",
            "row": row.n,
            "no_of_shares": None,
            "total_no_of_shares": None,
            "percentage_excel": None,
            "consideration": None,
            "total_consideration": None,
            "class": None,
            "type": None,
            "date": None,
            "end_date": None,
            "transfer": transfer,
            "declaration_of_trust": trust,
            "pre_transfer": False,
            "outdated": bool(reasons),
            "outdated_reasons": reasons,
        })

    def _add_holding(self, party: dict, row: Row, label: str) -> None:
        cols = self.cols
        reasons = self.sheet.reasons(row)
        if party["outdated"]:
            reasons = reasons + ["block"]
        transfer = self._row_transfer(row)
        trust = self._row_trust(row)
        party["shareholdings"].append({
            "label": label,
            "row": row.n,
            "no_of_shares": number_or_text(row.v(B)),
            "total_no_of_shares": number_or_text(row.v(C)),
            "percentage_excel": as_number(row.v(D)),
            "consideration": number_or_text(row.v(E)),
            "total_consideration": number_or_text(row.v(F)),
            "class": clean_text(row.v(G)),
            "type": clean_text(row.v(H)),
            "date": self._date(row, cols["appoint"], "持股日期", "holding.date"),
            "end_date": self._date(row, cols["resign"], "持股結束日期", "holding.end_date", end=True),
            "transfer": transfer,
            "declaration_of_trust": trust,
            "pre_transfer": False,
            "outdated": bool(reasons),
            "outdated_reasons": reasons,
        })

    @staticmethod
    def _dates_from_holdings(party: dict) -> None:
        """Fall back to the I / J dates written on the person's SH rows."""
        holdings = party["shareholdings"]
        is_date = lambda value: bool(value) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None
        if not party["appoint_date"]:
            first = next((h for h in holdings if is_date(h["date"])), None)
            if first:
                party["appoint_date"], party["appoint_date_row"] = first["date"], first["row"]
        # A holding's end date only means leaving the company for a pure shareholder who holds nothing now.
        shareholder_only = {role_kind(r) for r in party["roles"]} == {"SH"}
        if not party["resign_date"] and shareholder_only and not party["current_shares"]:
            last = next((h for h in reversed(holdings) if is_date(h["end_date"])), None)
            if last:
                party["resign_date"], party["resign_date_row"] = last["end_date"], last["row"]

    @staticmethod
    def _section(party: dict) -> str:
        kinds = [role_kind(r) for r in party["roles"]]
        if "DIR" in kinds:
            return "directors"
        if "CS" in kinds:
            return "company_secretaries"
        if kinds and all(k == "SH" for k in kinds):
            return "shareholders"
        return "other_parties"

    # ----- checks that need the whole company

    def _yellow_and_mixed_rows(self) -> None:
        run: list[int] = []
        outdated = self.sheet.outdated_fills

        def flush() -> None:
            if run:
                span = f"第 {run[0]} 行" if len(run) == 1 else f"第 {run[0]}–{run[-1]} 行"
                self.warn(run[0], "YELLOW_DATA_ROW", f"{span}有資料但塗了黃色（黃色本來是分隔），請確認意思", "info")
                run.clear()

        for row in self.rows:
            if any(fill in SEPARATOR_FILLS for fill in row.fills):
                run.append(row.n)
            else:
                flush()
            filled = [row.fills[i] in outdated for i in range(8) if not is_blank(row.v(i))]
            if any(filled) and not all(filled):
                self.warn(row, "MIXED_BLUE_ROW", "同一行只有部分儲存格塗藍", "info")
        flush()

    def _finish(self) -> None:
        c = self.c
        colour = c["label_color"]
        c["red_label"] = is_red(colour)
        c["color_name"] = color_name(colour)
        c["color_group"] = ("none" if not colour else "red" if c["red_label"]
                            else "orange" if is_orange(colour) else "other")
        if c["red_label"] and c["status"] == "active":
            c["status"] = "inactive"
        c["active"] = c["status"] == "active"
        if c["status"] in ("resigned", "deregistered") and not c["status_date"]:
            self.warn(self._status_row or c["row"], "STATUS_DATE_INVALID",
                      f"資料有誤，請檢查：A 欄寫了「{c['status_note']}」，但沒有有效日期", "error", "status")
        for party in self.parties:
            holdings = party["shareholdings"]
            if holdings:
                transfers = [i for i, h in enumerate(holdings) if h["transfer"]]
                start = transfers[-1] if transfers else 0
                for i, h in enumerate(holdings):
                    h["pre_transfer"] = i < start
                active = [h for h in holdings if not h["outdated"]]
                changes = sum(h["no_of_shares"] for h in active if isinstance(h["no_of_shares"], (int, float)))
                current = [h for h in holdings[start:] if not h["outdated"]]
                running = current[-1]["total_no_of_shares"] if current else None
                calc = current[-1] if current else None
                if calc is not None:
                    for holding in holdings:
                        if holding is calc:
                            break
                        if not holding["outdated"]:
                            holding["pre_transfer"] = True
                if party["outdated"] or not current:
                    party["current_shares"] = 0
                elif isinstance(running, (int, float)):
                    party["current_shares"] = running
                    party["current_holding_row"] = current[-1]["row"]
                    if running != changes:
                        self.warn(current[-1]["row"], "RUNNING_TOTAL_MISMATCH",
                                  f"現行持股行的股數加總是 {_fmt_num(changes)}，與累計股數（C 欄）"
                                  f"{_fmt_num(running)} 不同，已採用累計股數", "info", "holding.total_no_of_shares")
                else:
                    party["current_shares"] = changes
                    party["current_holding_row"] = current[-1]["row"]
                self._dates_from_holdings(party)
            c[self._section(party)].append(party)

        current_capital = [e for e in c["share_capital"] if not e["outdated"]]
        issued = None
        if current_capital:
            last_total = current_capital[-1]["total_no_of_shares"]
            if isinstance(last_total, (int, float)):
                issued = last_total
            else:
                numbers = [e["no_of_shares"] for e in current_capital
                           if isinstance(e["no_of_shares"], (int, float))]
                issued = sum(numbers) if numbers else None
        c["issued_shares"] = issued
        c["current_share_capital_row"] = current_capital[-1]["row"] if current_capital else None
        c["currency"] = current_capital[-1]["currency"] if current_capital else None
        self._check_share_capital(current_capital)
        capital_money = None
        if current_capital:
            entry = current_capital[-1]
            capital_money = next((entry[k] for k in ("total_consideration", "consideration")
                                  if isinstance(entry[k], (int, float))), None)

        total, counted = 0, False
        for section in ("directors", "shareholders", "company_secretaries", "other_parties"):
            for party in c[section]:
                shares = party["current_shares"]
                if issued and isinstance(shares, (int, float)):
                    party["percentage"] = round(shares / issued, 6)
                if not party["outdated"] and isinstance(shares, (int, float)) and shares:
                    self._current_consideration(party, issued, capital_money)
                    c["shareholding_summary"].append({
                        "name": party["name_en"] or party["name_zh"],
                        "label": party["label"],
                        "section": section,
                        "row": party["row"],
                        "current_shares": shares,
                        "percentage": party["percentage"],
                        "consideration": party["current_consideration"],
                        "consideration_source": party["current_consideration_source"],
                        "counted_in_total": section != "other_parties",
                    })
                    if section != "other_parties":
                        total += shares
                        counted = True
        c["holdings_total"] = total if counted else None

        if len(c["share_capital"]) == 1 and issued:
            for party in self.parties:
                for h in party["shareholdings"]:
                    held, shown = h["total_no_of_shares"], h["percentage_excel"]
                    if isinstance(held, (int, float)) and shown is not None and abs(held / issued - shown) > 0.005:
                        self.warn(h["row"], "PERCENT_MISMATCH",
                                  f"Excel 持股 % 是 {_fmt_pct(shown)}，按累計股數計算應為 {held / issued:.2%}", "info",
                                  "holding.percentage_excel")
        self._resolve_registered_office()
        self._check_company_secretary()
        self._validate()
        self.issues.sort(key=lambda issue: issue["row"] or 0)
        c["counts"] = {
            "current_directors": sum(1 for p in c["directors"] if not p["outdated"]),
            "current_company_secretaries": sum(1 for p in c["company_secretaries"] if not p["outdated"]),
            "current_shareholders": len(c["shareholding_summary"]),
            "issues": {s: sum(1 for i in self.issues if i["severity"] == s) for s in SEVERITIES},
        }

    def _current_consideration(self, party: dict, issued, capital_money) -> None:
        """Money paid for the current holding: Excel's total consideration, else a pro-rata estimate."""
        record = next((h for h in party["shareholdings"] if h["row"] == party["current_holding_row"]), None)
        money = None
        if record:
            money = next((record[k] for k in ("total_consideration", "consideration")
                          if isinstance(record[k], (int, float))), None)
        shares = party["current_shares"]
        estimate = round(shares / issued * capital_money, 2) if issued and capital_money is not None else None
        if money is not None and 0 < money <= 1 < shares and estimate is not None and estimate > 1:
            self.warn(record["row"], "CONSIDERATION_LOOKS_LIKE_PERCENT",
                      f"持股行的代價是 {_fmt_num(money)}，看來抄了持股 %，已按股本比例估算為 {_fmt_num(estimate)}",
                      "info", "holding.consideration")
            money = None
        if money is not None:
            party["current_consideration"], party["current_consideration_source"] = money, "excel"
        elif estimate is not None:
            party["current_consideration"], party["current_consideration_source"] = estimate, "estimated"

    def _check_share_capital(self, current_capital: list[dict]) -> None:
        """Share capital is mandatory for every company, active or not."""
        c = self.c
        if not c["share_capital"]:
            c["share_capital_missing"] = [key for key, _ in REQUIRED_SHARE_FIELDS]
            self.warn(c["row"], "NO_SHARES", "沒有股本 Shares 資料（必須填寫）", field="share_capital")
            return
        if not current_capital:
            c["share_capital_missing"] = [key for key, _ in REQUIRED_SHARE_FIELDS]
            self.warn(c["row"], "NO_CURRENT_SHARES", "股本 Shares 全部已過期（OUTDATED），沒有現行股本資料",
                      field="share_capital")
            return
        entry = current_capital[-1]
        missing = [(key, label) for key, label in REQUIRED_SHARE_FIELDS if entry[key] in (None, "")]
        c["share_capital_missing"] = [key for key, _ in missing]
        if missing:
            self.warn(entry["row"], "SHARES_FIELDS_MISSING",
                      "股本 Shares 缺少：" + "、".join(label for _, label in missing), field="share_capital")

    def _check_company_secretary(self) -> None:
        """At least one company secretary is required; more than one is allowed."""
        c = self.c
        if not c["company_secretaries"]:
            self.warn(c["row"], "NO_CS", "沒有任何公司秘書（Com Sec）記錄（必須最少一個）", field="company_secretaries")
        elif c["active"] and all(p["outdated"] for p in c["company_secretaries"]):
            self.warn(c["row"], "NO_CURRENT_CS", "沒有現任公司秘書（必須最少一個）", field="company_secretaries")

    def _resolve_registered_office(self) -> None:
        """Keep exactly one current RO: the one with the latest start date (column I)."""
        c = self.c
        offices = c["registered_offices"]
        severity = "warning" if c["active"] else "info"
        if not offices:
            self.warn(c["row"], "NO_RO", "沒有 RO（註冊辦事處）資料", severity, "registered_offices")
            return
        is_date = lambda value: bool(value) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None
        # Blue OR a real end date already makes an RO outdated; the latest start date wins among the rest.
        candidates = [e for e in offices if not e["outdated"]]
        dated = [e for e in candidates if is_date(e["from"])]
        if dated:
            current = max(dated, key=lambda e: (e["from"], e["row"]))
        else:
            current = candidates[-1] if candidates else None
            if len(candidates) > 1:
                self.warn(current["row"], "RO_WITHOUT_DATES",
                          f"有 {len(candidates)} 個 RO 都沒有日期，已把最後一個當作現行 RO", "info", "ro.status")
        if current is None:
            self.warn(c["row"], "NO_CURRENT_RO", "沒有現行 RO（所有 RO 都已塗藍或有結束日期）", severity,
                      "registered_offices")
            return
        for entry in candidates:
            if entry is not current:
                entry["outdated"], entry["outdated_reasons"] = True, ["superseded"]
        newer = [e for e in offices if e["outdated"] and e is not current and is_date(e["from"])
                 and is_date(current["from"]) and e["from"] > current["from"] and "superseded" not in e["outdated_reasons"]]
        if newer:
            self.warn(current["row"], "OLDER_RO_IS_CURRENT",
                      f"較新的 RO（第 {newer[-1]['row']} 行）已塗藍或有結束日期，現行 RO 是較舊的這一個，請確認",
                      field="ro.status")

    def _validate(self) -> None:
        c = self.c
        if c["status"] != "active":
            return
        row = c["row"]
        if not any(not p["outdated"] for p in c["directors"]):
            self.warn(row, "NO_CURRENT_DIRECTOR", "沒有現任董事", field="directors")
        issued, total = c["issued_shares"], c["holdings_total"]
        if issued is not None and total is not None and abs(total - issued) > 1e-6:
            self.warn(row, "SHARE_TOTAL_MISMATCH",
                      f"現行持股加總 {_fmt_num(total)} 股，與已發行股數 {_fmt_num(issued)} 股不符",
                      field="shareholding_summary")
        elif issued is not None and total is None:
            self.warn(row, "NO_CURRENT_HOLDINGS", "有股本資料但找不到任何現行持股（SH）", field="shareholding_summary")


# --------------------------------------------------------------------------- sheet

class _SheetParser:
    def __init__(self, wb, ws, outdated_fills):
        self.wb = wb
        self.ws = ws
        self.outdated_fills = frozenset(f.upper().lstrip("#")[-6:] for f in outdated_fills)
        self.fill_of = FillReader(wb)
        self.cols = dict(DEFAULT_COLUMNS)
        self.issues: list[dict] = []

    def is_blue(self, row: Row) -> bool:
        fills = row.fills
        if fills[A] in self.outdated_fills or fills[B] in self.outdated_fills:
            return True
        if is_blank(row.v(B)):
            return any(fills[i] in self.outdated_fills for i in range(C, H + 1) if not is_blank(row.v(i)))
        return False

    def has_end(self, row: Row) -> bool:
        """Only a real date in the resign / end column ends a record; unclear text is flagged instead."""
        result = parse_date(row.v(self.cols["resign"]))
        return bool(result and result.ok)

    def reasons(self, row: Row) -> list[str]:
        reasons = []
        if self.is_blue(row):
            reasons.append("blue")
        if self.has_end(row):
            reasons.append("end_date")
        return reasons

    def is_separator(self, row: Row) -> bool:
        return row.blank(A, H + 1) and any(fill in SEPARATOR_FILLS for fill in row.fills)

    def _read_rows(self) -> list[Row]:
        ws = self.ws
        max_col = max(ws.max_column or 0, MIN_COLUMNS)
        rows = []
        for cells in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=max_col):
            rows.append(Row(cells[0].row, [cell.value for cell in cells],
                            [self.fill_of(cell) for cell in cells[:8]]))
        return rows

    @staticmethod
    def _header_index(rows: list[Row]) -> int | None:
        for i, row in enumerate(rows[:60]):
            texts = {clean_text(v).lower() for v in row.values if not is_blank(v)}
            if "date of birth" in texts and texts & {"ci", "br"}:
                return i
        return None

    def parse(self, file_name: str | None) -> dict:
        rows = self._read_rows()
        hi = self._header_index(rows)
        if hi is None:
            raise ParseError(f"工作表「{self.ws.title}」找不到表頭（Date of birth / CI / BR）")
        header2 = rows[hi]
        header1 = rows[hi - 1] if hi > 0 else None
        for idx, value in enumerate(header2.values):
            key = HEADER_ALIASES.get(clean_text(value).lower()) if not is_blank(value) else None
            if key:
                self.cols[key] = idx
        skipped = [r for r in rows[:max(hi - 1, 0)] if not r.empty]
        if skipped:
            first = header1.n if header1 else header2.n
            self.issues.append(_issue(None, "warning", "PRE_HEADER_ROWS",
                                      f"表頭（第 {first}–{header2.n} 行）之前有 {len(skipped)} 行其他內容，"
                                      f"已略過、不會輸出；請確認是否包含帳號密碼等敏感資料"))
        data = rows[hi + 1:]
        for row in data:
            row.label = parse_label(row.v(A))
        companies = [_CompanyBuilder(self, group["rows"], group["separated"], group["issues"]).build()
                     for group in self._split(data)]
        self._assign_ids(companies)
        counts = Counter(i["severity"] for i in self.issues)
        for company in companies:
            counts.update(i["severity"] for i in company["issues"])
        sections = ("directors", "shareholders", "company_secretaries", "other_parties")
        return {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "file": file_name,
                "sheet": self.ws.title,
                "header_rows": [r.n for r in (header1, header2) if r is not None],
                "data_start_row": header2.n + 1,
                "parsed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            },
            "rules": {
                "outdated": "底色為藍色「或」J 欄（Resign date）有有效的辭任／結束日期，任何一項成立即當作 outdated；"
                            "不清晰的內容只會發出提示，不當作 outdated",
                "outdated_fills": sorted(self.outdated_fills),
                "separator_fills": sorted(SEPARATOR_FILLS),
                "text_dates": "文字日期按 日/月/年 讀取",
                "percentage": "持股 % 由股數重新計算；其他標籤（BO、DT 等）的持股不計入總數",
                "inactive": "公司編號（A 欄）底色為紅色，或 A 欄註明 Resigned／Dereg，即當作 inactive",
                "color_group": "公司編號底色分組：orange＝正常現行，red＝inactive，other＝其他顏色，none＝沒有底色",
                "registered_office": "塗藍或有結束日期的 RO 一定是 outdated；其餘 RO 中只保留開始日期（I 欄）最新的一個為現行，"
                                     "其他當作 outdated（superseded）",
                "share_capital": "股本 Shares 必須有；現行 Shares 行缺少 No. of shares、Total no. of shares、Consideration、"
                                 "Total consideration 或 Currency 都會發出警告",
                "company_secretary": "公司秘書可以多於一個，但必須最少一個；現行公司必須最少一個現任公司秘書",
                "transfers": "有轉讓時只用最後一次轉讓後的記錄計算現時持股；轉讓前的記錄保留並標示 pre_transfer",
                "missing_fields": "缺少中文名、證件、地址等資料不視為錯誤",
            },
            "general_header": {
                "row_1": {LETTERS[i]: clean_text(v) for i, v in enumerate(header1.values)
                          if header1 and not is_blank(v)} if header1 else {},
                "row_2": {LETTERS[i]: clean_text(v) for i, v in enumerate(header2.values) if not is_blank(v)},
            },
            "summary": {
                "companies": len(companies),
                "by_status": dict(Counter(c["status"] for c in companies)),
                "by_color_group": dict(Counter(c["color_group"] for c in companies)),
                "parties": sum(len(c[s]) for c in companies for s in sections),
                "issues": {s: counts.get(s, 0) for s in SEVERITIES},
            },
            "issues": self.issues,
            "companies": companies,
        }

    def _split(self, data: list[Row]) -> list[dict]:
        groups: list[dict] = []
        current: dict | None = None
        after_separator = True
        for row in data:
            if self.is_separator(row):
                after_separator = True
                continue
            if row.empty:
                continue
            label = row.label
            starts = (label is not None and label.kind == "COMPANY") or \
                     (after_separator and label is None and not is_blank(row.v(B))
                      and not _CompanyBuilder._is_subheader(row))
            if starts:
                current = {"rows": [row], "separated": after_separator or not groups, "issues": []}
                groups.append(current)
            elif current is None:
                self.issues.append(_issue(row.n, "warning", "BEFORE_FIRST_COMPANY",
                                          "表頭之後、第一間公司之前有資料，已略過"))
            else:
                if after_separator:
                    current["issues"].append(_issue(row.n, "info", "SEPARATOR_INSIDE_COMPANY",
                                                    "黃色分隔行之後不是公司首行，已當作同一間公司的資料"))
                current["rows"].append(row)
            after_separator = False
        return groups

    def _assign_ids(self, companies: list[dict]) -> None:
        seen: Counter = Counter(c["company_no"] for c in companies if c["company_no"])
        for company in companies:
            number = company["company_no"]
            if number and seen[number] > 1:
                company["issues"].append(_issue(company["row"], "warning", "DUPLICATE_COMPANY_NO",
                                                f"公司編號 {number} 重複出現"))
            company["id"] = number if number and seen[number] == 1 else f"R{company['row']}"


# --------------------------------------------------------------------------- entry point

def _pick_sheet(wb, sheet: str | None):
    if sheet and sheet in wb.sheetnames:
        return wb[sheet]
    if DEFAULT_SHEET in wb.sheetnames:
        return wb[DEFAULT_SHEET]
    for ws in wb.worksheets:
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 60), values_only=True):
            texts = {clean_text(v).lower() for v in row if not is_blank(v)}
            if "date of birth" in texts and texts & {"ci", "br"}:
                return ws
    raise ParseError("找不到 CDD 工作表（需要有 Date of birth / CI / BR 表頭）")


def parse_workbook(source: str | Path | bytes | Any, *, sheet: str | None = DEFAULT_SHEET,
                   outdated_fills=DEFAULT_OUTDATED_FILLS, file_name: str | None = None) -> dict:
    """Parse an Excel path, bytes or file object and return the JSON-ready dict."""
    if isinstance(source, (bytes, bytearray)):
        source = BytesIO(source)
    if file_name is None and isinstance(source, (str, Path)):
        file_name = Path(source).name
    wb = openpyxl.load_workbook(source, data_only=True)
    try:
        ws = _pick_sheet(wb, sheet)
        return _SheetParser(wb, ws, outdated_fills).parse(file_name)
    finally:
        wb.close()


MAX_PREVIEW_ROWS = 8000
MAX_PREVIEW_COLS = 40


def _preview_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        if value.hour or value.minute or value.second:
            return value.strftime("%Y-%m-%d %H:%M")
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


_BORDER_WIDTH = {
    "hair": "1px", "thin": "1px", "medium": "2px", "thick": "3px", "double": "3px",
    "mediumDashed": "2px", "mediumDashDot": "2px", "mediumDashDotDot": "2px", "slantDashDot": "2px",
}
_BORDER_LINE = {
    "dashed": "dashed", "mediumDashed": "dashed", "dashDot": "dashed", "mediumDashDot": "dashed",
    "dashDotDot": "dotted", "mediumDashDotDot": "dotted", "dotted": "dotted", "hair": "solid",
    "double": "double", "slantDashDot": "dashed",
}


def _style_color(color, palette: dict[str, str]) -> str | None:
    if color is None or getattr(color, "type", None) is None:
        return None
    if color.type == "rgb" and isinstance(color.rgb, str):
        value = color.rgb.upper()
        if value == "00000000":
            return "000000"
        if len(value) == 8 and value.startswith("00"):
            return None
        return value[-6:]
    if color.type == "theme" and color.theme is not None and color.theme < len(_THEME_INDEX):
        base = palette.get(_THEME_INDEX[color.theme])
        if base:
            return _apply_tint(base, color.tint) if color.tint else base
    if color.type == "indexed" and color.indexed is not None and color.indexed < len(COLOR_INDEX):
        return COLOR_INDEX[color.indexed][-6:]
    if color.type == "auto":
        return "000000"
    return None


def _side_css(side, palette: dict[str, str]) -> str | None:
    if side is None or not side.style or side.style == "none":
        return None
    width = _BORDER_WIDTH.get(side.style, "1px")
    line = _BORDER_LINE.get(side.style, "solid")
    color = _style_color(side.color, palette) or "000000"
    return f"{width} {line} #{color}"


def _cell_border(cell, palette: dict[str, str]) -> dict | None:
    border = getattr(cell, "border", None)
    if border is None:
        return None
    sides = {}
    for key, side in (("t", border.top), ("r", border.right), ("b", border.bottom), ("l", border.left)):
        css = _side_css(side, palette)
        if css:
            sides[key] = css
    return sides or None


def _column_width(ws, index: int) -> int:
    """Excel character width as CSS pixels, using the Calibri 11 column formula."""
    letter = get_column_letter(index)
    dimension = ws.column_dimensions.get(letter)
    if dimension is not None and dimension.hidden:
        return 0
    width = dimension.width if dimension is not None and dimension.width else None
    if not width:
        width = getattr(ws.sheet_format, "defaultColWidth", None) or 8.43
    width = float(width)
    if width < 1:
        return max(2, round(width * 12))
    return max(2, round(width * 7 + 5))


def _points_to_px(points: float) -> int:
    return max(1, round(float(points) * 96 / 72))


def _cell_font(cell, palette: dict[str, str]) -> dict | None:
    font = getattr(cell, "font", None)
    if font is None:
        return None
    spec: dict = {}
    if font.sz is not None and float(font.sz) != 11:
        spec["s"] = float(font.sz)
    if font.b:
        spec["b"] = True
    if font.i:
        spec["i"] = True
    name = font.name
    if name and name.lower() not in {"calibri", "等线"}:
        spec["f"] = name
    color = _style_color(font.color, palette)
    if color and color != "000000":
        spec["c"] = color
    if getattr(getattr(cell, "alignment", None), "wrap_text", None):
        spec["w"] = True
    return spec or None


def _row_height(ws, index: int, fonts: list[dict | None]) -> int:
    dimension = ws.row_dimensions[index]
    if getattr(dimension, "hidden", False):
        return 0
    if dimension.height:
        return _points_to_px(dimension.height)
    sizes = [item["s"] for item in fonts if item and item.get("s")]
    points = (max(sizes) if sizes else 11) * 15 / 11
    default = getattr(ws.sheet_format, "defaultRowHeight", None)
    if not sizes and default:
        points = float(default)
    return _points_to_px(points)


def preview_sheet(source: str | Path | bytes | Any, sheet: str | None = None) -> dict:
    """Read one worksheet for display, including text, fills and the drawn borders."""
    if isinstance(source, (bytes, bytearray)):
        source = BytesIO(source)
    wb = openpyxl.load_workbook(source, data_only=True)
    try:
        names = list(wb.sheetnames)
        if not names:
            raise ParseError("這個 Excel 沒有工作表")
        if sheet and sheet in names:
            ws = wb[sheet]
        elif DEFAULT_SHEET in names:
            ws = wb[DEFAULT_SHEET]
        else:
            ws = wb[names[0]]
        palette = _theme_palette(wb)
        fills = FillReader(wb)
        stored: list[dict] = []
        width = 1
        last_used = 0
        truncated_rows = bool(ws.max_row and ws.max_row > MAX_PREVIEW_ROWS)
        truncated_columns = bool(ws.max_column and ws.max_column > MAX_PREVIEW_COLS)
        for index, row in enumerate(
            ws.iter_rows(max_row=MAX_PREVIEW_ROWS, max_col=MAX_PREVIEW_COLS), start=1,
        ):
            texts: list[str] = []
            borders: list[dict | None] = []
            paints: list[str | None] = []
            marked: list[dict | None] = []
            fonts: list[dict | None] = []
            pieces: list[list | None] = []
            row_used = 0
            for cell in row:
                text = _preview_text(cell.value)
                comment = getattr(cell, "comment", None)
                note = str(getattr(comment, "text", "") or "") if comment is not None else ""
                spec, shown = preview_fill(text, note)
                border = _cell_border(cell, palette)
                paint = fills(cell)
                font = _cell_font(cell, palette)
                texts.append(shown)
                borders.append(border)
                paints.append(paint)
                marked.append(spec)
                fonts.append(font)
                pieces.append(None if spec else inline_parts(shown))
                if shown or spec or border or paint or font:
                    row_used = cell.column
            if row_used:
                last_used = index
                width = max(width, min(row_used, MAX_PREVIEW_COLS))
            stored.append({
                "cells": texts,
                "borders": borders,
                "fills": paints,
                "fields": marked,
                "fonts": fonts,
                "parts": pieces,
                "height": _row_height(ws, index, fonts),
                "owners": fill_owners(texts[0] if texts else ""),
            })
        merges = []
        for merged in ws.merged_cells.ranges:
            if merged.min_row > MAX_PREVIEW_ROWS or merged.min_col > MAX_PREVIEW_COLS:
                continue
            last_row = min(merged.max_row, MAX_PREVIEW_ROWS)
            last_col = min(merged.max_col, MAX_PREVIEW_COLS)
            merges.append({
                "r": merged.min_row,
                "c": merged.min_col,
                "rs": last_row - merged.min_row + 1,
                "cs": last_col - merged.min_col + 1,
            })
            last_used = max(last_used, last_row)
            width = max(width, last_col)
        stored = stored[:last_used]
        for item in stored:
            item["cells"] = (item["cells"] + [""] * width)[:width]
            item["borders"] = (item["borders"] + [None] * width)[:width]
            item["fills"] = (item["fills"] + [None] * width)[:width]
            item["fields"] = (item["fields"] + [None] * width)[:width]
            item["fonts"] = (item["fonts"] + [None] * width)[:width]
            item["parts"] = (item["parts"] + [None] * width)[:width]
        view = ws.sheet_view
        gridlines = True if view.showGridLines is None else bool(view.showGridLines)
        return {
            "sheet": ws.title,
            "sheets": names,
            "columns": [get_column_letter(i) for i in range(1, width + 1)],
            "widths": [_column_width(ws, i) for i in range(1, width + 1)],
            "default_font_size": 11,
            "default_row_height": _points_to_px(getattr(ws.sheet_format, "defaultRowHeight", None) or 15),
            "rows": stored,
            "merges": merges,
            "gridlines": gridlines,
            "truncated_rows": truncated_rows,
            "truncated_columns": truncated_columns,
        }
    finally:
        wb.close()
