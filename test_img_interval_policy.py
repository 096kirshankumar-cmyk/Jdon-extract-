#!/usr/bin/env python3
"""Image ownership policy: no cap, [IMG] count reconcile, boundary-tie."""
import unittest
import header_index as hi
import qbank_pipeline as qp


class ImgPlaceholderTests(unittest.TestCase):
    def test_count_match(self):
        ok, note = qp.reconcile_img_placeholders("see [IMG] then [IMG]", 2)
        self.assertTrue(ok)
        self.assertEqual(note, "")

    def test_mismatch_flags_no_rewrite(self):
        text = "only one [IMG] here"
        ok, note = qp.reconcile_img_placeholders(text, 3)
        self.assertFalse(ok)
        self.assertIn("mismatch", note)
        self.assertEqual(text, "only one [IMG] here")

    def test_high_count_flags_does_not_drop(self):
        recs = {i: {"_review_reasons": []} for i in range(1, 6)}
        files = {1: {"question": [], "solution": ["a"] * 6},
                 2: {"question": [], "solution": ["x"]},
                 3: {"question": [], "solution": []},
                 4: {"question": [], "solution": []},
                 5: {"question": [], "solution": []}}
        qp.flag_high_image_counts(recs, files)
        self.assertTrue(any("high_image_count" in r
                            for r in recs[1]["_review_reasons"]))
        self.assertEqual(len(files[1]["solution"]), 6)


class BoundaryTieTests(unittest.TestCase):
    def test_empty_plus_declared_wins(self):
        recs = [
            {"page": 1, "y": 700, "type": hi.T_SOLUTION, "n": 1},
            {"page": 1, "y": 400, "type": hi.T_SOLUTION, "n": 2},
        ]
        chapter = {
            1: {"has_figure_in_solution": False},
            2: {"has_figure_in_solution": True},
        }
        files = {1: {"solution": ["already"]}, 2: {"solution": []}}
        # Y near header of S2
        owner = hi.boundary_tie_owner(recs, 1, 405, chapter, files)
        self.assertEqual(owner, ("solution", 2))

    def test_still_ambiguous_returns_none(self):
        recs = [
            {"page": 1, "y": 700, "type": hi.T_SOLUTION, "n": 1},
            {"page": 1, "y": 400, "type": hi.T_SOLUTION, "n": 2},
        ]
        chapter = {
            1: {"has_figure_in_solution": True},
            2: {"has_figure_in_solution": True},
        }
        files = {1: {"solution": []}, 2: {"solution": []}}
        owner = hi.boundary_tie_owner(recs, 1, 405, chapter, files)
        self.assertIsNone(owner)


if __name__ == "__main__":
    unittest.main()
