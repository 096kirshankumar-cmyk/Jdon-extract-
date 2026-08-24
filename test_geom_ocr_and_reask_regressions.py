#!/usr/bin/env python3
"""RUN-32 regressions: the geometric crop parse must survive a bad text
layer, and a printed block past the chapter end must still be re-asked.

OPH-001 live (Railway 2026-08-24 08:07):

    Q crops=23 geom_ok=0 (0 Gemini calls) gemini_crops=23
    S crops=23 geom_ok=0 (0 Gemini calls) gemini_crops=23
    [GATE] OPH-001: 1 export-gate violation(s) -- NOT a clean export
      - missing_solution 23: no solution_text (header-only ...)

Two independent defects:

  1. _geom_item_from_interval bailed unless the PDF text layer read CLEAN, so
     on this book every one of the 46 crops fell through to Gemini. The same
     rendered pixels already OCR well enough for header anchors, so the body
     text was available and simply never requested.

  2. _printed_header_reask iterated the CLAMPED zone pages (12-22) while the
     crops are cut to file_end=ch_last+2. q23's solution header sits past
     page 22, so its crop was extracted, came back header-only, and the
     re-ask that exists precisely for that case never fired.

Run:  python3 test_geom_ocr_and_reask_regressions.py
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="run32_env_"))
os.environ["OUTPUT_DIR"] = str(_TMP / "out")

from PIL import Image                # noqa: E402

import boundary_phased as bph        # noqa: E402
import qbank_pipeline as qp          # noqa: E402

# text_layer_health needs a real page's worth of letters: a short fixture
# scores letters/n < 0.50 and reads DEGRADED, which is not what these tests
# are exercising. Verified against header_index.text_layer_health directly.
Q_TEXT = (
    "7. A 45-year-old woman presents with painless progressive loss of "
    "vision in the right eye for three months. On examination the best "
    "corrected visual acuity is 6/24 and the anterior segment is normal. "
    "Which of the following is the most likely diagnosis?\n"
    "(A) Primary open angle glaucoma\n"
    "(B) Senile cortical cataract\n"
    "(C) Central retinal vein occlusion\n"
    "(D) Diabetic macular edema")
S_TEXT = (
    "Solution to Question 7: The answer is B because senile cortical "
    "cataract causes painless progressive loss of vision with a normal "
    "anterior segment and no relative afferent pupillary defect on "
    "examination of the patient.")


class GeomCase(unittest.TestCase):
    """Drives the real _geom_item_from_interval with the page readers stubbed."""

    IV = {"n": 7, "start_page": 14, "end_page": 14,
          "strips": [{"page": 14, "y_hi": 700.0, "y_lo": 400.0}]}

    def setUp(self):
        self.runner = bph.ChapterRunner.__new__(bph.ChapterRunner)
        self.runner.chapter_id = "OPH-001"
        self.runner.pdf = "unused.pdf"
        self.runner._geom_stats = {}
        self._saved = {
            "pdftotext_page": qp.pdftotext_page,
            "ocr_crop_text": getattr(qp, "ocr_crop_text", None),
            "crop_strip_png": bph._crop_strip_png,
        }
        # A REAL png: the production code does Image.open() on these bytes,
        # so a fake header raises and the OCR path is silently skipped.
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="PNG")
        png = buf.getvalue()
        bph._crop_strip_png = lambda *a, **k: png

    def tearDown(self):
        qp.pdftotext_page = self._saved["pdftotext_page"]
        if self._saved["ocr_crop_text"] is not None:
            qp.ocr_crop_text = self._saved["ocr_crop_text"]
        bph._crop_strip_png = self._saved["crop_strip_png"]

    def _text(self, blob):
        qp.pdftotext_page = lambda pdf, page: blob

    def _ocr(self, blob):
        qp.ocr_crop_text = lambda img: blob

    def _run(self, label="Question"):
        return self.runner._geom_item_from_interval(self.IV, label)


class TestTextLayerStillWins(GeomCase):
    def test_clean_complete_text_is_parsed_with_no_ocr(self):
        self._text(Q_TEXT)
        self._ocr("MUST NOT BE USED")
        it = self._run("Question")
        self.assertIsNotNone(it)
        self.assertEqual(self.runner._geom_stats.get("text"), 1)
        self.assertNotIn("ocr", self.runner._geom_stats)

    def test_clean_but_incomplete_does_not_waste_time_on_ocr(self):
        """A readable text layer that fails to parse is a crop_parse coverage
        gap. OCR of the same words cannot do better, so it must not run."""
        self._text(Q_TEXT.split("\n")[0])   # stem only, no options
        called = {"n": 0}

        def _spy(img):
            called["n"] += 1
            return "anything"

        qp.ocr_crop_text = _spy
        self.assertIsNone(self._run("Question"))
        self.assertEqual(self.runner._geom_stats.get("text_clean_parse_missed"), 1)
        self.assertEqual(called["n"], 0, "OCR must not run on a CLEAN page")


class TestOcrFallback(GeomCase):
    def test_garbled_text_layer_falls_back_to_ocr(self):
        self._text("7. \u25a0\u25a0\u25a0\u25a0 \ufffd\ufffd\ufffd\ufffd\ufffd\ufffd")
        self._ocr(Q_TEXT)
        it = self._run("Question")
        self.assertIsNotNone(it, "OCR must recover a crop the text layer lost")
        self.assertEqual(self.runner._geom_stats.get("ocr"), 1)

    def test_empty_text_layer_falls_back_to_ocr_for_solutions(self):
        self._text("")
        self._ocr(S_TEXT)
        it = self._run("Solution")
        self.assertIsNotNone(it)
        self.assertEqual(self.runner._geom_stats.get("ocr"), 1)

    def test_no_tesseract_is_reported_not_silently_ignored(self):
        self._text("")
        self._ocr("")                        # binary absent -> ''
        self.assertIsNone(self._run("Question"))
        self.assertEqual(self.runner._geom_stats.get("text_empty_no_ocr"), 1)

    def test_incomplete_ocr_result_is_not_shipped(self):
        """OCR of a diagram is noisy. An incomplete read must fall through to
        Gemini, not ship half a question."""
        self._text("")
        self._ocr("7. Stem only, options lost in the figure")
        self.assertIsNone(self._run("Question"))
        self.assertEqual(
            self.runner._geom_stats.get("text_empty_ocr_parse_missed"), 1)

    def test_interval_with_no_strips_is_tallied(self):
        self.runner._geom_item_from_interval({"n": 7, "strips": []}, "Question")
        self.assertEqual(self.runner._geom_stats.get("no_strips"), 1)


class ReaskCase(unittest.TestCase):
    """Real _printed_header_reask with the PDF and Gemini leaves stubbed."""

    def _runner(self, zone_pages, hdrs, intervals):
        r = bph.ChapterRunner.__new__(bph.ChapterRunner)
        r.chapter_id = "OPH-001"
        r.notes = []
        r.ledger_rows = []
        r._printed_s_hdrs = hdrs
        r._ivs = intervals
        r.asked_pages = []
        r._visual_intervals = lambda label: r._ivs
        r._call_pages = lambda pages, prompt, dpi=110: (
            r.asked_pages.append(list(pages)) or "[]")
        r._ledger = lambda *a, **k: None
        r._printed_boundary_note = lambda *a, **k: ""
        return r

    IVALS = [{"n": 23, "start_page": 23, "end_page": 24,
              "strips": [{"page": 23, "y_hi": 9.0, "y_lo": 0.0},
                         {"page": 24, "y_hi": 9.0, "y_lo": 0.0}]}]

    def test_header_past_chapter_end_is_still_reasked(self):
        """q23's solution header is on p23; the zone stops at 22."""
        r = self._runner(zone_pages=list(range(12, 23)),
                         hdrs={23: {23}}, intervals=self.IVALS)
        items = [{"_qn": 23, "solution_text": "Solution to Question 23:"}]
        bph.ChapterRunner._printed_header_reask(
            r, "Solution", items, list(range(12, 23)), "_printed_s_hdrs",
            "{chapter_name}{start}{end}")
        self.assertTrue(r.asked_pages,
                        "a printed header with empty content must be re-asked")
        self.assertIn(23, r.asked_pages[0],
                      "the re-ask must read the page the header is on")

    def test_page_window_is_not_clamped_back_inside_the_zone(self):
        """The re-ask window is [header_page, header_page+1]. Clamped at
        max(zone_pages)=22 that range is empty, so the old code produced no
        pages at all and never called the model -- the fix must yield the
        real window, which runs to the crop's last page."""
        r = self._runner(zone_pages=list(range(12, 23)),
                         hdrs={23: {23}}, intervals=self.IVALS)
        items = [{"_qn": 23, "solution_text": ""}]
        bph.ChapterRunner._printed_header_reask(
            r, "Solution", items, list(range(12, 23)), "_printed_s_hdrs",
            "{chapter_name}{start}{end}")
        self.assertEqual(r.asked_pages, [[23, 24]])

    def test_nothing_missing_means_no_reask(self):
        r = self._runner(zone_pages=list(range(12, 23)),
                         hdrs={23: {23}}, intervals=self.IVALS)
        items = [{"_qn": 23, "solution_text": "A real solution."}]
        bph.ChapterRunner._printed_header_reask(
            r, "Solution", items, list(range(12, 23)), "_printed_s_hdrs",
            "{chapter_name}{start}{end}")
        self.assertEqual(r.asked_pages, [])

    def test_zone_only_case_is_unchanged(self):
        """Regression guard: the original in-zone behaviour still works."""
        r = self._runner(zone_pages=list(range(12, 23)),
                         hdrs={15: {15}}, intervals=[])
        items = [{"_qn": 15, "solution_text": ""}]
        bph.ChapterRunner._printed_header_reask(
            r, "Solution", items, list(range(12, 23)), "_printed_s_hdrs",
            "{chapter_name}{start}{end}")
        self.assertTrue(r.asked_pages)
        self.assertIn(15, r.asked_pages[0])


class TestPlaceholderCheckSkipsDeterministicText(unittest.TestCase):
    """RUN-33: OPH-001 after the OCR fallback started working --

        Q crops=23 geom_ok=22 ... | why: ocr=22 text_garbled_ocr_parse_missed=1
        S crops=23 geom_ok=23 ... | why: ocr=23

    -- then logged 17 img_placeholder_count_mismatch rows across 13 of 23
    questions and the chapter went to 15 REVIEW_NEEDED. crop_parse is a text
    parser; it never emits [IMG], so "0 tokens vs N owned files" fired on
    every figured item."""

    def _rec(self, text, method, qn=5):
        return {qn: {"question_text": text, "solution_text": "",
                     "_q_text_method": method, "_s_text_method": ""}}

    def test_deterministic_text_with_figures_is_not_flagged(self):
        recs = self._rec("Stem with a diagram but no placeholder.",
                         "geometric_text")
        qp.apply_img_placeholder_reconcile(
            recs, {5: {"question": ["OPH/OPH-001-005_Q_01.webp"],
                       "solution": []}})
        self.assertEqual(recs[5]["_review_reasons"], [],
                         "a geometric parse has no [IMG] discipline to check")

    def test_model_text_missing_placeholders_is_still_flagged(self):
        """The check still does its real job: a model read that dropped its
        [IMG] markers is a genuine defect."""
        recs = self._rec("Stem with a diagram but no placeholder.", "")
        qp.apply_img_placeholder_reconcile(
            recs, {5: {"question": ["OPH/OPH-001-005_Q_01.webp"],
                       "solution": []}})
        self.assertTrue(any("img_placeholder_count_mismatch" in r
                            for r in recs[5]["_review_reasons"]),
                        recs[5]["_review_reasons"])

    def test_model_text_with_matching_count_is_clean(self):
        recs = self._rec("Stem [IMG] with a diagram.", "")
        qp.apply_img_placeholder_reconcile(
            recs, {5: {"question": ["OPH/OPH-001-005_Q_01.webp"],
                       "solution": []}})
        self.assertEqual(recs[5]["_review_reasons"], [])

    def test_sides_are_judged_independently(self):
        """Deterministic question + model-written solution: only the solution
        side is checked."""
        recs = {5: {"question_text": "Stem, no placeholder.",
                    "solution_text": "Solution, no placeholder.",
                    "_q_text_method": "geometric_text",
                    "_s_text_method": ""}}
        qp.apply_img_placeholder_reconcile(
            recs, {5: {"question": ["a.webp"], "solution": ["b.webp"]}})
        notes = recs[5]["_review_reasons"]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("solution", notes[0])


if __name__ == "__main__":
    import shutil
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
