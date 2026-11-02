import os
import unittest
from unittest.mock import patch

from app.lmstudio import (
    LmStudioError, accepted_marks, answer_field_marks, cells_for_model, lmstudio_base, sample_value_marks,
)


def _grid():
    return {
        "sheet": "IRF",
        "columns": ["A", "B"],
        "rows": [
            {"cells": ["[[…]] 已是勾選", "請填寫"], "borders": [None, None]},
            {"cells": ["是否同意", ""], "borders": [None, {"t": "1px"}]},
            {"cells": ["公司名稱", "____"], "borders": [None, None]},
        ],
        "page": {"print_area": []},
    }


class LmStudioTests(unittest.TestCase):
    def test_only_this_computer_can_be_used(self):
        with patch("app.lmstudio._in_docker", return_value=False):
            with patch.dict(os.environ, {"CDD_LMSTUDIO_URL": "http://127.0.0.1:1234/v1"}):
                self.assertEqual(lmstudio_base(), "http://127.0.0.1:1234/v1")
            self.assertEqual(lmstudio_base("http://127.0.0.1:1234/v1"), "http://127.0.0.1:1234/v1")
        with patch.dict(os.environ, {"CDD_LMSTUDIO_URL": "https://api.example.com/v1"}):
            with self.assertRaises(LmStudioError):
                lmstudio_base()

    def test_docker_reaches_the_host_instead_of_itself(self):
        with patch("app.lmstudio._in_docker", return_value=True):
            self.assertEqual(
                lmstudio_base("http://127.0.0.1:1234/v1"),
                "http://host.docker.internal:1234/v1",
            )

    def test_outline_skips_cells_that_are_already_marks(self):
        cells, _lookup = cells_for_model(_grid())
        by_cell = {item["cell"]: item for item in cells}
        self.assertEqual(by_cell["A1"]["already"], "tick")
        self.assertEqual(by_cell["B2"]["text"], "（空白）")
        self.assertTrue(by_cell["B2"]["blank"])
        self.assertIn("B3", by_cell)

    def test_filled_example_can_become_a_text_field(self):
        grid = _grid()
        grid["columns"] = ["D", "E"]
        grid["rows"] = [{
            "cells": ["Location(s) 業務地區：", "Hong Kong"],
            "borders": [None, None],
        }]
        grid["merges"] = [{"r": 1, "c": 2, "rs": 1, "cs": 1}]
        cells, lookup = cells_for_model(grid)
        marks = accepted_marks({
            "marks": [
                {"cell": "D1", "kind": "text"},
                {"cell": "E1", "kind": "text"},
            ],
        }, cells, lookup)
        self.assertEqual(marks, [{"cell": "E1", "row": 1, "col": "E", "kind": "text"}])
        self.assertEqual(sample_value_marks(cells), [{"cell": "E1", "row": 1, "col": "E", "kind": "text"}])

    def test_empty_boxed_answer_next_to_a_label_is_a_text_field(self):
        box = {"l": "1px", "r": "1px", "t": "1px", "b": "1px"}
        grid = {
            "sheet": "IRF",
            "columns": ["D", "E"],
            "rows": [{
                "cells": ["Location(s) 來往地區：", ""],
                "borders": [{"r": "1px", "b": "1px"}, box],
                "fields": [None, None],
            }],
            "page": {"print_area": []},
        }
        cells, _lookup = cells_for_model(grid)
        self.assertEqual(
            [item["cell"] for item in answer_field_marks(cells)],
            ["E1"],
        )

    def test_language_notes_are_not_fields_and_cell_formulas_are(self):
        box = {"l": "1px", "r": "1px", "t": "1px", "b": "1px"}
        grid = {
            "sheet": "IRF",
            "columns": ["B", "C"],
            "rows": [{
                "cells": ["英文", "=C19"],
                "borders": [box, box],
                "fields": [None, None],
            }],
            "page": {"print_area": []},
        }
        cells, _lookup = cells_for_model(grid)
        self.assertEqual([item["cell"] for item in answer_field_marks(cells)], ["C1"])
