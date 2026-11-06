"""Files people upload for Excel and PDF preview. Nothing is read from a shared folder."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import HTTPException

ALLOWED_SUFFIXES = {".xlsx", ".xlsm", ".pdf", ".docx"}
MAX_FORM_BYTES = 30 * 1024 * 1024


def forms_dir() -> Path:
    configured = os.environ.get("CDD_FORMS_DIR", "").strip()
    path = Path(configured).expanduser() if configured else Path(__file__).resolve().parent.parent / "forms"
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(filename: str) -> str:
    name = Path((filename or "").replace("\\", "/")).name.strip()
    if not name or name in {".", ".."} or name.startswith("~$") or "\x00" in name:
        raise HTTPException(400, "檔名不正確")
    if len(name) > 180 or Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(400, "只接受 Excel、PDF 或 Word")
    return name


def form_path(filename: str) -> Path:
    root = forms_dir()
    path = (root / safe_name(filename)).resolve()
    if path.parent != root or path.is_symlink():
        raise HTTPException(400, "檔名不正確")
    return path


def list_forms() -> list[Path]:
    root = forms_dir()
    found = [
        path for path in root.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and not path.name.startswith((".", "~$"))
        and path.suffix.lower() in ALLOWED_SUFFIXES
    ]
    return sorted(found, key=lambda item: item.name.casefold())


def save_form(filename: str, content: bytes) -> Path:
    if len(content) > MAX_FORM_BYTES:
        raise HTTPException(413, "檔案超過 30 MB")
    path = form_path(filename)
    suffix = path.suffix.lower()
    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise HTTPException(400, "這不是 PDF")
    if suffix in {".xlsx", ".xlsm", ".docx"} and not content.startswith(b"PK"):
        raise HTTPException(400, "這不是有效的 Excel 或 Word")
    if path.exists():
        raise HTTPException(409, "已有同名檔案，請先刪除")
    temporary = path.parent / f".{uuid.uuid4().hex}.part"
    temporary.write_bytes(content)
    try:
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


def delete_form(filename: str) -> Path:
    path = form_path(filename)
    if not path.is_file():
        raise HTTPException(404, "找不到這個檔案")
    path.unlink()
    return path
