#!/usr/bin/env python3
"""RUN-30: the crop-vs-page decision must be visible in the log.

The user's report -- "I use the crop method but it still uses the page
method" -- was undiagnosable because _extract_phase chose between the two
paths with NO log line at all. When the visual header scan came back empty
the phase silently degraded to whole-page Gemini calls, and a fully
geometric crop phase printed nothing either, so the two looked identical.

These tests drive the real _extract_phase / _extract_from_crops bodies and
assert on what they print. Only the leaves that would touch the PDF or
Gemini are stubbed.

Run:  python3 test_crop_path_logging.py
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="run30_env_"))
os.environ["OUTPUT_DIR"] = str(_TMP / "out")

import boundary_phased as bph        # noqa: E402

IV = [{"n": 1, "start_page": 67, "end_page": 67, "strips": []},
      {"n": 2, "start_page": 68, "end_page": 68, "strips": []}]


class _Base:
    """Shared scaffolding. The REAL _extract_phase body is bound here; the
    subclasses decide whether _extract_from_crops is stubbed or real."""

    _extract_phase = bph.ChapterRunner._extract_phase

    def __init__(self, intervals, headers):
        self.chapter_id = "OPH-001"
        self.notes = []
        self.ledger_rows = []
        self._ivs = intervals
        self._visual_headers = headers
        self.crop_calls = []
        self.page_calls = []

    def _visual_intervals(self, label):
        return self._ivs if label in ("Question", "Solution") else []

    def _call_pages(self, pages, prompt, dpi=110):
        self.page_calls.append(tuple(pages))
        return "[]"

    def _c1_split_solutions(self, items):
        return items

    def _ledger(self, pass_name, pages, status, n_items, note=""):
        self.ledger_rows.append(pass_name)

    def _merge_phase_items(self, items):
        return items


class DecisionRunner(_Base):
    """For the crop-vs-page decision: stub the crop extractor so the test
    isolates _extract_phase's branching."""

    def _extract_from_crops(self, ivals, prompt_tmpl, label, pass_name, dpi=130):
        self.crop_calls.append((label, len(ivals)))
        return [{"_qn": iv["n"]} for iv in ivals]


class CropRunner(_Base):
    """For the geom/Gemini split: run the REAL _extract_from_crops, stub only
    the geometric reader and the Gemini batch call."""

    _extract_from_crops = bph.ChapterRunner._extract_from_crops
    # RUN-31 added a ship-check to the crop loop; both are staticmethods on
    # ChapterRunner, so re-wrap or `self` is passed as the item.
    _crop_item_shippable = staticmethod(bph.ChapterRunner._crop_item_shippable)

    def _geom_item_from_interval(self, iv, label):
        return None                     # force every crop to the Gemini leg

    def _crop_is_batchable(self, iv, label, leftover):
        return False

    def _gemini_crop_batch(self, batch, prompt_tmpl, label, pass_name, dpi):
        return [{"_qn": iv["n"]} for iv in batch]


class TestCropPathIsAnnounced(unittest.TestCase):
    def _run(self, label, intervals, headers):
        r = DecisionRunner(intervals, headers)
        buf = io.StringIO()
        with redirect_stdout(buf):
            r._extract_phase([67, 68], "{chapter_name}{start}{end}",
                             label, label[0])
        return r, buf.getvalue()

    def test_crop_path_announces_itself(self):
        r, out = self._run("Question", IV, headers=[{"page": 67}])
        self.assertEqual(r.crop_calls, [("Question", 2)])
        self.assertEqual(r.page_calls, [])
        self.assertIn("CROP path", out)
        self.assertIn("2 Question crop(s)", out)
        self.assertIn("67-68", out)

    def test_page_fallback_says_why(self):
        """The case the user hit: no crops available -> must say so."""
        r, out = self._run("Question", [], headers=[])
        self.assertEqual(r.crop_calls, [])
        self.assertTrue(r.page_calls)
        self.assertIn("PAGE path", out)
        self.assertIn("FALLBACK", out)
        self.assertIn("0 Question crops", out)
        # and it must be recorded on the chapter, not only on stdout
        self.assertTrue(any("no Question crops" in n for n in r.notes), r.notes)

    def test_fallback_reports_how_many_headers_were_scanned(self):
        """Headers scanned but none of this type is a different failure."""
        r, out = self._run("Solution", [],
                           headers=[{"page": 67, "type": "q"},
                                    {"page": 68, "type": "q"}])
        self.assertIn("2 header(s) scanned total", out)

    def test_answer_phase_page_path_is_not_called_a_fallback(self):
        """Whole pages are CORRECT for the answer-key table."""
        r, out = self._run("Answer", [], headers=[])
        self.assertIn("PAGE path", out)
        self.assertNotIn("FALLBACK", out)
        self.assertIn("answer key is a page-spanning table", out)
        self.assertEqual(r.notes, [])


class TestCropGeomSplitIsAlwaysReported(unittest.TestCase):
    def _run(self, r):
        buf = io.StringIO()
        with redirect_stdout(buf):
            r._extract_from_crops(IV, "{chapter_name}{start}{end}",
                                  "Question", "Q", dpi=130)
        return buf.getvalue()

    def test_all_geometric_phase_still_prints_its_zero_gemini_count(self):
        """A fully geometric phase is the best outcome -- it must not look
        like a phase that never ran."""
        r = CropRunner(IV, [{"page": 67}])
        r._geom_item_from_interval = lambda iv, label: {
            "stem": "S?", "options": ["a", "b", "c", "d"]}
        out = self._run(r)
        self.assertIn("crops=2", out)
        self.assertIn("geom_ok=2", out)
        self.assertIn("gemini_crops=0", out)
        self.assertIn("0 Gemini calls", out)

    def test_mixed_phase_reports_both_halves(self):
        r = CropRunner(IV, [{"page": 67}])
        calls = {"n": 0}

        def geom(iv, label):
            calls["n"] += 1
            return ({"stem": "S?", "options": ["a", "b", "c", "d"]}
                    if calls["n"] == 1 else None)

        r._geom_item_from_interval = geom
        out = self._run(r)
        self.assertIn("geom_ok=1", out)
        self.assertIn("gemini_crops=1", out)

    def test_all_gemini_phase_reports_the_full_count(self):
        r = CropRunner(IV, [{"page": 67}])
        out = self._run(r)
        self.assertIn("geom_ok=0", out)
        self.assertIn("gemini_crops=2", out)


if __name__ == "__main__":
    import shutil
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
