"""Split a form workbook into one worksheet per printed page.

Pages come from the Excel print area. A manual page break inside a range
starts another page, including when the sheet has several print areas or none.
The Index sheet is left out. Each page is shifted to the top-left of a new
sheet and the sheets are assembled into one workbook for filling.
"""

from __future__ import annotations

import re
import zipfile
from copy import copy
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.formula.translate import Translator
from openpyxl.comments import Comment
from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries
from openpyxl.workbook.workbook import Workbook as WorkbookType
from openpyxl.worksheet.worksheet import Worksheet

_INVALID_NAME = str.maketrans({":": " ", "\\": " ", "/": " ", "?": " ", "*": " ", "[": " ", "]": " "})
# A formula that only reads another cell, such as =C19 or =[1]Index!B15.
_CELL_CALL = re.compile(
    r"""^=\s*(?:(?:\[[^\]]+\])?(?:'(?:[^']|'')+'|[A-Za-z_][\w.]*)!)?\$?[A-Z]{1,3}\$?\d+\s*$""",
    re.IGNORECASE,
)
_CELL_REF = re.compile(
    r"""^=\s*(?:(?:\[[^\]]+\])?(?:'((?:[^']|'')+)'|([A-Za-z_][\w.]*))!)?(\$?)([A-Za-z]{1,3})(\$?)(\d+)\s*$""",
    re.IGNORECASE,
)
_QUOTED_SHEET = re.compile(r"^'(?P<name>(?:[^']|'')*)'!(?P<cells>.*)$")
_SSML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def page_ranges(ws: Worksheet, raw: str | None = None) -> list[tuple[int, int, int, int]]:
    """Return pages as ``(min_col, min_row, max_col, max_row)``."""
    formula = raw if raw is not None else str(getattr(ws, "print_area", None) or "")
    areas = _ranges_from_formula(formula, ws, ws.title)
    if not areas and ws.max_row and ws.max_column:
        areas = [(1, 1, ws.max_column, ws.max_row)]
    pages = []
    for area in areas:
        pages.extend(_split_on_breaks(ws, area))
    return pages


def split_form_pages(
    source: WorkbookType,
    displayed: WorkbookType | None = None,
    print_areas: dict[str, str] | None = None,
) -> Workbook:
    """Build a new workbook whose sheets are the printed pages of ``source``.

    ``displayed`` is the same file opened with cached values. A formula that only
    reads another cell keeps that shown text and records which cell it reads.
    """
    output = Workbook()
    default = output.active
    created = False
    for ws in source.worksheets:
        if _ignored(ws.title):
            continue
        ranges = page_ranges(ws, _raw_for(print_areas, ws.title))
        if not ranges:
            continue
        shown = displayed[ws.title] if displayed is not None and ws.title in displayed.sheetnames else None
        multiple = len(ranges) > 1
        for index, bounds in enumerate(ranges, start=1):
            title = _page_title(ws.title, index, multiple, output.sheetnames)
            sheet = output.create_sheet(title)
            _copy_page(ws, sheet, bounds, shown)
            created = True
    if created and default is not None and default.title == "Sheet" and default.max_row == 1 and default["A1"].value is None:
        output.remove(default)
    return output


def split_workbook_bytes(path: Path) -> bytes:
    book = load_workbook(path)
    displayed = load_workbook(path, data_only=True)
    try:
        output = split_form_pages(book, displayed, _workbook_print_areas(path))
        buffer = BytesIO()
        output.save(buffer)
        return buffer.getvalue()
    finally:
        book.close()
        displayed.close()


def _ignored(title: str) -> bool:
    return title.strip().lower() == "index"


def _raw_for(print_areas: dict[str, str] | None, title: str) -> str | None:
    if not print_areas:
        return None
    if title in print_areas:
        return print_areas[title]
    folded = title.casefold()
    for name, raw in print_areas.items():
        if name.casefold() == folded:
            return raw
    return None


def _workbook_print_areas(path: Path) -> dict[str, str]:
    """Print-area formulas keyed by sheet. Read from the file so a range openpyxl rejects is kept."""
    try:
        with zipfile.ZipFile(path) as book:
            root = ET.fromstring(book.read("xl/workbook.xml"))
    except (OSError, KeyError, ET.ParseError):
        return {}
    sheets = [node.attrib.get("name", "") for node in root.findall(f"{_SSML}sheets/{_SSML}sheet")]
    grouped: dict[str, list[str]] = {}
    for node in root.findall(f"{_SSML}definedNames/{_SSML}definedName"):
        if node.attrib.get("name") != "_xlnm.Print_Area":
            continue
        text = (node.text or "").strip().lstrip("=")
        if not text:
            continue
        owner = ""
        sheet_id = node.attrib.get("localSheetId")
        if sheet_id is not None:
            try:
                owner = sheets[int(sheet_id)]
            except (ValueError, IndexError):
                owner = ""
        grouped.setdefault(owner, []).append(text)
    return {name: ",".join(parts) for name, parts in grouped.items() if name}


def _split_union(raw: str) -> list[str]:
    parts = []
    buf: list[str] = []
    quoted = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "'":
            if quoted and index + 1 < len(raw) and raw[index + 1] == "'":
                buf.append("''")
                index += 2
                continue
            quoted = not quoted
            buf.append(char)
        elif char == "," and not quoted:
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
        else:
            buf.append(char)
        index += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _sheet_and_cells(piece: str) -> tuple[str, str]:
    piece = piece.strip()
    quoted = _QUOTED_SHEET.match(piece)
    if quoted:
        return quoted.group("name").replace("''", "'"), quoted.group("cells").strip()
    if "!" in piece:
        sheet, cells = piece.rsplit("!", 1)
        return sheet.strip(), cells.strip()
    return "", piece


def _bounds(cells: str, ws: Worksheet) -> tuple[int, int, int, int] | None:
    cells = cells.replace("$", "").strip()
    if not cells or cells.startswith("#"):
        return None
    try:
        min_col, min_row, max_col, max_row = range_boundaries(cells)
    except ValueError:
        return None
    if min_col is None:
        min_col = 1
    if max_col is None:
        max_col = ws.max_column or min_col
    if min_row is None:
        min_row = 1
    if max_row is None:
        max_row = ws.max_row or min_row
    if min_col > max_col or min_row > max_row:
        return None
    return min_col, min_row, max_col, max_row


def _ranges_from_formula(raw: str, ws: Worksheet, owner: str) -> list[tuple[int, int, int, int]]:
    ranges = []
    for piece in _split_union(raw.strip().lstrip("=")):
        sheet, cells = _sheet_and_cells(piece)
        if sheet and sheet.casefold() != owner.casefold():
            continue
        area = _bounds(cells, ws)
        if area is not None:
            ranges.append(area)
    return ranges


def _split_on_breaks(ws: Worksheet, area: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
    min_col, min_row, max_col, max_row = area
    row_breaks = sorted(
        break_.id for break_ in getattr(ws.row_breaks, "brk", [])
        if getattr(break_, "man", True) and min_row <= break_.id < max_row
    )
    col_breaks = sorted(
        break_.id for break_ in getattr(ws.col_breaks, "brk", [])
        if getattr(break_, "man", True) and min_col <= break_.id < max_col
    )
    row_edges = [min_row - 1, *row_breaks, max_row]
    col_edges = [min_col - 1, *col_breaks, max_col]
    pages = []
    for row_index in range(len(row_edges) - 1):
        for col_index in range(len(col_edges) - 1):
            pages.append((
                col_edges[col_index] + 1,
                row_edges[row_index] + 1,
                col_edges[col_index + 1],
                row_edges[row_index + 1],
            ))
    return pages


def _page_title(base: str, index: int, multiple: bool, existing: list[str]) -> str:
    cleaned = base.translate(_INVALID_NAME).strip() or "Sheet"
    name = cleaned
    if multiple:
        suffix = f"-P{index}"
        name = f"{cleaned[:31 - len(suffix)]}{suffix}"
    name = name[:31]
    if name not in existing:
        return name
    number = 2
    while True:
        suffix = f"_{number}"
        candidate = f"{name[:31 - len(suffix)]}{suffix}"
        if candidate not in existing:
            return candidate
        number += 1


def _title_rows(ws: Worksheet, page: tuple[int, int, int, int]) -> tuple[int, int] | None:
    raw = str(ws.print_title_rows or "").replace("$", "")
    if ":" not in raw:
        return None
    try:
        start, end = (int(part) for part in raw.split(":", 1))
    except ValueError:
        return None
    _min_col, min_row, _max_col, max_row = page
    if end < start or not (end < min_row or start > max_row):
        return None
    return start, end


def _copy_page(ws: Worksheet, target: Worksheet, bounds: tuple[int, int, int, int], displayed: Worksheet | None = None) -> None:
    min_col, min_row, max_col, max_row = bounds
    blocks = []
    titles = _title_rows(ws, bounds)
    dest_row = 1
    if titles:
        blocks.append((min_col, titles[0], max_col, titles[1], dest_row, 1))
        dest_row += titles[1] - titles[0] + 1
    blocks.append((min_col, min_row, max_col, max_row, dest_row, 1))
    for block in blocks:
        _copy_block(ws, target, block, displayed)
    _copy_sheet_setup(ws, target, blocks)


def _copy_block(ws: Worksheet, target: Worksheet, block: tuple[int, int, int, int, int, int], displayed: Worksheet | None = None) -> None:
    src_c1, src_r1, src_c2, src_r2, dest_r, dest_c = block

    def shift(row: int, col: int) -> tuple[int, int]:
        return dest_r + (row - src_r1), dest_c + (col - src_c1)

    for row in range(src_r1, src_r2 + 1):
        new_row, _new_col = shift(row, src_c1)
        source_dimension = ws.row_dimensions[row]
        target_dimension = target.row_dimensions[new_row]
        target_dimension.height = source_dimension.height
        target_dimension.hidden = source_dimension.hidden
        for col in range(src_c1, src_c2 + 1):
            cell = ws.cell(row, col)
            if isinstance(cell, MergedCell):
                continue
            new_row, new_col = shift(row, col)
            _copy_cell(
                cell,
                target.cell(new_row, new_col),
                bounds=(src_c1, src_r1, src_c2, src_r2),
                shift=shift,
                displayed=displayed.cell(row, col) if displayed is not None else None,
            )
    for col in range(src_c1, src_c2 + 1):
        letter = get_column_letter(col)
        new_letter = get_column_letter(shift(src_r1, col)[1])
        source_dimension = ws.column_dimensions[letter]
        target_dimension = target.column_dimensions[new_letter]
        target_dimension.width = source_dimension.width
        target_dimension.hidden = source_dimension.hidden
    for merged in ws.merged_cells.ranges:
        if merged.max_row < src_r1 or merged.min_row > src_r2 or merged.max_col < src_c1 or merged.min_col > src_c2:
            continue
        left, right = max(merged.min_col, src_c1), min(merged.max_col, src_c2)
        top, bottom = max(merged.min_row, src_r1), min(merged.max_row, src_r2)
        new_top, new_left = shift(top, left)
        new_bottom, new_right = shift(bottom, right)
        target.merge_cells(start_row=new_top, start_column=new_left, end_row=new_bottom, end_column=new_right)
        if (merged.min_row, merged.min_col) != (top, left):
            origin = ws.cell(merged.min_row, merged.min_col)
            corner = target.cell(new_top, new_left)
            if corner.value is None:
                _copy_cell(
                    origin,
                    corner,
                    bounds=(src_c1, src_r1, src_c2, src_r2),
                    shift=shift,
                    displayed=displayed.cell(origin.row, origin.column) if displayed is not None else None,
                )


def _reference_label(formula: str, bounds: tuple[int, int, int, int], shift) -> str:
    """Address a cell-call formula reads, moved when that cell is on this page."""
    match = _CELL_REF.match(formula.strip())
    if not match:
        return formula.lstrip("=").strip()
    quoted, bare, _col_abs, col, _row_abs, row = match.groups()
    sheet = (quoted or bare or "").replace("''", "'")
    col_index = column_index_from_string(col.upper())
    row_index = int(row)
    min_col, min_row, max_col, max_row = bounds
    address = f"{col.upper()}{row_index}"
    if not sheet and min_col <= col_index <= max_col and min_row <= row_index <= max_row:
        new_row, new_col = shift(row_index, col_index)
        return f"{get_column_letter(new_col)}{new_row}"
    if sheet:
        shown = f"'{sheet}'" if re.search(r"\s", sheet) else sheet
        return f"{shown}!{address}"
    return address


def _note_reference(cell, label: str) -> None:
    line = f"引用：{label}"
    if cell.comment is not None and str(cell.comment.text or "").strip():
        cell.comment.text = str(cell.comment.text).rstrip() + "\n" + line
        return
    cell.comment = Comment(line, "CDD")


def _blank_value(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _formula_text(value) -> bool:
    return isinstance(value, str) and value.strip().startswith("=")


_ABSENT = object()


def _referenced_value(source, formula: str):
    """The cell a reference reads. ``_ABSENT`` when that cell is not in this workbook."""
    match = _CELL_REF.match(formula.strip())
    if not match:
        return _ABSENT
    quoted, bare, _col_abs, col, _row_abs, row = match.groups()
    sheet = (quoted or bare or "").replace("''", "'")
    book = source.parent.parent
    if sheet:
        if sheet not in book.sheetnames:
            return _ABSENT
        worksheet = book[sheet]
    else:
        worksheet = source.parent
    return worksheet.cell(int(row), column_index_from_string(col)).value


def _copy_cell(
    source,
    target,
    source_at: tuple[int, int] | None = None,
    target_at: tuple[int, int] | None = None,
    bounds: tuple[int, int, int, int] | None = None,
    shift=None,
    displayed=None,
) -> None:
    value = source.value
    reference = None
    if isinstance(value, str) and _CELL_CALL.match(value.strip()):
        if bounds is not None and shift is not None:
            reference = _reference_label(value, bounds, shift)
        live = _referenced_value(source, value)
        if live is _ABSENT:
            cached = None if displayed is None else displayed.value
            value = "" if _blank_value(cached) or _formula_text(cached) else cached
        elif _blank_value(live) or _formula_text(live):
            value = ""
        else:
            value = live
    elif isinstance(value, str) and value.startswith("="):
        origin = source.coordinate if source_at is None else f"{get_column_letter(source_at[1])}{source_at[0]}"
        dest = target.coordinate if target_at is None else f"{get_column_letter(target_at[1])}{target_at[0]}"
        try:
            value = Translator(value, origin=origin).translate_formula(dest)
        except Exception:
            value = source.value
    target.value = value
    if source.has_style:
        target.font = copy(source.font)
        target.border = copy(source.border)
        target.fill = copy(source.fill)
        target.number_format = source.number_format
        target.protection = copy(source.protection)
        target.alignment = copy(source.alignment)
    if source.comment is not None:
        target.comment = copy(source.comment)
    if reference:
        _note_reference(target, reference)
    if source.hyperlink is not None:
        target.hyperlink = copy(source.hyperlink)


_PAGE_SETUP_FIELDS = (
    "orientation", "paperSize", "paperHeight", "paperWidth", "scale",
    "fitToWidth", "fitToHeight", "pageOrder", "usePrinterDefaults",
    "blackAndWhite", "draft", "cellComments", "useFirstPageNumber",
    "firstPageNumber", "copies", "errors", "horizontalDpi", "verticalDpi",
)


def _copy_sheet_setup(ws: Worksheet, target: Worksheet, blocks: list[tuple[int, int, int, int, int, int]]) -> None:
    last_row = 1
    last_col = 1
    for src_c1, _src_r1, src_c2, src_r2, dest_r, dest_c in blocks:
        last_row = max(last_row, dest_r + (src_r2 - _src_r1))
        last_col = max(last_col, dest_c + (src_c2 - src_c1))
    target.print_area = f"A1:{get_column_letter(last_col)}{last_row}"
    setup = ws.page_setup
    for name in _PAGE_SETUP_FIELDS:
        setattr(target.page_setup, name, getattr(setup, name))
    source_fit = ws.sheet_properties.pageSetUpPr
    target.sheet_properties.pageSetUpPr.fitToPage = None if source_fit is None else source_fit.fitToPage
    target.page_margins = copy(ws.page_margins)
    options = ws.print_options
    target.print_options.horizontalCentered = options.horizontalCentered
    target.print_options.verticalCentered = options.verticalCentered
    target.print_options.headings = options.headings
    target.print_options.gridLines = options.gridLines
    target.print_options.gridLinesSet = options.gridLinesSet
    target.sheet_format.defaultRowHeight = ws.sheet_format.defaultRowHeight
    target.sheet_format.defaultColWidth = ws.sheet_format.defaultColWidth
    view = ws.sheet_view
    target.sheet_view.showGridLines = view.showGridLines
    target.sheet_view.zoomScale = view.zoomScale
    target.sheet_view.zoomScaleNormal = view.zoomScaleNormal
    target.sheet_view.view = view.view
    target.sheet_view.zoomScalePageLayoutView = view.zoomScalePageLayoutView
    source_footer = ws.HeaderFooter
    target_footer = target.HeaderFooter
    target_footer.alignWithMargins = source_footer.alignWithMargins
    target_footer.differentFirst = source_footer.differentFirst
    target_footer.differentOddEven = source_footer.differentOddEven
    target_footer.scaleWithDoc = source_footer.scaleWithDoc
    for part in ("oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter"):
        source_header = getattr(ws, part)
        copied = getattr(target, part)
        for side in ("left", "center", "right"):
            getattr(copied, side).text = getattr(source_header, side).text
