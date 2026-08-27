#!/usr/bin/env python3
"""RUN-49 regressions: a question the book PRINTS must never vanish silently.

OPH-013 live run (Railway, 2026-08-26 21:18) -- the user's first reported
defect, "OPH-013-018 was skipped entirely":

    [GATE] OPH-013: 7 export-gate violation(s) -- NOT a clean export
      - unresolved_page_Q [306]: UNRESOLVED
      - unresolved_page_REASK_Q [306, 307]: UNRESOLVED
      - unresolved_image 307: OPH/OPH-p307-2037.webp (method=none)
      - chapter_not_locked None: ... Question: printed headers prove
        missing/empty block(s) q[18] -- targeted re-ask on [306, 307];
        Question re-ask unresolved: model blocked pages 306-307;
        ledger lock refused: extracted Q [1..17, 19..35] != key rows [1..35]
    [SPLIT] OPH-013: 34 questions / 34 answers / 34 solutions

Three independent failures, each with its own test class here:

  1. THE EXTRACTION HOLE. Gemini's recitation filter refused the crop IMAGE
     (finish_reason 4 -> ModelBlocked). The crop path had NO escape: it wrote
     one ledger row and gave up. The targeted re-ask then sent the SAME page
     images to the SAME filter and got the SAME refusal. The page path already
     escapes exactly this situation (RUN-42) by OCR-ing the printed text,
     because the filter is on the image, not on the text -- the crop path and
     the re-ask just never called it.

  2. THE INVISIBLE HOLE. qbank_validator.check_chapter computes qns/s at
     chapter scope but the numbering_gap / numbering_start appends were
     INDENTED INSIDE the suspect_truncated_table loop, so they only ran for a
     chapter with >=2 solutions sharing a table header. OPH-013 has none, so
     nothing anywhere flagged the missing 18.

  3. THE UNHELPFUL LOOKUP. /review/lookup answered "koi row nahi mili:
     013-018" with no hint, because the hint's row pool came from
     lookup_questions(out, "", None) -- an empty term returns [] by contract,
     so the pool was ALWAYS empty. '013-018' also failed the letter-prefixed
     chapter regex, so the chapter was never resolved either.

Run:  python3 test_missing_question_recovery_regressions.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="run49_env_"))
os.environ["OUTPUT_DIR"] = str(_TMP / "out")

import boundary_phased as bph        # noqa: E402
import qbank_pipeline as qp          # noqa: E402
import qbank_validator as qv         # noqa: E402

# The exact OPH-013 q18 crop: printed header on p306, body + the big drainage
# device figure running onto p307 -- a two-strip, two-page interval, which is
# why it was never batchable and went to Gemini isolated.
Q18_IV = {"n": 18, "start_page": 306, "end_page": 307,
          "strips": [{"page": 306, "y_hi": 720.0, "y_lo": 380.0},
                     {"page": 307, "y_hi": 740.0, "y_lo": 420.0}]}

Q18_OCR_ITEM = {"q_no": 18,
                "stem": "Which of the following is a drainage device used in "
                        "glaucoma surgery?",
                "options": {"A": "Ahmed valve", "B": "Scleral buckle",
                            "C": "Intraocular lens", "D": "Band keratoplasty"},
                "_qn": 18, "_ocr": True}


def _runner():
    """A real ChapterRunner object with no __init__ side effects (the pattern
    the other regression suites use), carrying exactly the attributes the
    code paths under test touch."""
    r = bph.ChapterRunner.__new__(bph.ChapterRunner)
    r.chapter_id = "OPH-013"
    r.subject = "OPH"
    r.chapter_no = 13
    r.pdf = "unused.pdf"
    r.notes = []
    r.ledger_rows = []
    r.orphan_items = []
    r._missing_qnos = []
    r._printed_q_max = None      # no printed ceiling proven in these fixtures
    r._printed_s_hdrs = {}
    return r


def _raise_blocked(*_a, **_k):
    raise bph.ModelBlocked()


class TestCropBlockEscapesViaOcr(unittest.TestCase):
    """_gemini_crop_batch: a blocked crop image must fall back to OCR text."""

    def test_blocked_crop_is_recovered_from_ocr_text(self):
        r = _runner()
        seen = {}
        r._call_crops = _raise_blocked

        def fake_ocr(pages, prompt, label, pass_name):
            seen["args"] = (list(pages), label, pass_name)
            return [dict(Q18_OCR_ITEM)]
        r._ocr_fallback = fake_ocr

        out = r._gemini_crop_batch([Q18_IV], bph.QUESTION_PROMPT,
                                   "Question", "Q", 130)

        self.assertEqual([it["_qn"] for it in out], [18])
        self.assertTrue(out[0].get("_ocr"),
                        "recovered text must stay marked _ocr -> REVIEW_NEEDED")
        # the OCR is over the crop's OWN pages, both strips
        self.assertEqual(seen["args"][0], [306, 307])
        self.assertEqual(seen["args"][1:], ("Question", "Q"))
        self.assertTrue(any("OCR text" in n for n in r.notes),
                        f"no recovery note in {r.notes}")
        # the block itself is still on the ledger -- honest, not papered over
        blocked = [row for row in r.ledger_rows
                   if "model blocked on crop" in (row.get("note") or "")]
        self.assertEqual(len(blocked), 1)

    def test_neighbouring_q_no_from_the_same_page_is_quarantined(self):
        """A whole-page OCR sees the next question too. Importing it here
        would bypass the crop isolation the crop path exists for."""
        r = _runner()
        r._call_crops = _raise_blocked
        neighbour = {"q_no": 19, "stem": "next question", "_qn": 19,
                     "_ocr": True}
        r._ocr_fallback = lambda *a, **k: [dict(Q18_OCR_ITEM), neighbour]

        out = r._gemini_crop_batch([Q18_IV], bph.QUESTION_PROMPT,
                                   "Question", "Q", 130)

        self.assertEqual([it["_qn"] for it in out], [18])
        self.assertEqual(len(r.orphan_items), 1)
        self.assertIn("outside this crop's expected", r.orphan_items[0]["reason"])
        self.assertEqual(r.orphan_items[0]["item"]["_qn"], 19)

    def test_ocr_failure_leaves_it_missing_not_invented(self):
        r = _runner()
        r._call_crops = _raise_blocked

        def boom(*a, **k):
            raise RuntimeError("tesseract: command not found")
        r._ocr_fallback = boom

        out = r._gemini_crop_batch([Q18_IV], bph.QUESTION_PROMPT,
                                   "Question", "Q", 130)

        self.assertEqual(out, [])
        self.assertTrue(any("left missing rather than shipped as a guess" in n
                            for n in r.notes), f"notes={r.notes}")

    def test_empty_ocr_result_leaves_it_missing(self):
        r = _runner()
        r._call_crops = _raise_blocked
        r._ocr_fallback = lambda *a, **k: []
        self.assertEqual(
            r._gemini_crop_batch([Q18_IV], bph.QUESTION_PROMPT,
                                 "Question", "Q", 130), [])
        self.assertEqual(r.orphan_items, [])

    def test_unblocked_crop_never_touches_the_ocr_path(self):
        """Zero extra cost on the happy path."""
        r = _runner()
        raw = json.dumps([{"q_no": 18, "stem": "printed stem",
                           "options": {"A": "a", "B": "b", "C": "c",
                                       "D": "d"}}])
        r._call_crops = lambda *a, **k: raw

        def no_ocr(*a, **k):
            raise AssertionError("OCR must not run when Gemini answered")
        r._ocr_fallback = no_ocr

        out = r._gemini_crop_batch([Q18_IV], bph.QUESTION_PROMPT,
                                   "Question", "Q", 130)
        self.assertEqual([it["_qn"] for it in out], [18])


class TestReaskBlockEscapesViaOcr(unittest.TestCase):
    """_printed_header_reask: the last resort must have a last resort."""

    def _reask(self, ocr_items, ocr_raises=False):
        r = _runner()
        r._printed_q_hdrs = {306: {18}, 307: set()}
        r._visual_intervals = lambda label: []
        r._printed_boundary_note = lambda phase, qns: ""
        r._call_pages = _raise_blocked
        if ocr_raises:
            def boom(*a, **k):
                raise RuntimeError("pdftoppm failed")
            r._ocr_fallback = boom
        else:
            r._ocr_fallback = lambda *a, **k: [dict(i) for i in ocr_items]
        # printed headers prove q18 exists; nothing was extracted for it
        return r, r._printed_header_reask("Question", [], [306, 307],
                                          "_printed_q_hdrs",
                                          bph.QUESTION_PROMPT)

    def test_blocked_reask_recovers_the_printed_question(self):
        _r, out = self._reask([Q18_OCR_ITEM])
        self.assertEqual([it["_qn"] for it in out], [18])
        self.assertTrue(out[0].get("_ocr"))
        self.assertTrue(out[0].get("_reasked"))

    def test_blocked_reask_with_no_ocr_still_leaves_the_hole_flagged(self):
        r, out = self._reask([], ocr_raises=True)
        self.assertEqual(out, [])
        self.assertTrue(any("unresolved" in n for n in r.notes),
                        f"notes={r.notes}")

    def test_an_already_complete_item_is_not_overwritten_by_ocr(self):
        """The OCR escape must not clobber a good model read."""
        r = _runner()
        r._printed_q_hdrs = {306: {18}}
        r._visual_intervals = lambda label: []
        r._printed_boundary_note = lambda phase, qns: ""
        r._call_pages = _raise_blocked
        r._ocr_fallback = lambda *a, **k: [dict(Q18_OCR_ITEM)]
        good = {"_qn": 18, "stem": "already extracted fine",
                "options": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"},
                            {"id": "C", "text": "c"}, {"id": "D", "text": "d"}]}
        out = r._printed_header_reask("Question", [good], [306],
                                      "_printed_q_hdrs", bph.QUESTION_PROMPT)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["stem"], "already extracted fine")


class TestLedgerLockNamesTheHole(unittest.TestCase):
    def test_refusal_lists_the_missing_question_numbers(self):
        r = _runner()
        r._key_evidence = {n: {"letter": "A", "method": "key_dual_gemini"}
                           for n in (17, 18, 19)}
        q = [{"_qn": 17}, {"_qn": 19}]
        a = [{"_qn": n, "correct_option": "A"} for n in (17, 18, 19)]
        s = [{"_qn": n, "solution_text": "sol"} for n in (17, 18, 19)]
        ok, why = r._ledger_lock(q, a, s)
        self.assertFalse(ok)
        self.assertIn("MISSING q[18]", why)
        self.assertEqual(r._missing_qnos, [18])

    def test_extra_question_is_named_too(self):
        r = _runner()
        r._key_evidence = {n: {"letter": "A", "method": "key_dual_gemini"}
                           for n in (17, 19)}
        ok, why = r._ledger_lock([{"_qn": 17}, {"_qn": 18}, {"_qn": 19}],
                                 [], [])
        self.assertFalse(ok)
        self.assertIn("EXTRA q[18]", why)


class TestNumberingGapIsUnconditional(unittest.TestCase):
    """qbank_validator.check_chapter: the gap check no longer depends on a
    chapter happening to contain a repeated solution table header."""

    @staticmethod
    def _rows(qnos):
        return [{"id": f"OPH-013-{n:03d}", "subject": "OPH",
                 "chapter_id": "OPH-013",
                 "question": {"text": f"Stem number {n} " + "x" * 40},
                 "options": [{"id": L, "text": f"option {L} for {n}"}
                             for L in "ABCD"],
                 "correct_options": ["A"],
                 "solution": {"text": f"Solution to {n}: " + "y" * 60}}
                for n in qnos]

    def test_hole_in_the_middle_is_flagged_with_no_tables_anywhere(self):
        flags = qv.check_chapter("OPH-013", self._rows([1, 2, 17, 19, 35]))
        gaps = [f for f in flags if f["kind"] == "numbering_gap"]
        want = [n for n in range(1, 36) if n not in (1, 2, 17, 19, 35)]
        self.assertEqual(sorted(f["q_no"] for f in gaps), want)
        self.assertIn(18, [f["q_no"] for f in gaps])

    def test_the_exact_oph013_series_flags_18(self):
        rows = self._rows(list(range(1, 18)) + list(range(19, 36)))
        gaps = [f for f in qv.check_chapter("OPH-013", rows)
                if f["kind"] == "numbering_gap"]
        self.assertEqual([f["q_no"] for f in gaps], [18])
        self.assertIn("18", gaps[0]["detail"])

    def test_contiguous_series_has_no_gap_flag(self):
        rows = self._rows(list(range(1, 36)))
        self.assertEqual([f for f in qv.check_chapter("OPH-013", rows)
                          if f["kind"] == "numbering_gap"], [])

    def test_series_not_starting_at_one_is_flagged(self):
        rows = self._rows(list(range(3, 8)))
        starts = [f for f in qv.check_chapter("OPH-013", rows)
                  if f["kind"] == "numbering_start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["q_no"], 3)

    def test_gap_flag_is_still_reported_next_to_a_table_group(self):
        """The original nesting did fire when a repeated table header existed;
        moving it out must not double-report."""
        rows = self._rows([1, 2, 4])
        for r in rows:
            r["solution"]["tables"] = [
                {"markdown": "| Drug | Dose |\n|---|---|\n| A | 1 |\n| B | 2 |"}]
            r["solution"]["text"] = r["solution"]["text"].rstrip() + " shown below:"
        gaps = [f for f in qv.check_chapter("OPH-013", rows)
                if f["kind"] == "numbering_gap"]
        self.assertEqual([f["q_no"] for f in gaps], [3])


class TestLookupMissHint(unittest.TestCase):
    """/review/lookup must explain WHY nothing matched '013-018'."""

    @classmethod
    def setUpClass(cls):
        out = Path(pipeline_output_root())
        (out / "data").mkdir(parents=True, exist_ok=True)
        (out / "split" / "OPH" / "OPH-013").mkdir(parents=True, exist_ok=True)
        rows = []
        for n in list(range(1, 18)) + list(range(19, 36)):
            rows.append({"id": f"OPH-013-{n:03d}", "subject": "OPH",
                         "chapter_id": "OPH-013",
                         "question": {"text": "stem"},
                         "options": [{"id": L, "text": L} for L in "ABCD"],
                         "correct_options": ["A"],
                         "solution": {"text": "sol"}})
        (out / "data" / "questions.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        import app as app_mod
        cls.app = app_mod

    def test_missing_row_names_the_chapter_and_the_series(self):
        c = self.app.app.test_client()
        html = c.get("/review/lookup?q=013-018").get_data(as_text=True)
        self.assertIn("koi row nahi mili", html)
        self.assertIn("OPH-013 me total 34 questions hain", html)
        self.assertIn("numbering_gap", html)

    def test_a_row_that_exists_still_resolves(self):
        c = self.app.app.test_client()
        html = c.get("/review/lookup?q=013-017").get_data(as_text=True)
        self.assertIn("OPH-013-017", html)
        self.assertNotIn("koi row nahi mili", html)


def pipeline_output_root():
    return qp.OUTPUT_ROOT


if __name__ == "__main__":
    unittest.main(verbosity=2)
