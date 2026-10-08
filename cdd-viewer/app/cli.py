"""Convert a CDD Excel workbook to JSON: python -m app.cli <excel> [-o output.json]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .excel_parser import DEFAULT_SHEET, ParseError, parse_workbook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 CDD Excel 轉成 JSON")
    parser.add_argument("excel", type=Path, help="Excel 檔案路徑")
    parser.add_argument("-o", "--output", type=Path, help="輸出 JSON 路徑（預設：與 Excel 同名的 .json）")
    parser.add_argument("--sheet", default=DEFAULT_SHEET, help=f"工作表名稱（預設：{DEFAULT_SHEET}）")
    args = parser.parse_args(argv)

    try:
        data = parse_workbook(args.excel, sheet=args.sheet)
    except (ParseError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    output = args.output or args.excel.with_suffix(".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = data["summary"]
    issues = summary["issues"]
    print(f"已輸出 {output}：{summary['companies']} 間公司，"
          f"{issues['error']} 個錯誤、{issues['warning']} 個警告、{issues['info']} 個提示")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
