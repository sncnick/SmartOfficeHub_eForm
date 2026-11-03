import io
import unittest
import zipfile

from app.word_preview import preview_docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}">
  <w:body>
    <w:p>
      <w:pPr><w:jc w:val="center"/></w:pPr>
      <w:r><w:rPr><w:b/><w:sz w:val="28"/><w:rFonts w:eastAsia="PMingLiU"/></w:rPr><w:t>公司 </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText> MERGEFIELD CompanyName </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>示範公司</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
    </w:p>
    <w:sdt>
      <w:sdtPr><w:tag w:val="company.name_en"/></w:sdtPr>
      <w:sdtContent><w:p><w:r><w:t>Acme</w:t></w:r></w:p></w:sdtContent>
    </w:sdt>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p>
            <w:fldSimple w:instr=" DOCVARIABLE SignDate ">
              <w:r><w:t>2026-01-01</w:t></w:r>
            </w:fldSimple>
          </w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
    <w:p>
      <w:pPr><w:framePr w:w="1440" w:h="400" w:x="2880" w:y="1440" w:hAnchor="page" w:vAnchor="page"/></w:pPr>
      <w:r><w:t>定位</w:t></w:r>
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
        archive.writestr("word/document.xml", XML)
    return buffer.getvalue()


class WordPreviewTests(unittest.TestCase):
    def test_fields_keep_shown_text_and_parameter_name(self):
        preview = preview_docx(_docx())
        self.assertEqual(preview["page_count"], 1)
        paragraph = preview["pages"][0]["blocks"][0]
        self.assertEqual(paragraph["items"][0]["text"], "公司 ")
        self.assertEqual(paragraph["align"], "center")
        self.assertTrue(paragraph["items"][0]["style"]["bold"])
        self.assertEqual(paragraph["items"][0]["style"]["size"], 14)
        self.assertEqual(paragraph["items"][0]["style"]["font"], "PMingLiU")
        self.assertEqual(preview["pages"][0]["setup"]["width"], 794)
        field = paragraph["items"][1]
        self.assertEqual(field["name"], "CompanyName")
        self.assertEqual(field["text"], "示範公司")
        control = preview["pages"][0]["blocks"][1]["items"][0]
        self.assertEqual(control["name"], "company.name_en")
        self.assertEqual(control["text"], "Acme")
        date = preview["pages"][0]["blocks"][2]["rows"][0][0]["blocks"][0]["items"][0]
        self.assertEqual(date["name"], "SignDate")
        self.assertEqual(date["text"], "2026-01-01")
        frame = preview["pages"][0]["blocks"][3]
        self.assertEqual(frame["type"], "frame")
        self.assertEqual(frame["x"], 192)
        self.assertEqual(frame["y"], 96)
        self.assertEqual(frame["anchorH"], "page")
        self.assertEqual(frame["anchorV"], "page")
        self.assertEqual(frame["blocks"][0]["items"][0]["text"], "定位")

    def test_text_boxes_use_their_own_origin_and_skip_the_fallback_copy(self):
        preview = preview_docx(_anchored_docx())
        blocks = preview["pages"][0]["blocks"]
        drawing = next(block for block in blocks if block.get("type") == "frame" and _frame_text(block) == "繪圖")
        self.assertEqual(drawing["x"], 96)
        self.assertEqual(drawing["y"], 48)
        self.assertEqual(drawing["anchorH"], "page")
        self.assertEqual(drawing["anchorV"], "page")
        self.assertFalse(any(_frame_text(block) == "舊圖" for block in blocks))
        vml = next(block for block in blocks if _frame_text(block) == "純文字框")
        self.assertEqual(vml["x"], 96)
        self.assertEqual(vml["y"], 48)
        self.assertEqual(vml["anchorH"], "margin")
        self.assertEqual(vml["anchorV"], "page")
        table = next(block for block in blocks if block.get("type") == "table")
        self.assertEqual(table["indent"], -24)


def _frame_text(block: dict) -> str:
    if block.get("type") != "frame":
        return ""
    parts = []
    for child in block.get("blocks") or []:
        for item in child.get("items") or []:
            parts.append(item.get("text") or "")
    return "".join(parts)


WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
VML = "urn:schemas-microsoft-com:vml"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
ANCHORED = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}" xmlns:wp="{WP}" xmlns:v="{VML}" xmlns:mc="{MC}">
  <w:body>
    <w:p>
      <mc:AlternateContent>
        <mc:Choice>
          <w:r><w:drawing>
            <wp:anchor>
              <wp:positionH relativeFrom="page"><wp:posOffset>914400</wp:posOffset></wp:positionH>
              <wp:positionV relativeFrom="page"><wp:posOffset>457200</wp:posOffset></wp:positionV>
              <wp:extent cx="1828800" cy="365760"/>
              <w:txbxContent><w:p><w:r><w:t>繪圖</w:t></w:r></w:p></w:txbxContent>
            </wp:anchor>
          </w:drawing></w:r>
        </mc:Choice>
        <mc:Fallback>
          <w:pict><v:shape style="position:absolute;margin-left:10pt;margin-top:10pt;width:100pt;height:20pt;mso-position-horizontal-relative:page;mso-position-vertical-relative:page">
            <v:textbox><w:txbxContent><w:p><w:r><w:t>舊圖</w:t></w:r></w:p></w:txbxContent></v:textbox>
          </v:shape></w:pict>
        </mc:Fallback>
      </mc:AlternateContent>
    </w:p>
    <w:p>
      <w:r><w:pict>
        <v:shape style="position:absolute;margin-left:72pt;margin-top:36pt;width:144pt;height:28pt;mso-position-horizontal-relative:margin;mso-position-vertical-relative:page">
          <v:textbox><w:txbxContent><w:p><w:r><w:t>純文字框</w:t></w:r></w:p></w:txbxContent></v:textbox>
        </v:shape>
      </w:pict></w:r>
    </w:p>
    <w:tbl>
      <w:tblPr><w:tblInd w:w="-360" w:type="dxa"/></w:tblPr>
      <w:tr><w:tc><w:p><w:r><w:t>表</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""


def _anchored_docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", ANCHORED)
    return buffer.getvalue()
