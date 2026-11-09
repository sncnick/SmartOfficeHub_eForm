import struct
import unittest
import zlib

from app.pdf_preview import preview_pdf


def _pdf(objects: list[bytes]) -> bytes:
    header = b"%PDF-1.4\n"
    chunks = [header]
    offsets = [0]
    cursor = len(header)
    for index, body in enumerate(objects, start=1):
        offsets.append(cursor)
        piece = f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
        chunks.append(piece)
        cursor += len(piece)
    xref = [f"xref\n0 {len(objects) + 1}\n", "0000000000 65535 f \n"]
    xref.extend(f"{offset:010d} 00000 n \n" for offset in offsets[1:])
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{cursor}\n%%EOF\n"
    )
    return b"".join(chunks) + "".join(xref).encode() + trailer.encode()


def _form_pdf() -> bytes:
    content = b"q\n0 0 0 rg\n72 700 228 30 re\nf\nQ\n"
    stream = b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream"
    return _pdf([
        b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [6 0 R 7 0 R] >> >>",
        b"<< /Type /Pages /Kids [3 0 R 8 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Annots [6 0 R 7 0 R] /Resources << /Font << /F1 5 0 R >> >> >>",
        stream,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (CompanyName) /Rect [72 700 300 730] /F 4 /P 3 0 R >>",
        b"<< /Type /Annot /Subtype /Widget /FT /Btn /T (Agree) /Rect [72 640 90 658] /F 4 /P 3 0 R /V /Off /AS /Off >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 9 0 R /Resources << >> >>",
        b"<< /Length 0 >>\nstream\nendstream",
    ])


class PdfPreviewTests(unittest.TestCase):
    def test_a_fillable_pdf_lists_its_widgets_on_the_page(self):
        laid = preview_pdf(_form_pdf())
        self.assertEqual(laid["kind"], "pdf")
        self.assertEqual(laid["render"], "page")
        self.assertEqual(laid["page_count"], 2)
        page = laid["pages"][0]
        self.assertEqual(page["width"], round(595 * 96 / 72))
        self.assertEqual(page["height"], round(842 * 96 / 72))
        names = {field["name"]: field for field in page["fields"]}
        text = names["CompanyName"]
        self.assertEqual(text["control"], "text")
        self.assertTrue(text["blank"])
        self.assertEqual(text["x"], round(72 * 96 / 72))
        self.assertEqual(text["y"], round((842 - 730) * 96 / 72))
        self.assertEqual(text["w"], round(228 * 96 / 72))
        self.assertEqual(text["h"], round(30 * 96 / 72))
        self.assertEqual(names["Agree"]["control"], "checkbox")
        self.assertEqual(laid["pages"][1]["fields"], [])

    @unittest.skipUnless(__import__("shutil").which("pdftoppm"), "needs poppler")
    def test_the_text_field_covers_the_box_drawn_on_the_page(self):
        from app.pdf_preview import page_png

        raw = _form_pdf()
        laid = preview_pdf(raw)
        field = next(item for item in laid["pages"][0]["fields"] if item["name"] == "CompanyName")
        png = page_png(raw, 1)
        width, height, luma = _luma(png)
        scale_x = width / laid["pages"][0]["width"]
        scale_y = height / laid["pages"][0]["height"]
        x0 = round(field["x"] * scale_x)
        y0 = round(field["y"] * scale_y)
        x1 = round((field["x"] + field["w"]) * scale_x)
        y1 = round((field["y"] + field["h"]) * scale_y)
        dark = 0
        inside = 0
        for y in range(height):
            row = y * width
            for x in range(width):
                if luma[row + x] > 40:
                    continue
                dark += 1
                if x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2:
                    inside += 1
        self.assertGreater(dark, 100)
        self.assertGreater(inside / dark, 0.9)

    def test_an_aes_form_with_an_empty_password_can_be_previewed(self):
        from io import BytesIO

        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("", algorithm="AES-256")
        raw = BytesIO()
        writer.write(raw)
        laid = preview_pdf(raw.getvalue())
        self.assertEqual(laid["kind"], "pdf")
        self.assertEqual(laid["page_count"], 1)

    def test_an_adobe_reader_shell_is_replaced_by_its_form(self):
        from io import BytesIO

        from pypdf import PdfWriter
        from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, TextStringObject

        template = """<?xml version="1.0"?>
<template xmlns="http://www.xfa.org/schema/xfa-template/3.0/">
  <subform name="form1" layout="position" w="210mm" h="297mm">
    <pageSet><pageArea>
      <contentArea x="0mm" y="0mm" w="210mm" h="297mm"/>
      <medium stock="a4" short="210mm" long="297mm"/>
    </pageArea></pageSet>
    <field name="CompanyName" x="20mm" y="40mm" w="80mm" h="8mm">
      <ui><textEdit/></ui>
      <caption><value><text>Company</text></value></caption>
    </field>
    <field name="PrivateBox" x="20mm" y="60mm" w="5mm" h="5mm">
      <ui><checkButton/></ui>
    </field>
  </subform>
</template>
""".encode()
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        page = writer.pages[0]
        warning = DecodedStreamObject()
        warning.set_data(
            b"BT /F1 12 Tf 72 720 Td "
            b"(The document you are trying to load requires Adobe Reader 8 or higher) Tj ET"
        )
        page[NameObject("/Contents")] = writer._add_object(warning)
        packet = DecodedStreamObject()
        packet.set_data(template)
        form = DictionaryObject()
        form[NameObject("/XFA")] = ArrayObject([
            TextStringObject("template"),
            writer._add_object(packet),
        ])
        writer._root_object[NameObject("/AcroForm")] = form
        raw = BytesIO()
        writer.write(raw)
        laid = preview_pdf(raw.getvalue())
        self.assertEqual(laid["render"], "browser")
        names = [item["name"] for item in laid["pages"][0]["fields"]]
        self.assertNotIn("form1.CompanyName", names)
        self.assertNotIn("form1.PrivateBox", names)

    def test_xfa_without_widgets_is_shown_even_without_the_reader_sentence(self):
        from io import BytesIO

        from pypdf import PdfWriter
        from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, TextStringObject

        template = (
            b'<?xml version="1.0"?><template xmlns="http://www.xfa.org/schema/xfa-template/3.0/">'
            b'<subform name="form1" layout="position">'
            b'<field name="Address" x="10mm" y="10mm" w="40mm" h="8mm"><ui><textEdit/></ui></field>'
            b"</subform></template>"
        )
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        packet = DecodedStreamObject()
        packet.set_data(template)
        form = DictionaryObject()
        form[NameObject("/XFA")] = ArrayObject([TextStringObject("template"), writer._add_object(packet)])
        writer._root_object[NameObject("/AcroForm")] = form
        raw = BytesIO()
        writer.write(raw)
        laid = preview_pdf(raw.getvalue())
        self.assertEqual(laid["render"], "browser")
        self.assertNotIn("form1.Address", [item["name"] for item in laid["pages"][0]["fields"]])

    def test_a_hidden_form_is_drawn_and_the_warning_is_left_out(self):
        template = """<?xml version="1.0"?>
<template xmlns="http://www.xfa.org/schema/xfa-template/3.0/">
  <subform name="form1" presence="hidden" layout="position">
    <draw name="Notice" x="10mm" y="10mm" w="160mm" h="20mm">
      <value><text>The document you are trying to load requires Adobe Reader 8 or higher. See http://www.adobe.com/go/pdf_forms_configure</text></value>
    </draw>
    <field name="Business" presence="hidden" x="20mm" y="80mm" w="70mm" h="8mm"><ui><textEdit/></ui></field>
  </subform>
</template>
""".encode()
        laid = preview_pdf(_xfa_pdf(template))
        self.assertEqual(laid["render"], "browser")
        self.assertNotIn("form1.Business", [item["name"] for item in laid["pages"][0]["fields"]])

    def test_a_script_with_a_less_than_sign_does_not_hide_the_field(self):
        template = b"""<?xml version="1.0"?>
<template xmlns="http://www.xfa.org/schema/xfa-template/3.0/">
  <subform name="form1" layout="position">
    <field name="Company" x="12mm" y="20mm" w="50mm" h="8mm">
      <ui><textEdit/></ui>
      <event activity="initialize"><script>if (a < b) { this.rawValue = 1; }</script></event>
    </field>
  </subform>
</template>"""
        laid = preview_pdf(_xfa_pdf(template))
        self.assertEqual(laid["render"], "browser")

    def test_a_use_reference_keeps_the_tick_box(self):
        template = b"""<?xml version="1.0"?>
<template xmlns="http://www.xfa.org/schema/xfa-template/3.0/">
  <subform name="form1" layout="position">
    <proto>
      <field id="Private" name="Private" w="5mm" h="5mm"><ui><checkButton/></ui></field>
    </proto>
    <field use="#Private" name="PrivateBox" x="20mm" y="30mm"/>
  </subform>
</template>"""
        laid = preview_pdf(_xfa_pdf(template))
        self.assertEqual(laid["render"], "browser")
        self.assertFalse(any(item["name"].endswith("PrivateBox") for item in laid["pages"][0]["fields"]))

    def test_the_packet_that_actually_contains_fields_is_used(self):
        stub = (
            b'<?xml version="1.0"?><template xmlns="http://www.xfa.org/schema/xfa-template/3.0/">'
            b'<subform name="empty"/></template>'
        )
        real = (
            b'<?xml version="1.0"?><template xmlns="http://www.xfa.org/schema/xfa-template/3.0/">'
            b'<subform name="form1" layout="position">'
            b'<field name="BusinessNo" x="15mm" y="20mm" w="40mm" h="8mm"><ui><textEdit/></ui></field>'
            b"</subform></template>"
        )
        laid = preview_pdf(_xfa_pdf(stub, extra=[("form", real)]))
        self.assertEqual(laid["render"], "browser")
        self.assertNotIn("form1.BusinessNo", [item["name"] for item in laid["pages"][0]["fields"]])


def _xfa_pdf(template: bytes, extra=None) -> bytes:
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, TextStringObject

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    page = writer.pages[0]
    warning = DecodedStreamObject()
    warning.set_data(b"BT /F1 12 Tf 72 720 Td (The document you are trying to load requires Adobe Reader 8 or higher) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(warning)
    packets = [("template", template), *(extra or [])]
    array = ArrayObject()
    for name, payload in packets:
        stream = DecodedStreamObject()
        stream.set_data(payload)
        array.append(TextStringObject(name))
        array.append(writer._add_object(stream))
    form = DictionaryObject()
    form[NameObject("/XFA")] = array
    writer._root_object[NameObject("/AcroForm")] = form
    raw = BytesIO()
    writer.write(raw)
    return raw.getvalue()


def _luma(data: bytes):
    pos = 8
    width = height = color = None
    compressed = b""
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, _bit, color = struct.unpack(">IIBB", chunk[:10])
        elif kind == b"IDAT":
            compressed += chunk
        elif kind == b"IEND":
            break
        pos += 12 + length
    raw = zlib.decompress(compressed)
    channels = 3 if color == 2 else 4
    stride = width * channels
    luma = bytearray(width * height)
    index = 0
    prev = bytearray(stride)
    for y in range(height):
        filt = raw[index]
        index += 1
        row = bytearray(raw[index:index + stride])
        index += stride
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
                upper = prev[x - channels] if x >= channels else 0
                estimate = left + up - upper
                nearest = left if abs(estimate - left) <= abs(estimate - up) and abs(estimate - left) <= abs(estimate - upper) else up if abs(estimate - up) <= abs(estimate - upper) else upper
                row[x] = (row[x] + nearest) & 255
        for x in range(width):
            pixel = x * channels
            luma[y * width + x] = row[pixel]
        prev = row
    return width, height, luma


if __name__ == "__main__":
    unittest.main()
