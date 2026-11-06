import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.form_library import delete_form, forms_dir, list_forms, save_form


class FormLibraryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        os.environ["CDD_FORMS_DIR"] = self.folder.name

    def tearDown(self):
        os.environ.pop("CDD_FORMS_DIR", None)
        self.folder.cleanup()

    def test_upload_lists_and_delete_stays_inside_the_library(self):
        saved = save_form(r"..\outside.pdf", b"%PDF-1.4\n")
        self.assertEqual(saved.parent, Path(self.folder.name).resolve())
        self.assertEqual(saved.name, "outside.pdf")
        self.assertEqual([path.name for path in list_forms()], ["outside.pdf"])
        self.assertEqual(delete_form("outside.pdf").name, "outside.pdf")
        self.assertEqual(list_forms(), [])

    def test_a_second_upload_of_the_same_name_is_refused(self):
        save_form("form.pdf", b"%PDF-1.4\n")
        with self.assertRaises(HTTPException) as caught:
            save_form("form.pdf", b"%PDF-1.4\n")
        self.assertEqual(caught.exception.status_code, 409)

    def test_a_pdf_name_with_other_bytes_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            save_form("notes.pdf", b"hello")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(list(forms_dir().glob("notes.pdf")), [])


if __name__ == "__main__":
    unittest.main()
