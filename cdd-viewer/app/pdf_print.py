"""Print a PDF by handing the browser the PDF itself.

The page boxes stay exactly as they are in the file, which is what a PDF
print uses. Typed values are drawn into those boxes; the page is not rebuilt
as a web page, so the top of the page is not sliced onto another sheet.
"""

from __future__ import annotations

import re
import tempfile
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path

from fpdf import FPDF
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    create_string_object,
)

from .pdf_preview import PdfPreviewError, iter_widgets, open_reader, preview_pdf

_JOBS: dict[str, tuple[float, bytes]] = {}
_JOB_SECONDS = 120
_CHECKED = {"☑", "✓", "✔", "yes", "true", "1"}
_FONT_LOCK = threading.Lock()
_FONT_FACE: Path | None = None
# Noto CJK here is a CFF collection. Putting it in FontFile2 makes viewers draw
# the wrong glyphs until the field is edited and rebuilt from the real value.
_FONTS = (
    Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
    Path("/usr/share/fonts/truetype/arphic/ukai.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
)


def compose_pdf(source: str | Path | bytes, fields: list) -> bytes:
    raw = Path(source).read_bytes() if not isinstance(source, bytes) else source
    reader = open_reader(raw)
    laid = preview_pdf(raw)
    wanted = _clean_fields(fields)
    writer = PdfWriter(clone_from=reader)
    for index, page in enumerate(writer.pages):
        number = index + 1
        specs = laid["pages"][index]["fields"] if index < len(laid["pages"]) else []
        crop = page.cropbox
        crop_w, crop_h = float(crop.width), float(crop.height)
        rotation = int(page.rotation or 0) % 360
        widgets = [obj for obj, _kind in iter_widgets(page, crop_w, crop_h, rotation)]
        for spec, widget in zip(specs, widgets):
            value = wanted.get((number, spec["id"]), "")
            if not value:
                continue
            if spec["control"] == "checkbox":
                _set_check(widget, value.casefold() in _CHECKED or value in _CHECKED)
                continue
            _show_widget_text(writer, widget, value, wanted.get((number, f"{spec['id']}:size")))
    _keep_appearances(writer)
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
        raw = str(item.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = raw.split("\n")
        text = lines[0].strip() if len(lines) == 1 else "\n".join(line.strip() for line in lines)
        if page < 1 or not ident.startswith("pdf-") or not text.strip():
            continue
        found[(page, ident)] = text[:500]
        size = item.get("size")
        try:
            found[(page, f"{ident}:size")] = str(float(size))
        except (TypeError, ValueError):
            pass
    return found


def _color_before_text(data: bytes) -> bytes:
    """fpdf sets the fill color inside BT/ET. Preview then keeps the text copyable but does not paint it."""
    pattern = re.compile(
        rb"BT(\s+[-\d.]+\s+[-\d.]+\s+Td)(\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\s+rg)",
    )
    return pattern.sub(rb"\2 BT\1", data)


def _show_widget_text(writer: PdfWriter, widget, text: str, requested) -> None:
    rect = widget.get("/Rect")
    if rect is None:
        return
    box = [float(item) for item in rect]
    width = abs(box[2] - box[0])
    height = abs(box[3] - box[1])
    if width < 2 or height < 2:
        return
    pdf = FPDF(unit="pt", format=(max(width, 4), max(height, 4)))
    pdf.set_auto_page_break(auto=False)
    pdf.set_margin(0)
    pdf.add_page()
    font = _font_file()
    pdf.add_font("Noto", fname=str(font))
    pdf.set_text_color(17, 17, 17)
    lines = text.split("\n") or [""]
    line_h = height / len(lines)
    pdf.set_font("Noto", size=max(4, min(line_h * 0.8, 18)))
    size = _font_size(pdf, max(lines, key=len), max(1, width - 4), line_h, None if len(lines) > 1 else requested)
    pdf.set_font("Noto", size=size)
    for index, line in enumerate(lines):
        pdf.text(2, (index + 0.8) * line_h, line)
    overlay = PdfReader(stream=BytesIO(pdf.output())).pages[0]
    cloned = overlay.clone(writer)
    contents = cloned.get_contents()
    data = b"" if contents is None else _color_before_text(contents.get_data())
    resources = cloned.get("/Resources")
    data = _isolate_font(resources, data)
    data = _unicode_appearance(resources, data)
    form = DecodedStreamObject()
    form.set_data(data)
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/FormType")] = NumberObject(1)
    form[NameObject("/BBox")] = RectangleObject([0, 0, width, height])
    if resources is not None:
        form[NameObject("/Resources")] = resources
    widget[NameObject("/AP")] = DictionaryObject({NameObject("/N"): writer._add_object(form)})
    widget[NameObject("/V")] = create_string_object(text)


def _widget_key(obj):
    ref = getattr(obj, "indirect_reference", None)
    if ref is not None:
        return (ref.idnum, ref.generation)
    return id(obj)


def _without_widget(items, widget):
    target = _widget_key(widget)
    kept = ArrayObject()
    for item in list(items):
        try:
            same = _widget_key(item) == target or _widget_key(item.get_object()) == target
        except Exception:
            same = False
        if not same:
            kept.append(item)
    return kept


def _retire_text_widget(writer: PdfWriter, page, widget) -> None:
    """Remove the empty field box so it cannot cover the text drawn on the page."""
    annots = page.get("/Annots")
    if annots is not None:
        page[NameObject("/Annots")] = _without_widget(annots.get_object(), widget)
    try:
        form = writer._root_object.get("/AcroForm")
        if form is None:
            return
        form = form.get_object()
        fields = form.get("/Fields")
        if fields is not None:
            form[NameObject("/Fields")] = _without_widget(fields.get_object(), widget)
    except Exception:
        return


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


def _stamp(writer: PdfWriter, page, texts: list[tuple[dict, str]]) -> None:
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
        lines = text.split("\n")
        measure = max(lines, key=len) if len(lines) > 1 else text
        line_h = user_h / len(lines) if len(lines) > 1 else user_h
        size = _font_size(pdf, measure, user_w, line_h, None if len(lines) > 1 else spec.get("_size"))
        pdf.set_font("Noto", size=size)
        _draw_text(pdf, text, draw_x, draw_y, user_w, user_h)
    overlay = PdfReader(stream=BytesIO(pdf.output())).pages[0]
    _attach_overlay(writer, page, overlay, left, bottom)


def _attach_overlay(writer: PdfWriter, page, overlay, left: float, bottom: float) -> None:
    """Draw the overlay as its own form so the page's original fonts stay untouched."""
    cloned = overlay.clone(writer)
    contents = cloned.get_contents()
    data = b"" if contents is None else _color_before_text(contents.get_data())
    resources = cloned.get("/Resources")
    data = _isolate_font(resources, data)
    data = _unicode_appearance(resources, data)
    form = DecodedStreamObject()
    form.set_data(data)
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/FormType")] = NumberObject(1)
    form[NameObject("/BBox")] = overlay.mediabox
    if resources is not None:
        form[NameObject("/Resources")] = resources
    form_ref = writer._add_object(form)
    page_resources = page.get("/Resources")
    if page_resources is None:
        page_resources = DictionaryObject()
        page[NameObject("/Resources")] = page_resources
    else:
        page_resources = page_resources.get_object()
    xobjects = page_resources.get("/XObject")
    if xobjects is None:
        xobjects = DictionaryObject()
        page_resources[NameObject("/XObject")] = xobjects
    else:
        xobjects = xobjects.get_object()
    name = "/CDDFill"
    index = 0
    while NameObject(name) in xobjects:
        index += 1
        name = f"/CDDFill{index}"
    xobjects[NameObject(name)] = form_ref
    piece = f"q\n1 0 0 1 {left:.4f} {bottom:.4f} cm\n{name} Do\nQ\n".encode()
    extra = DecodedStreamObject()
    extra.set_data(piece)
    extra_ref = writer._add_object(extra)
    current = page.get("/Contents")
    if current is None:
        page[NameObject("/Contents")] = extra_ref
        return
    current_obj = current.get_object()
    if isinstance(current_obj, ArrayObject):
        current_obj.append(extra_ref)
        return
    page[NameObject("/Contents")] = ArrayObject([current, extra_ref])


def _draw_text(pdf: FPDF, text: str, x: float, y: float, width: float, height: float) -> None:
    lines = text.split("\n")
    if len(lines) == 1:
        pdf.set_xy(x, y)
        pdf.cell(w=max(width, 1), h=max(height, 1), text=text, align="L")
        return
    line_h = max(height, 1) / len(lines)
    for index, line in enumerate(lines):
        pdf.set_xy(x, y + index * line_h)
        pdf.cell(w=max(width, 1), h=line_h, text=line, align="L")


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


def _keep_appearances(writer: PdfWriter) -> None:
    """The appearance we drew is the one to show. Regenerating it uses the form's old font and garbles the text."""
    try:
        form = writer._root_object.get("/AcroForm")
    except Exception:
        return
    if form is None:
        return
    try:
        form.get_object()[NameObject("/NeedAppearances")] = BooleanObject(False)
    except Exception:
        return


def _decode_literal(data: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(data):
        byte = data[index]
        if byte != 0x5C:
            out.append(byte)
            index += 1
            continue
        index += 1
        if index >= len(data):
            break
        escaped = data[index]
        if escaped in b"01234567":
            digits = bytes([escaped])
            for _ in range(2):
                if index + 1 < len(data) and data[index + 1] in b"01234567":
                    index += 1
                    digits += bytes([data[index]])
                else:
                    break
            out.append(int(digits, 8) & 0xFF)
        elif escaped == ord("n"):
            out.append(10)
        elif escaped == ord("r"):
            out.append(13)
        elif escaped == ord("t"):
            out.append(9)
        elif escaped == ord("b"):
            out.append(8)
        elif escaped == ord("f"):
            out.append(12)
        else:
            out.append(escaped)
        index += 1
    return bytes(out)


def _encode_literal(data: bytes) -> bytes:
    out = bytearray()
    for byte in data:
        if byte in (0x28, 0x29, 0x5C):
            out.extend((0x5C, byte))
        else:
            out.append(byte)
    return bytes(out)


def _parse_tounicode(data: bytes) -> dict[int, int]:
    text = data.decode("latin1", errors="ignore")
    found: dict[int, int] = {}
    for block in re.finditer(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block.group(1)):
            if len(dst) == 4:
                found[int(src, 16)] = int(dst, 16)
    for block in re.finditer(r"beginbfrange(.*?)endbfrange", text, re.S):
        body = block.group(1)
        for src1, src2, dst in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>",
            body,
        ):
            if len(dst) != 4:
                continue
            start, end, base = int(src1, 16), int(src2, 16), int(dst, 16)
            for offset, cid in enumerate(range(start, end + 1)):
                found[cid] = (base + offset) & 0xFFFF
    return found


def _glyph_id(cmap: bytes, cid: int) -> int:
    offset = cid * 2
    if offset + 2 > len(cmap):
        return 0
    return int.from_bytes(cmap[offset:offset + 2], "big")


def _parse_widths(values) -> dict[int, int]:
    found: dict[int, int] = {}
    items = list(values or [])
    index = 0
    while index < len(items):
        first = int(items[index])
        index += 1
        if index >= len(items):
            break
        nxt = items[index]
        index += 1
        if isinstance(nxt, ArrayObject) or str(type(nxt)).endswith("ArrayObject'>"):
            for offset, width in enumerate(nxt):
                found[first + offset] = int(width)
            continue
        if index >= len(items):
            break
        last = int(nxt)
        width = int(items[index])
        index += 1
        for cid in range(first, last + 1):
            found[cid] = width
    return found


def _tounicode_stream(codes: dict[int, int]) -> bytes:
    pairs = sorted((cid, cid) for cid in codes if 0 <= cid <= 0xFFFF)
    chunks = []
    for start in range(0, len(pairs), 100):
        group = pairs[start:start + 100]
        body = "\n".join(f"<{cid:04X}> <{uni:04X}>" for cid, uni in group)
        chunks.append(f"{len(group)} beginbfchar\n{body}\nendbfchar")
    body = "\n".join(chunks) or "0 beginbfchar\nendbfchar"
    return (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{body}\n"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("ascii")


def _rewrite_shown_text(data: bytes, codes: dict[int, int], glyphs: bytes, used: dict[int, int]) -> bytes:
    def convert(raw: bytes) -> bytes:
        if len(raw) < 2 or len(raw) % 2:
            return raw
        out = bytearray()
        changed = False
        for index in range(0, len(raw), 2):
            cid = int.from_bytes(raw[index:index + 2], "big")
            uni = codes.get(cid)
            if uni is None:
                out.extend(raw[index:index + 2])
                continue
            changed = True
            used[uni] = _glyph_id(glyphs, cid)
            out.extend(uni.to_bytes(2, "big"))
        return bytes(out) if changed else raw

    def replace_literal(match: re.Match) -> bytes:
        raw = _decode_literal(match.group(1))
        updated = convert(raw)
        if updated == raw:
            return match.group(0)
        return b"(" + _encode_literal(updated) + b")" + match.group(2)

    pattern = re.compile(rb"\(((?:\\.|[^\\)])*)\)(\s*Tj)")
    return pattern.sub(replace_literal, data)


def _unicode_appearance(resources, data: bytes) -> bytes:
    """Adobe paints subset character ids as the wrong glyphs. Use the real Unicode values."""
    if resources is None or not data:
        return data
    try:
        fonts = resources.get_object().get("/Font")
        if fonts is None:
            return data
        fonts = fonts.get_object()
    except Exception:
        return data
    for ref in fonts.values():
        try:
            font = ref.get_object()
        except Exception:
            continue
        if str(font.get("/Subtype") or "") != "/Type0":
            continue
        descendants = font.get("/DescendantFonts")
        tounicode = font.get("/ToUnicode")
        if not descendants or tounicode is None:
            continue
        try:
            cid = descendants[0].get_object()
            codes = _parse_tounicode(tounicode.get_object().get_data())
            gid_map = cid.get("/CIDToGIDMap")
            glyphs = b"" if gid_map is None or str(gid_map) == "/Identity" else gid_map.get_object().get_data()
        except Exception:
            continue
        if not codes:
            continue
        used: dict[int, int] = {}
        updated = _rewrite_shown_text(data, codes, glyphs, used)
        if not used or gid_map is None or str(gid_map) == "/Identity":
            continue
        table = bytearray(131072)
        for uni, gid in used.items():
            if 0 <= uni < 65536:
                table[uni * 2:uni * 2 + 2] = int(gid).to_bytes(2, "big")
        try:
            gid_map.get_object().set_data(bytes(table))
            tounicode.get_object().set_data(_tounicode_stream(used))
        except Exception:
            continue
        try:
            info = cid.get("/CIDSystemInfo")
            if info is not None:
                info = info.get_object()
                info[NameObject("/Ordering")] = create_string_object("Identity")
                info[NameObject("/Registry")] = create_string_object("Adobe")
        except Exception:
            pass
        try:
            old_widths = _parse_widths(cid.get("/W"))
            widths = ArrayObject()
            for cid_num, uni in codes.items():
                if uni not in used or cid_num not in old_widths:
                    continue
                widths.append(NumberObject(uni))
                widths.append(ArrayObject([NumberObject(old_widths[cid_num])]))
            if widths:
                cid[NameObject("/W")] = widths
        except Exception:
            pass
        data = updated
    return data


def _isolate_font(resources, data: bytes) -> bytes:
    """Keep the fill font off the page's /F1, which is a different encoding."""
    if resources is None or not data:
        return data
    try:
        fonts = resources.get_object().get("/Font")
        if fonts is None:
            return data
        fonts = fonts.get_object()
    except Exception:
        return data
    renamed = data
    for key in list(fonts.keys()):
        token = str(key)
        if not token.startswith("/F"):
            continue
        unique = "/CDD" + token[1:]
        fonts[NameObject(unique)] = fonts[key]
        del fonts[key]
        renamed = re.sub(
            rb"%s(?![0-9A-Za-z])" % re.escape(token.encode("ascii")),
            unique.encode("ascii"),
            renamed,
        )
    return renamed


def _family_name(font) -> str:
    for rec in font["name"].names:
        if rec.nameID != 1:
            continue
        try:
            return rec.toUnicode()
        except Exception:
            continue
    return ""


def _font_rank(name: str) -> int:
    if " HK" in name:
        return 0
    if " TW" in name:
        return 1
    return 2


def _glyf_face(path: Path) -> Path | None:
    from fontTools.ttLib import TTCollection, TTFont

    suffix = path.suffix.lower()
    if suffix in {".ttc", ".otc"}:
        collection = TTCollection(str(path))
        chosen = None
        rank = 99
        for font in collection.fonts:
            if "glyf" not in font:
                continue
            name = _family_name(font)
            score = _font_rank(name)
            if chosen is None or score < rank:
                chosen = font
                rank = score
        if chosen is None:
            return None
        dest = Path(tempfile.gettempdir()) / f"cdd-glyf-v2-{path.stem}.ttf"
        if not dest.is_file() or dest.stat().st_size < 1000:
            temporary = dest.with_suffix(".tmp")
            chosen.save(temporary)
            temporary.replace(dest)
        return dest
    font = TTFont(str(path), lazy=True)
    if "glyf" not in font:
        return None
    return path


def _font_file() -> Path:
    global _FONT_FACE
    with _FONT_LOCK:
        if _FONT_FACE is not None and _FONT_FACE.is_file():
            return _FONT_FACE
        for path in _FONTS:
            if not path.is_file():
                continue
            try:
                face = _glyf_face(path)
            except Exception:
                continue
            if face is not None:
                _FONT_FACE = face
                return face
    raise PdfPreviewError("無法準備這份 PDF")
