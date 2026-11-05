import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.worksheet.pagebreak import Break

from app.form_pages import page_ranges, split_form_pages, split_workbook_bytes


class FormPageTests(unittest.TestCase):
    def test_print_areas_become_numbered_sheets_and_index_is_skipped(self):
        book = Workbook()
        form = book.active
        form.title = "IRF-A"
        form["A1"] = "第一頁"
        form["A34"] = "第二頁"
        form["C67"] = "第三頁"
        form["A34"].font = Font(bold=True)
        form.merge_cells("A1:B2")
        form.column_dimensions["A"].width = 18
        form.row_dimensions[34].height = 30
        form.print_area = "A1:G33,A34:G66,A67:G99"
        form.page_setup.orientation = "portrait"
        form.page_setup.paperSize = 9
        form.page_setup.scale = 83
        form.page_setup.fitToWidth = None
        form.page_setup.fitToHeight = None
        form.sheet_properties.pageSetUpPr.fitToPage = False
        form.page_margins.left = 0.7
        form.page_margins.header = 0.3
        form.print_options.horizontalCentered = True
        form.oddHeader.center.text = "IRF-A"
        form.HeaderFooter.scaleWithDoc = True
        index = book.create_sheet("Index")
        index["A1"] = "目錄"
        single = book.create_sheet("Notes")
        single["B2"] = "只有一頁"
        single.print_area = "A1:C5"

        output = split_form_pages(book)

        self.assertEqual(output.sheetnames, ["IRF-A-P1", "IRF-A-P2", "IRF-A-P3", "Notes"])
        self.assertEqual(output["IRF-A-P1"]["A1"].value, "第一頁")
        self.assertEqual(output["IRF-A-P2"]["A1"].value, "第二頁")
        self.assertTrue(output["IRF-A-P2"]["A1"].font.bold)
        self.assertEqual(output["IRF-A-P2"].row_dimensions[1].height, 30)
        self.assertEqual(output["IRF-A-P3"]["C1"].value, "第三頁")
        self.assertEqual(list(output["IRF-A-P1"].merged_cells.ranges)[0].coord, "A1:B2")
        self.assertEqual(output["IRF-A-P1"].column_dimensions["A"].width, 18)
        self.assertEqual(output["IRF-A-P1"].page_setup.scale, 83)
        self.assertEqual(output["IRF-A-P2"].page_setup.scale, 83)
        self.assertEqual(output["IRF-A-P3"].page_setup.scale, 83)
        self.assertEqual(output["IRF-A-P2"].page_setup.paperSize, 9)
        self.assertFalse(output["IRF-A-P2"].sheet_properties.pageSetUpPr.fitToPage)
        self.assertEqual(output["IRF-A-P3"].page_margins.left, 0.7)
        self.assertEqual(output["IRF-A-P3"].page_margins.header, 0.3)
        self.assertTrue(output["IRF-A-P1"].print_options.horizontalCentered)
        self.assertEqual(output["IRF-A-P2"].oddHeader.center.text, "IRF-A")
        self.assertTrue(output["IRF-A-P2"].HeaderFooter.scaleWithDoc)
        self.assertIn("A1:G33", output["IRF-A-P1"].print_area.replace("$", ""))
        self.assertEqual(output["Notes"]["B2"].value, "只有一頁")

    def test_one_print_area_splits_on_a_manual_row_break(self):
        book = Workbook()
        form = book.active
        form.title = "CDD"
        form["A1"] = "上半"
        form["A11"] = "下半"
        form["B11"] = "=A11"
        form["C11"] = "=Z9"
        form["D11"] = "=A11+B11"
        form["E11"] = "=[1]Index!B15"
        form.print_area = "A1:E20"
        form.row_breaks.append(Break(id=10))

        output = split_form_pages(book)

        self.assertEqual(output.sheetnames, ["CDD-P1", "CDD-P2"])
        self.assertEqual(output["CDD-P1"]["A1"].value, "上半")
        self.assertEqual(output["CDD-P2"]["A1"].value, "下半")
        self.assertEqual(output["CDD-P2"]["B1"].value, "下半")
        self.assertEqual(output["CDD-P2"]["C1"].value, "")
        self.assertEqual(output["CDD-P2"]["D1"].value, "=A1+B1")
        self.assertEqual(output["CDD-P2"]["E1"].value, "")
        self.assertEqual(output["CDD-P2"]["B1"].comment.text, "引用：A1")
        self.assertEqual(output["CDD-P2"]["C1"].comment.text, "引用：Z9")
        self.assertEqual(output["CDD-P2"]["E1"].comment.text, "引用：Index!B15")

    def test_cell_call_keeps_the_shown_text_and_the_reference(self):
        book = Workbook()
        form = book.active
        form.title = "CDD"
        form["A1"] = "Hong Kong"
        form["B1"] = "=A1"
        form.print_area = "A1:B2"
        shown = Workbook()
        mirror = shown.active
        mirror.title = "CDD"
        mirror["B1"] = "Hong Kong"

        output = split_form_pages(book, shown)

        self.assertEqual(output["CDD"]["B1"].value, "Hong Kong")
        self.assertEqual(output["CDD"]["B1"].comment.text, "引用：A1")
        self.assertNotEqual(output["CDD"]["B1"].value, "[[]]")

    def test_an_empty_reference_stays_blank_even_when_a_cached_value_exists(self):
        book = Workbook()
        form = book.active
        form.title = "CDD"
        form["A1"] = None
        form["B1"] = "=A1"
        form.print_area = "A1:B2"
        shown = Workbook()
        mirror = shown.active
        mirror.title = "CDD"
        mirror["B1"] = "舊資料"

        output = split_form_pages(book, shown)

        self.assertEqual(output["CDD"]["B1"].value, "")
        self.assertEqual(output["CDD"]["B1"].comment.text, "引用：A1")

    def test_a_manual_break_inside_one_of_several_print_areas_adds_a_page(self):
        book = Workbook()
        form = book.active
        form.title = "CDD"
        form["A1"] = "第一範圍"
        form["A5"] = "第二範圍上"
        form["A9"] = "第二範圍下"
        form.print_area = "A1:B4,A5:B12"
        form.row_breaks.append(Break(id=8))

        output = split_form_pages(book)

        self.assertEqual(output.sheetnames, ["CDD-P1", "CDD-P2", "CDD-P3"])
        self.assertEqual(output["CDD-P1"]["A1"].value, "第一範圍")
        self.assertEqual(output["CDD-P2"]["A1"].value, "第二範圍上")
        self.assertEqual(output["CDD-P3"]["A1"].value, "第二範圍下")

    def test_manual_breaks_split_a_sheet_without_a_print_area(self):
        book = Workbook()
        form = book.active
        form.title = "CDD"
        form["A1"] = "上半"
        form["A8"] = "下半"
        form.row_breaks.append(Break(id=4))

        output = split_form_pages(book)

        self.assertEqual(output.sheetnames, ["CDD-P1", "CDD-P2"])
        self.assertEqual(output["CDD-P1"]["A1"].value, "上半")
        self.assertEqual(output["CDD-P2"]["A4"].value, "下半")

    def test_a_comma_in_the_sheet_name_keeps_every_print_area(self):
        book = Workbook()
        form = book.active
        form.title = "Foo, Bar"
        ranges = page_ranges(form, "'Foo, Bar'!$A$1:$B$2,'Foo, Bar'!$A$3:$B$4")
        self.assertEqual(ranges, [(1, 1, 2, 2), (1, 3, 2, 4)])

    def test_a_whole_column_range_does_not_drop_the_other_pages(self):
        book = Workbook()
        form = book.active
        form.title = "CDD"
        form["B4"] = "欄"
        ranges = page_ranges(form, "'CDD'!$A:$B,'CDD'!$D$1:$E$2")
        self.assertEqual(ranges, [(1, 1, 2, 4), (4, 1, 5, 2)])

    def test_a_print_area_openpyxl_drops_is_still_split(self):
        book = Workbook()
        form = book.active
        form.title = "CDD"
        form["A1"] = "上"
        form["A5"] = "下"
        form.print_area = "A1:B2"
        buffer = BytesIO()
        book.save(buffer)
        packed = zipfile.ZipFile(BytesIO(buffer.getvalue()))
        files = {name: packed.read(name) for name in packed.namelist()}
        workbook = files["xl/workbook.xml"].decode("utf-8")
        workbook = workbook.replace("$A$1:$B$2", "$A:$B,'CDD'!$A$5:$B$6", 1)
        files["xl/workbook.xml"] = workbook.encode("utf-8")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "form.xlsx"
            with zipfile.ZipFile(path, "w") as target:
                for name, data in files.items():
                    target.writestr(name, data)
            saved = load_workbook(path)
            self.assertFalse(str(saved["CDD"].print_area or "").strip())
            saved.close()
            output = load_workbook(BytesIO(split_workbook_bytes(path)))
        self.assertEqual(output.sheetnames, ["CDD-P1", "CDD-P2"])
        self.assertEqual(output["CDD-P1"]["A1"].value, "上")
        self.assertEqual(output["CDD-P2"]["A1"].value, "下")
        output.close()

    def test_page_ranges_follow_the_print_area_order(self):
        book = Workbook()
        form = book.active
        form.print_area = "A1:B2,D4:E5"
        self.assertEqual(page_ranges(form), [(1, 1, 2, 2), (4, 4, 5, 5)])

    def test_round_trip_bytes_keep_the_pages(self):
        book = Workbook()
        form = book.active
        form.title = "IRF-A"
        form["A1"] = "頁"
        form.print_area = "A1:B2,A3:B4"
        buffer = BytesIO()
        book.save(buffer)
        saved = load_workbook(BytesIO(buffer.getvalue()))
        output = split_form_pages(saved)
        self.assertEqual(output.sheetnames, ["IRF-A-P1", "IRF-A-P2"])
