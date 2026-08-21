#!/usr/bin/env python3
"""boundary_phased engine tests: spec prompts exact, verify-loop safety,
cross-check wiring, REAL Step-8 write-through (master rows, chapter file,
state chapters_done, gate blocker rows), and the quota-pause contract.
Gemini is stubbed everywhere; no PDF is touched (heavy seams are faked)."""
import json
import tempfile
import unittest
from pathlib import Path

import boundary_phased as bph
import qbank_pipeline as qp


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
        self.assertIn('status": "LOCKED" or "NEEDS_FIX"',
                      bph.CHAPTER_FINAL_PROMPT)

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

    def test_merge_boundary_chunks_unions_later_chunk_blocks(self):
        early = {"question_block": {"start_page": 5}, "answer_key_block": {},
                 "solution_block": {}, "confidence": "high", "notes": ""}
        later = {"question_block": {"start_page": 12},
                 "answer_key_block": {"start_page": 10, "end_page": 10},
                 "solution_block": {"start_page": 11, "end_page": 13},
                 "confidence": "medium", "notes": "chunk2 saw solutions"}
        m = bph._merge_boundary_chunks([early, later])
        self.assertEqual(m["question_block"]["start_page"], 5)   # earliest wins
        self.assertEqual(m["answer_key_block"]["start_page"], 10)
        self.assertEqual(m["solution_block"]["start_page"], 11)
        # -1/'none' is 'absent', never a page number
        neg = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": -1},
               "solution_block": {"start_page": "none"},
               "confidence": "high"}
        m2 = bph._merge_boundary_chunks([neg])
        self.assertIsNone(m2["answer_key_block"])
        self.assertIsNone(m2["solution_block"])

    def test_content_lock_static(self):
        """The engine may write extraction output, but must NEVER touch the
        human-edit primitives (flag-don't-fix: content edits are for /review)."""
        src = Path(bph.__file__).read_text(encoding="utf-8")
        for banned in ("apply_edit(", "apply_image_op(", "apply_orphan_merge("):
            self.assertNotIn(banned, src)


class _FakeModel:
    """Returns per-phase canned answers; drives a full run() end to end."""
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def generate_content(self, files, generation_config=None,
                         safety_settings=None):
        prompt = files[-1]
        self.calls.append(str(prompt))
        return _Resp(self.answers.pop(0) if self.answers else "{}")


class _Resp:
    def __init__(self, text):
        self.text = text


class EngineCase(unittest.TestCase):
    """Run Steps 0-8 with stubbed model + stubbed PDF seams into a tmp
    output root, then assert the REAL on-disk artifacts."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bph_eng_"))
        self._orig = (qp.OUTPUT_ROOT, qp.DATA_DIR, qp.ASSETS_DIR,
                      qp.STATE_FILE)
        qp.OUTPUT_ROOT = self.tmp
        qp.DATA_DIR = self.tmp / "data"
        qp.ASSETS_DIR = self.tmp / "assets"
        qp.STATE_FILE = self.tmp / "state.json"
        qp.DATA_DIR.mkdir(parents=True)
        qp.ASSETS_DIR.mkdir(parents=True)
        self._pace = bph.PACE_CALLS
        bph.PACE_CALLS = False
        self._png = bph._png_bytes
        bph._png_bytes = lambda *_a, **_k: b"png"
        self._render = bph._render_chapter_jpgs
        # no real PDF in tests: the zero-token render seam returns nothing
        # (the split layer tolerates an empty page_files by failing safely --
        # _commit catches it and the master rows still write).
        bph._render_chapter_jpgs = lambda *a, **k: ([], [])
        self.state = {"calls_today": 0, "pdf_progress": {}}

    def tearDown(self):
        (qp.OUTPUT_ROOT, qp.DATA_DIR, qp.ASSETS_DIR, qp.STATE_FILE) = self._orig
        bph.PACE_CALLS = self._pace
        bph._png_bytes = self._png
        bph._render_chapter_jpgs = self._render

    def _runner(self, model):
        r = bph.ChapterRunner("dummy.pdf", "OPH", 1, self.tmp, model=model,
                              state=self.state)
        r._image_pass = lambda *a, **k: []     # no PDF images in tests
        return r

    @staticmethod
    def _answers(lock_status="LOCKED", with_boundary_retry=False):
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
        cross = {"chapter": "OPH-001", "status": lock_status,
                 "total_questions": 1, "issues": []}
        # call order: boundary 2 chunks (5-11, 12-13) -> Q extract 1 chunk
        # (5-9 fits one 7-page chunk) + verify -> A extract (10) + verify
        # -> S extract (11-13) + verify -> Step 7 cross-check (one half)
        return [json.dumps(bnd), json.dumps(bnd),      # detect: 2 page chunks
                json.dumps(qs), json.dumps(ok),        # Q extract + verify
                json.dumps(an), json.dumps(ok),        # A extract + verify
                json.dumps(so), json.dumps(ok),        # S extract + verify
                json.dumps(cross)]                     # Step 7 cross-check

    def test_locked_run_writes_real_artifacts(self):
        model = _FakeModel(self._answers())
        r = self._runner(model)
        res = r.run(5, 13)
        self.assertTrue(res["locked"])
        self.assertTrue(res["committed"])
        # master row on disk, exact id/content
        master = qp.DATA_DIR / "questions.jsonl"
        rows = [json.loads(l) for l in master.read_text().splitlines()
                if l.strip()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "OPH-001-001")
        self.assertEqual(row["question"]["text"], "S?")
        self.assertEqual(row["correct_options"], ["A"])
        self.assertEqual(row["solution"]["text"], "Sol.")
        self.assertEqual(row["qa_status"], "READY")
        self.assertFalse(row["declared_has_figure_in_question"])
        # per-chapter file + state done-marking
        chap = qp.DATA_DIR / "by_chapter" / "OPH-001.jsonl"
        self.assertTrue(chap.exists())
        st = json.loads(qp.STATE_FILE.read_text())
        self.assertIn("OPH-001", st["pdf_progress"]["OPH"]["chapters_done"])
        # no blocker rows on a clean lock
        gate = qp.DATA_DIR / "export_gate.jsonl"
        if gate.exists():
            kinds = [json.loads(l)["kind"] for l in gate.read_text().splitlines()
                     if l.strip()]
            self.assertNotIn("chapter_not_locked", kinds)
        # spec prompts actually went out
        self.assertTrue(any("QUESTIONS" in c for c in model.calls))
        self.assertTrue(any("bleed" in c for c in model.calls))

    def test_unlocked_run_still_commits_but_flags_blocker(self):
        model = _FakeModel(self._answers(lock_status="NEEDS_FIX") +
                           [json.dumps({"chapter": "OPH-001",
                                        "status": "NEEDS_FIX",
                                        "total_questions": 1,
                                        "issues": [{"q_no": "1",
                                                    "issue": "x",
                                                    "block": "solution"}]})] *
                           2)  # cross-check retries MAX_FIX_ATTEMPTS times
        r = self._runner(model)
        res = r.run(5, 13)
        self.assertFalse(res["locked"])
        self.assertTrue(res["committed"])      # rows still written
        rows = [json.loads(l) for l in
                (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
                if l.strip()]
        self.assertEqual(len(rows), 1)
        kinds = [json.loads(l)["kind"] for l in
                 (qp.DATA_DIR / "export_gate.jsonl").read_text().splitlines()
                 if l.strip()]
        self.assertIn("chapter_not_locked", kinds)   # keeps final zip locked

    def test_zero_questions_never_commits_and_stays_undone(self):
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 0, "issues": []}
        answers = [json.dumps(bnd),
                   "not json", "not json", "not json",   # Q chunk + verify tries
                   json.dumps([]),                        # A extract (empty ok)
                   json.dumps([]),                        # S extract
                   json.dumps(cross)]                     # cross-check
        model = _FakeModel(answers)
        r = self._runner(model)
        res = r.run(5, 13)
        self.assertFalse(res["locked"])
        self.assertFalse(res["committed"])
        self.assertFalse((qp.DATA_DIR / "questions.jsonl").exists())
        st = json.loads(qp.STATE_FILE.read_text())
        self.assertNotIn("OPH-001",
                         st.get("pdf_progress", {}).get("OPH", {})
                         .get("chapters_done", []))
        kinds = [json.loads(l)["kind"] for l in
                 (qp.DATA_DIR / "export_gate.jsonl").read_text().splitlines()
                 if l.strip()]
        self.assertIn("chapter_not_locked", kinds)

    def test_missing_block_boundary_refuses_and_never_locks(self):
        """An unanswered boundary question (e.g. solution block start missing)
        must ABORT with no writes at all -- a half-zoned chapter locking as
        'done' is exactly the silent failure the spec forbids."""
        bnd = {"question_block": {"start_page": 5}, "answer_key_block": {},
               "solution_block": {}, "confidence": "high"}
        model = _FakeModel([json.dumps(bnd)])
        r = self._runner(model)
        with self.assertRaises(RuntimeError):
            r.run(5, 9)
        self.assertFalse((qp.DATA_DIR / "questions.jsonl").exists())

    def test_answer_comes_only_from_answer_key_phase(self):
        """spec isolation rule: a solution-phase item that happens to mention
        a letter must never BECOME the answer; only the key table sets it."""
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        qs = [{"q_no": "1", "stem": "S?", "options": {"A": "a", "B": "b"},
               "has_figure": False, "figure_location": None, "source_page": 5,
               "text_confidence": "high"}]
        an = [{"q_no": "1", "correct_option": "B", "low_confidence": False}]
        so = [{"q_no": "1", "solution_text": "Answer is A because...",
               "has_figure": False, "figure_location": None,
               "source_page_range": [11, 11], "text_confidence": "high"}]
        ok = {"phase": "x", "total_entries_checked": 1,
              "all_verified": True, "mismatches": []}
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 1, "issues": []}
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            json.dumps(qs), json.dumps(ok),
                            json.dumps(an), json.dumps(ok),
                            json.dumps(so), json.dumps(ok),
                            json.dumps(cross)])
        r = self._runner(model)
        res = r.run(5, 13)
        self.assertTrue(res["committed"])
        row = [json.loads(l) for l in
               (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()][0]
        self.assertEqual(row["correct_options"], ["B"])   # key table wins
        # 2 options < 4 -> structural INCOMPLETE (flagged for human, not fixed)
        self.assertEqual(row["qa_status"], "INCOMPLETE")

    def test_printed_header_reask_recovers_dropped_block(self):
        """OPH-001 live finding: the model silently dropped q15's solution
        although the page PRINTS 'Solution to Question 15'. The text-layer
        header proves existence -> one targeted re-ask must recover it."""
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        qs = [{"q_no": "1", "stem": "S1?", "options": {"A": "a", "B": "b",
               "C": "c", "D": "d"}, "has_figure": False, "figure_location": None,
               "source_page": 5, "text_confidence": "high"},
              {"q_no": "2", "stem": "S2?", "options": {"A": "a", "B": "b",
               "C": "c", "D": "d"}, "has_figure": False, "figure_location": None,
               "source_page": 5, "text_confidence": "high"}]
        an = [{"q_no": "1", "correct_option": "A", "low_confidence": False},
              {"q_no": "2", "correct_option": "B", "low_confidence": False}]
        so = [{"q_no": "1", "solution_text": "Sol1.", "has_figure": False,
               "figure_location": None, "source_page_range": [11, 11],
               "text_confidence": "high"}]          # q2 SILENTLY MISSING
        ok = {"phase": "x", "total_entries_checked": 2,
              "all_verified": True, "mismatches": []}
        reasked = [{"q_no": "2", "solution_text": "Sol2 recovered.",
                    "has_figure": False, "figure_location": None,
                    "source_page_range": [12, 12], "text_confidence": "high"}]
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 2, "issues": []}
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            json.dumps(qs), json.dumps(ok),
                            json.dumps(an), json.dumps(ok),
                            json.dumps(so), json.dumps(ok),
                            json.dumps(reasked),       # the re-ask call
                            json.dumps(cross)])
        r = self._runner(model)
        r._printed_s_hdrs = {12: {1, 2}}   # page 12 PROVES q2's solution exists
        res = r.run(5, 13)
        self.assertTrue(res["locked"])
        rows = {json.loads(l)["id"]: json.loads(l) for l in
                (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
                if l.strip()}
        self.assertEqual(rows["OPH-001-002"]["solution"]["text"],
                         "Sol2 recovered.")
        self.assertIn("REASK_S", [lr["pass"] for lr in
                                  r.ledger_rows])

    def test_quota_pause_is_systemexit_and_saves_state(self):
        model = _FakeModel(self._answers())
        r = self._runner(model)
        self.state["calls_today"] = 10 ** 9     # pool-less brake: hard-spent
        with self.assertRaises(SystemExit):
            r.run(5, 13)
        st = json.loads(qp.STATE_FILE.read_text())
        self.assertEqual(st["calls_today"], 10 ** 9)   # state was persisted


class DriverTest(unittest.TestCase):
    def test_run_all_pauses_cleanly_when_runner_raises_quota(self):
        tmp = Path(tempfile.mkdtemp(prefix="bph_drv_"))
        orig = (qp.OUTPUT_ROOT, qp.DATA_DIR, qp.ASSETS_DIR, qp.STATE_FILE)
        qp.OUTPUT_ROOT = tmp
        qp.DATA_DIR = tmp / "data"
        qp.ASSETS_DIR = tmp / "assets"
        qp.STATE_FILE = tmp / "state.json"
        qp.DATA_DIR.mkdir(parents=True)
        saved = {}
        class _Rdr:
            pages = [1, 2, 3]
        class _Runner:
            def __init__(self, *a, **k):
                pass
            def run(self, a, b):
                raise bph.QuotaPaused()
        try:
            _patch(bph.gemini_keys, "init", lambda *a, **k: None, saved)
            _patch(bph.genai, "GenerativeModel", lambda *a, **k: object(), saved)
            _patch(bph.gemini_keys, "track", lambda m: m, saved)
            _patch(qp, "reset_daily_counter_if_needed", lambda s: None, saved)
            _patch(qp, "_load_declared_allowances", lambda s: None, saved)
            _patch(qp, "find_watermark_object_ids", lambda p: {1}, saved)
            _patch(qp, "PdfReader", lambda p: _Rdr(), saved)
            _patch(qp, "extract_toc_chapters", lambda p: [], saved)
            _patch(qp, "compute_page_ranges",
                   lambda toc, off, tot: [{"chapter_no": 1, "chapter_title": "t",
                                           "file_start": 1, "file_end": 3}],
                   saved)
            _patch(qp, "build_subject_bundle", lambda *a, **k: None, saved)
            _patch(qp, "_dedupe_questions_by_id", lambda p: 0, saved)
            _patch(bph, "ChapterRunner", _Runner, saved)
            with self.assertRaises(SystemExit):
                bph.run_all([{"subject": "OPH", "path": "x.pdf",
                              "page_offset": 0}], state={})
            self.assertTrue((qp.DATA_DIR / "chapters.json").exists())
            self.assertTrue(qp.STATE_FILE.exists())
        finally:
            for obj, name, old in saved["patches"]:
                setattr(obj, name, old)
            (qp.OUTPUT_ROOT, qp.DATA_DIR, qp.ASSETS_DIR, qp.STATE_FILE) = orig


def _patch(obj, name, new, saved):
    saved.setdefault("patches", []).append((obj, name, getattr(obj, name)))
    setattr(obj, name, new)


if __name__ == "__main__":
    unittest.main()
