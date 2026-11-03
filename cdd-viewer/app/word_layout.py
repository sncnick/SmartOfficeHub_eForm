"""Draw a Word file the way a document engine lays it out.

The preview used to place text boxes from their stored offsets. Word then
wraps, paginates, and anchors those boxes, so the page drifted. This module
asks LibreOffice for the finished pages and only uses the picture as the
page. Field names still come from the docx; their click targets sit on the
words the engine actually drew.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import zlib
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
from io import BytesIO

from .word_preview import WordPreviewError, marked_docx

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MARKER = re.compile(r"~\{(\d+)\}~_*")
_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "v": "urn:schemas-microsoft-com:vml",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
}
_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}


class WordLayoutError(Exception):
    """The document cannot be drawn with the layout engine."""


def collect_fields(document: dict) -> list[dict]:
    found: list[dict] = []
    for page in document.get("pages") or []:
        _walk(page.get("blocks") or [], found)
    return found


def place_fields(fields: list[dict], pages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pin each field on its words, or on the locator written into a blank."""
    pieces: list[tuple[str, int, dict]] = []
    for page in pages:
        for word in page.get("words") or []:
            for ch in _compact(word.get("text") or ""):
                pieces.append((ch, page["n"], word))
    haystack = "".join(ch for ch, _, _ in pieces)
    used = [False] * len(pieces)
    markers: dict[str, tuple[int, dict]] = {}
    cursor = 0
    while cursor < len(pieces):
        word = pieces[cursor][2]
        end = cursor + 1
        while end < len(pieces) and pieces[end][2] is word:
            end += 1
        chunk = haystack[cursor:end]
        match = _MARKER.search(chunk)
        if match:
            markers[match.group(1)] = (pieces[cursor][1], {
                "x": word["x"], "y": word["y"], "w": word["w"], "h": word["h"],
            })
            for offset in range(cursor + match.start(), cursor + match.end()):
                used[offset] = True
        cursor = end
    placed: dict[int, list[dict]] = {page["n"]: [] for page in pages}
    loose: list[dict] = []
    for field in fields:
        needle = _compact(field.get("text") or "")
        idx = _find_unused(haystack, used, needle) if len(needle) >= 2 else -1
        if idx >= 0:
            for offset in range(idx, idx + len(needle)):
                used[offset] = True
            page_n = pieces[idx][1]
            words = _span_words(pieces, idx, idx + len(needle), page_n)
            if words:
                placed[page_n].append(_public_field(field, _union([(page_n, word) for word in words])))
                continue
        marker = markers.get(str(field.get("id")))
        if marker is None:
            loose.append(_public_field(field))
            continue
        page_n, box = marker
        placed[page_n].append(_public_field(field, box, blank=True))
    public = [
        {"n": page["n"], "width": page["width"], "height": page["height"], "fields": placed[page["n"]]}
        for page in pages
    ]
    return public, loose


def blank_gaps(pages: list[dict]) -> dict[int, list[dict]]:
    """A wide space between two words is the blank cell the reader can click."""
    found: dict[int, list[dict]] = {}
    for page in pages:
        words = [word for word in (page.get("words") or []) if (word.get("text") or "").strip()]
        gaps = []
        if len(words) >= 2:
            heights = sorted(word["h"] for word in words)
            line_h = heights[len(heights) // 2] or 14
            lines: list[list[dict]] = []
            for word in sorted(words, key=lambda item: (item["y"], item["x"])):
                if lines and abs(word["y"] - lines[-1][0]["y"]) <= max(4, line_h * 0.6):
                    lines[-1].append(word)
                else:
                    lines.append([word])
            for line in lines:
                line.sort(key=lambda item: item["x"])
                for left, right in zip(line, line[1:]):
                    start = left["x"] + left["w"]
                    gap = right["x"] - start
                    if gap < max(28, line_h * 2):
                        continue
                    label = " ".join((left.get("text") or "").split())[-40:] or "填寫"
                    gaps.append({
                        "id": f"gap-{page['n']}-{len(gaps) + 1}",
                        "name": label,
                        "text": "",
                        "control": "text",
                        "checked": False,
                        "blank": True,
                        "x": start,
                        "y": left["y"],
                        "w": gap,
                        "h": left["h"],
                    })
        found[page["n"]] = gaps
    return found


def page_returns(raw: bytes, pages: list[dict]) -> dict[int, list[dict]]:
    """Return marks for every line, every blank line, and every drawn frame.

    A frame that contains no text still gets a return inside the box that was drawn.
    A table that is only an underline is placed on that drawn line, not under the last word.
    """
    visual = _visual_returns(pages)
    anchored = paragraph_enters(raw, pages)
    specs = _collect_frame_specs(raw)
    _table_marks, table_cells = _table_returns(raw, pages)
    blank_tables = _blank_table_marks(raw, pages)
    pictured = _fields_on_drawn_lines(raw, pages)
    used: set[int] = set()
    found = {}
    for page in pages:
        marks = _combine_returns(page["n"], visual.get(page["n"], []), anchored.get(page["n"], []))
        marks.extend(_returns_for_frames(page, specs, used, marks))
        drawn = _returns_for_table_cells(page, [])
        snapped = pictured.get(page["n"], [])
        if drawn or snapped:
            page["cells"] = _table_grid_cells(page.get("frames") or [], page.get("width") or 794, page.get("height") or 1123)
            page["cells"] = list(page["cells"]) + [mark for mark in snapped if not any(_boxes_overlap(mark, cell) for cell in page["cells"])]
            marks = [mark for mark in marks if not mark.get("table")]
            marks.extend(drawn)
            marks.extend(mark for mark in snapped if not any(other.get("table") and _covers_same_area(mark, other) for other in marks))
        else:
            page["cells"] = table_cells.get(page["n"], [])
            marks = _keep_table_returns(marks, page["cells"], _table_marks.get(page["n"], []))
            marks.extend(blank_tables.get(page["n"], []))
        marks = [
            mark for mark in marks
            if mark.get("table") or not any(other.get("table") and _boxes_overlap(mark, other) for other in marks)
        ]
        for index, item in enumerate(marks, start=1):
            item["id"] = f"enter-{page['n']}-{index}"
        found[page["n"]] = marks
    return found


def _visual_returns(pages: list[dict]) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for page in pages:
        words = [word for word in (page.get("words") or []) if (word.get("text") or "").strip()]
        marks = []
        if words:
            heights = sorted(word["h"] for word in words)
            line_h = heights[len(heights) // 2] or 14
            lines: list[list[dict]] = []
            for word in sorted(words, key=lambda item: (item["y"], item["x"])):
                if lines and abs(word["y"] - lines[-1][0]["y"]) <= max(4, line_h * 0.6):
                    lines[-1].append(word)
                else:
                    lines.append([word])
            gap_at = max(20, line_h)
            for index, line in enumerate(lines):
                line.sort(key=lambda item: item["x"])
                for cursor, word in enumerate(line):
                    nxt = line[cursor + 1] if cursor + 1 < len(line) else None
                    end = word["x"] + word["w"]
                    if nxt is not None and nxt["x"] - end < gap_at:
                        continue
                    marks.append(_enter_icon(page["n"], len(marks) + 1, end, word["y"], word["h"], "↵"))
                if index + 1 >= len(lines):
                    continue
                bottom = max(item["y"] + item["h"] for item in line)
                below = min(item["y"] for item in lines[index + 1])
                gap = below - bottom
                if gap < line_h * 0.8 or gap > line_h * 2.6:
                    continue
                left = min(item["x"] for item in line + lines[index + 1])
                height = line[-1]["h"]
                y = round(bottom + max(0, (gap - height) / 2))
                if y < below:
                    marks.append(_enter_icon(page["n"], len(marks) + 1, left, y, height, "↵"))
        found[page["n"]] = marks
    return found


def _combine_returns(page_n: int, visual: list[dict], anchored: list[dict]) -> list[dict]:
    kept = [dict(item) for item in visual]
    for extra in anchored:
        same = [
            item for item in kept
            if abs(item["y"] - extra["y"]) <= max(6, (item.get("h") or 14) * 0.75)
        ]
        if not same:
            added = dict(extra)
            added["mark"] = "↵"
            kept.append(added)
            continue
        host = min(same, key=lambda item: abs(item["x"] - extra["x"]))
        if extra["x"] > host["x"] + 3:
            host["x"] = extra["x"]
    for index, item in enumerate(kept, start=1):
        item["id"] = f"enter-{page_n}-{index}"
        item["enter"] = True
        item["mark"] = item.get("mark") or "↵"
    return kept


def _page_frame_rects(pdf: Path, count: int) -> dict[int, list[dict]]:
    """Rectangles actually stroked on each finished page, in CSS pixels."""
    found = {number: [] for number in range(1, count + 1)}
    tool = shutil.which("pdftocairo")
    if tool is None:
        return found
    try:
        with tempfile.TemporaryDirectory(prefix="cdd-frame-") as folder:
            root = Path(folder)
            subprocess.run(
                [tool, "-svg", str(pdf), str(root / "page.svg")],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            files = sorted(root.glob("page*.svg"))
            if len(files) == 1 and count == 1:
                found[1] = _rects_from_svg(files[0].read_text(errors="replace"))
                return found
            for path in files:
                match = re.search(r"-(\d+)\.svg$", path.name)
                number = int(match.group(1)) if match else (1 if count == 1 else 0)
                if number in found:
                    found[number] = _rects_from_svg(path.read_text(errors="replace"))
    except (OSError, subprocess.TimeoutExpired):
        return {number: [] for number in range(1, count + 1)}
    return found


def _rects_from_svg(svg: str) -> list[dict]:
    view = re.search(r'viewBox="\s*[\d.]+\s+[\d.]+\s+[\d.]+\s+([\d.]+)"', svg)
    if view is None:
        return []
    page_h = float(view.group(1))
    svg = re.sub(r"<defs\b.*?</defs>", "", svg, flags=re.S)
    horiz: list[tuple[float, float, float]] = []
    vert: list[tuple[float, float, float]] = []
    boxes: list[dict] = []
    for attrs in re.findall(r"<path\b([^>]*)>", svg, flags=re.S):
        compact = attrs.replace(" ", "")
        stroked = "stroke" in attrs and "stroke:none" not in compact and 'stroke="none"' not in attrs
        drawing = re.search(r'\bd="([^"]+)"', attrs)
        if drawing is None:
            continue
        nums = [float(item) for item in re.findall(r"-?\d+(?:\.\d+)?", drawing.group(1))]
        points = list(zip(nums[0::2], nums[1::2]))
        if len(points) < 2:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        if stroked and max(xs) - min(xs) > 12 and max(ys) - min(ys) > 8:
            boxes.append(_pt_box(min(xs), min(ys), max(xs), max(ys), page_h))
        for start, end in zip(points, points[1:]):
            if abs(start[1] - end[1]) <= 1.5 and abs(start[0] - end[0]) >= 12:
                horiz.append((min(start[0], end[0]), max(start[0], end[0]), (start[1] + end[1]) / 2))
            elif abs(start[0] - end[0]) <= 1.5 and abs(start[1] - end[1]) >= 8:
                vert.append((min(start[1], end[1]), max(start[1], end[1]), (start[0] + end[0]) / 2))
    boxes.extend(_join_rectangles(horiz, vert, page_h))
    boxes.extend(_cells_from_rules(horiz, vert, page_h))
    return _prefer_inner(_dedupe_rects(boxes))


def _pt_box(x0: float, y0: float, x1: float, y1: float, page_h: float) -> dict:
    scale = 96 / 72
    left, right = sorted((x0, x1))
    low, high = sorted((y0, y1))
    return {
        "x": round(left * scale),
        "y": round((page_h - high) * scale),
        "w": max(1, round((right - left) * scale)),
        "h": max(1, round((high - low) * scale)),
    }


def _join_rectangles(horiz: list[tuple[float, float, float]], vert: list[tuple[float, float, float]], page_h: float) -> list[dict]:
    found = []
    for index, top in enumerate(horiz):
        for bottom in horiz[index + 1:]:
            if abs(top[0] - bottom[0]) > 3 or abs(top[1] - bottom[1]) > 3:
                continue
            y0, y1 = sorted((top[2], bottom[2]))
            if y1 - y0 < 8:
                continue
            left = any(abs(item[2] - top[0]) <= 3 and item[0] <= y0 + 3 and item[1] >= y1 - 3 for item in vert)
            right = any(abs(item[2] - top[1]) <= 3 and item[0] <= y0 + 3 and item[1] >= y1 - 3 for item in vert)
            if left and right:
                found.append(_pt_box(top[0], y0, top[1], y1, page_h))
    return found


def _cells_from_rules(horiz: list[tuple[float, float, float]], vert: list[tuple[float, float, float]], page_h: float) -> list[dict]:
    """Split a table grid into one box per cell.

    Only vertical rules that cross a horizontal band are used, so a border
    from the table above does not slice the row underneath.
    """
    ys = _cluster([item[2] for item in horiz])
    cells = []
    for low, high in zip(ys, ys[1:]):
        if high - low < 8:
            continue
        spanning = [item for item in vert if item[0] <= low + 8 and item[1] >= high - 8]
        xs = _cluster([item[2] for item in spanning])
        for left, right in zip(xs, xs[1:]):
            if right - left < 8:
                continue
            if not (_rule_covers(horiz, left, right, high) and _rule_covers(horiz, left, right, low)):
                continue
            cells.append(_pt_box(left, low, right, high, page_h))
    return cells


def _cluster(values: list[float], tolerance: float = 2.0) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    groups = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _rule_covers(horiz: list[tuple[float, float, float]], x0: float, x1: float, y: float, tolerance: float = 4.0) -> bool:
    pieces = [(start, end) for start, end, line in horiz if abs(line - y) <= tolerance and end >= x0 - tolerance and start <= x1 + tolerance]
    cursor = x0
    for start, end in sorted(pieces):
        if start > cursor + 8:
            return False
        cursor = max(cursor, end)
        if cursor >= x1 - 8:
            return True
    return cursor >= x1 - 8


def _rule_covers_vertical(vert: list[tuple[float, float, float]], y0: float, y1: float, x: float, tolerance: float = 4.0) -> bool:
    pieces = [(start, end) for start, end, line in vert if abs(line - x) <= tolerance and end >= y0 - tolerance and start <= y1 + tolerance]
    cursor = y0
    for start, end in sorted(pieces):
        if start > cursor + 8:
            return False
        cursor = max(cursor, end)
        if cursor >= y1 - 8:
            return True
    return cursor >= y1 - 8


def _returns_for_table_cells(page: dict, marks: list[dict]) -> list[dict]:
    """An empty table cell still has Word's return at its left margin."""
    words = [word for word in (page.get("words") or []) if (word.get("text") or "").strip()]
    added = []
    for rect in _table_grid_cells(page.get("frames") or [], page.get("width") or 794, page.get("height") or 1123):
        inside = [word for word in words if _word_in_rect(word, rect)]
        if inside:
            line_y = max(word["y"] for word in inside)
            line = [word for word in inside if abs(word["y"] - line_y) <= 4]
            last = max(line, key=lambda word: word["x"] + word["w"])
            x = last["x"] + last["w"]
            w = rect["x"] + rect["w"] - x
            if w < 28:
                continue
            added.append(_table_field(page["n"], len(added) + 1, x, rect["y"], w, rect["h"]))
        else:
            added.append(_table_field(page["n"], len(added) + 1, rect["x"], rect["y"], rect["w"], rect["h"]))
    return added


def _table_grid_cells(rects: list[dict], page_w: int, page_h: int) -> list[dict]:
    usable = [rect for rect in rects if _usable_frame(rect, page_w, page_h) and rect["h"] <= 160 and rect["w"] >= 36]
    grouped = [rect for rect in usable if any(other is not rect and _shares_edge(rect, other) for other in usable)]
    alone = [
        rect for rect in usable
        if rect not in grouped and rect["w"] >= 80 and 14 <= rect["h"] <= 80
    ]
    return grouped + alone


def _shares_edge(left: dict, right: dict) -> bool:
    if abs((left["x"] + left["w"]) - right["x"]) <= 3 or abs((right["x"] + right["w"]) - left["x"]) <= 3:
        overlap = min(left["y"] + left["h"], right["y"] + right["h"]) - max(left["y"], right["y"])
        if overlap > 8:
            return True
    if abs((left["y"] + left["h"]) - right["y"]) <= 3 or abs((right["y"] + right["h"]) - left["y"]) <= 3:
        overlap = min(left["x"] + left["w"], right["x"] + right["w"]) - max(left["x"], right["x"])
        if overlap > 8:
            return True
    return False


def _mark_visible_in_cell(rect: dict, mark: dict) -> bool:
    size = min(mark.get("h") or 12, 14)
    return (
        rect["x"] <= mark["x"] <= rect["x"] + rect["w"] - 4
        and rect["y"] <= mark["y"]
        and mark["y"] + size <= rect["y"] + rect["h"] + 2
    )


def _word_in_rect(word: dict, rect: dict) -> bool:
    overlap_w = min(word["x"] + word["w"], rect["x"] + rect["w"]) - max(word["x"], rect["x"])
    overlap_h = min(word["y"] + word["h"], rect["y"] + rect["h"]) - max(word["y"], rect["y"])
    if overlap_w <= 0 or overlap_h <= 0:
        return False
    return overlap_w * overlap_h >= 0.6 * word["w"] * word["h"]


def _dedupe_rects(rects: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for rect in rects:
        if any(
            abs(rect["x"] - old["x"]) <= 4
            and abs(rect["y"] - old["y"]) <= 4
            and abs(rect["w"] - old["w"]) <= 6
            and abs(rect["h"] - old["h"]) <= 6
            for old in kept
        ):
            continue
        kept.append(rect)
    return kept


def _prefer_inner(rects: list[dict]) -> list[dict]:
    """Drop a table outline when the cells inside it fill that outline."""
    kept = []
    for rect in rects:
        inners = [other for other in rects if other is not rect and _rect_contains(rect, other)]
        if not inners:
            kept.append(rect)
            continue
        inner_area = sum(other["w"] * other["h"] for other in inners)
        if inner_area > rect["w"] * rect["h"] * 0.45:
            continue
        kept.append(rect)
    return kept


def _rect_contains(rect: dict, other: dict) -> bool:
    return (
        other["x"] >= rect["x"] - 2
        and other["y"] >= rect["y"] - 2
        and other["x"] + other["w"] <= rect["x"] + rect["w"] + 2
        and other["y"] + other["h"] <= rect["y"] + rect["h"] + 2
        and other["w"] * other["h"] < rect["w"] * rect["h"] * 0.9
    )


def _collect_frame_specs(raw: bytes) -> list[dict]:
    try:
        with ZipFile(BytesIO(raw)) as package:
            root = ET.fromstring(package.read("word/document.xml"))
    except (BadZipFile, ET.ParseError, OSError, ValueError, KeyError):
        return []
    found: list[dict] = []
    body = root.find(f"{W}body")
    _walk_frame_blocks(body if body is not None else root, found)
    found.extend(_textbox_specs(root))
    return [spec for spec in found if spec["w"] >= 24 and spec["h"] >= 12 and spec["paras"] >= 1]


def _walk_frame_blocks(node: ET.Element | None, found: list[dict]) -> None:
    if node is None:
        return
    children = list(node)
    index = 0
    while index < len(children):
        child = children[index]
        name = _tag(child)
        key = _frame_key(child) if name == "p" else None
        if key and any(key):
            group = [child]
            index += 1
            while index < len(children) and _tag(children[index]) == "p" and _frame_key(children[index]) == key:
                group.append(children[index])
                index += 1
            found.append(_spec_from_frame(group))
            continue
        if name == "tbl":
            for row in child.findall(f"{W}tr"):
                for cell in row.findall(f"{W}tc"):
                    _walk_frame_blocks(cell, found)
        elif name == "sdt":
            _walk_frame_blocks(child.find(f"{W}sdtContent"), found)
        elif name in {"customXml", "smartTag"}:
            _walk_frame_blocks(child, found)
        index += 1


def _frame_key(paragraph: ET.Element) -> tuple[str, ...] | None:
    frame = paragraph.find(f"{W}pPr/{W}framePr")
    if frame is None:
        return None
    return tuple(frame.get(f"{W}{name}") or "" for name in ("x", "y", "w", "h", "hAnchor", "vAnchor"))


def _spec_from_frame(group: list[ET.Element]) -> dict:
    frame = group[0].find(f"{W}pPr/{W}framePr")
    spec = {
        "paras": len(group),
        "w": _twip_px(frame.get(f"{W}w") or 0),
        "h": _twip_px(frame.get(f"{W}h") or 0),
    }
    anchor_h = frame.get(f"{W}hAnchor") or "page"
    anchor_v = frame.get(f"{W}vAnchor") or "text"
    if frame.get(f"{W}x") is not None and frame.get(f"{W}y") is not None and anchor_h == "page" and anchor_v == "page":
        spec["x"] = _twip_px(frame.get(f"{W}x"))
        spec["y"] = _twip_px(frame.get(f"{W}y"))
    return spec


def _textbox_specs(root: ET.Element) -> list[dict]:
    specs = []
    for anchor in root.iter():
        if _tag(anchor) not in {"anchor", "inline"}:
            continue
        extent = next((child for child in list(anchor) if _tag(child) == "extent"), None)
        content = next((node for node in anchor.iter() if _tag(node) == "txbxContent"), None)
        if extent is None or content is None:
            continue
        try:
            width = round(int(extent.get("cx") or 0) / 9525)
            height = round(int(extent.get("cy") or 0) / 9525)
        except ValueError:
            continue
        paras = sum(1 for node in content.iter() if _tag(node) == "p")
        spec = {"paras": max(1, paras), "w": width, "h": height}
        origin = _page_offset(anchor)
        if origin is not None:
            spec["x"], spec["y"] = origin
        specs.append(spec)
    return specs


def _page_offset(anchor: ET.Element) -> tuple[int, int] | None:
    across = down = None
    for child in list(anchor):
        name = _tag(child)
        offset = next((node for node in child.iter() if _tag(node) == "posOffset"), None)
        if offset is None or not offset.text:
            continue
        try:
            pixels = round(int(offset.text) / 9525)
        except ValueError:
            continue
        if name == "positionH" and (child.get("relativeFrom") or "") == "page":
            across = pixels
        elif name == "positionV" and (child.get("relativeFrom") or "") == "page":
            down = pixels
    if across is None or down is None:
        return None
    return across, down


def _returns_for_frames(page: dict, specs: list[dict], used: set[int], marks: list[dict]) -> list[dict]:
    page_w = page.get("width") or 794
    page_h = page.get("height") or 1123
    rects = [rect for rect in (page.get("frames") or []) if _usable_frame(rect, page_w, page_h)]
    heights = [word["h"] for word in (page.get("words") or []) if word.get("h")]
    line_h = (sorted(heights)[len(heights) // 2] if heights else 16) or 16
    added: list[dict] = []
    for index, spec in enumerate(specs):
        if index in used or spec["w"] <= 0 or spec["h"] <= 0:
            continue
        match = _best_frame_rect(spec, rects)
        if match is None:
            continue
        used.add(index)
        rects.remove(match)
        added.extend(_fill_frame(match, spec["paras"], line_h, marks + added))
    for rect in rects:
        if _frame_contains_word(rect, page.get("words") or []) or _frame_contains_mark(rect, marks + added):
            continue
        added.extend(_fill_frame(rect, 1, line_h, marks + added))
    for index, spec in enumerate(specs):
        if index in used or spec.get("x") is None:
            continue
        if spec["y"] < 0 or spec["y"] >= page_h:
            continue
        box = {"x": spec["x"], "y": spec["y"], "w": spec["w"], "h": spec["h"]}
        used.add(index)
        if _frame_contains_mark(box, marks + added):
            continue
        added.extend(_fill_frame(box, spec["paras"], line_h, marks + added))
    return added


def _usable_frame(rect: dict, page_w: int, page_h: int) -> bool:
    if rect["w"] < 24 or rect["h"] < 12:
        return False
    if rect["w"] > page_w * 0.92 and rect["h"] > page_h * 0.92:
        return False
    return rect["w"] * rect["h"] <= page_w * page_h * 0.55


def _best_frame_rect(spec: dict, rects: list[dict]) -> dict | None:
    best = None
    score = None
    for rect in rects:
        if abs(rect["w"] - spec["w"]) > 8 or abs(rect["h"] - spec["h"]) > 8:
            continue
        if spec.get("x") is None:
            rank = rect["y"] * 10 + rect["x"]
        else:
            rank = abs(rect["x"] - spec["x"]) + abs(rect["y"] - spec["y"])
        if score is None or rank < score:
            best = rect
            score = rank
    return best


def _fill_frame(rect: dict, count: int, line_h: int, existing: list[dict]) -> list[dict]:
    step = max(12, line_h)
    marks = []
    for index in range(max(1, count)):
        y = rect["y"] + 3 + index * step
        if y > rect["y"] + rect["h"] - 8:
            break
        if _line_has_return(rect, y, step, existing + marks):
            continue
        marks.append(_enter_icon(0, 0, rect["x"] + 3, y, min(step, rect["h"]), "↵"))
    return marks


def _line_has_return(rect: dict, y: int, step: int, marks: list[dict]) -> bool:
    return any(
        rect["x"] - 4 <= mark["x"] <= rect["x"] + rect["w"] + 4 and abs(mark["y"] - y) <= step * 0.65
        for mark in marks
    )


def _frame_contains_word(rect: dict, words: list[dict]) -> bool:
    return any(
        rect["x"] <= word["x"] <= rect["x"] + rect["w"] and rect["y"] <= word["y"] <= rect["y"] + rect["h"]
        for word in words
    )


def _frame_contains_mark(rect: dict, marks: list[dict]) -> bool:
    return any(
        rect["x"] - 2 <= mark["x"] <= rect["x"] + rect["w"] + 2 and rect["y"] - 2 <= mark["y"] <= rect["y"] + rect["h"] + 2
        for mark in marks
    )


def paragraph_enters(raw: bytes, pages: list[dict]) -> dict[int, list[dict]]:
    """Put Word's paragraph mark where Enter was pressed, not at every wrapped line."""
    try:
        paragraphs = _paragraphs(raw)
    except (BadZipFile, ET.ParseError, OSError, ValueError, KeyError):
        return {}
    return _place_paragraph_marks(paragraphs, pages)


def _paragraphs(raw: bytes) -> list[dict]:
    with ZipFile(BytesIO(raw)) as package:
        root = ET.fromstring(package.read("word/document.xml"))
        body = root.find(f"{W}body")
        section = _section_metrics(body.find(f"{W}sectPr") if body is not None else None)
        found: list[dict] = []
        names = package.namelist()
        for name in sorted(item for item in names if item.startswith("word/header")):
            _walk_story(ET.fromstring(package.read(name)), found, section)
        _walk_story(body, found, section)
        for name in sorted(item for item in names if item.startswith("word/footer")):
            _walk_story(ET.fromstring(package.read(name)), found, section)
    return found


def _section_metrics(sect: ET.Element | None) -> dict:
    margin, default = 1440, 720
    if sect is not None:
        mar = sect.find(f"{W}pgMar")
        if mar is not None:
            try:
                margin = int(mar.get(f"{W}left") or 1440) + int(mar.get(f"{W}gutter") or 0)
            except ValueError:
                margin = 1440
        tab = sect.find(f"{W}defaultTabStop")
        if tab is not None:
            try:
                default = int(tab.get(f"{W}val") or 720)
            except ValueError:
                default = 720
    return {"margin": margin, "default": max(1, default)}


def _walk_story(node: ET.Element | None, found: list[dict], section: dict) -> None:
    if node is None:
        return
    for child in list(node):
        name = _tag(child)
        if name == "p":
            found.append(_make_paragraph(child, section))
            for box in _text_boxes(child):
                _walk_story(box, found, section)
        elif name == "tbl":
            for row in child.findall(f"{W}tr"):
                for cell in row.findall(f"{W}tc"):
                    _walk_story(cell, found, section)
        elif name == "sdt":
            _walk_story(child.find(f"{W}sdtContent"), found, section)
        elif name in {"customXml", "smartTag"}:
            _walk_story(child, found, section)
        elif name == "AlternateContent":
            choice = next((item for item in child if _tag(item) == "Choice"), None)
            fallback = next((item for item in child if _tag(item) == "Fallback"), None)
            _walk_story(choice if choice is not None else fallback, found, section)


def _make_paragraph(paragraph: ET.Element, section: dict) -> dict:
    tabs, indent = _paragraph_tabs(paragraph)
    return {
        "tabs": tabs,
        "default": section["default"],
        "margin": section["margin"],
        "indent": indent,
        "drawing": _has_drawing(paragraph),
        "tokens": _paragraph_tokens(paragraph),
    }


def _has_drawing(paragraph: ET.Element) -> bool:
    return any(_tag(node) in {"drawing", "pict", "object"} for node in paragraph.iter())


def _paragraph_tabs(paragraph: ET.Element) -> tuple[list[int], int]:
    props = paragraph.find(f"{W}pPr")
    indent = 0
    tabs: list[int] = []
    if props is None:
        return tabs, indent
    ind = props.find(f"{W}ind")
    if ind is not None:
        try:
            indent = int(ind.get(f"{W}left") or ind.get(f"{W}start") or 0)
        except ValueError:
            indent = 0
    container = props.find(f"{W}tabs")
    if container is not None:
        for tab in container.findall(f"{W}tab"):
            if (tab.get(f"{W}val") or "") == "clear":
                continue
            try:
                tabs.append(int(tab.get(f"{W}pos") or 0))
            except ValueError:
                continue
    return tabs, indent


def _paragraph_tokens(paragraph: ET.Element) -> list[tuple]:
    tokens: list[tuple] = []
    _inline_tokens(paragraph, tokens, [])
    tokens.append(("end",))
    return tokens


def _inline_tokens(node: ET.Element, tokens: list[tuple], stack: list[str]) -> None:
    for child in list(node):
        name = _tag(child)
        if name == "r":
            _run_tokens(child, tokens, stack)
        elif name in {"hyperlink", "ins", "smartTag", "sdtContent"}:
            _inline_tokens(child, tokens, stack)
        elif name == "sdt":
            content = child.find(f"{W}sdtContent")
            if content is not None:
                _inline_tokens(content, tokens, stack)
        elif name == "fldSimple":
            _inline_tokens(child, tokens, [])


def _run_tokens(run: ET.Element, tokens: list[tuple], stack: list[str]) -> None:
    for child in list(run):
        name = _tag(child)
        if name == "fldChar":
            kind = child.get(f"{W}fldCharType")
            if kind == "begin":
                stack.append("instr")
            elif kind == "separate" and stack:
                stack[-1] = "result"
            elif kind == "end" and stack:
                stack.pop()
            continue
        if stack and stack[-1] == "instr":
            continue
        if name == "t":
            _push_text(tokens, child.text or "")
        elif name == "tab":
            tokens.append(("tab",))
        elif name == "br":
            kind = "end" if child.get(f"{W}type") in {"page", "column"} else "break"
            tokens.append((kind,))
        elif name == "cr":
            tokens.append(("break",))


def _push_text(tokens: list[tuple], text: str) -> None:
    buf: list[str] = []

    def flush() -> None:
        if buf:
            tokens.append(("text", "".join(buf)))
            buf.clear()

    for char in text:
        if char.isspace() and char != "\u00a0":
            flush()
            tokens.append(("spaces", 1))
        else:
            buf.append(char)
    flush()


def _text_boxes(paragraph: ET.Element) -> list[ET.Element]:
    found: list[ET.Element] = []

    def walk(node: ET.Element, in_alt: bool) -> None:
        for child in list(node):
            name = _tag(child)
            if name == "AlternateContent":
                walk(child, True)
            elif in_alt and name == "Fallback":
                continue
            elif name == "txbxContent":
                found.append(child)
            else:
                walk(child, in_alt)

    walk(paragraph, False)
    return found


def _place_paragraph_marks(paragraphs: list[dict], pages: list[dict]) -> dict[int, list[dict]]:
    stream = _char_stream(pages)
    haystack = "".join(item["ch"] for item in stream)
    widths = {page["n"]: page.get("width") or 794 for page in pages}
    cursor = 0
    resolved: list[dict] = []
    for paragraph in paragraphs:
        texts = [token for token in paragraph["tokens"] if token[0] == "text"]
        if not texts:
            if not paragraph.get("drawing"):
                resolved.append({"blank": True, "paragraph": paragraph, "mark": "¶"})
            continue
        scan = cursor
        ends: list[dict] = []
        matched = True
        for token in texts:
            scan, item = _take_text(haystack, stream, scan, token[1])
            if item is None:
                matched = False
                break
            ends.append(item)
        if not matched:
            continue
        cursor = scan
        text_at = 0
        for index, token in enumerate(paragraph["tokens"]):
            if token[0] == "text":
                text_at += 1
                continue
            if token[0] not in {"break", "end"}:
                continue
            mark = "↵" if token[0] == "break" else "¶"
            if text_at == 0:
                resolved.append({"blank": True, "paragraph": paragraph, "mark": mark})
                continue
            anchor = ends[text_at - 1]
            word = anchor["word"]
            page_n = anchor["page"]
            resolved.append({
                "blank": False,
                "paragraph": paragraph,
                "mark": mark,
                "page": page_n,
                "x": _trail_x(_char_right(anchor), paragraph["tokens"], index, paragraph, widths.get(page_n, 794), word["h"]),
                "y": word["y"],
                "h": word["h"],
                "page_w": widths.get(page_n, 794),
            })
    return _materialize_marks(resolved, pages)


def _char_stream(pages: list[dict]) -> list[dict]:
    stream = []
    for page in pages:
        for word in page.get("words") or []:
            text = word.get("text") or ""
            for index, char in enumerate(text):
                if char.isspace():
                    continue
                stream.append({"ch": char, "page": page["n"], "word": word, "index": index})
    return stream


def _take_text(haystack: str, stream: list[dict], cursor: int, text: str) -> tuple[int, dict | None]:
    needle = "".join(char for char in text if not char.isspace())
    if not needle:
        return cursor, None
    found = haystack.find(needle, cursor)
    if found < 0:
        return cursor, None
    return found + len(needle), stream[found + len(needle) - 1]


def _char_right(item: dict) -> int:
    word = item["word"]
    count = len(word.get("text") or "") or 1
    return word["x"] + round(word["w"] * (item["index"] + 1) / count)


def clip_gaps(gaps: list[dict], cells: list[dict]) -> list[dict]:
    """A table cell owns its green field. Do not also paint a word gap across it."""
    if not cells:
        return gaps
    return [gap for gap in gaps if not any(_boxes_overlap(gap, cell) for cell in cells)]


def _keep_table_returns(marks: list[dict], cells: list[dict], table_marks: list[dict]) -> list[dict]:
    """Inside a table, keep the paragraph return and drop the guessed line marks."""
    if not table_marks:
        return marks
    kept = []
    for mark in marks:
        if mark.get("cell"):
            continue
        if any(_point_in_cell(mark, cell) for cell in cells):
            continue
        kept.append(mark)
    kept.extend(table_marks)
    return kept


def _table_field(page: int, index: int, x: int, y: int, w: int, h: int, name: str = "表格") -> dict:
    """A clickable field sitting on the area where Word lets you type."""
    return {
        "id": f"table-{page}-{index}",
        "name": name or "表格",
        "text": "",
        "control": "text",
        "checked": False,
        "blank": True,
        "table": True,
        "x": int(x),
        "y": int(y),
        "w": max(24, int(w)),
        "h": max(14, int(h)),
    }


def _boxes_overlap(left: dict, right: dict) -> bool:
    return (
        left["x"] < right["x"] + right.get("w", 0)
        and right["x"] < left["x"] + left.get("w", 0)
        and left["y"] < right["y"] + right.get("h", 0)
        and right["y"] < left["y"] + left.get("h", 0)
    )


def _covers_same_area(left: dict, right: dict) -> bool:
    """True when two fields are the same cell, not merely neighbours that share an edge."""
    overlap_w = min(left["x"] + left.get("w", 0), right["x"] + right.get("w", 0)) - max(left["x"], right["x"])
    overlap_h = min(left["y"] + left.get("h", 0), right["y"] + right.get("h", 0)) - max(left["y"], right["y"])
    if overlap_w < 8 or overlap_h < 6:
        return False
    small = min(max(left.get("w", 0), 1) * max(left.get("h", 0), 1), max(right.get("w", 0), 1) * max(right.get("h", 0), 1))
    return overlap_w * overlap_h >= small * 0.45


def _point_in_cell(mark: dict, cell: dict) -> bool:
    return cell["x"] <= mark["x"] <= cell["x"] + cell["w"] and cell["y"] - 2 <= mark["y"] <= cell["y"] + cell["h"]


def _blank_table_marks(raw: bytes, pages: list[dict]) -> dict[int, list[dict]]:
    """A table with no text still shows a return in each cell, just under the line above it."""
    try:
        items = _flow_items(raw)
    except (BadZipFile, ET.ParseError, OSError, ValueError, KeyError):
        return {}
    stream = _char_stream(pages)
    haystack = "".join(item["ch"] for item in stream)
    cursor = 0
    anchor = None
    found: dict[int, list[dict]] = {}
    for item in items:
        if item["kind"] == "p":
            if not item["text"]:
                if anchor is not None:
                    anchor = {**anchor, "y": anchor["y"] + (anchor["h"] or 16)}
                continue
            cursor, _first, last = _take_span(haystack, stream, cursor, item["text"])
            if last is None:
                continue
            word = last["word"]
            anchor = {"page": last["page"], "x": word["x"], "y": word["y"], "h": word["h"] or 16}
            continue
        if anchor is None:
            continue
        placed, bottom = _marks_under_anchor(anchor, item["model"])
        anchor = {"page": anchor["page"], "x": anchor["x"], "y": bottom, "h": anchor["h"]}
        found.setdefault(anchor["page"], []).extend(placed)
    return found


def _marks_under_anchor(anchor: dict, model: dict) -> tuple[list[dict], int]:
    widths = model["widths"] or [1]
    total = sum(widths) or 1
    table_w = model["table_w"] or _twip_px(total)
    if table_w <= 0:
        table_w = 360
    line_h = anchor["h"] or 16
    y = anchor["y"] + line_h + 4
    marks = []
    for row in model["rows"]:
        row_h = row["h"] or line_h + 8
        x = float(anchor["x"])
        column = 0
        for cell in row["cells"]:
            span = cell["span"]
            share = sum(widths[column:column + span]) / total if column < len(widths) else span / max(1, len(row["cells"]))
            cell_w = max(16, round(table_w * share))
            if not cell.get("skip"):
                for index in range(cell["paras"]):
                    marks.append(_table_field(
                        anchor["page"],
                        len(marks) + 1,
                        round(x) + 8,
                        y + index * line_h,
                        cell_w - 16,
                        max(14, row_h - 4),
                    ))
            x += cell_w
            column += span
        y += row_h
    return marks, y


def _fields_on_drawn_lines(raw: bytes, pages: list[dict]) -> dict[int, list[dict]]:
    """Put a table field on the line the page actually drew.

    Closed boxes already become fields from their borders. A table that only
    draws an underline has no box, so the field follows that line's left edge
    and width instead of the last word in the heading.
    """
    found: dict[int, list[dict]] = {page["n"]: [] for page in pages}
    free: dict[int, list[tuple[int, int, int]]] = {}
    covered: dict[int, list[dict]] = {}
    for page in pages:
        png = page.get("png")
        if not png:
            continue
        width = page.get("width") or 794
        height = page.get("height") or 1123
        horiz, vert = _bitmap_rules(png, width, height)
        bitmap = _css_cells(horiz, vert, width, height)
        frames = list(page.get("frames") or [])
        for rect in bitmap:
            if any(_boxes_overlap(rect, frame) for frame in frames):
                continue
            found[page["n"]].extend(_field_for_rect(page, rect))
        covered[page["n"]] = frames + bitmap
        free[page["n"]] = [line for line in horiz if not _line_on_rect(line, covered[page["n"]])]
    if any(page.get("png") for page in pages):
        for mark in _snap_tables_to_lines(raw, pages, free, covered):
            found.setdefault(mark["page"], []).append({key: value for key, value in mark.items() if key != "page"})
    return found


def _field_for_rect(page: dict, rect: dict) -> list[dict]:
    words = [word for word in (page.get("words") or []) if (word.get("text") or "").strip() and _word_in_rect(word, rect)]
    if not words:
        return [_table_field(page["n"], 1, rect["x"], rect["y"], rect["w"], rect["h"])]
    last = max(words, key=lambda word: word["x"] + word["w"])
    x = last["x"] + last["w"]
    width = rect["x"] + rect["w"] - x
    if width < 28:
        return []
    return [_table_field(page["n"], 1, x, rect["y"], width, rect["h"])]


def _line_on_rect(line: tuple[int, int, int], rects: list[dict]) -> bool:
    x0, x1, y = line
    for rect in rects:
        if abs(y - rect["y"]) > 3 and abs(y - (rect["y"] + rect["h"])) > 3:
            continue
        overlap = min(x1, rect["x"] + rect["w"]) - max(x0, rect["x"])
        if overlap > 20:
            return True
    return False


def _bitmap_rules(png: bytes, page_w: int, page_h: int) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """Horizontal (x0, x1, y) and vertical (y0, y1, x) rules, in CSS pixels."""
    image = _png_luma(png)
    if image is None:
        return [], []
    width, height, luma = image
    if width < 8 or height < 8 or not page_w or not page_h:
        return [], []
    sx = page_w / width
    sy = page_h / height
    gap = max(2, round(3 / sx))
    min_h = max(20, round(36 / sx))
    min_v = max(8, round(12 / sy))
    look = max(2, round(3 / sy))
    horiz_px: list[tuple[int, int, int]] = []
    for y in range(look, height - look):
        above = (y - look) * width
        row = y * width
        below = (y + look) * width

        def dark(x: int, row=row, above=above, below=below) -> bool:
            lum = luma[row + x]
            return lum < 200 and lum + 30 <= luma[above + x] and lum + 30 <= luma[below + x]

        for x0, x1 in _dark_runs(width, dark, min_h, gap):
            horiz_px.append((x0, x1, y))
    vert_px: list[tuple[int, int, int]] = []
    for x in range(look, width - look):

        def dark_v(y: int, x=x) -> bool:
            lum = luma[y * width + x]
            return lum < 200 and lum + 30 <= luma[y * width + x - look] and lum + 30 <= luma[y * width + x + look]

        for y0, y1 in _dark_runs(height, dark_v, min_v, gap):
            vert_px.append((y0, y1, x))
    return _merge_horiz(horiz_px, sx, sy), _merge_vert(vert_px, sx, sy)


def _dark_runs(length: int, dark, min_len: int, gap_allow: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = None
    last = None
    gap = 0
    for index in range(length):
        if dark(index):
            if start is None:
                start = index
            gap = 0
            last = index
            continue
        if start is None:
            continue
        gap += 1
        if gap <= gap_allow:
            continue
        if last is not None and last + 1 - start >= min_len:
            runs.append((start, last + 1))
        start = None
        last = None
        gap = 0
    if start is not None and last is not None and last + 1 - start >= min_len:
        runs.append((start, last + 1))
    return runs


def _merge_horiz(rules: list[tuple[int, int, int]], sx: float, sy: float) -> list[tuple[int, int, int]]:
    merged: list[dict] = []
    for x0, x1, y in sorted(rules, key=lambda item: (item[2], item[0])):
        host = None
        for item in merged:
            if y - item["y1"] <= 2 and x0 <= item["x1"] + 4 and x1 >= item["x0"] - 4:
                host = item
                break
        if host is None:
            merged.append({"x0": x0, "x1": x1, "y1": y})
            continue
        host["x0"] = min(host["x0"], x0)
        host["x1"] = max(host["x1"], x1)
        host["y1"] = y
    return [(round(item["x0"] * sx), round(item["x1"] * sx), round(item["y1"] * sy)) for item in merged if (item["x1"] - item["x0"]) * sx >= 36]


def _merge_vert(rules: list[tuple[int, int, int]], sx: float, sy: float) -> list[tuple[int, int, int]]:
    merged: list[dict] = []
    for y0, y1, x in sorted(rules, key=lambda item: (item[2], item[0])):
        host = None
        for item in merged:
            if abs(x - item["x"]) <= 2 and y0 <= item["y1"] + 4 and y1 >= item["y0"] - 4:
                host = item
                break
        if host is None:
            merged.append({"y0": y0, "y1": y1, "x": x})
            continue
        host["y0"] = min(host["y0"], y0)
        host["y1"] = max(host["y1"], y1)
    return [(round(item["y0"] * sy), round(item["y1"] * sy), round(item["x"] * sx)) for item in merged if (item["y1"] - item["y0"]) * sy >= 10]


def _css_cells(horiz: list[tuple[int, int, int]], vert: list[tuple[int, int, int]], page_w: int, page_h: int) -> list[dict]:
    ys = _cluster([float(item[2]) for item in horiz])
    cells = []
    for top, bottom in zip(ys, ys[1:]):
        if bottom - top < 8:
            continue
        spanning = [item for item in vert if item[0] <= top + 8 and item[1] >= bottom - 8]
        xs = _cluster([float(item[2]) for item in spanning])
        for left, right in zip(xs, xs[1:]):
            if right - left < 8:
                continue
            if not (_rule_covers(horiz, left, right, top) and _rule_covers(horiz, left, right, bottom)):
                continue
            cells.append({"x": round(left), "y": round(top), "w": max(1, round(right - left)), "h": max(1, round(bottom - top))})
    return _table_grid_cells(_prefer_inner(_dedupe_rects(cells)), page_w, page_h)


def _png_luma(data: bytes) -> tuple[int, int, bytearray] | None:
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    width = height = color = None
    compressed = b""
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR" and len(chunk) >= 10:
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            color = chunk[9]
        elif kind == b"IDAT":
            compressed += chunk
        elif kind == b"IEND":
            break
        pos += 12 + length
    if not width or not height or color not in (2, 6) or not compressed:
        return None
    try:
        raw = zlib.decompress(compressed)
    except zlib.error:
        return None
    channels = 3 if color == 2 else 4
    stride = width * channels
    luma = bytearray(width * height)
    index = 0
    prev = bytearray(stride)
    for y in range(height):
        if index >= len(raw):
            return None
        filt = raw[index]
        index += 1
        row = bytearray(raw[index:index + stride])
        index += stride
        if len(row) < stride:
            return None
        _png_unfilter(filt, row, prev, channels)
        for x in range(width):
            pixel = x * channels
            luma[y * width + x] = (row[pixel] * 3 + row[pixel + 1] * 6 + row[pixel + 2]) // 10
        prev = row
    return width, height, luma


def _png_unfilter(filt: int, row: bytearray, prev: bytearray, channels: int) -> None:
    stride = len(row)
    if filt == 1:
        for x in range(stride):
            row[x] = (row[x] + (row[x - channels] if x >= channels else 0)) & 255
    elif filt == 2:
        for x in range(stride):
            row[x] = (row[x] + prev[x]) & 255
    elif filt == 3:
        for x in range(stride):
            left = row[x - channels] if x >= channels else 0
            row[x] = (row[x] + ((left + prev[x]) // 2)) & 255
    elif filt == 4:
        for x in range(stride):
            left = row[x - channels] if x >= channels else 0
            up = prev[x]
            upper_left = prev[x - channels] if x >= channels else 0
            estimate = left + up - upper_left
            nearest = left if abs(estimate - left) <= abs(estimate - up) and abs(estimate - left) <= abs(estimate - upper_left) else up if abs(estimate - up) <= abs(estimate - upper_left) else upper_left
            row[x] = (row[x] + nearest) & 255
    elif filt != 0:
        return


def _snap_tables_to_lines(raw: bytes, pages: list[dict], lines: dict[int, list[tuple[int, int, int]]], covered: dict[int, list[dict]]) -> list[dict]:
    specs = _table_line_specs(raw, pages)
    words = {page["n"]: [word for word in (page.get("words") or []) if (word.get("text") or "").strip()] for page in pages}
    candidates = []
    for spec_index, spec in enumerate(specs):
        model = spec["model"]
        table_w = _spec_width(model)
        if table_w <= 0 or _table_already_drawn(spec, table_w, covered.get(spec["page"], [])):
            continue
        floating = model.get("float_y") is not None
        y_cursor = model["float_y"] if floating else spec["bottom"]
        for row_index, row in enumerate(model["rows"]):
            row_h = row["h"] or 20
            target = y_cursor + row_h
            for line_index, line in enumerate(lines.get(spec["page"], [])):
                x0, x1, y = line
                width = x1 - x0
                if floating:
                    if abs(y - target) > 70 or abs(x0 - model.get("float_x", x0)) > 90:
                        continue
                else:
                    if y < spec["bottom"] - 8 or y - spec["bottom"] > max(80, row_h + 70):
                        continue
                if abs(width - table_w) > max(48, table_w * 0.35):
                    continue
                score = abs(width - table_w) * 3 + abs(y - target) * 2
                if floating:
                    score += abs(x0 - model.get("float_x", x0))
                candidates.append((score, spec_index, row_index, line_index))
            y_cursor += row_h
    candidates.sort()
    used_rows: set[tuple[int, int]] = set()
    used_lines: set[tuple[int, int]] = set()
    marks = []
    for _score, spec_index, row_index, line_index in candidates:
        if (spec_index, row_index) in used_rows or (specs[spec_index]["page"], line_index) in used_lines:
            continue
        spec = specs[spec_index]
        used_rows.add((spec_index, row_index))
        used_lines.add((spec["page"], line_index))
        marks.extend(_fields_along_line(
            spec["page"],
            lines[spec["page"]][line_index],
            spec["model"]["rows"][row_index],
            spec["model"]["widths"],
            spec,
            words.get(spec["page"], []),
        ))
    for spec_index, spec in enumerate(specs):
        model = spec["model"]
        table_w = _spec_width(model)
        if table_w <= 0 or _table_already_drawn(spec, table_w, covered.get(spec["page"], [])):
            continue
        floating = model.get("float_y") is not None
        y_cursor = model["float_y"] if floating else spec["bottom"]
        x_cursor = model["float_x"] if floating else spec.get("left", 48)
        for row_index, row in enumerate(model["rows"]):
            row_h = row["h"] or 20
            bottom = y_cursor + row_h
            y_cursor = bottom
            if (spec_index, row_index) in used_rows:
                continue
            placed = _fields_along_line(
                spec["page"],
                (x_cursor, x_cursor + table_w, bottom),
                row,
                model["widths"],
                spec,
                words.get(spec["page"], []),
            )
            placed = [mark for mark in placed if not any(_covers_same_area(mark, other) for other in marks)]
            if placed:
                marks.extend(placed)
                used_rows.add((spec_index, row_index))
    return marks


def _spec_width(model: dict) -> int:
    return model["table_w"] or _twip_px(sum(model["widths"]) or 0)


def _table_already_drawn(spec: dict, table_w: int, rects: list[dict]) -> bool:
    for rect in rects:
        if rect["y"] + rect["h"] < spec["bottom"] - 8:
            continue
        if rect["y"] - spec["bottom"] > 90:
            continue
        if abs(rect["w"] - table_w) <= max(40, table_w * 0.4):
            return True
    return False


def _fields_along_line(page: int, line: tuple[int, int, int], row: dict, widths: list[int], spec: dict, words: list[dict]) -> list[dict]:
    x0, x1, y = line
    span = max(1, x1 - x0)
    total = sum(widths) or 1
    row_h = row["h"] or 20
    top = y - row_h
    if y > spec["bottom"] + 6:
        top = max(top, spec["bottom"] + 1)
    if y - top < 14:
        top = y - max(14, row_h)
    height = max(14, y - top)
    if height > 80:
        height = max(14, min(row_h, 40))
        top = y - height
    used = 0
    marks = []
    cells = row["cells"] or [{"span": 1, "skip": False}]
    edges = [x0]
    for cell in cells:
        used += cell["span"]
        if used >= len(widths) or not widths:
            edges.append(x1)
        else:
            edges.append(round(x0 + span * sum(widths[:used]) / total))
    edges[-1] = x1
    for index, cell in enumerate(cells):
        if cell.get("skip"):
            continue
        fx = edges[index]
        fw = max(8, edges[index + 1] - fx)
        rect = {"x": fx, "y": top, "w": fw, "h": height}
        inside = [word for word in words if _word_in_rect(word, rect)]
        if inside:
            right = max(word["x"] + word["w"] for word in inside)
            fw = fx + fw - right
            fx = right
            if fw < 28:
                continue
        marks.append({**_table_field(page, len(marks) + 1, fx, top, fw, height), "page": page})
    return marks


def _table_line_specs(raw: bytes, pages: list[dict]) -> list[dict]:
    try:
        items = _line_items(raw)
    except (BadZipFile, ET.ParseError, OSError, ValueError, KeyError):
        return []
    stream = _char_stream(pages)
    haystack = "".join(item["ch"] for item in stream)
    cursor = 0
    anchor = None
    located: list[dict | None] = []
    for kind, payload in items:
        if kind == "p" and payload:
            cursor, first, last = _take_span(haystack, stream, cursor, payload)
            if last is not None:
                word = last["word"]
                left = first["word"]["x"] if first is not None else word["x"]
                anchor = {"page": last["page"], "bottom": word["y"] + word["h"], "h": word["h"] or 16, "left": left}
        elif kind == "p" and anchor is not None:
            anchor = {**anchor, "bottom": anchor["bottom"] + (anchor["h"] or 16)}
        located.append(anchor)
    specs = []
    for index, (kind, payload) in enumerate(items):
        if kind != "tbl":
            continue
        chosen = located[index]
        if chosen is None:
            for later in located[index + 1:]:
                if later is not None:
                    chosen = later
                    break
        if chosen is None:
            continue
        specs.append({**chosen, "model": payload})
    return specs


def _line_items(raw: bytes) -> list[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    with ZipFile(BytesIO(raw)) as package:
        names = package.namelist()
        parts = [name for name in sorted(names) if name.startswith("word/header")]
        parts.append("word/document.xml")
        parts.extend(name for name in sorted(names) if name.startswith("word/footer"))
        for name in parts:
            if name not in names:
                continue
            _collect_line_items(ET.fromstring(package.read(name)), items)
    return items


def _collect_line_items(node: ET.Element, items: list[tuple[str, object]]) -> None:
    for child in list(node):
        name = _tag(child)
        if name == "Fallback":
            continue
        if name == "p":
            items.append(("p", _para_text(child)))
            _collect_line_items(child, items)
        elif name == "tbl":
            items.append(("tbl", _line_model(child)))
            _collect_line_items(child, items)
        elif name == "sdt":
            content = child.find(f"{W}sdtContent")
            if content is not None:
                _collect_line_items(content, items)
        else:
            _collect_line_items(child, items)


def _line_model(table: ET.Element) -> dict:
    model = _blank_model(table)
    rows = [child for child in list(table) if _tag(child) == "tr"]
    for row, source in zip(model["rows"], rows):
        cells = [child for child in list(source) if _tag(child) == "tc"]
        for cell, node in zip(row["cells"], cells):
            cell["text"] = _cell_has_own_text(node)
    placed = table.find(f"{W}tblPr/{W}tblpPr")
    if placed is not None and placed.get(f"{W}tblpX") is not None and placed.get(f"{W}tblpY") is not None:
        model["float_x"] = _twip_px(placed.get(f"{W}tblpX"))
        model["float_y"] = _twip_px(placed.get(f"{W}tblpY"))
        if (placed.get(f"{W}horzAnchor") or "page") in {"margin", "text"}:
            model["float_x"] += 96
        if (placed.get(f"{W}vertAnchor") or "page") in {"margin", "text"}:
            model["float_y"] += 96
    return model


def _cell_has_own_text(cell: ET.Element) -> bool:
    def walk(node: ET.Element, nested: bool) -> bool:
        if _tag(node) == "tbl":
            nested = True
        if not nested and _tag(node) == "t" and (node.text or "").strip():
            return True
        return any(walk(child, nested) for child in list(node))

    return walk(cell, False)


def _flow_items(raw: bytes) -> list[dict]:
    items: list[dict] = []
    seen: set[int] = set()
    with ZipFile(BytesIO(raw)) as package:
        names = package.namelist()
        parts = [name for name in sorted(names) if name.startswith("word/header")]
        parts.append("word/document.xml")
        parts.extend(name for name in sorted(names) if name.startswith("word/footer"))
        for name in parts:
            if name not in names:
                continue
            _collect_flow(ET.fromstring(package.read(name)), items, seen)
    return items


def _collect_flow(node: ET.Element, items: list[dict], seen: set[int]) -> None:
    for child in list(node):
        name = _tag(child)
        if name == "Fallback":
            continue
        if name == "p":
            items.append({"kind": "p", "text": _para_text(child)})
            _collect_flow(child, items, seen)
        elif name == "tbl" and id(child) not in seen:
            seen.add(id(child))
            if _table_is_blank(child):
                items.append({"kind": "blank", "model": _blank_model(child)})
            else:
                _collect_flow(child, items, seen)
        elif name == "sdt":
            content = child.find(f"{W}sdtContent")
            if content is not None:
                _collect_flow(content, items, seen)
        else:
            _collect_flow(child, items, seen)


def _table_is_blank(table: ET.Element) -> bool:
    for node in table.iter():
        if _tag(node) == "t" and (node.text or "").strip():
            return False
    return True


def _blank_model(table: ET.Element) -> dict:
    rows = []
    col_count = 0
    for row in [child for child in list(table) if _tag(child) == "tr"]:
        cells = []
        column = 0
        for cell in [child for child in list(row) if _tag(child) == "tc"]:
            span = _grid_span(cell)
            if _merge_continue(cell):
                cells.append({"span": span, "paras": 1, "skip": True})
            else:
                paras = sum(1 for child in cell.iter() if _tag(child) == "p" and not _inside_nested_table(child, cell))
                cells.append({"span": span, "paras": max(1, paras), "skip": False})
            column += span
        col_count = max(col_count, column)
        rows.append({"h": _row_height(row), "cells": cells})
    width = table.find(f"{W}tblPr/{W}tblW")
    table_w = 0
    if width is not None and (width.get(f"{W}type") or "dxa") == "dxa":
        table_w = _twip_px(width.get(f"{W}w") or 0)
    return {"rows": rows, "widths": _table_widths(table, col_count), "table_w": table_w}


def _inside_nested_table(node: ET.Element, cell: ET.Element) -> bool:
    return False
    return cell["x"] <= mark["x"] <= cell["x"] + cell["w"] and cell["y"] - 2 <= mark["y"] <= cell["y"] + cell["h"]


def _table_returns(raw: bytes, pages: list[dict]) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """One return for every paragraph in every table cell, including an empty cell."""
    try:
        events, metas = _table_events(raw)
    except (BadZipFile, ET.ParseError, OSError, ValueError, KeyError):
        return {}, {}
    if not events:
        return {}, {}
    stream = _char_stream(pages)
    haystack = "".join(item["ch"] for item in stream)
    cursor = 0
    anchors: dict[int, dict] = {}
    for index, event in enumerate(events):
        if not event["text"]:
            continue
        cursor, first, last = _take_span(haystack, stream, cursor, event["text"])
        if last is None:
            continue
        anchors[index] = {
            "page": last["page"],
            "x": first["word"]["x"],
            "right": _char_right(last),
            "y": last["word"]["y"],
            "h": last["word"]["h"],
        }
    found: dict[int, list[dict]] = {}
    cells: dict[int, list[dict]] = {}
    by_table: dict[int, list[int]] = {}
    for index, event in enumerate(events):
        by_table.setdefault(event["table"], []).append(index)
    for table_id, indexes in by_table.items():
        local_events = []
        local_anchors = {}
        for local, global_index in enumerate(indexes):
            local_events.append(events[global_index])
            if global_index in anchors:
                local_anchors[local] = anchors[global_index]
        table_marks, table_cells = _marks_for_table(metas[table_id], local_events, local_anchors)
        for mark in table_marks:
            found.setdefault(mark["page"], []).append({key: value for key, value in mark.items() if key != "page"})
        for cell in table_cells:
            cells.setdefault(cell["page"], []).append({key: value for key, value in cell.items() if key != "page"})
    return found, cells


def _take_span(haystack: str, stream: list[dict], cursor: int, text: str) -> tuple[int, dict | None, dict | None]:
    needle = "".join(char for char in text if not char.isspace())
    if not needle:
        return cursor, None, None
    found = haystack.find(needle, cursor)
    if found < 0:
        return cursor, None, None
    return found + len(needle), stream[found], stream[found + len(needle) - 1]


def _table_events(raw: bytes) -> tuple[list[dict], dict[int, dict]]:
    events: list[dict] = []
    metas: dict[int, dict] = {}
    seen: set[int] = set()
    with ZipFile(BytesIO(raw)) as package:
        names = package.namelist()
        parts = [name for name in sorted(names) if name.startswith("word/header")]
        parts.append("word/document.xml")
        parts.extend(name for name in sorted(names) if name.startswith("word/footer"))
        for name in parts:
            if name not in names:
                continue
            _gather_tables(ET.fromstring(package.read(name)), events, metas, seen)
    return events, metas


def _gather_tables(node: ET.Element, events: list[dict], metas: dict[int, dict], seen: set[int]) -> None:
    for child in list(node):
        if _tag(child) == "Fallback":
            continue
        if _tag(child) == "tbl" and id(child) not in seen:
            seen.add(id(child))
            _emit_table(child, events, metas, seen)
        else:
            _gather_tables(child, events, metas, seen)


def _emit_table(table: ET.Element, events: list[dict], metas: dict[int, dict], seen: set[int]) -> None:
    rows = [row for row in list(table) if _tag(row) == "tr"]
    occupied: list[dict] = []
    col_count = 0
    for row_index, row in enumerate(rows):
        column = 0
        for cell in [child for child in list(row) if _tag(child) == "tc"]:
            while any(item["row"] == row_index and item["col"] == column for item in occupied):
                column += 1
            span = _grid_span(cell)
            if not _merge_continue(cell):
                _emit_cell(cell, id(table), row_index, column, span, _row_height(row), events, metas, seen)
            if _merge_restart(cell):
                occupied.append({"row": row_index, "col": column, "rows": _merge_rows(cell), "span": span})
            column += span
        col_count = max(col_count, column)
    metas[id(table)] = {"widths": _table_widths(table, col_count), "rows": len(rows), "cols": col_count}


def _emit_cell(cell: ET.Element, table_id: int, row: int, column: int, span: int, row_h: int, events: list[dict], metas: dict[int, dict], seen: set[int], counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    for child in list(cell):
        name = _tag(child)
        if name == "Fallback":
            continue
        if name == "p":
            events.append({
                "table": table_id,
                "row": row,
                "col": column,
                "span": span,
                "para": counter[0],
                "text": _para_text(child),
                "row_h": row_h,
            })
            counter[0] += 1
        elif name == "tbl" and id(child) not in seen:
            seen.add(id(child))
            _emit_table(child, events, metas, seen)
        elif name in {"sdt", "customXml", "smartTag", "hyperlink", "ins"}:
            content = child.find(f"{W}sdtContent") if name == "sdt" else child
            if content is not None:
                _emit_cell(content, table_id, row, column, span, row_h, events, metas, seen, counter)


def _para_text(paragraph: ET.Element) -> str:
    parts = []
    for node in paragraph.iter():
        if _tag(node) == "t" and node.text:
            parts.append(node.text)
    return "".join(char for char in "".join(parts) if not char.isspace())


def _grid_span(cell: ET.Element) -> int:
    span = cell.find(f"{W}tcPr/{W}gridSpan")
    try:
        return max(1, int(span.get(f"{W}val") or 1)) if span is not None else 1
    except ValueError:
        return 1


def _merge_continue(cell: ET.Element) -> bool:
    merge = cell.find(f"{W}tcPr/{W}vMerge")
    if merge is None:
        return False
    return (merge.get(f"{W}val") or "continue") == "continue"


def _merge_restart(cell: ET.Element) -> bool:
    merge = cell.find(f"{W}tcPr/{W}vMerge")
    return merge is not None and merge.get(f"{W}val") == "restart"


def _merge_rows(cell: ET.Element) -> int:
    return 1


def _row_height(row: ET.Element) -> int:
    height = row.find(f"{W}trPr/{W}trHeight")
    if height is None:
        return 0
    return _twip_px(height.get(f"{W}val") or 0)


def _table_widths(table: ET.Element, count: int) -> list[int]:
    grid = table.find(f"{W}tblGrid")
    widths = []
    if grid is not None:
        for column in grid.findall(f"{W}gridCol"):
            try:
                widths.append(max(1, int(column.get(f"{W}w") or 0)))
            except ValueError:
                widths.append(1)
    if count <= 0:
        count = len(widths) or 1
    if len(widths) < count:
        widths.extend([widths[-1] if widths else 1] * (count - len(widths)))
    return widths[:count]


def _marks_for_table(meta: dict, events: list[dict], anchors: dict[int, dict]) -> list[dict]:
    widths = meta["widths"] or [1]
    total = sum(widths) or 1
    samples = []
    for event_index, event in enumerate(events):
        anchor = anchors.get(event_index)
        if anchor is None or event["para"] != 0:
            continue
        fraction = sum(widths[:event["col"]]) / total
        samples.append((fraction, anchor["x"]))
    lefts = _column_lefts(widths, samples)
    if lefts is None:
        return [], []
    row_y: dict[int, int] = {}
    row_page: dict[int, int] = {}
    row_h: dict[int, int] = {}
    for event_index, event in enumerate(events):
        anchor = anchors.get(event_index)
        row_h[event["row"]] = event["row_h"] or row_h.get(event["row"], 0)
        if anchor is None:
            continue
        row_page[event["row"]] = anchor["page"]
        row_y[event["row"]] = min(row_y.get(event["row"], anchor["y"]), anchor["y"])
    line_h = next((anchor["h"] for anchor in anchors.values() if anchor.get("h")), 16) or 16
    known = sorted(row_y)
    for row in range(meta["rows"]):
        if row in row_y:
            continue
        earlier = [item for item in known if item < row]
        later = [item for item in known if item > row]
        if earlier and later:
            previous, nxt = earlier[-1], later[0]
            share = (row - previous) / (nxt - previous)
            row_y[row] = round(row_y[previous] + (row_y[nxt] - row_y[previous]) * share)
            row_page[row] = row_page[previous]
        elif earlier:
            previous = earlier[-1]
            step = row_h.get(previous) or line_h
            row_y[row] = row_y[previous] + step * (row - previous)
            row_page[row] = row_page[previous]
    marks = []
    for event_index, event in enumerate(events):
        anchor = anchors.get(event_index)
        page_n = anchor["page"] if anchor else row_page.get(event["row"])
        if page_n is None:
            continue
        if anchor is not None:
            left = lefts[min(event["col"], len(lefts) - 1)]
            right = lefts[min(event["col"] + event["span"], len(lefts) - 1)]
            x = anchor["right"] + 2
            width = round(right) - 8 - x
            if width < 28:
                continue
            y, height = anchor["y"], anchor["h"]
        else:
            left = lefts[min(event["col"], len(lefts) - 1)]
            right = lefts[min(event["col"] + event["span"], len(lefts) - 1)]
            y = row_y.get(event["row"])
            if y is None:
                continue
            x = round(left) + 8
            width = max(28, round(right - left) - 16)
            height = line_h
            y += event["para"] * line_h
        marks.append({**_table_field(page_n, len(marks) + 1, x, y, width, height), "page": page_n})
    cells = []
    seen: set[tuple[int, int]] = set()
    for event in events:
        key = (event["row"], event["col"])
        if key in seen:
            continue
        seen.add(key)
        page_n = row_page.get(event["row"])
        y = row_y.get(event["row"])
        if page_n is None or y is None:
            continue
        left = lefts[min(event["col"], len(lefts) - 1)]
        right = lefts[min(event["col"] + event["span"], len(lefts) - 1)]
        cells.append({
            "page": page_n,
            "x": round(left),
            "y": y - 2,
            "w": max(16, round(right - left)),
            "h": max(line_h + 6, event["row_h"] or 0),
        })
    return marks, cells


def _column_lefts(widths: list[int], samples: list[tuple[float, int]]) -> list[float] | None:
    total = sum(widths) or 1
    if not samples:
        return None
    if len(samples) >= 2:
        count = len(samples)
        sum_f = sum(item[0] for item in samples)
        sum_x = sum(item[1] for item in samples)
        sum_f2 = sum(item[0] * item[0] for item in samples)
        sum_fx = sum(item[0] * item[1] for item in samples)
        denom = count * sum_f2 - sum_f * sum_f
        if abs(denom) > 1e-6:
            scale = (count * sum_fx - sum_f * sum_x) / denom
            origin = (sum_x - scale * sum_f) / count
        else:
            origin = samples[0][1]
            scale = total / 15
    else:
        fraction, across = samples[0]
        scale = total / 15
        origin = across - fraction * scale
    if scale <= 0:
        return None
    lefts = []
    used = 0
    for width in widths:
        lefts.append(origin + used / total * scale)
        used += width
    lefts.append(origin + scale)
    return lefts


def _trail_x(x: int, tokens: list[tuple], break_index: int, paragraph: dict, page_w: int, height: int) -> int:
    start = break_index
    while start > 0 and tokens[start - 1][0] in {"spaces", "tab"}:
        start -= 1
    for token in tokens[start:break_index]:
        if token[0] == "spaces":
            x += token[1] * _space_px(height)
        elif token[0] == "tab":
            x = _next_tab(x, paragraph, page_w)
    return x


def _blank_x(paragraph: dict, height: int, page_w: int) -> int:
    x = _twip_px(paragraph["margin"] + paragraph["indent"])
    for token in paragraph["tokens"]:
        if token[0] == "spaces":
            x += token[1] * _space_px(height)
        elif token[0] == "tab":
            x = _next_tab(x, paragraph, page_w)
        else:
            break
    return x


def _next_tab(x: int, paragraph: dict, page_w: int) -> int:
    """The next Word tab stop. Defaults to the left of a custom stop are not used."""
    margin = _twip_px(paragraph["margin"])
    custom = sorted(margin + _twip_px(pos) for pos in paragraph["tabs"])
    step = max(1, _twip_px(paragraph["default"]))
    stops = list(custom)
    grid = margin + step
    last_custom = custom[-1] if custom else margin
    while grid <= last_custom:
        grid += step
    while grid <= page_w + step:
        stops.append(grid)
        grid += step
    for stop in stops:
        if stop > x:
            return stop
    return x


def _space_px(height: int) -> int:
    return max(1, round((height or 14) * 0.34))


def _twip_px(value) -> int:
    try:
        return round(int(value) / 15)
    except (TypeError, ValueError):
        return 0


def _materialize_marks(resolved: list[dict], pages: list[dict]) -> dict[int, list[dict]]:
    output: dict[int, list[dict]] = {page["n"]: [] for page in pages}
    heights = {page["n"]: page.get("height") or 1123 for page in pages}
    pending: list[dict] = []
    previous = None
    sequence = 0

    def emit(item: dict) -> None:
        nonlocal sequence
        page_n = item.get("page")
        if page_n not in output:
            return
        sequence += 1
        output[page_n].append(_enter_icon(page_n, sequence, item["x"], item["y"], item["h"], item.get("mark") or "¶"))

    for item in resolved:
        if item.get("blank"):
            pending.append(item)
            continue
        if pending:
            _flush_blank_marks(previous, item, pending, heights, emit)
            pending = []
        emit(item)
        previous = item
    if pending:
        _flush_blank_marks(previous, None, pending, heights, emit)
    return output


def _flush_blank_marks(previous: dict | None, nxt: dict | None, blanks: list[dict], heights: dict, emit) -> None:
    count = len(blanks)
    if previous and nxt and previous["page"] == nxt["page"]:
        room = nxt["y"] - previous["y"]
        if room < previous["h"] * count:
            return
        step = room / (count + 1)
        for index, blank in enumerate(blanks, start=1):
            height = previous["h"]
            y = round(previous["y"] + step * index)
            if y < previous["y"] + previous["h"] - 2 or y + height > nxt["y"] + 2:
                continue
            emit({
                "page": previous["page"],
                "x": _blank_x(blank["paragraph"], height, previous.get("page_w") or 794),
                "y": y,
                "h": height,
                "mark": blank.get("mark") or "¶",
            })
        return
    if previous:
        for index, blank in enumerate(blanks, start=1):
            height = previous["h"]
            y = previous["y"] + height * index
            if y + height > heights.get(previous["page"], 1123):
                break
            emit({
                "page": previous["page"],
                "x": _blank_x(blank["paragraph"], height, previous.get("page_w") or 794),
                "y": y,
                "h": height,
                "mark": blank.get("mark") or "¶",
            })
        return
    if nxt:
        height = nxt["h"]
        for offset, blank in enumerate(reversed(blanks), start=1):
            y = nxt["y"] - height * offset
            if y < 0:
                continue
            emit({
                "page": nxt["page"],
                "x": _blank_x(blank["paragraph"], height, nxt.get("page_w") or 794),
                "y": y,
                "h": height,
                "mark": blank.get("mark") or "¶",
            })


def _enter_icon(page: int, index: int, x: int, y: int, size: int, mark: str = "¶") -> dict:
    side = max(10, size)
    return {
        "id": f"enter-{page}-{index}",
        "name": "Enter",
        "text": "",
        "control": "text",
        "checked": False,
        "blank": True,
        "enter": True,
        "mark": mark,
        "x": x,
        "y": y,
        "w": side,
        "h": side,
    }


def _box_overlaps(left: dict, right: dict) -> bool:
    return (
        left["x"] < right["x"] + right["w"]
        and right["x"] < left["x"] + left["w"]
        and left["y"] < right["y"] + right["h"]
        and right["y"] < left["y"] + left["h"]
    )


def collect_ticks(pages: list[dict]) -> dict[int, list[dict]]:
    """A standalone 口 is a checkbox. 口 inside a word such as 人口 is left as text."""
    found: dict[int, list[dict]] = {}
    for page in pages:
        boxes = []
        for word in page.get("words") or []:
            boxes.extend(_tick_boxes(word))
        boxes.sort(key=lambda box: (box["y"], box["x"]))
        found[page["n"]] = [
            {"id": f"{page['n']}-{index}", **box}
            for index, box in enumerate(boxes, start=1)
        ]
    return found


_OPEN_BOX = set("口\u2610\u25a1\u25a2\u25fb\u25fd\u2b1c")


def _tick_boxes(word: dict) -> list[dict]:
    text = word.get("text") or ""
    chars = list(text)
    if not chars or not any(char in _OPEN_BOX for char in chars):
        return []
    width = word["w"] / len(chars)
    boxes = []
    for index, char in enumerate(chars):
        if char not in _OPEN_BOX:
            continue
        previous = chars[index - 1] if index else ""
        if char == "口" and previous and _cjk(previous):
            continue
        boxes.append({
            "x": round(word["x"] + index * width),
            "y": word["y"],
            "w": max(1, round(width)),
            "h": word["h"],
        })
    return boxes


def _cjk(char: str) -> bool:
    code = ord(char)
    return 0x3400 <= code <= 0x9FFF


def drawing_inputs(raw: bytes) -> list[bytes]:
    """Draw the original file first. The annotated copy is only a fallback."""
    inputs = [raw]
    try:
        prepared = prepare_docx(marked_docx(raw))
    except (ET.ParseError, WordPreviewError, ValueError, KeyError, OSError):
        return inputs
    if prepared != raw:
        inputs.append(prepared)
    return inputs


def layout_docx(source: str | Path | bytes) -> dict:
    key, raw = _identity(source)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        error = None
        laid = None
        for document in drawing_inputs(raw):
            try:
                laid = _build(document)
                break
            except WordLayoutError as exc:
                error = exc
        if laid is None:
            raise error or WordLayoutError("無法按 Word 版面繪製這份文件")
        marks = page_returns(document, laid["pages"])
        for page in laid["pages"]:
            page["enters"] = marks.get(page["n"], [])
        laid["asset"] = key[-16:]
        _CACHE.clear()
        _CACHE[key] = laid
        return laid


def page_png(source: str | Path | bytes, number: int) -> bytes:
    laid = layout_docx(source)
    for page in laid["pages"]:
        if page["n"] == number:
            return page["png"]
    raise WordLayoutError("沒有這一頁")


def prepare_docx(raw: bytes) -> bytes:
    """Keep the shown field text and drop the field codes.

    The layout engine would otherwise refresh merge fields and wipe the
    text Word is currently showing. Text boxes stay in the same drawing.
    """
    incoming = BytesIO(raw)
    outgoing = BytesIO()
    with ZipFile(incoming) as source, ZipFile(outgoing, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "word/document.xml":
                data = _flatten_xml(data)
            target.writestr(info, data)
    return outgoing.getvalue()


def _flatten_xml(data: bytes) -> bytes:
    for prefix, uri in _NS.items():
        ET.register_namespace(prefix, uri)
    root = ET.fromstring(data)
    _flatten(root)
    return ET.tostring(root, xml_declaration=True, encoding="utf-8")


def _flatten(parent: ET.Element) -> None:
    for child in list(parent):
        _flatten(child)
    kept: list[ET.Element] = []
    changed = False
    for child in list(parent):
        if child.tag in {f"{W}fldChar", f"{W}instrText"}:
            changed = True
            continue
        if child.tag == f"{W}fldSimple":
            changed = True
            kept.extend(list(child))
            continue
        kept.append(child)
    if not changed:
        return
    for child in list(parent):
        parent.remove(child)
    for child in kept:
        parent.append(child)


def _build(raw: bytes) -> dict:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    pdftotext = shutil.which("pdftotext")
    pdftoppm = shutil.which("pdftoppm")
    pdfinfo = shutil.which("pdfinfo")
    if not soffice or not pdftotext or not pdftoppm or not pdfinfo:
        raise WordLayoutError("這部電腦沒有文件排版引擎")
    with tempfile.TemporaryDirectory(prefix="cdd-word-") as folder:
        root = Path(folder)
        docx = root / "source.docx"
        docx.write_bytes(raw)
        pdf = _to_pdf(soffice, docx, root)
        count = _page_count(pdfinfo, pdf)
        frames = _page_frame_rects(pdf, count)
        pages = []
        for number in range(1, count + 1):
            page = _one_page(pdftotext, pdftoppm, pdfinfo, pdf, root, number)
            page["frames"] = frames.get(number, [])
            pages.append(page)
    return {"pages": pages}


def _to_pdf(soffice: str, docx: Path, root: Path) -> Path:
    env = os.environ.copy()
    env["HOME"] = str(root)
    env["SAL_USE_VCLPLUGIN"] = "svp"
    env["LANG"] = "C.UTF-8"
    env["DBUS_SESSION_BUS_ADDRESS"] = "/dev/null"
    notes: list[str] = []
    for index, convert in enumerate(("pdf", "pdf:writer_pdf_Export")):
        profile = root / f"profile-{index}"
        profile.mkdir()
        for stale in root.glob("*.pdf"):
            stale.unlink()
        proc = subprocess.Popen(
            [
                soffice,
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--nologo",
                "--nofirststartwizard",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                convert,
                "--outdir",
                str(root),
                str(docx),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            output, _err = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise WordLayoutError("繪製逾時") from exc
        found = _find_pdf(root)
        if found is not None:
            return found
        note = _engine_note(output.decode("utf-8", "replace"))
        if note:
            notes.append(f"{note} ({proc.returncode})")
    detail = "無法按 Word 版面繪製這份文件"
    if notes:
        detail = f"{detail}：{notes[-1]}"
    raise WordLayoutError(detail)


def _find_pdf(root: Path) -> Path | None:
    found = [path for path in root.glob("*.pdf") if _usable_pdf(path)]
    if not found:
        return None
    return max(found, key=lambda path: path.stat().st_size)


def _engine_note(text: str) -> str:
    """Keep a short engine error. Document text is never included."""
    kept = []
    for line in text.splitlines():
        if any("\u3400" <= char <= "\u9fff" for char in line):
            continue
        line = " ".join(line.split())
        if not line or len(line) > 160:
            continue
        lowered = line.lower()
        if any(token in lowered for token in ("error", "fail", "could not", "exception", "not found")):
            kept.append(line)
    return kept[-1] if kept else ""


def _usable_pdf(path: Path) -> bool:
    """LibreOffice often writes the PDF and then exits with a warning code."""
    if not path.is_file() or path.stat().st_size < 100:
        return False
    return path.read_bytes()[:5] == b"%PDF-"


def _page_box(pdfinfo: str, pdf: Path, number: int) -> tuple[int, int]:
    proc = subprocess.run(
        [pdfinfo, "-f", str(number), "-l", str(number), str(pdf)],
        check=False,
        capture_output=True,
        text=True,
    )
    for line in (proc.stdout or "").splitlines():
        if "size:" not in line or "pts" not in line:
            continue
        numbers = [piece for piece in line.replace("x", " ").split() if piece.replace(".", "", 1).isdigit()]
        if len(numbers) >= 2:
            return _px(numbers[0]), _px(numbers[1])
    return 794, 1123


def _page_count(pdfinfo: str, pdf: Path) -> int:
    proc = subprocess.run([pdfinfo, str(pdf)], check=False, capture_output=True, text=True)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("Pages:"):
            try:
                return max(1, int(line.split()[1]))
            except (IndexError, ValueError):
                break
    raise WordLayoutError("無法按 Word 版面繪製這份文件：讀不到頁數")


def _one_page(pdftotext: str, pdftoppm: str, pdfinfo: str, pdf: Path, root: Path, number: int) -> dict:
    bbox = root / f"page-{number}.xml"
    text = subprocess.run(
        [pdftotext, "-bbox", "-f", str(number), "-l", str(number), str(pdf), str(bbox)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    width = height = 0
    words: list[dict] = []
    if text.returncode == 0 and bbox.is_file():
        try:
            width, height, words = _words(bbox.read_bytes())
        except (ET.ParseError, WordLayoutError):
            width = height = 0
    if not width or not height:
        width, height = _page_box(pdfinfo, pdf, number)
    for stale in root.glob("page-*.png"):
        stale.unlink()
    subprocess.run(
        [pdftoppm, "-png", "-r", "144", "-cropbox", "-f", str(number), "-l", str(number), str(pdf), str(root / "page")],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    images = sorted(root.glob("page-*.png"))
    if not images:
        raise WordLayoutError("無法按 Word 版面繪製這份文件：頁面圖片失敗")
    png = images[0]
    return {"n": number, "width": width, "height": height, "png": png.read_bytes(), "words": words}


def _words(data: bytes) -> tuple[int, int, list[dict]]:
    root = ET.fromstring(data)
    page = next((node for node in root.iter() if _tag(node) == "page"), None)
    if page is None:
        raise WordLayoutError("無法按 Word 版面繪製這份文件")
    width = _px(page.get("width"))
    height = _px(page.get("height"))
    words = []
    for word in page.iter():
        if _tag(word) != "word":
            continue
        text = word.text or ""
        if not text.strip():
            continue
        x0, y0 = _px(word.get("xMin")), _px(word.get("yMin"))
        x1, y1 = _px(word.get("xMax")), _px(word.get("yMax"))
        words.append({"text": text, "x": x0, "y": y0, "w": max(1, x1 - x0), "h": max(1, y1 - y0)})
    return width, height, words


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _identity(source: str | Path | bytes) -> tuple[str, bytes]:
    if isinstance(source, bytes):
        digest = hashlib.sha256(source).hexdigest()
        return f"b:{digest}", source
    path = Path(source)
    stat = path.stat()
    digest = hashlib.sha256(f"{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
    return f"p:{digest}", path.read_bytes()


def _walk(blocks: list[dict], found: list[dict]) -> None:
    for block in blocks:
        if block.get("type") == "frame":
            _walk(block.get("blocks") or [], found)
        elif block.get("type") == "table":
            for row in block.get("rows") or []:
                for cell in row:
                    _walk(cell.get("blocks") or [], found)
        else:
            for item in block.get("items") or []:
                if item.get("type") == "field":
                    found.append(item)


def _find_unused(haystack: str, used: list[bool], needle: str) -> int:
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return -1
        if all(not used[offset] for offset in range(idx, idx + len(needle))):
            return idx
        start = idx + 1


def _public_field(field: dict, box: dict | None = None, blank: bool = False) -> dict:
    item = {
        "id": str(field.get("id") or ""),
        "name": field.get("name") or "欄位",
        "text": field.get("text") or "",
        "control": field.get("control") or "text",
        "checked": bool(field.get("checked")),
    }
    if blank or field.get("blank"):
        item["blank"] = True
    if box:
        item.update(box)
    return item


def _span_words(pieces: list[tuple[str, int, dict]], start: int, end: int, page_n: int) -> list[dict]:
    words: list[dict] = []
    seen: set[int] = set()
    for offset in range(start, end):
        word_page, word = pieces[offset][1], pieces[offset][2]
        if word_page != page_n or id(word) in seen:
            continue
        seen.add(id(word))
        words.append(word)
    return words


def _union(span: list[tuple]) -> dict:
    words = [item[-1] for item in span]
    left = min(word["x"] for word in words)
    top = min(word["y"] for word in words)
    right = max(word["x"] + word["w"] for word in words)
    bottom = max(word["y"] + word["h"] for word in words)
    return {"x": left, "y": top, "w": right - left, "h": bottom - top}


def _compact(text: str) -> str:
    return "".join(text.split())


def _px(value) -> int:
    try:
        return round(float(value) * 96 / 72)
    except (TypeError, ValueError):
        return 0
