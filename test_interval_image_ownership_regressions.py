#!/usr/bin/env python3
"""RUN-39 regressions: image ownership decided by the CROP INTERVAL.

Text and figures used to be attributed by two different header systems:
crops from header_index.intervals, images from qp.union_block_headers_on_page
page by page with a cross-page "carry" to bridge them. The carry only existed
because the image pass never saw the intervals.

The case that motivated this is OPH-001 q12, whose layout is:

    page 7, bottom:  "Question 12:"            (y = 50)
    page break
    page 8, top:     [figure]                  (y = 700)
                     "A child was brought ... as shown below."
    page 8, lower:   "Question 13:"            (y = 600)

q12's interval therefore spans page 7 (down to the page bottom) and page 8
from y=9999 down to q13's header at y=600 -- so the figure at y=700 is
INSIDE q12's block by construction. The old per-page pass saw no heading
above the figure on page 8 and fell back to a carry claim.

Run:  python3 test_interval_image_ownership_regressions.py
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

_TMP = Path(tempfile.mkdtemp(prefix="run39_env_"))
os.environ["OUTPUT_DIR"] = str(_TMP / "out")

import boundary_phased as bph        # noqa: E402
import qbank_pipeline as qp          # noqa: E402

SUB, CH_NO, CH_ID = "OPH", 1, "OPH-001"

# q12 header on page 7 at y=50; q13 header on page 8 at y=600.
# Exactly what header_index.intervals produces for that layout.
Q12_INTERVAL = {"n": 12, "start_page": 7, "end_page": 8, "strips": [
    {"page": 7, "y_hi": 64.0, "y_lo": 0.0},
    {"page": 8, "y_hi": 9999.0, "y_lo": 600.0},
]}
Q13_INTERVAL = {"n": 13, "start_page": 8, "end_page": 8, "strips": [
    {"page": 8, "y_hi": 614.0, "y_lo": 0.0},
]}


class IntervalCase(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="run39_case_"))
        self._saved = (qp.DATA_DIR, qp.ASSETS_DIR, qp.image_positions_on_page)
        qp.DATA_DIR = self.out / "data"
        qp.ASSETS_DIR = self.out / "assets"
        qp.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (qp.ASSETS_DIR / "questions" / SUB).mkdir(parents=True, exist_ok=True)

        self.runner = bph.ChapterRunner.__new__(bph.ChapterRunner)
        self.runner.chapter_id = CH_ID
        self.runner.subject = SUB
        self.runner.chapter_no = CH_NO
        self.runner.pdf = "unused.pdf"
        self.runner._interval_cache = None
        self.intervals = {"Question": [], "Solution": []}
        self.runner._visual_intervals = lambda label: self.intervals.get(label, [])
        self.records = {12: {"question_text": "q12"}, 13: {"question_text": "q13"},
                        10: {"solution_text": "s10"}}

    def tearDown(self):
        qp.DATA_DIR, qp.ASSETS_DIR = self._saved[0], self._saved[1]
        qp.image_positions_on_page = self._saved[2]
        shutil.rmtree(self.out, ignore_errors=True)

    def _img(self, oid, page=8):
        """Create the temp image file and place it at y on `page`."""
        rel = f"{SUB}/{SUB}-p{page}-{oid}.webp"
        (qp.ASSETS_DIR / "questions" / SUB / f"{SUB}-p{page}-{oid}.webp"
         ).write_bytes(b"\xff" * 4000)
        return rel

    def _place(self, positions):
        qp.image_positions_on_page = lambda pdf, page: dict(positions)

    def _claim(self, page, rels):
        by_q = {}
        left = bph.ChapterRunner._claim_images_by_interval(
            self.runner, page, rels, self.records, by_q)
        return left, by_q

    def _ledger(self):
        p = qp.DATA_DIR / "image_ownership.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


class TestIntervalOwnership(IntervalCase):
    def test_figure_above_its_stem_belongs_to_the_block_no_carry(self):
        """The q12 case: figure at y=700 on page 8, q13's header below it at
        y=600. Inside q12's interval, so q12 owns it -- deterministically."""
        self.intervals["Question"] = [Q12_INTERVAL, Q13_INTERVAL]
        rel = self._img(18)
        self._place({18: (700.0, 100.0, 0, 200, 150)})
        left, by_q = self._claim(8, [rel])
        self.assertEqual(left, [])
        self.assertEqual(len(by_q[12]["question"]), 1)
        rows = self._ledger()
        self.assertEqual(rows[0]["method"], qp.INTERVAL_CLAIM_SOURCE)
        self.assertEqual(rows[0]["confidence"], "high")
        self.assertEqual(rows[0]["owner"], f"{CH_ID}-012")
        self.assertNotEqual(rows[0]["method"], qp.CARRY_CLAIM_SOURCE)

    def test_figure_below_a_header_belongs_to_that_block(self):
        """Same page, figure at y=300 -> inside q13's interval, not q12's."""
        self.intervals["Question"] = [Q12_INTERVAL, Q13_INTERVAL]
        rel = self._img(19)
        self._place({19: (300.0, 100.0, 0, 200, 150)})
        left, by_q = self._claim(8, [rel])
        self.assertEqual(left, [])
        self.assertEqual(list(by_q), [13])

    def test_solution_intervals_own_solution_figures(self):
        self.intervals["Solution"] = [
            {"n": 10, "strips": [{"page": 15, "y_hi": 800.0, "y_lo": 400.0}]}]
        rel = self._img(37, page=15)
        self._place({37: (600.0, 100.0, 0, 200, 150)})
        left, by_q = self._claim(15, [rel])
        self.assertEqual(left, [])
        self.assertEqual(len(by_q[10]["solution"]), 1)
        self.assertEqual(self._ledger()[0]["slot"], "solution")

    def test_figure_inside_no_interval_is_left_alone(self):
        """Page furniture / a full plate: the fallback passes decide."""
        self.intervals["Question"] = [Q13_INTERVAL]
        rel = self._img(99)
        self._place({99: (900.0, 100.0, 0, 200, 150)})   # above q13's header
        left, by_q = self._claim(8, [rel])
        self.assertEqual(left, [rel])
        self.assertEqual(by_q, {})

    def test_overlapping_intervals_are_not_guessed(self):
        """Two blocks claim the same point -> geometry does not decide."""
        self.intervals["Question"] = [
            {"n": 12, "strips": [{"page": 8, "y_hi": 9999.0, "y_lo": 200.0}]},
            {"n": 13, "strips": [{"page": 8, "y_hi": 900.0, "y_lo": 0.0}]},
        ]
        rel = self._img(50)
        self._place({50: (500.0, 100.0, 0, 200, 150)})
        left, by_q = self._claim(8, [rel])
        self.assertEqual(left, [rel])
        self.assertEqual(by_q, {})

    def test_a_q_no_with_no_record_is_left_alone(self):
        self.intervals["Question"] = [
            {"n": 99, "strips": [{"page": 8, "y_hi": 9999.0, "y_lo": 0.0}]}]
        rel = self._img(51)
        self._place({51: (500.0, 100.0, 0, 200, 150)})
        left, by_q = self._claim(8, [rel])
        self.assertEqual(left, [rel])
        self.assertEqual(by_q, {})

    def test_no_intervals_means_nothing_is_claimed(self):
        rel = self._img(52)
        self._place({52: (500.0, 100.0, 0, 200, 150)})
        left, by_q = self._claim(8, [rel])
        self.assertEqual(left, [rel])
        self.assertEqual(by_q, {})

    def test_no_position_data_means_nothing_is_claimed(self):
        self.intervals["Question"] = [Q13_INTERVAL]
        rel = self._img(53)
        self._place({})
        left, _ = self._claim(8, [rel])
        self.assertEqual(left, [rel])

    def test_negative_height_is_flip_normalised_to_the_bottom_edge(self):
        """A flipped cm reports y at the top with a negative height; the
        bottom edge is y + h, and that is what must be tested."""
        self.intervals["Question"] = [Q13_INTERVAL]
        rel = self._img(54)
        # y=500, h=-250 -> bottom edge 250, which IS inside q13 (0..614).
        self._place({54: (500.0, 100.0, 0, 200, -250)})
        left, by_q = self._claim(8, [rel])
        self.assertEqual(left, [])
        self.assertEqual(list(by_q), [13])

    def test_unparsable_oid_falls_through(self):
        self.intervals["Question"] = [Q13_INTERVAL]
        rel = f"{SUB}/not-a-temp-name.webp"
        (qp.ASSETS_DIR / "questions" / rel).write_bytes(b"\xff" * 4000)
        self._place({})
        left, by_q = self._claim(8, [rel])
        self.assertEqual(left, [rel])
        self.assertEqual(by_q, {})


class TestIntervalCache(IntervalCase):
    def test_intervals_are_built_once_and_reused(self):
        self.intervals["Question"] = [Q13_INTERVAL]
        calls = {"n": 0}

        def counting(label):
            calls["n"] += 1
            return self.intervals.get(label, [])

        self.runner._visual_intervals = counting
        first = bph.ChapterRunner._all_intervals(self.runner)
        second = bph.ChapterRunner._all_intervals(self.runner)
        self.assertIs(first, second)
        self.assertEqual(calls["n"], 2, "both sides are read on the first call")

    def test_a_side_that_raises_does_not_break_the_other(self):
        def flaky(label):
            if label == "Question":
                raise RuntimeError("scan failed")
            return [{"n": 10, "strips": [{"page": 15, "y_hi": 8.0, "y_lo": 0.0}]}]

        self.runner._visual_intervals = flaky
        out = bph.ChapterRunner._all_intervals(self.runner)
        self.assertEqual([k for k, _q, _iv in out], ["solution"])


class TestAttributionCountsIntervalClaims(IntervalCase):
    def _ledger_rows(self, rows):
        (qp.DATA_DIR / "image_ownership.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_interval_is_counted_separately_from_carry(self):
        self._ledger_rows([
            {"chapter_id": CH_ID, "outcome": "claimed", "final_file": "a",
             "method": qp.INTERVAL_CLAIM_SOURCE},
            {"chapter_id": CH_ID, "outcome": "claimed", "final_file": "b",
             "method": qp.CARRY_CLAIM_SOURCE},
            {"chapter_id": CH_ID, "outcome": "claimed", "final_file": "c",
             "method": "positional"},
        ])
        s = qp.image_attribution_summary(CH_ID)
        self.assertEqual(s["interval"], 1)
        self.assertEqual(s["carry"], 1)
        self.assertEqual(s["positional"], 1)
        self.assertEqual(s["claimed_total"], 3)

    def test_interval_claims_are_not_flagged_for_review(self):
        f = f"{SUB}/{CH_ID}-012_Q_01.webp"
        (qp.ASSETS_DIR / "questions" / f).write_bytes(b"\xff" * 4000)
        self._ledger_rows([
            {"chapter_id": CH_ID, "outcome": "claimed", "page": 8, "file": f,
             "final_file": f, "owner": f"{CH_ID}-012", "slot": "question",
             "method": qp.INTERVAL_CLAIM_SOURCE, "confidence": "high"}])
        rec = {"question_text": "A child was brought with decreased vision.",
               "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
               "correct_option": "A",
               "solution_text": "Incomplete closure of the embryonic fissure."}
        row = qp.build_final_question(SUB, CH_ID, CH_NO, 12, rec,
                                     {"question": [f], "solution": []},
                                     source_pages=[7, 8],
                                     ownership_pages={f: 8})
        self.assertEqual(row["qa_status"], "READY", row["qa_reasons"])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
