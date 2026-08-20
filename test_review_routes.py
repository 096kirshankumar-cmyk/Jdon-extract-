#!/usr/bin/env python3
"""Flask-route smoke tests for the manual review screen.

The routes read Path(pipeline.OUTPUT_ROOT) per REQUEST, so monkeypatching it
in setUp redirects the whole review stack at a temp output root regardless of
import order in a combined suite run.
"""
import json
import tempfile
import unittest
from pathlib import Path

import app as webapp
import qbank_pipeline as pipeline
import review_queue as rq

SUB, CH = "OBG", "OBG-003"


def _row(qn, status="REVIEW_NEEDED"):
    qid = f"{CH}-{qn:03d}"
    return {"id": qid, "subject": SUB, "chapter_id": CH,
            "question": {"text": f"Stem {qn}?", "images": []},
            "options": [{"id": L, "text": f"{L} t{qn}", "images": []}
                        for L in "ABCD"],
            "correct_options": ["A"],
            "solution": {"text": f"Sol {qn}.", "images": [], "tables": []},
            "source_pages": [50], "qa_status": status,
            "qa_reasons": ["fixture flag"]}


class Routes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rq_route_"))
        (self.tmp / "data").mkdir(parents=True)
        (self.tmp / "split" / SUB / CH).mkdir(parents=True)
        self._saved_root = pipeline.OUTPUT_ROOT
        pipeline.OUTPUT_ROOT = self.tmp
        self._saved_status = dict(webapp.state)
        webapp.state["status"] = "idle"
        rows = [_row(1), _row(2, "READY")]
        lines = "\n".join(json.dumps(r) for r in rows) + "\n"
        (self.tmp / "data" / "questions.jsonl").write_text(lines)
        (self.tmp / "split" / SUB / CH / "questions.jsonl").write_text(
            "".join(json.dumps({"q_id": r["id"], "chapter_id": CH,
                                "subject": SUB, "chapter_no": 3,
                                "q_no": int(r["id"].rsplit("-", 1)[1]),
                                "question_text": r["question"]["text"],
                                "options": [{"id": o["id"], "text": o["text"],
                                             "images": []}
                                            for o in r["options"]],
                                "question_images": [], "tables": [],
                                "source_pages": [50],
                                "extraction_status": "COMPLETE"})
                    + "\n" for r in rows))
        self.client = webapp.app.test_client()

    def tearDown(self):
        pipeline.OUTPUT_ROOT = self._saved_root
        webapp.state.clear(); webapp.state.update(self._saved_status)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_review_page_lists_open_flag(self):
        resp = self.client.get("/review")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(f"{CH}-001", html)
        self.assertIn("review", html.lower())

    def test_decide_via_route_clears_from_page(self):
        q = rq.collect_review_queue(self.tmp)
        fk = q["rows"][0]["flag_key"]
        resp = self.client.post("/review-decide", data={
            "flag_key": fk, "action": "approved", "reason": "seen",
            "q_id": f"{CH}-001"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        q2 = rq.collect_review_queue(self.tmp)
        self.assertTrue(q2["clear"])
        # persisted on disk — refresh = same state
        self.assertIn("approved",
                      (self.tmp / "data" / "review_decisions.jsonl").read_text())

    def test_text_edit_route_updates_and_verifies(self):
        resp = self.client.post("/review/apply-text", data={
            "q_id": f"{CH}-001", "field": "question_text",
            "value": "Fixed via screen?", "reason": "book typo"})
        self.assertEqual(resp.status_code, 302)
        row = [json.loads(l) for l in
               (self.tmp / "data" / "questions.jsonl").read_text().splitlines()][0]
        self.assertEqual(row["question"]["text"], "Fixed via screen?")

    def test_edit_locked_while_pipeline_processing(self):
        webapp.state["status"] = "processing"
        resp = self.client.post("/review/apply-text", data={
            "q_id": f"{CH}-001", "field": "question_text", "value": "X?"})
        self.assertEqual(resp.status_code, 302)
        row = [json.loads(l) for l in
               (self.tmp / "data" / "questions.jsonl").read_text().splitlines()][0]
        self.assertEqual(row["question"]["text"], "Stem 1?")   # NOT touched

    def test_final_zip_423_locked_then_ok(self):
        resp = self.client.get("/download-final")
        self.assertEqual(resp.status_code, 423)          # queue open -> locked
        for r in rq.collect_review_queue(self.tmp)["rows"]:
            rq.record_decision(self.tmp, r["flag_key"], "approved",
                               q_id=r.get("q_id"))
        resp = self.client.get("/download-final")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")


if __name__ == "__main__":
    unittest.main()


class TestTemplateReadability(Routes):
    """The screen must render CONTENT readably: inline images, HTML tables,
    page-context links, filters -- never raw markdown only."""

    def seed_with_image_and_table(self):
        imgdir = self.tmp / "assets" / "questions" / SUB
        imgdir.mkdir(parents=True, exist_ok=True)
        (imgdir / f"{CH}-001_SOL_01.webp").write_bytes(b"\xff" * 3000)
        rows = [_row(1)]
        rows[0]["solution"]["images"] = [{"file": f"{SUB}/{CH}-001_SOL_01.webp"}]
        rows[0]["solution"]["tables"] = [{"type": "cmp",
                                          "markdown": "| a | b |\n|---|---|\n| 1 | 2 |"}]
        (self.tmp / "data" / "questions.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")

    def test_renders_readable_content(self):
        self.seed_with_image_and_table()
        r = self.client.get("/review")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"/review/img?f=", r.data)      # inline image
        self.assertIn(b"<table", r.data)              # table rendered as HTML
        self.assertIn(b"all chapters", r.data)        # filter bar
        self.assertIn(b"delete table", r.data.lower())
        self.assertIn(b"/review/page?subject=", r.data)  # book-page context links

    def test_filters(self):
        r = self.client.get("/review?kind=review_needed")
        self.assertEqual(r.status_code, 200)
        r2 = self.client.get("/review?kind=nonsense")
        self.assertIn(b"0 row(s) shown", r2.data)

    def test_img_endpoint_guards(self):
        self.seed_with_image_and_table()
        ok = self.client.get(f"/review/img?f={SUB}/{CH}-001_SOL_01.webp")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.client.get("/review/img?f=../../etc/passwd").status_code, 400)
        self.assertEqual(self.client.get(f"/review/img?f={SUB}/nope.webp").status_code, 404)


class TestLookupFullView(Routes):
    """Lookup page = full app-style view: complete stem/options/solution,
    rendered tables, all images -- never a 240-char preview."""

    def seed_long(self):
        imgdir = self.tmp / "assets" / "questions" / SUB
        imgdir.mkdir(parents=True, exist_ok=True)
        (imgdir / f"{CH}-p50-128.webp").write_bytes(b"\xff" * 3000)
        long_sol = "Long solution. " * 60       # way past any preview cap
        rows = [_row(1)]
        rows[0]["solution"]["text"] = long_sol
        (self.tmp / "data" / "questions.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")

    def test_full_view_and_attach_link(self):
        self.seed_long()
        r = self.client.get("/review/lookup?q=1&chapter=" + CH)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Long solution. ".encode() * 50, r.data)   # NOT truncated
        self.assertIn((f"{CH}-001").encode(), r.data)

    def test_file_status_and_auto_chapter(self):
        self.seed_long()
        r = self.client.get(f"/review/lookup?q=1&f={SUB}/{CH}-p50-128.webp")
        self.assertIn(b"File status", r.data)
        self.assertIn(b"KISI KO NAHI", r.data)       # honestly shows unlinked
        # page 50 IS in fixture chapter range -> note shows only when detected
        auto = b"auto-detected" in r.data
        self.assertTrue(auto)
