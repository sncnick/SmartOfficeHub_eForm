"""Ask a local LM Studio server which form cells are ticks or text fields.

The workbook text stays on this computer. Only a loopback address is accepted,
and company records are never included.
"""

from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .excel_fields import inline_parts
from .excel_parser import clip_to_print_area

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "host.docker.internal", "::1"}
_BLANKISH = re.compile(r"[\s_．.·…—\-]*")


class LmStudioError(Exception):
    pass


def _in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def lmstudio_base(override: str | None = None) -> str:
    """OpenAI-compatible base URL. Refuses anything that is not this computer.

    Inside Docker, ``127.0.0.1`` is the container, so a loopback address is
    rewritten to the host running LM Studio.
    """
    raw = (override or os.environ.get("CDD_LMSTUDIO_URL") or "http://127.0.0.1:1234/v1").strip().rstrip("/")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _LOCAL_HOSTS or parsed.username or parsed.password:
        raise LmStudioError("LM Studio 只可以連本機")
    if host in {"127.0.0.1", "localhost", "::1"} and _in_docker():
        host = "host.docker.internal"
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{host}{port}{path}"


def _request(path: str, payload: dict | None = None, timeout: float = 8, base: str | None = None) -> dict:
    base = base or lmstudio_base()
    request = Request(
        f"{base}{path}",
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - host is allowlisted above
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise LmStudioError("LM Studio 拒絕了這次分析") from exc
    except URLError as exc:
        raise LmStudioError(f"連不到 {base}。請確認 LM Studio 已開啟本機伺服器，或在側欄改位址") from exc
    except TimeoutError as exc:
        raise LmStudioError("本機 LM Studio 太久沒有回應") from exc
    except json.JSONDecodeError as exc:
        raise LmStudioError("LM Studio 沒有交回可用的結果") from exc


def _chat_models(models: list[str]) -> list[str]:
    chat = [model for model in models if "embed" not in model.lower()]
    return chat or models


def status(url: str | None = None) -> dict:
    """Whether a model is loaded. This does not read a workbook."""
    base = lmstudio_base(url)
    found = _request("/models", timeout=3, base=base)
    models = [item.get("id") for item in found.get("data") or [] if item.get("id")]
    return {"ok": bool(models), "models": _chat_models(models), "url": base}


def _already(text: str) -> str:
    parts = inline_parts(text) or []
    if any(part.get("tick") for part in parts):
        return "tick"
    if any(part.get("pick") or part.get("field") for part in parts):
        return "text"
    stripped = str(text or "").strip()
    if re.fullmatch(r"[\[［]{2}\s*[\]］]{2}", stripped):
        return "text"
    return ""


def _blankish(text: str) -> bool:
    return _BLANKISH.fullmatch(str(text or "")) is not None


def _sides(border) -> str:
    if not isinstance(border, dict):
        return ""
    return "".join(side for side in ("l", "r", "t", "b") if border.get(side))


def _full_box(item: dict) -> bool:
    return set(item.get("sides") or "") >= set("lrtb")


def _header(text: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", text or "")
    return len(letters) >= 4 and letters.isupper()


def _bilingual_label(text: str) -> bool:
    raw = str(text or "")
    return bool(re.search(r"[A-Za-z]", raw) and re.search(r"[\u4e00-\u9fff]", raw))


def _is_label(text: str) -> bool:
    return str(text or "").strip().endswith((":", "："))


def _instruction(text: str) -> bool:
    """A note such as 英文 tells the neighbouring cell which language to use."""
    return bool(re.fullmatch(r"(?:英文|中文|english|chinese)", str(text or "").strip(), re.IGNORECASE))


def _cell_formula(text: str) -> bool:
    return bool(re.fullmatch(
        r"=\s*(?:(?:\[[^\]]+\])?(?:'(?:[^']|'')+'|[A-Za-z_][\w.]*)!)?\$?[A-Z]{1,3}\$?\d+\s*",
        str(text or "").strip(),
        re.IGNORECASE,
    ))


def _sheet_view(grid: dict) -> tuple[list[str], list[dict], dict[tuple[int, str], tuple[int, str]]]:
    clipped = clip_to_print_area(grid)
    if clipped:
        columns = clipped["columns"]
        rows = clipped["rows"]
        merges = clipped.get("merges") or []
    else:
        columns = list(grid.get("columns") or [])
        rows = [{"n": index, **row} for index, row in enumerate(grid.get("rows") or [], start=1)]
        merges = grid.get("merges") or []
    covered: dict[tuple[int, str], tuple[int, str]] = {}
    for merge in merges:
        if merge["c"] - 1 >= len(columns):
            continue
        origin = (merge["r"], columns[merge["c"] - 1])
        for row_number in range(merge["r"], merge["r"] + merge["rs"]):
            for col_number in range(merge["c"], merge["c"] + merge["cs"]):
                if col_number - 1 >= len(columns):
                    continue
                covered[(row_number, columns[col_number - 1])] = origin
    return columns, rows, covered


def cells_for_model(grid: dict) -> tuple[list[dict], dict[str, dict]]:
    """Cell addresses and short labels from one sheet. Company records are not included."""
    columns, rows, covered = _sheet_view(grid)
    cells = []
    lookup: dict[str, dict] = {}
    for row in rows:
        for index, column in enumerate(columns):
            origin = covered.get((row["n"], column))
            if origin and origin != (row["n"], column):
                continue
            values = row.get("cells") or []
            borders = row.get("borders") or []
            fields = row.get("fields") or []
            text = str(values[index] if index < len(values) else "" or "")
            border = borders[index] if index < len(borders) else None
            field = fields[index] if index < len(fields) else None
            if not text.strip() and not border and not isinstance(field, dict):
                continue
            shown = text.strip()
            already = _already(text)
            if not already and isinstance(field, dict) and field.get("kind"):
                already = "tick" if field.get("kind") == "bool" else "text"
            item = {
                "cell": f"{column}{row['n']}",
                "row": row["n"],
                "col": column,
                "text": (shown[:160] if shown else "（空白）"),
                "already": already,
                "blank": _blankish(shown),
                "label": _is_label(shown),
                "instruction": _instruction(shown),
                "formula": _cell_formula(shown),
                "sides": _sides(border),
            }
            cells.append(item)
            lookup[item["cell"]] = item
            if len(cells) >= 250:
                break
        if len(cells) >= 250:
            break
    for (row_number, column), (origin_row, origin_col) in covered.items():
        origin = lookup.get(f"{origin_col}{origin_row}")
        if origin:
            lookup[f"{column}{row_number}"] = origin
    return cells, lookup


def _model_id(preferred: str | None, base: str) -> str:
    chosen = (preferred or os.environ.get("CDD_LMSTUDIO_MODEL") or "").strip()
    found = status(base)
    models = found["models"]
    if chosen and chosen in models:
        return chosen
    if not models:
        raise LmStudioError("LM Studio 還沒有載入模型")
    return models[0]


def _extract_json(text: str) -> dict:
    raw = str(text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise LmStudioError("LM Studio 沒有交回欄位清單")
    try:
        found = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LmStudioError("LM Studio 沒有交回欄位清單") from exc
    if not isinstance(found, dict):
        raise LmStudioError("LM Studio 沒有交回欄位清單")
    return found


def accepted_marks(payload: dict, cells: list[dict], lookup: dict[str, dict] | None = None) -> list[dict]:
    """Keep tick or text decisions. A filled example such as a city name can be a text field."""
    by_cell = lookup or {item["cell"]: item for item in cells}
    marks = payload.get("marks") if isinstance(payload, dict) else None
    if not isinstance(marks, list):
        return []
    accepted = []
    seen = set()
    for item in marks:
        if not isinstance(item, dict):
            continue
        cell = str(item.get("cell") or "").replace("$", "").upper()
        kind = str(item.get("kind") or "").lower()
        current = by_cell.get(cell)
        if current is None or kind not in {"tick", "text"} or current["already"]:
            continue
        cell = current["cell"]
        if cell in seen:
            continue
        if kind == "text" and (current["label"] or current.get("instruction") or (not current["blank"] and not current.get("formula") and len(current["text"]) > 80)):
            continue
        if kind == "tick" and current["blank"]:
            continue
        seen.add(cell)
        accepted.append({"cell": cell, "row": current["row"], "col": current["col"], "kind": kind})
    return accepted


def _prompt(sheet: str, cells: list[dict]) -> str:
    lines = []
    for item in cells:
        state = f" 已是{item['already']}" if item["already"] else ""
        lines.append(f"{item['cell']}\t{item['text']}{state}")
    return (
        "你在看一頁表格。請判斷哪些格應該變成勾選框，哪些格應該變成可對應的文字欄 [[]]。\n"
        "只回 JSON，格式是 {\"marks\":[{\"cell\":\"E27\",\"kind\":\"text\"}]}。\n"
        "kind 只可以是 tick 或 text。\n"
        "tick：這格是勾選、是非、選項，或含有 ☐、☒、[[…]]、[[...]] 的句子。已是tick 的不要再列。\n"
        "text：這格是要填寫的答案，包括已經寫了範例的格子，例如 Location 旁邊的 Hong Kong，以及只引用另一格的公式如 =C19。它要變成 [[]]。空白、底線也算。\n"
        "不要把欄名、標題，或「英文」「中文」這類填寫指示轉成 text。以冒號結尾的欄名也不要列。\n"
        "不確定就不要列。cell 必須是下面出現過的位址。\n"
        f"工作表：{sheet}\n"
        + "\n".join(lines)
    )


def _mark(item: dict) -> dict:
    return {"cell": item["cell"], "row": item["row"], "col": item["col"], "kind": "text"}


def sample_value_marks(cells: list[dict]) -> list[dict]:
    """A short answer written beside a label, such as Hong Kong beside Location, is a text field."""
    marks = []
    previous = None
    for item in cells:
        if (
            previous
            and previous["row"] == item["row"]
            and previous["label"]
            and not item["already"]
            and not item["label"]
            and not item.get("instruction")
            and not item["blank"]
            and len(item["text"]) <= 40
        ):
            marks.append(_mark(item))
        previous = item
    return marks


def answer_field_marks(cells: list[dict]) -> list[dict]:
    """Boxed answer cells become text fields, including blanks beside a label."""
    grouped: dict[int, list[dict]] = {}
    for item in cells:
        grouped.setdefault(item["row"], []).append(item)
    marks = []
    for items in grouped.values():
        for index, item in enumerate(items):
            if item["already"] or item["label"] or item.get("instruction"):
                continue
            if item.get("formula"):
                marks.append(_mark(item))
                continue
            if _header(item["text"]) or _bilingual_label(item["text"]) or not _full_box(item):
                continue
            if item["blank"]:
                marks.append(_mark(item))
                continue
            if len(item["text"]) > 40:
                continue
            later = items[index + 1:]
            if any(_full_box(other) and not other["already"] and not other["label"] and (other["blank"] or len(other["text"]) <= 40) for other in later):
                continue
            marks.append(_mark(item))
    chosen = {item["cell"] for item in marks}
    chosen.update(item["cell"] for item in cells if item["already"] == "text")
    for item in cells:
        if item["cell"] in chosen or item["already"] or item["label"] or not item["blank"] or not item["sides"]:
            continue
        same_row = any(other["row"] == item["row"] and other["cell"] in chosen for other in cells)
        above = any(other["row"] == item["row"] - 1 and other["col"] == item["col"] and other["cell"] in chosen for other in cells)
        if same_row or above:
            marks.append(_mark(item))
            chosen.add(item["cell"])
    return marks


def analyze_sheet(grid: dict, url: str | None = None, model: str | None = None) -> dict:
    """Classify one sheet. Returns marks the viewer can apply without editing the file."""
    cells, lookup = cells_for_model(grid)
    pending = [item for item in cells if not item["already"]]
    if not pending:
        return {"model": "", "marks": [], "url": lmstudio_base(url)}
    base = lmstudio_base(url)
    model = _model_id(model, base)
    found = _request("/chat/completions", {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "你只輸出 JSON 物件，不要加說明。"},
            {"role": "user", "content": _prompt(grid.get("sheet") or "", cells)},
        ],
    }, timeout=180, base=base)
    try:
        content = found["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LmStudioError("LM Studio 沒有交回欄位清單") from exc
    marks = accepted_marks(_extract_json(content), cells, lookup)
    seen = {item["cell"] for item in marks}
    for extra in sample_value_marks(cells) + answer_field_marks(cells):
        if extra["cell"] not in seen:
            marks.append(extra)
            seen.add(extra["cell"])
    return {"model": model, "url": base, "marks": marks}
