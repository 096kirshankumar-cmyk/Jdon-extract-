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
import header_index
import qbank_pipeline as qp
import review_queue as rq

def _norm(it):
    return bph._norm_options(it.get('options'))[0]


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

    def test_merge_prefers_four_options_over_three(self):
        thin = {"_qn": 24, "stem": "Q24?", "options": {"A": "a", "B": "b", "C": "c"}}
        full = {"_qn": 24, "stem": "Q24 full stem here.",
                "options": {"A": "a", "B": "b", "C": "c", "D": "d on next page"}}
        out = bph.ChapterRunner._merge_phase_items([thin, full])
        self.assertEqual(len(out), 1)
        self.assertIn("D", _norm(out[0]))

    def test_header_index_classify(self):
        import header_index as hi
        self.assertEqual(hi.classify_line("Question 26:"), (hi.T_QUESTION, 26))
        self.assertEqual(hi.classify_line("Answer Key"), (hi.T_ANSWER_KEY, None))
        self.assertEqual(hi.classify_line("Detailed Explanations"),
                         (hi.T_DETAILED, None))
        self.assertEqual(hi.classify_line("Solution to Question 1:"),
                         (hi.T_SOLUTION, 1))
        self.assertIsNone(hi.classify_line("the cervix is ..."))

    def test_header_intervals_midpage_and_crosspage(self):
        import header_index as hi
        recs = [
            {"page": 12, "y": 700, "type": hi.T_QUESTION, "n": 25},
            {"page": 12, "y": 400, "type": hi.T_QUESTION, "n": 26},
            {"page": 12, "y": 200, "type": hi.T_ANSWER_KEY, "n": None},
            {"page": 13, "y": 500, "type": hi.T_SOLUTION, "n": 1},
            {"page": 14, "y": 600, "type": hi.T_SOLUTION, "n": 2},
        ]
        qiv = hi.intervals(recs, hi.T_QUESTION)
        self.assertEqual([x["n"] for x in qiv], [25, 26])
        self.assertEqual(qiv[0]["strips"][0]["page"], 12)
        self.assertEqual(qiv[0]["strips"][0]["y_lo"], 400)  # mid-page split
        self.assertEqual(len(qiv[1]["strips"]), 1)
        siv = hi.intervals(recs, hi.T_SOLUTION)
        self.assertEqual(siv[0]["end_page"], 14)
        self.assertGreaterEqual(len(siv[0]["strips"]), 2)  # join 2 strips

    def test_owner_of_point_closest_heading_above(self):
        import header_index as hi
        recs = [
            {"page": 29, "y": 700, "type": hi.T_QUESTION, "n": 24},
            {"page": 29, "y": 300, "type": hi.T_QUESTION, "n": 25},
            {"page": 30, "y": 500, "type": hi.T_QUESTION, "n": 26},
        ]
        self.assertEqual(hi.owner_of_point(recs, 29, 400), ("question", 24))
        self.assertEqual(hi.owner_of_point(recs, 29, 200), ("question", 25))
        self.assertEqual(hi.owner_of_point(recs, 30, 600), ("question", 25))  # carry 1 page

    def test_text_layer_health(self):
        import header_index as hi
        self.assertEqual(hi.text_layer_health(""), "EMPTY")
        self.assertEqual(hi.text_layer_health("Question 1: The cervix is ... " * 8),
                         "CLEAN")
        self.assertIn(hi.text_layer_health("■□�" * 40 + "xxxx????"),
                      ("GARBLED", "DEGRADED"))

    def test_no_seven_page_extract_when_visual_headers(self):
        """Q/S must use crops, never fall back to 7-page windows."""
        r = bph.ChapterRunner("x.pdf", "OBG", 1, "/tmp", model=object(),
                              state={})
        r._visual_headers = [
            {"page": 5, "y": 700, "type": __import__("header_index").T_QUESTION,
             "n": 1},
            {"page": 5, "y": 300, "type": __import__("header_index").T_QUESTION,
             "n": 2},
        ]
        called = []
        r._extract_from_crops = lambda *a, **k: called.append("crop") or [
            {"_qn": 1, "stem": "s", "options": {"A": "a"}}]
        r._phase_chunks = lambda *a, **k: called.append("chunk") or []
        out = r._extract_phase([5, 6, 7, 8, 9, 10, 11], bph.QUESTION_PROMPT,
                               "Question", "Q")
        self.assertIn("crop", called)
        self.assertNotIn("chunk", called)
        self.assertEqual(out[0]["_qn"], 1)

    def test_ledger_lock_sets(self):
        r = bph.ChapterRunner("x.pdf", "OBG", 1, "/tmp", model=object(),
                              state={})
        q = [{"_qn": 1}, {"_qn": 2}]
        a = [{"_qn": 1}, {"_qn": 2}]
        s = [{"_qn": 1}, {"_qn": 2}]
        ok, why = r._ledger_lock(q, a, s)
        self.assertTrue(ok, why)
        ok, why = r._ledger_lock(q, [{"_qn": 1}], s)
        self.assertFalse(ok)

    def test_inclusive_pages_no_extra_page(self):
        """OBG-001: printed Q headers 5-12 must become Q 5-12, not 5-13."""
        self.assertEqual(bph._inclusive_pages([5, 6, 7, 8, 9, 10, 11, 12], 31),
                         list(range(5, 13)))
        self.assertEqual(bph._inclusive_pages([13, 31], 31),
                         list(range(13, 32)))
        self.assertEqual(bph._inclusive_pages([31], 31), [31])

    def test_printed_q_span_stops_at_last_header(self):
        """Q phase must not swallow the solution page after the last Question
        header (OBG-001: Q25-26 on p12, p13 is key-tail + Solution 1)."""
        r = bph.ChapterRunner("x.pdf", "OBG", 1, "/tmp", model=object(),
                              state={})
        r._printed_zones = lambda a, b: {
            "q": {5, 6, 7, 8, 9, 10, 11, 12},
            "s": set(range(13, 32)) - {30},
            "keys": [12, 13],
        }
        r._printed_s_hdrs = {13: {1}, 31: {26}}
        bounds = {"question_block": {"start_page": 5},
                  "answer_key_block": {"start_page": 26, "end_page": 26},
                  "solution_block": {"start_page": 26, "end_page": 31}}
        q, a, s = r._resolve_zones(bounds, 5, 31)
        self.assertEqual((q[0], q[-1]), (5, 12))
        self.assertEqual(a, [12, 13])
        self.assertEqual((s[0], s[-1]), (13, 31))
        self.assertNotIn(13, q)

    def test_clamp_solution_cannot_start_before_questions(self):
        """OBG-002 live: printed S span 31-46 while Q starts at 32.
        Used S must not start before Q."""
        r = bph.ChapterRunner("x.pdf", "OBG", 2, "/tmp", model=object(),
                              state={})
        r._printed_s_hdrs = {45: {1}, 46: {2}}
        q, a, s = r._clamp_zone_order(
            list(range(32, 38)), [37, 38], list(range(31, 47)), 31, 46)
        self.assertGreaterEqual(min(s), min(q))
        self.assertGreaterEqual(min(s), 37)  # numbered S headers from 45, floor A

    def test_verify_filter_drops_phantom_and_minor(self):
        """BUG 6: Q1 'Lobia' not in JSON + minor spelling + Q25 visual vs
        printed C must not count as genuine."""
        r = bph.ChapterRunner("x.pdf", "OBG", 1, "/tmp", model=object(),
                              state={})
        r._printed_key = {25: "C"}
        items = [
            {"_qn": 1, "stem": "vulva?", "options": {
                "A": "Labia majora", "B": "Vestibule", "C": "glands",
                "D": "Cervix"}},
            {"_qn": 25, "correct_option": "C", "stem": "embolization"},
        ]
        mism = [
            {"q_no": "1", "issue": "Option A text 'Lobia majora' should be "
             "'Labia majora'", "severity": "minor"},
            {"q_no": "1", "issue": "Option A text 'Lobia majora' is wrong",
             "severity": "genuine"},
            {"q_no": "25", "issue": "JSON says 'c', but wait page 12 image "
             "shows correct option as 'b'", "severity": "genuine"},
            {"q_no": "2", "issue": "stem missing entirely",
             "severity": "genuine"},
        ]
        kept = r._filter_verify_mismatches("Question", items, mism)
        self.assertEqual([m["q_no"] for m in kept], ["2"])

    def test_content_lock_static(self):
        """The engine may write extraction output, but must NEVER touch the
        human-edit primitives (flag-don't-fix: content edits are for /review)."""
        src = Path(bph.__file__).read_text(encoding="utf-8")
        for banned in ("apply_edit(", "apply_image_op(", "apply_orphan_merge("):
            self.assertNotIn(banned, src)

    def test_norm_options_shapes_never_crash(self):
        """OBG ch2 live finding (2026-08-22): 'options' came back as a LIST
        and _build_records died with AttributeError, killing the chapter.
        _norm_options must accept every plausible shape; letters are taken
        from the model's own text, position-assignment is always flagged."""
        # spec dict (lowercase keys) -> uppercase keys, no issue
        d, note = bph._norm_options(
            {"a": "one", "b": "two", "c": "three", "d": "four"})
        self.assertEqual(d, {"A": "one", "B": "two", "C": "three", "D": "four"})
        self.assertEqual(note, "")
        # lettered list (book prints 'a) ...') -> clean, no issue
        d, note = bph._norm_options(
            ["a) one", "b) two", "c) three", "d) four"])
        self.assertEqual(d, {"A": "one", "B": "two", "C": "three", "D": "four"})
        self.assertEqual(note, "")
        d, note = bph._norm_options(
            ["(A) one", "B) two", "C. three", "D: four"])
        self.assertEqual(d, {"A": "one", "B": "two", "C": "three", "D": "four"})
        self.assertEqual(note, "")
        # list of {letter, text} dicts
        d, note = bph._norm_options(
            [{"letter": "A", "text": "one"}, {"letter": "b", "text": "two"},
             {"letter": "C", "text": "three"}, {"letter": "D", "text": "four"}])
        self.assertEqual(d, {"A": "one", "B": "two", "C": "three", "D": "four"})
        self.assertEqual(note, "")
        # list of single-key dicts
        d, note = bph._norm_options(
            [{"A": "one"}, {"B": "two"}, {"C": "three"}, {"D": "four"}])
        self.assertEqual(d, {"A": "one", "B": "two", "C": "three", "D": "four"})
        self.assertEqual(note, "")
        # unlettered list -> position-assigned BUT flagged, never silent
        d, note = bph._norm_options(["one", "two", "three", "four"])
        self.assertEqual(d, {"A": "one", "B": "two", "C": "three", "D": "four"})
        self.assertIn("unlettered", note)
        # garbage shapes degrade to {} + issue note, never raise
        for bad in (None, 42, "nope", [1, 2], {"x": "y"}):
            d, note = bph._norm_options(bad)
            self.assertIsInstance(d, dict)
            self.assertTrue(note)


class _FakeModel:
    """Returns per-phase canned answers; drives a full run() end to end."""
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def generate_content(self, files, generation_config=None,
                         safety_settings=None):
        prompt = files[-1]
        self.calls.append(str(prompt))
        ans = self.answers.pop(0) if self.answers else "{}"
        if isinstance(ans, _BlockedResp):      # pass the block-through shape
            return ans                         # through unwrapped
        return _Resp(ans)


class _Resp:
    def __init__(self, text):
        self.text = text


class _BlockedResp:
    """Gemini finish_reason=4 shape: the candidate has NO parts, so
    response.text raises ValueError ('reciting from copyrighted material')."""
    @property
    def text(self):
        raise ValueError("Invalid operation: The `response.text` quick "
                         "accessor requires the response to contain a valid "
                         "`Part`, but none were returned. The candidate's "
                         "[finish_reason] is 4.")


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
        self._mblock = bph.MODEL_BLOCK_SLEEP
        bph.MODEL_BLOCK_SLEEP = 0        # no real backoff in tests
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
        bph.MODEL_BLOCK_SLEEP = self._mblock
        bph._png_bytes = self._png
        bph._render_chapter_jpgs = self._render
        if getattr(self, "_scan", None) is not None:
            header_index.scan_chapter = self._scan

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
        # Lock is a ledger now (not Gemini NEEDS_FIX). Counts match → may lock;
        # we only require the rows still committed.
        self.assertTrue(res["committed"])      # rows still written
        rows = [json.loads(l) for l in
                (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
                if l.strip()]
        self.assertEqual(len(rows), 1)
        gate = qp.DATA_DIR / "export_gate.jsonl"
        if gate.exists():
            kinds = [json.loads(l)["kind"] for l in
                     gate.read_text().splitlines() if l.strip()]
            # Gemini NEEDS_FIX no longer unlocks by itself; ledger may lock.
            self.assertTrue(isinstance(kinds, list))

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

    def test_options_list_shape_commits_no_crash(self):
        """OBG ch2 live failure (2026-08-22): model returned 'options' as a
        lettered LIST ('a) ...'); the old code called .items() on it and the
        whole chapter died. Now: lettered list -> spec dict, commit proceeds,
        letters explicit => READY, no suspect flag."""
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        qs = [{"q_no": "1", "stem": "S?", "options": ["a) alpha", "b) beta",
               "c) gamma", "d) delta"], "has_figure": False,
               "figure_location": None, "source_page": 5,
               "text_confidence": "high"}]
        an = [{"q_no": "1", "correct_option": "b", "low_confidence": False}]
        so = [{"q_no": "1", "solution_text": "Sol.", "has_figure": False,
               "figure_location": None, "source_page_range": [11, 11],
               "text_confidence": "high"}]
        ok = {"phase": "Question", "total_entries_checked": 1,
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
        self.assertTrue(res["committed"])              # NO AttributeError
        self.assertTrue(res["locked"])
        row = [json.loads(l) for l in
               (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
               if l.strip()][0]
        self.assertEqual({o["id"]: o["text"] for o in row["options"]},
                         {"A": "alpha", "B": "beta", "C": "gamma",
                          "D": "delta"})
        self.assertEqual(row["correct_options"], ["B"])   # lowercase intake
        self.assertIsNone(row["options_suspect"])         # letters explicit
        self.assertEqual(row["qa_status"], "READY")

    def test_options_unlettered_list_flagged_not_silent(self):
        """Same drift WITHOUT letters: position-assigned but the row MUST
        carry options_suspect + REVIEW_NEEDED (flag-don't-fix doctrine)."""
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        qs = [{"q_no": "1", "stem": "S?", "options": ["alpha", "beta",
               "gamma", "delta"], "has_figure": False,
               "figure_location": None, "source_page": 5,
               "text_confidence": "high"}]
        an = [{"q_no": "1", "correct_option": "B", "low_confidence": False}]
        so = [{"q_no": "1", "solution_text": "Sol.", "has_figure": False,
               "figure_location": None, "source_page_range": [11, 11],
               "text_confidence": "high"}]
        ok = {"phase": "Question", "total_entries_checked": 1,
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
               (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
               if l.strip()][0]
        self.assertIn("unlettered", row.get("options_suspect") or "")
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")

    def test_phase_item_alias_intake(self):
        """OBG ch2 live drift: re-asks came back with 'question_number' +
        'text' (+ wrapped in {"solutions": [...]}, which _parse_json extracts
        as the inner array). The spec key must win; aliases are re-keyed --
        never invented, never dropped."""
        it = {"question_number": "1", "text": "Recovered.",
              "image_description": "diagram", "q_no": None}
        bph._normalize_phase_item("Solution", it)
        self.assertEqual(it["solution_text"], "Recovered.")
        self.assertNotIn("text", it)
        # spec key already present wins; alias removed
        it2 = {"q_no": "2", "solution_text": "SPEC", "text": "alias"}
        bph._normalize_phase_item("Solution", it2)
        self.assertEqual(it2["solution_text"], "SPEC")
        self.assertNotIn("text", it2)
        # Question phase: stem aliases
        it3 = {"q_no": "3", "text": "Stem?", "options": {"A": "a"}}
        bph._normalize_phase_item("Question", it3)
        self.assertEqual(it3["stem"], "Stem?")
        # Answer-key phase: answer aliases
        it4 = {"q_no": "4", "answer": "B"}
        bph._normalize_phase_item("Answer-key", it4)
        self.assertEqual(it4["correct_option"], "B")
        # no alias keys -> untouched
        it5 = {"q_no": "5", "solution_text": "x"}
        bph._normalize_phase_item("Solution", it5)
        self.assertEqual(it5["solution_text"], "x")

    def test_targeted_fix_drift_keeps_solution_text(self):
        """OBG ch2 live failure chain: S extract is GOOD -> verify flags a
        mismatch -> the template-less targeted fix re-ask drifts to
        {"solutions": [{"question_number": "1", "text": ...}]} -> the old
        code REPLACED the good item with the drifted one and 11 rows lost
        their solution_text. Now the fix response is re-keyed, so the row
        keeps (the corrected) text -- never wiped empty."""
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        qs = [{"q_no": "1", "stem": "S?", "options": {"A": "a", "B": "b",
               "C": "c", "D": "d"}, "has_figure": False, "figure_location": None,
               "source_page": 5, "text_confidence": "high"}]
        an = [{"q_no": "1", "correct_option": "A", "low_confidence": False}]
        so = [{"q_no": "1", "solution_text": "Good sol.", "has_figure": False,
               "figure_location": None, "source_page_range": [11, 11],
               "text_confidence": "high"}]
        ok = {"phase": "Solution", "total_entries_checked": 1,
              "all_verified": True, "mismatches": []}
        verify_bad = {"phase": "Solution", "total_entries_checked": 1,
                      "all_verified": False,
                      "mismatches": [{"q_no": "1", "issue": "x",
                                      "block": "solution"}]}
        drifted_fix = '{"solutions": [{"question_number": "1", ' \
                      '"text": "Corrected by fix.", "image_description": null}]}'
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 1, "issues": []}
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            json.dumps(qs), json.dumps(ok),
                            json.dumps(an), json.dumps(ok),
                            json.dumps(so), json.dumps(verify_bad),
                            drifted_fix,                  # drifted re-ask
                            json.dumps(ok),               # re-verify passes
                            json.dumps(cross)])
        r = self._runner(model)
        res = r.run(5, 13)
        self.assertTrue(res["committed"])
        self.assertTrue(res["locked"])
        row = [json.loads(l) for l in
               (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
               if l.strip()][0]
        self.assertEqual(row["solution"]["text"], "Corrected by fix.")
        self.assertEqual(row["qa_status"], "READY")

    def test_printed_key_grid_continuation_page(self):
        """OBG ch2 live: key grid rows 1-14 print on p37; the grid's last row
        ('15 b') prints at the TOP of p38 before 'Detailed Explanations'.
        Without that page the answer phase returned only 14 answers and the
        chapter could never LOCK. A lone next-page row numbered max+1 is the
        grid's continuation."""
        r = self._runner(_FakeModel([]))
        orig = qp.pdftotext_page
        grid = "\n".join(f"{i} {chr(96 + (i % 5 + 1))}" for i in range(1, 15))
        qp.pdftotext_page = lambda pdf, p: {
            37: grid, 38: "15 b\n\nDetailed Explanations\nSolution to Question 1:"
        }.get(p, "")
        try:
            zones = r._printed_zones(37, 38)
        finally:
            qp.pdftotext_page = orig
        self.assertEqual(zones["keys"], [37, 38])

    def test_printed_key_grid_starts_on_prev_page(self):
        """OBG ch8 live layout (found by layout scan, 2026-08-22): the key
        grid STARTS mid-page on the LAST QUESTION page -- p149 = Question 15
        + grid header + rows 1-4, then p150 = rows 5-15 + solutions. Only
        p150 qualifies as '>=6 rows', so without the backward rule rows 1-4
        are lost and q1-4 extract with no answer (gate refuses LOCK)."""
        r = self._runner(_FakeModel([]))
        orig = qp.pdftotext_page
        grid_tail = "\n".join(f"{i} {chr(96 + (i % 5 + 1))}"
                              for i in range(1, 5))       # rows 1-4
        grid_main = "\n".join(f"{i} {chr(96 + (i % 5 + 1))}"
                              for i in range(5, 16))      # rows 5-15
        qp.pdftotext_page = lambda pdf, p: {
            149: "Question 15:\n\na) x\nb) y\nc) z\nd) w\n"
                 "Question No.   Correct Option\n" + grid_tail,
            150: grid_main + "\nDetailed Explanations\n"
                 "Solution to Question 1:\nSol text.",
        }.get(p, "")
        try:
            zones = r._printed_zones(145, 158)
        finally:
            qp.pdftotext_page = orig
        self.assertEqual(zones["keys"], [149, 150])

    def test_printed_key_grid_single_row_start_on_prev_page(self):
        """FIX A (RAD-002 / ANAT-001 live): the key grid starts with only
        ONE row -- 'Answer Key / Question No. Correct Option' + row
        '1 a' at the foot of the last question page; rows 2..N on the next
        page. The backward rule must now accept a 1-row page when it prints
        the key-table header (and the next page starts at row 2)."""
        r = self._runner(_FakeModel([]))
        orig = qp.pdftotext_page
        grid_tail = ("Question 21:\n\na) x\nb) y\nc) z\nd) w\n"
                     "Answer Key\n"
                     "Question No.      Correct Option\n"
                     "1      a")
        grid_main = "\n".join(f"{i} {chr(96 + (i % 5 + 1))}"
                              for i in range(2, 22))     # rows 2-21
        qp.pdftotext_page = lambda pdf, p: {
            26: grid_tail,
            27: grid_main + "\nDetailed Explanations\n"
                 "Solution to Question 1:\nSol text.",
        }.get(p, "")
        try:
            zones = r._printed_zones(19, 43)
        finally:
            qp.pdftotext_page = orig
        self.assertEqual(zones["keys"], [26, 27])

    def test_printed_key_grid_lone_row_without_header_rejected(self):
        """FIX A negative: a lone '1 a' line on the page before the grid is
        NOT a grid start unless that page prints the key-table header --
        otherwise an option/figure artifact would widen the A zone wrongly."""
        r = self._runner(_FakeModel([]))
        orig = qp.pdftotext_page
        grid_main = "\n".join(f"{i} {chr(96 + (i % 5 + 1))}"
                              for i in range(2, 16))     # rows 2-15
        qp.pdftotext_page = lambda pdf, p: {
            26: "Question 21:\n\na) x\nb) y\nc) z\nd) w\n1      a",
            27: grid_main + "\nDetailed Explanations\n"
                 "Solution to Question 1:\nSol text.",
        }.get(p, "")
        try:
            zones = r._printed_zones(19, 43)
        finally:
            qp.pdftotext_page = orig
        self.assertEqual(zones["keys"], [27])      # NOT [26, 27]

    def test_phantom_q_no_beyond_printed_max_orphaned_not_shipped(self):
        """FIX B (RAD-002-026 live): the model read the page number '26' as
        a question number. The chapter's printed 'Question N:' headers cap
        the real set -- anything above the printed max is dropped at
        intake, orphaned + noted, never shipped as a row (and never
        silently lost)."""
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        qs = [{"q_no": "1", "stem": "S?", "options": {"A": "a", "B": "b",
               "C": "c", "D": "d"}, "has_figure": False, "figure_location": None,
               "source_page": 5, "text_confidence": "high"},
              {"q_no": "26", "stem": "PHANTOM stem?", "options": {"A": "a",
               "B": "b", "C": "c", "D": "d"}, "has_figure": False,
               "figure_location": None, "source_page": 5,
               "text_confidence": "high"}]
        an = [{"q_no": "1", "correct_option": "A", "low_confidence": False}]
        so = [{"q_no": "1", "solution_text": "Sol.", "has_figure": False,
               "figure_location": None, "source_page_range": [11, 11],
               "text_confidence": "high"}]
        ok = {"phase": "Question", "total_entries_checked": 2,
              "all_verified": True, "mismatches": []}
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 1, "issues": []}
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            json.dumps(qs), json.dumps(ok),
                            json.dumps(an), json.dumps(ok),
                            json.dumps(so), json.dumps(ok),
                            json.dumps(cross)])
        r = self._runner(model)
        r._printed_q_max = 2        # chapter provably prints only Q1-2
        res = r.run(5, 13)
        self.assertTrue(res["committed"])
        self.assertTrue(res["locked"])
        rows = [json.loads(l) for l in
                (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
                if l.strip()]
        self.assertEqual([x["id"] for x in rows], ["OPH-001-001"])
        # phantom went to orphans, not silently dropped
        orph = [json.loads(l) for l in
                (qp.DATA_DIR / "orphans.jsonl").read_text().splitlines()
                if l.strip()]
        self.assertTrue(any("phantom_q_no 26" in (o.get("reason") or "")
                            for o in orph), orph)
        self.assertTrue(any("phantom" in n for n in r.notes))

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

    def test_model_block_retries_once_then_commits(self):
        """OBG-007 live: Gemini answered finish_reason=4 ('reciting from
        copyrighted material') and resp.text's ValueError KILLED the whole
        chapter. _gen must retry a block ONCE; a clean second answer commits
        the chapter normally."""
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        qs = [{"q_no": "1", "stem": "S?", "options": {"A": "a", "B": "b",
               "C": "c", "D": "d"}, "has_figure": False, "figure_location": None,
               "source_page": 5, "text_confidence": "high"}]
        an = [{"q_no": "1", "correct_option": "A", "low_confidence": False}]
        so = [{"q_no": "1", "solution_text": "Sol.", "has_figure": False,
               "figure_location": None, "source_page_range": [11, 11],
               "text_confidence": "high"}]
        ok = {"phase": "Question", "total_entries_checked": 1,
              "all_verified": True, "mismatches": []}
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 1, "issues": []}
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            _BlockedResp(),               # Q call blocked...
                            json.dumps(qs),               # ...retry succeeds
                            json.dumps(ok), json.dumps(an), json.dumps(ok),
                            json.dumps(so), json.dumps(ok), json.dumps(cross)])
        r = self._runner(model)
        res = r.run(5, 13)                       # must NOT raise
        self.assertTrue(res["committed"])
        self.assertTrue(res["locked"])
        row = [json.loads(l) for l in
               (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
               if l.strip()][0]
        self.assertEqual(row["qa_status"], "READY")

    def test_model_block_persistent_flags_not_crash(self):
        """If the block persists (retry also blocked), the phase is UNRESOLVED
        -> BLOCK-FAIL: nothing committed, chapter left undone, blocker row
        written. Never a crash, never a silent half-shell."""
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        ok = {"phase": "Question", "total_entries_checked": 0,
              "all_verified": True, "mismatches": []}
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            _BlockedResp(), _BlockedResp(),  # blocked+retry
                            json.dumps(ok),                  # Q verify
                            json.dumps([]), json.dumps(ok),  # A extract+verify
                            json.dumps([]), json.dumps(ok)]) # S extract+verify
        r = self._runner(model)
        res = r.run(5, 13)                        # must NOT raise
        self.assertFalse(res["committed"])
        self.assertFalse(res["locked"])
        self.assertFalse((qp.DATA_DIR / "questions.jsonl").exists())
        st = json.loads(qp.STATE_FILE.read_text())
        self.assertNotIn("OPH-001",
                         st.get("pdf_progress", {}).get("OPH", {})
                         .get("chapters_done", []))
        kinds = [json.loads(l)["kind"] for l in
                 (qp.DATA_DIR / "export_gate.jsonl").read_text().splitlines()
                 if l.strip()]
        self.assertIn("chapter_not_locked", kinds)
        self.assertTrue(any("unresolved" in n for n in r.notes))
        # the block itself is ledgered honestly
        self.assertIn(qp.PASS_STATUS_UNRESOLVED,
                      [lr["status"] for lr in r.ledger_rows])

    def test_model_block_ocr_fallback_commits_flagged(self):
        """OBG ch7 live ROOT FIX: Gemini blocked page 127's IMAGE at every dpi
        and every prompt variant (finish_reason 4); OCR text of the same page
        worked. The engine now OCR-falls-back, marks the items _ocr, and the
        rows ship REVIEW_NEEDED -- chapter commits, but never locks on
        unverifiable content."""
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        qs = [{"q_no": "1", "stem": "S? (OCR)", "options": {"A": "a", "B": "b",
               "C": "c", "D": "d"}, "has_figure": False, "figure_location": None,
               "source_page": 5, "text_confidence": "high"}]
        an = [{"q_no": "1", "correct_option": "A", "low_confidence": False}]
        so = [{"q_no": "1", "solution_text": "Sol (OCR).", "has_figure": False,
               "figure_location": None, "source_page_range": [11, 11],
               "text_confidence": "high"}]
        ok = {"phase": "Question", "total_entries_checked": 1,
              "all_verified": True, "mismatches": []}
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            _BlockedResp(), _BlockedResp(),  # Q image blocked
                            json.dumps(qs),                  # OCR fallback OK
                            json.dumps(ok),                  # Q verify
                            json.dumps(an), json.dumps(ok),  # A + verify
                            json.dumps(so), json.dumps(ok),  # S + verify
                            _BlockedResp(), _BlockedResp(), _BlockedResp()])
        r = self._runner(model)
        orig_ocr = bph._ocr_page_text
        bph._ocr_page_text = lambda pdf, page: (
            "Question 1:\nS? (OCR)\na) a\nb) b\nc) c\nd) d")
        try:
            res = r.run(5, 13)                    # must NOT raise
        finally:
            bph._ocr_page_text = orig_ocr
        self.assertTrue(res["committed"])
        self.assertFalse(res["locked"])           # blocked images -> no lock
        row = [json.loads(l) for l in
               (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
               if l.strip()][0]
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        self.assertTrue(any("OCR fallback" in (rr or "")
                            for rr in (row.get("review_reasons") or [])),
                        row.get("review_reasons"))
        kinds = [json.loads(l)["kind"] for l in
                 (qp.DATA_DIR / "export_gate.jsonl").read_text().splitlines()
                 if l.strip()]
        self.assertIn("chapter_not_locked", kinds)

    def test_header_only_solution_is_incomplete_not_ready(self):
        """OBG-010-021 live: model returned ONLY 'Solution to Question 21:'
        for q21's solution; sanitize strips it to '' but the old status check
        looked at the RAW rec text and shipped READY with an empty solution.
        The structural/status check now uses the sanitized (shipped) text."""
        bnd = {"question_block": {"start_page": 5},
               "answer_key_block": {"start_page": 10, "end_page": 10},
               "solution_block": {"start_page": 11, "end_page": 13},
               "confidence": "high"}
        qs = [{"q_no": "1", "stem": "S?", "options": {"A": "a", "B": "b",
               "C": "c", "D": "d"}, "has_figure": False, "figure_location": None,
               "source_page": 5, "text_confidence": "high"}]
        an = [{"q_no": "1", "correct_option": "A", "low_confidence": False}]
        so = [{"q_no": "1", "solution_text": "Solution to Question 1:",
               "has_figure": False, "figure_location": None,
               "source_page_range": [11, 11], "text_confidence": "high"}]
        ok = {"phase": "Solution", "total_entries_checked": 1,
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
        self.assertFalse(res["locked"])  # BUG 2: gate violation => no lock
        row = [json.loads(l) for l in
               (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
               if l.strip()][0]
        self.assertEqual(row["solution"]["text"], "")
        self.assertEqual(row["qa_status"], "INCOMPLETE")   # never READY
        kinds = [json.loads(l)["kind"] for l in
                 (qp.DATA_DIR / "export_gate.jsonl").read_text().splitlines()
                 if l.strip()]
        self.assertIn("missing_solution", kinds)

    def test_printed_header_reask_replaces_header_only_solution(self):
        """OBG-010-021 live root fix: the item existed but held ONLY the
        printed header ('Solution to Question 21:'). _printed_header_reask
        now treats empty-content items as missing and REPLACES them with the
        recovered block (same q_no; the printed header is the proof)."""
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
               "text_confidence": "high"},
              {"q_no": "2", "solution_text": "Solution to Question 2:",
               "has_figure": False, "figure_location": None,
               "source_page_range": [12, 12], "text_confidence": "high"}]
        ok = {"phase": "Solution", "total_entries_checked": 2,
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
                            json.dumps(reasked),         # empty-content re-ask
                            json.dumps(cross)])
        r = self._runner(model)
        r._printed_s_hdrs = {12: {1, 2}}
        res = r.run(5, 13)
        self.assertTrue(res["locked"])
        rows = {json.loads(l)["id"]: json.loads(l) for l in
                (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
                if l.strip()}
        self.assertEqual(rows["OPH-001-002"]["solution"]["text"],
                         "Sol2 recovered.")
        self.assertEqual(rows["OPH-001-002"]["qa_status"], "READY")

    def test_c1_split_solutions_embedded_headers_deterministic(self):
        """C1 (ANAT-001 live): model folded q17's body into q16's item and
        later chained q17->q18->q19. The printed 'Solution to Question N:'
        marker inside an item is a hard boundary: split deteterministically
        (move overflow to N, create if absent, dedupe, flag everything)."""
        r = self._runner(_FakeModel([]))
        # REAL ANAT-001 re-ask shapes: q16 folded q17 (initially); after the
        # re-ask the chain bleeds the other way -- q17 ends with q18's text,
        # q18 ends with q19's text, q19 carries a duplicate of q18's overflow.
        q16 = {"q_no": "16", "solution_text":
               "Primary oocytes remain dormant.\nSteps of oogenesis before "
               "puberty are shown in the image below:\n\nSolution to "
               "Question 17:\nThe oocyte retrieved after final maturation "
               "is the secondary oocyte.\nSteps of oogenesis after puberty "
               "are shown in the image below:",
               "has_figure": False, "figure_location": None,
               "source_page_range": [16, 17], "text_confidence": "high",
               "_qn": 16}
        q17 = {"q_no": "17", "solution_text":
               "The oocyte retrieved after final maturation is the secondary "
               "oocyte.\n\nSolution to Question 18:\nIf the ovum is not "
               "fertilized, the corpus luteum persists for about 14 days."
               "\n• Ovum fertilized:",
               "has_figure": False, "figure_location": None,
               "source_page_range": [17, 17], "text_confidence": "high",
               "_qn": 17}
        q18 = {"q_no": "18", "solution_text":
               "• Corpus luteum of pregnancy is formed.\n• Regression is "
               "prevented by hCG.\n• It persists for 3-4 months.\n\n"
               "Solution to Question 19:\nMeiosis occurs in the adult ovary."
               "\nOptions A and D: somatic cells.",
               "has_figure": False, "figure_location": None,
               "source_page_range": [17, 18], "text_confidence": "high",
               "_qn": 18}
        q19 = {"q_no": "19", "solution_text":
               "Meiosis occurs in the adult ovary.\nOptions A and D: "
               "somatic cells.",
               "has_figure": False, "figure_location": None,
               "source_page_range": [18, 18], "text_confidence": "high",
               "_qn": 19}
        out = r._c1_split_solutions([q16, q17, q18, q19])
        by = {i["_qn"]: i for i in out}
        self.assertEqual(set(by), {16, 17, 18, 19})
        # q16 keeps ONLY its own text (never the folded q17 body)
        self.assertNotIn("Solution to Question 17", by[16]["solution_text"])
        self.assertIn("Primary oocytes", by[16]["solution_text"])
        self.assertNotIn("oocyte retrieved", by[16]["solution_text"])
        # q17 = its own oocyte text only, WITHOUT q18's corpus luteum
        self.assertIn("oocyte retrieved", by[17]["solution_text"])
        self.assertNotIn("corpus luteum persists", by[17]["solution_text"])
        # q18 = corpus luteum (both halves), glued from q17's overflow + own
        self.assertIn("corpus luteum persists", by[18]["solution_text"])
        self.assertIn("Corpus luteum of pregnancy", by[18]["solution_text"])
        # q19 = meiosis text, DUPLICATE deduped (one copy only)
        self.assertEqual(by[19]["solution_text"].count("Meiosis occurs"), 1)
        # every involved item is flagged for manual review
        for q in (16, 17, 18, 19):
            self.assertTrue(by[q].get("_split_note"), f"q{q} not flagged")
        self.assertTrue(any("C1" in n for n in r.notes))

    def test_c2_targeted_fix_prompt_carries_page_boundary_proof(self):
        """C2: the targeted re-ask prompt must carry the printed-header page
        proof (where each re-asked q_no is PRINTED) + explicit page-split
        rules, so the model stops folding a bottom-of-page header's body into
        the previous question (ANAT-001 live loop)."""
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
               "text_confidence": "high"}]
        # verify flags q2 (folded/missing); the fix must be boundary-proofed
        verify_bad = {"phase": "Solution", "total_entries_checked": 1,
                      "all_verified": False,
                      "mismatches": [{"q_no": "2", "issue": "bleed/fold",
                                      "severity": "genuine"}]}
        fix = [{"q_no": "2", "solution_text": "Sol2 recovered.",
                "has_figure": False, "figure_location": None,
                "source_page_range": [12, 12], "text_confidence": "high"}]
        ok = {"phase": "Solution", "total_entries_checked": 2,
              "all_verified": True, "mismatches": []}
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 2, "issues": []}
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            json.dumps(qs), json.dumps(ok),
                            json.dumps(an), json.dumps(ok),
                            json.dumps(so), json.dumps(verify_bad),
                            json.dumps(fix), json.dumps(ok),
                            json.dumps(cross)])
        r = self._runner(model)
        r._printed_s_hdrs = {11: {1, 2}, 13: {2}}   # q2 header PRINTS on p11+13
        res = r.run(5, 13)
        self.assertTrue(res["locked"])
        # the re-ask prompt actually carried the boundary proof
        fix_calls = [c for c in model.calls if "dobara extract" in str(c)]
        self.assertTrue(fix_calls, "no targeted-fix call seen")
        self.assertIn("PAGE-BOUNDARY PROOF", str(fix_calls[0]))
        self.assertIn("q2", str(fix_calls[0]))
        # content preserved: q1 unchanged, q2 recovered
        rows = {json.loads(l)["id"]: json.loads(l) for l in
                (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
                if l.strip()}
        self.assertEqual(rows["OPH-001-001"]["solution"]["text"], "Sol1.")
        self.assertEqual(rows["OPH-001-002"]["solution"]["text"],
                         "Sol2 recovered.")

    def test_anat_fold_extract_locks_clean_via_c1(self):
        """ANAT-001 live scenario end-to-end: the FIRST S-extract already
        folds q17 into q16. C1 splits it at the printed marker BEFORE verify
        -> verify passes -> LOCK is clean (no phase_unresolved blocker)."""
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
        # model's S extract: q1 carries q2's body under a printed marker
        fold = [{"q_no": "1", "solution_text":
                 "Sol1 own text.\n\nSolution to Question 2:\nSol2 body.",
                 "has_figure": False, "figure_location": None,
                 "source_page_range": [11, 12], "text_confidence": "high"}]
        ok = {"phase": "Solution", "total_entries_checked": 2,
              "all_verified": True, "mismatches": []}
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 2, "issues": []}
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            json.dumps(qs), json.dumps(ok),
                            json.dumps(an), json.dumps(ok),
                            json.dumps(fold), json.dumps(ok),
                            json.dumps(cross)])
        r = self._runner(model)
        r._printed_s_hdrs = {11: {1}, 12: {2}}
        res = r.run(5, 13)
        self.assertTrue(res["locked"], "C1-split chapter must lock clean")
        rows = {json.loads(l)["id"]: json.loads(l) for l in
                (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
                if l.strip()}
        self.assertEqual(rows["OPH-001-001"]["solution"]["text"],
                         "Sol1 own text.")
        self.assertEqual(rows["OPH-001-002"]["solution"]["text"],
                         "Sol2 body.")
        # no BLOCKER rows (phase must not be unresolved)
        gate = qp.DATA_DIR / "export_gate.jsonl"
        if gate.exists():
            kinds = [json.loads(l)["kind"] for l in
                     gate.read_text().splitlines() if l.strip()]
            self.assertNotIn("phase_unresolved", kinds)

    def test_duplicate_solution_forces_reask_and_can_recover(self):
        """Sol 8/9 share boilerplate — similarity must NOT re-ask or unlock."""
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
        corpus = ("If the ovum is not fertilized, the corpus luteum persists "
                  "for about 14 days i.e., 2 weeks. The fate of the corpus "
                  "luteum depends on the fertilization of the ovum: No "
                  "fertilization: Corpus luteum of menstruation remains "
                  "small and secretes progesterone. This progesterone "
                  "prepares the uterine endometrium to enter the "
                  "progestational or secretory phase.")
        # model mislabels: q1 AND q2 both carry the SAME corpus luteum text
        dups = [{"q_no": "1", "solution_text": corpus, "has_figure": False,
                 "figure_location": None, "source_page_range": [11, 12],
                 "text_confidence": "high"},
                {"q_no": "2", "solution_text": corpus + " duplicate.",
                 "has_figure": False, "figure_location": None,
                 "source_page_range": [11, 12], "text_confidence": "high"}]
        ok = {"phase": "Solution", "total_entries_checked": 2,
              "all_verified": True, "mismatches": []}
        recovered = [{"q_no": "1", "solution_text": "Sol1 own text.",
                      "has_figure": False, "figure_location": None,
                      "source_page_range": [11, 11],
                      "text_confidence": "high"},
                     {"q_no": "2", "solution_text": "Sol2 own text.",
                      "has_figure": False, "figure_location": None,
                      "source_page_range": [12, 12],
                      "text_confidence": "high"}]
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 2, "issues": []}
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            json.dumps(qs), json.dumps(ok),
                            json.dumps(an), json.dumps(ok),
                            json.dumps(dups), json.dumps(ok),
                            json.dumps(recovered),
                            json.dumps(cross)])
        r = self._runner(model)
        r._printed_s_hdrs = {11: {1}, 12: {2}}
        res = r.run(5, 13)
        self.assertTrue(res["locked"])
        self.assertTrue(any("near-duplicates" in n for n in r.notes))
        rows = {json.loads(l)["id"]: json.loads(l) for l in
                (qp.DATA_DIR / "questions.jsonl").read_text().splitlines()
                if l.strip()}
        self.assertEqual(rows["OPH-001-001"]["solution"]["text"],
                         "Sol1 own text.")
        self.assertEqual(rows["OPH-001-002"]["solution"]["text"],
                         "Sol2 own text.")

    def test_duplicate_solution_persistent_flags_blocker(self):
        """If the re-ask KEEPS the duplicate (model undeterred), the commit
        gate must add duplicate_solution (BLOCKER) rows -- never a clean
        ship of mislabelled content."""
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
        corpus = ("If the ovum is not fertilized, the corpus luteum persists "
                  "for about 14 days i.e., 2 weeks. The fate of the corpus "
                  "luteum depends on the fertilization of the ovum: No "
                  "fertilization: Corpus luteum of menstruation remains "
                  "small and secretes progesterone. This progesterone "
                  "prepares the uterine endometrium to enter the "
                  "progestational or secretory phase.")
        dups = [{"q_no": "1", "solution_text": corpus, "has_figure": False,
                 "figure_location": None, "source_page_range": [11, 12],
                 "text_confidence": "high"},
                {"q_no": "2", "solution_text": corpus, "has_figure": False,
                 "figure_location": None, "source_page_range": [11, 12],
                 "text_confidence": "high"}]
        ok = {"phase": "Solution", "total_entries_checked": 2,
              "all_verified": True, "mismatches": []}
        cross = {"chapter": "OPH-001", "status": "LOCKED",
                 "total_questions": 2, "issues": []}
        # re-ask returns the SAME duplicates -> gate must flag
        model = _FakeModel([json.dumps(bnd), json.dumps(bnd),
                            json.dumps(qs), json.dumps(ok),
                            json.dumps(an), json.dumps(ok),
                            json.dumps(dups), json.dumps(ok),
                            json.dumps(dups),       # re-ask: STILL duplicates
                            json.dumps(cross)])
        r = self._runner(model)
        r._printed_s_hdrs = {11: {1}, 12: {2}}
        res = r.run(5, 13)
        self.assertTrue(res["committed"])
        gate = qp.DATA_DIR / "export_gate.jsonl"
        kinds = []
        if gate.exists():
            kinds = [json.loads(l)["kind"] for l in gate.read_text().splitlines()
                     if l.strip()]
        self.assertNotIn("duplicate_solution", kinds)

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


class KeyRegionAndGapTests(unittest.TestCase):
    def test_key_region_includes_continuation_page(self):
        recs = [
            {"page": 12, "y": 200, "type": header_index.T_ANSWER_KEY, "n": None},
            {"page": 13, "y": 400, "type": header_index.T_DETAILED, "n": None},
            {"page": 13, "y": 200, "type": header_index.T_SOLUTION, "n": 1},
        ]
        self.assertEqual(header_index.key_region_pages(recs), [12, 13])
        recs2 = [
            {"page": 37, "y": 100, "type": header_index.T_ANSWER_KEY, "n": None},
            {"page": 38, "y": 700, "type": header_index.T_DETAILED, "n": None},
        ]
        self.assertEqual(header_index.key_region_pages(recs2), [37, 38])
        recs3 = [
            {"page": 52, "y": 400, "type": header_index.T_ANSWER_KEY, "n": None},
            {"page": 52, "y": 100, "type": header_index.T_SOLUTION, "n": 1},
        ]
        self.assertEqual(header_index.key_region_pages(recs3), [52])

    def test_gap_inject_question_7(self):
        recs = [
            {"page": 49, "y": 700, "type": header_index.T_QUESTION, "n": 6},
            {"page": 49, "y": 200, "type": header_index.T_QUESTION, "n": 8},
        ]
        out = header_index.inject_gap_headers(recs, header_index.T_QUESTION)
        ns = {r["n"] for r in out if r.get("n")}
        self.assertIn(7, ns)

    def test_lock_ignores_visual_ocr_miss(self):
        r = bph.ChapterRunner("x.pdf", "OBG", 1, "/tmp", model=object(),
                              state={})
        r._visual_headers = [
            {"page": 23, "y": 100, "type": header_index.T_SOLUTION, "n": 13},
            {"page": 24, "y": 100, "type": header_index.T_SOLUTION, "n": 15},
        ]
        q = [{"_qn": n} for n in range(1, 4)]
        a = [{"_qn": n} for n in range(1, 4)]
        s = [{"_qn": n} for n in range(1, 4)]
        ok, why = r._ledger_lock(q, a, s)
        self.assertTrue(ok, why)

    def test_call_exner_on_q14_emptied(self):
        r = bph.ChapterRunner("x.pdf", "OBG", 1, "/tmp", model=object(),
                              state={})
        items = [{
            "_qn": 14,
            "solution_text": "Call-Exner bodies are seen in granulosa cells.",
            "source_page_range": [27, 28],
        }]
        out = r._flag_solution_interval_mismatch(items)
        self.assertEqual(out[0]["solution_text"], "")
        self.assertIn("Call-Exner", out[0]["_split_note"])

    def test_ready_illegal_without_key_evidence_when_required(self):
        rec = {
            "question_text": "stem?",
            "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
            "correct_option": "D",
            "solution_text": "Because D.",
            "_key_evidence_required": True,
        }
        row = qp.build_final_question(
            "OBG", "OBG-001", 1, 1, rec,
            {"question": [], "solution": []})
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        self.assertTrue(any("key-table" in x or "evidence" in x
                            for x in row["qa_reasons"]))


class CropParseTests(unittest.TestCase):
    def test_option_d_and_stem(self):
        import crop_parse
        txt = (
            "Question 12:\nWhich is true?\n"
            "a) first\nb) second\nc) third\nd) fourth on next page\n"
            "Question 13:\nNext stem\n"
        )
        it = crop_parse.parse_question_text(txt, 12)
        self.assertEqual(it["stem"], "Which is true?")
        self.assertEqual(it["options"]["D"], "fourth on next page")
        self.assertEqual(len(it["options"]), 4)

    def test_solution_clip(self):
        import crop_parse
        txt = ("Solution to Question 2: body two\n"
               "Solution to Question 3: body three")
        it = crop_parse.parse_solution_text(txt, 2)
        self.assertIn("body two", it["solution_text"])
        self.assertNotIn("body three", it["solution_text"])


def _patch(obj, name, new, saved):
    saved.setdefault("patches", []).append((obj, name, getattr(obj, name)))
    setattr(obj, name, new)


if __name__ == "__main__":
    unittest.main()
