#!/usr/bin/env python3
"""RUN-37 regressions: a carry claim corroborated by block-interval geometry.

OPH-001 produced 6 of 23 REVIEW_NEEDED rows from carry claims that were
actually correct -- the book prints the figure ABOVE its question's stem,
with the "Question N:" header at the bottom of the previous page:

    ## Question 12:            <- bottom of page 7
    ---                        <- page break
    [figure]                   <- top of page 8
    A child was brought with complaints of decreased vision. Fundus
    examination shows a developmental anomaly as shown below.

The fix adds evidence rather than removing the check: if the owner's crop
interval covers the figure's page, the figure lies INSIDE that block, which is
the same geometric fact a block-position claim rests on. Uncorroborated
carries still flag.

Run:  python3 test_carry_corroboration_regressions.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="run37_env_"))
os.environ["OUTPUT_DIR"] = str(_TMP / "out")

import boundary_phased as bph        # noqa: E402
import qbank_pipeline as qp          # noqa: E402

SUB, CH_ID, CH_NO = "OPH", "OPH-001", 1


def _rec(qn=12):
    return {"question_text": "A child was brought with complaints of decreased "
                             "vision. Fundus examination shows a developmental "
                             "anomaly as shown below. What is the cause?",
            "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
            "correct_option": "A",
            "solution_text": "Incomplete closure of the embryonic fissure is "
                             "the cause of this developmental anomaly here."}


class CarryCase(unittest.TestCase):
    """One carry-owned figure; the ledger row is written by hand."""

    F = f"{SUB}/{CH_ID}-012_Q_01.webp"
    OWNER = f"{CH_ID}-012"

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="run37_case_"))
        self._saved = (qp.DATA_DIR, qp.ASSETS_DIR)
        qp.DATA_DIR = self.out / "data"
        qp.ASSETS_DIR = self.out / "assets"
        qp.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (qp.ASSETS_DIR / "questions" / SUB).mkdir(parents=True, exist_ok=True)
        (qp.ASSETS_DIR / "questions" / self.F).write_bytes(b"\xff" * 4000)

    def tearDown(self):
        qp.DATA_DIR, qp.ASSETS_DIR = self._saved
        shutil.rmtree(self.out, ignore_errors=True)

    def _ledger(self, rows):
        (qp.DATA_DIR / "image_ownership.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def _carry_row(self, page=8, owner=None, slot="question", final=None,
                   chapter=None):
        return {"subject": SUB, "chapter_id": chapter or CH_ID, "page": page,
                "file": self.F, "final_file": final or self.F,
                "owner": owner or self.OWNER, "slot": slot,
                "method": qp.CARRY_CLAIM_SOURCE, "confidence": "medium",
                "outcome": "claimed"}

    def _row(self, corroborated=None):
        return qp.build_final_question(
            SUB, CH_ID, CH_NO, 12, _rec(),
            {"question": [self.F], "solution": []},
            source_pages=[7, 8], ownership_pages={self.F: 8},
            carry_corroborated=corroborated)


class TestCarryClaimsReader(CarryCase):
    def test_reads_only_carry_claims_for_this_chapter(self):
        self._ledger([
            self._carry_row(),
            {"chapter_id": CH_ID, "page": 4, "file": "x", "final_file": "x",
             "owner": f"{CH_ID}-003", "slot": "question",
             "method": "positional", "outcome": "claimed"},
            {"chapter_id": "OPH-002", "page": 30, "file": "y",
             "final_file": "y", "owner": "OPH-002-001", "slot": "question",
             "method": qp.CARRY_CLAIM_SOURCE, "outcome": "claimed"},
            self._carry_row(page=9, owner=f"{CH_ID}-013", slot="solution",
                            final=f"{SUB}/{CH_ID}-013_SOL_01.webp"),
            {"chapter_id": CH_ID, "page": 5, "file": "z", "final_file": "z",
             "owner": f"{CH_ID}-005", "slot": "question",
             "method": qp.CARRY_CLAIM_SOURCE, "outcome": "refused_tiny"},
        ])
        rows = qp.carry_claims(CH_ID)
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual({r["owner"] for r in rows},
                         {f"{CH_ID}-012", f"{CH_ID}-013"})

    def test_no_ledger_returns_empty(self):
        self.assertEqual(qp.carry_claims(CH_ID), [])


class TestCorroboratedCarryIsNotFlagged(CarryCase):
    def test_uncorroborated_carry_still_flags(self):
        self._ledger([self._carry_row()])
        row = self._row()
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        self.assertTrue(any("cross-page carry" in r for r in row["qa_reasons"]),
                        row["qa_reasons"])

    def test_corroborated_carry_is_ready(self):
        self._ledger([self._carry_row()])
        row = self._row(corroborated={self.F})
        self.assertEqual(row["qa_status"], "READY", row["qa_reasons"])
        self.assertFalse(row["manual_review"])

    def test_corroboration_only_clears_the_files_it_names(self):
        """A second, uncorroborated carry on the same question must still
        flag -- clearing the class wholesale would hide real guesses."""
        other = f"{SUB}/{CH_ID}-012_Q_02.webp"
        (qp.ASSETS_DIR / "questions" / other).write_bytes(b"\xff" * 4000)
        self._ledger([self._carry_row(),
                      self._carry_row(page=9, final=other)])
        row = qp.build_final_question(
            SUB, CH_ID, CH_NO, 12, _rec(),
            {"question": [self.F, other], "solution": []},
            source_pages=[7, 8, 9],
            ownership_pages={self.F: 8, other: 9},
            carry_corroborated={self.F})
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        blob = " ".join(row["qa_reasons"])
        self.assertIn(other, blob)
        self.assertNotIn(self.F, blob)

    def test_model_claims_are_never_cleared_by_corroboration(self):
        self._ledger([{"chapter_id": CH_ID, "page": 8, "file": self.F,
                       "final_file": self.F, "owner": self.OWNER,
                       "slot": "question", "method": "isolated_crop_vision",
                       "confidence": "medium", "outcome": "claimed"}])
        row = self._row(corroborated={self.F})
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        self.assertTrue(any("model-only" in r for r in row["qa_reasons"]))


class TestCorroborationIsComputedFromIntervals(CarryCase):
    """The real geometry: does the owner's block interval reach the figure's
    page?"""

    def _runner(self, intervals):
        r = bph.ChapterRunner.__new__(bph.ChapterRunner)
        r.chapter_id = CH_ID
        r._ivs = intervals
        r._visual_intervals = lambda label: r._ivs.get(label, [])
        return r

    def _corroborate(self, intervals, page=8, owner=None, slot="question"):
        self._ledger([self._carry_row(page=page, owner=owner, slot=slot)])
        return bph.ChapterRunner._corroborated_carry_files(
            self._runner(intervals))

    def test_interval_covering_the_figure_page_corroborates(self):
        ivals = {"Question": [{"n": 12, "strips": [{"page": 7}, {"page": 8}]}]}
        self.assertEqual(self._corroborate(ivals), {self.F})

    def test_interval_not_reaching_the_page_does_not(self):
        ivals = {"Question": [{"n": 12, "strips": [{"page": 7}]}]}
        self.assertEqual(self._corroborate(ivals), set())

    def test_a_different_owner_does_not_corroborate(self):
        ivals = {"Question": [{"n": 11, "strips": [{"page": 7}, {"page": 8}]}]}
        self.assertEqual(self._corroborate(ivals), set())

    def test_the_other_side_does_not_corroborate(self):
        """A question figure is not proven by the SOLUTION block's extent."""
        ivals = {"Solution": [{"n": 12, "strips": [{"page": 7}, {"page": 8}]}]}
        self.assertEqual(self._corroborate(ivals), set())

    def test_solution_side_uses_the_solution_intervals(self):
        f = f"{SUB}/{CH_ID}-010_SOL_01.webp"
        (qp.ASSETS_DIR / "questions" / f).write_bytes(b"\xff" * 4000)
        self._ledger([{"chapter_id": CH_ID, "page": 16, "file": f,
                       "final_file": f, "owner": f"{CH_ID}-010",
                       "slot": "solution", "method": qp.CARRY_CLAIM_SOURCE,
                       "confidence": "medium", "outcome": "claimed"}])
        ivals = {"Solution": [{"n": 10,
                               "strips": [{"page": 15}, {"page": 16}]}]}
        self.assertEqual(
            bph.ChapterRunner._corroborated_carry_files(self._runner(ivals)),
            {f})

    def test_no_intervals_means_nothing_is_cleared(self):
        """Safe default: without a visual header index every carry still
        flags."""
        self.assertEqual(self._corroborate({}), set())

    def test_option_slots_are_skipped(self):
        """An option-scoped figure has no block interval to prove it against,
        so it must stay flagged even when the page is covered."""
        ivals = {"Question": [{"n": 12, "strips": [{"page": 7}, {"page": 8}]}]}
        self.assertEqual(self._corroborate(ivals, slot="option"), set())


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
