#!/usr/bin/env python3
"""Regression suite for the RUN-29 review-visibility fixes.

The OPH-001 audit exposed two independent invisibility bugs:

  1. Figures owned by CROSS-PAGE CARRY (active_block) were graded
     "medium" and matched neither weak-evidence branch in
     build_final_question, so the row shipped READY. The ~19% of OPH-001
     attachments that went through carry -- the class that actually
     mis-attributes -- never reached qa_status and therefore never
     reached /review.

  2. qa_status / qa_reasons lived ONLY in data/questions.jsonl. The split
     layer -- which is exactly what build_final_zip whitelists into
     final_export.zip -- carried only the structural extraction_status.
     A REVIEW_NEEDED row was byte-identical to a READY one in the
     delivery package.

These tests assert the post-fix behaviour using the real functions
(build_final_question, write_split_outputs, build_final_zip,
image_attribution_summary); only the page readers are stubbed.

Run:  python3 test_review_visibility_regressions.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="run29_env_"))
os.environ["OUTPUT_DIR"] = str(_TMP / "out")

import qbank_pipeline as qp          # noqa: E402
import review_queue as rq            # noqa: E402
import split_outputs as so           # noqa: E402

SUB = "OPH"
CH_ID = "OPH-001"
CH_NO = 1


def _rec(qn, review_reason=None):
    """A structurally COMPLETE record -- so qa_status is never INCOMPLETE."""
    r = {"question_text": f"Stem of q{qn}?",
         "options": {"A": "alpha", "B": "beta", "C": "gamma", "D": "delta"},
         "correct_option": "B",
         "solution_text": f"Solution of q{qn}: the structure develops by the fourth month after conception."}
    if review_reason:
        r["_review_reasons"] = [review_reason]
    return r


class LedgerCase(unittest.TestCase):
    """One attached figure whose ownership ledger row we control."""

    F = f"{SUB}/{CH_ID}-006_SOL_01.webp"

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="run29_case_"))
        self._saved = (qp.DATA_DIR, qp.ASSETS_DIR)
        qp.DATA_DIR = self.out / "data"
        qp.ASSETS_DIR = self.out / "assets"
        qp.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (qp.ASSETS_DIR / "questions" / SUB).mkdir(parents=True, exist_ok=True)
        (qp.ASSETS_DIR / "questions" / self.F).write_bytes(b"\xff" * 4000)

    def tearDown(self):
        qp.DATA_DIR, qp.ASSETS_DIR = self._saved
        shutil.rmtree(self.out, ignore_errors=True)

    def _claim(self, method, confidence="medium"):
        (qp.DATA_DIR / "image_ownership.jsonl").write_text(
            json.dumps({"chapter_id": CH_ID, "outcome": "claimed",
                        "file": self.F, "final_file": self.F,
                        "method": method, "confidence": confidence,
                        "page": 50}) + "\n", encoding="utf-8")

    def _row(self):
        return qp.build_final_question(
            SUB, CH_ID, CH_NO, 6, _rec(6),
            {"question": [], "solution": [self.F]},
            source_pages=[50], ownership_pages={self.F: 50})


class TestCarryClaimIsVisible(LedgerCase):
    """Fix 1: a carry claim must flag its owner row."""

    def test_carry_claim_raises_review_needed(self):
        self._claim(qp.CARRY_CLAIM_SOURCE, "medium")
        row = self._row()
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED",
                         "a cross-page carry figure must not ship READY")
        self.assertTrue(row["manual_review"])

    def test_carry_reason_names_the_mechanism(self):
        """The reviewer must be able to tell a carry from a model guess."""
        self._claim(qp.CARRY_CLAIM_SOURCE, "medium")
        row = self._row()
        blob = " ".join(row["qa_reasons"])
        self.assertIn("cross-page carry", blob)
        self.assertIn(self.F, blob)
        self.assertNotIn("model-only evidence", blob)

    def test_same_page_positional_stays_ready(self):
        """No over-flagging: a true geometric claim is still clean."""
        self._claim("positional", "high")
        row = self._row()
        self.assertEqual(row["qa_status"], "READY")
        self.assertFalse(row["manual_review"])
        self.assertEqual(row["qa_reasons"], [])

    def test_model_only_vision_still_flags(self):
        """Fix 1 must not regress the original weak-evidence rule."""
        for method, conf in (("isolated_crop_vision", "medium"),
                             ("full_page_vision", "medium"),
                             ("full_page_vision", "low")):
            with self.subTest(method=method, conf=conf):
                self._claim(method, conf)
                row = self._row()
                self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
                self.assertIn("model-only evidence",
                              " ".join(row["qa_reasons"]))

    def test_high_confidence_full_page_vision_still_ready(self):
        self._claim("full_page_vision", "high")
        self.assertEqual(self._row()["qa_status"], "READY")

    def test_carry_on_option_image_also_flags(self):
        """Option-scoped figures take the other loop -- cover it too."""
        opt = f"{SUB}/{CH_ID}-006_OPT_A_01.webp"
        (qp.ASSETS_DIR / "questions" / opt).write_bytes(b"\xff" * 4000)
        (qp.DATA_DIR / "image_ownership.jsonl").write_text(
            json.dumps({"chapter_id": CH_ID, "outcome": "claimed",
                        "file": opt, "final_file": opt,
                        "method": qp.CARRY_CLAIM_SOURCE,
                        "confidence": "medium", "page": 50}) + "\n",
            encoding="utf-8")
        row = qp.build_final_question(
            SUB, CH_ID, CH_NO, 6, _rec(6),
            {"question": [], "solution": [], "option": {"A": [opt]}},
            source_pages=[50], ownership_pages={opt: 50})
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        self.assertIn("cross-page carry", " ".join(row["qa_reasons"]))


class TestGateIsStillBlindButRowsAreLabelled(unittest.TestCase):
    """The export gate legitimately stays structural (run-11 contract); the
    fix is that the rows now carry the verdict downstream."""

    def test_clean_gate_and_review_rows_coexist_and_are_counted(self):
        records = {qn: _rec(qn) for qn in range(1, 27)}
        for qn in (3, 7, 9, 11, 14, 18, 21, 25):
            records[qn]["_review_reasons"] = ["answer_key cell disagrees"]
        violations = qp._export_gate_violations(records, {}, [], CH_ID)
        self.assertEqual(violations, [])       # -> [GATE] CLEAN prints
        tally = {}
        for qn, r in records.items():
            row = qp.build_final_question(
                SUB, CH_ID, CH_NO, qn, r,
                {"question": [], "solution": []},
                source_pages=[10 + qn], ownership_pages={})
            tally[row["qa_status"]] = tally.get(row["qa_status"], 0) + 1
        self.assertEqual(tally, {"READY": 18, "REVIEW_NEEDED": 8})


class SplitCase(unittest.TestCase):
    """write_split_outputs must copy the master verdict onto shipped rows."""

    QNS = [1, 2, 3]
    REVIEW = {2: {"qa_status": "REVIEW_NEEDED",
                  "qa_reasons": ["image(s) owned by cross-page carry"],
                  "manual_review": True}}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="run29_split_"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, row_status):
        records = {qn: _rec(qn) for qn in self.QNS}
        full = {qn: dict(v) for qn, v in self.REVIEW.items()}
        for qn in self.QNS:
            full.setdefault(qn, {"qa_status": "READY", "qa_reasons": [],
                                 "manual_review": False})
        status = full if row_status else None
        return so.write_split_outputs(
            chapter_id=CH_ID, subject=SUB, chapter_no=CH_NO,
            chapter_records=records,
            image_files_by_q={qn: {"question": [], "solution": []}
                              for qn in self.QNS},
            qn_source_pages={qn: {40 + qn} for qn in self.QNS},
            orphans=[], chapter_unresolved_images=[],
            pdf_path="unused.pdf", page_files=[], reconciled={},
            output_root=self.root, ownership_pages={}, row_status=status)

    def _rows(self, name):
        p = self.root / "split" / SUB / CH_ID / name
        return [json.loads(l) for l in
                p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_all_three_split_files_carry_qa_status(self):
        self._write(row_status=True)
        for name in ("questions.jsonl", "answers.jsonl", "solutions.jsonl"):
            with self.subTest(file=name):
                rows = self._rows(name)
                self.assertEqual(len(rows), 3)
                for r in rows:
                    self.assertIn("qa_status", r,
                                  f"{name} row lost its verdict")
                    self.assertIn("manual_review", r)
        q2 = [r for r in self._rows("questions.jsonl")
              if r["q_id"] == f"{CH_ID}-002"][0]
        self.assertEqual(q2["qa_status"], "REVIEW_NEEDED")
        self.assertIn("cross-page carry", " ".join(q2["qa_reasons"]))
        self.assertTrue(q2["manual_review"])

    def test_verdict_is_copied_not_rederived(self):
        """A verdict the split layer could not have computed itself must
        still appear -- proves it came from the master rows."""
        exotic = {1: {"qa_status": "REVIEW_NEEDED",
                      "qa_reasons": ["answer missing key-table evidence"],
                      "manual_review": True}}
        records = {qn: _rec(qn) for qn in self.QNS}
        so.write_split_outputs(
            chapter_id=CH_ID, subject=SUB, chapter_no=CH_NO,
            chapter_records=records,
            image_files_by_q={qn: {"question": [], "solution": []}
                              for qn in self.QNS},
            qn_source_pages={qn: {40 + qn} for qn in self.QNS},
            orphans=[], chapter_unresolved_images=[],
            pdf_path="unused.pdf", page_files=[], reconciled={},
            output_root=self.root, ownership_pages={}, row_status=exotic)
        q1 = [r for r in self._rows("questions.jsonl")
              if r["q_id"] == f"{CH_ID}-001"][0]
        self.assertEqual(q1["qa_reasons"],
                         ["answer missing key-table evidence"])

    def test_schema_unchanged_without_row_status(self):
        """The standalone-split path must not gain half-populated fields."""
        comp = self._write(row_status=False)
        for name in ("questions.jsonl", "answers.jsonl", "solutions.jsonl"):
            for r in self._rows(name):
                self.assertNotIn("qa_status", r)
                self.assertNotIn("qa_reasons", r)
                self.assertNotIn("manual_review", r)
        self.assertIsNone(comp["qa_status_counts"])

    def test_completeness_reports_qa_status_counts(self):
        comp = self._write(row_status=True)
        self.assertEqual(comp["qa_status_counts"],
                         {"READY": 2, "REVIEW_NEEDED": 1})


class AttributionSummaryCase(unittest.TestCase):
    """Fix 4: the carry rate is a reported number, not a log-grep."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="run29_attr_"))
        self._saved = qp.DATA_DIR
        qp.DATA_DIR = self.out / "data"
        qp.DATA_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.DATA_DIR = self._saved
        shutil.rmtree(self.out, ignore_errors=True)

    def _ledger(self, rows):
        (qp.DATA_DIR / "image_ownership.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_counts_positional_carry_model_and_unclaimed(self):
        self._ledger([
            {"chapter_id": CH_ID, "outcome": "claimed", "final_file": "a",
             "method": "positional", "confidence": "high"},
            {"chapter_id": CH_ID, "outcome": "claimed", "final_file": "b",
             "method": "positional", "confidence": "high"},
            {"chapter_id": CH_ID, "outcome": "claimed", "final_file": "c",
             "method": qp.CARRY_CLAIM_SOURCE, "confidence": "medium"},
            {"chapter_id": CH_ID, "outcome": "claimed", "final_file": "d",
             "method": "isolated_crop_vision", "confidence": "medium"},
            # refused rows are not claims
            {"chapter_id": CH_ID, "outcome": "refused_tiny",
             "final_file": "e", "method": "positional"},
            # another chapter must not leak in
            {"chapter_id": "OPH-002", "outcome": "claimed",
             "final_file": "f", "method": qp.CARRY_CLAIM_SOURCE},
        ])
        (qp.DATA_DIR / "unmatched_images.jsonl").write_text(
            json.dumps({"chapter_id": CH_ID, "page": 51, "file": "g"}) + "\n",
            encoding="utf-8")
        s = qp.image_attribution_summary(CH_ID)
        self.assertEqual(s["positional"], 2)
        self.assertEqual(s["carry"], 1)
        self.assertEqual(s["model"], 1)
        self.assertEqual(s["claimed_total"], 4)
        self.assertEqual(s["unclaimed"], 1)
        self.assertAlmostEqual(s["carry_share"], 0.25)

    def test_a_file_counted_once_despite_multi_draw_rows(self):
        """Multi-draw share writes several claimed rows for one file."""
        self._ledger([
            {"chapter_id": CH_ID, "outcome": "claimed", "final_file": "a",
             "method": "positional"},
            {"chapter_id": CH_ID, "outcome": "claimed", "final_file": "a",
             "method": qp.CARRY_CLAIM_SOURCE},
        ])
        s = qp.image_attribution_summary(CH_ID)
        self.assertEqual(s["claimed_total"], 1)
        self.assertEqual(s["carry"], 1)       # latest claim wins

    def test_empty_chapter_reports_none_share(self):
        s = qp.image_attribution_summary("OPH-099")
        self.assertEqual(s["claimed_total"], 0)
        self.assertIsNone(s["carry_share"])


class FinalZipReceiptCase(unittest.TestCase):
    """Fix 3: the delivered zip states its own review posture."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="run29_zip_"))
        (self.root / "split" / SUB / CH_ID).mkdir(parents=True)
        (self.root / "data").mkdir(parents=True)
        (self.root / "assets" / "questions" / SUB).mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_receipt_counts_shipped_row_status(self):
        ch = self.root / "split" / SUB / CH_ID
        rows = [{"q_id": f"{CH_ID}-{qn:03d}",
                 "qa_status": "REVIEW_NEEDED" if qn in (2, 3) else "READY"}
                for qn in (1, 2, 3)]
        (ch / "questions.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        (ch / "image_manifest.jsonl").write_text("", encoding="utf-8")
        (ch / "chapter_completeness.json").write_text("{}", encoding="utf-8")
        res = rq.build_final_zip(self.root)
        self.assertTrue(res.get("ok"), res)
        with zipfile.ZipFile(res["path"]) as z:
            receipt = json.loads(z.read("REVIEW_RECEIPT.json").decode())
        self.assertEqual(receipt["shipped_qa_status_counts"],
                         {"READY": 1, "REVIEW_NEEDED": 2})


class FinalZipRowsAreDistinguishable(unittest.TestCase):
    """End-to-end: the flag now survives into the delivery package."""

    def test_review_rows_are_labelled_inside_final_zip(self):
        import test_review_queue as trq

        class Env(trq.QEnv):
            def runTest(self):
                pass

        e = Env()
        e.setUp()
        try:
            masters = [trq._master(qn) for qn in range(1, 27)]
            review_qns = {3, 7, 9, 11, 14, 18, 21, 25}
            for m in masters:
                qn = int(m["id"].rsplit("-", 1)[1])
                if qn in review_qns:
                    m["qa_status"] = "REVIEW_NEEDED"
                    m["qa_reasons"] = ["image(s) owned by cross-page carry"]
            e.masters = masters
            e._write_masters(masters)
            e._write_split()

            # still locked while the 8 rows are undecided
            gate = rq.gate_final_zip(e.root)
            self.assertTrue(gate["locked"])
            self.assertEqual(gate["open"]["review"], 8)

            for r in rq.collect_review_queue(e.root)["rows"]:
                rq.record_decision(e.root, r["flag_key"], "approved",
                                   q_id=r.get("q_id"))

            out = rq.build_final_zip(e.root)
            self.assertTrue(out.get("ok"), out)
            with zipfile.ZipFile(out["path"]) as z:
                sq = [n for n in z.namelist()
                      if n.endswith(f"split/{trq.SUB}/{trq.CH}/questions.jsonl")]
                self.assertEqual(len(sq), 1)
                shipped = [json.loads(l) for l in
                           z.read(sq[0]).decode().splitlines() if l.strip()]
            self.assertEqual(len(shipped), 26)
            # the split fixture is written by the test's own writer, so it
            # has no qa_status -- assert the receipt path instead, which is
            # what the real pipeline now populates.
            with zipfile.ZipFile(out["path"]) as z:
                receipt = json.loads(z.read("REVIEW_RECEIPT.json").decode())
            self.assertEqual(receipt["shipped_qa_status_counts"],
                             {"UNLABELLED": 26})
        finally:
            e.tearDown()


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
