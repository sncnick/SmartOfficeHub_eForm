"""CDD platform: Excel is imported once into a document database, then edited there."""

from __future__ import annotations

import datetime as dt
import json
import logging
import mimetypes
import os
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import quote
from zipfile import BadZipFile

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from openpyxl.utils.exceptions import InvalidFileException
from starlette.concurrency import run_in_threadpool

from .documents import add_party, add_record, blank_company, ensure_party_ids, set_outdated, set_value
from .excel_fields import suggest_fields
from .excel_parser import (
    DEFAULT_SHEET,
    ParseError,
    clip_to_print_area,
    filled_copy,
    parse_workbook,
    preview_sheet,
    release_preview,
)
from .form_library import delete_form, form_path, list_forms, save_form
from .form_pages import split_workbook_bytes
from .word_layout import WordLayoutError, blank_gaps, clip_gaps, collect_fields, collect_ticks, layout_docx, page_png, place_fields
from .word_preview import WordPreviewError, preview_docx
from .pdf_preview import PdfPreviewError, page_png as pdf_page_png, preview_pdf
from .pdf_print import compose_pdf, recall as recall_pdf_print, remember as remember_pdf_print
from .lmstudio import LmStudioError, analyze_sheet, lmstudio_base, status as lmstudio_status
from .store import get_store

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = Path(os.environ.get("CDD_OUTPUT_DIR", BASE_DIR.parent / "output")).resolve()
DEFAULT_FILE = os.environ.get("CDD_DEFAULT_FILE", "")
SHEET = os.environ.get("CDD_SHEET", DEFAULT_SHEET)
OUTDATED_FILLS = [c.strip() for c in os.environ.get("CDD_OUTDATED_FILLS", "00B0F0").split(",") if c.strip()]
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
WORD_SUFFIXES = {".docx"}
PDF_SUFFIXES = {".pdf"}
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_UPLOADS_KEPT = 5
READ_ERRORS = (ParseError, InvalidFileException, BadZipFile, KeyError, ValueError, OSError)

log = logging.getLogger("cdd_viewer")
app = FastAPI(title="CDD Viewer")
_lock = threading.Lock()


@app.middleware("http")
async def no_cache_pages(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response
_cache: dict[Path, tuple[float, dict]] = {}
_grid_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_uploads: dict[str, dict] = {}


def _stored(suffixes: set[str]) -> list[Path]:
    return [path for path in list_forms() if path.suffix.lower() in suffixes]


def _resolve_stored(name: str, suffixes: set[str], missing: str) -> Path:
    path = form_path(name)
    if path.suffix.lower() not in suffixes or not path.is_file():
        raise HTTPException(404, missing)
    return path


def _resolve(name: str) -> Path:
    return _resolve_stored(name, EXCEL_SUFFIXES, "找不到這個 Excel 檔")


def _parse(source, file_name: str) -> dict:
    try:
        return parse_workbook(source, sheet=SHEET, outdated_fills=OUTDATED_FILLS, file_name=file_name)
    except ParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except READ_ERRORS as exc:
        log.warning("cannot read %s: %s", file_name, exc)
        raise HTTPException(422, "無法讀取這個 Excel 檔") from exc


def _write_output(path: Path, data: dict) -> None:
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        target = OUTPUT_DIR / f"{path.stem}.json"
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("cannot write JSON output for %s: %s", path.name, exc)


def _file_data(name: str) -> dict:
    path = _resolve(name)
    mtime = path.stat().st_mtime
    with _lock:
        cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    data = _parse(path, path.name)
    with _lock:
        _cache[path] = (mtime, data)
    _write_output(path, data)
    return data


def _upload_data(upload_id: str) -> dict:
    with _lock:
        data = _uploads.get(upload_id)
    if data is None:
        raise HTTPException(404, "上載的資料已過期，請重新上載")
    return data


def _select(file: str | None, upload: str | None) -> dict:
    if upload:
        return _upload_data(upload)
    if file:
        return _file_data(file)
    raise HTTPException(400, "請指定 file 或 upload")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _import_parsed(data: dict, file_name: str, replace: bool) -> dict:
    store = get_store()
    if store.count() and not replace:
        raise HTTPException(409, "資料庫已有公司。再匯入會取代現有資料，請確認後再執行。")
    docs = []
    now = _now()
    for company in data.get("companies") or []:
        ensure_party_ids(company)
        company["imported_from"] = file_name
        company["created_at"] = now
        company["updated_at"] = now
        docs.append(company)
    source = data.get("source") or {}
    meta = {
        "file": file_name,
        "imported_at": source.get("parsed_at") or now,
        "sheet": source.get("sheet") or "",
        "header_rows": source.get("header_rows") or [],
        "sheet_issues": data.get("issues") or [],
        "count": len(docs),
    }
    store.replace_all(docs, meta)
    return {"imported": len(docs), "meta": meta}


def _database_payload() -> dict:
    store = get_store()
    meta = store.get_meta()
    companies = store.all_docs()
    for company in companies:
        if ensure_party_ids(company):
            store.save(company)
    sheet_issues = list(meta.get("sheet_issues") or [])
    company_issues = [issue for company in companies for issue in (company.get("issues") or [])]
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in sheet_issues + company_issues:
        severity = issue.get("severity")
        if severity in counts:
            counts[severity] += 1
    return {
        "source": {
            "file": meta.get("file") or "",
            "sheet": meta.get("sheet") or "",
            "header_rows": meta.get("header_rows") or [],
            "parsed_at": meta.get("imported_at") or _now(),
        },
        "summary": {"companies": len(companies), "issues": counts},
        "issues": sheet_issues,
        "companies": companies,
    }


@app.get("/api/health")
def health() -> dict:
    try:
        get_store().ping()
    except Exception as exc:
        log.warning("database is not ready: %s", exc)
        raise HTTPException(503, "資料庫未就緒") from exc
    return {"status": "ok"}


@app.get("/api/database")
def database() -> dict:
    return _database_payload()


@app.post("/api/companies")
def create_company() -> dict:
    """Add an empty company. It is not taken from Excel."""
    store = get_store()
    company = blank_company()
    while store.get(company["id"]) is not None:
        company = blank_company()
    company["created_at"] = company["updated_at"] = _now()
    return store.save(company)


@app.post("/api/companies/{company_id}/parties")
def create_party(company_id: str, payload: dict = Body(...)) -> dict:
    store = get_store()
    company = store.get(company_id)
    if company is None:
        raise HTTPException(404, "找不到這間公司")
    try:
        updated = add_party(company, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    updated["updated_at"] = _now()
    return store.save(updated)


@app.post("/api/companies/{company_id}/records")
def create_record(company_id: str, payload: dict = Body(...)) -> dict:
    store = get_store()
    company = store.get(company_id)
    if company is None:
        raise HTTPException(404, "找不到這間公司")
    try:
        updated = add_record(company, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    updated["updated_at"] = _now()
    return store.save(updated)


@app.post("/api/companies/{company_id}/field")
def update_field(company_id: str, payload: dict = Body(...)) -> dict:
    store = get_store()
    company = store.get(company_id)
    if company is None:
        raise HTTPException(404, "找不到這間公司")
    try:
        updated = set_value(company, payload.get("path"), payload.get("value"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    updated["updated_at"] = _now()
    return store.save(updated)


@app.post("/api/companies/{company_id}/outdated")
def mark_outdated(company_id: str, payload: dict = Body(...)) -> dict:
    store = get_store()
    company = store.get(company_id)
    if company is None:
        raise HTTPException(404, "找不到這間公司")
    try:
        updated = set_outdated(company, str(payload.get("party_id") or ""), bool(payload.get("outdated")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    updated["updated_at"] = _now()
    return store.save(updated)


@app.post("/api/import")
async def import_file(file: str, replace: bool = False) -> dict:
    path = _resolve(file)
    data = await run_in_threadpool(_parse, path, path.name)
    return _import_parsed(data, path.name, replace)


@app.post("/api/import/upload")
async def import_upload(replace: bool = False, file: UploadFile = File(...)) -> dict:
    name = Path(file.filename or "").name
    if Path(name).suffix.lower() not in EXCEL_SUFFIXES:
        raise HTTPException(400, "只接受 .xlsx 或 .xlsm 檔")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "檔案超過 30 MB")
    saved = await run_in_threadpool(save_form, name, content)
    data = await run_in_threadpool(_parse, saved, saved.name)
    return _import_parsed(data, saved.name, replace)


@app.get("/api/export")
def export_db() -> Response:
    payload = _database_payload()
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''cdd-database.json"},
    )


def _cached_preview(path: Path, sheet: str | None) -> dict:
    mtime = path.stat().st_mtime
    key = (str(path), sheet or "")
    with _lock:
        cached = _grid_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    data = preview_sheet(path, sheet)
    with _lock:
        _grid_cache[key] = (mtime, data)
    return data


@app.get("/api/excel/fields")
def excel_field_choices(q: str = "", owners: str = "") -> dict:
    """Searchable fields for a ``[[]]`` cell. This does not read the workbook."""
    chosen = [part.strip() for part in owners.split(",") if part.strip()]
    return {"fields": suggest_fields(q, chosen)}


@app.get("/api/excel/grid")
def excel_grid(file: str, sheet: str | None = None, start: int = 1, limit: int = 60,
               within_print: bool = False) -> dict:
    """One page of a workbook for the side reader. This does not change the database."""
    path = _resolve(file)
    limit = max(1, min(int(limit), 8000 if within_print else 200))
    start = max(1, int(start))
    try:
        grid = _cached_preview(path, sheet or None)
    except ParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except READ_ERRORS as exc:
        log.warning("cannot preview %s: %s", path.name, exc)
        raise HTTPException(422, "無法讀取這個 Excel 檔") from exc
    clipped = clip_to_print_area(grid) if within_print else None
    if clipped:
        numbered = clipped["rows"]
        columns = clipped["columns"]
        widths = clipped["widths"]
        all_merges = clipped["merges"]
    else:
        numbered = [{"n": index, **item} for index, item in enumerate(grid["rows"], start=1)]
        columns = grid["columns"]
        widths = grid.get("widths") or []
        all_merges = grid.get("merges") or []
    page = numbered[start - 1:start - 1 + limit]
    visible_rows = {item["n"] for item in page}
    column_count = len(columns)
    merges = []
    if not visible_rows:
        all_merges = []
    for merge in all_merges:
        last_row = merge["r"] + merge["rs"] - 1
        last_col = merge["c"] + merge["cs"] - 1
        if last_row < min(visible_rows, default=0) or merge["r"] > max(visible_rows, default=0):
            continue
        if last_col < 1 or merge["c"] > column_count:
            continue
        merges.append(merge)
    return {
        "file": file,
        "sheet": grid["sheet"],
        "sheets": grid["sheets"],
        "columns": columns,
        "widths": widths,
        "print_area": bool(clipped),
        "default_font_size": grid.get("default_font_size") or 11,
        "default_row_height": grid.get("default_row_height") or 20,
        "gridlines": grid.get("gridlines", True),
        "page": grid.get("page") or {},
        "merges": merges,
        "row_count": len(numbered),
        "start": start,
        "truncated_rows": grid["truncated_rows"],
        "truncated_columns": grid["truncated_columns"],
        "rows": page,
    }


@app.get("/api/lmstudio")
def lmstudio(url: str = "") -> dict:
    """Report whether the local LM Studio server has a model loaded."""
    try:
        return lmstudio_status(url or None)
    except LmStudioError as exc:
        tried = ""
        try:
            tried = lmstudio_base(url or None)
        except LmStudioError:
            tried = url
        return {"ok": False, "models": [], "url": tried, "detail": str(exc)}


@app.post("/api/excel/lmstudio")
def excel_lmstudio(payload: dict = Body(...)) -> dict:
    """Classify the current sheet. The file is not changed and company records are not sent."""
    file_name = str(payload.get("file") or "")
    sheet = payload.get("sheet") or None
    path = _resolve(file_name)
    try:
        grid = _cached_preview(path, sheet)
        found = analyze_sheet(grid, payload.get("url") or None, payload.get("model") or None)
    except LmStudioError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except READ_ERRORS as exc:
        log.warning("cannot analyze workbook: %s", type(exc).__name__)
        raise HTTPException(422, "無法讀取這個 Excel 檔") from exc
    return {"file": file_name, "sheet": grid.get("sheet") or "", **found}


@app.get("/api/excel/split-pages")
def excel_split_pages(file: str) -> Response:
    """Download one workbook whose sheets are the printed pages of ``file``."""
    path = _resolve(file)
    try:
        content = split_workbook_bytes(path)
    except ParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except READ_ERRORS as exc:
        log.warning("cannot split workbook: %s", type(exc).__name__)
        raise HTTPException(422, "無法依列印範圍拆分這個 Excel 檔") from exc
    name = f"{path.stem}_form.xlsx"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


def _overlaps(left: dict, right: dict) -> bool:
    return (
        left["x"] < right["x"] + right["w"]
        and right["x"] < left["x"] + left["w"]
        and left["y"] < right["y"] + right["h"]
        and right["y"] < left["y"] + left["h"]
    )


def _resolve_word(name: str) -> Path:
    return _resolve_stored(name, WORD_SUFFIXES, "找不到這個 Word 檔")


@app.get("/api/word/preview")
def word_preview(file: str) -> dict:
    """Preview a Word form as laid-out pages. Fields keep their parameter name."""
    path = _resolve_word(file)
    parsed = {"pages": []}
    try:
        parsed = preview_docx(path)
    except WordPreviewError as exc:
        log.warning("cannot parse docx fields: %s", type(exc).__name__)
    except (OSError, ValueError, KeyError) as exc:
        log.warning("cannot parse docx fields: %s", type(exc).__name__)
    try:
        laid = layout_docx(path)
        pages, unplaced = place_fields(collect_fields(parsed), laid["pages"])
        gaps = blank_gaps(laid["pages"])
        enters = {item["n"]: item.get("enters") or [] for item in laid["pages"]}
        cells = {item["n"]: item.get("cells") or [] for item in laid["pages"]}
        for page in pages:
            have = page["fields"]
            incoming = enters.get(page["n"], [])
            have.extend(item for item in incoming if item.get("table"))
            for gap in clip_gaps(gaps.get(page["n"], []), cells.get(page["n"], [])):
                if not any(_overlaps(gap, item) for item in have):
                    have.append(gap)
            have.extend(item for item in incoming if not item.get("table"))
        ticks = collect_ticks(laid["pages"])
        for page in pages:
            page["ticks"] = ticks.get(page["n"], [])
    except WordLayoutError as exc:
        log.warning("cannot draw docx: %s", type(exc).__name__)
        raise HTTPException(422, str(exc)) from exc
    return {
        "file": file,
        "kind": "docx",
        "render": "page",
        "page_count": len(pages),
        "pages": pages,
        "unplaced": unplaced,
        "asset": laid["asset"],
    }


@app.get("/api/word/page")
def word_page(file: str, n: int = 1) -> Response:
    """One finished page image. The picture is the Word layout, not a rebuilt table."""
    path = _resolve_word(file)
    try:
        png = page_png(path, n)
    except WordLayoutError as exc:
        log.warning("cannot draw docx page: %s", type(exc).__name__)
        raise HTTPException(422, "無法按 Word 版面繪製這份文件") from exc
    except OSError as exc:
        log.warning("cannot read docx: %s", type(exc).__name__)
        raise HTTPException(422, "無法讀取這個 Word 檔") from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


def _resolve_pdf(name: str) -> Path:
    return _resolve_stored(name, PDF_SUFFIXES, "找不到這個 PDF 檔")


@app.get("/api/pdf/preview")
def pdf_preview(file: str, response: Response) -> dict:
    """Preview a PDF, with one field for each box the form lets you fill."""
    response.headers["Cache-Control"] = "no-store"
    path = _resolve_pdf(file)
    try:
        laid = preview_pdf(path)
    except PdfPreviewError as exc:
        log.warning("cannot preview pdf: %s", type(exc).__name__)
        raise HTTPException(422, str(exc)) from exc
    except (OSError, ValueError) as exc:
        log.warning("cannot read pdf: %s", type(exc).__name__)
        raise HTTPException(422, "無法讀取這個 PDF") from exc
    laid["file"] = file
    return laid


@app.get("/api/pdf/page")
def pdf_page(file: str, n: int = 1) -> Response:
    """One PDF page image."""
    path = _resolve_pdf(file)
    try:
        png = pdf_page_png(path, n)
    except PdfPreviewError as exc:
        log.warning("cannot draw pdf page: %s", type(exc).__name__)
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        log.warning("cannot read pdf: %s", type(exc).__name__)
        raise HTTPException(422, "無法讀取這個 PDF") from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/pdf/file")
def pdf_file(file: str) -> FileResponse:
    """The original PDF, so the browser can draw it without a rebuilt layout."""
    path = _resolve_pdf(file)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "no-store"},
    )


_FORM_SUFFIXES = {"pdf": {".pdf"}, "word": {".docx"}, "excel": {".xlsx", ".xlsm"}}
_WORD_FIELD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,60}\Z")
_EXCEL_COLUMN = re.compile(r"[A-Z]{1,3}\Z")


def _form_mapping_name(name: str, kind: str) -> str:
    cleaned = Path(str(name or "")).name.strip()
    suffix = Path(cleaned).suffix.lower()
    if not cleaned or cleaned != str(name or "").strip() or suffix not in _FORM_SUFFIXES[kind]:
        raise HTTPException(400, "請指定檔案")
    if cleaned.startswith("~$") or len(cleaned) > 180:
        raise HTTPException(400, "請指定檔案")
    return cleaned


def _pdf_mapping_name(name: str) -> str:
    return _form_mapping_name(name, "pdf")


def _pdf_field_id(ident: str) -> bool:
    if not ident.startswith("pdf-") or not 5 < len(ident) <= 120:
        return False
    return all(char.isalnum() or char in "._-" for char in ident[4:])


def _excel_cell_id(ident: str) -> bool:
    sheet, sep, rest = ident.partition("\t")
    row, sep2, col = rest.partition("\t")
    if not sep or not sep2 or "\t" in col:
        return False
    if not sheet or len(sheet) > 31 or any(char in sheet for char in "\\/?*[]\n\r"):
        return False
    if not row.isdigit() or not 1 <= int(row) <= 200000:
        return False
    return bool(_EXCEL_COLUMN.fullmatch(col))


def _clean_field(value: dict) -> dict | None:
    if value.get("lock") is True and not str(value.get("key") or "").strip():
        return {"lock": True}
    item = {
        "key": str(value.get("key") or "")[:80],
        "label": str(value.get("label") or "")[:160],
        "kind": str(value.get("kind") or "")[:20],
        "group": str(value.get("group") or "")[:40],
        "options": [],
    }
    if item["kind"] == "free":
        item["text"] = str(value.get("text") or "")[:500]
    options = value.get("options")
    if isinstance(options, list):
        for option in options[:12]:
            if not isinstance(option, dict):
                continue
            item["options"].append({
                "value": str(option.get("value") or "")[:40],
                "label": str(option.get("label") or "")[:80],
            })
    if value.get("lock") is True:
        item["lock"] = True
    option = str(value.get("option") or "")[:40]
    if option:
        item["option"] = option
    return item if item["key"] else None


def _clean_field_maps(raw, ident_ok) -> dict:
    if not isinstance(raw, dict):
        return {}
    kept = {}
    for key, value in list(raw.items())[:2000]:
        ident = str(key)
        if not ident_ok(ident) or not isinstance(value, dict):
            continue
        item = _clean_field(value)
        if item:
            kept[ident] = item
    return kept


def _clean_flag_map(raw, ident_ok) -> dict:
    if not isinstance(raw, dict):
        return {}
    kept = {}
    for key, value in list(raw.items())[:2000]:
        ident = str(key)
        if ident_ok(ident) and value:
            kept[ident] = True
    return kept


def _bounded_int(value, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _clean_excel_mark(mark) -> dict | None:
    if not isinstance(mark, dict):
        return None
    scope = str(mark.get("scope") or "")
    if scope not in {"cell", "part", "lock"}:
        return None
    item: dict = {"scope": scope}
    if scope == "part":
        item["start"] = _bounded_int(mark.get("start"), 0, 20000)
        item["end"] = _bounded_int(mark.get("end"), 0, 20000)
        item["excerpt"] = str(mark.get("excerpt") or "")[:400]
    if scope == "lock":
        return item
    field = _clean_field(mark["field"]) if isinstance(mark.get("field"), dict) else None
    item["field"] = field if field and "key" in field else None
    fields = []
    if isinstance(mark.get("fields"), list):
        for field_item in mark["fields"][:12]:
            if not isinstance(field_item, dict):
                continue
            cleaned = _clean_field(field_item)
            if cleaned and "key" in cleaned:
                fields.append(cleaned)
    if fields:
        item["fields"] = fields
    return item


def _clean_excel_marks(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    kept = {}
    for key, value in list(raw.items())[:2000]:
        ident = str(key)
        if not _excel_cell_id(ident) or not isinstance(value, list):
            continue
        marks = [cleaned for mark in value[:40] if (cleaned := _clean_excel_mark(mark))]
        if marks:
            kept[ident] = marks
    return kept


def _clean_pdf_maps(raw) -> dict:
    return _clean_field_maps(raw, _pdf_field_id)


def _clean_pdf_ticks(raw) -> dict:
    return _clean_flag_map(raw, _pdf_field_id)


@app.get("/api/pdf/mappings")
def pdf_mappings(file: str) -> dict:
    """PDF field mappings stored in MongoDB, separate from company records."""
    name = _pdf_mapping_name(file)
    found = get_store().get_pdf_mapping(name) or {}
    return {"file": name, "maps": found.get("maps") or {}, "ticks": found.get("ticks") or {}}


@app.put("/api/pdf/mappings")
def save_pdf_mappings(payload: dict = Body(...)) -> dict:
    """Replace one PDF's mappings. Other files and the company database stay."""
    name = _pdf_mapping_name(str(payload.get("file") or ""))
    saved = get_store().save_pdf_mapping(
        name,
        _clean_pdf_maps(payload.get("maps")),
        _clean_pdf_ticks(payload.get("ticks")),
        _now(),
    )
    return {"file": name, "maps": saved["maps"], "ticks": saved["ticks"]}


@app.get("/api/word/mappings")
def word_mappings(file: str) -> dict:
    """Word field mappings stored in MongoDB, separate from company records."""
    name = _form_mapping_name(file, "word")
    found = get_store().get_form_mapping("word", name) or {}
    return {"file": name, "maps": found.get("maps") or {}, "ticks": found.get("ticks") or {}}


@app.put("/api/word/mappings")
def save_word_mappings(payload: dict = Body(...)) -> dict:
    """Replace one Word file's mappings. Other files and the company database stay."""
    name = _form_mapping_name(str(payload.get("file") or ""), "word")
    saved = get_store().save_form_mapping(
        "word",
        name,
        {
            "maps": _clean_field_maps(payload.get("maps"), lambda ident: bool(_WORD_FIELD_ID.fullmatch(ident))),
            "ticks": _clean_flag_map(payload.get("ticks"), lambda ident: bool(_WORD_FIELD_ID.fullmatch(ident))),
        },
        _now(),
    )
    return {"file": name, "maps": saved["maps"], "ticks": saved["ticks"]}


@app.get("/api/excel/mappings")
def excel_mappings(file: str) -> dict:
    """Excel cell mappings stored in MongoDB, separate from company records."""
    name = _form_mapping_name(file, "excel")
    found = get_store().get_form_mapping("excel", name) or {}
    return {"file": name, "maps": found.get("maps") or {}, "marks": found.get("marks") or {}}


@app.put("/api/excel/mappings")
def save_excel_mappings(payload: dict = Body(...)) -> dict:
    """Replace one workbook's mappings. Other files and the company database stay."""
    name = _form_mapping_name(str(payload.get("file") or ""), "excel")
    saved = get_store().save_form_mapping(
        "excel",
        name,
        {
            "maps": _clean_field_maps(payload.get("maps"), _excel_cell_id),
            "marks": _clean_excel_marks(payload.get("marks")),
        },
        _now(),
    )
    return {"file": name, "maps": saved["maps"], "marks": saved["marks"]}


@app.post("/api/pdf/print")
async def pdf_print(payload: dict = Body(...)) -> dict:
    """Build the original PDF, with typed values, for the browser PDF print."""
    path = _resolve_pdf(str(payload.get("file") or ""))
    fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
    try:
        pdf = await run_in_threadpool(compose_pdf, path, fields)
    except PdfPreviewError as exc:
        raise HTTPException(422, "無法準備這份 PDF") from exc
    except OSError as exc:
        raise HTTPException(422, "無法準備這份 PDF") from exc
    return {"token": remember_pdf_print(pdf)}


@app.post("/api/excel/filled")
async def excel_filled(payload: dict = Body(...)) -> Response:
    """Return a copy of the workbook with the filled text written in. The source file is not changed."""
    path = _resolve(str(payload.get("file") or ""))
    cells = payload.get("cells") if isinstance(payload.get("cells"), list) else []
    try:
        data = await run_in_threadpool(filled_copy, path, cells)
    except (OSError, BadZipFile, InvalidFileException) as exc:
        raise HTTPException(422, "無法準備這份填好的 Excel") from exc
    name = f"{path.stem}-filled.xlsx"
    disposition = "attachment; filename*=UTF-8''" + quote(name)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
    )


@app.get("/api/pdf/print/{token}")
def pdf_print_file(token: str) -> Response:
    pdf = recall_pdf_print(token)
    if pdf is None:
        raise HTTPException(404, "這次列印已經過期，請再按一次列印表格")
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=form.pdf", "Cache-Control": "no-store"},
    )


def _forget_stored(path: Path) -> None:
    release_preview(path)
    with _lock:
        _cache.pop(path, None)
        dead = [key for key in _grid_cache if key[0] == str(path)]
        for key in dead:
            _grid_cache.pop(key, None)


@app.get("/api/files")
def list_files() -> dict:
    files = _stored(EXCEL_SUFFIXES)
    preview = _stored(EXCEL_SUFFIXES | WORD_SUFFIXES | PDF_SUFFIXES)
    names = [path.name for path in files]
    default = DEFAULT_FILE if DEFAULT_FILE in names else (names[0] if names else None)
    return {
        "files": [{"name": path.name, "size": path.stat().st_size} for path in files],
        "preview_files": [
            {"name": path.name, "size": path.stat().st_size, "kind": path.suffix.lower().lstrip(".")}
            for path in preview
        ],
        "default": default,
    }


@app.post("/api/forms")
async def upload_form(file: UploadFile = File(...)) -> dict:
    """Keep an Excel, PDF, or Word file on this machine. The original folder is not used."""
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "檔案超過 30 MB")
    saved = await run_in_threadpool(save_form, file.filename or "", content)
    return {"name": saved.name}


@app.delete("/api/forms")
def remove_form(name: str) -> dict:
    """Remove one uploaded file. Company records already in the database stay."""
    removed = delete_form(name)
    _forget_stored(removed)
    return {"deleted": removed.name}


@app.get("/api/data")
def get_data(file: str | None = None, upload: str | None = None) -> dict:
    return _select(file, upload)


@app.get("/api/download")
def download(file: str | None = None, upload: str | None = None) -> Response:
    data = _select(file, upload)
    name = f"{Path(data['source']['file'] or 'cdd').stem}.json"
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)) -> dict:
    name = Path(file.filename or "upload.xlsx").name
    if Path(name).suffix.lower() not in EXCEL_SUFFIXES:
        raise HTTPException(400, "只接受 .xlsx 或 .xlsm 檔")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "檔案超過 30 MB")
    data = await run_in_threadpool(_parse, content, name)
    upload_id = uuid.uuid4().hex
    data["source"]["upload_id"] = upload_id
    with _lock:
        _uploads[upload_id] = data
        while len(_uploads) > MAX_UPLOADS_KEPT:
            _uploads.pop(next(iter(_uploads)))
    return data


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


mimetypes.add_type("text/javascript", ".mjs")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
