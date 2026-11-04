"""Print a PDF by handing the browser the PDF itself.

The page boxes stay exactly as they are in the file, which is what a PDF
print uses. Typed values are drawn into those boxes; the page is not rebuilt
as a web page, so the top of the page is not sliced onto another sheet.
"""

from __future__ import annotations

import time
import uuid
from io import BytesIO
from pathlib import Path

from fpdf import FPDF
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.errors import PdfReadError
from pypdf.generic import NameObject

from .pdf_preview import PdfPreviewError, iter_widgets, preview_pdf

_JOBS: dict[str, tuple[float, bytes]] = {}
_JOB_SECONDS = 120
_CHECKED = {"☑", "✓", "✔", "yes", "true", "1"}
_FONTS = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
)


def compose_pdf(source: str | Path | bytes, fields: list) -> bytes:
    raw = Path(source).read_bytes() if not isinstance(source, bytes) else source
    if raw[:5] != b"%PDF-":
        raise PdfPreviewError("這不是 PDF")
    try:
        reader = PdfReader(stream=BytesIO(raw), strict=False)
    except PdfReadError as exc:
        raise PdfPreviewError("無法讀取這個 PDF") from exc
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("")
        except Exception as exc:
            raise PdfPreviewError("這份 PDF 有密碼，無法預覽") from exc
        if opened == 0:
            raise PdfPreviewError("這份 PDF 有密碼，無法預覽")
    laid = preview_pdf(raw)
    wanted = _clean_fields(fields)
    writer = PdfWriter()
    writer.append(reader)
    for index, page in enumerate(writer.pages):
        number = index + 1
        specs = laid["pages"][index]["fields"] if index < len(laid["pages"]) else []
        crop = page.cropbox
        crop_w, crop_h = float(crop.width), float(crop.height)
        rotation = int(page.rotation or 0) % 360
        widgets = [obj for obj, _kind in iter_widgets(page, crop_w, crop_h, rotation)]
        texts = []
        for spec, widget in zip(specs, widgets):
            value = wanted.get((number, spec["id"]), "")
            if not value:
                continue
            if spec["control"] == "checkbox":
                _set_check(widget, value.casefold() in _CHECKED or value in _CHECKED)
                continue
            stamped = dict(spec)
            stamped["_size"] = wanted.get((number, f"{spec['id']}:size"))
            texts.append((stamped, value))
        if texts:
            _stamp(page, texts)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def remember(data: bytes) -> str:
    now = time.time()
    expired = [key for key, (stamp, _) in _JOBS.items() if now - stamp > _JOB_SECONDS]
    for key in expired:
        _JOBS.pop(key, None)
    token = uuid.uuid4().hex
    _JOBS[token] = (now, data)
    return token


def recall(token: str) -> bytes | None:
    if len(token) != 32 or any(ch not in "0123456789abcdef" for ch in token):
        return None
    item = _JOBS.get(token)
    if item is None:
        return None
    stamp, data = item
    if time.time() - stamp > _JOB_SECONDS:
        _JOBS.pop(token, None)
        return None
    return data


def _clean_fields(fields: list) -> dict[tuple[int, str], str]:
    found = {}
    if not isinstance(fields, list):
        return found
    for item in fields[:400]:
        if not isinstance(item, dict):
            continue
        try:
            page = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        ident = str(item.get("id") or "")
        text = str(item.get("text") or "").replace("\r", " ").replace("\n", " ").strip()
        if page < 1 or not ident.startswith("pdf-") or not text:
            continue
        found[(page, ident)] = text[:500]
        size = item.get("size")
        try:
            found[(page, f"{ident}:size")] = str(float(size))
        except (TypeError, ValueError):
            pass
    return found


def _set_check(widget, checked: bool) -> None:
    state = _on_state(widget) if checked else NameObject("/Off")
    widget[NameObject("/AS")] = state
    widget[NameObject("/V")] = state
    parent = widget.get("/Parent")
    if parent is None:
        return
    try:
        parent.get_object()[NameObject("/V")] = state
    except Exception:
        return


def _on_state(widget):
    appearance = widget.get("/AP")
    normal = None if appearance is None else appearance.get("/N")
    if normal is not None:
        try:
            keys = list(normal.keys())
        except Exception:
            keys = []
        for key in keys:
            if str(key) != "/Off":
                return key if isinstance(key, NameObject) else NameObject(str(key))
    return NameObject("/Yes")


def _stamp(page, texts: list[tuple[dict, str]]) -> None:
    media = page.mediabox
    left = float(media.left)
    bottom = float(media.bottom)
    width = float(media.width)
    height = float(media.height)
    crop = page.cropbox
    crop_w = float(crop.width)
    crop_h = float(crop.height)
    rotation = int(page.rotation or 0) % 360
    origin_x = float(crop.left)
    origin_y = float(crop.bottom)
    pdf = FPDF(unit="pt", format=(width, height))
    pdf.set_auto_page_break(auto=False)
    pdf.set_margin(0)
    pdf.add_page()
    font = _font_file()
    pdf.add_font("Noto", fname=str(font))
    pdf.set_text_color(17, 17, 17)
    for spec, text in texts:
        box = _user_box(spec, crop_w, crop_h, rotation)
        user_x = origin_x + box[0]
        user_bottom = origin_y + box[1]
        user_w = box[2]
        user_h = box[3]
        user_top = user_bottom + user_h
        draw_x = user_x - left
        draw_y = (bottom + height) - user_top
        size = _font_size(pdf, text, user_w, user_h, spec.get("_size"))
        pdf.set_font("Noto", size=size)
        pdf.set_xy(draw_x, draw_y)
        pdf.cell(w=max(user_w, 1), h=max(user_h, 1), text=text, align="L")
    overlay = PdfReader(stream=BytesIO(pdf.output()))
    page.merge_transformed_page(
        overlay.pages[0],
        Transformation().translate(left, bottom),
        expand=False,
    )


def _font_size(pdf: FPDF, text: str, width: float, height: float, requested) -> float:
    size = height * 0.72
    try:
        chosen = float(requested)
    except (TypeError, ValueError):
        chosen = 0
    if chosen > 0:
        size = chosen * 72 / 96
    size = max(4, min(size, max(height - 1, 4)))
    pdf.set_font("Noto", size=size)
    while size > 4 and pdf.get_string_width(text) > max(1, width - 2):
        size = round(size - 0.4, 2)
        pdf.set_font("Noto", size=size)
    return size


def _user_box(spec: dict, crop_w: float, crop_h: float, rotation: int) -> tuple[float, float, float, float]:
    scale = 72 / 96
    x = float(spec["x"]) * scale
    y = float(spec["y"]) * scale
    w = float(spec["w"]) * scale
    h = float(spec["h"]) * scale
    corners = ((x, y), (x + w, y), (x, y + h), (x + w, y + h))
    mapped = [_from_top_left(px, py, crop_w, crop_h, rotation) for px, py in corners]
    xs = [item[0] for item in mapped]
    ys = [item[1] for item in mapped]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _from_top_left(x: float, y: float, crop_w: float, crop_h: float, rotation: int) -> tuple[float, float]:
    if rotation == 90:
        return y, x
    if rotation == 180:
        return crop_w - x, y
    if rotation == 270:
        return crop_w - y, crop_h - x
    return x, crop_h - y


def _font_file() -> Path:
    for path in _FONTS:
        if path.is_file():
            return path
    raise PdfPreviewError("無法準備這份 PDF")
