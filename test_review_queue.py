#!/usr/bin/env python3
"""Full regression suite for review_queue.py — the manual-review engine.

Locks every design contract: queue union across ALL flag files, watchdog on
unregistered files, validator catch-all, persistence across "refresh"
(rebuild), stale-decision resurrection, atomic multi-copy edits with
read-back verify, image ops (attach/detach/move + shared-file protection),
and the gated final zip.
"""
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import review_queue as rq

SUB = "OBG"
CH = "OBG-003"


def _master(qn, status="READY", reasons=None, stem=None, ans="A", sol=None):
    qid = f"{CH}-{qn:03d}"
    return {
        "id": qid, "subject": SUB, "chapter_id": CH,
        "question": {"text": stem or f"Stem of q{qn}?", "images": []},
        "options": [{"id": L, "text": f"{L} text {qn}", "images": []}
                    for L in "ABCD"],
        "correct_options": [ans],
        "solution": {"text": sol or f"Solution of q{qn}.", "images": [],
                     "tables": []},
        "tags": [], "source_pages": [48 + qn],
        "qa_status": status, "qa_reasons": reasons or [],
    }


class QEnv(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rq_case_"))
        r = self.root
        (r / "data").mkdir(parents=True)
        (r / "split" / SUB / CH).mkdir(parents=True)
        (r / "subjects" / SUB / "chapters").mkdir(parents=True)
        (r / "assets" / "questions" / SUB).mkdir(parents=True)
        self.masters = [_master(1), _master(2), _master(3)]
        self._write_masters(self.masters)
        self._write_split()
        # two real images: q2 owns SOL_01 (shared with none), plus a spare
        self.img1 = (r / "assets" / "questions" / SUB / f"{CH}-002_SOL_01.webp")
        self.img1.write_bytes(b"\xff" * 3000)
        self.spare = (r / "assets" / "questions" / SUB / "OBG-p61-999.webp")
        self.spare.write_bytes(b"\xff" * 3000)
        self._attach_split_image(2)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- fixture writers ------------------------------------------------------
    def _write_masters(self, rows):
        lines = "\n".join(json.dumps(x) for x in rows) + "\n"
        (self.root / "data" / "questions.jsonl").write_text(lines)
        (self.root / "subjects" / SUB / "questions.jsonl").write_text(lines)
        (self.root / "subjects" / SUB / "chapters" / f"{CH}.jsonl").write_text(lines)

    def _split_q(self, m):
        return {"q_id": m["id"], "chapter_id": CH, "subject": SUB,
                "chapter_no": 3, "q_no": int(m["id"].rsplit("-", 1)[1]),
                "question_text": m["question"]["text"],
                "options": [{"id": o["id"], "text": o["text"],
                             "images": list(o["images"])}
                            for o in m["options"]],
                "question_images": list(m["question"]["images"]),
                "tables": [dict(t) for t in m["question"].get("tables", [])],
                "source_pages": m["source_pages"],
                "extraction_status": "COMPLETE"}

    def _split_a(self, m):
        return {"q_id": m["id"], "chapter_id": CH, "subject": SUB,
                "chapter_no": 3, "q_no": int(m["id"].rsplit("-", 1)[1]),
                "correct_option": m["correct_options"][0],
                "source_pages": m["source_pages"],
                "extraction_status": "COMPLETE"}

    def _split_s(self, m):
        return {"q_id": m["id"], "chapter_id": CH, "subject": SUB,
                "chapter_no": 3, "q_no": int(m["id"].rsplit("-", 1)[1]),
                "solution_text": m["solution"]["text"],
                "tables": [dict(t) for t in m["solution"].get("tables", [])],
                "solution_images": list(m["solution"]["images"]),
                "source_pages": m["source_pages"],
                "extraction_status": "COMPLETE"}

    def _write_split(self):
        ch = self.root / "split" / SUB / CH
        ch.joinpath("questions.jsonl").write_text(
            "".join(json.dumps(self._split_q(m)) + "\n" for m in self.masters))
        ch.joinpath("answers.jsonl").write_text(
            "".join(json.dumps(self._split_a(m)) + "\n" for m in self.masters))
        ch.joinpath("solutions.jsonl").write_text(
            "".join(json.dumps(self._split_s(m)) + "\n" for m in self.masters))
        ch.joinpath("chapter_completeness.json").write_text(json.dumps({
            "chapter_id": CH, "subject": SUB, "chapter_no": 3,
            "question_records": 3, "answer_records": 3, "solution_records": 3,
            "image_manifest_records": 0, "incomplete_questions": 0,
            "incomplete_answers": 0, "incomplete_solutions": 0,
            "unresolved_qid_count": 0, "orphan_count": 0,
            "unresolved_image_count": 0}))
        ch.joinpath("image_manifest.jsonl").write_text("")

    def _attach_split_image(self, qn):
        """q2's solution references SOL_01 everywhere (master+split+manifest+ledger)."""
        f = f"{SUB}/{CH}-002_SOL_01.webp"
        for m in self.masters:
            if m["id"].endswith(f"-{qn:03d}"):
                m["solution"]["images"] = [{"file": f, "source_pages": [58]}]
        self._write_masters(self.masters)
        self._write_split()
        ch = self.root / "split" / SUB / CH
        ch.joinpath("image_manifest.jsonl").write_text(json.dumps({
            "q_id": f"{CH}-002", "type": "SOLUTION", "option_letter": None,
            "file": f, "source_pages": [58], "extraction_page": 58}) + "\n")
        (self.root / "data" / "image_ownership.jsonl").write_text(json.dumps({
            "subject": SUB, "chapter_id": CH, "page": 58,
            "file": "OBG/OBG-p58-555.webp", "owner": f"{CH}-002",
            "slot": "solution", "method": "positional", "evidence": "t",
            "confidence": "high", "outcome": "claimed",
            "ts": "2026-08-19T00:00:00Z", "obj_id": 555,
            "final_file": f}) + "\n")


class TestQueueUnion(QEnv):
    def test_empty_root_is_clear_and_silent(self):
        with tempfile.TemporaryDirectory() as d:
            q = rq.collect_review_queue(d)
        self.assertTrue(q["clear"])
        self.assertEqual(q["rows"], [])

    def test_qa_status_flags_surface(self):
        self.masters[0]["qa_status"] = "REVIEW_NEEDED"
        self.masters[0]["qa_reasons"] = ["gate: something"]
        self.masters[2]["qa_status"] = "INCOMPLETE"
        self._write_masters(self.masters)
        q = rq.collect_review_queue(self.root)
        kinds = {(r["q_id"], r["kind"]) for r in q["rows"]}
        self.assertIn((f"{CH}-001", "review_needed"), kinds)
        self.assertIn((f"{CH}-003", "incomplete"), kinds)
        self.assertEqual(q["counts"]["blocker"], 1)
        self.assertEqual(q["counts"]["review"], 1)

    def test_every_flag_file_is_union_in(self):
        (self.root / "data" / "integrity_flags.jsonl").write_text(json.dumps({
            "kind": "answer_key_disagrees", "chapter_id": CH, "q_no": 1,
            "rows": [{"q_no": 1, "record": "B", "key": "C"}]}) + "\n")
        (self.root / "data" / "export_gate.jsonl").write_text(json.dumps({
            "chapter_id": CH, "kind": "figure_page_mismatch", "q_no": 2,
            "detail": "image from page 99 vs anchor 58"}) + "\n")
        (self.root / "data" / "orphans.jsonl").write_text(json.dumps({
            "chapter_id": CH, "batch_start": "53"}) + "\n")
        q = rq.collect_review_queue(self.root)
        srcs = {r["source"] for r in q["rows"]}
        self.assertIn("integrity_flags.jsonl", srcs)
        self.assertIn("export_gate.jsonl", srcs)
        self.assertIn("orphans.jsonl", srcs)
        self.assertEqual(q["counts"]["blocker"], 1)   # key disagreement

    def test_validator_catchall_surfaces_unmapped_kinds(self):
        (self.root / "data" / "validation_report.json").write_text(json.dumps({
            "summary": {"flags_total": 1},
            "chapters": {CH: [{"kind": "totally_new_kind_nobody_mapped",
                               "q_no": 2, "detail": "brand new issue"}]}}))
        q = rq.collect_review_queue(self.root)
        kinds = [r["kind"] for r in q["rows"]]
        self.assertIn("totally_new_kind_nobody_mapped", kinds)

    def test_watchdog_screams_on_unregistered_flag_file(self):
        (self.root / "data" / "some_future_flags.jsonl").write_text("{}\n")
        q = rq.collect_review_queue(self.root)
        self.assertTrue(any("some_future_flags.jsonl" in w
                            for w in q["warnings"]))
        self.assertTrue(any(r["kind"] == "watchdog_unregistered_file"
                            and r["severity"] == "BLOCKER" for r in q["rows"]))

    def test_incomplete_split_records_are_blockers(self):
        cp = self.root / "split" / SUB / CH / "chapter_completeness.json"
        c = json.loads(cp.read_text()); c["incomplete_solutions"] = 1
        cp.write_text(json.dumps(c))
        q = rq.collect_review_queue(self.root)
        self.assertTrue(any(r["kind"] == "incomplete_records"
                            and r["severity"] == "BLOCKER" for r in q["rows"]))


class TestDecisionPersistence(QEnv):
    def _flag_key(self):
        self.masters[0]["qa_status"] = "REVIEW_NEEDED"
        self._write_masters(self.masters)
        q = rq.collect_review_queue(self.root)
        return q["rows"][0]["flag_key"], q

    def test_decision_survives_rebuild_refresh(self):
        fk, q1 = self._flag_key()
        self.assertEqual(q1["counts"]["review"], 1)
        rq.record_decision(self.root, fk, "approved", "verified on page",
                           q_id=f"{CH}-001")
        q2 = rq.collect_review_queue(self.root)   # == a browser refresh
        self.assertTrue(q2["clear"])
        self.assertEqual(q2["counts"]["resolved"], 1)

    def test_stale_decision_resurfaces_when_content_changes(self):
        fk, _ = self._flag_key()
        rq.record_decision(self.root, fk, "approved", q_id=f"{CH}-001")
        self.masters[0]["question"]["text"] = "EDITED stem (bk re-ran ch)"
        self._write_masters(self.masters)
        q = rq.collect_review_queue(self.root)
        row = [r for r in q["rows"] if r["flag_key"] == fk][0]
        self.assertEqual(row["state"], "open")
        self.assertIn("changed since", row["stale_note"])


class TestTextEdits(QEnv):
    def test_stem_edit_updates_every_copy_and_verifies(self):
        res = rq.apply_edit(self.root, f"{CH}-001", "question_text",
                            "Corrected stem?", reason="typo in book")
        self.assertTrue(res["ok"] and res["verified"])
        # all copies agree
        for p in (self.root / "data" / "questions.jsonl",
                  self.root / "subjects" / SUB / "questions.jsonl",
                  self.root / "split" / SUB / CH / "questions.jsonl"):
            rows = [json.loads(l) for l in p.read_text().splitlines()]
            r = [x for x in rows if (x.get("id") or x.get("q_id")) == f"{CH}-001"][0]
            txt = (r.get("question") or {}).get("text") or r.get("question_text")
            self.assertEqual(txt, "Corrected stem?")
        led = (self.root / "data" / "human_edit_ledger.jsonl").read_text()
        self.assertIn("Corrected stem?", led)

    def test_answer_edit_maps_to_answers_file(self):
        res = rq.apply_edit(self.root, f"{CH}-002", "correct_option", "c")
        self.assertTrue(res["ok"] and res["verified"])
        a = [json.loads(l) for l in
             (self.root / "split" / SUB / CH / "answers.jsonl")
             .read_text().splitlines()]
        self.assertEqual(a[1]["correct_option"], "C")

    def test_option_edit(self):
        res = rq.apply_edit(self.root, f"{CH}-001", "option", "New B text",
                            option_letter="b")
        self.assertTrue(res["ok"] and res["verified"])

    def test_broken_table_refused_before_touching_files(self):
        self.masters[2]["solution"]["tables"] = [
            {"type": "t", "markdown": "| a | b |\n|---|---|\n| 1 | 2 |"}]
        self._write_masters(self.masters); self._write_split()
        res = rq.apply_edit(self.root, f"{CH}-003", "table",
                            "| a | b |\n|---|---|\n| 1 | 2 | 3 |",   # uneven!
                            table_index=0)
        self.assertFalse(res["ok"])
        self.assertIn("uneven", res["error"])

    def test_bad_answer_letter_refused(self):
        res = rq.apply_edit(self.root, f"{CH}-001", "correct_option", "E")
        self.assertFalse(res["ok"])

    def test_table_append_slot_creates_and_verifies(self):
        """A row with NO tables: table_index 0 (== len) creates the slot;
        disk read-back must prove master AND split show it."""
        res = rq.apply_edit(self.root, f"{CH}-001", "table",
                            "| x | y |\n|---|---|\n| 1 | 2 |", table_index=0)
        self.assertTrue(res["ok"] and res["verified"], res)
        m = rq._find_master_row(self.root, f"{CH}-001")
        self.assertEqual(m["solution"]["tables"][0]["type"], "human_added")
        # a gap beyond the append slot is refused
        res2 = rq.apply_edit(self.root, f"{CH}-001", "table",
                             "| x | y |\n|---|---|\n| 1 | 2 |", table_index=5)
        self.assertFalse(res2["ok"])
        self.assertIn("append slot", res2["error"])

    def test_question_side_table_edit(self):
        self.masters[0]["question"]["tables"] = [
            {"type": "qt", "markdown": "| a |\n|---|\n| 1 |"}]
        self._write_masters(self.masters); self._write_split()
        res = rq.apply_edit(self.root, f"{CH}-001", "table_q",
                            "| a |\n|---|\n| 9 |", table_index=0)
        self.assertTrue(res["ok"] and res["verified"], res)
        m = rq._find_master_row(self.root, f"{CH}-001")
        self.assertEqual(m["question"]["tables"][0]["markdown"], "| a |\n|---|\n| 9 |")


class TestImageOps(QEnv):
    F2 = f"{SUB}/{CH}-002_SOL_01.webp"

    def test_detach_unlinks_but_keeps_file(self):
        res = rq.apply_image_op(self.root, f"{CH}-002", "detach", self.F2)
        self.assertTrue(res["ok"])
        self.assertTrue((self.root / "assets" / "questions" / SUB /
                         f"{CH}-002_SOL_01.webp").exists())  # file kept
        a = [json.loads(l) for l in
             (self.root / "split" / SUB / CH / "solutions.jsonl")
             .read_text().splitlines()]
        self.assertEqual(a[1]["solution_images"], [])
        man = (self.root / "split" / SUB / CH / "image_manifest.jsonl").read_text()
        self.assertEqual(man.strip(), "")

    def test_move_renames_and_keeps_ledger_chain(self):
        res = rq.apply_image_op(self.root, f"{CH}-002", "move", self.F2,
                                to_qid=f"{CH}-003", reason="wrong owner")
        self.assertTrue(res["ok"])
        newf = res["new_file"]
        self.assertTrue(newf.startswith(f"{SUB}/{CH}-003_SOL"))
        self.assertFalse((self.root / "assets" / "questions" / SUB /
                          f"{CH}-002_SOL_01.webp").exists())
        self.assertTrue((self.root / "assets" / "questions" /
                         newf).exists())
        led = (self.root / "data" / "image_ownership.jsonl").read_text()
        self.assertIn('"method": "human_edit"', led)
        self.assertIn(newf, led)

    def test_move_shared_file_refused_loudly(self):
        # q3 also uses the same file (multi-draw reality)
        rq.apply_image_op(self.root, f"{CH}-003", "attach", self.F2)
        res = rq.apply_image_op(self.root, f"{CH}-002", "move", self.F2,
                                to_qid=f"{CH}-003")
        self.assertFalse(res["ok"])
        self.assertIn("SHARED", res["error"])

    def test_attach_of_other_owner_marks_shared(self):
        res = rq.apply_image_op(self.root, f"{CH}-003", "attach", self.F2)
        self.assertTrue(res["ok"])
        self.assertEqual(res["shared_with"], [f"{CH}-002"])

    def test_attach_missing_file_refused(self):
        res = rq.apply_image_op(self.root, f"{CH}-001", "attach",
                                f"{SUB}/nope.webp")
        self.assertFalse(res["ok"])


class TestFinalZipGate(QEnv):
    def test_locked_until_queue_clear(self):
        self.masters[0]["qa_status"] = "INCOMPLETE"
        self._write_masters(self.masters)
        res = rq.build_final_zip(self.root)
        self.assertFalse(res["ok"])
        self.assertTrue(res["locked"])
        # decide everything
        q = rq.collect_review_queue(self.root)
        for r in q["rows"]:
            rq.record_decision(self.root, r["flag_key"], "approved",
                               q_id=r.get("q_id"))
        out = rq.build_final_zip(self.root)
        self.assertTrue(out["ok"], out)
        with zipfile.ZipFile(out["path"]) as z:
            names = z.namelist()
        self.assertIn("REVIEW_RECEIPT.json", names)
        self.assertTrue(any(n.startswith("split/") for n in names))
        self.assertTrue(any(n.endswith(".webp") for n in names))
        # no ledgers/flags in the delivery package
        self.assertFalse(any("image_ownership" in n for n in names))
        self.assertFalse(any("export_gate" in n for n in names))


if __name__ == "__main__":
    unittest.main()


class TestReviewScreenHelpers(QEnv):
    def test_md_to_html_renders_and_escapes(self):
        html = rq.md_to_html("| A | <b>B</b> |\n|---|---|\n| 1 | 2 |")
        self.assertIn("<table", html)
        self.assertIn("&lt;b&gt;B&lt;/b&gt;", html)   # cell escaping
        self.assertIn("<th", html)
        self.assertEqual(rq.md_to_html("not a table"), "")

    def test_flag_extra_pages_and_images(self):
        # an image named inside the detail that exists on disk gets surfaced
        img = self.root / "assets" / "questions" / SUB / f"{CH}-p61-999.webp"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"\xff" * 3000)
        flag = {"kind": "image_unresolved", "detail":
                f"page 61 unresolved: {SUB}/{CH}-p61-999.webp (isolated_crop)",
                "q_id": None, "chapter_id": CH, "subject": SUB}
        ex = rq.flag_extra(self.root, flag)
        self.assertEqual(ex["images"], [f"{SUB}/{CH}-p61-999.webp"])
        self.assertEqual(ex["pages"], [61])

    def test_flag_extra_expands_incomplete_records(self):
        # make one split solution INCOMPLETE, then the chapter-level
        # completeness flag must name the exact q_id + side
        sp = self.root / "split" / SUB / CH / "solutions.jsonl"
        rows = rq._read_jsonl(sp)
        rows[0]["extraction_status"] = "INCOMPLETE"
        rows[0]["missing_fields"] = ["solution_text"]
        sp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        flag = {"kind": "incomplete_records", "chapter_id": CH, "detail": "x",
                "q_id": None}
        ex = rq.flag_extra(self.root, flag)
        ids = [e["q_id"] for e in ex["expand"]]
        self.assertIn(f"{CH}-001", ids)
        self.assertIn("solution_text", ex["expand"][0]["missing"])

    def test_orphan_readable_parses_fragment(self):
        row = {"chapter_id": CH, "new_pages": [53, 54],
               "item": {"q_no": None, "solution_text": "Pelvis divides...",
                        "tables": [{"type": "t"}]},
               "blocked_reason": "owner solution already complete"}
        r = rq.orphan_readable(row)
        self.assertEqual(r["pages"], [53, 54])
        self.assertIn("SOLUTION fragment", r["text"])
        self.assertIn("owner solution already complete", r["text"])

    def test_unresolved_images_registered_no_watchdog_scream(self):
        (self.root / "data" / "unresolved_images.jsonl").write_text(
            json.dumps({"kind": "image_unresolved", "chapter_id": CH,
                        "detail": "x"}) + "\n")
        q = rq.collect_review_queue(self.root)
        self.assertFalse(any("unresolved_images" in w for w in q["warnings"]))
        kinds = [r["kind"] for r in q["rows"]]
        self.assertIn("image_unresolved", kinds)

    def test_unclaimed_images_pool(self):
        img = self.root / "assets" / "questions" / SUB / f"{CH}-p70-5.webp"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"\xff" * 3000)
        self.assertIn(f"{SUB}/{CH}-p70-5.webp",
                      rq.unclaimed_images(self.root, SUB))


class TestTableDelete(QEnv):
    def test_delete_removes_from_all_copies_and_verifies(self):
        self.masters[2]["solution"]["tables"] = [
            {"type": "t1", "markdown": "| a |\n|---|\n| 1 |"},
            {"type": "t2", "markdown": "| b |\n|---|\n| 2 |"}]
        self._write_masters(self.masters); self._write_split()
        res = rq.apply_edit(self.root, f"{CH}-003", "table_delete", "",
                            table_index=0)
        self.assertTrue(res["ok"] and res["verified"], res)
        m = rq._find_master_row(self.root, f"{CH}-003")
        self.assertEqual(len(m["solution"]["tables"]), 1)
        self.assertEqual(m["solution"]["tables"][0]["type"], "t2")
        srows = rq._read_jsonl(self.root / "split" / SUB / CH / "solutions.jsonl")
        self.assertEqual(len([r for r in srows
                              if r["q_id"] == f"{CH}-003"][0]["tables"]), 1)
        # second delete of a now-missing index is refused, not half-applied
        res2 = rq.apply_edit(self.root, f"{CH}-003", "table_delete", "",
                             table_index=5)
        self.assertFalse(res2["ok"])


class TestOrphanMerge(QEnv):
    """orphan suspect flow: compare panel data + merge op + dup guard."""

    def _seed_orphan(self, frag_text):
        (self.root / "data" / "orphans.jsonl").write_text(json.dumps({
            "chapter_id": CH, "batch_start": "53",
            "pdf_pages": [53, 54], "new_pages": [53, 54],
            "inferred_owner": 2, "pass": "S",
            "item": {"q_no": None, "solution_text": frag_text,
                     "tables": [{"type": "sizes", "markdown": "| s |\n|---|\n| 9 |"}]},
            "blocked_reason": "owner solution already complete"}) + "\n")

    def test_merge_appends_fragment_and_table_with_verify(self):
        self._seed_orphan("Recovered tail of the real answer.")
        frag_key = None
        for r in rq._read_jsonl(self.root / "data" / "orphans.jsonl"):
            frag_key = rq.orphan_key(r)
        res = rq.apply_orphan_merge(self.root, CH, frag_key, f"{CH}-002")
        self.assertTrue(res["ok"] and res["verified"], res)
        self.assertEqual(res["tables"], 1)
        m = rq._find_master_row(self.root, f"{CH}-002")
        self.assertIn("Recovered tail", m["solution"]["text"])
        self.assertEqual(m["solution"]["tables"][-1]["type"], "sizes")  # appended

    def test_extra_copy_refused_loudly(self):
        m = self.masters[1]
        m["solution"]["text"] = "Precise existing solution sentence here."
        self._write_masters(self.masters); self._write_split()
        self._seed_orphan("Precise existing solution sentence here.")
        fk = rq.orphan_key(rq._read_jsonl(self.root / "data" / "orphans.jsonl")[0])
        res = rq.apply_orphan_merge(self.root, CH, fk, f"{CH}-002")
        self.assertFalse(res["ok"])
        self.assertIn("extra copy", res["error"])

    def test_missing_fragment_refused(self):
        self._seed_orphan("Whatever.")
        res = rq.apply_orphan_merge(self.root, CH, "deadbeefdeadbeef", f"{CH}-002")
        self.assertFalse(res["ok"])

    def test_compare_panel_data_comes_from_flag_extra(self):
        self.masters[1]["solution"]["text"] = "Owner current solution text."
        self._write_masters(self.masters); self._write_split()
        self._seed_orphan("Owner current solution text.")   # duplicate!
        row = rq._read_jsonl(self.root / "data" / "orphans.jsonl")[0]
        flag = {"kind": "orphan_unresolved", "source": "orphans.jsonl",
                "chapter_id": CH, "detail": json.dumps(row, default=str)[:300]}
        ex = rq.flag_extra(self.root, flag)
        self.assertEqual(ex["owner_qid"], f"{CH}-002")
        self.assertTrue(ex["already_inside"])        # screen will say: just Ignore
        self.assertIn("Owner current", ex["owner_sol"])


class TestLookup(QEnv):
    def test_lookup_by_full_and_bare(self):
        rows = rq.lookup_questions(self.root, f"{CH}-002")
        self.assertEqual(len(rows), 1)
        rows = rq.lookup_questions(self.root, "2", chapter_id=CH)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], f"{CH}-002")

    def test_image_status_answers_ownership(self):
        (self.root / "assets" / "questions" / SUB).mkdir(parents=True,
                                                         exist_ok=True)
        f = f"{SUB}/{CH}-p47-128.webp"
        (self.root / "assets" / "questions" / f).write_bytes(b"\xff" * 3000)
        st = rq.image_status(self.root, f)
        self.assertTrue(st["exists_on_disk"])
        self.assertEqual(st["owners"], [])
        self.assertEqual(st["page"], 47)
        # attach it, then status must name the owner
        r = rq.apply_image_op(self.root, f"{CH}-001", "attach", f,
                              side="solution")
        self.assertTrue(r["ok"], r)
        st = rq.image_status(self.root, f"{SUB}/{CH}-001_SOL_01.webp")
        self.assertEqual(st["owners"], [f"{CH}-001"])
