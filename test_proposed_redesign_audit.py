#!/usr/bin/env python3
"""Forensic evaluation of the external "vision-first semantic extraction"
redesign (proposed_redesign/), run in ISOLATION so production is untouched.

The audit's DIAGNOSIS (fragile regex/crops, page-zone assumptions) is partly
right. Its PRESCRIPTION -- one vision call per page, sticky `current_q_no`
assembly, and a 5-check verifier -- is a REGRESSION against this project's
core rule ("zero wrong data"). Each test below replays a REAL defect this
session fixed in production and shows the proposed design reproduces it
SILENTLY (no flag), i.e. it would ship wrong data.

These tests PASS by asserting the bad outcome EXISTS in the proposal. They are
the evidence for NOT replacing the production pipeline.

Run:  python3 test_proposed_redesign_audit.py
"""
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from proposed_redesign.assembler import Assembler          # noqa: E402
from proposed_redesign.verifier import Verifier            # noqa: E402


def _page(no, blocks, images=None, ptype="QUESTIONS"):
    return ({"page_no": no, "full_text": "", "raw_images": images or []},
            {"page_type": ptype, "blocks": blocks})


class TestProposedReintroducesPhantomQno(unittest.TestCase):
    """OPH-011 live: a page number read as 'Question 134' became a real row.
    Production quarantines it via the answer-key census (RUN-50). The proposed
    sticky assembler has no census -> 134 ships."""

    def test_page_number_becomes_a_question(self):
        a = Assembler("OPH", 11)
        pd, ex = _page(267, [
            {"block_type": "stem", "q_no": 23, "text": "cataract question",
             "has_figure": False},
            # the model reads the printed page number '267' / a footer as q134
            {"block_type": "stem", "q_no": 134, "text": "stray footer text",
             "has_figure": False},
        ])
        a.process_page_blocks(pd, ex)
        q, _, _, _ = a.build_records()
        self.assertIn(134, [r["q_no"] for r in q],
                      "proposed design ships the phantom page-number question")


class TestProposedMisattributesFigures(unittest.TestCase):
    """Production's hardest-won feature is per-image ownership with evidence.
    The proposed assembler dumps EVERY page image onto the last active q_no,
    so q22's figure lands on q23 -- exactly the wrong-owner class we gate."""

    def test_all_page_images_go_to_last_question(self):
        a = Assembler("OPH", 2)
        img22 = {"file": "p10_a.webp", "source_pages": [10], "extraction_page": 10}
        img23 = {"file": "p10_b.webp", "source_pages": [10], "extraction_page": 10}
        pd, ex = _page(10, [
            {"block_type": "stem", "q_no": 22, "text": "q22 stem",
             "has_figure": True},
            {"block_type": "stem", "q_no": 23, "text": "q23 stem",
             "has_figure": True},
        ], images=[img22, img23])
        a.process_page_blocks(pd, ex)
        _, _, _, man = a.build_records()
        by_q = {}
        for m in man:
            by_q.setdefault(m["q_id"], []).append(m["file"])
        q22 = by_q.get("OPH-002-022", [])
        q23 = by_q.get("OPH-002-023", [])
        # q22's figure was stolen by q23 (the last active q_no)
        self.assertEqual(q22, [], "q22 lost its own figure")
        self.assertIn(img22["file"], q23, "q23 wrongly owns q22's figure")


class TestProposedVerifierShipsWrongData(unittest.TestCase):
    """The 5-check verifier only looks at presence, never correctness. Real
    wrong-answer and bleed rows pass with ZERO flags."""

    def _rows(self, sol):
        q = [{"q_id": "OPH-001-007", "question_text": "stem about schlemm",
              "options": [{"id": L, "text": "opt"} for L in "ABCD"]}]
        a = [{"q_id": "OPH-001-007", "correct_option": "B"}]
        s = [{"q_id": "OPH-001-007", "solution_text": sol}]
        return q, a, s

    def test_wrong_answer_letter_not_flagged(self):
        # model misread the key: says B, printed key + solution say D.
        q, a, s = self._rows("The correct finding is option D, orbital.")
        self.assertEqual(Verifier.verify_and_flag(q, a, s), [],
                         "wrong answer ships with no flag (no key cross-check)")

    def test_solution_bleed_not_flagged(self):
        # solution carries the PREVIOUS question's explanation tail.
        q, a, s = self._rows("Some prior question's explanation text here. "
                             "The answer is B because schlemm.")
        self.assertEqual(Verifier.verify_and_flag(q, a, s), [],
                         "foreign-solution bleed ships with no flag")


if __name__ == "__main__":
    unittest.main(verbosity=2)
