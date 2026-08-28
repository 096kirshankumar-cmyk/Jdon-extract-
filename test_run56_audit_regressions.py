#!/usr/bin/env python3
"""RUN-56 regressions for the external audit's verified findings.

F1: merge_dual_key_reads -- one Gemini read + OCR third read agreeing is a
    deterministic claim (key_single_gemini_ocr), not key_conflict.
F3: _export_gate_violations -- >4 options now flagged (FORMAT.md says A-D).
F4: key_conflict overlay no longer erases a present answer (logic asserted
    inline in boundary_phased.run; here we assert the merge side that feeds it).

Run:  python3 test_run56_audit_regressions.py
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import header_index as hi          # noqa: E402
import qbank_pipeline as qp        # noqa: E402


class TestKeySingleReadPlusOcr(unittest.TestCase):
    def test_single_read_plus_ocr_agrees_yields_letter(self):
        r = hi.merge_dual_key_reads({5: "B", 6: "A"}, {6: "A"},
                                    third={5: "B", 6: "A"})
        self.assertEqual(r[5]["letter"], "B")
        self.assertTrue(r[5]["agree"])
        self.assertEqual(r[5]["method"], "key_single_gemini_ocr")
        # both reads present + agree stays dual
        self.assertEqual(r[6]["method"], "key_dual_gemini")

    def test_single_read_no_ocr_stays_conflict(self):
        r = hi.merge_dual_key_reads({5: "B"}, {}, third={})
        self.assertIsNone(r[5]["letter"])
        self.assertEqual(r[5]["method"], "key_conflict")

    def test_single_read_ocr_disagrees_stays_conflict(self):
        r = hi.merge_dual_key_reads({5: "B"}, {}, third={5: "C"})
        self.assertIsNone(r[5]["letter"])
        self.assertEqual(r[5]["method"], "key_conflict")

    def test_two_disagreeing_reads_stay_conflict_even_if_ocr_sides_with_one(self):
        # OCR confirms an ABSENT read; it never breaks a tie between two
        # present disagreeing Gemini reads.
        r = hi.merge_dual_key_reads({2: "B"}, {2: "D"}, third={2: "B"})
        self.assertIsNone(r[2]["letter"])
        self.assertEqual(r[2]["method"], "key_conflict")


class TestGateCatchesExtraOptions(unittest.TestCase):
    def _recs(self, opts):
        return {1: {"question_text": "stem text here",
                    "correct_option": "A",
                    "solution_text": "a sufficiently long explanation.",
                    "options": opts}}

    def test_five_options_flagged(self):
        v = qp._export_gate_violations(
            self._recs({"A": "a", "B": "b", "C": "c", "D": "d", "E": "e"}),
            {}, [], "TST-001")
        self.assertIn("bad_options", [k for k, _, _ in v])

    def test_four_options_not_flagged_for_count(self):
        v = qp._export_gate_violations(
            self._recs({"A": "a", "B": "b", "C": "c", "D": "d"}),
            {}, [], "TST-001")
        self.assertNotIn("bad_options", [k for k, _, _ in v])


if __name__ == "__main__":
    unittest.main(verbosity=2)
