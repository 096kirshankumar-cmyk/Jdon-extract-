#!/usr/bin/env python3
"""Regression suite for the RESUME-RELINK fix (OBG-003, 2026-08-19).

Bug: when a chapter is re-run after a pause/crash, its figures were already
claimed and renamed to the locked final convention by the earlier run, so
extract_real_images skips them ("figure bytes already owned on disk") and the
fresh chapter_records export with EMPTY image lists although the files sit on
disk. Proven live: resumed OBG ch3 exported q3-q11 with empty
solution_images while OBG-003-003_SOL_01.webp etc. existed.

Fix: qbank_pipeline._relink_resume_owned_images -- re-attaches prior-run
claims using ONLY ledger evidence (outcome claimed|shared + locked final
name + file on disk + owner record present). Never guesses from position or
bare filenames.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import qbank_pipeline as qp

SUBJECT = "OPH"
CH_NO = 3
CH_ID = f"{SUBJECT}-{CH_NO:03d}"


def _rec(qn):
    return {"q_no": qn, "question_text": f"Stem {qn}?",
            "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
            "correct_option": "A", "solution_text": f"Sol {qn}.",
            "tables": [], "has_figure_in_question": False,
            "has_figure_in_solution": True}


def _row(owner, final_file, outcome="claimed", page=53, method="positional_carry",
         chapter_id=CH_ID, temp="OPH/OPH-p53-2249.webp"):
    return {"subject": SUBJECT, "chapter_id": chapter_id, "page": page,
            "file": temp, "owner": owner, "slot": "solution",
            "method": method, "evidence": "t", "confidence": "high",
            "outcome": outcome, "ts": "2026-08-19T06:13:27Z",
            "obj_id": 2249, "final_file": final_file}


class RelinkEnv(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="relink_case_"))
        self._saved = (qp.ASSETS_DIR, qp.DATA_DIR, qp.STATE_FILE)
        qp.ASSETS_DIR = self.root / "assets"
        qp.DATA_DIR = self.root / "data"
        qp.STATE_FILE = self.root / "state.json"
        (qp.ASSETS_DIR / "questions" / SUBJECT).mkdir(parents=True)
        qp.DATA_DIR.mkdir(parents=True)

    def tearDown(self):
        qp.ASSETS_DIR, qp.DATA_DIR, qp.STATE_FILE = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def touch(self, rel):
        p = qp.ASSETS_DIR / "questions" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xff" * 3000)   # > MIN_IMAGE_BYTES
        return rel

    def write_ledger(self, rows):
        (qp.DATA_DIR / "image_ownership.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")

    def run_relink(self, records, by_q=None, opages=None):
        return qp._relink_resume_owned_images(
            CH_ID, SUBJECT, CH_NO, records,
            by_q if by_q is not None else {}, opages)


class TestResumeRelink(RelinkEnv):
    def test_claimed_file_reattaches_to_owner(self):
        """The core OBG ch3 failure: prior-run claim + file on disk +
        fresh record -> exported with the image again."""
        f = self.touch("OPH/OPH-003-003_SOL_01.webp")
        self.write_ledger([_row("OPH-003-003", f)])
        by_q = {}
        notes = self.run_relink({3: _rec(3)}, by_q)
        self.assertEqual(by_q[3]["solution"], [f])
        self.assertEqual(len(notes), 1)

    def test_shared_row_reattaches_to_both_owners(self):
        """Multi-draw: one object drawn twice; claim row for q3 names the
        shared final file, q4's shared row points at the same file. After
        relink BOTH owners carry it (the original user bug class)."""
        f = self.touch("OPH/OPH-003-003_SOL_01.webp")
        self.write_ledger([
            _row("OPH-003-003", f, page=55),
            _row("OPH-003-004", f, outcome="shared", page=55,
                 method="multi_draw_geometry"),
        ])
        by_q = {}
        notes = self.run_relink({3: _rec(3), 4: _rec(4)}, by_q)
        self.assertEqual(by_q[3]["solution"], [f])
        self.assertEqual(by_q[4]["solution"], [f])
        self.assertEqual(len(notes), 2)

    def test_refused_rows_never_attach(self):
        f = self.touch("OPH/OPH-003-005_SOL_01.webp")
        self.write_ledger([_row("OPH-003-005", f, outcome="refused_cap")])
        by_q = {}
        self.run_relink({5: _rec(5)}, by_q)
        self.assertEqual(by_q.get(5, {}).get("solution", []), [])

    def test_missing_file_never_attaches(self):
        self.write_ledger([_row("OPH-003-006", "OPH/OPH-003-006_SOL_01.webp")])
        by_q = {}
        self.run_relink({6: _rec(6)}, by_q)
        self.assertEqual(by_q.get(6, {}).get("solution", []), [])

    def test_other_chapter_rows_do_not_leak(self):
        f = self.touch("OPH/OPH-004-001_SOL_01.webp")
        self.write_ledger([
            _row("OPH-004-001", f, chapter_id="OPH-004",
                 temp="OPH/OPH-p100-7.webp"),
        ])
        by_q = {}
        self.run_relink({1: _rec(1)}, by_q)
        self.assertEqual(by_q, {})

    def test_no_ledger_is_noop(self):
        by_q = {}
        notes = self.run_relink({1: _rec(1)}, by_q)
        self.assertEqual((by_q, notes), ({}, []))

    def test_bare_file_without_ledger_row_never_attaches(self):
        """Anti-guessing guard: a final-named file sitting on disk with NO
        ledger claim is NOT attached. Only the ledger is proof of ownership."""
        self.touch("OPH/OPH-003-007_SOL_01.webp")   # no ledger row at all
        by_q = {}
        notes = self.run_relink({7: _rec(7)}, by_q)
        self.assertEqual(by_q, {})
        self.assertEqual(notes, [])

    def test_stale_claim_for_reconciled_away_record_skipped(self):
        """Ledger claims a question this run no longer has (reconciled out):
        skipped, no phantom entry created."""
        f = self.touch("OPH/OPH-003-009_SOL_01.webp")
        self.write_ledger([_row("OPH-003-009", f)])
        by_q = {}
        notes = self.run_relink({8: _rec(8)}, by_q)   # q9 absent
        self.assertEqual(by_q, {})
        self.assertEqual(notes, [])

    def test_live_run_idempotent_no_duplicates(self):
        """On a normal live run the file is already in image_files_by_q;
        relink must not double-attach it."""
        f = self.touch("OPH/OPH-003-010_SOL_01.webp")
        self.write_ledger([_row("OPH-003-010", f)])
        by_q = {10: {"question": [], "solution": [f]}}
        notes = self.run_relink({10: _rec(10)}, by_q)
        self.assertEqual(by_q[10]["solution"], [f])
        self.assertEqual(notes, [])

    def test_option_side_and_page_map(self):
        f = self.touch("OPH/OPH-003-011_OPT_C_01.webp")
        self.write_ledger([_row("OPH-003-011", f, page=61)])
        opages = {}
        notes = self.run_relink({11: _rec(11)}, {}, opages)
        self.assertEqual(notes[0]["kind"], "OPT_C")
        self.assertEqual(opages.get(f), 61)

    @unittest.skipUnless(
        Path("/home/user/audit/run_obg_ch3/data/image_ownership.jsonl").exists(),
        "production OBG ch3 ledger fixture absent (developer-machine data)")
    def test_real_obg_ch3_ledger_replays(self):
        """Replay the ACTUAL production ledger from the paused+resumed
        OBG ch3 run: every claimed/shared solution figure re-attaches to
        its owner, including both multi-draw shareds (q4, q16)."""
        src = Path("/home/user/audit/run_obg_ch3/data/image_ownership.jsonl")
        rows = [json.loads(l) for l in src.read_text().splitlines()
                if l.strip()]
        # remap subject OBG->OPH not needed: run under the real names
        (qp.ASSETS_DIR / "questions" / "OBG").mkdir(parents=True)
        for r in rows:
            ff = r.get("final_file")
            if ff and r.get("outcome") in ("claimed", "shared"):
                p = qp.ASSETS_DIR / "questions" / ff
                if not p.exists():
                    p.write_bytes(b"\xff" * 3000)
        (qp.DATA_DIR / "image_ownership.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")
        records = {n: _rec(n) for n in range(1, 17)}
        by_q = {}
        notes = qp._relink_resume_owned_images(
            "OBG-003", "OBG", 3, records, by_q)
        # q3's shared figure file must land on BOTH 3 and 4
        self.assertIn("OBG/OBG-003-003_SOL_01.webp", by_q[3]["solution"])
        self.assertIn("OBG/OBG-003-003_SOL_01.webp", by_q[4]["solution"])
        # q16 shares q15's file
        self.assertIn("OBG/OBG-003-015_SOL_01.webp", by_q[15]["solution"])
        self.assertIn("OBG/OBG-003-015_SOL_01.webp", by_q[16]["solution"])
        # q7 keeps its two figures
        self.assertEqual(len(by_q[7]["solution"]), 2)
        # ledger said 15 claimed/shared rows -> 15 attachments
        self.assertEqual(len(notes), 15)


if __name__ == "__main__":
    unittest.main()
