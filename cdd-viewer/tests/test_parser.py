import os
import unittest
from io import BytesIO
from pathlib import Path

import openpyxl
from openpyxl.styles import PatternFill

from app.excel_parser import (
    color_name, follows_address_fields, id_document_type, is_orange, is_red, parse_date, parse_label,
    parse_workbook, preview_sheet, split_address,
)
from app.excel_fields import fill_owners, inline_parts, resolve_fill_token, suggest_fields

DUMMY = Path(os.environ.get(
    "CDD_TEST_FILE",
    Path(__file__).resolve().parents[2] / "Database purge" / "CDD base dummy.xlsx",
))


class IdDocumentTypeTests(unittest.TestCase):
    def test_hkid_and_passport_word(self):
        self.assertEqual(id_document_type("A123456(7)"), "hkid")
        self.assertEqual(id_document_type("Passport AB1"), "passport")
        self.assertIsNone(id_document_type("12345678"))


class AddressFieldOrderTests(unittest.TestCase):
    def test_only_a_run_from_field_one_is_an_address(self):
        self.assertTrue(follows_address_fields(["A", "B", "C", "D"]))
        self.assertTrue(follows_address_fields(["A", "B", "", ""]))
        self.assertFalse(follows_address_fields(["", "B", "C", "D"]))
        self.assertFalse(follows_address_fields(["", "", "", "D"]))
        self.assertFalse(follows_address_fields(["", "", "C", "D"]))


class PreviewSheetTests(unittest.TestCase):
    def test_preview_keeps_excel_row_numbers_text_and_borders(self):
        from openpyxl.styles import Border, PatternFill, Side

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "CDD"
        sheet["B1"] = "Name"
        sheet["B1"].fill = PatternFill("solid", fgColor="FFFF00")
        sheet["B1"].border = Border(
            left=Side(style="thin", color="FF000000"),
            right=Side(style="medium", color="FF000000"),
            top=Side(style="thin", color="FF000000"),
            bottom=Side(style="thin", color="FF000000"),
        )
        sheet["A3"] = 12
        sheet["B3"] = "Ada"
        sheet.merge_cells("A2:B2")
        buffer = BytesIO()
        book.save(buffer)
        preview = preview_sheet(buffer.getvalue(), "CDD")
        self.assertEqual(preview["sheet"], "CDD")
        self.assertEqual(preview["columns"][:2], ["A", "B"])
        self.assertEqual(preview["rows"][0]["cells"][1], "Name")
        self.assertEqual(preview["rows"][0]["fills"][1], "FFFF00")
        self.assertEqual(preview["rows"][0]["borders"][1]["l"], "1px solid #000000")
        self.assertEqual(preview["rows"][0]["borders"][1]["r"], "2px solid #000000")
        self.assertEqual(preview["rows"][2]["cells"][0], "12")
        self.assertEqual(preview["rows"][2]["cells"][1], "Ada")
        self.assertEqual(len(preview["rows"]), 3)
        self.assertEqual(preview["merges"], [{"r": 2, "c": 1, "rs": 1, "cs": 2}])
        self.assertTrue(preview["gridlines"])

    def test_preview_keeps_column_width_row_height_and_font_size(self):
        from openpyxl.styles import Font

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "CDD"
        sheet.column_dimensions["B"].width = 30
        sheet.row_dimensions[1].height = 30
        sheet["B1"] = "Wide"
        sheet["B1"].font = Font(name="Calibri", size=18, bold=True)
        buffer = BytesIO()
        book.save(buffer)
        preview = preview_sheet(buffer.getvalue(), "CDD")
        self.assertEqual(preview["widths"][1], round(30 * 7 + 5))
        self.assertEqual(preview["rows"][0]["height"], round(30 * 96 / 72))
        self.assertEqual(preview["rows"][0]["fonts"][1], {"s": 18.0, "b": True})

    def test_cell_mark_and_comment_become_fillable_fields(self):
        from openpyxl.comments import Comment

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "CDD"
        sheet["A1"] = "[[company.name_en]]"
        sheet["B1"] = "Ada"
        sheet["B1"].comment = Comment("DIR1.dob", "CDD")
        sheet["C1"] = "[[not.a.field]]"
        buffer = BytesIO()
        book.save(buffer)
        preview = preview_sheet(buffer.getvalue(), "CDD")
        row = preview["rows"][0]
        self.assertEqual(row["cells"][0], "")
        self.assertEqual(row["fields"][0]["key"], "company.name_en")
        self.assertEqual(row["fields"][0]["kind"], "text")
        self.assertEqual(row["cells"][1], "Ada")
        self.assertEqual(row["fields"][1]["key"], "DIR1.dob")
        self.assertEqual(row["cells"][2], "[[not.a.field]]")
        self.assertIsNone(row["fields"][2])

    def test_empty_brackets_wait_for_a_field_choice(self):
        from openpyxl.comments import Comment

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "CDD"
        sheet["A1"] = "Dir 1"
        sheet["B1"] = "[[]]"
        sheet["C1"] = "Ada"
        sheet["C1"].comment = Comment("[[ ]]", "CDD")
        sheet["D1"] = "[[company.name_en]]"
        buffer = BytesIO()
        book.save(buffer)
        preview = preview_sheet(buffer.getvalue(), "CDD")
        row = preview["rows"][0]
        self.assertEqual(row["owners"], ["DIR1"])
        self.assertEqual(row["cells"][1], "")
        self.assertEqual(row["fields"][1]["kind"], "pick")
        self.assertEqual(row["cells"][2], "Ada")
        self.assertEqual(row["fields"][2]["kind"], "pick")
        self.assertEqual(row["fields"][3]["key"], "company.name_en")

    def test_fill_tokens_follow_the_form_fields(self):
        self.assertEqual(resolve_fill_token("company.type")["kind"], "choice")
        self.assertEqual(resolve_fill_token("SH1.transfer.from")["key"], "SH1.transfer.from")
        self.assertEqual(resolve_fill_token("CS.license")["kind"], "bool")
        self.assertIsNone(resolve_fill_token("DIR1.license"))
        self.assertEqual(resolve_fill_token("BR001.address.4")["key"], "BR001.address.4")
        self.assertEqual(resolve_fill_token("RO.from")["key"], "RO.from")
        self.assertIsNone(resolve_fill_token("Ada"))

    def test_field_search_prefers_the_row_owner(self):
        self.assertEqual(fill_owners("Dir 1, SH1"), ["DIR1", "SH1"])
        self.assertEqual(fill_owners("RO"), ["RO"])
        self.assertEqual(fill_owners("BR 001"), ["BR001"])
        self.assertEqual(fill_owners("351"), ["company"])
        self.assertEqual(fill_owners("[[]]"), [])
        english = suggest_fields("英文", ["DIR1"])
        self.assertEqual(english[0]["key"], "DIR1.name_en")
        self.assertIn("company.name_en", [item["key"] for item in english])
        self.assertEqual(suggest_fields("DIR2 電話")[0]["key"], "DIR2.tel")
        self.assertEqual(suggest_fields("com sec 牌照")[0]["key"], "CS.license")
        self.assertFalse(any(item["key"].endswith(".license") for item in suggest_fields("牌照", ["DIR1"])))
        self.assertEqual(suggest_fields("BR001 地址")[0]["key"], "BR001.address.1")
        self.assertEqual(suggest_fields("", ["RO"])[0]["key"], "RO.address.1")
        self.assertEqual(suggest_fields("company.type")[0]["kind"], "choice")

    def test_inline_mark_keeps_surrounding_text_and_ticks_a_bool(self):
        parts = inline_parts("無須領牌 [[CS.license]]，見原因")
        self.assertEqual(parts[0]["text"], "無須領牌 ")
        self.assertEqual(parts[1]["field"]["key"], "CS.license")
        self.assertEqual(parts[1]["field"]["kind"], "bool")
        self.assertEqual(parts[2]["text"], "，見原因")
        self.assertEqual(inline_parts("名稱 [[company.name_en]] 完")[1]["field"]["key"], "company.name_en")
        self.assertIsNone(inline_parts("[[DIR1.ubo]]"))
        self.assertEqual(inline_parts("保留 [[not.a.field]]")[0]["text"], "保留 [[not.a.field]]")
        ellipsis = inline_parts("[[…]] Formation of a new company 成立新公司")
        self.assertEqual(ellipsis[0], {"tick": True})
        self.assertEqual(ellipsis[1]["text"], " Formation of a new company 成立新公司")
        self.assertNotIn("[", ellipsis[1]["text"])
        self.assertEqual(inline_parts("[[...]] yes")[0], {"tick": True})
        leftover = inline_parts("[…]] Information verification")
        self.assertEqual(leftover[0], {"tick": True})
        self.assertNotIn("[", leftover[1]["text"])
        self.assertNotIn("]", leftover[1]["text"])
        empty_box = inline_parts("☐ Formation of a new company")
        self.assertEqual(empty_box[0], {"tick": True, "checked": False})
        self.assertNotIn("☐", empty_box[1]["text"])
        crossed = inline_parts("☒ 已勾選")
        self.assertEqual(crossed[0], {"tick": True, "checked": True})
        self.assertNotIn("☒", crossed[1]["text"])

    def test_preview_keeps_excel_print_setup(self):
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "CDD"
        sheet["A1"] = "Form"
        sheet.page_setup.paperSize = 9
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_margins.left = 0.5
        sheet.page_margins.right = 0.5
        buffer = BytesIO()
        book.save(buffer)
        page = preview_sheet(buffer.getvalue(), "CDD")["page"]
        self.assertEqual(page["paper"], "A4")
        self.assertEqual(page["orientation"], "landscape")
        self.assertTrue(page["fit"])
        self.assertTrue(page["margin"].endswith("0.5in"))
        self.assertEqual(page["print_area"], [])

    def test_print_area_limits_the_form_rows_and_columns(self):
        from app.excel_parser import clip_to_print_area

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "CDD"
        sheet["A1"] = "outside"
        sheet["B2"] = "inside"
        sheet["C4"] = "edge"
        sheet["D5"] = "outside-too"
        sheet.print_area = "B2:C4"
        buffer = BytesIO()
        book.save(buffer)
        grid = preview_sheet(buffer.getvalue(), "CDD")
        self.assertEqual(grid["page"]["print_area"], [{"r1": 2, "c1": 2, "r2": 4, "c2": 3}])
        clipped = clip_to_print_area(grid)
        self.assertEqual(clipped["columns"], ["B", "C"])
        self.assertEqual([row["n"] for row in clipped["rows"]], [2, 3, 4])
        self.assertEqual(clipped["rows"][0]["cells"][0], "inside")
        self.assertNotIn("outside-too", clipped["rows"][-1]["cells"])

    def test_print_area_keeps_columns_past_the_preview_cap(self):
        from app.excel_parser import clip_to_print_area

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "CDD"
        sheet["A1"] = "start"
        sheet["AS3"] = "far"
        sheet.print_area = "A1:AS5"
        buffer = BytesIO()
        book.save(buffer)
        grid = preview_sheet(buffer.getvalue(), "CDD")
        clipped = clip_to_print_area(grid)
        self.assertEqual(clipped["columns"][-1], "AS")
        self.assertEqual(len(clipped["rows"]), 5)
        self.assertEqual(clipped["rows"][2]["cells"][-1], "far")

    def test_preview_width_follows_used_columns_not_the_scan_cap(self):
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "CDD"
        sheet["A1"] = "only"
        sheet["G4"] = "edge"
        buffer = BytesIO()
        book.save(buffer)
        grid = preview_sheet(buffer.getvalue(), "CDD")
        self.assertEqual(grid["columns"], ["A", "B", "C", "D", "E", "F", "G"])


class LabelTests(unittest.TestCase):
    def test_role_variants_are_normalised(self):
        cases = {
            "DIR1": ("DIR1",), "Dir 1": ("DIR1",), " DR1 ": ("DIR1",), "Sh 2": ("SH2",),
            "SH1, DR1 ": ("SH1", "DIR1"), "Dir 1 & SH1": ("DIR1", "SH1"), "Dir 1, SH!": ("DIR1", "SH1"),
            "SH DIR 2": ("SH2", "DIR2"), "DIR, MB 1": ("DIR1", "MB1"), "SC1, DIR1": ("SC1", "DIR1"),
            "Com Sec": ("CS",), "Com Sec1": ("CS1",), "CS2": ("CS2",), "SH1-2.1": ("SH1-2.1",),
        }
        for raw, roles in cases.items():
            with self.subTest(raw=raw):
                label = parse_label(raw)
                self.assertEqual(label.kind, "ROLE")
                self.assertEqual(label.roles, roles)

    def test_other_label_kinds(self):
        self.assertEqual(parse_label(351).company_no, "351")
        self.assertEqual(parse_label("156\nResigned on 13/8/2026").company_no, "156")
        self.assertEqual(parse_label("??").kind, "COMPANY")
        self.assertEqual(parse_label("RO ").kind, "RO")
        self.assertEqual(parse_label("Resigned on\n23Jul2026").kind, "STATUS")
        self.assertEqual(parse_label("Old name").kind, "OLD_NAME")
        self.assertEqual(parse_label("BR001").kind, "BRANCH")
        self.assertEqual(parse_label("未確認轉唔轉").kind, "OTHER")
        self.assertIsNone(parse_label("  "))


class DateTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_date("25/4/2024").value, "2024-04-25")
        self.assertEqual(parse_date("30/3/26").value, "2026-03-30")
        self.assertEqual(parse_date("(A)12/3/2020").value, "2020-03-12")
        self.assertEqual(parse_date("12/3/2020 ®").note, "®")
        self.assertEqual(parse_date("23Jul2026").value, "2026-07-23")
        self.assertEqual(parse_date("15 July 2026").value, "2026-07-15")
        self.assertEqual(parse_date("9/3//2020").value, "2020-03-09")

    def test_problems(self):
        self.assertEqual(parse_date("14/21/2021").problem, "invalid")
        self.assertEqual(parse_date("Cancel").problem, "unparsed")
        self.assertEqual(parse_date(2023).problem, "year_only")
        self.assertIsNone(parse_date("-"))
        self.assertIsNone(parse_date("??"))


class ColourTests(unittest.TestCase):
    def test_red_company_number_fill(self):
        for colour in ("FF0000", "FF7474", "FFC6C6", "C00000", "FF3300"):
            self.assertTrue(is_red(colour), colour)
        for colour in ("FFC000", "FFCE3C", "FFCCFF", "F8CBAD", "00B0F0", "FFFFFF", None):
            self.assertFalse(is_red(colour), colour)

    def test_orange_and_colour_names(self):
        for colour in ("FFC000", "FFCE3C"):
            self.assertTrue(is_orange(colour), colour)
        for colour in ("F8CBAD", "BF9000", "FFFF00", "FF0000", None):
            self.assertFalse(is_orange(colour), colour)
        names = {"FFC000": "橙色", "FFC6C6": "淺紅色", "C5E0B4": "淺綠色", "975CCB": "紫色", "F8CBAD": "杏色"}
        for colour, name in names.items():
            self.assertEqual(color_name(colour), name, colour)


SUBHEADER = ["", "No. of shares", "Total no. of shares", "Consideration", "Total consideration", "Currency"]


def build_workbook(rows, blue_rows=(), red_rows=()):
    """Small synthetic CDD sheet: two header rows, then the given rows (Excel row 3 onwards)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CDD"
    ws.append(SUBHEADER)
    ws.append(["", "", "Date of birth", "CI", "BR", "Incorporation", "Director", "Shareholder",
               "Appoint date", "Resign date"])
    for row in rows:
        ws.append(row)
    for n in blue_rows:
        for col in (1, 2):
            ws.cell(n, col).fill = PatternFill("solid", fgColor="FF00B0F0")
    for n in red_rows:
        ws.cell(n, 1).fill = PatternFill("solid", fgColor="FFFF0000")
    buffer = BytesIO()
    wb.save(buffer)
    return parse_workbook(buffer.getvalue(), file_name="synthetic.xlsx")


def ro(address, start=None, end=None):
    return ["RO", address, None, None, None, None, None, None, start, end]


def codes(company):
    return {i["code"]: i for i in company["issues"]}


class RegisteredOfficeTests(unittest.TestCase):
    def test_latest_start_date_is_the_only_current_ro(self):
        data = build_workbook([[101, "Test A Limited"], ro("old address", "01/01/2015"), ro("new address", "01/06/2020")])
        offices = data["companies"][0]["registered_offices"]
        self.assertEqual([(e["address"], e["outdated"]) for e in offices], [("old address", True), ("new address", False)])
        self.assertEqual(offices[0]["outdated_reasons"], ["superseded"])
        self.assertEqual([i for i in data["companies"][0]["issues"] if i["field"] and i["field"].startswith("ro")], [])

    def test_blue_ro_is_outdated_even_if_latest(self):
        data = build_workbook([[105, "Test E Limited"], ro("old address", "01/01/2015"), ro("new address", "01/06/2020")],
                              blue_rows=(5,))
        company = data["companies"][0]
        current = [e["address"] for e in company["registered_offices"] if not e["outdated"]]
        self.assertEqual(current, ["old address"])
        self.assertIn("OLDER_RO_IS_CURRENT", codes(company))

    def test_latest_date_wins_regardless_of_row_order(self):
        data = build_workbook([[102, "Test B Limited"], ro("new address", "01/06/2020"), ro("old address", "01/01/2015")])
        current = [e["address"] for e in data["companies"][0]["registered_offices"] if not e["outdated"]]
        self.assertEqual(current, ["new address"])

    def test_missing_ro_is_reported(self):
        data = build_workbook([[103, "Test C Limited"]])
        self.assertIn("NO_RO", {i["code"] for i in data["companies"][0]["issues"]})

    def test_all_blue_without_dates_is_reported(self):
        data = build_workbook([[104, "Test D Limited"], ro("old address"), ro("older address")], blue_rows=(4, 5))
        company = data["companies"][0]
        self.assertTrue(all(e["outdated"] for e in company["registered_offices"]))
        self.assertIn("NO_CURRENT_RO", {i["code"] for i in company["issues"]})


class ShareCapitalTests(unittest.TestCase):
    def test_missing_shares_warns_even_when_inactive(self):
        data = build_workbook([[201, "Active Limited"], ro("addr", "01/01/2020"),
                               [202, "Red Limited"], ro("addr", "01/01/2020")], red_rows=(5,))
        active, inactive = data["companies"]
        self.assertFalse(inactive["active"])
        for company in (active, inactive):
            self.assertEqual(codes(company)["NO_SHARES"]["severity"], "warning")

    def test_missing_share_fields_are_listed(self):
        data = build_workbook([[203, "Fields Limited"], ro("addr", "01/01/2020"), SUBHEADER,
                               ["Shares", 100, 100, None, None, None, "Ordinary"]])
        company = data["companies"][0]
        self.assertEqual(company["share_capital_missing"], ["consideration", "total_consideration", "currency"])
        self.assertIn("Currency", codes(company)["SHARES_FIELDS_MISSING"]["message"])

    def test_unlabelled_row_after_subheader_is_share_capital(self):
        data = build_workbook([[204, "No Label Limited"], ro("addr", "01/01/2020"), SUBHEADER,
                               ["", 10, 10, 10, 10, "HKD", "Ordinary"]])
        company = data["companies"][0]
        self.assertEqual(company["issued_shares"], 10)
        self.assertIn("SHARES_NO_LABEL", codes(company))
        self.assertNotIn("NO_SHARES", codes(company))


def cs(name, start="01/01/2020", end=None):
    return ["Com Sec", name, None, None, None, None, None, None, start, end]


class CompanySecretaryTests(unittest.TestCase):
    def test_more_than_one_current_cs_is_allowed(self):
        data = build_workbook([[301, "Two Secs Limited"], cs("Sec One Limited"), cs("Sec Two Limited")])
        company = data["companies"][0]
        self.assertEqual(sum(not p["outdated"] for p in company["company_secretaries"]), 2)
        self.assertFalse({"NO_CS", "NO_CURRENT_CS", "MULTIPLE_CURRENT_CS"} & set(codes(company)))

    def test_no_cs_is_a_warning_even_when_inactive(self):
        data = build_workbook([[302, "No Sec Limited"], [303, "Red No Sec Limited"]], red_rows=(4,))
        for company in data["companies"]:
            self.assertEqual(codes(company)["NO_CS"]["severity"], "warning")

    def test_active_company_needs_a_current_cs(self):
        data = build_workbook([[304, "Old Sec Limited"], cs("Sec One Limited", end="01/01/2023")])
        self.assertIn("NO_CURRENT_CS", codes(data["companies"][0]))

    def test_inactive_company_with_past_cs_is_fine(self):
        data = build_workbook([[305, "Red Old Sec Limited"], cs("Sec One Limited", end="01/01/2023")], red_rows=(3,))
        self.assertFalse({"NO_CS", "NO_CURRENT_CS"} & set(codes(data["companies"][0])))


def person(label, name, start=None, end=None):
    return [label, name, None, None, None, None, None, None, start, end]


class DateProblemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = build_workbook([
            [401, "Date Test Limited"],
            person("DIR1", "Person A", "01/01/2020", "Cancel"),
            person("DIR2", "Person B", "01/01/2020", "??"),
            person("DIR3", "Person C", "01/01/2020", 2023),
            person("DIR4", "Person D", "abc"),
            [402, "Status Test Limited"],
            ["Resigned", "status test chinese name"],
            [403, "Branch Test Limited"],
            ["BR0001 Resigned", "branch address 1"],
            ["BR0002 resigned on 1/2/2024", "branch address 2"],
            ["BR0003", "branch address 3", None, None, None, None, None, None, None, "unclear"],
        ])
        cls.dates, cls.status, cls.branch = cls.data["companies"]

    def issue_at(self, company, row, field):
        return next(i for i in company["issues"] if i["row"] == row and i["field"] == field)

    def test_resign_dates_that_are_not_dates_are_errors(self):
        for row in (4, 5, 6):
            errors = [i for i in self.dates["issues"]
                      if i["row"] == row and i["field"] == "party.resign_date" and i["severity"] == "error"]
            self.assertEqual(len(errors), 1, row)
            self.assertIn("資料有誤", errors[0]["message"])

    def test_question_marks_are_kept_for_display(self):
        director = next(d for d in self.dates["directors"] if d["row"] == 5)
        self.assertEqual(director["resign_date"], "??")

    def test_other_date_fields_are_warnings(self):
        self.assertEqual(self.issue_at(self.dates, 7, "party.appoint_date")["severity"], "warning")

    def test_resigned_status_without_date_is_an_error(self):
        issue = self.issue_at(self.status, 9, "status")
        self.assertEqual((issue["code"], issue["severity"]), ("STATUS_DATE_INVALID", "error"))

    def test_unclear_branch_content_stays_current_with_warning(self):
        first, second, third = self.branch["branches"]
        self.assertEqual((first["label"], first["outdated"]), ("BR0001", False))
        self.assertEqual(self.issue_at(self.branch, 11, "branch.label")["severity"], "warning")
        self.assertEqual((third["to"], third["outdated"]), ("unclear", False))
        self.assertEqual(self.issue_at(self.branch, 13, "branch.to")["severity"], "warning")

    def test_branch_note_with_real_date_ends_the_branch(self):
        second = self.branch["branches"][1]
        self.assertEqual((second["to"], second["outdated"]), ("2024-02-01", True))

    def test_unclear_resign_text_does_not_make_a_person_outdated(self):
        director = next(d for d in self.dates["directors"] if d["row"] == 4)
        self.assertEqual(director["resign_date"], "Cancel")
        self.assertFalse(director["outdated"])

    def test_every_issue_names_a_field_key(self):
        for company in self.data["companies"]:
            for issue in company["issues"]:
                self.assertIn("field", issue)


def holding(label, shares, total, pct, consideration, total_consideration):
    return [label, shares, total, pct, consideration, total_consideration, "Ordinary", "Allotment", "01/01/2020"]


class ConsiderationTests(unittest.TestCase):
    def test_percent_copy_and_missing_money_are_estimated(self):
        data = build_workbook([
            [601, "Money Limited"], ro("addr", "01/01/2020"), SUBHEADER,
            ["Shares", 100, 100, 100, 100, "HKD", "Ordinary", "Allotment"],
            person("DIR1", "Holder One", "01/01/2020"), holding("SH1", 50, 50, 0.5, 0.5, 0.5),
            person("DIR2", "Holder Two", "01/01/2020"), holding("SH2", 50, 50, 0.5, None, None),
        ])
        company = data["companies"][0]
        self.assertEqual(company["currency"], "HKD")
        summary = {s["name"]: (s["consideration"], s["consideration_source"]) for s in company["shareholding_summary"]}
        self.assertEqual(summary, {"Holder One": (50, "estimated"), "Holder Two": (50, "estimated")})
        self.assertIn("CONSIDERATION_LOOKS_LIKE_PERCENT", codes(company))


class AddressSplitTests(unittest.TestCase):
    def test_comma_split_fills_four_fields(self):
        self.assertEqual(split_address("Flat A, 1/F, Test Building, Hong Kong"),
                         ["Flat A", "1/F", "Test Building", "Hong Kong"])
        self.assertEqual(split_address("只有一段"), ["", "", "", "只有一段"])
        self.assertEqual(split_address("Causeway Bay, Hong Kong"), ["", "", "Causeway Bay", "Hong Kong"])
        self.assertEqual(split_address("a，b, c, d, e, f"), ["a, b, c", "d", "e", "f"])

    def test_workbook_address_lines(self):
        data = build_workbook([
            [802, "Split Limited"],
            ["RO", "Room 1, Test House, Test Street, Hong Kong"],
            person("DIR1", "Person A", "01/01/2020"),
            ["", "Flat A, 1/F, Test Building, Kowloon"],
        ])
        company = data["companies"][0]
        self.assertEqual(company["registered_offices"][0]["lines"],
                         ["Room 1", "Test House", "Test Street", "Hong Kong"])
        self.assertEqual(company["directors"][0]["addresses"][0]["lines"],
                         ["Flat A", "1/F", "Test Building", "Kowloon"])


class SuspiciousAddressTests(unittest.TestCase):
    def test_share_and_id_in_an_address_are_flagged_on_that_line(self):
        data = build_workbook([
            [804, "Suspicious Address Limited"],
            ["RO", "Room 1, SHARE, ID card, Hong Kong"],
            person("DIR1", "Person A", "01/01/2020"),
            ["", "Flat A, A123456(7), Middle Road, Hong Kong"],
        ])
        company = data["companies"][0]
        flagged = [(i["field"], i["row"]) for i in company["issues"] if i["code"] == "ADDRESS_SUSPICIOUS"]
        self.assertIn(("address.line.2", 4), flagged)
        self.assertIn(("address.line.3", 4), flagged)
        self.assertIn(("address.line.2", 6), flagged)
        self.assertNotIn(("address.line.3", 6), flagged)

    def test_ordinary_address_words_are_not_flagged(self):
        data = build_workbook([[805, "Plain Address Limited"], ["RO", "Room 1, Test Building, Middle Road, Hong Kong"]])
        self.assertFalse(any(i["code"] == "ADDRESS_SUSPICIOUS" for i in data["companies"][0]["issues"]))


class GeneralHeaderTailTests(unittest.TestCase):
    def test_transfer_columns_on_a_name_row_are_not_unclassified(self):
        row = ["SH1", "Holder Name"] + [None] * 9 + ["From Person", "To Person"]
        data = build_workbook([[901, "Transfer Limited"], row])
        holder = data["companies"][0]["shareholders"][0]
        self.assertEqual(holder["other"], [])
        self.assertEqual(holder["shareholdings"][0]["transfer"]["from"], "From Person")
        self.assertEqual(holder["shareholdings"][0]["transfer"]["to"], "To Person")


class AddressStartDateTests(unittest.TestCase):
    def test_unreadable_start_date_is_marked_on_the_address_field(self):
        data = build_workbook([
            [803, "Address Date Limited"],
            person("DIR1", "Person A", "01/01/2020"),
            ["", "Flat A, 1 Test Street, Hong Kong", None, None, None, None, None, None, "2023"],
        ])
        company = data["companies"][0]
        issue = next(i for i in company["issues"] if i["code"] == "DATE_YEAR_ONLY")
        self.assertEqual((issue["field"], issue["row"], issue["severity"]), ("undefined", 5, "warning"))
        self.assertEqual(company["directors"][0]["other"][0]["from"], "2023")
        self.assertEqual(company["directors"][0]["addresses"], [])


class AddressDateTests(unittest.TestCase):
    def test_date_next_to_an_address_is_the_address_date(self):
        data = build_workbook([
            [801, "Address Limited"],
            person("DIR1", "Person A", "01/01/2020"),
            ["", "Flat A, 1/F, Test Building, Hong Kong", "01/03/2021"],
            ["", "A123456(7)", "01/01/1980"],
            ["", "Flat B, 2/F, Test Building, Hong Kong", None, None, None, None, None, None, "01/06/2022"],
        ])
        director = data["companies"][0]["directors"][0]
        self.assertEqual([(a["value"][:6], a.get("from")) for a in director["addresses"]],
                         [("Flat A", "2021-03-01"), ("Flat B", "2022-06-01")])
        self.assertEqual(director["date_of_birth"], "1980-01-01")


@unittest.skipUnless(DUMMY.exists(), f"dummy workbook not found: {DUMMY}")
class DummyWorkbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = parse_workbook(DUMMY)

    def company(self, number):
        return next(c for c in self.data["companies"] if c["company_no"] == number)

    def test_companies_and_header(self):
        self.assertEqual([c["company_no"] for c in self.data["companies"]], ["351", "46", "182"])
        self.assertEqual(self.data["source"]["header_rows"], [1, 2])

    def test_general_header_fields(self):
        general = self.company("351")["general"]
        self.assertEqual(general["name_en"], "company1")
        self.assertEqual(general["name_zh"], "company1_chinese")
        self.assertEqual(general["br_no"], "76487373")
        self.assertEqual(general["incorporation_date"], "2024-04-25")

    def test_director_owns_matching_shareholding(self):
        director = self.company("351")["directors"][0]
        self.assertEqual(director["roles"], ["DIR1", "SH1"])
        self.assertEqual(director["current_shares"], 10000)
        self.assertEqual(director["percentage"], 1.0)
        self.assertEqual(director["phones"][0]["value"], "sh1_tel")

    def test_blue_rows_are_outdated(self):
        company = self.company("46")
        current = [d for d in company["directors"] if not d["outdated"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["row"], 35)
        self.assertEqual(company["holdings_total"], company["issued_shares"])
        old_ro, new_ro = company["registered_offices"]
        self.assertEqual(old_ro["outdated_reasons"], ["blue", "end_date"])
        self.assertFalse(new_ro["outdated"])

    def test_field_level_outdated_address(self):
        secretary = self.company("351")["company_secretaries"][0]
        self.assertFalse(secretary["outdated"])
        self.assertEqual([a["outdated"] for a in secretary["addresses"]], [True, False])

    def test_resign_date_alone_is_outdated_without_warning(self):
        company = self.company("182")
        last_secretary = company["company_secretaries"][-1]
        self.assertEqual(last_secretary["outdated_reasons"], ["end_date"])
        all_codes = {i["code"] for c in self.data["companies"] for i in c["issues"]}
        self.assertFalse(all_codes & {"END_DATE_NOT_BLUE", "PARTIAL_BLUE", "RO_SUPERSEDED", "LATEST_RO_BLUE"})

    def test_transfer_uses_post_transfer_record(self):
        director = next(d for d in self.company("46")["directors"] if not d["outdated"])
        flags = [(h["row"], h["pre_transfer"]) for h in director["shareholdings"]]
        self.assertEqual(flags, [(38, True), (39, False)])
        self.assertEqual(director["current_shares"], 50000)
        self.assertEqual(director["current_holding_row"], 39)

    def test_dates_written_on_sh_rows_are_used(self):
        director = self.company("351")["directors"][0]
        self.assertEqual((director["appoint_date"], director["appoint_date_row"]), ("2024-04-25", 12))
        dir2 = next(d for d in self.company("46")["directors"] if d["row"] == 27)
        self.assertEqual((dir2["appoint_date"], dir2["appoint_date_row"]), ("2011-04-26", 30))
        shareholder = self.company("182")["shareholders"][0]
        self.assertEqual((shareholder["appoint_date"], shareholder["resign_date"]), ("2021-04-28", "2023-01-21"))
        self.assertEqual(shareholder["resign_date_row"], 69)

    def test_label_row_dates_take_priority(self):
        director = next(d for d in self.company("46")["directors"] if d["row"] == 35)
        self.assertEqual((director["appoint_date"], director["appoint_date_row"]), ("2015-10-10", 35))
        self.assertIsNone(director["resign_date"])

    def test_missing_optional_fields_are_not_warnings(self):
        company = self.company("46")
        self.assertIsNone(company["general"]["name_zh"])
        self.assertEqual([i for i in company["issues"] if i["severity"] != "info"], [])

    def test_dummy_consideration_comes_from_excel(self):
        money = {c["company_no"]: [(s["consideration"], s["consideration_source"]) for s in c["shareholding_summary"]]
                 for c in self.data["companies"]}
        self.assertEqual(money, {"351": [(10000, "excel")], "46": [(50000, "excel")], "182": [(1, "excel")]})
        self.assertEqual({c["currency"] for c in self.data["companies"]}, {"HKD"})

    def test_dummy_share_capital_is_complete(self):
        for company in self.data["companies"]:
            self.assertEqual(company["share_capital_missing"], [], company["company_no"])
            self.assertIsNotNone(company["current_share_capital_row"])

    def test_dummy_has_one_current_ro_each(self):
        for company in self.data["companies"]:
            current = [e["row"] for e in company["registered_offices"] if not e["outdated"]]
            self.assertEqual(len(current), 1, company["company_no"])
        self.assertEqual([e["row"] for e in self.company("46")["registered_offices"] if not e["outdated"]], [20])
        self.assertEqual([e["row"] for e in self.company("182")["registered_offices"] if not e["outdated"]], [61])

    def test_colour_groups(self):
        groups = {c["company_no"]: c["color_group"] for c in self.data["companies"]}
        self.assertEqual(groups, {"351": "orange", "46": "orange", "182": "red"})
        self.assertEqual(self.data["summary"]["by_color_group"], {"orange": 2, "red": 1})

    def test_red_company_is_inactive(self):
        company = self.company("182")
        self.assertTrue(company["red_label"])
        self.assertFalse(company["active"])
        self.assertTrue(self.company("351")["active"])
        self.assertFalse(self.company("351")["red_label"])

    def test_status_and_independent_shareholder(self):
        company = self.company("182")
        self.assertEqual(company["status"], "resigned")
        self.assertEqual(company["status_date"], "2026-03-30")
        self.assertEqual(company["shareholders"][0]["name_en"], "sh1_name")
        self.assertEqual(company["shareholding_summary"][0]["label"], "SH1, DIR1")


if __name__ == "__main__":
    unittest.main()
