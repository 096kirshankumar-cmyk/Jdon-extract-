#!/usr/bin/env python3
"""Tests for the AI-verified false-flag pass (spec-ordered)."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import flag_verifier as fv
import review_queue as rq


class VEnv(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fv_case_"))
        (self.root / "data").mkdir(parents=True)
        (self.root / "assets" / "questions" / "OPH").mkdir(parents=True)
        # masters with a couple rows
        rows = [{
            "id": f"OPH-005-{n:03d}", "subject": "OPH", "chapter_id": "OPH-005",
            "question": {"text": f"Stem {n}?", "images": []},
            "options": [{"id": l, "text": f"o{l}", "images": []} for l in "ABCD"],
            "correct_options": ["A"],
            "solution": {"text": f"Solution {n}.", "images": [], "tables": []},
            "source_pages": [50], "qa_status": "READY", "qa_reasons": []}
            for n in (1, 2)]
        (self.root / "data" / "questions.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")
        # the flagged row: q1 wrongly declared missing figure
        (self.root / "data" / "export_gate.jsonl").write_text(json.dumps(
            {"kind": "missing_declared_figure_question", "chapter_id": "OPH-005",
             "q_no": 1, "detail": "declared but absent"}) + "\n")
        # book stub existing so render path proceeds (render stubbed anyway)
        self.books = self.root / "books"; self.books.mkdir()
        (self.books / "OPH.pdf").write_bytes(b"%PDF-stub")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, verdict, env_extra=None, stub_page=True):
        env = dict(os.environ); env.pop("GEMINI_VERIFY_API_KEYS", None)
        for i in range(1, 9):
            env.pop(f"GEMINI_VERIFY_API_KEY_{i}", None)
        env["GEMINI_VERIFY_API_KEY_1"] = "K1" if not env_extra else env_extra.pop("k1")
        if env_extra:
            env.update(env_extra)
        if stub_page:
            self._svp = getattr(fv, "render_page_png")
            fv.render_page_png = lambda pdf, page, dpi=110: b"png-bytes"
        calls = []
        def fake_call(png, prompt, key):
            calls.append(prompt)
            return json.dumps(verdict) if verdict is not None else "not json"
        try:
            os.environ.clear(); os.environ.update(env)
            return fv.run_verification(self.root, books_dir=str(self.books),
                                       call_model=fake_call)
        finally:
            if stub_page:
                fv.render_page_png = self._svp
            os.environ.clear()
            os.environ.update({"GEMINI_API_KEY": "DUMMY"})


class TestPromptAndParse(unittest.TestCase):
    def test_prompt_is_spec_shape(self):
        p = fv.build_prompt({"stem": "S?", "opt_a": "a", "opt_b": "b",
                             "opt_c": "c", "opt_d": "d", "correct_option": "A",
                             "solution_text": "sol", "attached_images": "none",
                             "flag_kind": "k", "flag_detail": "d",
                             "q_id": "X-001-001"})
        for needle in ("Question: S?", "Correct Answer: A", "FLAG RAISED: k",
                       "confidence", "flag_genuine", "high", "medium", "low"):
            self.assertIn(needle, p)

    def test_parse_strictness(self):
        self.assertIsNone(fv.classify_response("garbage"))
        self.assertIsNone(fv.classify_response("{\"flag_genuine\": false}"))
        v = fv.classify_response('wrap {"flag_genuine": false, "reason": "x", '
                                 '"confidence": "high"} end')
        self.assertIsNotNone(v)
        self.assertFalse(v["flag_genuine"])
        self.assertEqual(v["confidence"], "high")
        self.assertIsNone(fv.classify_response(
            '{"flag_genuine": false, "reason": "x", "confidence": "sure"}'))


class TestRunVerification(VEnv):
    def test_highconf_false_auto_resolves_and_audits(self):
        res = self._run({"flag_genuine": False, "reason": "no figure printed",
                         "confidence": "high"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["resolved"], 1)
        self.assertEqual(res["checked"], 1)
        led = rq._read_jsonl(self.root / "data" / fv.AUDIT_LOG)
        self.assertEqual(len(led), 1)
        self.assertEqual(led[0]["q_id"], "OPH-005-001")
        self.assertFalse(led[0]["sampled_back"])
        # the queue row is closed with the dedicated action
        q = rq.collect_review_queue(self.root)
        self.assertEqual(q["counts"]["resolved"], 1)

    def test_medium_conf_is_never_closed(self):
        res = self._run({"flag_genuine": False, "reason": "x", "confidence": "medium"})
        self.assertEqual(res["resolved"], 0)
        q = rq.collect_review_queue(self.root)
        self.assertEqual(q["counts"]["resolved"], 0)

    def test_genuine_stays_open(self):
        res = self._run({"flag_genuine": True, "reason": "x", "confidence": "high"})
        self.assertEqual(res["resolved"], 0)

    def test_parse_fail_keeps_flag(self):
        res = self._run(None)
        self.assertEqual(res["parse_failed"], 1)
        q = rq.collect_review_queue(self.root)
        self.assertEqual(q["counts"]["resolved"], 0)

    def test_no_pool_configured_skips_everything(self):
        # NOTHING anywhere: no verify keys AND no extraction keys -> polite skip
        env = {k: "" for k in (
            ["GEMINI_VERIFY_API_KEYS", "GEMINI_API_KEYS", "GEMINI_API_KEY"]
            + [f"GEMINI_VERIFY_API_KEY_{i}" for i in range(1, 9)]
            + [f"GEMINI_API_KEY_{i}" for i in range(1, 9)])}
        # _run() pops verify vars and always adds key1; make it empty here
        res = self._run({"flag_genuine": False, "reason": "x",
                         "confidence": "high"}, env_extra={"k1": "", **env})
        self.assertTrue(res["skipped"])
        self.assertIn("GEMINI_API_KEY", res["error"])

    def test_reopen_undoes_the_ai_close(self):
        self._run({"flag_genuine": False, "reason": "x", "confidence": "high"})
        q1 = rq.collect_review_queue(self.root)
        self.assertEqual(q1["counts"]["resolved"], 1)
        fk = rq._read_jsonl(self.root / "data" / fv.AUDIT_LOG)[0]["flag_key"]
        rq.record_decision(self.root, fk, "reopened")
        q2 = rq.collect_review_queue(self.root)
        self.assertEqual(q2["counts"]["resolved"], 0)   # back to open
        self.assertEqual(len(q2["rows"]), 1)

    def test_selfaudit_sample_stays_open(self):
        # craft a flag_key with hash % 15 == 0 deterministically
        from review_queue import _mk_flag
        seed = 0
        while True:
            seed += 1
            fake = _mk_flag("missing_declared_figure_question", "REVIEW",
                            f"detail seed {seed}", "export_gate.jsonl",
                            chapter_id="OPH-005", q_id="OPH-005-001", q_no=1,
                            subject="OPH")
            if int(fake["flag_key"], 16) % 15 == 0:
                break
        (self.root / "data" / "export_gate.jsonl").write_text(json.dumps(
            {"kind": "missing_declared_figure_question", "chapter_id": "OPH-005",
             "q_no": 1, "detail": f"detail seed {seed}"}) + "\n")
        res = self._run({"flag_genuine": False, "reason": "x", "confidence": "high"})
        self.assertEqual(res["checked"], 1)
        self.assertEqual(res["sampled_back"], 1)
        self.assertEqual(res["resolved"], 0)          # sample never closes it
        led = rq._read_jsonl(self.root / "data" / fv.AUDIT_LOG)
        self.assertTrue(led[0]["sampled_back"])


class TestContentLock(unittest.TestCase):
    def test_verifier_never_calls_edit_primitives(self):
        src = Path(fv.__file__).read_text(encoding="utf-8")
        for banned in ("apply_edit(", "apply_image_op(", "apply_image_attach(",
                       "apply_orphan_merge(", "record_decision(", ):
            if banned == "record_decision(":
                continue   # flag STATUS is its only write path (contract)
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()


class TestKeyFallback(unittest.TestCase):
    def test_falls_back_to_extraction_pool(self):
        import os as _os
        env = {"GEMINI_VERIFY_API_KEYS": "", "GEMINI_VERIFY_API_KEY_1": ""}
        # clear all verify vars the test env may hold
        rr = dict(env)
        rr.pop("GEMINI_VERIFY_API_KEY_1", None)
        keys = fv._discover_verify_keys({"GEMINI_VERIFY_API_KEY_1": "",
                                         "GEMINI_API_KEY_1": "K_A", "GEMINI_API_KEY_2": "K_B"})
        self.assertEqual(keys, ["K_A", "K_B"])  # verify pool empty -> extraction keys

    def test_verify_pool_wins_when_set(self):
        keys = fv._discover_verify_keys({"GEMINI_VERIFY_API_KEY_1": "V1",
                                         "GEMINI_API_KEY_1": "X"})
        self.assertEqual(keys, ["V1"])

    def test_nothing_anywhere_empty(self):
        self.assertEqual(fv._discover_verify_keys({}), [])
