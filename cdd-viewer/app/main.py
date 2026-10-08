"""Web viewer for the CDD workbook: JSON API plus a static form-style page."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path
from urllib.parse import quote
from zipfile import BadZipFile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from openpyxl.utils.exceptions import InvalidFileException
from starlette.concurrency import run_in_threadpool

from .excel_parser import DEFAULT_SHEET, ParseError, parse_workbook

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


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


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
