"""Read a Word document into a preview of its text and fields.

Word fields (content controls, MERGEFIELD, DOCVARIABLE, REF, and legacy form
fields) keep the text Word currently shows. The preview also records the
parameter name the field references, so the page can map that parameter.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
VML = "{urn:schemas-microsoft-com:vml}"
_MAX_XML = 8_000_000
_INSTR = re.compile(
    r"(MERGEFIELD|DOCVARIABLE|REF|QUOTE|FORMTEXT|FORMCHECKBOX)\s+(\"[^\"]+\"|'[^']+'|[^\s\\]+)",
    re.IGNORECASE,
)


class WordPreviewError(Exception):
    """The docx cannot be shown as a form preview."""


def marked_docx(source: str | Path | bytes) -> bytes:
    """Same document, with a tiny locator written into each empty field.

    The locator is what makes a blank fill-in clickable. Fields that already
    show text are left unchanged so the page does not move.
    """
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    xml, styles_xml = _package(raw)
    root = ET.fromstring(xml)
    _register_namespaces()
    body = root.find(f"{W}body")
    if body is not None:
        _pages(body, _styles(styles_xml))
    stamped = ET.tostring(root, xml_declaration=True, encoding="utf-8")
    return _swap_document(raw, stamped)


def preview_docx(source: str | Path | bytes) -> dict:
    """Return pages of paragraphs, tables, and fields. No company data is read."""
    xml, styles_xml = _package(source)
    try:
        root = ET.fromstring(xml)
        styles = _styles(styles_xml)
    except ET.ParseError as exc:
        raise WordPreviewError("無法讀取這個 Word 檔") from exc
    body = root.find(f"{W}body")
    if body is None:
        raise WordPreviewError("無法讀取這個 Word 檔")
    pages = _pages(body, styles)
    return {"kind": "docx", "pages": pages, "page_count": len(pages)}


def _package(source: str | Path | bytes) -> tuple[bytes, bytes | None]:
    try:
        if isinstance(source, bytes):
            from io import BytesIO
            archive = zipfile.ZipFile(BytesIO(source))
        else:
            archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WordPreviewError("無法讀取這個 Word 檔") from exc
    with archive:
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError as exc:
            raise WordPreviewError("無法讀取這個 Word 檔") from exc
        if info.file_size > _MAX_XML:
            raise WordPreviewError("無法讀取這個 Word 檔")
        styles = archive.read("word/styles.xml") if "word/styles.xml" in archive.namelist() else None
        return archive.read("word/document.xml"), styles


def _pages(body: ET.Element, styles: dict) -> list[dict]:
    pages: list[list[dict]] = [[]]
    setups: list[dict | None] = [None]
    counter = {"n": 0, "styles": styles, "rbase": {}}
    stack: list[dict] = []
    label = ""
    for child in list(body):
        if child.tag == f"{W}sectPr":
            continue
        if child.tag == f"{W}p":
            _push_all(pages[-1], _flow_paragraph(child, counter, stack))
            text = _clean_label(_texts(child))
            if text:
                label = text
            section = child.find(f"{W}pPr/{W}sectPr")
            if section is not None and _attr(section.find(f"{W}type")) not in {"continuous", "nextColumn"}:
                setups[-1] = _page_setup(section)
                pages.append([])
                setups.append(None)
        elif child.tag == f"{W}tbl":
            _push_all(pages[-1], [_table_block(child, counter, stack, label)])
            label = ""
        elif child.tag == f"{W}sdt":
            pages[-1].extend(_sdt_blocks(child, counter, stack, label))
            label = ""
    final = _page_setup(body.find(f"{W}sectPr"))
    if setups[-1] is None:
        setups[-1] = final
    kept = [(blocks, setup) for blocks, setup in zip(pages, setups) if blocks]
    if not kept:
        kept = [([], final)]
    return [{"n": index, "blocks": blocks, "setup": setup or final} for index, (blocks, setup) in enumerate(kept, start=1)]


def _table(table: ET.Element, counter: dict, stack: list[dict], carry: str = "") -> dict:
    rows = []
    grid = [_px(col.get(f"{W}w")) or 0 for col in table.findall(f"{W}tblGrid/{W}gridCol")]
    borders = _borders(table.find(f"{W}tblPr/{W}tblBorders"))
    above: dict[int, str] = {}
    carry_used = False
    for row in table.findall(f"{W}tr"):
        cells = []
        column = 0
        left = ""
        next_above: dict[int, str] = {}
        row_h = _row_px(row)
        for cell in row.findall(f"{W}tc"):
            span = int(_attr(cell.find(f"{W}tcPr/{W}gridSpan")) or "1")
            width = _span(cell.find(f"{W}tcPr/{W}tcW")) or sum(grid[column:column + span]) or None
            own_borders = _borders(cell.find(f"{W}tcPr/{W}tcBorders"))
            shown = own_borders if own_borders is not None else borders
            merge = cell.find(f"{W}tcPr/{W}vMerge")
            continued = merge is not None and _attr(merge) != "restart"
            neighbor = above.get(column) or left
            chosen = neighbor
            borrowed = False
            if not chosen and carry and not carry_used:
                chosen = carry
                borrowed = True
            counter["boxW"] = width or None
            counter["boxH"] = row_h or 36
            blocks = []
            made = False
            pending = chosen
            if not continued:
                for child in list(cell):
                    if child.tag == f"{W}p" and not made and _open_paragraph(child) and (pending or _boxed(shown)):
                        blocks.append({"type": "p", "items": [_blank_field(counter, pending or "填寫", child)]})
                        made = True
                        if borrowed and not neighbor:
                            carry_used = True
                        pending = ""
                        continue
                    if child.tag == f"{W}p":
                        blocks.extend(_flow_paragraph(child, counter, stack))
                        text = _clean_label(_texts(child))
                        if text:
                            pending = text
                            left = text
                    elif child.tag == f"{W}tbl":
                        blocks.append(_table_block(child, counter, stack, pending or carry))
                for field in _direct_fields(blocks):
                    if chosen and field.get("name") in {"填寫", "欄位"}:
                        field["name"] = chosen
                        if borrowed and not neighbor:
                            carry_used = True
                    if field.get("blank") or _short(field.get("text") or ""):
                        if width:
                            field["boxW"] = width
                        field["boxH"] = row_h or field.get("boxH") or 36
            text = "" if continued else _clean_label(_texts(cell))
            if text:
                left = text
                for offset in range(span):
                    next_above[column + offset] = text
            column += span
            counter["boxW"] = None
            counter["boxH"] = None
            cells.append({
                "blocks": blocks,
                "width": width,
                "span": span,
                "align": _valign(_attr(cell.find(f"{W}tcPr/{W}vAlign"))),
                "fill": _fill(cell.find(f"{W}tcPr/{W}shd")),
                "borders": shown,
            })
        above = next_above
        rows.append(cells)
    table_width = table.find(f"{W}tblPr/{W}tblW")
    indent = table.find(f"{W}tblPr/{W}tblInd")
    return {
        "width": _span(table_width) or (sum(grid) if grid else None),
        "indent": _px(indent.get(f"{W}w") if indent is not None else None) or 0,
        "align": _attr(table.find(f"{W}tblPr/{W}jc")),
        "rows": rows,
    }


def _sdt_blocks(sdt: ET.Element, counter: dict, stack: list[dict], carry: str = "") -> list[dict]:
    props = sdt.find(f"{W}sdtPr")
    content = sdt.find(f"{W}sdtContent")
    if content is None:
        return []
    children = list(content)
    simple = (
        len(children) == 1
        and children[0].tag == f"{W}p"
        and children[0].find(f".//{W}drawing") is None
        and children[0].find(f".//{VML}shape") is None
        and (children[0].find(f"{W}pPr/{W}framePr") is None)
    )
    if simple and props is not None and (_attr(props.find(f"{W}tag")) or _attr(props.find(f"{W}alias"))):
        field = _content_control(sdt, counter, stack)
        return [{"type": "p", "items": [field]}] if field else []
    blocks = []
    label = _attr(props.find(f"{W}tag")) or _attr(props.find(f"{W}alias")) or carry if props is not None else carry
    for child in children:
        if child.tag == f"{W}p":
            blocks.extend(_flow_paragraph(child, counter, stack))
            text = _clean_label(_texts(child))
            if text:
                label = text
        elif child.tag == f"{W}tbl":
            blocks.append(_table_block(child, counter, stack, label))
            label = _attr(props.find(f"{W}tag")) or _attr(props.find(f"{W}alias")) if props is not None else ""
        elif child.tag == f"{W}sdt":
            blocks.extend(_sdt_blocks(child, counter, stack, label))
    return blocks


def _push_all(page: list[dict], blocks: list[dict]) -> None:
    for block in blocks:
        previous = page[-1] if page else None
        if (
            block.get("type") == "frame"
            and previous
            and previous.get("type") == "frame"
            and all(previous.get(key) == block.get(key) for key in ("x", "y", "anchorH", "anchorV", "width", "height"))
        ):
            previous["blocks"].extend(block.get("blocks") or [])
            continue
        page.append(block)


def _flow_paragraph(paragraph: ET.Element, counter: dict, stack: list[dict]) -> list[dict]:
    block = _paragraph_block(paragraph, counter, stack)
    frame = _frame_box(paragraph)
    anchored = _anchored_boxes(paragraph, counter, stack)
    visible = any((item.get("text") or "").strip() or item.get("type") in {"field", "break"} for item in block["items"])
    placed = []
    if frame:
        placed.append({"type": "frame", **frame, "blocks": [block]})
    elif visible or not anchored:
        placed.append(block)
    placed.extend(anchored)
    return placed


def _table_block(table: ET.Element, counter: dict, stack: list[dict], carry: str = "") -> dict:
    data = _table(table, counter, stack, carry)
    place = table.find(f"{W}tblPr/{W}tblpPr")
    if place is not None and (place.get(f"{W}tblpX") is not None or place.get(f"{W}tblpY") is not None):
        return {
            "type": "frame",
            "x": _px(place.get(f"{W}tblpX")) or 0,
            "y": _px(place.get(f"{W}tblpY")) or 0,
            "anchorH": place.get(f"{W}horzAnchor") or "page",
            "anchorV": place.get(f"{W}vertAnchor") or "page",
            "width": data.get("width"),
            "blocks": [{"type": "table", **data}],
        }
    return {"type": "table", **data}


def _frame_box(paragraph: ET.Element) -> dict | None:
    frame = paragraph.find(f"{W}pPr/{W}framePr")
    if frame is None:
        return None
    if all(frame.get(f"{W}{name}") is None for name in ("x", "y", "xAlign", "yAlign")):
        return None
    return {
        "x": _px(frame.get(f"{W}x")) or 0,
        "y": _px(frame.get(f"{W}y")) or 0,
        "width": _px(frame.get(f"{W}w")),
        "height": _px(frame.get(f"{W}h")),
        # Word's own default: horizontal from the page, vertical from the surrounding text.
        "anchorH": frame.get(f"{W}hAnchor") or "page",
        "anchorV": "text" if frame.get(f"{W}yAlign") == "inline" else (frame.get(f"{W}vAnchor") or "text"),
        "alignH": frame.get(f"{W}xAlign") or "",
        "alignV": frame.get(f"{W}yAlign") or "",
    }


def _anchored_boxes(node: ET.Element, counter: dict, stack: list[dict]) -> list[dict]:
    parents = {child: parent for parent in node.iter() for child in list(parent)}

    def in_fallback(element: ET.Element) -> bool:
        current = element
        while current is not None:
            if str(current.tag).endswith("Fallback"):
                return True
            current = parents.get(current)
        return False

    frames = []
    for drawing in node.iter(f"{W}drawing"):
        if in_fallback(drawing):
            continue
        for anchor in list(drawing.iter(f"{WP}anchor")) + list(drawing.iter(f"{WP}inline")):
            content = next(anchor.iter(f"{W}txbxContent"), None)
            if content is None:
                continue
            frames.append(_placed_box(_wp_box(anchor), _content_blocks(content, counter, stack)))
    for shape in node.iter(f"{VML}shape"):
        if in_fallback(shape):
            continue
        content = next(shape.iter(f"{W}txbxContent"), None)
        if content is None:
            continue
        frames.append(_placed_box(_vml_box(shape.get("style") or ""), _content_blocks(content, counter, stack)))
    return frames


def _content_blocks(node: ET.Element, counter: dict, stack: list[dict]) -> list[dict]:
    blocks = []
    for child in list(node):
        if child.tag == f"{W}p":
            blocks.extend(_flow_paragraph(child, counter, stack))
        elif child.tag == f"{W}tbl":
            blocks.append(_table_block(child, counter, stack))
    return blocks


def _placed_box(box: dict, blocks: list[dict]) -> dict:
    placed = {"type": "frame", **box, "blocks": blocks}
    if placed.get("x") is None and placed.get("y") is None and not placed.get("alignH") and not placed.get("alignV"):
        placed["x"] = 0
        placed["y"] = 0
        placed["anchorH"] = placed.get("anchorH") or "text"
        placed["anchorV"] = placed.get("anchorV") or "text"
    return placed


def _wp_box(anchor: ET.Element) -> dict:
    extent = anchor.find(f"{WP}extent")
    box = {
        "width": _emu(extent.get("cx") if extent is not None else None),
        "height": _emu(extent.get("cy") if extent is not None else None),
    }
    if anchor.tag == f"{WP}inline":
        return box
    def axis(name: str):
        node = anchor.find(f"{WP}{name}")
        default = "page" if name == "positionH" else "paragraph"
        if node is None:
            return 0, default, "", None
        relative = node.get("relativeFrom") or default
        offset = node.find(f"{WP}posOffset")
        if offset is not None and (offset.text or "").strip():
            return _emu(offset.text) or 0, relative, "", None
        align = node.find(f"{WP}align")
        if align is not None and (align.text or "").strip():
            return 0, relative, align.text.strip(), None
        for child in list(node):
            if child.tag.endswith("pctPosHOffset") or child.tag.endswith("pctPosVOffset"):
                try:
                    return 0, relative, "", int(child.text) / 100000
                except (TypeError, ValueError):
                    break
        return 0, relative, "", None
    box["x"], box["anchorH"], box["alignH"], box["pctX"] = axis("positionH")
    box["y"], box["anchorV"], box["alignV"], box["pctY"] = axis("positionV")
    return box


def _vml_box(style: str) -> dict:
    parts = {}
    for chunk in style.split(";"):
        if ":" not in chunk:
            continue
        key, value = chunk.split(":", 1)
        parts[key.strip().lower()] = value.strip()

    def length(key: str):
        raw = parts.get(key, "").lower()
        for suffix, scale in (("pt", 96 / 72), ("in", 96), ("cm", 96 / 2.54), ("mm", 96 / 25.4), ("px", 1)):
            if raw.endswith(suffix):
                try:
                    return round(float(raw[:-len(suffix)]) * scale)
                except ValueError:
                    return None
        return None

    absolute = parts.get("position") == "absolute"
    return {
        "x": length("margin-left") if length("margin-left") is not None else length("left"),
        "y": length("margin-top") if length("margin-top") is not None else length("top"),
        "width": length("width"),
        "height": length("height"),
        "anchorH": parts.get("mso-position-horizontal-relative") or ("page" if absolute else "text"),
        "anchorV": parts.get("mso-position-vertical-relative") or ("page" if absolute else "text"),
    }


def _emu(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return round(int(float(value)) / 9525)
    except (TypeError, ValueError):
        return None


def _paragraph_block(paragraph: ET.Element, counter: dict, stack: list[dict]) -> dict:
    props = paragraph.find(f"{W}pPr")
    style_id = _attr(props.find(f"{W}pStyle")) if props is not None else ""
    counter["rbase"] = _resolve(counter["styles"], "r", style_id, _rpr(props.find(f"{W}rPr") if props is not None else None))
    block = {"type": "p", "items": _paragraph(paragraph, counter, stack)}
    block.update(_resolve(counter["styles"], "p", style_id, _ppr(props)))
    return block


def _paragraph(paragraph: ET.Element, counter: dict, stack: list[dict], promote_blanks: bool = True) -> list[dict]:
    items: list[dict] = []

    def add_text(text: str, style: dict | None = None) -> None:
        if not text:
            return
        if stack and stack[-1]["mode"] == "result":
            stack[-1]["result"] += text
            return
        if stack and stack[-1]["mode"] == "instr":
            return
        style = style or {}
        if items and items[-1]["type"] == "text" and items[-1].get("style") == style:
            items[-1]["text"] += text
            return
        item = {"type": "text", "text": text}
        if style:
            item["style"] = style
        items.append(item)

    def walk(node: ET.Element) -> None:
        for child in list(node):
            if child.tag == f"{W}sdt":
                field = _content_control(child, counter, stack)
                if field:
                    items.append(field)
                continue
            if child.tag == f"{W}fldSimple":
                field = _simple_field(child, counter)
                if field:
                    items.append(field)
                continue
            if child.tag == f"{W}r":
                _run(child, counter, stack, items, add_text, node, promote_blanks)
                continue
            if child.tag in {f"{W}hyperlink", f"{W}ins", f"{W}smartTag"}:
                walk(child)

    walk(paragraph)
    return items


def _run(run: ET.Element, counter: dict, stack: list[dict], items: list[dict], add_text, parent: ET.Element, promote_blanks: bool) -> None:
    form_name = _form_field_name(run)
    style = _run_style(run, counter)
    if form_name:
        counter["n"] += 1
        item = {
            "type": "field",
            "id": str(counter["n"]),
            "name": form_name,
            "instr": form_name,
            "text": _texts(run),
            "control": "checkbox" if run.find(f".//{W}checkBox") is not None else "text",
            "checked": False,
        }
        _pin_blank(parent, run, item, counter)
        items.append(item)
        return
    if promote_blanks and not stack and _is_fill_run(run):
        counter["n"] += 1
        shown = _texts(run)
        item = {
            "type": "field",
            "id": str(counter["n"]),
            "name": _blank_name(items),
            "instr": "",
            "text": "" if _short(shown) else shown,
            "control": "text",
            "checked": False,
            "blank": True,
        }
        _pin_blank(parent, run, item, counter)
        items.append(item)
        return
    for child in list(run):
        if child.tag == f"{W}instrText":
            if stack:
                stack[-1]["instr"] += child.text or ""
            continue
        if child.tag == f"{W}fldChar":
            kind = child.get(f"{W}fldCharType")
            if kind == "begin":
                stack.append({"instr": "", "result": "", "mode": "instr"})
            elif kind == "separate" and stack:
                stack[-1]["mode"] = "result"
            elif kind == "end" and stack:
                field = stack.pop()
                item = _field_item(counter, field["instr"], field["result"])
                _pin_blank(parent, run, item, counter)
                items.append(item)
            continue
        if child.tag == f"{W}br":
            if not (stack and stack[-1]["mode"] == "instr"):
                items.append({"type": "break"})
            continue
        if child.tag == f"{W}tab":
            add_text("\t", style)
            continue
        if child.tag == f"{W}t":
            add_text(child.text or "", style)


def _content_control(sdt: ET.Element, counter: dict, stack: list[dict]) -> dict | None:
    props = sdt.find(f"{W}sdtPr")
    content = sdt.find(f"{W}sdtContent")
    if props is None or content is None:
        return None
    name = _attr(props.find(f"{W}tag")) or _attr(props.find(f"{W}alias")) or "欄位"
    checkbox = _is_checkbox(props)
    parts = []
    for child in list(content):
        if child.tag == f"{W}p":
            parts.extend(_paragraph(child, counter, stack, promote_blanks=False))
        elif child.tag == f"{W}r":
            parts.append({"type": "text", "text": _texts(child)})
    shown = "".join(part.get("text") or "" for part in parts if part.get("type") == "text")
    counter["n"] += 1
    item = {
        "type": "field",
        "id": str(counter["n"]),
        "name": name,
        "instr": name,
        "text": shown,
        "control": "checkbox" if checkbox else "text",
        "checked": checkbox and _checkbox_on(props),
    }
    if _short(shown) and content is not None:
        _prepend_content(content, item["id"])
        item["blank"] = True
        _attach_box(counter, item)
    return item


def _simple_field(field: ET.Element, counter: dict) -> dict:
    item = _field_item(counter, field.get(f"{W}instr") or "", _texts(field))
    _pin_blank(field, None, item, counter)
    return item


def _field_item(counter: dict, instr: str, shown: str) -> dict:
    counter["n"] += 1
    name = _parameter_name(instr)
    checkbox = "FORMCHECKBOX" in instr.upper()
    return {
        "type": "field",
        "id": str(counter["n"]),
        "name": name,
        "instr": re.sub(r"\s+", " ", instr).strip(),
        "text": shown,
        "control": "checkbox" if checkbox else "text",
        "checked": False,
    }


def _parameter_name(instr: str) -> str:
    match = _INSTR.search(re.sub(r"\s+", " ", instr).strip())
    if not match:
        token = re.sub(r"\s+", " ", instr).strip()
        return token[:80] or "欄位"
    return match.group(2).strip("\"'")


def _form_field_name(run: ET.Element) -> str:
    name = run.find(f".//{W}ffData/{W}name")
    return _attr(name)


def _texts(node: ET.Element) -> str:
    return "".join(item.text or "" for item in node.iter(f"{W}t"))


def _short(text: str) -> bool:
    return len("".join((text or "").split())) < 2


def _clean_label(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip(" \t:：")
    token = re.search(r"[A-Za-z][A-Za-z0-9_.]{2,}", text)
    if token:
        return token.group(0)
    return text[-40:]


def _row_px(row: ET.Element) -> int | None:
    node = row.find(f"{W}trPr/{W}trHeight")
    if node is None:
        return None
    return _px(node.get(f"{W}val"))


def _boxed(borders: list[bool] | None) -> bool:
    return bool(borders) and any(borders)


def _open_paragraph(paragraph: ET.Element) -> bool:
    if paragraph.find(f".//{W}sdt") is not None or paragraph.find(f".//{W}fldChar") is not None or paragraph.find(f".//{W}fldSimple") is not None:
        return False
    if any(_is_fill_run(run) for run in paragraph.findall(f"{W}r")):
        return False
    return _short(_texts(paragraph))


def _blank_field(counter: dict, name: str, paragraph: ET.Element) -> dict:
    counter["n"] += 1
    item = {
        "type": "field",
        "id": str(counter["n"]),
        "name": name or "填寫",
        "instr": "",
        "text": "",
        "control": "text",
        "checked": False,
        "blank": True,
    }
    _attach_box(counter, item)
    props = paragraph.find(f"{W}pPr")
    index = list(paragraph).index(props) + 1 if props is not None else 0
    paragraph.insert(index, _marker_run(item["id"]))
    return item


def _direct_fields(blocks: list[dict]) -> list[dict]:
    found = []
    for block in blocks:
        if block.get("type") in {"table", "frame"}:
            continue
        for item in block.get("items") or []:
            if item.get("type") == "field":
                found.append(item)
    return found


def _attach_box(counter: dict, item: dict) -> None:
    if not item.get("blank"):
        return
    if counter.get("boxW"):
        item["boxW"] = counter["boxW"]
    if counter.get("boxH"):
        item["boxH"] = counter["boxH"]


def _is_fill_run(run: ET.Element) -> bool:
    """An underline, tab, or underscore run that is a blank to write in."""
    if _form_field_name(run) or run.find(f"{W}fldChar") is not None or run.find(f"{W}instrText") is not None:
        return False
    text = _texts(run)
    if text and any(ch not in " \t\u3000_＿.．·・—–-" for ch in text):
        return False
    underscores = sum(ch in "_＿" for ch in text)
    spaces = sum(ch.isspace() or ch == "\u3000" for ch in text)
    if underscores >= 3:
        return True
    underline = run.find(f"{W}rPr/{W}u")
    underlined = underline is not None and _attr(underline) != "none"
    has_tab = run.find(f"{W}tab") is not None
    return underlined and (has_tab or spaces >= 2 or underscores >= 2)


def _blank_name(items: list[dict]) -> str:
    for item in reversed(items):
        if item.get("type") != "text":
            continue
        label = re.sub(r"[\s:：]+$", "", (item.get("text") or "").strip())
        if label:
            return label[-40:]
    return "填寫"


def _marker_run(fid: str) -> ET.Element:
    run = ET.Element(f"{W}r")
    props = ET.SubElement(run, f"{W}rPr")
    size = ET.SubElement(props, f"{W}sz")
    size.set(f"{W}val", "22")
    size_cs = ET.SubElement(props, f"{W}szCs")
    size_cs.set(f"{W}val", "22")
    color = ET.SubElement(props, f"{W}color")
    color.set(f"{W}val", "FFFFFF")
    text = ET.SubElement(run, f"{W}t")
    text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text.text = f"~{{{fid}}}~________"
    return run


def _pin_blank(parent: ET.Element, child: ET.Element | None, item: dict, counter: dict | None = None) -> None:
    if not _short(item.get("text") or ""):
        return
    item["blank"] = True
    marker = _marker_run(item["id"])
    if child is None:
        parent.insert(0, marker)
    else:
        kids = list(parent)
        if child in kids:
            parent.insert(kids.index(child), marker)
    if counter is not None:
        _attach_box(counter, item)


def _prepend_content(content: ET.Element, fid: str) -> None:
    paragraph = next((child for child in content if child.tag == f"{W}p"), None)
    if paragraph is None:
        paragraph = ET.Element(f"{W}p")
        content.insert(0, paragraph)
    props = paragraph.find(f"{W}pPr")
    index = list(paragraph).index(props) + 1 if props is not None else 0
    paragraph.insert(index, _marker_run(fid))


def _register_namespaces() -> None:
    for prefix, uri in {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
        "v": "urn:schemas-microsoft-com:vml",
        "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    }.items():
        ET.register_namespace(prefix, uri)


def _swap_document(raw: bytes, document_xml: bytes) -> bytes:
    from io import BytesIO
    incoming = BytesIO(raw)
    outgoing = BytesIO()
    with zipfile.ZipFile(incoming) as source, zipfile.ZipFile(outgoing, "w") as target:
        for info in source.infolist():
            data = document_xml if info.filename == "word/document.xml" else source.read(info.filename)
            target.writestr(info, data)
    return outgoing.getvalue()


def _attr(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return str(node.get(f"{W}val") or "")


def _is_checkbox(props: ET.Element) -> bool:
    return any(child.tag.endswith("checkbox") or child.tag.endswith("checkBox") for child in props.iter())


def _run_style(run: ET.Element, counter: dict) -> dict:
    direct = _rpr(run.find(f"{W}rPr"))
    style_id = _attr(run.find(f"{W}rPr/{W}rStyle"))
    base = dict(counter.get("rbase") or {})
    if style_id:
        base.update(_resolve(counter["styles"], "r", style_id, {}))
    base.update(direct)
    return {key: value for key, value in base.items() if value not in (None, False, "")}


def _styles(xml: bytes | None) -> dict:
    empty = {"defaults": {"p": {}, "r": {}}, "styles": {}}
    if not xml:
        return empty
    root = ET.fromstring(xml)
    defaults = {"p": {}, "r": {}}
    doc = root.find(f"{W}docDefaults")
    if doc is not None:
        defaults["r"] = _rpr(doc.find(f"{W}rPrDefault/{W}rPr"))
        defaults["p"] = _ppr(doc.find(f"{W}pPrDefault/{W}pPr"))
    styles = {}
    for style in root.findall(f"{W}style"):
        sid = style.get(f"{W}styleId")
        if not sid:
            continue
        styles[sid] = {
            "basedOn": _attr(style.find(f"{W}basedOn")),
            "p": _ppr(style.find(f"{W}pPr")),
            "r": _rpr(style.find(f"{W}rPr")),
        }
    return {"defaults": defaults, "styles": styles}


def _resolve(pack: dict, kind: str, style_id: str, direct: dict) -> dict:
    merged = dict((pack or {}).get("defaults", {}).get(kind) or {})
    chain = []
    seen = set()
    current = style_id
    styles = (pack or {}).get("styles") or {}
    while current and current not in seen and current in styles:
        seen.add(current)
        chain.append(current)
        current = styles[current]["basedOn"]
    for sid in reversed(chain):
        merged.update(styles[sid][kind])
    merged.update(direct)
    return {key: value for key, value in merged.items() if value not in (None, False, "")}


def _ppr(node: ET.Element | None) -> dict:
    if node is None:
        return {}
    found = {}
    align = {"both": "justify", "center": "center", "right": "right", "left": "left"}.get(_attr(node.find(f"{W}jc")))
    if align:
        found["align"] = align
    spacing = node.find(f"{W}spacing")
    if spacing is not None:
        for key, attr in (("before", "before"), ("after", "after")):
            value = _px(spacing.get(f"{W}{attr}"))
            if value:
                found[key] = value
        raw_line = spacing.get(f"{W}line")
        rule = spacing.get(f"{W}lineRule") or "auto"
        if raw_line and str(raw_line).isdigit():
            found["lineRule"] = rule
            found["line"] = round(int(raw_line) / 240, 3) if rule == "auto" else _px(raw_line)
    indent = node.find(f"{W}ind")
    if indent is not None:
        for key, attr in (("left", "left"), ("right", "right"), ("first", "firstLine"), ("hanging", "hanging")):
            value = _px(indent.get(f"{W}{attr}"))
            if value:
                found[key] = value
    return found


def _rpr(node: ET.Element | None) -> dict:
    if node is None:
        return {}
    found = {}
    bold = node.find(f"{W}b")
    if bold is not None:
        found["bold"] = _attr(bold) != "0"
    italic = node.find(f"{W}i")
    if italic is not None:
        found["italic"] = _attr(italic) != "0"
    if node.find(f"{W}u") is not None and _attr(node.find(f"{W}u")) != "none":
        found["underline"] = True
    size = _attr(node.find(f"{W}sz")) or _attr(node.find(f"{W}szCs"))
    if size.isdigit():
        found["size"] = int(size) / 2
    fonts = node.find(f"{W}rFonts")
    if fonts is not None:
        face = fonts.get(f"{W}eastAsia") or fonts.get(f"{W}ascii") or fonts.get(f"{W}hAnsi")
        if face:
            found["font"] = face
    color = _attr(node.find(f"{W}color"))
    if color and color != "auto":
        found["color"] = color
    return found


def _page_setup(section: ET.Element | None) -> dict:
    width, height = 794, 1123
    margin = [96, 96, 96, 96]
    if section is not None:
        size = section.find(f"{W}pgSz")
        if size is not None:
            width = _px(size.get(f"{W}w"), width)
            height = _px(size.get(f"{W}h"), height)
        edges = section.find(f"{W}pgMar")
        if edges is not None:
            margin = [_px(edges.get(f"{W}{side}"), 96) for side in ("top", "right", "bottom", "left")]
    return {"width": width, "height": height, "padding": f"{margin[0]}px {margin[1]}px {margin[2]}px {margin[3]}px"}


def _borders(node: ET.Element | None) -> list[bool] | None:
    if node is None:
        return None
    shown = []
    for side in ("top", "left", "bottom", "right"):
        edge = node.find(f"{W}{side}")
        value = _attr(edge) if edge is not None else ""
        shown.append(value not in {"", "nil", "none"})
    return shown


def _valign(value: str) -> str:
    return {"center": "middle", "bottom": "bottom"}.get(value, "top")


def _fill(node: ET.Element | None) -> str:
    paint = node.get(f"{W}fill") if node is not None else ""
    if not paint or paint in {"auto", "nil"}:
        return ""
    return paint


def _span(node: ET.Element | None):
    """A width that is a real length. Percentages stay unset so the grid can decide."""
    if node is None:
        return None
    if (node.get(f"{W}type") or "dxa") != "dxa":
        return None
    return _px(node.get(f"{W}w"))


def _px(value, default=None):
    if value in (None, ""):
        return default
    try:
        return round(int(value) / 15)
    except (TypeError, ValueError):
        return default


def _checkbox_on(props: ET.Element) -> bool:
    for child in props.iter():
        if child.tag.endswith("checked"):
            return str(child.get(f"{W}val") or child.get("{http://schemas.microsoft.com/office/word/2010/wordml}val") or "") in {"1", "true"}
    return False
