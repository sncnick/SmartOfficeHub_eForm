"""Add a director, shareholder or company secretary, mark a record outdated, or edit a form field."""

from __future__ import annotations

import copy
import re
import uuid

SECTIONS = ("directors", "shareholders", "company_secretaries", "other_parties")
KIND_SECTION = {"DIR": "directors", "SH": "shareholders", "CS": "company_secretaries"}
GENERAL_FIELDS = {"name_en", "name_zh", "ci_no", "br_no", "incorporation_date"}
PARTY_FIELDS = {"name_en", "name_zh", "date_of_birth", "ci_no", "br_no", "appoint_date", "resign_date"}
LIST_FIELDS = {"id_numbers", "emails", "phones", "other"}
SHARE_FIELDS = {
    "no_of_shares", "total_no_of_shares", "consideration", "total_consideration",
    "currency", "class", "type", "date",
}
NUMERIC_FIELDS = {
    "no_of_shares", "total_no_of_shares", "consideration", "total_consideration", "issued_shares",
}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def summary(doc: dict) -> dict:
    general = doc.get("general") or {}
    labels = []
    for section in SECTIONS:
        for party in doc.get(section) or []:
            if isinstance(party, dict) and party.get("label"):
                labels.append(str(party["label"]))
    issues = [issue for issue in doc.get("issues") or [] if issue.get("severity") != "info"]
    return {
        "id": doc.get("id"),
        "company_no": doc.get("company_no") or "",
        "title": general.get("name_en") or general.get("name_zh") or "",
        "status": doc.get("status") or "",
        "active": bool(doc.get("active", True)),
        "label_color": doc.get("label_color"),
        "element_count": len(labels),
        "labels": labels,
        "issue_count": len(issues),
        "updated_at": doc.get("updated_at"),
    }


def _stamp(record: dict) -> bool:
    changed = False
    if not isinstance(record, dict):
        return False
    if not record.get("record_id"):
        record["record_id"] = record.get("party_id") or new_id()
        changed = True
    if "roles" in record or "shareholdings" in record or "name_en" in record:
        if not record.get("party_id"):
            record["party_id"] = record["record_id"]
            changed = True
    return changed


def _address_parts(address: dict) -> list[str]:
    lines = [str(part or "").strip() for part in (address.get("lines") or [])]
    if any(lines):
        return [part for part in lines if part]
    text = str(address.get("address") or address.get("value") or "")
    return [part.strip() for part in text.replace("，", ",").replace("\n", ",").split(",") if part.strip()]


def _fit_four(parts: list[str]) -> list[str]:
    if len(parts) > 4:
        parts = [", ".join(parts[:-3]), *parts[-3:]]
    while len(parts) < 4:
        parts.insert(0, "")
    return parts[:4]


def collapse_current_addresses(party: dict) -> bool:
    """One person has one current address block. Extra current rows join that block."""
    addresses = [item for item in (party.get("addresses") or []) if isinstance(item, dict)]
    current = [item for item in addresses if not item.get("outdated")]
    if len(current) <= 1:
        return False
    groups: list[list[str]] = []
    for item in current:
        parts = _address_parts(item)
        if parts and parts not in groups:
            groups.append(parts)
    merged = dict(current[-1])
    lines = _fit_four([part for group in groups for part in group])
    merged["lines"] = lines
    merged["address"] = ", ".join(part for part in lines if part)
    kept_row = current[-1].get("row")
    merged["alias_rows"] = [
        item.get("row") for item in current
        if item.get("row") is not None and item.get("row") != kept_row
    ]
    if not merged.get("from"):
        merged["from"] = next((item.get("from") for item in reversed(current) if item.get("from")), None)
    if not merged.get("to"):
        merged["to"] = next((item.get("to") for item in reversed(current) if item.get("to")), None)
    party["addresses"] = [item for item in addresses if item.get("outdated")] + [merged]
    return True


def _line_list(address: dict) -> list[str]:
    lines = [str(part or "").strip() for part in (address.get("lines") or [])]
    if len(lines) >= 4:
        return lines[:4]
    return _fit_four(_address_parts(address))


def _is_bypass(address: dict) -> bool:
    from .excel_parser import follows_address_fields
    return not follows_address_fields(_line_list(address))


def _remember_undefined(party: dict, address: dict) -> bool:
    """Keep a removed address visible. The same Excel row is not stored twice."""
    text = str(address.get("value") or address.get("address") or "").strip()
    if not text:
        text = ", ".join(part for part in _line_list(address) if part)
    if not text:
        return False
    party.setdefault("other", [])
    row = address.get("row")
    for item in party["other"]:
        if str(item.get("value") or "") != text:
            continue
        if row is None or item.get("row") == row:
            return False
    party["other"].append({
        "value": text,
        "row": row,
        "column": address.get("column"),
        "from": address.get("from"),
        "to": address.get("to"),
        "outdated": bool(address.get("outdated")),
        "outdated_reasons": list(address.get("outdated_reasons") or []),
    })
    return True


def single_secretary_address(party: dict) -> bool:
    """Drop secretary address blocks that were not filled from field 1 through field 4."""
    return park_misfilled_addresses(party)


def park_misfilled_addresses(party: dict) -> bool:
    """An address block is fields 1, 2, 3, 4 in order. Anything else belongs in 未定義."""
    addresses = [item for item in (party.get("addresses") or []) if isinstance(item, dict)]
    if not addresses:
        return False
    kept = []
    changed = False
    for item in addresses:
        lines = _line_list(item)
        if any(lines) and _is_bypass(item):
            _remember_undefined(party, item)
            changed = True
            continue
        kept.append(item)
    if len(kept) != len(addresses):
        party["addresses"] = kept
        changed = True
    return changed


def reclaim_outdated_addresses(party: dict, parsed_addresses: list) -> bool:
    """Put an outdated address back on the address form when it was stored under 未定義."""
    parsed = [item for item in (parsed_addresses or []) if isinstance(item, dict)]
    party.setdefault("addresses", [])
    party.setdefault("other", [])
    address_rows = {item.get("row") for item in party["addresses"] if isinstance(item, dict) and item.get("row") is not None}
    changed = False
    for item in parsed:
        if not item.get("outdated") or _is_bypass(item):
            continue
        row = item.get("row")
        if row is not None and row in address_rows:
            continue
        lines = _line_list(item)
        text = str(item.get("value") or item.get("address") or "").strip()
        joined = ", ".join(part for part in lines if part)
        if not text:
            text = joined
        kept_other = []
        removed = False
        for other in party["other"]:
            stored = str(other.get("value") or "") if isinstance(other, dict) else ""
            same = isinstance(other, dict) and other.get("row") == row and stored in {text, joined}
            if same and not removed:
                removed = True
                continue
            kept_other.append(other)
        if not removed:
            continue
        party["other"] = kept_other
        party["addresses"].append({
            "value": item.get("value") or text,
            "address": ", ".join(part for part in lines if part),
            "lines": lines,
            "row": row,
            "column": item.get("column"),
            "from": item.get("from"),
            "to": item.get("to"),
            "outdated": True,
            "outdated_reasons": list(item.get("outdated_reasons") or []),
        })
        if row is not None:
            address_rows.add(row)
        changed = True
    return changed


def restore_address_issue_fields(stored: dict, parsed: dict) -> bool:
    """Point a warning back at the address once that Excel row is an address again."""
    parsed_fields = {}
    for issue in parsed.get("issues") or []:
        field = str(issue.get("field") or "")
        if field.startswith("address."):
            parsed_fields.setdefault((issue.get("row"), issue.get("code")), field)
    address_rows = set()
    for section in SECTIONS:
        for party in stored.get(section) or []:
            if not isinstance(party, dict):
                continue
            for item in party.get("addresses") or []:
                if isinstance(item, dict) and item.get("row") is not None:
                    address_rows.add(item.get("row"))
    changed = False
    for issue in stored.get("issues") or []:
        if issue.get("field") != "undefined" or issue.get("row") not in address_rows:
            continue
        field = parsed_fields.get((issue.get("row"), issue.get("code")))
        if not field:
            continue
        issue["field"] = field
        changed = True
    return changed


def remember_merged_rows(party: dict, parsed_addresses: list) -> bool:
    """Keep a warning aimed at an address row that was joined into the remaining block."""
    stored = [item for item in (party.get("addresses") or []) if isinstance(item, dict)]
    if not stored:
        return False
    parsed = [item for item in (parsed_addresses or []) if isinstance(item, dict)]
    kept = next((item for item in stored if any(item.get("row") == other.get("row") for other in parsed)), stored[-1])
    kept_row = kept.get("row")
    other_rows = {item.get("row") for item in (party.get("other") or []) if isinstance(item, dict)}
    extra = []
    for item in parsed:
        row = item.get("row")
        if row is None or row == kept_row or row in other_rows or row in extra:
            continue
        if any(other.get("row") == row for other in stored):
            continue
        extra.append(row)
    if not extra:
        return False
    current = [row for row in (kept.get("alias_rows") or []) if row not in extra and row != kept_row]
    updated = current + extra
    if updated == list(kept.get("alias_rows") or []):
        return False
    kept["alias_rows"] = updated
    return True


def retag_undefined_address_issues(company: dict) -> bool:
    """Point an address warning at 未定義 when that Excel row is no longer an address block."""
    other_rows: set = set()
    address_rows: set = set()
    for section in SECTIONS:
        for party in company.get(section) or []:
            if not isinstance(party, dict):
                continue
            for item in party.get("other") or []:
                if isinstance(item, dict) and item.get("row") is not None:
                    other_rows.add(item.get("row"))
            for item in party.get("addresses") or []:
                if isinstance(item, dict) and item.get("row") is not None:
                    address_rows.add(item.get("row"))
    changed = False
    for issue in company.get("issues") or []:
        field = str(issue.get("field") or "")
        row = issue.get("row")
        if row in other_rows and row not in address_rows and field.startswith("address."):
            issue["field"] = "undefined"
            changed = True
    return changed


def stamp_id_types(party: dict) -> bool:
    """Remember HKID or passport when the stored number already says which it is."""
    from .excel_parser import id_document_type
    changed = False
    for item in party.get("id_numbers") or []:
        if not isinstance(item, dict) or item.get("id_type") in {"hkid", "passport"}:
            continue
        guessed = id_document_type(item.get("value"))
        if guessed:
            item["id_type"] = guessed
            changed = True
    return changed


def mark_earlier_holdings(party: dict) -> bool:
    """Only the latest holding is current. Earlier rows are the transfer history."""
    holdings = [item for item in (party.get("shareholdings") or []) if isinstance(item, dict)]
    current_row = party.get("current_holding_row")
    calc = next((item for item in holdings if current_row is not None and item.get("row") == current_row
                 and not item.get("outdated")), None)
    if calc is None:
        return False
    changed = False
    before = True
    for item in holdings:
        if item is calc:
            before = False
            desired = False
        elif before and not item.get("outdated"):
            desired = True
        else:
            desired = bool(item.get("pre_transfer"))
        if bool(item.get("pre_transfer")) != desired:
            item["pre_transfer"] = desired
            changed = True
    return changed


def ensure_party_ids(company: dict) -> bool:
    changed = False
    for section in SECTIONS:
        for party in company.get(section) or []:
            changed = _stamp(party) or changed
            changed = stamp_id_types(party) or changed
            changed = park_misfilled_addresses(party) or changed
            if section == "company_secretaries":
                changed = single_secretary_address(party) or changed
            else:
                changed = collapse_current_addresses(party) or changed
            for holding in party.get("shareholdings") or []:
                changed = _stamp(holding) or changed
            changed = mark_earlier_holdings(party) or changed
            for key in ("addresses", "id_numbers", "emails", "phones", "other"):
                for item in party.get(key) or []:
                    changed = _stamp(item) or changed
    for key in ("registered_offices", "branches", "share_capital"):
        for record in company.get(key) or []:
            changed = _stamp(record) or changed
    changed = retag_undefined_address_issues(company) or changed
    return changed


def chosen_label(raw: str, kind: str | None = None) -> str:
    """Use the number the user typed. The same number may already exist."""
    text = re.sub(r"\s+", "", str(raw or ""))
    if not text:
        raise ValueError("請填寫編號，例如 DIR1。可以和現有編號相同，再把舊的一筆標為 OUTDATED。")
    if kind and re.fullmatch(r"\d+", text):
        text = f"{kind}{text}"
    if re.fullmatch(r"(DIR|SH|CS|BR|COMSEC)\d+", text, re.I):
        return text.upper()
    return text


def _number(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


def _is_date(value: str | None) -> bool:
    text = (value or "").strip()
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) or re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", text))


def _text(data: dict, key: str) -> str | None:
    value = str(data.get(key) or "").strip()
    return value or None


def add_party(company: dict, data: dict) -> dict:
    company = copy.deepcopy(company)
    kind = str(data.get("kind") or "").upper()
    section = KIND_SECTION.get(kind)
    if section is None:
        raise ValueError("只可以新增 DIR、SH 或 Com Sec")
    label = chosen_label(data.get("label"), kind)
    lines = [str(data.get(f"line{index}") or "").strip() for index in range(1, 5)]
    addresses = []
    if any(lines):
        addresses.append({
            "address": ", ".join(line for line in lines if line),
            "lines": lines,
            "from": None,
            "to": None,
            "row": None,
            "outdated": False,
            "outdated_reasons": [],
        })
    id_numbers = []
    id_number = _text(data, "id_number")
    if id_number:
        from .excel_parser import id_document_type
        chosen = str(data.get("id_type") or "").strip().lower()
        if chosen not in {"hkid", "passport"}:
            chosen = id_document_type(id_number) or ""
        id_numbers.append({
            "value": id_number, "row": None, "column": None,
            "id_type": chosen or None,
            "outdated": False, "outdated_reasons": [],
        })
    resign = _text(data, "resign_date")
    outdated = _is_date(resign)
    shares = _number(data.get("shares")) if kind == "SH" else None
    party = {
        "party_id": new_id(),
        "label": label,
        "label_raw": label,
        "roles": [label],
        "row": None,
        "outdated": outdated,
        "outdated_reasons": ["end_date"] if outdated else [],
        "added": True,
        "name_en": _text(data, "name_en"),
        "name_zh": _text(data, "name_zh"),
        "date_of_birth": _text(data, "date_of_birth"),
        "ci_no": _text(data, "ci_no") if kind == "CS" else None,
        "br_no": _text(data, "br_no") if kind == "CS" else None,
        "id_numbers": id_numbers,
        "addresses": addresses,
        "emails": [],
        "phones": [],
        "other": [],
        "appoint_date": _text(data, "appoint_date"),
        "appoint_date_row": None,
        "resign_date": resign,
        "resign_date_row": None,
        "ubo": str(data.get("ubo") or "").strip().lower() in {"1", "true", "yes", "on"},
        "shareholdings": [],
        "current_shares": shares,
        "current_holding_row": None,
        "current_consideration": None,
        "current_consideration_source": None,
        "percentage": None,
    }
    if shares is not None:
        party["shareholdings"].append({
            "label": label,
            "row": None,
            "no_of_shares": shares,
            "total_no_of_shares": shares,
            "percentage_excel": None,
            "consideration": None,
            "total_consideration": None,
            "class": None,
            "type": None,
            "date": party["appoint_date"],
            "end_date": resign,
            "transfer": None,
            "declaration_of_trust": None,
            "pre_transfer": False,
            "outdated": outdated,
            "outdated_reasons": list(party["outdated_reasons"]),
        })
        issued = company.get("issued_shares")
        company.setdefault("shareholding_summary", []).append({
            "name": party["name_en"] or party["name_zh"] or label,
            "label": label,
            "section": section,
            "row": None,
            "party_id": party["party_id"],
            "current_shares": shares,
            "percentage": (shares / issued) if isinstance(issued, (int, float)) and issued else None,
            "consideration": None,
            "consideration_source": None,
            "counted_in_total": not outdated,
        })
    company.setdefault(section, []).append(party)
    _refresh_counts(company)
    return company


def _lines(data: dict) -> list[str]:
    return [str(data.get(f"line{index}") or "").strip() for index in range(1, 5)]


def _address_record(data: dict) -> dict:
    lines = _lines(data)
    end = _text(data, "to")
    outdated = _is_date(end)
    return {
        "record_id": new_id(),
        "address": ", ".join(line for line in lines if line),
        "lines": lines,
        "from": _text(data, "from"),
        "to": end,
        "row": None,
        "outdated": outdated,
        "outdated_reasons": ["end_date"] if outdated else [],
        "added": True,
    }


def add_record(company: dict, data: dict) -> dict:
    """Add one record of a kind that can also be marked outdated."""
    company = copy.deepcopy(company)
    kind = str(data.get("kind") or "").upper()
    if kind == "RO":
        company.setdefault("registered_offices", []).append(_address_record(data))
    elif kind == "BR":
        record = _address_record(data)
        record["label"] = chosen_label(data.get("label"), "BR")
        record["br_no"] = _text(data, "br_no")
        company.setdefault("branches", []).append(record)
    elif kind == "SHARES":
        entry = {
            "record_id": new_id(),
            "row": None,
            "outdated": False,
            "outdated_reasons": [],
            "added": True,
            "date": _text(data, "date"),
        }
        for key in ("no_of_shares", "total_no_of_shares", "consideration", "total_consideration", "currency", "class", "type"):
            entry[key] = _stored(data.get(key), numeric=key in NUMERIC_FIELDS)
        company.setdefault("share_capital", []).append(entry)
        company["current_share_record_id"] = entry["record_id"]
    elif kind == "HOLDING":
        party = None
        for section in SECTIONS:
            for item in company.get(section) or []:
                if isinstance(item, dict) and _same_id(item, str(data.get("party_id") or "")):
                    party = item
                    break
            if party:
                break
        if party is None:
            raise ValueError("找不到這個人")
        end = _text(data, "end_date")
        outdated = _is_date(end)
        transfer_keys = ("transfer_from", "transfer_to", "transfer_date")
        holding = {
            "record_id": new_id(),
            "label": party.get("label") or "",
            "row": None,
            "no_of_shares": _number(data.get("no_of_shares")),
            "total_no_of_shares": _number(data.get("total_no_of_shares")),
            "percentage_excel": None,
            "consideration": _number(data.get("consideration")),
            "total_consideration": _number(data.get("total_consideration")),
            "class": _text(data, "class"),
            "type": _text(data, "type"),
            "date": _text(data, "date"),
            "end_date": end,
            "transfer": {
                "from": _text(data, "transfer_from"),
                "to": _text(data, "transfer_to"),
                "date": _text(data, "transfer_date"),
                "no_of_shares": None,
                "consideration": None,
            } if any(_text(data, key) for key in transfer_keys) else None,
            "declaration_of_trust": None,
            "pre_transfer": False,
            "outdated": outdated,
            "outdated_reasons": ["end_date"] if outdated else [],
            "added": True,
        }
        party.setdefault("shareholdings", []).append(holding)
    elif kind == "OTHER":
        label = chosen_label(data.get("label"))
        party = {
            "party_id": new_id(),
            "record_id": new_id(),
            "label": label,
            "label_raw": label,
            "roles": [label],
            "row": None,
            "outdated": False,
            "outdated_reasons": [],
            "added": True,
            "name_en": _text(data, "name_en"),
            "name_zh": _text(data, "name_zh"),
            "date_of_birth": None,
            "ci_no": None,
            "br_no": None,
            "id_numbers": [],
            "addresses": [],
            "emails": [],
            "phones": [],
            "other": [],
            "appoint_date": None,
            "appoint_date_row": None,
            "resign_date": None,
            "resign_date_row": None,
            "shareholdings": [],
            "current_shares": None,
            "current_holding_row": None,
            "current_consideration": None,
            "current_consideration_source": None,
            "percentage": None,
        }
        party["record_id"] = party["party_id"]
        company.setdefault("other_parties", []).append(party)
    else:
        raise ValueError("不能新增這個記錄")
    return company


def _same_id(record: dict, record_id: str) -> bool:
    return record_id and (record.get("party_id") == record_id or record.get("record_id") == record_id)


def _keep_one_current_secretary_address(company: dict, record: dict) -> None:
    """Restoring one secretary address leaves the previous current address outdated."""
    if record.get("outdated"):
        return
    for party in company.get("company_secretaries") or []:
        addresses = [item for item in (party.get("addresses") or []) if isinstance(item, dict)]
        if record not in addresses:
            continue
        for other in addresses:
            if other is record or other.get("outdated"):
                continue
            reasons = [reason for reason in (other.get("outdated_reasons") or []) if reason != "manual"]
            reasons.append("manual")
            other["outdated"] = True
            other["outdated_reasons"] = reasons
        return


def _find_record(company: dict, record_id: str) -> dict:
    for section in SECTIONS:
        for party in company.get(section) or []:
            if isinstance(party, dict) and _same_id(party, record_id):
                return party
            for holding in party.get("shareholdings") or []:
                if isinstance(holding, dict) and _same_id(holding, record_id):
                    return holding
            for key in ("addresses", "id_numbers", "emails", "phones", "other"):
                for item in party.get(key) or []:
                    if isinstance(item, dict) and _same_id(item, record_id):
                        return item
    for key in ("registered_offices", "branches", "share_capital"):
        for record in company.get(key) or []:
            if isinstance(record, dict) and _same_id(record, record_id):
                return record
    raise ValueError("找不到這筆資料")


def set_outdated(company: dict, party_id: str, outdated: bool) -> dict:
    """The user's choice wins, including records that were outdated by blue or an end date."""
    company = copy.deepcopy(company)
    ensure_party_ids(company)
    party = _find_record(company, party_id)
    reasons = [reason for reason in (party.get("outdated_reasons") or []) if reason != "manual"]
    if outdated:
        reasons.append("manual")
    else:
        reasons = [reason for reason in reasons if reason not in {"blue", "end_date", "superseded", "block"}]
    party["outdated"] = bool(outdated)
    party["outdated_reasons"] = reasons
    _keep_one_current_secretary_address(company, party)
    for row in company.get("shareholding_summary") or []:
        same = (row.get("party_id") and row.get("party_id") == party_id) or (
            party.get("row") and row.get("row") == party.get("row") and row.get("label") == party.get("label")
        )
        if same:
            row["counted_in_total"] = not party["outdated"]
    _refresh_counts(company)
    return company


def _stored(value, numeric: bool = False):
    text = "" if value is None else str(value).strip()
    if text == "":
        return None
    if numeric:
        number = _number(text)
        return text if number is None else number
    return text


def _blank_address() -> dict:
    return {
        "address": "", "lines": ["", "", "", ""], "from": None, "to": None,
        "row": None, "outdated": False, "outdated_reasons": [],
    }


def _blank_item() -> dict:
    return {"value": "", "row": None, "outdated": False, "outdated_reasons": []}


def _blank_capital() -> dict:
    entry = {key: None for key in SHARE_FIELDS}
    entry.update({"row": None, "outdated": False, "outdated_reasons": []})
    return entry


def _at(items: list, index: str, factory):
    for record in items:
        if isinstance(record, dict) and _same_id(record, index):
            return record
    try:
        number = int(index)
    except (TypeError, ValueError) as exc:
        raise ValueError("不能修改這個欄位") from exc
    if number < 0 or number > len(items) or number > 40:
        raise ValueError("不能修改這個欄位")
    if number == len(items):
        items.append(factory())
    return items[number]


def _apply_resign(party: dict) -> None:
    reasons = [reason for reason in (party.get("outdated_reasons") or []) if reason != "end_date"]
    if _is_date(party.get("resign_date")):
        reasons.append("end_date")
        party["outdated"] = True
    else:
        party["outdated"] = bool(reasons)
    party["outdated_reasons"] = reasons


def set_value(company: dict, path: list, value) -> dict:
    """Change one existing form field. The path is the fixed box, not a new kind of field."""
    company = copy.deepcopy(company)
    if not isinstance(path, list) or not 1 <= len(path) <= 6:
        raise ValueError("不能修改這個欄位")
    parts = [str(part) for part in path if str(part) not in {"", ".", ".."} and "$" not in str(part)]
    if len(parts) != len(path):
        raise ValueError("不能修改這個欄位")
    root, rest = parts[0], parts[1:]
    if root == "company_no" and not rest:
        company["company_no"] = _stored(value) or ""
    elif root == "issued_shares" and not rest:
        company["issued_shares"] = _stored(value, numeric=True)
    elif root in {"former_names", "business_names"} and len(rest) == 2 and rest[1] == "value":
        item = _at(company.setdefault(root, []), rest[0], _blank_item)
        item["value"] = _stored(value) or ""
    elif root == "general" and len(rest) == 1 and rest[0] in GENERAL_FIELDS:
        company.setdefault("general", {})[rest[0]] = _stored(value)
    elif root == "share_capital" and len(rest) == 2 and rest[1] in SHARE_FIELDS:
        entry = _at(company.setdefault("share_capital", []), rest[0], _blank_capital)
        entry[rest[1]] = _stored(value, numeric=rest[1] in NUMERIC_FIELDS)
        if company.get("current_share_capital_row") is None:
            company["current_share_capital_row"] = entry.get("row")
    elif root == "registered_offices":
        _set_record_tail(company.setdefault("registered_offices", []), rest, value, _blank_address)
    elif root == "branches":
        _set_branch(company.setdefault("branches", []), rest, value)
    elif root in SECTIONS:
        _set_party_value(company, root, rest, value)
    else:
        raise ValueError("不能修改這個欄位")
    return company


def _set_party_value(company: dict, section: str, rest: list[str], value) -> None:
    if len(rest) < 2:
        raise ValueError("不能修改這個欄位")
    party = next((item for item in company.get(section) or [] if item.get("party_id") == rest[0]), None)
    if party is None:
        raise ValueError("找不到這筆資料")
    key, tail = rest[1], rest[2:]
    if key == "shareholdings" and len(tail) >= 2:
        holding = _at(party.setdefault("shareholdings", []), tail[0], lambda: {"record_id": tail[0]})
        sub = tail[1:]
        if len(sub) == 1 and sub[0] in {
            "label", "no_of_shares", "total_no_of_shares", "consideration", "total_consideration",
            "class", "type", "date", "end_date",
        }:
            holding[sub[0]] = _stored(value, numeric=sub[0] in NUMERIC_FIELDS)
            return
        if len(sub) == 2 and sub[0] == "transfer" and sub[1] in {"from", "to", "date", "no_of_shares", "consideration"}:
            holding.setdefault("transfer", {})
            if holding["transfer"] is None:
                holding["transfer"] = {}
            holding["transfer"][sub[1]] = _stored(value, numeric=sub[1] in NUMERIC_FIELDS)
            return
        if len(sub) == 2 and sub[0] == "declaration_of_trust" and sub[1] in {"bo", "trustee", "date", "shares"}:
            holding.setdefault("declaration_of_trust", {})
            if holding["declaration_of_trust"] is None:
                holding["declaration_of_trust"] = {}
            holding["declaration_of_trust"][sub[1]] = _stored(value, numeric=sub[1] == "shares")
            return
        raise ValueError("不能修改這個欄位")
    if key == "ubo" and not tail:
        party["ubo"] = str(value).strip().lower() in {"1", "true", "yes", "on"}
        return
    if key in PARTY_FIELDS and not tail:
        party[key] = _stored(value)
        if key == "resign_date":
            _apply_resign(party)
        if key in {"name_en", "name_zh"}:
            name = party.get("name_en") or party.get("name_zh")
            for row in company.get("shareholding_summary") or []:
                same = row.get("party_id") == party.get("party_id") or (
                    party.get("row") and row.get("row") == party.get("row") and row.get("label") == party.get("label"))
                if same:
                    row["name"] = name
        return
    if key in LIST_FIELDS and tail == []:
        raise ValueError("不能修改這個欄位")
    if key == "id_numbers" and len(tail) == 2 and tail[1] == "id_type":
        chosen = str(_stored(value) or "").strip().lower()
        if chosen not in {"", "hkid", "passport"}:
            raise ValueError("證件種類只可以是 HKID 或 Passport")
        item = _at(party.setdefault(key, []), tail[0], _blank_item)
        item["id_type"] = chosen or None
        return
    if key in LIST_FIELDS and len(tail) == 2 and tail[1] == "value":
        item = _at(party.setdefault(key, []), tail[0], _blank_item)
        item["value"] = _stored(value) or ""
        return
    if key == "addresses":
        if section == "company_secretaries":
            party["addresses"] = party.get("addresses") or []
            single_secretary_address(party) if party["addresses"] else None
            if not party["addresses"]:
                party["addresses"] = [_blank_address()]
            tail = ["0", *tail[1:]] if tail else tail
        _set_record_tail(party.setdefault("addresses", []), tail, value, _blank_address)
        if section == "company_secretaries":
            single_secretary_address(party)
        return
    raise ValueError("不能修改這個欄位")


def _set_record_tail(records: list, tail: list[str], value, factory) -> None:
    if not tail:
        raise ValueError("不能修改這個欄位")
    record = _at(records, tail[0], factory)
    target = tail[1:]
    if len(target) == 2 and target[0] == "lines" and target[1] in {"0", "1", "2", "3"}:
        lines = list(record.get("lines") or ["", "", "", ""])
        while len(lines) < 4:
            lines.append("")
        lines[int(target[1])] = _stored(value) or ""
        record["lines"] = lines[:4]
        record["address"] = ", ".join(line for line in record["lines"] if str(line).strip())
        return
    if len(target) == 1 and target[0] in {"from", "to", "address", "br_no", "label"}:
        record[target[0]] = _stored(value)
        return
    raise ValueError("不能修改這個欄位")


def _blank_branch() -> dict:
    entry = _blank_address()
    entry.update({"label": "", "br_no": None})
    return entry


def _set_branch(records: list, rest: list[str], value) -> None:
    _set_record_tail(records, rest, value, _blank_branch)


def _refresh_counts(company: dict) -> None:
    counted = [row for row in company.get("shareholding_summary") or [] if row.get("counted_in_total")]
    company["holdings_total"] = sum(row.get("current_shares") or 0 for row in counted) or None
    counts = company.setdefault("counts", {})
    counts["current_directors"] = sum(1 for party in company.get("directors") or [] if not party.get("outdated"))
    counts["current_company_secretaries"] = sum(
        1 for party in company.get("company_secretaries") or [] if not party.get("outdated"))
    counts["current_shareholders"] = len(counted)
    issues = company.get("issues") or []
    counts["issues"] = {
        "error": sum(1 for issue in issues if issue.get("severity") == "error"),
        "warning": sum(1 for issue in issues if issue.get("severity") == "warning"),
        "info": sum(1 for issue in issues if issue.get("severity") == "info"),
    }
