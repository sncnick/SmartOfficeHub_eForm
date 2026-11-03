"""Show a PDF the way it prints, and expose its fillable fields.

The page picture comes from the same renderer used for Word. AcroForm
widgets become clickable fields on that picture so a value can be mapped
onto the box the reader is meant to fill.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}
_PUSHBUTTON = 1 << 16


class PdfPreviewError(Exception):
    """The PDF cannot be shown."""


def preview_pdf(source: str | Path | bytes) -> dict:
    key, raw = _identity(source)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            cached = _read(raw)
            cached["raw"] = raw
            cached["pngs"] = {}
            _CACHE.clear()
            _CACHE[key] = cached
        pages = cached["pages"]
        asset = cached["asset"]
    return {
        "file": Path(source).name if not isinstance(source, bytes) else "",
        "kind": "pdf",
        "render": "page",
        "page_count": len(pages),
        "pages": pages,
        "unplaced": [],
        "asset": asset,
    }


def page_png(source: str | Path | bytes, number: int) -> bytes:
    key, raw = _identity(source)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            cached = _read(raw)
            cached["raw"] = raw
            cached["pngs"] = {}
            _CACHE.clear()
            _CACHE[key] = cached
        stored = cached["pngs"].get(number)
        if stored is None:
            if not any(page["n"] == number for page in cached["pages"]):
                raise PdfPreviewError("沒有這一頁")
            stored = _render_page(cached["raw"], number)
            cached["pngs"][number] = stored
        return stored


def _read(raw: bytes) -> dict:
    if raw[:5] != b"%PDF-":
        raise PdfPreviewError("這不是 PDF")
    try:
        reader = PdfReader(stream=_stream(raw), strict=False)
    except PdfReadError as exc:
        raise PdfPreviewError("無法讀取這個 PDF") from exc
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("")
        except Exception as exc:
            raise PdfPreviewError("這份 PDF 有密碼，無法預覽") from exc
        if opened == 0:
            raise PdfPreviewError("這份 PDF 有密碼，無法預覽")
    if not reader.pages:
        raise PdfPreviewError("這份 PDF 沒有頁面")
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        crop_w, crop_h, rotation = _crop(page)
        width, height = _css_size(crop_w, crop_h, rotation)
        pages.append({
            "n": index,
            "width": width,
            "height": height,
            "fields": _widgets(page, index, crop_w, crop_h, rotation),
            "ticks": [],
        })
    return {"pages": pages, "asset": hashlib.sha256(raw).hexdigest()[-16:]}


def _widgets(page, number: int, crop_w: float, crop_h: float, rotation: int) -> list[dict]:
    found = []
    annots = page.get("/Annots") or []
    for annot in annots:
        try:
            obj = annot.get_object()
        except Exception:
            continue
        if str(obj.get("/Subtype") or "") != "/Widget":
            continue
        kind = str(_inherited(obj, "/FT") or "")
        flags = _int(_inherited(obj, "/Ff"))
        if kind == "/Btn" and flags & _PUSHBUTTON:
            continue
        rect = obj.get("/Rect")
        if rect is None or len(rect) < 4:
            continue
        box = _display_box(
            [float(item) for item in rect[:4]],
            _origin(page),
            crop_w,
            crop_h,
            rotation,
        )
        if box is None:
            continue
        x, y, w, h = (_px(box[0]), _px(box[1]), _px(box[2]), _px(box[3]))
        page_w, page_h = _css_size(crop_w, crop_h, rotation)
        x = max(0, min(x, page_w - 1))
        y = max(0, min(y, page_h - 1))
        w = max(0, min(w, page_w - x))
        h = max(0, min(h, page_h - y))
        if w < 4 or h < 4:
            continue
        name = _field_name(obj) or "欄位"
        found.append({
            "id": f"pdf-{number}-{len(found) + 1}",
            "name": name,
            "text": "",
            "control": "checkbox" if kind == "/Btn" else "text",
            "checked": False,
            "blank": True,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
        })
    return found


def _field_name(obj) -> str:
    parts = []
    seen: set[int] = set()
    current = obj
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        label = current.get("/T")
        if label:
            parts.append(str(label))
        parent = current.get("/Parent")
        if parent is None:
            break
        try:
            current = parent.get_object()
        except Exception:
            break
    return ".".join(reversed(parts))


def _inherited(obj, key: str):
    seen: set[int] = set()
    current = obj
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if key in current:
            return current.get(key)
        parent = current.get("/Parent")
        if parent is None:
            return None
        try:
            current = parent.get_object()
        except Exception:
            return None
    return None


def _origin(page) -> tuple[float, float]:
    box = page.cropbox
    return float(box.left), float(box.bottom)


def _crop(page) -> tuple[float, float, int]:
    box = page.cropbox
    return float(box.width), float(box.height), int(page.rotation or 0) % 360


def _css_size(crop_w: float, crop_h: float, rotation: int) -> tuple[int, int]:
    if rotation in {90, 270}:
        crop_w, crop_h = crop_h, crop_w
    return _px(crop_w), _px(crop_h)


def _display_box(rect: list[float], origin: tuple[float, float], crop_w: float, crop_h: float, rotation: int) -> tuple[float, float, float, float] | None:
    x0, y0, x1, y1 = rect
    left, right = sorted((x0, x1))
    bottom, top = sorted((y0, y1))
    ox, oy = origin
    corners = (
        (left - ox, bottom - oy),
        (right - ox, bottom - oy),
        (left - ox, top - oy),
        (right - ox, top - oy),
    )
    mapped = [_to_top_left(x, y, crop_w, crop_h, rotation) for x, y in corners]
    xs = [item[0] for item in mapped]
    ys = [item[1] for item in mapped]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if width < 1 or height < 1:
        return None
    return min(xs), min(ys), width, height


def _to_top_left(x: float, y: float, crop_w: float, crop_h: float, rotation: int) -> tuple[float, float]:
    """User-space point, from the crop box lower left, as displayed top-left."""
    if rotation == 90:
        return y, x
    if rotation == 180:
        return crop_w - x, y
    if rotation == 270:
        return crop_h - y, crop_w - x
    return x, crop_h - y


def _render_page(raw: bytes, number: int) -> bytes:
    tool = shutil.which("pdftoppm")
    if tool is None:
        raise PdfPreviewError("這部電腦沒有 PDF 繪圖工具")
    with tempfile.TemporaryDirectory(prefix="cdd-pdf-") as folder:
        root = Path(folder)
        pdf = root / "source.pdf"
        pdf.write_bytes(raw)
        subprocess.run(
            [tool, "-png", "-r", "144", "-cropbox", "-f", str(number), "-l", str(number), str(pdf), str(root / "page")],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        images = sorted(root.glob("page*.png"))
        if not images:
            raise PdfPreviewError("無法繪製這個 PDF 頁面")
        return images[0].read_bytes()


def _identity(source: str | Path | bytes) -> tuple[str, bytes]:
    if isinstance(source, bytes):
        return f"b:{hashlib.sha256(source).hexdigest()}", source
    path = Path(source)
    stat = path.stat()
    digest = hashlib.sha256(f"{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
    return f"p:{digest}", path.read_bytes()


def _stream(raw: bytes):
    from io import BytesIO
    return BytesIO(raw)


def _px(value: float) -> int:
    return round(float(value) * 96 / 72)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
