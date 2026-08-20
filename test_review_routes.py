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
        self.assertIn(b"0 issue(s)", r2.data)

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


class TestAttachGallery(Routes):
    def test_thumbnails_replace_name_only_select(self):
        imgdir = self.tmp / "assets" / "questions" / SUB
        imgdir.mkdir(parents=True, exist_ok=True)
        (imgdir / f"{SUB}-p158-388.webp").write_bytes(b"\xff" * 3000)
        r = self.client.get("/review")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"att-pick", r.data)                # thumbnail picker
        self.assertIn(f"/review/img?f={SUB}/{SUB}-p158-388.webp".encode(), r.data)
        self.assertIn(b"book p158", r.data)               # page chip on thumb


class TestGroupedCards(Routes):
    """Same question flagged by the gate AND validator AND qa_status for one
    underlying problem => ONE card, ONE decision closes every flag."""

    def _multi_flag_q1(self):
        d = self.tmp / "data"
        (d / "export_gate.jsonl").write_text(json.dumps(
            {"kind": "missing_declared_figure_question", "chapter_id": CH,
             "q_no": 1, "detail": "declared but absent"}) + "\n")
        (d / "integrity_flags.jsonl").write_text(json.dumps(
            {"kind": "answer_key_disagrees", "chapter_id": CH,
             "rows": [{"q_no": 1, "record": "A", "key": "C"}]}) + "\n")

    def test_two_flags_one_card_one_click(self):
        self._multi_flag_q1()
        import review_queue as rq
        q = rq.collect_review_queue(self.tmp)
        cards = rq.group_review_rows(self.tmp, q["rows"], {
            r["flag_key"]: rq.flag_extra(self.tmp, r) for r in q["rows"]})
        q1_cards = [c for c in cards if c.get("q_id") == f"{CH}-001"]
        self.assertEqual(len(q1_cards), 1)
        self.assertEqual(len(q1_cards[0]["flag_keys"]), 3)   # qa row + gate + integrity
        self.assertIn("answer_key_disagrees", q1_cards[0]["kinds"])
        self.assertIn("missing_declared_figure_question", q1_cards[0]["kinds"])
        # one decision click closes all three
        keys = json.loads(q1_cards[0]["flag_keys_json"])
        for k in keys:
            rq.record_decision(self.tmp, k, "approved")
        q2 = rq.collect_review_queue(self.tmp)
        self.assertFalse(any(r.get("q_id") == f"{CH}-001" for r in q2["rows"]))

    def test_route_groups_and_decides_all(self):
        self._multi_flag_q1()
        import review_queue as rq
        q = rq.collect_review_queue(self.tmp)
        cards = rq.group_review_rows(self.tmp, q["rows"], {})
        keys = [c["flag_keys_json"] for c in cards if c.get("q_id") == f"{CH}-001"]
        r = self.client.post("/review-decide", data={
            "flag_keys": keys[0], "action": "approved", "reason": "group"})
        self.assertEqual(r.status_code, 302)
        q2 = rq.collect_review_queue(self.tmp)
        self.assertFalse(any(x.get("q_id") == f"{CH}-001" for x in q2["rows"]))


class TestSelfVerifyingAndGuides(Routes):
    """attach the image ONCE -> its flag auto-closes on next queue rebuild
    (user pain: 'approval ke baad bhi flag khada tha'). Same file flagged by
    3 sources -> one card (group by file)."""

    def _seed_unclaimed_flag(self):
        d = self.tmp / "data"
        (self.tmp / "assets" / "questions" / SUB).mkdir(parents=True, exist_ok=True)
        f = f"{SUB}/{CH}-p31-83.webp"
        (self.tmp / "assets" / "questions" / f).write_bytes(b"\xff" * 3000)
        (d / "unmatched_images.jsonl").write_text(json.dumps(
            {"subject": SUB, "page": 31, " files": [f], "detail": f"not owned: ['{f}']"})+"\n")
        (d / "unresolved_images.jsonl").write_text(json.dumps(
            {"subject": SUB, "page": 31, "file": f,
             "reason": "no owner after all levels"})+"\n")

    def test_same_file_one_card(self):
        self._seed_unclaimed_flag()
        import review_queue as rq
        q = rq.collect_review_queue(self.tmp)
        views = {}
        for row in q["rows"]:
            views[row["flag_key"]] = rq.flag_extra(self.tmp, row)
        cards = rq.group_review_rows(self.tmp, q["rows"], views)
        img_cards = [c for c in cards if c["kinds"] and any(
            t in k for k in c["kinds"] for t in ("image", "unresolved", "unmatched"))]
        self.assertEqual(len(img_cards), 1)
        self.assertGreaterEqual(len(img_cards[0]["flag_keys"]), 2)
        self.assertIn("Attach", img_cards[0]["guide"] + "")

    def test_attach_autocloses_its_flags(self):
        self._seed_unclaimed_flag()
        import review_queue as rq
        f = f"{SUB}/{CH}-p31-83.webp"
        r1 = rq.collect_review_queue(self.tmp)
        open0 = len(r1["rows"])
        self.assertGreaterEqual(open0, 2)                      # image flags exist
        rq.apply_image_op(self.tmp, f"{CH}-001", "attach", f, side="solution")
        r2 = rq.collect_review_queue(self.tmp)
        # every image-naming flag auto-resolved; ONLY the unrelated qa row
        # is still open -- it was never about this image
        self.assertFalse(any("p31-83" in str(x.get("detail")) for x in r2["rows"]))
        leftover = [x["kind"] for x in r2["rows"]]
        self.assertEqual(leftover, ["review_needed"],
                         "only the unrelated qa_status flag should remain")
        self.assertGreaterEqual(r2["counts"].get("auto_resolved", 0), 1)
        self.assertIn("now owned", r2["auto_resolved_rows"][0]["auto_note"])


class TestLookupFullEdit(Routes):
    def test_ready_row_is_editable_from_lookup(self):
        r = self.client.get("/review/lookup?q=1&chapter=" + CH)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Edit karo ye question", r.data)
        self.assertIn(b'value="question_text"', r.data)
        self.assertIn(b"Save answer", r.data)
        self.assertIn(b'name="back" value="/review/lookup', r.data)


class TestManualUpload(Routes):
    """User ask: figure never extracted -> human uploads the file, it lands
    under the locked slot name and attaches through the verified path."""

    def test_manual_upload_end_to_end(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (220, 160), (200, 30, 30)).save(buf, "PNG")
        buf.seek(0)
        r = self.client.post("/review/upload-image", content_type="multipart/form-data",
            data={"q_id": f"{CH}-001", "side": "solution", "reason": "manual fig",
                  "image": (buf, "fig.png")})
        self.assertEqual(r.status_code, 302)
        m = rq._find_master_row(self.tmp, f"{CH}-001")
        self.assertEqual(m["solution"]["images"][0]["file"], f"{SUB}/{CH}-001_SOL_01.webp")
        self.assertTrue((self.tmp / "assets" / "questions" / SUB /
                         f"{CH}-001_SOL_01.webp").exists())
        # junk upload refuses without leaving dust
        self.assertEqual(self.client.post("/review/upload-image",
            content_type="multipart/form-data",
            data={"q_id": f"{CH}-001", "side": "solution"}).status_code, 302)

    def test_lookup_and_queue_both_show_upload(self):
        r = self.client.get("/review/lookup?q=1&chapter=" + CH)
        self.assertIn(b"upload-image", r.data)
        r2 = self.client.get("/review")
        self.assertIn(b"upload-image", r2.data)
