#!/usr/bin/env python3
"""boundary_phased: spec prompts exact, verify-loop safety, cross-check wiring,
content-lock sanity. Gemini is stubbed everywhere."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import boundary_phased as bph


class T(unittest.TestCase):
    def test_prompts_are_exact_spec_text(self):
        for needle in ("QUESTIONS → ANSWER-KEY", '"start_page"', "confidence",
                       "answer-key", '"question_block"'):
            self.assertIn(needle, bph.BOUNDARY_PROMPT)
        for needle in ("SIRF stem aur options nikaalo", "has_figure",
                       "text_confidence"):
            self.assertIn(needle, bph.QUESTION_PROMPT)
        self.assertIn("SIRF table ke andar", bph.ANSWER_KEY_PROMPT)
        self.assertIn("bleed", bph.SOLUTION_PROMPT)
        self.assertIn("Anchor rule", bph.SOLUTION_PROMPT)
        self.assertIn("number-marker", bph.SOLUTION_PROMPT)
        self.assertIn("status\": \"LOCKED\" or \"NEEDS_FIX\"",
                      bph.CHAPTER_FINAL_PROMPT.replace("status\": \"LOCKED\" or \"NEEDS_FIX\"",
                                                      'status": "LOCKED" or "NEEDS_FIX"'))

    def test_verify_bleed_line_only_on_solutions(self):
        with_bleed = bph.VERIFY_PROMPT.format(phase_name="Solution",
                                              bleed_line=bph.BLEED_LINE)
        without = bph.VERIFY_PROMPT.format(phase_name="Answer-key",
                                           bleed_line="")
        self.assertIn("bleed", with_bleed)
        self.assertNotIn("Extra check", without)

    def test_parse_json_fail_safe(self):
        self.assertIsNone(bph._parse_json("not json at all"))
        self.assertEqual(bph._parse_json('noise [{"x": 1}] more'), [{"x": 1}])


class _FakeModel:
    """Returns per-phase canned answers; drives a full run() end to end."""
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def generate_content(self, files, generation_config=None):
        prompt = files[-1]
        self.calls.append(str(prompt))
        return _Resp(self.answers.pop(0) if self.answers else "{}")


class _Resp:
    def __init__(self, text):
        self.text = text


class RunShapes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bph_"))
        (self.tmp / "data" / "boundary_phased").mkdir(parents=True)

    def test_full_run_locks_with_perfect_stubs(self):
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        qs = [{"q_no": "1", "stem": "S?", "options": {"A": "a", "B": "b",
               "C": "c", "D": "d"}, "has_figure": False,
               "figure_location": None, "source_page": 5,
               "text_confidence": "high"}]
        an = [{"q_no": "1", "correct_option": "A", "low_confidence": False}]
        so = [{"q_no": "1", "solution_text": "Sol.", "has_figure": False,
               "figure_location": None, "source_page_range": [11, 11],
               "text_confidence": "high"}]
        ok = {"phase": "Question", "total_entries_checked": 1,
              "all_verified": True, "mismatches": []}
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 1, "issues": []}
        answers = [  # boundary chunks 2 calls, then Q extract+verify, A, S, cross
            json.dumps(bnd), json.dumps(bnd),          # detect: 2 page chunks
            json.dumps(qs), json.dumps(ok),            # Q
            json.dumps(an), json.dumps(ok),            # A     (page 10 only)
            json.dumps(so), json.dumps(ok),            # S     (pages 11-13)
            json.dumps(cross),                          # Step 7
        ]
        bph._png_bytes = lambda *_: b"png"            # stub the raster too
        model = _FakeModel(answers)
        r = bph.ChapterRunner("dummy.pdf", "OPH", 1, self.tmp, model=model)
        work = r.run(5, 13)
        self.assertTrue(work["locked"])
        self.assertEqual(work["questions"], 1)
        self.assertEqual(work["answers"], 1)
        self.assertEqual(work["solutions"], 1)
        # prompts saw the exact spec language
        self.assertTrue(any("QUESTIONS" in c for c in model.calls))
        self.assertTrue(any("bleed" in c for c in model.calls))

    def test_missing_block_boundary_refuses_and_never_locks(self):
        """An unanswered boundary question (e.g. solution block start missing)
        must ABORT with no writes at all -- a half-zoned chapter locking as
        'done' is exactly the silent failure the spec forbids."""
        bnd = {"question_block": {"start_page": 5}, "answer_key_block": {},
               "solution_block": {}, "confidence": "high"}
        bph._png_bytes = lambda *_: b"png"
        model = _FakeModel([json.dumps(bnd)])
        r = bph.ChapterRunner("dummy.pdf", "OPH", 1, self.tmp, model=model)
        with self.assertRaises(RuntimeError):
            r.run(5, 9)

    def test_content_lock_static(self):
        src = Path(bph.__file__).read_text(encoding="utf-8")
        for banned in ("apply_edit(", "apply_image_op(", "apply_orphan_merge("):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
