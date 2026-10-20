"""CDD platform: Excel is imported once into a document database, then edited there."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
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

from .documents import add_party, add_record, ensure_party_ids, set_outdated, set_value
from .excel_fields import suggest_fields
from .excel_parser import DEFAULT_SHEET, ParseError, clip_to_print_area, parse_workbook, preview_sheet
from .store import get_store

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("CDD_DATA_DIR", BASE_DIR.parent / "data")).resolve()
OUTPUT_DIR = Path(os.environ.get("CDD_OUTPUT_DIR", BASE_DIR.parent / "output")).resolve()
DEFAULT_FILE = os.environ.get("CDD_DEFAULT_FILE", "")
SHEET = os.environ.get("CDD_SHEET", DEFAULT_SHEET)
OUTDATED_FILLS = [c.strip() for c in os.environ.get("CDD_OUTDATED_FILLS", "00B0F0").split(",") if c.strip()]
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
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


def _excel_files() -> list[Path]:
    if not DATA_DIR.is_dir():
        return []
    return sorted(p for p in DATA_DIR.rglob("*")
                  if p.is_file() and p.suffix.lower() in EXCEL_SUFFIXES and not p.name.startswith("~$"))


def _resolve(name: str) -> Path:
    path = (DATA_DIR / name).resolve()
    if DATA_DIR not in path.parents or path.suffix.lower() not in EXCEL_SUFFIXES or not path.is_file():
        raise HTTPException(404, "找不到這個 Excel 檔")
    return path


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
    data = _parse(path, str(path.relative_to(DATA_DIR)))
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
    name = Path(file.filename or "upload.xlsx").name
    if Path(name).suffix.lower() not in EXCEL_SUFFIXES:
        raise HTTPException(400, "只接受 .xlsx 或 .xlsm 檔")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "檔案超過 30 MB")
    data = await run_in_threadpool(_parse, content, name)
    return _import_parsed(data, name, replace)


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


@app.get("/api/files")
def list_files() -> dict:
    files = _excel_files()
    names = [str(p.relative_to(DATA_DIR)) for p in files]
    default = DEFAULT_FILE if DEFAULT_FILE in names else (names[0] if names else None)
    return {
        "files": [{"name": name, "size": path.stat().st_size} for name, path in zip(names, files)],
        "default": default,
    }


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


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
