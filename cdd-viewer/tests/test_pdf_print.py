import shutil
import subprocess
import tempfile
import unittest
import zlib
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from app.pdf_preview import _to_top_left
from app.pdf_print import _from_top_left, compose_pdf
from tests.test_pdf_preview import _luma, _pdf, _xfa_pdf


def _bar_pdf() -> bytes:
    # A black strip on the physical top edge, and a text field lower down.
    content = b"q\n0 0 0 rg\n0 822 595 20 re\nf\nQ\n"
    stream = b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream"
    return _pdf([
        b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Annots [5 0 R] /Resources << >> >>",
        stream,
        b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (CompanyName) /Rect [72 400 300 430] /F 4 /P 3 0 R >>",
    ])


def _render(raw: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        pdf = root / "page.pdf"
        pdf.write_bytes(raw)
        subprocess.run(
            ["pdftoppm", "-png", "-r", "72", "-cropbox", "-f", "1", "-l", "1", str(pdf), str(root / "out")],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        images = sorted(root.glob("out*.png"))
        if not images:
            raise AssertionError("renderer produced no page")
        return images[0].read_bytes()


def _row_mean(luma: bytes, width: int, y: int) -> float:
    start = y * width
    return sum(luma[start:start + width]) / width


class PdfPrintTests(unittest.TestCase):
    def test_display_points_round_trip_to_the_pdf_page(self):
        for rotation in (0, 90, 180, 270):
            for point in ((0, 0), (10, 20), (200, 400), (594, 841)):
                shown = _to_top_left(point[0], point[1], 595, 842, rotation)
                back = _from_top_left(shown[0], shown[1], 595, 842, rotation)
                self.assertAlmostEqual(back[0], point[0], places=4)
                self.assertAlmostEqual(back[1], point[1], places=4)

    @unittest.skipUnless(shutil.which("pdftoppm"), "needs poppler")
    def test_the_top_of_the_page_stays_on_the_same_page(self):
        raw = _bar_pdf()
        printed = compose_pdf(raw, [{"page": 1, "id": "pdf-1-1", "text": "Acme", "size": 12}])
        reader = PdfReader(BytesIO(printed))
        box = reader.pages[0].mediabox
        self.assertAlmostEqual(float(box.width), 595, places=2)
        self.assertAlmostEqual(float(box.height), 842, places=2)
        self.assertEqual(len(reader.pages), 1)
        _width, _height, before = _luma(_render(raw))
        width, height, after = _luma(_render(printed))
        self.assertEqual((width, height), (595, 842))
        self.assertLess(_row_mean(before, width, 5), 40)
        self.assertLess(_row_mean(after, width, 5), 40)
        self.assertGreater(_row_mean(after, width, 30), 200)
        text_rows = range(412, 442)
        dark = 0
        for y in text_rows:
            start = y * width
            dark += sum(1 for value in after[start:start + width] if value < 200)
        self.assertGreater(dark, 8)
        self.assertLess(height, 900)

    def test_filled_cjk_uses_a_truetype_appearance(self):
        printed = compose_pdf(_bar_pdf(), [{"page": 1, "id": "pdf-1-1", "text": "測試地址", "size": 12}])
        reader = PdfReader(BytesIO(printed))
        widget = reader.pages[0]["/Annots"][0].get_object()
        self.assertEqual(str(widget.get("/V")), "測試地址")
        appearance = widget["/AP"]["/N"].get_object()
        stream = appearance.get_data()
        self.assertIn("測試".encode("utf-16-be"), stream)
        self.assertIn("地址".encode("utf-16-be"), stream)
        self.assertNotIn(b"/F1 ", stream)
        font = appearance["/Resources"]["/Font"]["/CDDF1"].get_object()
        cid = font["/DescendantFonts"][0].get_object()
        gid_map = cid["/CIDToGIDMap"].get_object().get_data()

        def glyph(code: int) -> int:
            return int.from_bytes(gid_map[code * 2:code * 2 + 2], "big")

        self.assertGreater(glyph(0x5730), 0)
        self.assertGreater(glyph(0x5740), 0)
        embedded = cid["/FontDescriptor"].get_object()["/FontFile2"].get_object().get_data()
        self.assertTrue(embedded.startswith(b"\x00\x01\x00\x00"))
        form = reader.trailer["/Root"]["/AcroForm"].get_object()
        self.assertFalse(form["/NeedAppearances"].value)

    def test_browser_values_are_written_into_the_form_data(self):
        template = (
            b'<?xml version="1.0"?>'
            b'<template xmlns="http://www.xfa.org/schema/xfa-template/3.0/">'
            b'<subform name="form1"><pageSet><pageArea><contentArea/></pageArea></pageSet>'
            b'<field name="BusinessName"><ui><textEdit/></ui></field></subform></template>'
        )
        datasets = (
            b'<xfa:datasets xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/">'
            b'<xfa:data><form1><BusinessName></BusinessName></form1></xfa:data></xfa:datasets>'
        )
        raw = _xfa_pdf(template, extra=[("datasets", datasets)])
        printed = compose_pdf(raw, [{
            "page": 1,
            "id": "pdf-1.field1.BusinessName",
            "name": "form1.BusinessName",
            "text": "Acme",
        }])
        xfa = PdfReader(BytesIO(printed)).trailer["/Root"]["/AcroForm"].get_object()["/XFA"]
        saved = list(xfa)[3].get_object().get_data()
        self.assertIn(b"Acme", saved)
        self.assertNotIn(b"<?xml", saved)

    def test_a_filtered_dataset_can_still_be_saved(self):
        datasets = (
            b'<xfa:datasets xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/">'
            b'<xfa:data><form1><BusinessName></BusinessName></form1></xfa:data></xfa:datasets>'
        )
        template = (
            b'<?xml version="1.0"?>'
            b'<template xmlns="http://www.xfa.org/schema/xfa-template/3.0/">'
            b'<subform name="form1"><pageSet><pageArea><contentArea/></pageArea></pageSet>'
            b'<field name="BusinessName"><ui><textEdit/></ui></field></subform></template>'
        )
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        warning = DecodedStreamObject()
        warning.set_data(b"BT /F1 12 Tf 72 720 Td (The document you are trying to load requires Adobe Reader 8 or higher) Tj ET")
        writer.pages[0][NameObject("/Contents")] = writer._add_object(warning)
        compressed = zlib.compress(datasets)
        dataset = EncodedStreamObject()
        dataset[NameObject("/Filter")] = ArrayObject([NameObject("/FlateDecode")])
        dataset._data = compressed
        dataset[NameObject("/Length")] = NumberObject(len(compressed))
        template_stream = DecodedStreamObject()
        template_stream.set_data(template)
        array = ArrayObject([
            TextStringObject("template"),
            writer._add_object(template_stream),
            TextStringObject("datasets"),
            writer._add_object(dataset),
        ])
        form = DictionaryObject({NameObject("/XFA"): array})
        writer._root_object[NameObject("/AcroForm")] = form
        raw = BytesIO()
        writer.write(raw)
        printed = compose_pdf(raw.getvalue(), [{
            "page": 1,
            "id": "pdf-1.field1.BusinessName",
            "name": "form1.BusinessName",
            "text": "Acme",
        }])
        xfa = PdfReader(BytesIO(printed)).trailer["/Root"]["/AcroForm"].get_object()["/XFA"]
        saved = list(xfa)[3].get_object().get_data()
        self.assertIn(b"Acme", saved)
        self.assertNotIn(b"<?xml", saved)

    def test_utf16_form_data_keeps_its_encoding_and_has_no_extra_header(self):
        datasets = (
            "<xfa:datasets xmlns:xfa=\"http://www.xfa.org/schema/xfa-data/1.0/\">"
            "<xfa:data><form1><BusinessName></BusinessName></form1></xfa:data></xfa:datasets>"
        ).encode("utf-16")
        template = (
            b'<?xml version="1.0"?>'
            b'<template xmlns="http://www.xfa.org/schema/xfa-template/3.0/">'
            b'<subform name="form1"><pageSet><pageArea><contentArea/></pageArea></pageSet>'
            b'<field name="BusinessName"><ui><textEdit/></ui></field></subform></template>'
        )
        raw = _xfa_pdf(template, extra=[("datasets", datasets)])
        printed = compose_pdf(raw, [{
            "page": 1,
            "id": "pdf-1.field1.BusinessName",
            "name": "form1.BusinessName",
            "text": "Acme",
        }])
        xfa = PdfReader(BytesIO(printed)).trailer["/Root"]["/AcroForm"].get_object()["/XFA"]
        saved = list(xfa)[3].get_object().get_data()
        text = saved.decode("utf-16")
        self.assertIn("Acme", text)
        self.assertNotIn("<?xml", text)


if __name__ == "__main__":
    unittest.main()
