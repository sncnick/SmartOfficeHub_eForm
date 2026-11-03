import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.word_layout import _usable_pdf, _words, blank_gaps, collect_fields, collect_ticks, drawing_inputs, page_returns, paragraph_enters, place_fields, prepare_docx
from app.word_preview import marked_docx
from app.word_preview import preview_docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:wps="{WPS}">
  <w:body>
    <w:p>
      <w:r><w:t xml:space="preserve">本文在上 </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> MERGEFIELD TopName </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>頁首公司</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
    </w:p>
    <w:p>
      <w:r><w:drawing>
        <wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="251659264" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">
          <wp:simplePos x="0" y="0"/>
          <wp:positionH relativeFrom="page"><wp:posOffset>1143000</wp:posOffset></wp:positionH>
          <wp:positionV relativeFrom="page"><wp:posOffset>6858000</wp:posOffset></wp:positionV>
          <wp:extent cx="3200400" cy="457200"/>
          <wp:effectExtent l="0" t="0" r="0" b="0"/>
          <wp:wrapNone/>
          <wp:docPr id="1" name="TextBox1"/>
          <wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>
          <a:graphic>
            <a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
              <wps:wsp>
                <wps:cNvSpPr txBox="1"/>
                <wps:spPr>
                  <a:xfrm><a:off x="0" y="0"/><a:ext cx="3200400" cy="457200"/></a:xfrm>
                  <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
                </wps:spPr>
                <wps:txbx><w:txbxContent><w:p>
                  <w:r><w:fldChar w:fldCharType="begin"/></w:r>
                  <w:r><w:instrText xml:space="preserve"> MERGEFIELD BoxCode </w:instrText></w:r>
                  <w:r><w:fldChar w:fldCharType="separate"/></w:r>
                  <w:r><w:t>框內編號</w:t></w:r>
                  <w:r><w:fldChar w:fldCharType="end"/></w:r>
                </w:p></w:txbxContent></wps:txbx>
                <wps:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0"/>
              </wps:wsp>
            </a:graphicData>
          </a:graphic>
        </wp:anchor>
      </w:drawing></w:r>
    </w:p>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""


def _docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""")
        archive.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""")
        archive.writestr("word/_rels/document.xml.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>""")
        archive.writestr("word/document.xml", DOCUMENT)
    return buffer.getvalue()


class WordLayoutTests(unittest.TestCase):
    def test_page_words_are_read_when_the_markup_has_a_namespace(self):
        width, _height, words = _words(
            b"""<?xml version="1.0"?>
            <html xmlns="http://www.w3.org/1999/xhtml"><body><doc>
              <page width="595.2" height="841.9">
                <flow><block><line>
                  <word xMin="72" yMin="74" xMax="140" yMax="88">&#38913;&#39318;&#20844;&#21496;</word>
                </line></block></flow>
              </page>
            </doc></body></html>"""
        )
        self.assertGreater(width, 700)
        self.assertEqual(words[0]["text"], "頁首公司")
        self.assertGreater(words[0]["y"], 90)

    def test_field_codes_are_removed_without_moving_the_text_box(self):
        prepared = prepare_docx(_docx())
        with zipfile.ZipFile(io.BytesIO(prepared)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        self.assertNotIn("MERGEFIELD", xml)
        self.assertNotIn("fldChar", xml)
        self.assertIn("頁首公司", xml)
        self.assertIn("框內編號", xml)
        self.assertIn("6858000", xml)

    def test_fields_inside_a_text_box_are_collected(self):
        fields = collect_fields(preview_docx(_docx()))
        self.assertEqual([field["name"] for field in fields], ["TopName", "BoxCode"])
        self.assertEqual([field["text"] for field in fields], ["頁首公司", "框內編號"])

    def test_a_field_is_pinned_to_the_words_that_show_it(self):
        pages, loose = place_fields(
            [{"id": "1", "name": "TopName", "text": "頁首公司", "control": "text"},
             {"id": "2", "name": "Short", "text": "A", "control": "text"},
             {"id": "3", "name": "BoxCode", "text": "框內編號", "control": "text"}],
            [{
                "n": 1,
                "width": 794,
                "height": 1123,
                "words": [
                    {"text": "頁首公司", "x": 96, "y": 80, "w": 80, "h": 16},
                    {"text": "框內編號", "x": 120, "y": 720, "w": 80, "h": 16},
                ],
            }],
        )
        pinned = {field["name"]: field for field in pages[0]["fields"]}
        self.assertEqual(pinned["TopName"]["y"], 80)
        self.assertEqual(pinned["BoxCode"]["y"], 720)
        self.assertEqual([field["name"] for field in loose], ["Short"])

    def test_empty_controls_and_underlines_are_clickable_fields(self):
        raw = _blank_docx()
        fields = collect_fields(preview_docx(raw))
        self.assertEqual([field["name"] for field in fields], ["TopName", "EmptyCo", "姓名", "編號"])
        self.assertFalse(fields[0].get("blank"))
        self.assertTrue(fields[1]["blank"])
        self.assertEqual(fields[1]["text"], "")
        self.assertTrue(fields[2]["blank"])
        self.assertEqual(fields[3]["text"], "________")
        with zipfile.ZipFile(io.BytesIO(marked_docx(raw))) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        self.assertNotIn("~{1}~", xml)
        self.assertIn("~{2}~", xml)
        self.assertIn("~{3}~", xml)
        self.assertNotIn("~{4}~", xml)
        pages, loose = place_fields(fields, [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [
                {"text": "頁首公司", "x": 80, "y": 70, "w": 70, "h": 14},
                {"text": "~{2}~", "x": 80, "y": 110, "w": 8, "h": 4},
                {"text": "~{3}~", "x": 140, "y": 150, "w": 8, "h": 4},
                {"text": "________", "x": 80, "y": 190, "w": 90, "h": 14},
            ],
        }])
        pinned = {field["name"]: field for field in pages[0]["fields"]}
        self.assertEqual(pinned["TopName"]["y"], 70)
        self.assertEqual(pinned["EmptyCo"]["x"], 80)
        self.assertEqual(pinned["EmptyCo"]["y"], 110)
        self.assertLess(pinned["EmptyCo"]["w"], 100)
        self.assertEqual(pinned["姓名"]["x"], 140)
        self.assertEqual(pinned["編號"]["w"], 90)
        self.assertEqual(loose, [])

    def test_a_pdf_is_usable_even_when_the_engine_exits_with_a_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            good = root / "page.pdf"
            bad = root / "short.pdf"
            good.write_bytes(b"%PDF-1.4\n" + b"0" * 120)
            bad.write_bytes(b"%PDF-")
            self.assertTrue(_usable_pdf(good))
            self.assertFalse(_usable_pdf(bad))

    def test_a_file_that_cannot_be_annotated_is_still_drawn_from_the_original(self):
        raw = b"not-a-word-file"
        self.assertEqual(drawing_inputs(raw), [raw])
        raw_doc = _company_docx()
        prepared = drawing_inputs(raw_doc)
        self.assertEqual(prepared[0], raw_doc)
        self.assertGreater(len(prepared), 1)

    def test_a_wide_space_between_words_is_a_clickable_blank(self):
        gaps = blank_gaps([{
            "n": 1,
            "words": [
                {"text": "CompanyName", "x": 40, "y": 80, "w": 90, "h": 14},
                {"text": "Address", "x": 280, "y": 82, "w": 60, "h": 14},
                {"text": "人口", "x": 40, "y": 100, "w": 28, "h": 14},
                {"text": "統計", "x": 72, "y": 100, "w": 28, "h": 14},
            ],
        }])[1]
        spaces = [gap for gap in gaps if not gap.get("enter")]
        self.assertEqual(len(spaces), 1)
        self.assertEqual(spaces[0]["x"], 130)
        self.assertEqual(spaces[0]["y"], 80)
        self.assertEqual(spaces[0]["w"], 150)
        self.assertEqual(spaces[0]["h"], 14)
        self.assertGreater(spaces[0]["w"], 100)
        self.assertEqual(spaces[0]["name"], "CompanyName")

    def test_an_enter_sits_at_the_paragraph_end_not_on_a_wrapped_line(self):
        pages = [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [
                {"text": "甲乙", "x": 96, "y": 40, "w": 28, "h": 14},
                {"text": "丙丁", "x": 96, "y": 58, "w": 28, "h": 14},
            ],
        }]
        marks = paragraph_enters(_enter_docx("甲乙丙丁"), pages)[1]
        self.assertEqual([(mark["x"], mark["y"], mark["mark"]) for mark in marks], [(124, 58, "¶")])

    def test_enter_marks_follow_each_paragraph_and_a_blank_line(self):
        pages = [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [
                {"text": "姓名", "x": 96, "y": 40, "w": 36, "h": 14},
                {"text": "地址", "x": 96, "y": 84, "w": 36, "h": 14},
            ],
        }]
        xml = """
          <w:p><w:r><w:t>姓名</w:t></w:r></w:p>
          <w:p/>
          <w:p><w:r><w:t>地址</w:t></w:r></w:p>
        """
        marks = paragraph_enters(_enter_docx(xml, body=True), pages)[1]
        self.assertEqual([(mark["x"], mark["y"], mark["mark"]) for mark in marks], [(132, 40, "¶"), (96, 62, "¶"), (132, 84, "¶")])

    def test_a_trailing_tab_moves_the_enter_to_the_tab_stop(self):
        pages = [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [{"text": "姓名", "x": 96, "y": 40, "w": 36, "h": 14}],
        }]
        xml = """
          <w:p>
            <w:pPr><w:tabs><w:tab w:val="left" w:pos="4000"/></w:tabs></w:pPr>
            <w:r><w:t>姓名</w:t></w:r><w:r><w:tab/></w:r>
          </w:p>
        """
        marks = paragraph_enters(_enter_docx(xml, body=True), pages)[1]
        self.assertEqual([(mark["x"], mark["y"], mark["mark"]) for mark in marks], [(363, 40, "¶")])

    def test_a_manual_line_break_is_not_the_paragraph_mark(self):
        pages = [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [
                {"text": "甲乙", "x": 96, "y": 40, "w": 28, "h": 14},
                {"text": "丙丁", "x": 96, "y": 58, "w": 28, "h": 14},
            ],
        }]
        xml = """
          <w:p>
            <w:r><w:t>甲乙</w:t></w:r>
            <w:r><w:br/></w:r>
            <w:r><w:t>丙丁</w:t></w:r>
          </w:p>
        """
        marks = paragraph_enters(_enter_docx(xml, body=True), pages)[1]
        self.assertEqual([(mark["x"], mark["y"], mark["mark"]) for mark in marks], [(124, 40, "↵"), (124, 58, "¶")])

    def test_every_visible_line_gets_a_return_even_when_the_paragraph_text_is_missing(self):
        pages = [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [
                {"text": "姓名", "x": 96, "y": 40, "w": 36, "h": 14},
                {"text": "地址", "x": 96, "y": 58, "w": 36, "h": 14},
            ],
        }]
        marks = page_returns(b"not-a-docx", pages)[1]
        self.assertEqual([(mark["x"], mark["y"], mark["mark"]) for mark in marks], [(132, 40, "↵"), (132, 58, "↵")])

    def test_a_blank_line_and_each_side_of_a_wide_gap_get_a_return(self):
        pages = [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [
                {"text": "姓名", "x": 40, "y": 40, "w": 36, "h": 14},
                {"text": "地址", "x": 280, "y": 40, "w": 36, "h": 14},
                {"text": "電話", "x": 40, "y": 90, "w": 36, "h": 14},
            ],
        }]
        marks = page_returns(b"not-a-docx", pages)[1]
        placed = {(mark["x"], mark["y"]) for mark in marks}
        self.assertIn((76, 40), placed)
        self.assertIn((316, 40), placed)
        self.assertIn((76, 90), placed)
        self.assertTrue(any(mark["x"] == 40 and 54 < mark["y"] < 90 for mark in marks))
        self.assertTrue(all(mark["mark"] == "↵" for mark in marks))

    def test_words_in_one_phrase_share_one_return(self):
        pages = [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [
                {"text": "公司", "x": 40, "y": 40, "w": 28, "h": 14},
                {"text": "名稱", "x": 72, "y": 40, "w": 28, "h": 14},
            ],
        }]
        marks = page_returns(b"not-a-docx", pages)[1]
        self.assertEqual([(mark["x"], mark["y"]) for mark in marks], [(100, 40)])

    @unittest.skipUnless(shutil.which("soffice") and shutil.which("pdftotext"), "needs LibreOffice")
    def test_a_rendered_page_puts_the_paragraph_mark_after_the_word(self):
        from app.word_layout import layout_docx

        raw = _enter_docx("""
          <w:p>
            <w:pPr><w:tabs><w:tab w:val="left" w:pos="4000"/></w:tabs></w:pPr>
            <w:r><w:t>姓名</w:t></w:r><w:r><w:tab/></w:r>
          </w:p>
          <w:p/>
          <w:p><w:r><w:t>地址</w:t></w:r></w:p>
        """, body=True)
        page = layout_docx(raw)["pages"][0]
        name = next(word for word in page["words"] if "姓名" in word["text"])
        addr = next(word for word in page["words"] if "地址" in word["text"])
        enters = page["enters"]
        name_mark = [mark for mark in enters if abs(mark["y"] - name["y"]) <= 4]
        addr_mark = [mark for mark in enters if abs(mark["y"] - addr["y"]) <= 4]
        self.assertEqual(len(name_mark), 1)
        self.assertEqual(len(addr_mark), 1)
        self.assertGreater(name_mark[0]["x"], name["x"] + name["w"] + 40)
        self.assertLess(abs(addr_mark[0]["x"] - (addr["x"] + addr["w"])), 12)
        between = [mark for mark in enters if name["y"] < mark["y"] < addr["y"]]
        self.assertEqual(len(between), 1)
        self.assertLess(abs(between[0]["x"] - name["x"]), 8)

    def test_a_standalone_kou_can_be_ticked_and_a_kou_inside_a_word_cannot(self):
        ticks = collect_ticks([{
            "n": 1,
            "words": [
                {"text": "人口", "x": 10, "y": 10, "w": 28, "h": 14},
                {"text": "口同意", "x": 10, "y": 40, "w": 42, "h": 14},
                {"text": "口", "x": 80, "y": 40, "w": 14, "h": 14},
            ],
        }])[1]
        self.assertEqual([(tick["x"], tick["y"]) for tick in ticks], [(10, 40), (80, 40)])
        self.assertEqual(ticks[0]["w"], 14)

    def test_the_cell_under_a_company_name_is_a_clickable_field(self):
        raw = _company_docx()
        fields = collect_fields(preview_docx(raw))
        self.assertEqual([field["name"] for field in fields], ["CompanyName"])
        self.assertTrue(fields[0]["blank"])
        self.assertEqual(fields[0]["boxW"], 200)
        self.assertGreaterEqual(fields[0]["boxH"], 27)
        pages, loose = place_fields(fields, [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [{"text": "~{1}~________", "x": 96, "y": 160, "w": 120, "h": 16}],
        }])
        hit = pages[0]["fields"][0]
        self.assertEqual(hit["name"], "CompanyName")
        self.assertEqual(hit["x"], 96)
        self.assertEqual(hit["y"], 160)
        self.assertEqual(hit["w"], 120)
        self.assertEqual(hit["h"], 16)
        self.assertEqual(loose, [])
        fitted, _loose = place_fields(fields, [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [
                {"text": "~{1}~", "x": 100, "y": 200, "w": 8, "h": 4},
                {"text": "旁邊", "x": 220, "y": 200, "w": 28, "h": 14},
                {"text": "下一行", "x": 100, "y": 240, "w": 40, "h": 14},
            ],
        }])
        cell = fitted[0]["fields"][0]
        self.assertEqual(cell["x"], 100)
        self.assertEqual(cell["y"], 200)
        self.assertLess(cell["x"] + cell["w"], 220)
        self.assertLess(cell["y"] + cell["h"], 240)

    @unittest.skipUnless(shutil.which("soffice") and shutil.which("pdftotext"), "needs LibreOffice")
    def test_a_text_box_stays_where_the_layout_engine_drew_it(self):
        from app.word_layout import layout_docx

        raw = _docx()
        laid = layout_docx(raw)
        pages, _loose = place_fields(collect_fields(preview_docx(raw)), laid["pages"])
        pinned = {field["name"]: field for field in pages[0]["fields"]}
        self.assertIn("TopName", pinned)
        self.assertIn("BoxCode", pinned)
        self.assertGreater(pinned["BoxCode"]["y"], pinned["TopName"]["y"] + 150)
        self.assertGreater(pages[0]["width"], 700)
        self.assertLess(pages[0]["width"], 900)

    @unittest.skipUnless(shutil.which("soffice") and shutil.which("pdftotext"), "needs LibreOffice")
    def test_a_drawn_frame_keeps_a_return_inside_it(self):
        from app.word_layout import layout_docx

        page = layout_docx(_frame_docx())["pages"][0]
        enters = page["enters"]
        empty = [mark for mark in enters if 110 <= mark["x"] <= 160 and 260 <= mark["y"] <= 330]
        lower = [mark for mark in enters if 160 <= mark["x"] <= 230 and 350 <= mark["y"] <= 410]
        self.assertTrue(empty, [(mark["x"], mark["y"]) for mark in enters])
        self.assertTrue(lower, [(mark["x"], mark["y"]) for mark in enters])

    def test_a_stroked_outline_is_the_frame_box(self):
        from app.word_layout import _collect_frame_specs, _rects_from_svg

        svg = """<svg viewBox="0 0 595.3 841.89">
          <path stroke="#000" d="M 90 641.4 L 306 641.4"/>
          <path stroke="#000" d="M 305.5 641.9 L 305.5 571.9"/>
          <path stroke="#000" d="M 306 572.4 L 90 572.4"/>
          <path stroke="#000" d="M 90.5 571.9 L 90.5 641.9"/>
        </svg>"""
        self.assertEqual(_rects_from_svg(svg), [{"x": 120, "y": 267, "w": 288, "h": 92}])
        specs = sorted((spec["w"], spec["h"], spec["paras"]) for spec in _collect_frame_specs(_frame_docx()))
        self.assertEqual(specs, [(288, 60, 1), (288, 72, 2), (288, 93, 1)])

    def test_a_table_grid_is_split_into_cells(self):
        from app.word_layout import _rects_from_svg

        svg = """<svg viewBox="0 0 800 800">
          <path d="M 100 700 L 300 700"/>
          <path d="M 100 600 L 300 600"/>
          <path d="M 100 500 L 300 500"/>
          <path d="M 100 500 L 100 700"/>
          <path d="M 200 500 L 200 700"/>
          <path d="M 300 500 L 300 700"/>
        </svg>"""
        self.assertEqual(len(_rects_from_svg(svg)), 4)

    def test_an_empty_table_cell_gets_a_return_from_the_columns(self):
        from app.word_layout import page_returns

        pages = [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [
                {"text": "甲", "x": 100, "y": 40, "w": 14, "h": 14},
                {"text": "乙", "x": 220, "y": 70, "w": 14, "h": 14},
            ],
            "frames": [],
        }]
        marks = page_returns(_table_docx(), pages)[1]
        fields = [mark for mark in marks if mark.get("table")]
        self.assertTrue(any(mark["x"] == 116 and mark["y"] == 40 and mark["w"] >= 40 for mark in fields), [(mark["x"], mark["y"], mark["w"]) for mark in fields])
        self.assertTrue(any(mark["y"] == 40 and 210 <= mark["x"] <= 250 and mark["w"] >= 40 for mark in fields), [(mark["x"], mark["y"], mark["w"]) for mark in fields])
        self.assertTrue(any(abs(mark["y"] - 70) <= 4 and mark["x"] < 140 and mark["w"] >= 40 for mark in fields), [(mark["x"], mark["y"], mark["w"]) for mark in fields])

    def test_a_table_with_no_text_shows_a_return_under_the_heading(self):
        from app.word_layout import page_returns

        xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}">
  <w:body>
    <w:p><w:r><w:t>CompanyName</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblW w:w="6000" w:type="dxa"/></w:tblPr>
      <w:tblGrid><w:gridCol w:w="6000"/></w:tblGrid>
      <w:tr><w:trPr><w:trHeight w:val="400"/></w:trPr><w:tc><w:p/></w:tc></w:tr>
    </w:tbl>
    <w:p><w:r><w:t>Name</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblW w:w="6000" w:type="dxa"/></w:tblPr>
      <w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>
      <w:tr><w:tc><w:p/></w:tc><w:tc><w:p/></w:tc><w:tc><w:p/></w:tc></w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""
        pages = [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [
                {"text": "CompanyName", "x": 100, "y": 40, "w": 90, "h": 16},
                {"text": "Name", "x": 100, "y": 120, "w": 40, "h": 16},
            ],
            "frames": [],
        }]
        marks = [mark for mark in page_returns(_pack_docx(xml), pages)[1] if mark.get("table")]
        self.assertTrue(any(100 <= mark["x"] <= 130 and 56 <= mark["y"] <= 70 and mark["w"] >= 100 for mark in marks), [(mark["x"], mark["y"], mark["w"]) for mark in marks])
        name_row = [mark for mark in marks if 136 <= mark["y"] <= 150 and mark["w"] >= 40]
        self.assertGreaterEqual(len(name_row), 3, [(mark["x"], mark["y"]) for mark in marks])

    def test_a_content_control_in_a_cell_still_places_the_empty_cell(self):
        from app.word_layout import page_returns

        xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}">
  <w:body>
    <w:tbl>
      <w:tblGrid><w:gridCol w:w="2400"/><w:gridCol w:w="2400"/></w:tblGrid>
      <w:tr>
        <w:tc><w:sdt><w:sdtContent><w:p><w:r><w:t>甲</w:t></w:r></w:p></w:sdtContent></w:sdt></w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""
        pages = [{
            "n": 1,
            "width": 794,
            "height": 1123,
            "words": [{"text": "甲", "x": 80, "y": 50, "w": 14, "h": 14}],
            "frames": [],
        }]
        marks = page_returns(_pack_docx(xml), pages)[1]
        self.assertTrue(any(mark["y"] == 50 and mark["x"] > 120 for mark in marks), [(mark["x"], mark["y"]) for mark in marks])

    def test_a_blank_inside_a_cell_stops_at_the_cell_edge(self):
        from app.word_layout import clip_gaps

        clipped = clip_gaps(
            [{"x": 114, "y": 40, "w": 120, "h": 14, "id": "g"}],
            [{"x": 100, "y": 36, "w": 120, "h": 22}],
        )
        self.assertEqual(clipped, [])

    @unittest.skipUnless(shutil.which("soffice") and shutil.which("pdftotext"), "needs LibreOffice")
    def test_an_empty_table_under_a_heading_has_a_return_at_the_left(self):
        from app.word_layout import layout_docx

        page = layout_docx(_blank_field_tables_docx())["pages"][0]
        fields = [rect for rect in page.get("frames") or [] if rect["w"] >= 120 and 12 <= rect["h"] <= 80]
        self.assertGreaterEqual(len(fields), 4, [(rect["x"], rect["y"], rect["w"], rect["h"]) for rect in page.get("frames") or []])
        for rect in fields:
            at_left = [
                mark for mark in page["enters"]
                if mark.get("table")
                and mark["x"] == rect["x"]
                and mark["y"] == rect["y"]
                and mark["w"] == rect["w"]
                and mark["h"] == rect["h"]
            ]
            self.assertTrue(at_left, (rect, [(mark["x"], mark["y"]) for mark in page["enters"] if mark.get("cell")]))
        from app.word_layout import layout_docx

        page = layout_docx(_table_docx())["pages"][0]
        cells = [rect for rect in page["frames"] if rect["w"] >= 40 and rect["h"] >= 12]
        self.assertGreaterEqual(len(cells), 4, [(rect["x"], rect["y"], rect["w"], rect["h"]) for rect in page["frames"]])
        for cell in cells:
            inside = [
                mark for mark in page["enters"]
                if mark.get("table") and cell["x"] <= mark["x"] <= cell["x"] + cell["w"] - 8 and cell["y"] <= mark["y"] <= cell["y"] + cell["h"]
            ]
            self.assertTrue(inside, (cell, [(mark["x"], mark["y"], mark.get("cell")) for mark in page["enters"]]))

    @unittest.skipUnless(shutil.which("soffice") and shutil.which("pdftotext"), "needs LibreOffice")
    def test_a_blank_field_keeps_a_locator_the_engine_can_see(self):
        from app.word_layout import layout_docx

        laid = layout_docx(_blank_docx())
        text = "".join(word["text"] for page in laid["pages"] for word in page["words"])
        self.assertIn("~{2}~", text)
        self.assertIn("~{3}~", text)

    def test_an_underline_field_uses_the_line_ends(self):
        from app.word_layout import _fields_along_line

        marks = _fields_along_line(
            1,
            (222, 622, 138),
            {"h": 24, "cells": [{"span": 1, "skip": False}, {"span": 1, "skip": False}, {"span": 1, "skip": False}]},
            [2000, 2000, 2000],
            {"bottom": 114, "h": 18},
            [],
        )
        self.assertEqual([mark["x"] for mark in marks], [222, 355, 489])
        self.assertTrue(all(mark["y"] + mark["h"] == 138 for mark in marks))
        self.assertLessEqual(marks[-1]["x"] + marks[-1]["w"], 622)

    @unittest.skipUnless(shutil.which("soffice") and shutil.which("pdftotext"), "needs LibreOffice")
    def test_an_underline_table_sits_on_the_drawn_line(self):
        from app.word_layout import layout_docx

        page = layout_docx(_underline_tables_docx())["pages"][0]
        tables = [mark for mark in page["enters"] if mark.get("table")]
        wide = [mark for mark in tables if mark["w"] >= 400]
        self.assertTrue(wide, [(mark["x"], mark["y"], mark["w"], mark["h"]) for mark in tables])
        self.assertLess(wide[0]["x"], 120)
        self.assertGreater(wide[0]["x"] + wide[0]["w"], 640)
        self.assertGreater(wide[0]["y"] + wide[0]["h"], 130)
        self.assertLess(wide[0]["y"] + wide[0]["h"], 155)
        cells = [mark for mark in tables if mark["w"] < 400]
        self.assertGreaterEqual(len(cells), 3, [(mark["x"], mark["y"], mark["w"]) for mark in tables])
        self.assertGreaterEqual(min(mark["x"] for mark in cells), 180)

    def test_neighbouring_cells_are_not_the_same_field(self):
        from app.word_layout import _covers_same_area

        left = {"x": 41, "y": 67, "w": 300, "h": 26}
        right = {"x": 340, "y": 67, "w": 300, "h": 26}
        same = {"x": 41, "y": 67, "w": 300, "h": 26}
        self.assertFalse(_covers_same_area(left, right))
        self.assertTrue(_covers_same_area(left, same))

    @unittest.skipUnless(shutil.which("soffice") and shutil.which("pdftotext"), "needs LibreOffice")
    def test_every_column_and_a_borderless_table_is_shown(self):
        from app.word_layout import layout_docx

        page = layout_docx(_missed_tables_docx())["pages"][0]
        tables = [mark for mark in page["enters"] if mark.get("table")]
        shown = [(mark["x"], mark["y"], mark["w"], mark["h"]) for mark in tables]
        side_by_side = any(
            abs(left["y"] - right["y"]) <= 8 and right["x"] >= left["x"] + left["w"] - 8 and left["w"] >= 180 and right["w"] >= 180
            for left in tables for right in tables
        )
        self.assertTrue(side_by_side, shown)
        self.assertGreaterEqual(len(tables), 4, shown)
        self.assertTrue(any(mark["y"] > 280 and mark["x"] > 140 for mark in tables), shown)


BLANK = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}">
  <w:body>
    <w:p>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> MERGEFIELD TopName </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>頁首公司</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
    </w:p>
    <w:sdt>
      <w:sdtPr><w:tag w:val="EmptyCo"/></w:sdtPr>
      <w:sdtContent><w:p/></w:sdtContent>
    </w:sdt>
    <w:p>
      <w:r><w:t>姓名</w:t></w:r>
      <w:r><w:rPr><w:u w:val="single"/></w:rPr><w:tab/></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>編號</w:t></w:r>
      <w:r><w:t>________</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


COMPANY = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}">
  <w:body>
    <w:tbl>
      <w:tblPr>
        <w:tblBorders>
          <w:top w:val="single"/><w:left w:val="single"/><w:bottom w:val="single"/><w:right w:val="single"/>
        </w:tblBorders>
      </w:tblPr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>公司名稱 CompanyName</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:trPr><w:trHeight w:val="600"/></w:trPr>
        <w:tc>
          <w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>
          <w:p/>
        </w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""


def _company_docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", COMPANY)
    return buffer.getvalue()


def _frame_docx() -> bytes:
    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:wps="{WPS}">
  <w:body>
    <w:p><w:r><w:t>BODY</w:t></w:r></w:p>
    <w:p>
      <w:pPr>
        <w:pBdr>
          <w:top w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:left w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:bottom w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:right w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        </w:pBdr>
        <w:framePr w:w="4320" w:h="900" w:hRule="exact" w:wrap="around" w:vAnchor="page" w:hAnchor="page" w:x="1800" w:y="2200"/>
      </w:pPr>
      <w:r><w:t>FRAMED</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr>
        <w:pBdr>
          <w:top w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:left w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:bottom w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:right w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        </w:pBdr>
        <w:framePr w:w="4320" w:h="1400" w:hRule="exact" w:wrap="around" w:vAnchor="page" w:hAnchor="page" w:x="1800" w:y="4000"/>
      </w:pPr>
    </w:p>
    <w:p>
      <w:r><w:drawing>
        <wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="1" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">
          <wp:simplePos x="0" y="0"/>
          <wp:positionH relativeFrom="page"><wp:posOffset>1600200</wp:posOffset></wp:positionH>
          <wp:positionV relativeFrom="page"><wp:posOffset>3200400</wp:posOffset></wp:positionV>
          <wp:extent cx="2743200" cy="685800"/>
          <wp:effectExtent l="0" t="0" r="0" b="0"/>
          <wp:wrapNone/>
          <wp:docPr id="2" name="Box"/>
          <wp:cNvGraphicFramePr/>
          <a:graphic>
            <a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
              <wps:wsp>
                <wps:cNvSpPr txBox="1"/>
                <wps:spPr>
                  <a:xfrm><a:off x="0" y="0"/><a:ext cx="2743200" cy="685800"/></a:xfrm>
                  <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
                  <a:ln w="12700"><a:solidFill><a:srgbClr val="000000"/></a:solidFill></a:ln>
                </wps:spPr>
                <wps:txbx><w:txbxContent>
                  <w:p><w:r><w:t>BOXED</w:t></w:r></w:p>
                  <w:p/>
                </w:txbxContent></wps:txbx>
                <wps:bodyPr wrap="square" lIns="36000" tIns="36000" rIns="36000" bIns="36000"/>
              </wps:wsp>
            </a:graphicData>
          </a:graphic>
        </wp:anchor>
      </w:drawing></w:r>
    </w:p>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    return _pack_docx(xml)


def _enter_docx(text: str, body: bool = False) -> bytes:
    inner = text if body else f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}">
  <w:body>
    {inner}
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    return _pack_docx(xml)


def _table_in_box_docx() -> bytes:
    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:wps="{WPS}">
  <w:body>
    <w:p><w:r><w:drawing>
      <wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="1" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">
        <wp:simplePos x="0" y="0"/>
        <wp:positionH relativeFrom="page"><wp:posOffset>1371600</wp:posOffset></wp:positionH>
        <wp:positionV relativeFrom="page"><wp:posOffset>1828800</wp:posOffset></wp:positionV>
        <wp:extent cx="4572000" cy="1828800"/>
        <wp:effectExtent l="0" t="0" r="0" b="0"/>
        <wp:wrapNone/>
        <wp:docPr id="7" name="Box"/>
        <wp:cNvGraphicFramePr/>
        <a:graphic>
          <a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
            <wps:wsp>
              <wps:cNvSpPr txBox="1"/>
              <wps:spPr>
                <a:xfrm><a:off x="0" y="0"/><a:ext cx="4572000" cy="1828800"/></a:xfrm>
                <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
                <a:ln w="12700"><a:solidFill><a:srgbClr val="000000"/></a:solidFill></a:ln>
              </wps:spPr>
              <wps:txbx><w:txbxContent>
                <w:tbl>
                  <w:tblPr>
                    <w:tblW w:w="6000" w:type="dxa"/>
                    <w:tblBorders>
                      <w:top w:val="single" w:sz="8" w:space="0" w:color="auto"/>
                      <w:left w:val="single" w:sz="8" w:space="0" w:color="auto"/>
                      <w:bottom w:val="single" w:sz="8" w:space="0" w:color="auto"/>
                      <w:right w:val="single" w:sz="8" w:space="0" w:color="auto"/>
                      <w:insideH w:val="single" w:sz="8" w:space="0" w:color="auto"/>
                      <w:insideV w:val="single" w:sz="8" w:space="0" w:color="auto"/>
                    </w:tblBorders>
                  </w:tblPr>
                  <w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid>
                  <w:tr>
                    <w:tc><w:p><w:r><w:t>甲</w:t></w:r></w:p></w:tc>
                    <w:tc><w:p/></w:tc>
                  </w:tr>
                  <w:tr>
                    <w:tc><w:p/></w:tc>
                    <w:tc><w:p><w:r><w:t>乙</w:t></w:r></w:p></w:tc>
                  </w:tr>
                </w:tbl>
              </w:txbxContent></wps:txbx>
              <wps:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0"/>
            </wps:wsp>
          </a:graphicData>
        </a:graphic>
      </wp:anchor>
    </w:drawing></w:r></w:p>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    return _pack_docx(xml)


def _missed_tables_docx() -> bytes:
    bottom = """
      <w:tblBorders>
        <w:top w:val="nil"/>
        <w:left w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        <w:bottom w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        <w:right w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        <w:insideH w:val="nil"/>
        <w:insideV w:val="single" w:sz="8" w:space="0" w:color="auto"/>
      </w:tblBorders>
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}">
  <w:body>
    <w:p><w:r><w:t>Cols</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblW w:w="8000" w:type="dxa"/>{bottom}</w:tblPr>
      <w:tblGrid><w:gridCol w:w="4000"/><w:gridCol w:w="4000"/></w:tblGrid>
      <w:tr><w:trPr><w:trHeight w:val="400"/></w:trPr><w:tc><w:p/></w:tc><w:tc><w:p/></w:tc></w:tr>
    </w:tbl>
    <w:p><w:r><w:t>Plain</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblW w:w="5000" w:type="dxa"/></w:tblPr>
      <w:tblGrid><w:gridCol w:w="5000"/></w:tblGrid>
      <w:tr><w:trPr><w:trHeight w:val="400"/></w:trPr><w:tc><w:p/></w:tc></w:tr>
    </w:tbl>
    <w:p><w:r><w:t>Float</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr>
        <w:tblpPr w:horzAnchor="page" w:vertAnchor="page" w:tblpX="3200" w:tblpY="5600"/>
        <w:tblW w:w="4200" w:type="dxa"/>
        <w:tblBorders>
          <w:top w:val="nil"/><w:left w:val="nil"/><w:bottom w:val="single" w:sz="12" w:space="0" w:color="auto"/><w:right w:val="nil"/>
          <w:insideH w:val="nil"/><w:insideV w:val="nil"/>
        </w:tblBorders>
      </w:tblPr>
      <w:tblGrid><w:gridCol w:w="4200"/></w:tblGrid>
      <w:tr><w:trPr><w:trHeight w:val="400"/></w:trPr><w:tc><w:p/></w:tc></w:tr>
    </w:tbl>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    return _pack_docx(xml)


def _underline_tables_docx() -> bytes:
    bottom = """
      <w:tblBorders>
        <w:top w:val="nil"/>
        <w:left w:val="nil"/>
        <w:bottom w:val="single" w:sz="12" w:space="0" w:color="auto"/>
        <w:right w:val="nil"/>
        <w:insideH w:val="nil"/>
        <w:insideV w:val="nil"/>
      </w:tblBorders>
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}">
  <w:body>
    <w:p><w:r><w:t>1 Company Name</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblW w:w="9000" w:type="dxa"/>{bottom}</w:tblPr>
      <w:tblGrid><w:gridCol w:w="9000"/></w:tblGrid>
      <w:tr><w:trPr><w:trHeight w:val="400"/></w:trPr><w:tc><w:p/></w:tc></w:tr>
    </w:tbl>
    <w:p><w:r><w:t>Name</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblW w:w="6000" w:type="dxa"/><w:tblInd w:w="2200" w:type="dxa"/>{bottom}</w:tblPr>
      <w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>
      <w:tr><w:trPr><w:trHeight w:val="360"/></w:trPr>
        <w:tc><w:p/></w:tc><w:tc><w:p/></w:tc><w:tc><w:p/></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    return _pack_docx(xml)


def _blank_field_tables_docx() -> bytes:
    borders = """
      <w:tblBorders>
        <w:top w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        <w:left w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        <w:bottom w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        <w:right w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        <w:insideH w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        <w:insideV w:val="single" w:sz="8" w:space="0" w:color="auto"/>
      </w:tblBorders>
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}">
  <w:body>
    <w:p><w:r><w:t>1 Company Name</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblW w:w="9000" w:type="dxa"/>{borders}</w:tblPr>
      <w:tblGrid><w:gridCol w:w="9000"/></w:tblGrid>
      <w:tr><w:trPr><w:trHeight w:val="400"/></w:trPr><w:tc><w:p/></w:tc></w:tr>
    </w:tbl>
    <w:p><w:r><w:t>Name</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblW w:w="9000" w:type="dxa"/>{borders}</w:tblPr>
      <w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid>
      <w:tr>
        <w:trPr><w:trHeight w:val="360"/></w:trPr>
        <w:tc><w:p/></w:tc>
        <w:tc><w:p/></w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    return _pack_docx(xml)


def _table_docx() -> bytes:
    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}">
  <w:body>
    <w:tbl>
      <w:tblPr>
        <w:tblW w:w="5000" w:type="dxa"/>
        <w:tblBorders>
          <w:top w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:left w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:bottom w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:right w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:insideH w:val="single" w:sz="8" w:space="0" w:color="auto"/>
          <w:insideV w:val="single" w:sz="8" w:space="0" w:color="auto"/>
        </w:tblBorders>
      </w:tblPr>
      <w:tblGrid>
        <w:gridCol w:w="2500"/>
        <w:gridCol w:w="2500"/>
      </w:tblGrid>
      <w:tr>
        <w:tc><w:p><w:r><w:t>甲</w:t></w:r></w:p></w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
      <w:tr>
        <w:trPr><w:trHeight w:val="500"/></w:trPr>
        <w:tc><w:p/></w:tc>
        <w:tc><w:p><w:r><w:t>乙</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    return _pack_docx(xml)


def _pack_docx(xml: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""")
        archive.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""")
        archive.writestr("word/_rels/document.xml.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>""")
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _blank_docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""")
        archive.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""")
        archive.writestr("word/_rels/document.xml.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>""")
        archive.writestr("word/document.xml", BLANK)
    return buffer.getvalue()
