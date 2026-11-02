"""Field marks that turn a preview cell into an input.

``[[]]`` marks a cell as fillable and still unmapped: the preview asks which
field it is. A whole-cell ``[[owner.field]]``, or the first comment line as the
field key, ``[[owner.field]]``, or ``field:owner.field``, maps it immediately.
Any other text stays ordinary cell content.
"""

from __future__ import annotations

import re

_BRACKET = re.compile(r"^\[\[\s*([^\[\]]+?)\s*\]\]$")
_EMPTY_MARK = re.compile(r"^\[\[\s*\]\]$")
_FIELD_PREFIX = re.compile(r"(?i:field)\s*:\s*(.+)")
_PERSON = re.compile(r"^(DIR|SH|CS)(\d+(?:-\d+(?:\.\d+)?)?)?\.(.+)$", re.IGNORECASE)
_BRANCH = re.compile(r"^(BR[0-9A-Za-z-]+)\.(.+)$", re.IGNORECASE)
_CANON_OWNER = re.compile(r"^(?:(?:DIR|SH|CS)(?:\d+(?:-\d+(?:\.\d+)?)?)?|BR[0-9A-Z-]+)$")
_QUERY_OWNER = re.compile(
    r"(?i)(com\s*sec(?:\s*\d+(?:-\d+(?:\.\d+)?)?)?"
    r"|公司|註冊地址|股本"
    r"|(?<![A-Za-z])company(?![A-Za-z])"
    r"|(?<![A-Za-z])shares(?![A-Za-z])"
    r"|br\s*[0-9A-Za-z-]+"
    r"|(?:dir|dr|sh|cs)\s*\d+(?:-\d+(?:\.\d+)?)?"
    r"|(?<![A-Za-z])ro(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:dir|dr|sh|cs)(?![A-Za-z]))"
)
_PERSON_SUFFIXES = (
    "name_en", "name_zh", "dob", "id", "id_type", "ci", "br", "email", "tel",
    "appoint", "resign", "ubo",
    "address.1", "address.2", "address.3", "address.4", "address.from", "address.to",
    "shares", "share_total", "consideration", "total_consideration",
    "share_class", "share_type", "share_date", "share_end",
    "transfer.date", "transfer.from", "transfer.to", "transfer.shares", "transfer.consideration",
    "trust.bo", "trust.trustee", "trust.date", "trust.shares",
    "license", "license_reason",
)

_COMPANY = {
    "no": ("公司編號", "text"),
    "name_en": ("英文名稱", "text"),
    "name_zh": ("中文名稱", "text"),
    "ci": ("CI", "text"),
    "br": ("BR", "text"),
    "incorporation": ("成立日期", "date"),
    "issued_shares": ("已發行股數", "number"),
}
_PERSON_FIELDS = {
    "name_en": ("英文名稱", "text"),
    "name_zh": ("中文名稱", "text"),
    "dob": ("出生日期", "date"),
    "id": ("證件號碼", "text"),
    "ci": ("CI", "text"),
    "br": ("BR", "text"),
    "appoint": ("委任日期", "date"),
    "resign": ("辭任日期", "date"),
    "email": ("Email", "text"),
    "tel": ("電話", "text"),
    "shares": ("股數", "number"),
    "share_total": ("累計股數", "number"),
    "consideration": ("代價", "number"),
    "total_consideration": ("總代價", "number"),
    "share_class": ("股份類別", "text"),
    "share_type": ("股份種類", "text"),
    "share_date": ("持股日期", "date"),
    "share_end": ("持股結束日期", "date"),
    "ubo": ("是否 UBO", "bool"),
}
_TRANSFER = {
    "date": ("轉讓日期", "date"),
    "from": ("轉讓自", "text"),
    "to": ("轉讓至", "text"),
    "shares": ("轉讓股數", "number"),
    "consideration": ("轉讓代價", "number"),
}
_TRUST = {
    "bo": ("DT BO", "text"),
    "trustee": ("DT Trustee", "text"),
    "date": ("DT 日期", "date"),
    "shares": ("DT 股數", "number"),
}
_CAPITAL = {
    "no_of_shares": ("股數", "number"),
    "total_no_of_shares": ("累計股數", "number"),
    "consideration": ("代價", "number"),
    "total_consideration": ("總代價", "number"),
    "currency": ("貨幣", "text"),
    "class": ("類別", "text"),
    "type": ("種類", "text"),
    "date": ("日期", "date"),
}
_ID_OPTIONS = (
    {"value": "hkid", "label": "HKID"},
    {"value": "passport", "label": "Passport"},
)
_TYPE_OPTIONS = (
    {"value": "private", "label": "私人 Private"},
    {"value": "public", "label": "公眾 Public"},
)


def _pack(key: str, label: str, kind: str = "text", options=()) -> dict:
    return {"key": key, "label": label, "kind": kind, "options": [dict(item) for item in options]}


def _address(owner: str, field: str, *, dates: str) -> dict | None:
    line = re.fullmatch(r"address\.([1-4])", field)
    if line:
        return _pack(f"{owner}.address.{line.group(1)}", f"{owner} 地址 {line.group(1)}")
    if dates == "short" and field in {"from", "address.from"}:
        return _pack(f"{owner}.from", f"{owner} 開始日期", "date")
    if dates == "short" and field in {"to", "address.to"}:
        return _pack(f"{owner}.to", f"{owner} 結束日期", "date")
    if dates == "address" and field == "address.from":
        return _pack(f"{owner}.address.from", f"{owner} 地址開始日期", "date")
    if dates == "address" and field == "address.to":
        return _pack(f"{owner}.address.to", f"{owner} 地址結束日期", "date")
    return None


def _dotted(owner: str, field: str, table: dict[str, tuple[str, str]], prefix: str) -> dict | None:
    if not field.startswith(prefix + "."):
        return None
    name, kind = table.get(field[len(prefix) + 1:], (None, None))
    if name is None:
        return None
    return _pack(f"{owner}.{field}", f"{owner} {name}", kind)


def _person(owner: str, field: str) -> dict | None:
    address = _address(owner, field, dates="address")
    if address:
        return address
    transfer = _dotted(owner, field, _TRANSFER, "transfer")
    if transfer:
        return transfer
    trust = _dotted(owner, field, _TRUST, "trust")
    if trust:
        return trust
    if field == "id_type":
        return _pack(f"{owner}.id_type", f"{owner} 證件種類", "choice", _ID_OPTIONS)
    if owner.startswith("CS") and field == "license":
        return _pack(f"{owner}.license", f"{owner} 無須領有牌照", "bool")
    if owner.startswith("CS") and field == "license_reason":
        return _pack(f"{owner}.license_reason", f"{owner} 牌照原因")
    name, kind = _PERSON_FIELDS.get(field, (None, None))
    if name is None:
        return None
    return _pack(f"{owner}.{field}", f"{owner} {name}", kind)


def _branch(owner: str, field: str) -> dict | None:
    address = _address(owner, field, dates="short")
    if address:
        return address
    if field == "br":
        return _pack(f"{owner}.br", f"{owner} BR")
    return None


def resolve_fill_token(token: str) -> dict | None:
    """Return the fillable field for one mark, or None when the mark is not a field."""
    text = str(token or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    lower = text.lower()
    if lower.startswith("company."):
        field = lower.split(".", 1)[1]
        if field == "type":
            return _pack("company.type", "公司類別", "choice", _TYPE_OPTIONS)
        name, kind = _COMPANY.get(field, (None, None))
        return _pack(f"company.{field}", name, kind) if name else None
    if lower.startswith("ro."):
        return _address("RO", lower.split(".", 1)[1], dates="short")
    if lower.startswith("shares."):
        field = lower.split(".", 1)[1]
        name, kind = _CAPITAL.get(field, (None, None))
        return _pack(f"SHARES.{field}", f"股本 {name}", kind) if name else None
    person = _PERSON.fullmatch(text)
    if person:
        owner = person.group(1).upper() + (person.group(2) or "")
        return _person(owner, person.group(3).lower())
    branch = _BRANCH.fullmatch(text)
    if branch:
        return _branch(branch.group(1).upper(), branch.group(2).lower())
    return None


def _candidate(text: str) -> str:
    raw = str(text or "").strip()
    bracket = _BRACKET.fullmatch(raw)
    if bracket:
        raw = bracket.group(1).strip()
    prefixed = _FIELD_PREFIX.fullmatch(raw)
    if prefixed:
        raw = prefixed.group(1).strip()
    return raw


def unmapped_fill() -> dict:
    """A ``[[]]`` cell: fillable, waiting for a field to be chosen."""
    return {"key": "", "label": "選擇欄位", "kind": "pick", "options": []}


def _is_unmapped_mark(text: str) -> bool:
    return bool(_EMPTY_MARK.fullmatch(str(text or "").strip()))


_INLINE = re.compile(r"\[\[\s*([^\[\]]*?)\s*\]\]")
_TICK_TOKEN = re.compile(r"^(?:\.{2,}|…+|⋯+|．{2,}|。{2,})$")
# One opening bracket is enough. The whole cluster, including a leftover "]]", is the tick.
_LOOSE_TICK = re.compile(r"[\[［]{1,2}\s*(?:\.{2,}|…+|⋯+|．{2,}|。{2,})\s*[\]］]{1,3}")
# ☐ and □ are empty boxes. ☑ is checked and ☒ is crossed. The mark itself is not text.
_BALLOT = {
    "\u2610": False,
    "\u25a1": False,
    "\u25a2": False,
    "\u25fb": False,
    "\u25fd": False,
    "\u2b1c": False,
    "\u2611": True,
    "\u2612": True,
}
_BALLOT_MARK = re.compile("[" + "".join(_BALLOT) + "]")


def _is_tick_token(token: str) -> bool:
    """``[[...]]`` or ``[[…]]`` marks a checkbox and is not a field name."""
    return bool(_TICK_TOKEN.fullmatch(re.sub(r"\s+", "", str(token or ""))))


def inline_parts(value_text: str) -> list[dict] | None:
    """Split a cell that mixes ordinary text with ``[[field]]`` marks.

    A mark that names a field becomes that field. Any other text, including an
    unrecognised mark, stays in place. A cell that is only one mark is left to
    ``preview_fill``.
    """
    raw = str(value_text or "")
    if not _INLINE.search(raw) and not _LOOSE_TICK.search(raw) and not _BALLOT_MARK.search(raw):
        return None
    stripped = raw.strip()
    if _is_unmapped_mark(stripped):
        return None
    if _BRACKET.fullmatch(stripped) and resolve_fill_token(_candidate(stripped)):
        return None
    parts: list[dict] = []

    def add_text(text: str) -> None:
        if not text:
            return
        if parts and list(parts[-1]) == ["text"]:
            parts[-1]["text"] += text
        else:
            parts.append({"text": text})

    position = 0
    for match in _INLINE.finditer(raw):
        add_text(raw[position:match.start()])
        token = match.group(1).strip()
        if _is_tick_token(token):
            parts.append({"tick": True})
            position = match.end()
            continue
        spec = resolve_fill_token(token) if token else None
        if spec is None and token:
            add_text(match.group(0))
        elif spec is None:
            parts.append({"pick": True})
        else:
            parts.append({"field": spec})
        position = match.end()
    add_text(raw[position:])
    cleaned: list[dict] = []
    for part in parts:
        if "text" not in part:
            cleaned.append(part)
            continue
        cursor = 0
        for tick in _LOOSE_TICK.finditer(part["text"]):
            add_piece = part["text"][cursor:tick.start()]
            if add_piece:
                if cleaned and list(cleaned[-1]) == ["text"]:
                    cleaned[-1]["text"] += add_piece
                else:
                    cleaned.append({"text": add_piece})
            cleaned.append({"tick": True})
            cursor = tick.end()
        tail = part["text"][cursor:]
        if tail:
            if cleaned and list(cleaned[-1]) == ["text"]:
                cleaned[-1]["text"] += tail
            else:
                cleaned.append({"text": tail})
    with_boxes: list[dict] = []
    for part in cleaned:
        if "text" not in part:
            with_boxes.append(part)
            continue
        cursor = 0
        for box in _BALLOT_MARK.finditer(part["text"]):
            add_piece = part["text"][cursor:box.start()]
            if add_piece:
                if with_boxes and list(with_boxes[-1]) == ["text"]:
                    with_boxes[-1]["text"] += add_piece
                else:
                    with_boxes.append({"text": add_piece})
            with_boxes.append({"tick": True, "checked": _BALLOT[box.group()]})
            cursor = box.end()
        tail = part["text"][cursor:]
        if tail:
            if with_boxes and list(with_boxes[-1]) == ["text"]:
                with_boxes[-1]["text"] += tail
            else:
                with_boxes.append({"text": tail})
    return with_boxes or None


def preview_fill(value_text: str, comment_text: str = "") -> tuple[dict | None, str]:
    """Return ``(field, text to display)``. A bracket mark hides itself."""
    raw = str(value_text or "").strip()
    if _is_unmapped_mark(raw):
        return unmapped_fill(), ""
    if _BRACKET.fullmatch(raw):
        spec = resolve_fill_token(_candidate(raw))
        if spec:
            return spec, ""
        return None, value_text
    first = comment_text.splitlines()[0].strip() if str(comment_text or "").strip() else ""
    if first and (_is_unmapped_mark(first) or _is_unmapped_mark(_candidate(first))):
        return unmapped_fill(), value_text
    if first:
        spec = resolve_fill_token(_candidate(first))
        if spec:
            return spec, value_text
    return None, value_text


def _canon_owner(value: str) -> str | None:
    text = re.sub(r"\s+", "", str(value or "")).upper().replace("!", "1")
    if text in {"公司", "COMPANY"}:
        return "company"
    if text in {"註冊地址", "RO"}:
        return "RO"
    if text in {"股本", "SHARES"}:
        return "SHARES"
    if text.startswith("COMSEC"):
        text = "CS" + text[6:]
    if text.startswith("DR") and not text.startswith("DIR"):
        text = "DIR" + text[2:]
    if text == "COMPANY":
        return "company"
    if _CANON_OWNER.fullmatch(text):
        return text
    return None


def _group_for(key: str) -> str:
    owner = key.split(".", 1)[0]
    if owner == "company":
        return "公司"
    if owner == "RO":
        return "註冊地址"
    if owner == "SHARES":
        return "股本"
    if owner.startswith("BR"):
        return "分行"
    return "人物"


def _fields_for(owner: str) -> list[dict]:
    canon = _canon_owner(owner)
    if not canon:
        return []
    if canon == "company":
        keys = [f"company.{key}" for key in (
            "no", "name_en", "name_zh", "ci", "br", "incorporation", "issued_shares", "type",
        )]
        group = "公司"
    elif canon == "RO":
        keys = [f"RO.{key}" for key in ("address.1", "address.2", "address.3", "address.4", "from", "to")]
        group = "註冊地址"
    elif canon == "SHARES":
        keys = [f"shares.{key}" for key in _CAPITAL]
        group = "股本"
    elif canon.startswith("BR"):
        keys = [f"{canon}.{key}" for key in ("address.1", "address.2", "address.3", "address.4", "from", "to", "br")]
        group = "分行"
    else:
        keys = [f"{canon}.{key}" for key in _PERSON_SUFFIXES]
        group = "人物"
    items = []
    for key in keys:
        spec = resolve_fill_token(key)
        if spec is None:
            continue
        item = dict(spec)
        item["group"] = group
        items.append(item)
    return items


def fill_owners(value: object) -> list[str]:
    """Owners named by a row label, used to suggest fields for a ``[[]]`` cell."""
    from .excel_parser import parse_label

    label = parse_label(value)
    if label is None:
        return []
    if label.kind == "COMPANY":
        return ["company"]
    if label.kind in {"RO", "SHARES"}:
        return [label.kind]
    if label.kind == "BRANCH":
        owner = _canon_owner(label.text)
        return [owner] if owner else []
    if label.kind == "ROLE":
        found = []
        for role in label.roles:
            owner = _canon_owner(role)
            if owner and owner not in found:
                found.append(owner)
        return found
    return []


def _split_query(query: str) -> tuple[list[str], list[str]]:
    owners: list[str] = []
    words: list[str] = []
    position = 0
    for match in _QUERY_OWNER.finditer(query):
        gap = query[position:match.start()].strip(" .")
        if gap:
            words.extend(gap.split())
        owner = _canon_owner(match.group(0))
        if owner and owner not in owners:
            owners.append(owner)
        position = match.end()
    tail = query[position:].strip(" .")
    if tail:
        words.extend(tail.split())
    return owners, [word for word in words if word]


def _haystack(spec: dict) -> str:
    options = " ".join(str(item.get("label") or "") for item in spec.get("options") or [])
    return f"{spec.get('group') or ''} {spec.get('label') or ''} {spec.get('key') or ''} {options}".casefold()


def suggest_fields(query: str = "", owners: list[str] | None = None, limit: int = 40) -> list[dict]:
    """Searchable field choices for one ``[[]]`` cell.

    A row label such as DIR1 or RO narrows the list. Typed text can name another
    owner (``DIR2 電話``) or a field (``英文名稱``).
    """
    text = " ".join(str(query or "").split())[:80]
    hint: list[str] = []
    for owner in owners or []:
        canon = _canon_owner(owner)
        if canon and canon not in hint:
            hint.append(canon)
    typed, words = _split_query(text)
    targets = typed or hint
    if not words:
        pool_owners = targets or ["company", "RO", "SHARES"]
    else:
        pool_owners = list(targets)
        if not typed:
            for extra in ("company", "RO", "SHARES"):
                if extra not in pool_owners:
                    pool_owners.append(extra)
    pool: list[dict] = []
    seen: set[str] = set()
    for owner in pool_owners:
        for spec in _fields_for(owner):
            if spec["key"] in seen:
                continue
            seen.add(spec["key"])
            pool.append(spec)
    exact = resolve_fill_token(text) if text and " " not in text else None
    if exact is not None:
        exact = dict(exact)
        exact["group"] = _group_for(exact["key"])
    if words:
        pool = [spec for spec in pool if all(word.casefold() in _haystack(spec) for word in words)]
    if exact is not None and all(exact["key"] != spec["key"] for spec in pool):
        pool.insert(0, exact)

    def rank(spec: dict) -> int:
        owner = spec["key"].split(".", 1)[0]
        score = 0
        if owner in typed:
            score += 5
        elif owner in hint:
            score += 3
        if words and all(word.casefold() in str(spec["label"]).casefold() for word in words):
            score += 2
        if text.casefold() == spec["key"].casefold():
            score += 10
        return score

    ranked = [spec for _, spec in sorted(enumerate(pool), key=lambda pair: (-rank(pair[1]), pair[0]))]
    return ranked[:max(1, min(int(limit), 40))]
