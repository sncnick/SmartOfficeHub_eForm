import os
import unittest

os.environ["CDD_STORE"] = "memory"

from fastapi import HTTPException

from app.main import (
    excel_mappings,
    pdf_mappings,
    save_excel_mappings,
    save_pdf_mappings,
    save_word_mappings,
    word_mappings,
)
from app.store import MemoryStore, reset_store


class PdfMappingStoreTests(unittest.TestCase):
    def test_company_replace_leaves_pdf_mappings(self):
        store = MemoryStore()
        store.save_pdf_mapping(
            "sample-form.pdf",
            {"pdf-1-1": {"key": "RO.address", "label": "地址", "kind": "text", "group": "RO", "options": []}},
            {"pdf-1-2": True},
            "t",
        )
        store.save_form_mapping(
            "word",
            "sample-form.docx",
            {"maps": {"12": {"key": "RO.address", "label": "地址", "kind": "text"}}, "ticks": {"1-1": True}},
            "t",
        )
        store.save_form_mapping(
            "excel",
            "sample-form.xlsx",
            {"maps": {"CDD\t2\tB": {"key": "company.no", "label": "公司編號", "kind": "text"}}, "marks": {}},
            "t",
        )
        store.replace_all([], {"file": "import.xlsx"})
        saved = store.get_pdf_mapping("sample-form.pdf")
        self.assertEqual(saved["maps"]["pdf-1-1"]["key"], "RO.address")
        self.assertTrue(saved["ticks"]["pdf-1-2"])
        self.assertEqual(store.get_form_mapping("word", "sample-form.docx")["maps"]["12"]["key"], "RO.address")
        self.assertEqual(store.get_form_mapping("excel", "sample-form.xlsx")["maps"]["CDD\t2\tB"]["key"], "company.no")
        self.assertEqual(store.count(), 0)


class PdfMappingApiTests(unittest.TestCase):
    def setUp(self):
        reset_store()

    def test_put_then_get_keeps_one_file_and_drops_bad_keys(self):
        name = "sample-form.pdf"
        body = save_pdf_mappings({
            "file": name,
            "maps": {
                "pdf-1-1": {
                    "key": "RO.address",
                    "label": "地址",
                    "kind": "text",
                    "group": "RO",
                    "options": [],
                },
                "nope": {"key": "X", "label": "X", "kind": "text"},
            },
            "ticks": {"pdf-1-2": True, "nope": True},
        })
        self.assertEqual(set(body["maps"]), {"pdf-1-1"})
        self.assertEqual(body["ticks"], {"pdf-1-2": True})
        again = pdf_mappings(name)
        self.assertEqual(again["maps"]["pdf-1-1"]["label"], "地址")

    def test_a_browser_field_name_is_kept(self):
        name = "sample-form.pdf"
        ident = "pdf-form1.Page1.BusinessName"
        body = save_pdf_mappings({
            "file": name,
            "maps": {ident: {"key": "company.name_zh", "label": "中文名稱", "kind": "text", "group": "公司"}},
            "ticks": {},
        })
        self.assertEqual(body["maps"][ident]["key"], "company.name_zh")
        self.assertNotIn("pdf-has space", save_pdf_mappings({
            "file": name,
            "maps": {"pdf-has space": {"key": "X", "label": "X", "kind": "text"}},
            "ticks": {},
        })["maps"])

    def test_a_choice_option_is_kept_for_a_tick_box(self):
        name = "sample-form.pdf"
        body = save_pdf_mappings({
            "file": name,
            "maps": {
                "pdf-1-3": {
                    "key": "company.type",
                    "label": "公司類別 · 私人 Private",
                    "kind": "choice",
                    "group": "公司",
                    "option": "private",
                    "options": [
                        {"value": "private", "label": "私人 Private"},
                        {"value": "public", "label": "公眾 Public"},
                    ],
                },
            },
            "ticks": {},
        })
        saved = body["maps"]["pdf-1-3"]
        self.assertEqual(saved["option"], "private")
        self.assertEqual(saved["options"][0]["value"], "private")
        again = pdf_mappings(name)
        self.assertEqual(again["maps"]["pdf-1-3"]["option"], "private")

    def test_a_second_file_does_not_replace_the_first(self):
        first = "sample-form.pdf"
        second = "other-form.pdf"
        save_pdf_mappings({
            "file": first,
            "maps": {"pdf-1-1": {"key": "RO.address", "label": "地址", "kind": "text"}},
            "ticks": {},
        })
        save_pdf_mappings({
            "file": second,
            "maps": {"pdf-1-1": {"key": "company_no", "label": "編號", "kind": "text"}},
            "ticks": {},
        })
        kept = pdf_mappings(first)
        self.assertEqual(kept["maps"]["pdf-1-1"]["key"], "RO.address")

    def test_a_path_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            save_pdf_mappings({"file": "../sample-form.pdf", "maps": {}, "ticks": {}})
        self.assertEqual(caught.exception.status_code, 400)

    def test_word_mapping_keeps_fields_and_drops_bad_keys(self):
        name = "sample-form.docx"
        body = save_word_mappings({
            "file": name,
            "maps": {
                "12": {"key": "RO.address", "label": "地址", "kind": "text", "group": "註冊地址"},
                "gap-1-2": {"lock": True},
                "../nope": {"key": "X", "label": "X", "kind": "text"},
            },
            "ticks": {"1-3": True, "bad tick": True},
        })
        self.assertEqual(set(body["maps"]), {"12", "gap-1-2"})
        self.assertEqual(body["maps"]["gap-1-2"], {"lock": True})
        self.assertEqual(body["ticks"], {"1-3": True})
        again = word_mappings(name)
        self.assertEqual(again["maps"]["12"]["key"], "RO.address")

    def test_excel_mapping_keeps_cells_marks_and_sheet_names(self):
        name = "sample-form.xlsx"
        body = save_excel_mappings({
            "file": name,
            "maps": {
                "CDD\t2\tB": {"key": "company.no", "label": "公司編號", "kind": "text", "group": "公司"},
                "bad": {"key": "X", "label": "X", "kind": "text"},
            },
            "marks": {
                "表格\t3\tAA": [{
                    "scope": "cell",
                    "field": {"key": "RO.address", "label": "地址", "kind": "text", "group": "註冊地址"},
                    "fields": [{"key": "RO.address", "label": "地址", "kind": "text", "group": "註冊地址"}],
                }],
                "CDD\t4\tA": [{"scope": "lock"}],
                "CDD\t5\tC": [{"scope": "part", "start": 1, "end": 3, "excerpt": "[[ ]]", "field": None}],
            },
        })
        self.assertEqual(set(body["maps"]), {"CDD\t2\tB"})
        self.assertEqual(body["marks"]["表格\t3\tAA"][0]["field"]["key"], "RO.address")
        self.assertEqual(body["marks"]["CDD\t4\tA"], [{"scope": "lock"}])
        self.assertEqual(body["marks"]["CDD\t5\tC"][0]["excerpt"], "[[ ]]")
        self.assertIsNone(body["marks"]["CDD\t5\tC"][0]["field"])
        again = excel_mappings(name)
        self.assertEqual(again["maps"]["CDD\t2\tB"]["label"], "公司編號")

    def test_excel_and_word_paths_are_refused(self):
        with self.assertRaises(HTTPException) as caught:
            save_word_mappings({"file": "../sample-form.docx", "maps": {}, "ticks": {}})
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException) as caught_excel:
            save_excel_mappings({"file": "../sample-form.xlsx", "maps": {}, "marks": {}})
        self.assertEqual(caught_excel.exception.status_code, 400)
