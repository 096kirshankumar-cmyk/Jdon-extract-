#!/usr/bin/env python3
"""RUN-31 regressions: zone span must not absorb the next chapter, and a
crop that is still bad after retry must be discarded rather than shipped.

OPH-001 live run (Railway, 2026-08-24 08:07):

    [BPH] OPH-001: model-derived zones Q 4-11 | A [12] | S 12-22     <- correct
    [BPH] OPH-001: printed-header probe Q pages [4..11, 24] | ...
    [BPH] OPH-001: USED zones Q 4-22 | A [12] | S 12-22              <- wrong

The boundary JSON was right. One Question header on page 24 -- which belongs
to the NEXT chapter, and is visible only because detect_boundaries scans
ch_last+2 -- was spanned min..max with the real headers, turning Q 4-11 into
Q 4-22 and overlapping the entire solution zone.

Second defect: _extract_from_crops re-sent a failed crop as a single, then
appended whatever came back with no further check, so a second bad read
shipped as if it were good.

Run:  python3 test_zone_and_retry_regressions.py
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

_TMP = Path(tempfile.mkdtemp(prefix="run31_env_"))
os.environ["OUTPUT_DIR"] = str(_TMP / "out")

import boundary_phased as bph        # noqa: E402


class TestZoneSpanIgnoresNextChapter(unittest.TestCase):
    """_zone_pages_from_headers is the unit that changed."""

    def test_oph001_next_chapter_header_does_not_stretch_the_zone(self):
        """The exact live case: p24 is chapter 2's first question."""
        self.assertEqual(
            bph._zone_pages_from_headers(
                [4, 5, 6, 7, 8, 9, 10, 11, 24], ch_last=22),
            list(range(4, 12)))

    def test_ordinary_headers_are_unchanged(self):
        self.assertEqual(
            bph._zone_pages_from_headers([4, 5, 6, 7, 8, 9, 10, 11], 22),
            list(range(4, 12)))

    def test_internal_gap_is_preserved(self):
        """A question spanning pages 6-8 leaves no header on 7. Stopping at
        the first gap would lose pages 8-9, so only ch_last is filtered."""
        self.assertEqual(
            bph._zone_pages_from_headers([4, 5, 6, 8, 9], 22),
            [4, 5, 6, 7, 8, 9])

    def test_all_headers_past_chapter_end_returns_empty(self):
        """Caller keeps the model-derived zone instead of getting an empty
        one -- an empty Q zone aborts the chapter."""
        self.assertEqual(bph._zone_pages_from_headers([23, 24], 22), [])

    def test_empty_and_none_inputs(self):
        self.assertEqual(bph._zone_pages_from_headers([], 22), [])
        self.assertEqual(bph._zone_pages_from_headers([None, None], 22), [])

    def test_header_exactly_at_chapter_end_is_kept(self):
        self.assertEqual(bph._zone_pages_from_headers([4, 22], 22),
                         list(range(4, 23)))

    def test_solutions_get_the_same_treatment(self):
        """Solution headers on p23-24 are the next chapter's too; the span
        stops at the last in-chapter header (14), not at ch_last."""
        self.assertEqual(
            bph._zone_pages_from_headers([12, 13, 14, 23, 24], 22),
            [12, 13, 14])


class CropRunner:
    """Real _extract_from_crops body; PDF and Gemini leaves stubbed."""

    _extract_from_crops = bph.ChapterRunner._extract_from_crops
    # Both are staticmethods on ChapterRunner; reading them off the class
    # yields the plain function, so re-wrap or `self` gets passed as `it`.
    _crop_item_ok = staticmethod(bph.ChapterRunner._crop_item_ok)
    _crop_item_shippable = staticmethod(bph.ChapterRunner._crop_item_shippable)

    def __init__(self, batch_result, retry_result):
        self.chapter_id = "OPH-001"
        self.notes = []
        self.batch_result = batch_result
        self.retry_result = retry_result
        self.calls = []

    def _geom_item_from_interval(self, iv, label):
        return None                        # force every crop to Gemini

    def _crop_is_batchable(self, iv, label, leftover):
        return False                       # one crop per call

    def _gemini_crop_batch(self, batch, prompt_tmpl, label, pass_name, dpi):
        self.calls.append(tuple(iv["n"] for iv in batch))
        return self.retry_result

    def _merge_phase_items(self, items):
        return items


IV1 = [{"n": 5, "start_page": 8, "end_page": 8, "strips": []}]


class TestShippableIsNotTheSameAsRetryable(unittest.TestCase):
    """has_figure must trigger a retry but must NOT block shipping."""

    def test_figured_question_is_retryable_but_shippable(self):
        it = {"stem": "S?", "options": ["a", "b", "c", "d"], "has_figure": True}
        self.assertFalse(bph.ChapterRunner._crop_item_ok(it, "Question"),
                         "a figure is worth a second read")
        self.assertTrue(bph.ChapterRunner._crop_item_shippable(it, "Question"),
                        "but it is not a text defect -- the image pass owns it")

    def test_incomplete_item_is_neither(self):
        self.assertFalse(
            bph.ChapterRunner._crop_item_shippable(
                {"stem": "S?", "options": ["a", "b"]}, "Question"))
        self.assertFalse(
            bph.ChapterRunner._crop_item_shippable({"solution_text": "  "},
                                                   "Solution"))

    def test_complete_items_ship(self):
        self.assertTrue(bph.ChapterRunner._crop_item_shippable(
            {"stem": "S?", "options": ["a", "b", "c", "d"]}, "Question"))
        self.assertTrue(bph.ChapterRunner._crop_item_shippable(
            {"solution_text": "Because."}, "Solution"))


class TestBadCropIsDiscardedAfterRetry(unittest.TestCase):
    def _run(self, retry_result):
        r = CropRunner(batch_result=None, retry_result=retry_result)
        buf = io.StringIO()
        with redirect_stdout(buf):
            # _crop_item_ok returns False for a figure-less 2-option item, so
            # the crop is queued for a single-crop retry.
            out = r._extract_from_crops(IV1, "{chapter_name}{start}{end}",
                                        "Question", "Q", dpi=130)
        return r, out, buf.getvalue()

    def test_still_bad_after_retry_is_discarded_not_shipped(self):
        bad = [{"_qn": 5, "stem": "S?", "options": ["a", "b"]}]
        r, out, log = self._run(bad)
        self.assertEqual(out, [], "a structurally incomplete item must not ship")
        self.assertTrue(any("DISCARDED" in n for n in r.notes), r.notes)
        self.assertIn("DISCARDED", log)
        self.assertIn("q5", log)

    def test_good_item_after_retry_is_kept(self):
        good = [{"_qn": 5, "stem": "S?", "options": ["a", "b", "c", "d"]}]
        r, out, log = self._run(good)
        self.assertEqual(len(out), 1)
        self.assertFalse(any("DISCARDED" in n for n in r.notes))

    def test_figured_item_after_retry_is_kept(self):
        """The case the old _crop_item_ok test would have wrongly dropped."""
        fig = [{"_qn": 5, "stem": "S?", "options": ["a", "b", "c", "d"],
                "has_figure": True}]
        r, out, _ = self._run(fig)
        self.assertEqual(len(out), 1,
                         "a diagram is not a reason to discard the question")

    def test_empty_retry_result_is_discarded_quietly(self):
        r, out, _ = self._run([])
        self.assertEqual(out, [])


if __name__ == "__main__":
    import shutil
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
