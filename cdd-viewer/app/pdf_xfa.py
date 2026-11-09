"""Turn an Adobe XFA form into ordinary pages when the PDF only shows the Reader warning."""

from __future__ import annotations

import gzip
import html
import io
import re
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

_MAX_PAGES = 30
_MAX_FIELDS = 2000
_LAYOUT = {"field", "subform", "subformSet", "exclGroup", "draw", "area", "pageSet", "pageArea", "break", "breakBefore"}
_SKIP = {"medium", "contentArea", "proto", "occur", "bind", "desc", "variables", "script", "event", "calculate", "validate"}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def to_pt(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    number = ""
    unit = ""
    for char in text:
        if char.isdigit() or char in ".-":
            number += char
        else:
            unit += char
    if not number:
        return None
    size = float(number)
    unit = unit.strip()
    if unit in {"", "pt"}:
        return size
    if unit == "in":
        return size * 72
    if unit == "mm":
        return size * 72 / 25.4
    if unit == "cm":
        return size * 72 / 2.54
    if unit == "px":
        return size * 72 / 96
    return size


def is_reader_warning(text: str) -> bool:
    low = " ".join(str(text or "").lower().split())
    if not low:
        return False
    if "pdf_forms_configure" in low or "proper contents of the document" in low or "requires adobe reader" in low:
        return True
    return ("adobe reader" in low or "adobe acrobat" in low) and len(low) < 700


def _px(value: float) -> int:
    return round(float(value) * 96 / 72)


def unwrap(data: bytes) -> bytes:
    if not data:
        return b""
    if data.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(data)
        except Exception:
            return data
    if data[:2] in {b"\x78\x9c", b"\x78\x01", b"\x78\xda"}:
        try:
            return zlib.decompress(data)
        except Exception:
            return data
    return data


def _looks_like_form(data: bytes) -> bool:
    if not data or len(data) > 15_000_000:
        return False
    if b"<field" in data or b"<template" in data or b":template" in data:
        return True
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"<\x00" in data[:400]:
        return b"f\x00i\x00e\x00l\x00d" in data or b"t\x00e\x00m\x00p\x00l\x00a\x00t\x00e" in data
    return False


def _stream_bytes(stream) -> bytes:
    data = b""
    try:
        data = stream.get_data() or b""
    except Exception:
        data = b""
    data = unwrap(data)
    if _looks_like_form(data):
        return data
    raw = unwrap(getattr(stream, "_data", b"") or b"")
    if _looks_like_form(raw):
        return raw
    try:
        inflated = zlib.decompress(getattr(stream, "_data", b"") or b"")
    except Exception:
        inflated = b""
    return inflated if _looks_like_form(inflated) else b""


def _packet_name(item) -> str:
    if isinstance(item, bytes):
        return item.decode("latin1", "replace").strip("/() ")
    text = str(item).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return text.strip("/ ")


def xfa_packets(reader) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    try:
        root = reader.trailer["/Root"].get_object()
        form = root.get("/AcroForm")
        if form is not None:
            form = form.get_object()
            packet = form.get("/XFA")
            if packet is not None:
                packet = packet.get_object()
                if hasattr(packet, "get_data"):
                    data = _stream_bytes(packet)
                    if data:
                        found["xfa"] = data
                else:
                    items = list(packet)
                    index = 0
                    while index + 1 < len(items):
                        name = _packet_name(items[index]) or f"packet{index}"
                        stream = items[index + 1]
                        try:
                            stream = stream.get_object()
                            data = _stream_bytes(stream)
                            if data:
                                found[name] = data
                        except Exception:
                            pass
                        index += 2
    except Exception:
        found = {}
    if not any(_looks_like_form(chunk) for chunk in found.values()):
        try:
            extra = reader.xfa or {}
        except Exception:
            extra = {}
        for key, data in extra.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            data = unwrap(data if isinstance(data, bytes) else b"")
            if _looks_like_form(data):
                found[str(key)] = data
    return found


def decode_xml(data: bytes) -> str:
    data = unwrap(data)
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", "replace")
    elif data.startswith(b"\xef\xbb\xbf"):
        text = data.decode("utf-8-sig", "replace")
    elif b"\x00" in data[:80]:
        try:
            text = data.decode("utf-16", "replace")
        except Exception:
            text = data.decode("utf-8", "replace")
    else:
        text = data.decode("utf-8", "replace")
    start = text.find("<")
    if start > 0:
        text = text[start:]
    return text.replace("\x00", "")


def _clean_xml(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    for tag in ("script", "calculate", "validate", "event"):
        text = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", text, flags=re.I | re.S)

    def repl(match: re.Match) -> str:
        name = match.group(1).lower()
        if name in {"amp", "lt", "gt", "quot", "apos"}:
            return match.group(0)
        return html.unescape(match.group(0))

    return re.sub(r"&([A-Za-z][A-Za-z0-9]+);", repl, text)


def _parse(text: str):
    cleaned = _clean_xml(text)
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError:
        cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.S)
        try:
            return ET.fromstring(cleaned)
        except ET.ParseError:
            return None


def template_root(packets: dict[str, bytes]):
    root, _text = _choose_template(list(packets.values()))
    return root


def _field_score(text: str) -> int:
    return text.lower().count("<field")


def _choose_template(chunks: list[bytes]):
    ranked = []
    for chunk in chunks:
        if not chunk:
            continue
        text = decode_xml(chunk)
        if "<" not in text:
            continue
        ranked.append((_field_score(text), len(text), text))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _score, _length, text in ranked:
        root = _parse(text)
        if root is None:
            continue
        node = root if local(root.tag) == "template" else next((item for item in root.iter() if local(item.tag) == "template"), root)
        if any(local(item.tag) == "field" for item in node.iter()):
            return node, text
    for score, _length, text in ranked:
        if score > 0 and _loose_fields(text):
            return None, text
    return None, ""


def _object_xml(reader) -> list[bytes]:
    found = []
    seen = set()
    xref = getattr(reader, "xref", {}) or {}
    for table in xref.values():
        if not hasattr(table, "keys"):
            continue
        for obj_id in list(table):
            if obj_id in seen:
                continue
            seen.add(obj_id)
            try:
                obj = reader.get_object(int(obj_id))
            except Exception:
                continue
            if not hasattr(obj, "get_data"):
                continue
            data = _stream_bytes(obj)
            if data:
                found.append(data)
    try:
        attachments = getattr(reader, "attachments", None) or {}
        for blob in attachments.values():
            data = unwrap(blob if isinstance(blob, bytes) else b"")
            if _looks_like_form(data):
                found.append(data)
    except Exception:
        pass
    return found


def _page_bytes(page) -> bytes:
    try:
        contents = page.get_contents()
        if contents is None:
            return b""
        streams = contents if isinstance(contents, list) else [contents]
        return b"\n".join(item.get_data() for item in streams)
    except Exception:
        return b""


def _deep_bytes(page) -> bytes:
    chunks = [_page_bytes(page)]
    try:
        resources = page.get("/Resources")
        if resources is None:
            return b"\n".join(chunks)
        resources = resources.get_object()
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return b"\n".join(chunks)
        xobjects = xobjects.get_object()
        for value in xobjects.values():
            obj = value.get_object()
            if hasattr(obj, "get_data"):
                chunks.append(obj.get_data()[:200000])
    except Exception:
        pass
    return b"\n".join(chunks)


def _is_reader_shell(reader) -> bool:
    try:
        pages = list(reader.pages[:3])
    except Exception:
        return False
    for page in pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if is_reader_warning(text) or "Adobe Reader" in text:
            return True
        if b"Adobe Reader" in _deep_bytes(page):
            return True
    return False


def _widget_count(reader) -> int:
    count = 0
    try:
        pages = list(reader.pages)
    except Exception:
        return 0
    for page in pages:
        try:
            annots = page.get("/Annots") or []
            count += len(list(annots))
        except Exception:
            continue
    return count


def _page_size(template) -> tuple[float, float, float, float]:
    width, height = 595.27, 841.89
    origin_x = origin_y = 0.0
    for node in template.iter():
        name = local(node.tag)
        if name == "medium":
            short = to_pt(node.get("short"))
            long = to_pt(node.get("long"))
            stock = (node.get("stock") or "").lower()
            if short and long:
                width, height = short, long
            elif stock == "letter":
                width, height = 612.0, 792.0
            elif "a4" in stock:
                width, height = 595.27, 841.89
        elif name == "contentArea":
            origin_x = to_pt(node.get("x")) or 0.0
            origin_y = to_pt(node.get("y")) or 0.0
            area_w = to_pt(node.get("w"))
            area_h = to_pt(node.get("h"))
            if area_w and area_h and area_w > 200 and area_h > 200:
                width, height = area_w + origin_x, area_h + origin_y
    return width, height, origin_x, origin_y


def _text(node) -> str:
    parts = []
    for item in node.iter():
        kind = local(item.tag)
        if kind in {"script", "calculate", "validate", "event"}:
            continue
        if kind in {"text", "p", "span"} and item.text:
            parts.append(item.text)
    return " ".join("".join(parts).split())


def _control(node) -> str:
    for item in node.iter():
        if local(item.tag) == "checkButton":
            return "checkbox"
    return "text"


def _font():
    roots = [Path("/usr/share/fonts"), Path("/System/Library/Fonts")]
    names = ("NotoSansCJK-Regular.ttc", "NotoSansCJK-Regular.otf", "PingFang.ttc")
    for root in roots:
        if not root.is_dir():
            continue
        for name in names:
            found = list(root.rglob(name))
            if found:
                return str(found[0])
    return None


def _lines(text: str, font, width: int) -> list[str]:
    lines = []
    line = ""
    for char in text:
        if char == "\n":
            lines.append(line)
            line = ""
            continue
        trial = line + char
        try:
            wide = font.getlength(trial) > width
        except Exception:
            wide = len(trial) * 7 > width
        if wide and line:
            lines.append(line)
            line = char
        else:
            line = trial
    if line:
        lines.append(line)
    return lines or [""]


def _paint(page: dict) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (page["width"], page["height"]), "white")
    draw = ImageDraw.Draw(image)
    font_path = _font()
    for item in page["draws"]:
        if item["kind"] == "rect":
            box = (item["x"], item["y"], item["x"] + item["w"], item["y"] + item["h"])
            draw.rectangle(box, outline="#334155", width=1)
        elif item["kind"] == "line":
            draw.line((item["x"], item["y"], item["x2"], item["y2"]), fill="#334155", width=1)
        elif item["kind"] == "text" and item["text"] and not is_reader_warning(item["text"]):
            size = max(8, min(14, int(item.get("h") or 14)))
            try:
                font = ImageFont.truetype(font_path, size=size) if font_path else ImageFont.load_default()
            except Exception:
                font = ImageFont.load_default()
            width = max(40, int(item.get("w") or page["width"] - item["x"] - 8))
            top = item["y"]
            for line in _lines(item["text"], font, width)[:12]:
                if top > page["height"] - 8:
                    break
                draw.text((item["x"], top), line, fill="#111111", font=font)
                top += size + 2
    stored = io.BytesIO()
    image.save(stored, format="PNG")
    return stored.getvalue()


def _new_page(width: float, height: float, number: int) -> dict:
    return {"n": number, "width": _px(width), "height": _px(height), "fields": [], "ticks": [], "draws": []}


def _attrs(raw: str) -> dict[str, str]:
    found = {}
    for name, value in re.findall(r"""([A-Za-z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", raw):
        found[name.split(":")[-1]] = html.unescape(value or "")
    return found


def _loose_fields(text: str) -> list[dict]:
    cleaned = _clean_xml(text)
    starts = list(re.finditer(r"<field\b([^>]*)>", cleaned, flags=re.I))
    found = []
    seen = set()
    for match in starts:
        attrs = _attrs(match.group(1))
        close = cleaned.lower().find("</field>", match.end())
        body = cleaned[match.end():close if close != -1 else match.end() + 500]
        use = attrs.get("use") or attrs.get("usehref") or ""
        key = (attrs.get("name") or use, attrs.get("x"), attrs.get("y"))
        if key in seen:
            continue
        if not attrs.get("name") and not attrs.get("x") and not use:
            continue
        seen.add(key)
        found.append({
            "name": attrs.get("name") or "欄位",
            "x": attrs.get("x"),
            "y": attrs.get("y"),
            "w": attrs.get("w") or attrs.get("minW"),
            "h": attrs.get("h") or attrs.get("minH"),
            "use": use,
            "checkbox": "checkButton" in body or "checkbutton" in body.lower(),
        })
    by_id = {}
    for match in re.finditer(r"<field\b([^>]*)>", cleaned, flags=re.I):
        attrs = _attrs(match.group(1))
        if attrs.get("id"):
            by_id[attrs["id"]] = attrs
        if attrs.get("name"):
            by_id.setdefault(attrs["name"], attrs)
    for field in found:
        token = field["use"].split("#")[-1].split(".")[-1].strip("() ")
        source = by_id.get(token)
        if source is None:
            continue
        for key in ("x", "y", "w", "h"):
            if not field.get(key) and source.get(key):
                field[key] = source[key]
        if "checkButton" in source.get("ui", "") or source.get("ui") == "checkButton":
            field["checkbox"] = True
    return found


def _lookup(token: str, by_id: dict, by_name: dict):
    if not token:
        return None
    token = token.split("#")[-1]
    token = token.replace("som(", "").replace(")", "").strip(". ")
    ident = token.split(".")[-1]
    found = by_id.get(ident)
    if found is not None:
        return found
    return by_name.get(ident)


def render_template(template) -> tuple[list[dict], dict[int, bytes]] | None:
    width, height, origin_x, origin_y = _page_size(template)
    pages = [_new_page(width, height, 1)]
    names: list[str] = []
    by_id = {}
    by_name = {}
    for node in template.iter():
        if node.get("id"):
            by_id[node.get("id")] = node
        if node.get("name"):
            by_name.setdefault(node.get("name"), node)
    stack: set[int] = set()

    def page_for(top: float) -> dict:
        index = min(_MAX_PAGES - 1, max(0, int(top // height)))
        while len(pages) <= index:
            pages.append(_new_page(width, height, len(pages) + 1))
        return pages[index]

    def total() -> int:
        return sum(len(page["fields"]) for page in pages)

    def kids_of(node):
        return [child for child in list(node) if local(child.tag) in _LAYOUT or local(child.tag) in {"template", "pageSet", "pageArea"}]

    def walk(node, left: float, top: float, layout: str) -> float:
        if total() >= _MAX_FIELDS:
            return 0.0
        marker = id(node)
        if marker in stack:
            return 0.0
        stack.add(marker)
        try:
            return _walk(node, left, top, layout)
        finally:
            stack.discard(marker)

    def _walk(node, left: float, top: float, layout: str) -> float:
        name = local(node.tag)
        if name in _SKIP or name in {"break", "breakBefore"}:
            return 0.0
        if name in {"template", "pageSet", "pageArea"}:
            for child in kids_of(node):
                walk(child, left, top, layout)
            return 0.0
        proto = _lookup(node.get("use") or node.get("usehref") or "", by_id, by_name)
        if proto is node:
            proto = None
        source = node if proto is None else proto
        own_x = to_pt(node.get("x") if node.get("x") is not None else source.get("x")) or 0.0
        own_y = to_pt(node.get("y") if node.get("y") is not None else source.get("y")) or 0.0
        own_w = to_pt(node.get("w") if node.get("w") is not None else source.get("w"))
        own_h = to_pt(node.get("h") if node.get("h") is not None else source.get("h"))
        child_layout = node.get("layout") or source.get("layout") or layout
        child_layout = "tb" if child_layout in {"tb", "lr-tb"} else "position"
        abs_x = left + own_x
        abs_y = top + own_y
        if name == "field":
            control = "checkbox" if _control(node) == "checkbox" or _control(source) == "checkbox" else "text"
            box_w = own_w if own_w and own_w > 0 else (to_pt(node.get("minW") or source.get("minW")) or (12.0 if control == "checkbox" else 120.0))
            box_h = own_h if own_h and own_h > 0 else (to_pt(node.get("minH") or source.get("minH")) or (12.0 if control == "checkbox" else 16.0))
            target = page_for(abs_y)
            local_y = abs_y - (target["n"] - 1) * height
            caption = ""
            for host in (node, source):
                for child in list(host):
                    kind = local(child.tag)
                    if kind == "caption" and not caption:
                        caption = _text(child)
                    elif kind == "assist" and not caption:
                        caption = _text(child)
            if caption and not is_reader_warning(caption):
                target["draws"].append({
                    "kind": "text",
                    "x": _px(abs_x),
                    "y": max(0, _px(local_y) - 14),
                    "w": max(40, _px(box_w)),
                    "h": 14,
                    "text": caption[:180],
                })
            label = node.get("name") or source.get("name") or "欄位"
            target["fields"].append({
                "id": f"pdf-{target['n']}-{len(target['fields']) + 1}",
                "name": ".".join([*names, label]),
                "text": "",
                "control": control,
                "checked": False,
                "blank": True,
                "x": _px(abs_x),
                "y": _px(local_y),
                "w": max(8, _px(box_w)),
                "h": max(8, _px(box_h)),
            })
            if control == "checkbox":
                target["draws"].append({
                    "kind": "rect",
                    "x": _px(abs_x),
                    "y": _px(local_y),
                    "w": max(8, _px(box_w)),
                    "h": max(8, _px(box_h)),
                })
            return box_h
        if name == "draw":
            text = _text(node) or _text(source)
            if is_reader_warning(text) or is_reader_warning(node.get("name") or ""):
                return own_h or 0.0
            target = page_for(abs_y)
            local_y = abs_y - (target["n"] - 1) * height
            if text:
                target["draws"].append({
                    "kind": "text",
                    "x": _px(abs_x),
                    "y": _px(local_y),
                    "w": max(40, _px(own_w or (width - abs_x))),
                    "h": max(12, _px(own_h or 14)),
                    "text": text[:800],
                })
            elif any(local(child.tag) in {"rectangle", "line"} for child in list(node)):
                target["draws"].append({
                    "kind": "rect",
                    "x": _px(abs_x),
                    "y": _px(local_y),
                    "w": max(1, _px(own_w or 1)),
                    "h": max(1, _px(own_h or 1)),
                })
            return own_h or 0.0
        if name in {"subform", "exclGroup", "subformSet", "area"}:
            if is_reader_warning(node.get("name") or ""):
                return 0.0
            pushed = False
            if node.get("name"):
                names.append(node.get("name"))
                pushed = True
            kids = kids_of(node)
            if not kids and proto is not None:
                kids = kids_of(proto)
            try:
                if child_layout == "tb":
                    flow = abs_y
                    for child in kids:
                        if local(child.tag) in {"break", "breakBefore"} or child.get("breakBefore"):
                            flow = (int(flow // height) + 1) * height + origin_y
                        flow += walk(child, abs_x, flow, "tb")
                    return max(own_h or 0.0, flow - abs_y)
                next_band = abs_y
                for child in kids:
                    child_h = to_pt(child.get("h")) or 0
                    page_block = local(child.tag) in {"subform", "area", "subformSet"} and child_h >= height * 0.6
                    child_y = to_pt(child.get("y"))
                    if page_block and next_band > abs_y + 1 and child_y in {None, 0}:
                        walk(child, abs_x, next_band, "position")
                        next_band += child_h
                    else:
                        walk(child, abs_x, abs_y, "position")
                        if page_block:
                            next_band = max(next_band, abs_y + child_h)
                return own_h or 0.0
            finally:
                if pushed:
                    names.pop()
        for child in kids_of(node):
            walk(child, abs_x, abs_y, child_layout)
        return own_h or 0.0

    walk(template, origin_x, origin_y, "position")
    if not any(page["fields"] for page in pages):
        for node in template.iter():
            if local(node.tag) != "proto":
                continue
            for child in list(node):
                walk(child, origin_x, origin_y, "position")
    pages = [page for page in pages if page["fields"] or page["draws"]]
    if not any(page["fields"] for page in pages):
        return None
    pngs = {page["n"]: _paint(page) for page in pages}
    for page in pages:
        page.pop("draws", None)
    return pages, pngs


def render_loose(text: str) -> tuple[list[dict], dict[int, bytes]] | None:
    fields = _loose_fields(text)
    if not fields:
        return None
    width, height = 595.27, 841.89
    pages = [_new_page(width, height, 1)]
    flow = 36.0
    for field in fields[:_MAX_FIELDS]:
        own_x = to_pt(field.get("x"))
        own_y = to_pt(field.get("y"))
        box_w = to_pt(field.get("w")) or (12.0 if field["checkbox"] else 160.0)
        box_h = to_pt(field.get("h")) or (12.0 if field["checkbox"] else 16.0)
        if own_x is None and own_y is None:
            own_x, own_y = 36.0, flow
            flow += box_h + 8
        else:
            own_x = own_x or 36.0
            own_y = own_y or flow
        index = min(_MAX_PAGES - 1, max(0, int(own_y // height)))
        while len(pages) <= index:
            pages.append(_new_page(width, height, len(pages) + 1))
        target = pages[index]
        local_y = own_y - index * height
        target["fields"].append({
            "id": f"pdf-{target['n']}-{len(target['fields']) + 1}",
            "name": field["name"],
            "text": "",
            "control": "checkbox" if field["checkbox"] else "text",
            "checked": False,
            "blank": True,
            "x": _px(own_x),
            "y": _px(local_y),
            "w": max(8, _px(box_w)),
            "h": max(8, _px(box_h)),
        })
    pngs = {page["n"]: _paint(page) for page in pages if page["fields"]}
    pages = [page for page in pages if page["fields"]]
    return (pages, pngs) if pages else None


def needs_browser_pdf(reader) -> bool:
    """The original file should be drawn by the browser, not rebuilt here."""
    packets = list(xfa_packets(reader).values())
    has_form = any(_looks_like_form(chunk) for chunk in packets)
    if not has_form and _is_reader_shell(reader):
        has_form = any(_looks_like_form(chunk) for chunk in _object_xml(reader))
    if not has_form:
        return False
    if _is_reader_shell(reader):
        return True
    return _widget_count(reader) < 3


def xfa_preview(reader) -> dict | None:
    """Replacement pages for an Adobe dynamic form, or None for a normal PDF."""
    packets = xfa_packets(reader)
    chunks = list(packets.values())
    if not any(_looks_like_form(chunk) for chunk in chunks) and _is_reader_shell(reader):
        chunks.extend(_object_xml(reader))
    root, text = _choose_template(chunks)
    rendered = render_template(root) if root is not None else None
    if rendered is None and text:
        rendered = render_loose(text)
    if rendered is None:
        return None
    pages, pngs = rendered
    xfa_fields = sum(len(page["fields"]) for page in pages)
    if xfa_fields < 1:
        return None
    if not _is_reader_shell(reader) and _widget_count(reader) >= max(3, xfa_fields // 5):
        return None
    return {"pages": pages, "xfa_pngs": pngs}
