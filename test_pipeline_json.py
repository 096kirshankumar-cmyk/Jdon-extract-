import copy
import inspect
import json
import pathlib
import shutil
import tempfile
import unittest
import zlib
from unittest import mock
from pathlib import Path

from PIL import Image

import qbank_pipeline as qp
import qbank_validator as qv
from qbank_pipeline import _dedupe_tables, _normalize_solution_payload, looks_truncated_solution, parse_gemini_json_array


def _write_test_pdf(path, texts, images, img_size=(20, 10)):
    """Build a tiny single-page PDF (612x792) with Helvetica text at
    (x, y) -- y in PDF user space, origin bottom-left -- and one red image
    XObject per (obj_num, name, x, y). Pure-python; no poppler needed."""
    objects = {}
    stream_parts = []
    for i, (t, x, y, sz) in enumerate(texts):
        # leading "0 0 Td" resets the text line matrix: pypdf's visitor
        # reports tm=(0,0) for a second run on the SAME baseline without it
        # (needed for horizontal/2x2 option rows in the tests)
        stream_parts.append(f"BT 0 0 Td /F1 {sz} Tf {x} {y} Td ({t}) Tj ET".encode("latin-1"))
    w, h = img_size
    img_data = zlib.compress(b"\xff\x00\x00" * (w * h))
    xobjs = {}
    for obj_num, name, x, y in images:
        objects[obj_num] = (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                            f"/Length {len(img_data)} >>\nstream\n{img_data.decode('latin-1')}\nendstream")
        xobjs[name] = f"{obj_num} 0 R"
        stream_parts.append(f"q {w} 0 0 {h} {x} {y} cm /{name} Do Q".encode("latin-1"))
    objects[5] = "<< /Length %d >>\nstream\n%s\nendstream" % (
        sum(len(p) + 1 for p in stream_parts), b"\n".join(stream_parts).decode("latin-1"))
    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
    xres = " ".join(f"/{n} {r}" for n, r in xobjs.items())
    objects[3] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                  f"/Resources << /Font << /F1 4 0 R >> /XObject << {xres} >> >> /Contents 5 0 R >>")
    objects[4] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    out = b"%PDF-1.4\n"
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num].encode("latin-1") + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for num in sorted(objects):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    path.write_bytes(out)


class SolutionFigureMappingTests(unittest.TestCase):
    """Regression tests for the solutions-page figure mapping fix
    (user report: a 7-figure solutions page collapsed into 2 solutions
    because a single decoded header swallowed every image on the page)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_assets = qp.ASSETS_DIR
        qp.ASSETS_DIR = self.tmp / "assets"
        self.subj_dir = qp.ASSETS_DIR / "questions" / "PSY"
        self.subj_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.ASSETS_DIR = self._old_assets

    def _claim(self, pdf, oids, recs, page=1):
        rels = []
        for oid in oids:
            fname = f"PSY-p{page}-{oid}.webp"
            (self.subj_dir / fname).write_bytes(b"x" * 3000)  # > MIN_IMAGE_BYTES
            rels.append(f"PSY/{fname}")
        image_files_by_q = {}
        leftover = qp.claim_page_images(rels, pdf, page, "PSY", 1, recs, image_files_by_q)
        return leftover, image_files_by_q

    def test_headers_located_with_positions_top_first(self):
        pdf = self.tmp / "solutions.pdf"
        _write_test_pdf(pdf, [
            ("Solution to Question 3:", 72, 700, 12),
            ("Explanation text for q3", 72, 660, 10),
            ("Solution to Question 7:", 72, 550, 12),
            ("Solution to Question 2:", 72, 400, 12),
        ], [])
        self.assertEqual(qp.solution_headers_on_page(pdf, 1, {2: {}, 3: {}, 7: {}}),
                         [(3, 700.0), (7, 550.0), (2, 400.0)])
        # a header whose q_no is not in the chapter is ignored
        self.assertEqual(qp.solution_headers_on_page(pdf, 1, {3: {}}), [(3, 700.0)])

    def test_each_figure_maps_to_its_own_solution_block(self):
        # 3 headers at y=700/550/400, one figure under each (y=640/490/340)
        pdf = self.tmp / "three_blocks.pdf"
        _write_test_pdf(pdf, [
            ("Solution to Question 3:", 72, 700, 12),
            ("Solution to Question 7:", 72, 550, 12),
            ("Solution to Question 2:", 72, 400, 12),
        ], [(6, "Im6", 300, 640), (7, "Im7", 300, 490), (8, "Im8", 300, 340)])
        recs = {2: {"has_figure_in_solution": True},
                3: {"has_figure_in_solution": True},
                7: {"has_figure_in_solution": True}}
        leftover, owned = self._claim(pdf, [6, 7, 8], recs)
        self.assertEqual(leftover, [])
        # each figure lands on the solution whose header is drawn above it
        self.assertEqual(owned[3]["solution"], ["PSY/PSY-001-003_SOL_01.webp"])
        self.assertEqual(owned[7]["solution"], ["PSY/PSY-001-007_SOL_01.webp"])
        self.assertEqual(owned[2]["solution"], ["PSY/PSY-001-002_SOL_01.webp"])

    def test_under_detected_headers_no_longer_swallow_the_page(self):
        # ONE decoded header but FIVE figures below it (the old code dumped
        # all five onto that one solution) -> cap at MAX_SOLUTION_IMAGES,
        # the rest stay unclaimed for the model/manual pass.
        pdf = self.tmp / "one_block.pdf"
        _write_test_pdf(pdf, [
            ("Solution to Question 3:", 72, 700, 12),
            ("Explanation text for q3", 72, 660, 10),
        ], [(6, "Im6", 300, 640), (7, "Im7", 300, 600), (8, "Im8", 300, 560),
            (9, "Im9", 300, 520), (10, "Im10", 300, 480)])
        leftover, owned = self._claim(pdf, [6, 7, 8, 9, 10], {3: {"has_figure_in_solution": True}})
        self.assertEqual(len(owned[3]["solution"]), qp.MAX_SOLUTION_IMAGES)
        self.assertEqual(len(leftover), 3)

    def test_figure_above_all_headers_is_not_guessed(self):
        # figure drawn ABOVE the only header -> no deterministic owner
        pdf = self.tmp / "above_header.pdf"
        _write_test_pdf(pdf, [
            ("Solution to Question 5:", 72, 400, 12),
        ], [(6, "Im6", 300, 500)])
        leftover, owned = self._claim(pdf, [6], {5: {"has_figure_in_solution": True}})
        self.assertEqual(leftover, ["PSY/PSY-p1-6.webp"])
        self.assertEqual((owned.get(5) or {}).get("solution") or [], [])

    def test_no_headers_means_no_auto_claim(self):
        pdf = self.tmp / "no_headers.pdf"
        _write_test_pdf(pdf, [("Plain text with no headers", 72, 700, 12)],
                        [(6, "Im6", 300, 500)])
        leftover, owned = self._claim(pdf, [6], {5: {"has_figure_in_solution": True}})
        self.assertIn("PSY/PSY-p1-6.webp", leftover)
        self.assertEqual((owned.get(5) or {}).get("solution") or [], [])


class RetryForeignFragmentGuardTests(unittest.TestCase):
    """Wrong-owner guard for targeted-retry solution continuations
    (external-audit 2026-08-02: q16's truncated re-ask returned q17's
    solution and the old code APPENDED it, blending two solutions)."""

    def setUp(self):
        self.rec = {"q_no": 16, "options": {"A": "alpha", "B": "beta",
                                            "C": "gamma", "D": "delta"},
                    "solution_text": "q16's own partial explanation leads to:"}
        self.chapter = {15: {"solution_text": "q15's solution text"},
                        16: self.rec,
                        17: {"solution_text": "q17's completely different "
                                              "explanation of q17's topic"}}

    def test_genuine_continuation_is_kept(self):
        frag = "and here the continuation continues without overlap"
        self.assertIsNone(qp._solution_fragment_foreign(frag, 16, self.rec, self.chapter))

    def test_foreign_option_line_head_blocked(self):
        # owner has no 'Option D' explanation topic matching this line
        frag = "Option D: the exact wording of some other question's option"
        self.assertIsNotNone(qp._solution_fragment_foreign(frag, 16, self.rec, self.chapter))

    def test_embedded_solution_header_for_another_question_blocked(self):
        frag = "text...\nSolution to Question 17: q17's completely different explanation"
        self.assertIsNotNone(qp._solution_fragment_foreign(frag, 16, self.rec, self.chapter))

    def test_own_header_not_blocked(self):
        frag = "text...\nSolution to Question 16: continued"
        self.assertIsNone(qp._solution_fragment_foreign(frag, 16, self.rec, self.chapter))

    def test_first_line_verbatim_in_sibling_blocked(self):
        # the retry fragment restates q17's solution -- its first line IS a
        # verbatim line of q17's solution (sibling-donor proof)
        self.chapter[17]["solution_text"] = ("q17's completely different explanation of q17's "
                                             "topic with lots more detail and then even more")
        frag = ("q17's completely different explanation of q17's topic with lots more "
                "detail and then even more\nand the fragment continues here")
        self.assertIsNotNone(qp._solution_fragment_foreign(frag, 16, self.rec, self.chapter))


class OrphanForeignGuardTests(unittest.TestCase):
    """recover_orphans rule-3 append must not glue a neighbour's solution
    onto a partial owner (audit foreign-tail candidates: 006-014, 011-017,
    011-026, 012-002, 014-015, 022-008)."""

    def test_foreign_fragment_blocked_and_kept_for_review(self):
        recs = {
            16: {"q_no": 16, "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                 "question_text": "q16 stem", "correct_option": "B",
                 "solution_text": "q16's own partial explanation leads to:", "tables": []},
            17: {"q_no": 17, "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                 "question_text": "q17 stem", "correct_option": "C",
                 "solution_text": "q17's completely different explanation of q17's "
                                  "topic with lots more detail and then even more",
                 "tables": []},
        }
        frag = ("q17's completely different explanation of q17's topic with lots more "
                "detail and then even more\nand the fragment continues here")
        orphans = [{"chapter_id": "PSY-016", "batch_start": 0, "pdf_pages": [1],
                    "new_pages": [1], "carry_q_no": None, "cut_part": None,
                    "last_qn_in_batch": 16,
                    "item": {"q_no": None, "question_text": None, "options": None,
                             "correct_option": None, "solution_text": frag,
                             "tables": [], "has_figure_in_question": False,
                             "has_figure_in_solution": False}}]
        stats = {"orphans_recovered": 0, "foreign_fragments_blocked": 0,
                 "carry_merges": 0, "chapter_id": "PSY-016"}
        remaining = qp.recover_orphans(orphans, recs, "PSY", 16, stats)
        # the fragment must NOT be appended to q16
        self.assertNotIn("q17's completely", recs[16]["solution_text"])
        self.assertIn("q17's completely", recs[17]["solution_text"])
        # and must be kept for review with a blocked reason
        self.assertEqual(len(remaining), 1)
        self.assertIn("blocked_reason", remaining[0])
        self.assertIn("foreign", remaining[0]["blocked_reason"])
        self.assertEqual(stats["foreign_fragments_blocked"], 1)

    def test_genuine_continuation_still_appends(self):
        recs = {
            16: {"q_no": 16, "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                 "question_text": "q16 stem", "correct_option": "B",
                 "solution_text": "q16's own partial explanation leads to:", "tables": []},
        }
        frag = "and here the genuine continuation continues without any overlap"
        orphans = [{"chapter_id": "PSY-016", "batch_start": 0, "pdf_pages": [1],
                    "new_pages": [1], "carry_q_no": None, "cut_part": None,
                    "last_qn_in_batch": 16,
                    "item": {"q_no": None, "solution_text": frag, "tables": [],
                             "question_text": None, "options": None,
                             "correct_option": None}}]
        stats = {"orphans_recovered": 0, "foreign_fragments_blocked": 0,
                 "carry_merges": 0, "chapter_id": "PSY-016"}
        remaining = qp.recover_orphans(orphans, recs, "PSY", 16, stats)
        self.assertIn("genuine continuation", recs[16]["solution_text"])
        self.assertEqual(remaining, [])
        self.assertEqual(stats["orphans_recovered"], 1)


class SolutionGateBypassTests(unittest.TestCase):
    """find_incomplete_records must treat a printed 'Solution to Question N:'
    header as per-question proof the book prints that explanation, even when
    the chapter as a whole is below the 60% gate (ch25 class)."""

    def _chapter(self):
        records = {}
        for i in range(1, 10):
            rec = {"q_no": i, "question_text": f"stem {i}",
                   "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                   "correct_option": "A"}
            rec["solution_text"] = f"solution {i}" if i <= 4 else None  # 4/9 = 44% < 60%
            records[i] = rec
        return records

    def test_gate_suppresses_below_threshold(self):
        incomplete = qp.find_incomplete_records(self._chapter())
        sol_qns = {qn for qn, missing in incomplete if "solution" in missing}
        self.assertEqual(sol_qns, set())

    def test_printed_header_bypasses_gate_for_that_qn_only(self):
        incomplete = qp.find_incomplete_records(self._chapter(),
                                                printed_solution_qns={7})
        sol_qns = {qn for qn, missing in incomplete if "solution" in missing}
        self.assertEqual(sol_qns, {7})


class AnchorlessDropTests(unittest.TestCase):
    """Records with no stem/options/solution after all recovery are phantom
    answer-key rows (ch24 q12/13 class) -- dropped with a ledger entry."""

    def test_anchorless_detection(self):
        self.assertTrue(qp._anchorless_record(
            {"q_no": 12, "question_text": None, "options": None,
             "solution_text": None, "correct_option": None}))
        self.assertFalse(qp._anchorless_record(
            {"q_no": 12, "question_text": None, "options": None,
             "solution_text": "x", "correct_option": None}))
        self.assertFalse(qp._anchorless_record(
            {"q_no": 12, "question_text": "q", "options": {},
             "solution_text": None, "correct_option": None}))
        self.assertFalse(qp._anchorless_record(
            {"q_no": 12, "question_text": None, "options": {"A": "a"},
             "solution_text": None, "correct_option": None}))


class LocatePagesTests(unittest.TestCase):
    """locate_missing_record_pages finds the pages where a missing q_no is
    printed (question stem or solution header) via the text layer."""

    def test_locate_question_stem_and_solution_header_pages(self):
        fake = {1: "1. Question one\n2. Question two\n",
                2: "Solution to Question 7:\nExplanation\n",
                3: "plain text with no markers\n"}
        orig = qp.pdftotext_page
        qp.pdftotext_page = lambda pdf, page: fake.get(page, "")
        try:
            page_files = [Path(f"/tmp/x/page-{n:03d}.jpg") for n in (1, 2, 3)]
            loc = qp.locate_missing_record_pages("pdf", page_files,
                                                 {1: None, 2: None, 7: None}, {})
        finally:
            qp.pdftotext_page = orig
        self.assertEqual(loc, {1: [1], 2: [1], 7: [2]})

    def test_unlocatable_qn_is_omitted(self):
        orig = qp.pdftotext_page
        qp.pdftotext_page = lambda pdf, page: "nothing useful\n"
        try:
            page_files = [Path("/tmp/x/page-001.jpg"), Path("/tmp/x/page-002.jpg")]
            loc = qp.locate_missing_record_pages("pdf", page_files, {9: None}, {})
        finally:
            qp.pdftotext_page = orig
        self.assertEqual(loc, {})


class DedupeQuestionsTests(unittest.TestCase):
    """Surgical re-runs append duplicate rows; _dedupe_questions_by_id keeps
    the newest row per id (idempotent re-runs at 20-book scale)."""

    def test_keeps_last_row_per_id(self):
        path = Path(tempfile.mkdtemp()) / "questions.jsonl"
        path.write_text('{"id": "PSY-001-001", "v": 1}\n'
                        '{"id": "PSY-001-001", "v": 2}\n'
                        '{"id": "PSY-001-002", "v": 1}\n', encoding="utf-8")
        n = qp._dedupe_questions_by_id(path)
        self.assertEqual(n, 1)
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 2)
        by_id = {r["id"]: r["v"] for r in rows}
        self.assertEqual(by_id, {"PSY-001-001": 2, "PSY-001-002": 1})


class SectionWindowTests(unittest.TestCase):
    """build_section_windows must send the chapter in section-sized windows:
    the whole questions+answers stretch in LARGE windows (1-2 calls, 1-page
    overlap -- no boundary splits, 33%->10% overlap-token waste), and the
    Solutions section in smaller recitation-safe chunks. Pass activation is
    deliberately NOT changed (probe-based), so section labels here only SIZE
    windows and mark the carry reset."""

    def setUp(self):
        self.files = [Path(f"/tmp/s-{n:03d}.jpg") for n in range(3, 17)]

    def _fake_text(self, mapping):
        orig = qp.pdftotext_page
        qp.pdftotext_page = lambda pdf, page: mapping.get(page, "")
        self.addCleanup(setattr, qp, "pdftotext_page", orig)

    def test_questions_and_solutions_planned(self):
        # ch1-like: questions pp.3-10 (answer key interleaved at 7-8),
        # solutions pp.11-16
        text = {p: "1. Question\n2. Question\n" for p in range(3, 11)}
        text.update({p: "ANSWER KEY\n| Question No. | Correct Option |" for p in (7, 8)})
        text.update({p: "Solution to Question 1:\nSolution to Question 2:\n"
                        for p in range(11, 17)})
        self._fake_text(text)
        wins = qp.build_section_windows(self.files, "pdf")
        sections = [s for _, s in wins]
        self.assertEqual(sections, ["Q", "S", "S"])
        # the whole question+answer stretch in ONE large window (8 pages)
        self.assertEqual(wins[0][0], list(range(3, 11)))
        # RUN-12 cross-section overlap: the first S window includes the last
        # question page (10) so a boundary-spanning question's tail is seen
        self.assertEqual(wins[1][0], [10, 11, 12, 13, 14])
        self.assertEqual(wins[2][0], [15, 16])
        # page 10 is shared across the Q/S boundary (tail continuity)
        self.assertEqual(set(wins[0][0]) & set(wins[1][0]), {10})

    def test_big_question_section_chunked_too(self):
        # 12 question pages -> 2 Q windows with 1-page overlap, then solutions
        text = {p: "1. Question\n" for p in range(3, 15)}
        text.update({p: "Solution to Question 1:\nSolution to Question 2:\n"
                        for p in range(15, 17)})
        self._fake_text(text)
        wins = qp.build_section_windows(self.files, "pdf")
        q_wins = [w for w, s in wins if s == "Q"]
        self.assertEqual(len(q_wins), 2)
        self.assertEqual(len(q_wins[0]), qp.QUESTIONS_CHUNK_PAGES)
        self.assertEqual(q_wins[0][-1], q_wins[1][0])  # 1-page overlap

    def test_answer_key_only_chapter_falls_back(self):
        # no solutions section at all -> fixed-window fallback ([] means the
        # caller keeps the old 6-page loop, which never skips a pass)
        files = [Path(f"/tmp/s-{n:03d}.jpg") for n in range(3, 11)]
        text = {p: "1. Question\n" for p in range(3, 9)}
        text[9] = "ANSWER KEY\n| Question No. | Correct Option |"
        text[10] = "ANSWER KEY\n| Question No. | Correct Option |"
        self._fake_text(text)
        self.assertEqual(qp.build_section_windows(files, "pdf"), [])

    def test_no_sections_detected_falls_back(self):
        self._fake_text({p: "just prose\n" for p in range(3, 17)})
        self.assertEqual(qp.build_section_windows(self.files, "pdf"), [])

    def test_garbled_text_layer_falls_back(self):
        self._fake_text({})
        self.assertEqual(qp.build_section_windows(self.files, "pdf"), [])

    def test_solutions_chunked_with_intra_section_overlap(self):
        # solutions pp.6-13 (8 pages) -> 2 S windows; the first S window
        # carries the boundary page 5 (cross-section overlap for the tail)
        files = [Path(f"/tmp/s-{n:03d}.jpg") for n in range(3, 14)]
        text = {p: "1. Q\n" for p in (3, 4)}
        text[5] = "ANSWER KEY\n| Q No | Answer |"
        text.update({p: "Solution to Question 1:\nSolution to Question 2:\n"
                        for p in range(6, 14)})
        self._fake_text(text)
        wins = qp.build_section_windows(files, "pdf")
        q_wins = [w for w, s in wins if s == "Q"]
        s_wins = [w for w, s in wins if s == "S"]
        self.assertEqual(len(s_wins), 2)
        self.assertEqual(len(s_wins[0]), qp.SOLUTIONS_CHUNK_PAGES)
        # first S window starts with the boundary page (5) shared with the Q
        # window -> a question spanning 5->6 keeps its tail in the Q pass
        self.assertEqual(s_wins[0][0], 5)
        self.assertEqual(q_wins[0][-1], 5)
        self.assertEqual(s_wins[1][0], 10)   # remaining solutions chunk


class FigureMapTests(unittest.TestCase):
    """The _figure_map control object (Gemini declares q_no+slot per figure in
    reading order) must be peeled by extract_batch_meta and used by
    claim_figure_map_images to attach images to their questions -- the
    run-6 user ask ("bta ye image kis question ki h") to stop unclaimed
    images."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_assets = qp.ASSETS_DIR
        qp.ASSETS_DIR = self.tmp / "assets"
        self.subj_dir = qp.ASSETS_DIR / "questions" / "PSY"
        self.subj_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.ASSETS_DIR = self._old_assets

    def _rels(self, oids, page=1):
        rels = []
        for oid in oids:
            fname = f"PSY-p{page}-{oid}.webp"
            (self.subj_dir / fname).write_bytes(b"x" * 3000)
            rels.append(f"PSY/{fname}")
        return rels

    def test_extract_batch_meta_peels_figure_map(self):
        items, meta = qp.extract_batch_meta([
            {"q_no": 1, "question_text": "s"},
            {"_figure_map": [{"q_no": 1, "slot": "question"},
                             {"q_no": None, "slot": None}]},
            {"_batch_meta": {"last_q_no": 1, "ends_mid_content": False}},
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual(meta["figure_map"][0], {"q_no": 1, "slot": "question"})
        self.assertEqual(meta["last_q_no"], 1)

    def test_exact_count_map_claims_every_image(self):
        fig_map = [{"q_no": 3, "slot": "question"},
                   {"q_no": 7, "slot": "solution"},
                   {"q_no": None, "slot": None}]
        rels = self._rels([6, 7, 8])
        window_rows = [(1, rels)]
        owned = {}
        remaining = qp.claim_figure_map_images(fig_map, window_rows, "PSY", 1,
                                               {3: {}, 7: {}}, owned)
        self.assertEqual(owned[3]["question"], ["PSY/PSY-001-003_Q_01.webp"])
        self.assertEqual(owned[7]["solution"], ["PSY/PSY-001-007_SOL_01.webp"])
        # the decorative entry (q_no null) left ITS image unclaimed -- the
        # alignment stayed exact for the two real owners
        self.assertEqual(remaining[1], ["PSY/PSY-p1-8.webp"])

    def test_count_mismatch_skips_entirely(self):
        fig_map = [{"q_no": 3, "slot": "question"}]   # 1 declared, 2 extracted
        rels = self._rels([6, 7])
        remaining = qp.claim_figure_map_images(fig_map, [(1, rels)], "PSY", 1,
                                               {3: {}}, {})
        self.assertEqual(set(remaining[1]), set(rels))  # nothing claimed

    def test_unknown_q_or_bad_slot_stays_unclaimed(self):
        fig_map = [{"q_no": 99, "slot": "question"},   # not in chapter
                   {"q_no": 3, "slot": "sideways"}]    # invalid slot
        rels = self._rels([6, 7])
        remaining = qp.claim_figure_map_images(fig_map, [(1, rels)], "PSY", 1,
                                               {3: {}}, {})
        self.assertEqual(set(remaining[1]), set(rels))

    def test_guard_refused_image_stays_but_others_claimed(self):
        # first image too small -> tiny-crop guard refuses rename
        rels = self._rels([6, 7])
        (self.subj_dir / "PSY-p1-6.webp").write_bytes(b"x" * 100)  # < MIN_IMAGE_BYTES
        fig_map = [{"q_no": 3, "slot": "question"},
                   {"q_no": 3, "slot": "question"}]
        owned = {}
        remaining = qp.claim_figure_map_images(fig_map, [(1, rels)], "PSY", 1,
                                               {3: {}}, owned)
        # the tiny one refused -> stays; the other claimed
        self.assertIn("PSY/PSY-p1-6.webp", remaining[1])
        self.assertNotIn("PSY/PSY-p1-7.webp", remaining[1])


class CrossFieldContaminationTests(unittest.TestCase):
    """Run-7 hardening: a recovered SOLUTION fragment must never populate
    question_text, recovery is patch-only by field, OCR noise is stripped
    before merge, and 'field is populated' != 'field is valid'."""

    def _rec(self, qn, **kw):
        r = {"q_no": qn, "question_text": None, "options": None,
             "correct_option": None, "solution_text": None, "tables": [],
             "has_figure_in_question": False, "has_figure_in_solution": False,
             "_prov": {}}
        r.update(kw)
        return r

    # -- 1. S-pass recovery must never fill the stem -------------------------
    def test_s_pass_item_cannot_fill_question_text(self):
        item = {"q_no": 3, "_prov": "S_PASS",
                "question_text": "The correct answer is B because the basal "
                                 "ganglia circuit is disrupted in OCD patients "
                                 "and this explains the compulsions seen here "
                                 "with additional detail about the pathway.",
                "solution_text": "The correct answer is B because the basal "
                                 "ganglia circuit is disrupted in OCD patients "
                                 "and this explains the compulsions seen here "
                                 "with additional detail about the pathway.",
                "options": None, "correct_option": "B", "tables": []}
        recs, _ = qp.merge_question_records({}, [item], stats := {"chapter_id": "PSY-016"})
        self.assertIsNone(recs[3]["question_text"])   # stem NEVER populated
        self.assertEqual(recs[3]["solution_text"], item["solution_text"])

    def test_ocr_s_item_cannot_fill_question_text(self):
        item = {"q_no": 7, "_prov": "OCR_S",
                "question_text": "Ans. is C. The dissociation amnesia "
                                 "resolves when the patient is removed from "
                                 "the stressful military environment and "
                                 "supportive psychotherapy is instituted.",
                "solution_text": "Ans. is C. The dissociation amnesia "
                                 "resolves when the patient is removed from "
                                 "the stressful military environment and "
                                 "supportive psychotherapy is instituted.",
                "options": None, "correct_option": "C", "tables": []}
        recs, _ = qp.merge_question_records({}, [item], {"chapter_id": "PSY-017"})
        self.assertIsNone(recs[7]["question_text"])

    # -- 2. q_no=None OCR fragment containing a neighbor's solution ----------
    def test_s_orphan_cannot_fill_stem_via_recover_orphans(self):
        frag = ("Ans. is A. The patient's symptoms of depersonalisation "
                "resolve gradually with cognitive behavioural therapy and "
                "grounding techniques over several months of treatment.")
        orphans = [{"chapter_id": "PSY-017", "batch_start": 0,
                    "pdf_pages": [218], "new_pages": [218],
                    "carry_q_no": None, "cut_part": None,
                    "last_qn_in_batch": 10, "pass": "S",
                    "item": {"q_no": None, "question_text": frag,
                             "solution_text": frag, "options": None,
                             "correct_option": None, "tables": [],
                             "has_figure_in_question": False,
                             "has_figure_in_solution": False}}]
        recs = {10: self._rec(10, question_text="Real stem ten",
                              solution_text="partial solution ten")}
        stats = {"orphans_recovered": 0, "foreign_fragments_blocked": 0,
                 "carry_merges": 0, "contaminated_stems_blocked": 0,
                 "chapter_id": "PSY-017"}
        qp.recover_orphans(orphans, recs, "PSY", 17, stats)
        # the real stem survives; the S-fragment's stem text is blocked
        self.assertEqual(recs[10]["question_text"], "Real stem ten")
        self.assertIn("partial solution ten", recs[10]["solution_text"])
        self.assertGreaterEqual(stats["contaminated_stems_blocked"], 0)

    # -- 3. OCR cleanup strips page numbers / watermarks ---------------------
    def test_clean_ocr_text_strips_page_noise(self):
        dirty = ("Solution to Question 3:\n"
                 "The diagnosis is delirium.\n"
                 "12\n"
                 "- 45 -\n"
                 "Page 12 of 300\n"
                 "www.example-qbank.com\n"
                 "© 2026 Example Publishers\n")
        clean = qp._clean_ocr_text(dirty)
        self.assertIn("The diagnosis is delirium.", clean)
        self.assertNotIn("\n12\n", "\n" + clean + "\n")
        self.assertNotIn("- 45 -", clean)
        self.assertNotIn("Page 12 of 300", clean)
        self.assertNotIn("www.example", clean)
        self.assertNotIn("©", clean)
        self.assertIn("Solution to Question 3:", clean)  # header preserved

    def test_clean_ocr_text_preserves_prose(self):
        text = ("The key feature is that the mood episode is not better "
                "explained by substance use.\n")
        # content is preserved verbatim (trailing newline normalization from
        # splitlines is the only difference)
        self.assertEqual(qp._clean_ocr_text(text).strip(), text.strip())

    # -- 4. non-empty question consisting of solution prose ------------------
    def test_contaminated_stem_rejected_at_merge(self):
        sol = ("The correct answer is A. In Korsakoff syndrome the amnesia "
               "is characterised by anterograde and retrograde memory loss "
               "with confabulation, and the pathology lies in the mammillary "
               "bodies and the dorsomedial nucleus of the thalamus with "
               "severe vitamin B1 deficiency being the underlying cause.")
        item = {"q_no": 5, "_prov": "Q_PASS",
                "question_text": sol,   # contaminated: is the solution
                "solution_text": sol, "options": None, "correct_option": "A",
                "tables": []}
        stats = {"chapter_id": "PSY-010", "contaminated_stems_rejected": 0}
        recs, _ = qp.merge_question_records({}, [item], stats)
        self.assertIsNone(recs[5]["question_text"])   # rejected, not shipped
        self.assertEqual(recs[5]["solution_text"], sol)
        self.assertEqual(stats["contaminated_stems_rejected"], 1)

    def test_find_incomplete_treats_contaminated_stem_as_missing(self):
        sol = ("The correct answer is B. Body dysmorphic disorder involves "
               "a preoccupation with an imagined defect in appearance that "
               "causes clinically significant distress and impaired "
               "functioning with repetitive checking behaviours.")
        recs = {9: self._rec(9, question_text=sol, solution_text=sol,
                             correct_option="B",
                             options={"A": "a", "B": "b", "C": "c", "D": "d"})}
        incomplete = qp.find_incomplete_records(recs)
        self.assertTrue(any(qn == 9 and "question" in missing
                            for qn, missing in incomplete))

    # -- 5. valid existing stem survives S/OCR recovery unchanged ------------
    def test_valid_stem_survives_s_pass_merge(self):
        recs = {4: self._rec(4, question_text="Which neurotransmitter is "
                                              "reduced in Parkinson's disease?",
                             solution_text="Dopamine is reduced.")}
        item = {"q_no": 4, "_prov": "S_PASS",
                "question_text": "stray solution prose that must not land",
                "solution_text": "Dopamine is reduced in the substantia nigra.",
                "options": None, "correct_option": "A", "tables": []}
        qp.merge_question_records(recs, [item], {"chapter_id": "PSY-027"})
        self.assertEqual(recs[4]["question_text"],
                         "Which neurotransmitter is reduced in Parkinson's disease?")

    # -- 6. drain scope: an A-drain cannot patch solutions -------------------
    def test_recovery_scope_limits_fields(self):
        item = {"q_no": 2, "question_text": "stem?", "options": {"A": "a"},
                "correct_option": "C", "solution_text": "sol", "tables": []}
        out = qp._apply_recovery_scope(dict(item), qp._RECOVERY_SCOPE["S"], "OCR_S")
        self.assertIsNone(out["question_text"])
        self.assertIsNone(out["options"])
        self.assertEqual(out["solution_text"], "sol")
        self.assertEqual(out["_prov"], "OCR_S")
        out2 = qp._apply_recovery_scope(dict(item), qp._RECOVERY_SCOPE["A"], "DRAIN_A")
        self.assertEqual(out2["correct_option"], "C")
        self.assertIsNone(out2["solution_text"])


class ContinuationOwnershipTests(unittest.TestCase):
    """Run-8: unnumbered continuations crossing an overlap boundary must be
    assigned to the question whose heading is on the overlap page -- via the
    deterministic compute_carry S-pass fallback + carry-forward orphan
    recovery -- never left q_no=null when ownership is provable, and never
    guessed when it is not."""

    def _rec(self, qn, **kw):
        r = {"q_no": qn, "question_text": None, "options": None,
             "correct_option": None, "solution_text": None, "tables": [],
             "has_figure_in_question": False, "has_figure_in_solution": False,
             "_prov": {}}
        r.update(kw)
        return r

    def _s_orphan(self, frag, carry_qn, last_qn, page=18):
        return {"chapter_id": "PSY-016", "batch_start": page, "pdf_pages": [page, 21],
                "new_pages": [page, 19, 20, 21], "carry_q_no": carry_qn,
                "cut_part": "solution", "last_qn_in_batch": last_qn, "pass": "S",
                "item": {"q_no": None, "question_text": None, "solution_text": frag,
                         "options": None, "correct_option": None, "tables": [],
                         "has_figure_in_question": False,
                         "has_figure_in_solution": False}}

    # -- 1. Q2's heading at the bottom of the overlap page; all Q2 content on
    #      the next page -> continuation must be assigned to Q2 ------------
    def test_heading_on_overlap_page_assigns_continuation_to_owner(self):
        # window 1 ends with q2's truncated solution (its "Solution to
        # Question 2:" heading is at the bottom of the overlap page)
        trunc = "The correct answer is A because the defence mechanism here is:"
        items1 = [{"q_no": 2, "question_text": None, "solution_text": trunc,
                   "options": None, "correct_option": None, "tables": []}]
        recs = {2: self._rec(2, solution_text=trunc)}
        carry = qp.compute_carry({}, items1, recs, 17)   # no _batch_meta
        self.assertEqual(carry["last_open_question"], 2)
        self.assertEqual(carry["cut_part"], "solution")
        # window 2 still returns the continuation as q_no=null -> the orphan
        # carries q2 and rule 2 attaches it
        frag = "repression, because the impulse is pushed out of awareness into the unconscious mind."
        orphans = [self._s_orphan(frag, carry_qn=2, last_qn=3)]
        stats = {"orphans_recovered": 0, "foreign_fragments_blocked": 0,
                 "carry_merges": 0, "contaminated_stems_blocked": 0,
                 "chapter_id": "PSY-016"}
        recs[3] = self._rec(3, question_text="Stem three",
                            solution_text="complete solution three")
        remaining = qp.recover_orphans(orphans, recs, "PSY", 16, stats)
        self.assertEqual(remaining, [])
        self.assertIn("repression", recs[2]["solution_text"])
        self.assertEqual(recs[3]["solution_text"], "complete solution three")

    # -- 2. Q2 starts on overlap page, continues, then explicit Q3 heading ->
    #      initial continuation to Q2, subsequent content to Q3 ------------
    def test_continuation_then_explicit_next_heading_stays_separate(self):
        recs = {2: self._rec(2, question_text="Stem two",
                             solution_text="The answer is A because:"),
                3: self._rec(3, question_text="Stem three",
                             solution_text="complete solution three")}
        # the null fragment is q2's continuation; q3's numbered item exists
        # separately (already merged) and must NOT absorb the fragment
        frag = "the patient uses rationalisation to minimise the guilt feeling."
        orphans = [self._s_orphan(frag, carry_qn=2, last_qn=3)]
        stats = {"orphans_recovered": 0, "foreign_fragments_blocked": 0,
                 "carry_merges": 0, "contaminated_stems_blocked": 0,
                 "chapter_id": "PSY-016"}
        remaining = qp.recover_orphans(orphans, recs, "PSY", 16, stats)
        self.assertEqual(remaining, [])
        self.assertIn("rationalisation", recs[2]["solution_text"])
        self.assertEqual(recs[3]["solution_text"], "complete solution three")

    # -- 3. unnumbered text, no reliable owner -> stays unassigned, never
    #      guessed ---------------------------------------------------------
    def test_unowned_continuation_stays_unassigned(self):
        # window 1 ended with a COMPLETE solution -> no carry created
        items1 = [{"q_no": 2, "question_text": None,
                   "solution_text": "The answer is A. Repression is complete.",
                   "options": None, "correct_option": None, "tables": []}]
        recs = {2: self._rec(2, solution_text="The answer is A. Repression is complete."),
                3: self._rec(3, question_text="Stem three",
                             solution_text="complete solution three")}
        self.assertIsNone(qp.compute_carry({}, items1, recs, 17))
        # an unrelated unnumbered fragment with no carry must NOT be glued
        # onto q2 (complete solution) or guessed at all
        frag = "Some unnumbered text that has no provable owner on the overlap page."
        orphans = [self._s_orphan(frag, carry_qn=None, last_qn=2)]
        stats = {"orphans_recovered": 0, "foreign_fragments_blocked": 0,
                 "carry_merges": 0, "contaminated_stems_blocked": 0,
                 "chapter_id": "PSY-016"}
        remaining = qp.recover_orphans(orphans, recs, "PSY", 16, stats)
        self.assertEqual(len(remaining), 1)          # stays for review
        self.assertNotIn("unnumbered text", recs[2]["solution_text"])
        self.assertNotIn("unnumbered text", recs[3]["solution_text"])

    # -- 4. overlap content must not be duplicated into the final solution --
    def test_overlap_reextraction_does_not_duplicate_solution(self):
        # window 1 returns q2 partial; window 2 (with q2's page as overlap)
        # returns q2 complete -> the FULL solution replaces the partial one
        # (last-write-wins), never concatenated
        full = ("The answer is A. Repression is complete. The impulse is "
                "pushed out of awareness into the unconscious mind.")
        recs = {}
        qp.merge_question_records(recs, [
            {"q_no": 2, "question_text": None, "_prov": "S_PASS",
             "solution_text": "The answer is A. Repression is complete.",
             "options": None, "correct_option": None, "tables": []}],
            {"chapter_id": "PSY-016"})
        qp.merge_question_records(recs, [
            {"q_no": 2, "question_text": None, "_prov": "S_PASS",
             "solution_text": full,
             "options": None, "correct_option": None, "tables": []}],
            {"chapter_id": "PSY-016"})
        self.assertEqual(recs[2]["solution_text"], full)   # not doubled
        self.assertEqual(recs[2]["solution_text"].count("Repression is complete."), 1)

    # -- 5. S-pass continuation must never enter question_text -------------
    def test_s_pass_continuation_never_enters_question_text(self):
        # the S orphan carries a stray question_text (Gemini filled both) ->
        # blocked from the stem; the solution still merges under its owner
        frag_sol = ("the patient uses rationalisation to minimise guilt feelings.")
        stray_stem = ("Rationalisation is a defence mechanism that involves "
                      "providing a logical explanation for behaviour.")
        orphans = [{"chapter_id": "PSY-016", "batch_start": 18, "pdf_pages": [18],
                    "new_pages": [18], "carry_q_no": 2, "cut_part": "solution",
                    "last_qn_in_batch": 2, "pass": "S",
                    "item": {"q_no": None, "question_text": stray_stem,
                             "solution_text": frag_sol, "options": None,
                             "correct_option": None, "tables": [],
                             "has_figure_in_question": False,
                             "has_figure_in_solution": False}}]
        recs = {2: self._rec(2, question_text="Stem two",
                             solution_text="The answer is A because:")}
        stats = {"orphans_recovered": 0, "foreign_fragments_blocked": 0,
                 "carry_merges": 0, "contaminated_stems_blocked": 0,
                 "chapter_id": "PSY-016"}
        qp.recover_orphans(orphans, recs, "PSY", 16, stats)
        self.assertEqual(recs[2]["question_text"], "Stem two")      # untouched
        self.assertIn("rationalisation", recs[2]["solution_text"])  # merged

    # -- 6. valid existing content survives continuation recovery ----------
    def test_existing_content_never_overwritten_by_continuation(self):
        recs = {2: self._rec(2, question_text="The real stem stays intact",
                             solution_text="The answer is A because:")}
        # fill_only merge (recovery) must NEVER overwrite existing content,
        # even when the incoming S patch carries a (wrong) stem and a fuller
        # solution
        qp.merge_question_records(recs, [
            {"q_no": 2, "question_text": "WRONG stem from a stray S fragment",
             "solution_text": "The answer is A because: the full correct "
                              "explanation continues here with real content.",
             "options": None, "correct_option": None, "tables": [],
             "_prov": "S_RETRY"}],
            {"chapter_id": "PSY-016"}, fill_only=True)
        self.assertEqual(recs[2]["question_text"], "The real stem stays intact")
        self.assertEqual(recs[2]["solution_text"], "The answer is A because:")
        # the REAL continuation path (recover_orphans, truncated owner +
        # carry) appends the novel tail ONCE -- existing text preserved
        frag = "the full correct explanation continues here with real content."
        orphans = [self._s_orphan(frag, carry_qn=2, last_qn=2)]
        stats = {"orphans_recovered": 0, "foreign_fragments_blocked": 0,
                 "carry_merges": 0, "contaminated_stems_blocked": 0,
                 "chapter_id": "PSY-016"}
        qp.recover_orphans(orphans, recs, "PSY", 16, stats)
        self.assertEqual(recs[2]["question_text"], "The real stem stays intact")
        self.assertIn("full correct explanation", recs[2]["solution_text"])
        self.assertEqual(recs[2]["solution_text"].count("because:"), 1)


class GeometryFirstImageTests(unittest.TestCase):
    """Run-9: image ownership is DETERMINISTIC-FIRST -- every image belongs
    to the closest question/solution heading ABOVE it (real PDF y positions),
    or the carried active block for cross-page continuations. Gemini never
    overrides a deterministic assignment, and a single 'decorative' verdict
    never discards an image (it goes to unresolved_images.jsonl)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_assets = qp.ASSETS_DIR
        qp.ASSETS_DIR = self.tmp / "assets"
        self.subj_dir = qp.ASSETS_DIR / "questions" / "PSY"
        self.subj_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.ASSETS_DIR = self._old_assets

    def _rels(self, oids, page=1):
        rels = []
        for oid in oids:
            fname = f"PSY-p{page}-{oid}.webp"
            (self.subj_dir / fname).write_bytes(b"x" * 3000)
            rels.append(f"PSY/{fname}")
        return rels

    def _claim(self, pdf, oids, recs, page=1, active_block=None):
        owned = {}
        leftover = qp.claim_page_images(self._rels(oids, page), pdf, page,
                                        "PSY", 1, recs, owned,
                                        active_block=active_block)
        return leftover, owned

    # -- 1. image inside Q1 question block -> Q1 question image ------------
    def test_image_inside_question_block_maps_to_that_question(self):
        pdf = self.tmp / "q1_block.pdf"
        _write_test_pdf(pdf, [
            ("1. Which defence mechanism is being used?", 72, 700, 12),
            ("Option A: text", 72, 660, 10),
        ], [(6, "Im6", 300, 600)])
        leftover, owned = self._claim(pdf, [6], {1: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])

    # -- 2. image inside Q6 solution block -> Q6 solution image ------------
    def test_image_inside_solution_block_maps_to_that_solution(self):
        pdf = self.tmp / "q6_sol.pdf"
        _write_test_pdf(pdf, [
            ("Solution to Question 6:", 72, 700, 12),
            ("The answer is B because:", 72, 660, 10),
        ], [(6, "Im6", 300, 600)])
        leftover, owned = self._claim(pdf, [6], {6: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[6]["solution"], ["PSY/PSY-001-006_SOL_01.webp"])

    # -- 3. image after Q1 heading but before Q2 heading -> Q1 -------------
    def test_image_between_two_question_headings_belongs_to_first(self):
        pdf = self.tmp / "q1_q2.pdf"
        _write_test_pdf(pdf, [
            ("1. First question stem", 72, 700, 12),
            ("2. Second question stem", 72, 400, 12),
        ], [(6, "Im6", 300, 550)])
        leftover, owned = self._claim(pdf, [6], {1: {}, 2: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])
        self.assertNotIn(2, owned)

    # -- 4. multiple figures inside same block -> all stay with that owner --
    def test_multiple_figures_in_one_block_stay_with_owner(self):
        pdf = self.tmp / "multi_fig.pdf"
        _write_test_pdf(pdf, [
            ("1. First question stem", 72, 700, 12),
        ], [(6, "Im6", 300, 600), (7, "Im7", 300, 500)])
        leftover, owned = self._claim(pdf, [6, 7], {1: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(len(owned[1]["question"]), 2)

    # -- 5. multiple questions/images on same page -> each by position ------
    def test_multiple_questions_each_image_maps_by_position(self):
        pdf = self.tmp / "two_q_two_img.pdf"
        _write_test_pdf(pdf, [
            ("1. First question stem", 72, 700, 12),
            ("2. Second question stem", 72, 400, 12),
        ], [(6, "Im6", 300, 600), (7, "Im7", 300, 300)])
        leftover, owned = self._claim(pdf, [6, 7], {1: {}, 2: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])
        self.assertEqual(owned[2]["question"], ["PSY/PSY-001-002_Q_01.webp"])

    # -- 6. cross-page continuation image -> carried owner -----------------
    def test_cross_page_continuation_image_uses_carried_owner(self):
        # the new page's image has NO heading above it (block started on the
        # previous page) -> active_block (q6 solution) owns it
        pdf = self.tmp / "carry.pdf"
        _write_test_pdf(pdf, [
            ("text continues from previous page", 72, 600, 10),
        ], [(6, "Im6", 300, 700)])
        leftover, owned = self._claim(pdf, [6], {6: {}}, page=1,
                                      active_block=("solution", 6))
        self.assertEqual(leftover, [])
        self.assertEqual(owned[6]["solution"], ["PSY/PSY-001-006_SOL_01.webp"])

    def test_cross_page_without_carry_stays_unclaimed(self):
        pdf = self.tmp / "nocarry.pdf"
        _write_test_pdf(pdf, [
            ("text continues from previous page", 72, 600, 10),
        ], [(6, "Im6", 300, 700)])
        leftover, _ = self._claim(pdf, [6], {6: {}}, page=1, active_block=None)
        self.assertEqual(leftover, ["PSY/PSY-p1-6.webp"])

    # -- 7. genuine watermark -> excluded at extraction (deterministic) -----
    def test_watermark_object_excluded_at_extraction(self):
        pdf = self.tmp / "wm.pdf"
        # images must be > 5000 px or extract_real_images drops them as noise
        _write_test_pdf(pdf, [
            ("1. Question stem", 72, 700, 12),
        ], [(6, "Im6", 300, 600), (7, "Im7", 300, 300)], img_size=(90, 90))
        # watermark_id = obj 6 -> only obj 7 survives extraction
        saved = qp.extract_real_images(pdf, 1, 6, "PSY", self.subj_dir)
        self.assertEqual(saved, ["PSY/PSY-p1-7.webp"])

    # -- 8. ambiguous image -> unresolved, NOT decorative -------------------
    def test_ambiguous_image_recorded_as_unresolved_not_decorative(self):
        tmp = Path(tempfile.mkdtemp())
        old_data = qp.DATA_DIR
        qp.DATA_DIR = tmp / "data"
        try:
            qp._record_unresolved_image("PSY", "PSY-001", 4, "PSY/PSY-p4-7.webp",
                                        "model-declared decorative",
                                        model_verdict={"decorative": True})
            unresolved = qp.DATA_DIR / "unresolved_images.jsonl"
            decorative = qp.DATA_DIR / "decorative_images.jsonl"
            self.assertTrue(unresolved.exists())
            entry = json.loads(unresolved.read_text().splitlines()[0])
            self.assertEqual(entry["file"], "PSY/PSY-p4-7.webp")
            self.assertEqual(entry["model_verdict"], {"decorative": True})
            self.assertFalse(decorative.exists())   # NOT permanently discarded
        finally:
            qp.DATA_DIR = old_data

    # -- 9. Gemini disagreement must NOT override deterministic ownership ---
    def test_gemini_figure_map_cannot_override_geometry(self):
        # image is inside Q1's block (geometry claims it first); a Gemini
        # figure-map that would say Q2 runs on the LEFTOVERS only and cannot
        # move it
        pdf = self.tmp / "override.pdf"
        _write_test_pdf(pdf, [
            ("1. First question stem", 72, 700, 12),
        ], [(6, "Im6", 300, 600)])
        rels = self._rels([6])
        owned = {}
        # geometry-first: image claimed by Q1's block
        leftover = qp.claim_page_images(rels, pdf, 1, "PSY", 1, {1: {}, 2: {}},
                                        owned, active_block=None)
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])
        # the figure-map runs on the (empty) leftovers: even a contradictory
        # map cannot re-claim the already-owned image
        remaining = qp.claim_figure_map_images(
            [{"q_no": 2, "slot": "question"}], [(1, [])], "PSY", 1,
            {1: {}, 2: {}}, owned)
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])
        self.assertNotIn(2, owned)


class OptionImageOwnershipTests(unittest.TestCase):
    """Run-10: OPTION-LEVEL image ownership. An image geometrically inside an
    option label's block (vertical) or on a horizontal/2x2 option row is
    assigned to THAT option deterministically; everything else stays at
    question level. Never guessed, never dropped, Gemini never overrides."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_assets = qp.ASSETS_DIR
        qp.ASSETS_DIR = self.tmp / "assets"
        self.subj_dir = qp.ASSETS_DIR / "questions" / "PSY"
        self.subj_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.ASSETS_DIR = self._old_assets

    def _rels(self, oids, page=1):
        rels = []
        for oid in oids:
            fname = f"PSY-p{page}-{oid}.webp"
            (self.subj_dir / fname).write_bytes(b"x" * 3000)
            rels.append(f"PSY/{fname}")
        return rels

    def _claim(self, pdf, oids, recs, page=1, active_block=None):
        owned = {}
        leftover = qp.claim_page_images(self._rels(oids, page), pdf, page,
                                        "PSY", 1, recs, owned,
                                        active_block=active_block)
        return leftover, owned

    # -- 1. normal question image (no option labels) -> question-level -----
    def test_normal_question_image_stays_question_level(self):
        pdf = self.tmp / "q1_no_opts.pdf"
        _write_test_pdf(pdf, [
            ("1. Which diagnosis is shown?", 72, 700, 12),
        ], [(6, "Im6", 300, 620)])
        leftover, owned = self._claim(pdf, [6], {1: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])
        self.assertEqual(owned[1].get("option", {}), {})

    # -- 2. image under option A -> option A -------------------------------
    def test_image_under_option_a_maps_to_option_a(self):
        pdf = self.tmp / "opt_a.pdf"
        _write_test_pdf(pdf, [
            ("1. Identify the structure", 72, 700, 12),
            ("A. text", 72, 650, 10),
            ("B. text", 72, 550, 10),
        ], [(6, "Im6", 300, 620)])
        leftover, owned = self._claim(pdf, [6], {1: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["option"]["A"], ["PSY/PSY-001-001_OPT_A_01.webp"])
        self.assertEqual(owned[1]["question"], [])

    # -- 3. four vertical option images -> A/B/C/D -------------------------
    def test_four_vertical_option_images_map_correctly(self):
        pdf = self.tmp / "opt_abcd.pdf"
        _write_test_pdf(pdf, [
            ("1. Identify the structure", 72, 720, 12),
            ("A. text", 72, 650, 10), ("B. text", 72, 550, 10),
            ("C. text", 72, 450, 10), ("D. text", 72, 350, 10),
        ], [(6, "Im6", 300, 620), (7, "Im7", 300, 520),
            (8, "Im8", 300, 420), (9, "Im9", 300, 320)])
        leftover, owned = self._claim(pdf, [6, 7, 8, 9], {1: {}})
        self.assertEqual(leftover, [])
        opt = owned[1]["option"]
        self.assertEqual(opt["A"], ["PSY/PSY-001-001_OPT_A_01.webp"])
        self.assertEqual(opt["B"], ["PSY/PSY-001-001_OPT_B_01.webp"])
        self.assertEqual(opt["C"], ["PSY/PSY-001-001_OPT_C_01.webp"])
        self.assertEqual(opt["D"], ["PSY/PSY-001-001_OPT_D_01.webp"])
        self.assertEqual(owned[1]["question"], [])

    # -- 4. horizontal / 2x2 image options -> correct via x+y geometry -----
    def test_2x2_horizontal_option_images_map_by_xy(self):
        pdf = self.tmp / "opt_2x2.pdf"
        _write_test_pdf(pdf, [
            ("1. Identify the structure", 72, 720, 12),
            ("A. text", 72, 650, 10), ("B. text", 350, 650, 10),
            ("C. text", 72, 550, 10), ("D. text", 350, 550, 10),
        ], [(6, "Im6", 200, 620), (7, "Im7", 420, 620),
            (8, "Im8", 200, 520), (9, "Im9", 420, 520)])
        leftover, owned = self._claim(pdf, [6, 7, 8, 9], {1: {}})
        self.assertEqual(leftover, [])
        opt = owned[1]["option"]
        self.assertEqual(opt["A"], ["PSY/PSY-001-001_OPT_A_01.webp"])
        self.assertEqual(opt["B"], ["PSY/PSY-001-001_OPT_B_01.webp"])
        self.assertEqual(opt["C"], ["PSY/PSY-001-001_OPT_C_01.webp"])
        self.assertEqual(opt["D"], ["PSY/PSY-001-001_OPT_D_01.webp"])

    # -- 5. two images in the same option -> both preserved ----------------
    def test_two_images_in_same_option_both_preserved(self):
        pdf = self.tmp / "opt_two.pdf"
        _write_test_pdf(pdf, [
            ("1. Identify the structure", 72, 700, 12),
            ("A. text", 72, 650, 10), ("B. text", 72, 550, 10),
        ], [(6, "Im6", 300, 620), (7, "Im7", 300, 590)])
        leftover, owned = self._claim(pdf, [6, 7], {1: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(len(owned[1]["option"]["A"]), 2)

    # -- 6. image between stem and option A -> question-level ---------------
    def test_image_before_option_a_stays_question_level(self):
        pdf = self.tmp / "stem_img.pdf"
        _write_test_pdf(pdf, [
            ("1. Identify the structure", 72, 700, 12),
            ("A. text", 72, 650, 10),
        ], [(6, "Im6", 300, 670)])   # above A's label
        leftover, owned = self._claim(pdf, [6], {1: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])
        self.assertEqual(owned[1].get("option", {}), {})

    # -- 7. solution image with "Option A:" prose -> solution image ---------
    def test_solution_image_with_option_prose_stays_solution(self):
        pdf = self.tmp / "sol_opt.pdf"
        _write_test_pdf(pdf, [
            ("Solution to Question 5:", 72, 700, 12),
            ("Option A: the correct answer because", 72, 650, 10),
        ], [(6, "Im6", 300, 600)])
        leftover, owned = self._claim(pdf, [6], {5: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[5]["solution"], ["PSY/PSY-001-005_SOL_01.webp"])
        self.assertEqual(owned[5].get("option", {}), {})

    # -- 8. ambiguous option ownership (shared figure) -> question-level ----
    def test_shared_figure_ambiguous_stays_question_level(self):
        pdf = self.tmp / "shared.pdf"
        _write_test_pdf(pdf, [
            ("1. Identify the structure", 72, 700, 12),
            ("A. text", 72, 650, 10), ("B. text", 350, 650, 10),
        ], [(6, "Im6", 211, 620)])   # x = midpoint(72, 350) -> equidistant
        leftover, owned = self._claim(pdf, [6], {1: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])
        self.assertEqual(owned[1].get("option", {}), {})

    # -- 9. Gemini figure-map cannot override deterministic option ownership
    def test_gemini_cannot_override_option_ownership(self):
        pdf = self.tmp / "nooverride.pdf"
        _write_test_pdf(pdf, [
            ("1. Identify the structure", 72, 700, 12),
            ("A. text", 72, 650, 10), ("B. text", 72, 550, 10),
        ], [(6, "Im6", 300, 620)])
        rels = self._rels([6])
        owned = {}
        leftover = qp.claim_page_images(rels, pdf, 1, "PSY", 1, {1: {}}, owned)
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["option"]["A"], ["PSY/PSY-001-001_OPT_A_01.webp"])
        # a contradictory figure-map runs on leftovers only (here: none) and
        # cannot move the already-claimed image
        qp.claim_figure_map_images([{"q_no": 1, "slot": "question"}], [(1, [])],
                                   "PSY", 1, {1: {}}, owned)
        self.assertEqual(owned[1]["option"]["A"], ["PSY/PSY-001-001_OPT_A_01.webp"])
        self.assertEqual(owned[1]["question"], [])

    # -- 10. JSON round-trip preserves option images ------------------------
    def test_json_round_trip_preserves_option_images(self):
        pdf = self.tmp / "rt.pdf"
        _write_test_pdf(pdf, [
            ("1. Identify the structure", 72, 700, 12),
            ("A. text", 72, 650, 10), ("B. text", 72, 550, 10),
        ], [(6, "Im6", 300, 620), (7, "Im7", 300, 520)])
        owned = {}
        leftover = qp.claim_page_images(self._rels([6, 7]), pdf, 1, "PSY", 1,
                                        {1: {}}, owned)
        self.assertEqual(leftover, [])
        rec = {"q_no": 1, "question_text": "Identify the structure",
               "options": {"A": "text", "B": "text"}, "correct_option": "A",
               "solution_text": "sol", "tables": [], "_prov": {}}
        final = qp.build_final_question("PSY", "PSY-001", 1, 1, rec, owned[1])
        opt_by_id = {o["id"]: o for o in final["options"]}
        self.assertEqual([i["file"] for i in opt_by_id["A"]["images"]],
                         ["PSY/PSY-001-001_OPT_A_01.webp"])
        self.assertEqual([i["file"] for i in opt_by_id["B"]["images"]],
                         ["PSY/PSY-001-001_OPT_B_01.webp"])
        # round-trip through final_q_to_record preserves option ownership
        rec2, owned2 = qp.final_q_to_record(final)
        self.assertEqual(owned2["option"]["A"], ["PSY/PSY-001-001_OPT_A_01.webp"])
        self.assertEqual(owned2["option"]["B"], ["PSY/PSY-001-001_OPT_B_01.webp"])
        # schema backward compatible: options still have id/text, question
        # images still on question
        self.assertEqual([o["id"] for o in final["options"]], ["A", "B"])
        self.assertEqual(final["question"]["images"], [])

    # -- 11. block-level tests still green (option logic doesn't disturb) ---
    def test_solution_block_geometry_unchanged(self):
        pdf = self.tmp / "sol_only.pdf"
        _write_test_pdf(pdf, [
            ("Solution to Question 3:", 72, 700, 12),
        ], [(6, "Im6", 300, 600)])
        leftover, owned = self._claim(pdf, [6], {3: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[3]["solution"], ["PSY/PSY-001-003_SOL_01.webp"])

    # -- 12. horizontal row, stem figure ABOVE the option row -> question --
    def test_horizontal_stem_figure_above_row_stays_question_level(self):
        pdf = self.tmp / "hstem.pdf"
        _write_test_pdf(pdf, [
            ("1. Identify the structure", 72, 720, 12),
            ("A. text", 72, 650, 10), ("B. text", 350, 650, 10),
        ], [(6, "Im6", 200, 700)])   # above the option row
        leftover, owned = self._claim(pdf, [6], {1: {}})
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])
        self.assertEqual(owned[1].get("option", {}), {})

    # -- 13. horizontal 4-across tight row -> ambiguous -> question-level ---
    def test_tight_4across_ambiguous_stays_question_level(self):
        pdf = self.tmp / "tight4.pdf"
        _write_test_pdf(pdf, [
            ("1. Identify the structure", 72, 720, 12),
            ("A. text", 72, 650, 10), ("B. text", 220, 650, 10),
            ("C. text", 370, 650, 10), ("D. text", 520, 650, 10),
        ], [(6, "Im6", 150, 620)])   # ~midway between A and B
        leftover, owned = self._claim(pdf, [6], {1: {}})
        self.assertEqual(leftover, [])
        # geometry cannot safely prove which option -> question-level, never
        # guessed and never dropped
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])
        self.assertEqual(owned[1].get("option", {}), {})


class Run11ForensicHardeningTests(unittest.TestCase):
    """Run-11 root-cause hardening: stale-path image lifecycle, structured
    pass status, answer-key rescue targeting, export gate."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_assets = qp.ASSETS_DIR
        qp.ASSETS_DIR = self.tmp / "assets"
        self.subj_dir = qp.ASSETS_DIR / "questions" / "PSY"
        self.subj_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.ASSETS_DIR = self._old_assets

    # -- RC-1: stale-path image lifecycle --------------------------------
    def test_stale_path_returns_already_claimed_not_unmatched(self):
        # image was already renamed by a claim; the stale temp path no longer
        # exists -> attribute_orphan_image must say already_claimed (NOT
        # decorative, NOT a model call)
        called = []
        class FakeModel:
            def generate_content(self, *a, **k):
                called.append(True)
                raise AssertionError("must not call Gemini for a missing file")
        verdict = qp.attribute_orphan_image(FakeModel(), "PSY/PSY-p4-7.webp",
                                            {1: {}}, {"calls_today": 0})
        self.assertEqual(verdict, {"decorative": "already_claimed"})
        self.assertEqual(called, [])

    def test_figure_map_fully_claimed_page_returns_empty_leftover(self):
        # the caller feeds a page's leftovers to the figure-map; if the map
        # claims ALL of them, the page must NOT appear in fig_leftover (so the
        # caller clears its stale list instead of keeping temp names)
        for oid in (6, 7):
            (self.subj_dir / f"PSY-p1-{oid}.webp").write_bytes(b"x" * 3000)
        rels = ["PSY/PSY-p1-6.webp", "PSY/PSY-p1-7.webp"]
        fig_map = [{"q_no": 1, "slot": "question"}, {"q_no": 1, "slot": "question"}]
        owned = {}
        remaining = qp.claim_figure_map_images(fig_map, [(1, rels)], "PSY", 1,
                                               {1: {}}, owned)
        self.assertEqual(remaining, {})            # fully claimed
        self.assertEqual(len(owned[1]["question"]), 2)
        # the "page fully claimed -> leftover cleared" rule the caller applies
        leftover_by_page = {1: rels}               # stale temp names remain
        for page_no, _rels in [(1, rels)]:
            leftover_by_page[page_no] = remaining.get(page_no) or []   # the fix
        self.assertEqual(leftover_by_page[1], [])

    # -- RC-5: structured pass status -------------------------------------
    def test_pass_status_classification(self):
        self.assertEqual(qp._classify_pass_status("S", "Q", 0, False, True),
                         qp.PASS_STATUS_EXPECTED_EMPTY)
        self.assertEqual(qp._classify_pass_status("S", "S", 0, False, True),
                         qp.PASS_STATUS_PARTIAL)          # FAILED_ZERO suspect
        self.assertEqual(qp._classify_pass_status("S", "S", 9, False, True),
                         qp.PASS_STATUS_SUCCESS)
        self.assertEqual(qp._classify_pass_status("S", "S", 9, True, True),
                         qp.PASS_STATUS_RETRYABLE_FAILURE)
        self.assertEqual(qp._classify_pass_status("S", "S", 0, True, False),
                         qp.PASS_STATUS_UNRESOLVED)

    # -- RC-4: answer-key page targeting ----------------------------------
    def test_locate_missing_record_pages_finds_answer_key_pages(self):
        fake = {1: "1. Question one\n",
                2: "ANSWER KEY\n| Question No. | Correct Option |\n| 1 | B |\n| 2 | C |"}
        orig = qp.pdftotext_page
        qp.pdftotext_page = lambda pdf, page: fake.get(page, "")
        try:
            page_files = [Path(f"/tmp/x/page-{n:03d}.jpg") for n in (1, 2)]
            loc = qp.locate_missing_record_pages("pdf", page_files,
                                                 {1: ["answer"], 2: ["answer"]}, {})
        finally:
            qp.pdftotext_page = orig
        # q1's answer is on the KEY page (2), not just the question page (1)
        self.assertEqual(loc[1], [1, 2])
        self.assertEqual(loc[2], [2])

    def test_answer_rescue_prompt_is_answer_only(self):
        rec = {"q_no": 7, "question_text": "Which drug?", "correct_option": None}
        prompt = qp.answer_rescue_prompt(7, rec, {7: rec})
        self.assertIn('"correct_option"', prompt)
        self.assertNotIn("solution_text", prompt)
        self.assertIn("Question 7", prompt)

    def test_locate_answer_rows_without_probe_header(self):
        # answer rows in "13. B" / "13 - B" format on a page with NO "Answer
        # Key" header must still locate q13's answer page (run-12: ch15 q15's
        # rescue went to the question page because the key page lacked the
        # probe header)
        fake = {1: "1. Question one\n",
                2: "13. B\n14. C\n15. A\n"}
        orig = qp.pdftotext_page
        qp.pdftotext_page = lambda pdf, page: fake.get(page, "")
        try:
            page_files = [Path(f"/tmp/x/page-{n:03d}.jpg") for n in (1, 2)]
            loc = qp.locate_missing_record_pages("pdf", page_files,
                                                 {13: ["answer"], 15: ["answer"]}, {})
        finally:
            qp.pdftotext_page = orig
        self.assertIn(2, loc[13])
        self.assertIn(2, loc[15])

    # -- Export gate ------------------------------------------------------
    def test_export_gate_catches_missing_stems_and_answers(self):
        recs = {1: {"q_no": 1, "question_text": "stem",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": None, "solution_text": "sol"},
                2: {"q_no": 2, "question_text": None,
                    "options": {}, "correct_option": None,
                    "solution_text": None}}
        image_files = {1: {"question": ["PSY/PSY-001-001_Q_01.webp"],
                           "solution": [], "option": {}}}
        # missing asset ref -> broken_asset_ref
        vio = qp._export_gate_violations(recs, image_files, [], "PSY-001")
        kinds = {k for k, _q, _d in vio}
        self.assertIn("missing_answer", kinds)
        self.assertIn("missing_stem", kinds)
        self.assertIn("bad_options", kinds)
        self.assertIn("missing_solution", kinds)
        self.assertIn("broken_asset_ref", kinds)

    def test_export_gate_clean_when_everything_accounted(self):
        recs = {1: {"q_no": 1, "question_text": "stem",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": "B", "solution_text": "sol"}}
        vio = qp._export_gate_violations(recs, {}, [], "PSY-001")
        self.assertEqual(vio, [])

    def test_clean_ocr_preserves_prose_and_strips_page_noise(self):
        dirty = ("The answer is A.\n12\nPage 5 of 200\nwww.x.com\nmore prose\n")
        clean = qp._clean_ocr_text(dirty)
        self.assertIn("The answer is A.", clean)
        self.assertIn("more prose", clean)
        self.assertNotIn("Page 5 of 200", clean)
        self.assertNotIn("www.x.com", clean)


class Run12StemContaminationTests(unittest.TestCase):
    """Run-12: the contaminated-stem guard must not destroy GOOD question-
    shaped stems that its own solution restates, and the merge must never let
    a contaminated re-read replace a valid stem."""

    def _rec(self, qn, stem, sol, **kw):
        r = {"q_no": qn, "question_text": stem, "options": None,
             "correct_option": None, "solution_text": sol, "tables": [],
             "has_figure_in_question": False, "has_figure_in_solution": False,
             "_prov": {}}
        r.update(kw)
        return r

    # -- 1. a short QUESTION-SHAPED stem restated by its own solution is NOT
    #        contamination (the run-12 false-positive class) ---------------
    def test_question_shaped_stem_restated_by_solution_is_kept(self):
        stem = ("Which of the following drugs is most likely to improve the "
                "negative symptoms of schizophrenia?")
        sol = ("The correct answer is clozapine. The drug that improves the "
               "negative symptoms of schizophrenia is clozapine, which is "
               "reserved for treatment-resistant cases.")
        rec = self._rec(1, stem, sol)
        # high token overlap with the solution, but question-shaped + short ->
        # a GOOD stem, not contamination
        self.assertIsNone(qp._stem_reject_reason(stem, rec))

    def test_declarative_solution_prose_is_still_rejected(self):
        # long declarative explanation-as-stem (the real contamination class)
        sol = ("The correct answer is A. In Korsakoff syndrome the amnesia "
               "is characterised by anterograde and retrograde memory loss "
               "with confabulation, and the pathology lies in the mammillary "
               "bodies and the dorsomedial nucleus of the thalamus with "
               "severe vitamin B1 deficiency being the underlying cause.")
        rec = self._rec(5, sol, sol)
        self.assertIsNotNone(qp._stem_reject_reason(sol, rec))

    def test_explanation_opener_is_still_rejected(self):
        # "Option A:" opener -> contamination regardless of length
        stem = "Option A: CAGE questionnaire is used for addiction cases"
        rec = self._rec(1, stem, "Option A: CAGE questionnaire is used for "
                                "addiction and substance abuse cases")
        self.assertIsNotNone(qp._stem_reject_reason(stem, rec))

    # -- 2. merge: a contaminated incoming stem must never replace a valid one
    def test_merge_keeps_valid_stem_over_contaminated(self):
        good = ("Which of the following drugs is most likely to improve the "
                "negative symptoms of schizophrenia?")
        sol = ("The correct answer is clozapine. The drug that improves the "
               "negative symptoms of schizophrenia is clozapine, which is "
               "reserved for treatment-resistant cases.")
        recs = {1: self._rec(1, good, sol)}
        contaminated = ("The correct answer is clozapine. The drug that "
                        "improves the negative symptoms of schizophrenia is "
                        "clozapine, which is reserved for treatment-resistant "
                        "cases and should be tried before the others fail.")
        qp.merge_question_records(recs, [
            {"q_no": 1, "question_text": contaminated,
             "solution_text": sol, "options": None, "correct_option": None,
             "tables": [], "_prov": "Q_PASS"}], {"chapter_id": "PSY-001"})
        self.assertEqual(recs[1]["question_text"], good)   # valid stem survived

    def test_merge_does_not_fill_empty_stem_with_contaminated(self):
        sol = ("The correct answer is B. Body dysmorphic disorder involves "
               "a preoccupation with an imagined defect in appearance that "
               "causes clinically significant distress and impaired "
               "functioning with repetitive checking behaviours.")
        recs = {9: self._rec(9, None, sol)}
        qp.merge_question_records(recs, [
            {"q_no": 9, "question_text": sol, "solution_text": sol,
             "options": None, "correct_option": None, "tables": [],
             "_prov": "Q_PASS"}], {"chapter_id": "PSY-009"})
        self.assertIsNone(recs[9]["question_text"])       # stayed empty for retry

    def test_stem_conflict_resolver_never_picks_contaminated(self):
        # stem conflict: old clean vs new contaminated -> must keep old even
        # though the contaminated variant coheres with the solution perfectly
        good = ("Which of the following is the most common defence mechanism "
                "used by patients with conversion disorder?")
        sol = ("The correct answer is repression. The defence mechanism used "
               "by patients with conversion disorder is repression, in which "
               "the anxiety is pushed into the unconscious and converted into "
               "a physical symptom.")
        recs = {1: self._rec(1, good, sol)}
        contaminated = ("The defence mechanism used by patients with "
                        "conversion disorder is repression, in which the "
                        "anxiety is pushed into the unconscious and converted "
                        "into a physical symptom, and this is the most common "
                        "mechanism seen in this population.")
        qp.merge_question_records(recs, [
            {"q_no": 1, "question_text": contaminated,
             "solution_text": sol, "options": None, "correct_option": "B",
             "tables": [], "_prov": "Q_PASS"}], {"chapter_id": "PSY-001"})
        self.assertEqual(recs[1]["question_text"], good)

    # -- 3. retry strategy switch: after a contamination block, the prompt is
    #        stem-region-only for that q ------------------------------------
    def test_stem_only_prompt_after_contamination_block(self):
        rec = self._rec(3, None, "some solution text")
        prompt = qp.build_targeted_retry_prompt([(3, ["question"])], {3: rec},
                                                stem_only_qns={3})
        self.assertIn("QUESTION STEM", prompt)
        self.assertIn("option labels", prompt)
        self.assertIn("Do NOT include any option text", prompt)
        # the plain (non-stem-only) prompt asks for stem + options together
        plain = qp.build_targeted_retry_prompt([(3, ["question"])], {3: rec})
        self.assertIn("all four options", plain)
        self.assertNotIn("option labels", plain)

    # -- 4. Q-pass activation on solution windows ---------------------------
    def test_q_pass_skipped_on_pure_solution_window(self):
        self.assertFalse(qp._should_run_q_pass("S", False, None, False))
        self.assertFalse(qp._should_run_q_pass("S", False, None, True))
        # but runs when the window carries the boundary tail or a Q carry
        self.assertTrue(qp._should_run_q_pass("S", True, None, False))
        self.assertTrue(qp._should_run_q_pass("S", False, {"last_open_question": 5},
                                              False))
        # and always on Q windows / before the extraction boundary
        self.assertTrue(qp._should_run_q_pass("Q", False, None, False))
        self.assertFalse(qp._should_run_q_pass("Q", False, None, True))

    # -- 5. malformed-JSON recovery status ----------------------------------
    def test_recovered_after_error_is_retryable_not_unresolved(self):
        # a successful same-batch re-ask after malformed JSON is a RECOVERED
        # pass (run-12 ledger fix: ch13 was falsely flagged UNRESOLVED)
        self.assertEqual(qp._classify_pass_status("A", "A", 14, True, True),
                         qp.PASS_STATUS_RETRYABLE_FAILURE)
        self.assertNotEqual(qp._classify_pass_status("A", "A", 14, True, True),
                            qp.PASS_STATUS_UNRESOLVED)


class ZAIVerificationTests(unittest.TestCase):
    """Production-shaped regression tests from the independent review's
    scenarios. They LOCK IN the safe behavior (verified against the current
    code) so the adversarial layouts can never be broken by a naive
    'nearest-question' or overwrite change."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_assets = qp.ASSETS_DIR
        qp.ASSETS_DIR = self.tmp / "assets"
        self.subj_dir = qp.ASSETS_DIR / "questions" / "PSY"
        self.subj_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.ASSETS_DIR = self._old_assets

    # -- Z-Test 1: solution restates the stem ("Regarding [exact stem]...") --
    def test_solution_restating_stem_with_regarding_is_kept(self):
        stem = ("Regarding the management of a patient with opioid use "
                "disorder, which medication is most appropriate?")
        sol = ("Regarding the management of a patient with opioid use "
               "disorder, the most appropriate medication is buprenorphine, "
               "which reduces cravings and withdrawal symptoms and can be "
               "prescribed in office-based treatment.")
        rec = {"question_text": stem, "solution_text": sol}
        # high token overlap with the solution, but question-shaped + short
        # -> a GOOD stem, never stripped as contamination
        self.assertIsNone(qp._stem_reject_reason(stem, rec))

    # -- Z-Test 2a: q_no=None OPTIONS fragment is buffered, not dropped and
    #               never attached to the 'nearest' question ----------------
    def test_qno_none_options_buffered_not_attached_not_dropped(self):
        # Layout 1 (adversarial): a page boundary between a stem and its
        # options; another question started at the bottom of the previous
        # page. The q_no=None options must NOT be glued onto the wrong
        # question.
        recs = {45: {"q_no": 45, "question_text": "A patient presents with...",
                     "options": {"A": "old-a", "B": "old-b", "C": "old-c",
                                 "D": "old-d"},
                     "correct_option": None, "solution_text": None,
                     "tables": [], "has_figure_in_question": False,
                     "has_figure_in_solution": False, "_prov": {}}}
        frag = {"q_no": None, "question_text": None,
                "options": {"A": "new-a", "B": "new-b", "C": "new-c",
                            "D": "new-d"},
                "correct_option": None, "solution_text": None, "tables": [],
                "has_figure_in_question": False, "has_figure_in_solution": False,
                "_prov": "Q_PASS"}
        merged, skipped = qp.merge_question_records(recs, [frag],
                                                    {"chapter_id": "PSY-999"})
        # not attached (no ownership proof), not dropped (buffered as orphan)
        self.assertEqual(skipped, [frag])
        self.assertEqual(merged[45]["options"]["A"], "old-a")   # untouched

    # -- Z-Test 2b: q_no=None ANSWER-KEY TABLE is consumed as a key, not
    #               attached to a question ---------------------------------
    def test_qno_none_answer_key_table_consumed_not_attached(self):
        # Layout 2 (adversarial): a q_no=None table containing the answer
        # key must fill answers deterministically, never corrupt a question.
        frag = {"q_no": None, "question_text": None, "options": None,
                "correct_option": None, "solution_text": None,
                "tables": [{"type": "answer key",
                            "markdown": "| Question No. | Correct Option |\n"
                                        "|---|---|\n| 1 | A |\n| 2 | C |"}],
                "has_figure_in_question": False, "has_figure_in_solution": False,
                "_prov": "A_PASS"}
        recs = {1: {"q_no": 1, "question_text": "stem1", "options": None,
                    "correct_option": None, "solution_text": None, "tables": [],
                    "has_figure_in_question": False,
                    "has_figure_in_solution": False, "_prov": {}},
                2: {"q_no": 2, "question_text": "stem2", "options": None,
                    "correct_option": None, "solution_text": None, "tables": [],
                    "has_figure_in_question": False,
                    "has_figure_in_solution": False, "_prov": {}}}
        orphans = [{"chapter_id": "PSY-999", "batch_start": 0, "pdf_pages": [5],
                    "new_pages": [5], "carry_q_no": None, "cut_part": None,
                    "last_qn_in_batch": 2, "pass": "A", "item": frag}]
        stats = {"orphans_recovered": 0, "foreign_fragments_blocked": 0,
                 "carry_merges": 0, "contaminated_stems_blocked": 0,
                 "chapter_id": "PSY-999"}
        remaining = qp.recover_orphans(orphans, recs, "PSY", 999, stats)
        self.assertEqual(remaining, [])                    # key consumed
        self.assertEqual(recs[1]["correct_option"], "A")   # answers filled
        self.assertEqual(recs[2]["correct_option"], "C")
        self.assertEqual(recs[1]["question_text"], "stem1")  # stem untouched

    # -- Z-Test 3: DRAIN crop-ladder items are NOT overwritten by OCR -------
    def test_drain_ocr_merge_never_overwrites_crop_items(self):
        # ch17 p218 case: crop ladder produced items, OCR fallback produced a
        # different item. The merge is fill-only -> the earlier crop content
        # must survive.
        recs = {7: {"q_no": 7, "question_text": "stem7",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": None, "solution_text": "crop-ladder sol",
                    "tables": [], "has_figure_in_question": False,
                    "has_figure_in_solution": False, "_prov": {}}}
        # an OCR item that would REPLACE the solution if merge overwrote
        ocr_item = {"q_no": 7, "question_text": "stem7",
                    "solution_text": "OCR DIFFERENT solution", "tables": [],
                    "options": None, "correct_option": None,
                    "has_figure_in_question": False,
                    "has_figure_in_solution": False, "_prov": "OCR_S"}
        # fill_only merge (the drain path) must keep the crop-ladder content
        qp.merge_question_records(recs, [ocr_item],
                                  {"chapter_id": "PSY-017", "duplicates_merged": 0},
                                  fill_only=True)
        self.assertEqual(recs[7]["solution_text"], "crop-ladder sol")
        self.assertNotIn("OCR DIFFERENT", recs[7]["solution_text"])


class ValidatorContaminationTests(unittest.TestCase):
    """qbank_validator must flag cross-field contamination and OCR noise in
    the FINAL rows (run-7 hardening #3/#6)."""

    def _row(self, qtext, stext):
        return {"id": "PSY-001-001", "chapter_id": "PSY-001",
                "question": {"text": qtext, "images": []},
                "options": [{"id": "A", "text": "a", "images": []},
                            {"id": "B", "text": "b", "images": []},
                            {"id": "C", "text": "c", "images": []},
                            {"id": "D", "text": "d", "images": []}],
                "correct_options": ["B"],
                "solution": {"text": stext, "images": [], "tables": []}}

    def test_contaminated_question_flagged(self):
        import qbank_validator as qv
        sol = ("The correct answer is C. The patient has schizophrenia "
               "with predominantly negative symptoms which respond poorly "
               "to typical antipsychotics and require clozapine trial.")
        flags = qv.check_row(self._row(sol, sol), Path("/nonexistent"))
        kinds = {f["kind"] for f in flags}
        self.assertIn("contaminated_question", kinds)

    def test_explanation_opening_flagged(self):
        import qbank_validator as qv
        flags = qv.check_row(self._row("Option B: explanation text here",
                                       "real solution"), Path("/nonexistent"))
        kinds = {f["kind"] for f in flags}
        self.assertIn("contaminated_question", kinds)

    # run-17: validator must agree with pipeline._stem_reject_reason -- a
    # REAL short question-shaped stem that its solution restates (ch26 q1
    # class) is NOT contamination; a stem == solution verbatim (ch7 q23/25)
    # IS.
    def test_real_restated_stem_not_flagged(self):
        import qbank_validator as qv
        stem = ("The acts that a person says or does to disclose himself "
                "as having the status of boy or man is called _______?")
        sol = (stem + " Gender role is the public manifestation of gender "
               "identity. It includes behavior, dress, and mannerisms "
               "culturally associated with masculinity or femininity.")
        reason = qv._stem_contamination_reason(stem, sol)
        self.assertIsNone(reason)                       # no false positive

    def test_verbatim_stem_solution_flagged(self):
        import qbank_validator as qv
        text = ("The patient has developed acute muscular dystonia (spasm of "
                "muscles of tongue, face, neck, and back) which is an "
                "extrapyramidal side effect of haloperidol. This occurs "
                "within 1-5 days of drug intake.")
        reason = qv._stem_contamination_reason(text, text)
        self.assertIsNotNone(reason)                    # verbatim -> flagged
        self.assertIn("verbatim", reason)

    def test_ocr_noise_solution_flagged(self):
        import qbank_validator as qv
        flags = qv.check_row(self._row("Real stem question text here?",
                                       "The answer is A.\nPage 12 of 300\n"
                                       "www.qbank.example\nend"),
                             Path("/nonexistent"))
        kinds = {f["kind"] for f in flags}
        self.assertIn("ocr_noise_solution", kinds)

    def test_clean_row_not_flagged(self):
        import qbank_validator as qv
        flags = qv.check_row(self._row("Which drug is first line in ADHD?",
                                       "Methylphenidate is first line."),
                             Path("/nonexistent"))
        kinds = {f["kind"] for f in flags}
        self.assertNotIn("contaminated_question", kinds)
        self.assertNotIn("ocr_noise_solution", kinds)


class GeminiJsonParserTests(unittest.TestCase):
    def test_parses_one_array(self):
        self.assertEqual(parse_gemini_json_array('[{"q_no": 1}]'), [{"q_no": 1}])

    def test_recovers_adjacent_arrays(self):
        response = '[{"q_no": 1}]\n[{"q_no": 2}]'
        self.assertEqual(
            parse_gemini_json_array(response),
            [{"q_no": 1}, {"q_no": 2}],
        )

    def test_recovers_newline_delimited_objects(self):
        response = '{"q_no": 1}\n{"q_no": 2}'
        self.assertEqual(
            parse_gemini_json_array(response),
            [{"q_no": 1}, {"q_no": 2}],
        )

    def test_recovers_json_prefixed_by_model_prose(self):
        self.assertEqual(
            parse_gemini_json_array('Here is the requested data:\n```json\n[{"q_no": 1}]\n```'),
            [{"q_no": 1}],
        )

    def test_table_makes_dangling_lead_in_complete(self):
        self.assertFalse(looks_truncated_solution("Stages are:", has_tables=True))
        self.assertTrue(looks_truncated_solution("Stages are:", has_tables=False))

    def test_missing_terminal_period_is_not_a_truncation_signal(self):
        self.assertFalse(looks_truncated_solution("This is a complete source sentence"))
        self.assertFalse(looks_truncated_solution("Complete OCR sentence "))

    def test_table_dedupe_prefers_full_overlap_capture_in_any_order(self):
        partial = {"markdown": "| Stage | Goal |\n|---|---|\n| One | Trust |\n| Two | Autonomy |"}
        full = {"markdown": "| Stage | Goal |\n|---|---|\n| One | Trust |\n| Two | Autonomy |\n| Three | Initiative |"}
        self.assertEqual(_dedupe_tables([partial, full]), [full])
        self.assertEqual(_dedupe_tables([full, partial]), [full])

    def test_inline_table_is_moved_out_of_solution_prose(self):
        text = "Explanation:\n| Phase | Result |\n|---|---|\n| Oral | Fixation |\nEnd."
        clean, tables = _normalize_solution_payload(text, [], 7)
        self.assertEqual(clean, "Explanation:\nEnd.")
        self.assertEqual(len(tables), 1)

    def test_plain_bullet_solution_and_words_are_unchanged(self):
        text = "• First clinical finding\n• the classic example of this is Stockholm syndrome"
        clean, tables = _normalize_solution_payload(text, [], 24)
        self.assertEqual(clean, text)
        self.assertEqual(tables, [])
        self.assertIn(" is Stockholm", clean)

    def test_same_source_table_is_kept_for_different_questions(self):
        table = {"markdown": "| Phase | Result |\n|---|---|\n| Oral | Fixation |"}
        # Dedupe is deliberately record-local: q7 and q8 may both print it.
        self.assertEqual(_dedupe_tables([table]), [table])
        self.assertEqual(_dedupe_tables([table]), [table])

    def test_rejects_non_json_tail(self):
        with self.assertRaises(ValueError):
            parse_gemini_json_array('[{"q_no": 1}] explanation')


def _write_test_pdf_pages(path, texts, images, target_page, total_pages,
                          img_size=(20, 10)):
    """N-page PDF whose text+image content lives ONLY on `target_page`
    (1-based); the other pages are blank. Same object numbering as
    _write_test_pdf, so callers keep the usual PSY-p1-<oid>.webp naming and
    can exercise rendering of ANY real page number (e.g. page 4 of 4)."""
    objects = {}
    stream_parts = []
    for i, (t, x, y, sz) in enumerate(texts):
        stream_parts.append(f"BT 0 0 Td /F1 {sz} Tf {x} {y} Td ({t}) Tj ET".encode("latin-1"))
    w, h = img_size
    img_data = zlib.compress(b"\xff\x00\x00" * (w * h))
    xobjs = {}
    for obj_num, name, x, y in images:
        objects[obj_num] = (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                            f"/Length {len(img_data)} >>\nstream\n{img_data.decode('latin-1')}\nendstream")
        xobjs[name] = f"{obj_num} 0 R"
        stream_parts.append(f"q {w} 0 0 {h} {x} {y} cm /{name} Do Q".encode("latin-1"))
    real = b"\n".join(stream_parts).decode("latin-1")
    objects[5] = "<< /Length %d >>\nstream\n%s\nendstream" % (len(real) + 1, real)
    objects[15] = "<< /Length 0 >>\nstream\n\nendstream"     # blank page content
    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{100 + p} 0 R" for p in range(1, total_pages + 1))
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {total_pages} >>"
    xres = " ".join(f"/{n} {r}" for n, r in xobjs.items())
    for p in range(1, total_pages + 1):
        if p == target_page:
            objects[100 + p] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                                f"/Resources << /Font << /F1 4 0 R >> /XObject << {xres} >> >> "
                                f"/Contents 5 0 R >>")
        else:
            objects[100 + p] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                                f"/Resources << /Font << /F1 4 0 R >> >> /Contents 15 0 R >>")
    objects[4] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    out = b"%PDF-1.4\n"
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num].encode("latin-1") + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for num in sorted(objects):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    path.write_bytes(out)


def _write_test_pdf_form(path, texts, images, img_size=(20, 10)):
    """Like _write_test_pdf, but every image is drawn INSIDE a Form XObject
    (the real book wraps figures in Form/clip-mask wrappers -- the run-9 flat
    content-stream walk could not see those images, so they had no position
    and therefore no geometry owner). The image OBJECT id stays the same, so
    tests keep the usual PSY-p1-<oid>.webp naming."""
    objects = {}
    stream_parts = []
    for i, (t, x, y, sz) in enumerate(texts):
        stream_parts.append(f"BT 0 0 Td /F1 {sz} Tf {x} {y} Td ({t}) Tj ET".encode("latin-1"))
    w, h = img_size
    img_data = zlib.compress(b"\xff\x00\x00" * (w * h))
    xobjs = {}
    for obj_num, name, x, y in images:
        frm_num = obj_num + 100
        objects[obj_num] = (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                            f"/Length {len(img_data)} >>\nstream\n{img_data.decode('latin-1')}\nendstream")
        form_data = (f"q {w} 0 0 {h} {x} {y} cm /ImX Do Q").encode("latin-1")
        objects[frm_num] = (f"<< /Type /XObject /Subtype /Form /FormType 1 "
                            f"/BBox [0 0 612 792] /Resources << /XObject << /ImX {obj_num} 0 R >> >> "
                            f"/Length {len(form_data)} >>\nstream\n{form_data.decode('latin-1')}\nendstream")
        xobjs[name] = f"{frm_num} 0 R"
        stream_parts.append(f"q /{name} Do Q".encode("latin-1"))
    objects[5] = "<< /Length %d >>\nstream\n%s\nendstream" % (
        sum(len(p) + 1 for p in stream_parts), b"\n".join(stream_parts).decode("latin-1"))
    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
    xres = " ".join(f"/{n} {r}" for n, r in xobjs.items())
    objects[3] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                  f"/Resources << /Font << /F1 4 0 R >> /XObject << {xres} >> >> /Contents 5 0 R >>")
    objects[4] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    out = b"%PDF-1.4\n"
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num].encode("latin-1") + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for num in sorted(objects):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    path.write_bytes(out)


class UnifiedImageOwnershipTests(unittest.TestCase):
    """run-13: unified image-ownership architecture.

    Root-cause class under test: the run-9 geometry system reads question
    headings from the PDF TEXT LAYER. On the real book's QUESTION pages the
    body-font text layer is garbled/absent, so no heading is ever found,
    the one-to-one matcher also finds no printed q_no ("ambiguous printed
    owners"), and the figure falls to an ISOLATED-crop Gemini call with no
    page context -- which mislabeled a real Q1 figure "decorative" (page-4
    class). The synthetic tests that passed never modelled (a) an unreadable
    text layer, (b) Form-wrapped images, or (c) an isolated-crop 4th pass.

    New ladder: L1 text geometry -> L2 OCR-anchored geometry (rendered page,
    tesseract) -> L3 full-page vision (rendered page, bboxes highlighted,
    layout-only) -> L4 unresolved_images.jsonl + export-gate flag. Every
    level records provenance to data/image_ownership.jsonl."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_assets = qp.ASSETS_DIR
        self._old_data = qp.DATA_DIR
        self._old_state = qp.STATE_FILE
        self._old_pace = qp._pace_gemini_call
        qp.ASSETS_DIR = self.tmp / "assets"
        qp.DATA_DIR = self.tmp / "data"
        qp.STATE_FILE = self.tmp / "state.json"
        qp._pace_gemini_call = lambda: None
        self.subj_dir = qp.ASSETS_DIR / "questions" / "PSY"
        self.subj_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.ASSETS_DIR = self._old_assets
        qp.DATA_DIR = self._old_data
        qp.STATE_FILE = self._old_state
        qp._pace_gemini_call = self._old_pace
        qp._RENDER_CACHE.clear()

    def _rels(self, oids, page=1):
        rels = []
        for oid in oids:
            fname = f"PSY-p{page}-{oid}.webp"
            (self.subj_dir / fname).write_bytes(b"x" * 3000)
            rels.append(f"PSY/{fname}")
        return rels

    class _FakeVisionModel:
        """generate_content([prompt, PIL, ...]) -> {IMG-1: verdict} JSON."""
        def __init__(self, payload):
            self.payload = payload
            self.calls = 0
            self.last_parts = None

        def generate_content(self, parts, **kw):
            self.calls += 1
            self.last_parts = parts
            class _R:
                candidates = [object()]
                text = json.dumps(self.payload)
            return _R()

    # -- A. Form-XObject-wrapped images now get a position + owner ---------
    def test_form_wrapped_image_position_parsed_with_drawn_size(self):
        pdf = self.tmp / "form_img.pdf"
        _write_test_pdf_form(pdf, [
            ("1. First question stem", 72, 700, 12),
        ], [(7, "Im7", 300, 600)])
        pos = qp.image_positions_on_page(pdf, 1)
        self.assertIn(7, pos)
        y, x, didx, w, h = pos[7]
        self.assertEqual((x, y), (300.0, 600.0))   # bottom-left corner
        self.assertEqual((w, h), (20.0, 10.0))     # drawn size from cm scale
        # and the geometry claimer now attaches it to Q1's block
        owned = {}
        leftover = qp.claim_page_images(self._rels([7]), pdf, 1, "PSY", 1,
                                        {1: {}}, owned, active_block=None)
        self.assertEqual(leftover, [])
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])

    # -- B. page-4 class: unreadable text layer -> full-page vision claims --
    def test_garbled_text_layer_question_image_claimed_by_full_page_vision(self):
        pdf = self.tmp / "garbled_p4.pdf"
        # clean VISIBLE page 4 of 4: "1. ..." stem with the figure below it
        _write_test_pdf_pages(pdf, [
            ("1. Which of the following is true?", 72, 700, 12),
        ], [(7, "Im7", 300, 600)], target_page=4, total_pages=4)
        rel = "PSY/PSY-p4-7.webp"
        (self.subj_dir / rel.replace("PSY/", "")).write_bytes(b"x" * 3000)
        rels = [rel]
        # L1 text layer dead (garbled), one_to_one probe dead -> both return []
        orig_wl, orig_qns = qp._page_word_lines, qp.qns_printed_on_page
        qp._page_word_lines = lambda *a, **k: []
        qp.qns_printed_on_page = lambda *a, **k: []
        try:
            leftover, owned = {}, {}
            leftovers = qp.claim_page_images(rels, pdf, 4, "PSY", 1, {1: {}},
                                             owned, active_block=None)
            # L2 OCR unavailable too (no tesseract in sandbox / no anchors)
            self.assertEqual(leftovers, rels)     # the "unclaimed for now" state
            pos = qp.image_positions_on_page(pdf, 4)
            model = self._FakeVisionModel({
                "IMG-1": {"q_no": 1, "slot": "question",
                          "confidence": "high",
                          "evidence": "printed '1.' heading is directly above"}})
            claimed, still, verdicts = qp.full_page_vision_ownership(
                model, pdf, 4, leftovers, pos, "PSY", 1, {1: {}}, "PSY-001",
                {"calls_today": 0}, {}, dpi=72)
            self.assertEqual(model.calls, 1)
            self.assertEqual(still, [])
            self.assertEqual([c[1] for c in claimed], ["PSY-001-001"])
            # renamed to the locked slot name
            self.assertTrue((self.subj_dir / "PSY-001-001_Q_01.webp").exists())
            # the model received the RENDERED PAGE (with the highlight), not
            # an isolated crop -- a PIL image is part of the call
            self.assertTrue(any(isinstance(p, Image.Image)
                                for p in model.last_parts))
            # provenance ledger written
            ledger = (qp.DATA_DIR / "image_ownership.jsonl")
            self.assertTrue(ledger.exists())
            entry = json.loads(ledger.read_text().splitlines()[0])
            self.assertEqual(entry["method"], "full_page_vision")
            self.assertEqual(entry["owner"], "PSY-001-001")
            self.assertIn("1.", entry["evidence"])
        finally:
            qp._page_word_lines, qp.qns_printed_on_page = orig_wl, orig_qns

    # -- C. vision runs ONLY on leftovers; cannot override L1 --------------
    def test_vision_never_overrides_deterministic_geometry(self):
        pdf = self.tmp / "clean.pdf"
        _write_test_pdf(pdf, [
            ("1. First question stem", 72, 700, 12),
        ], [(7, "Im7", 300, 600)])
        owned = {}
        leftovers = qp.claim_page_images(self._rels([7]), pdf, 1, "PSY", 1,
                                         {1: {}}, owned, active_block=None)
        self.assertEqual(leftovers, [])     # L1 claimed it
        model = self._FakeVisionModel({})
        pos = qp.image_positions_on_page(pdf, 1)
        claimed, still, _ = qp.full_page_vision_ownership(
            model, pdf, 1, [], pos, "PSY", 1, {1: {}}, "PSY-001",
            {"calls_today": 0}, {}, dpi=72)
        self.assertEqual(model.calls, 0)    # nothing left to ask about
        self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])

    # -- D. L2 OCR-anchored geometry claims when the text layer is dead ----
    def test_ocr_geometry_claims_when_text_layer_garbled(self):
        pdf = self.tmp / "ocr_q.pdf"
        _write_test_pdf(pdf, [
            ("1. Stem text", 72, 700, 12),
        ], [(7, "Im7", 300, 600)])
        rels = self._rels([7])
        orig_wl, orig_ocr = qp._page_word_lines, qp.ocr_page_anchors
        qp._page_word_lines = lambda *a, **k: []        # text layer dead
        qp.ocr_page_anchors = lambda *a, **k: [("question", 1, 650.0)]
        try:
            owned = {}
            leftover = qp.claim_block_images_ocr(rels, pdf, 1, "PSY", 1,
                                                 {1: {}}, owned,
                                                 chapter_id="PSY-001")
            self.assertEqual(leftover, [])
            self.assertEqual(owned[1]["question"], ["PSY/PSY-001-001_Q_01.webp"])
        finally:
            qp._page_word_lines, qp.ocr_page_anchors = orig_wl, orig_ocr

    def test_ocr_anchor_line_matching_converts_coordinates(self):
        # unit-test the OCR anchor matcher WITHOUT the tesseract binary:
        # feed the word dict image_to_data would return and check the line
        # grouping, the regex, and the pixel->PDF-space y conversion
        png = Image.new("RGB", (1500, 2000), "white")
        words = {
            "text": ["1.", "Which", "drug?", "2.", "Next", "Question"],
            "left": [100, 140, 220, 100, 140, 260],
            "top":  [100, 100, 100, 400, 400, 400],
            "width":  [30, 70, 60, 30, 70, 80],
            "height": [20, 20, 20, 20, 20, 20],
            "conf":  [92, 95, 90, 93, 96, 91],
        }
        orig_which, orig_td = qp.shutil.which, qp.pytesseract.image_to_data
        qp.shutil.which = lambda *a, **k: "/usr/bin/tesseract"
        qp.pytesseract.image_to_data = lambda *a, **k: words
        try:
            scale = 150.0 / 72.0
            page_h_pt = 2000 / scale
            anchors = qp.ocr_page_anchors(png, scale, page_h_pt)
            qs = [(k, qn) for k, qn, _y in anchors if k == "question"]
            self.assertEqual(qs, [("question", 1), ("question", 2)])
            # first anchor: line center y_px = 110 -> y_pdf = (2000-110)/scale
            y1 = [y for k, qn, y in anchors if qn == 1][0]
            self.assertAlmostEqual(y1, (2000 - 110) / scale, places=1)
        finally:
            qp.shutil.which, qp.pytesseract.image_to_data = orig_which, orig_td

    # -- E. export gate: unresolved images block a CLEAN verdict -----------
    def test_unresolved_image_blocks_clean_gate(self):
        recs = {1: {"q_no": 1, "question_text": "stem",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": "B", "solution_text": "sol"}}
        unresolved = [{"page": 4, "file": "PSY/PSY-p4-7.webp",
                       "method": "all_levels_failed", "confidence": None,
                       "deterministic_junk": False}]
        vio = qp._export_gate_violations(recs, {}, [], "PSY-001", unresolved)
        kinds = {k for k, _q, _d in vio}
        self.assertIn("unresolved_image", kinds)   # the page-4 false-CLEAN fix

    def test_broken_crop_not_gate_violation(self):
        recs = {1: {"q_no": 1, "question_text": "stem",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": "B", "solution_text": "sol"}}
        unresolved = [{"page": 4, "file": "PSY/PSY-p4-414B.webp",
                       "method": "all_levels_failed", "confidence": None,
                       "deterministic_junk": True}]
        vio = qp._export_gate_violations(recs, {}, [], "PSY-001", unresolved)
        self.assertEqual(vio, [])   # junk crop is not a relevant figure


class RealPdfOptInFixtureTests(unittest.TestCase):
    """Opt-in regression fixture for the REAL production PDF. The synthetic
    suite cannot reproduce the page-4 class faithfully (its PDFs use clean
    Helvetica text), so when the real PSY.pdf is present at fixtures/PSY.pdf
    (drop it there, or run this on the Railway volume), these tests dump the
    REAL pypdf word coordinates + image bboxes for page 4 and assert the
    image that production failed to own has a parseable drawn position."""

    PDF = Path(__file__).resolve().parent / "fixtures" / "PSY.pdf"

    def test_page4_pypdf_dump_and_image_position(self):
        if not self.PDF.exists():
            self.skipTest("fixtures/PSY.pdf absent -- add the real PDF to "
                          "reproduce the exact page-4 layout")
        pdf = str(self.PDF)
        dump = {"page": 4,
                "words": qp._page_word_lines(pdf, 4),
                "image_positions": {str(k): v for k, v in
                                    qp.image_positions_on_page(pdf, 4).items()}}
        out = Path(__file__).resolve().parent / "debug"
        out.mkdir(parents=True, exist_ok=True)
        (out / "page4_pypdf_dump.json").write_text(
            json.dumps(dump, indent=2, default=str))
        self.assertIn(7, qp.image_positions_on_page(pdf, 4),
                      "PSY-p4-7 has no parsed drawn bbox -- the position "
                      "parser still cannot see this figure")


class Run13FinalAuditFixesTests(unittest.TestCase):
    """run-13 FINAL AUDIT fixes, driven by the fresh PAY production run
    (90 validator flags / 25 of 33 chapters):

    1. Q-pass ACTIVATION: the section planner labels a whole chapter "S" when
       the text layer shows solution headers on its first pages (often the
       previous chapter's solution tail), and _should_run_q_pass then skips
       the Q-pass on every S window -> the run's MASS stem/option loss
       (ch2/7/11/16/18/19/24/25/28/30/32: 9-27 records each needing
       [question]+[options] targeted retry; tails like ch7 q23-26, ch2
       q25-26, ch19 q11-12, ch24 q12-13 lost entirely). Fix: if OCR of the
       RENDERED pages still finds question-stem headings, Q-pass MUST run.
    2. EXPORT-GATE ORPHAN ACCOUNTING: ch11/17/33 printed
       "orphans: N unresolved" next to "[GATE] ... CLEAN". A meaningful
       unclaimed fragment must block CLEAN.
    3. STEM QUARANTINE: ch26 q1 / ch7 q24-26 shipped missing_stem because
       the sweep stripped the stem to None and retry could not refill it.
       Suspect stems are now kept + flagged; a passing candidate replaces
       them even in fill_only recovery.
    4. L3 vision never silently skips (p104 class) -- logs + method tags.
    5. Q-pass prompt forbids q_no:null for within-page continuations."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_assets, self._old_data, self._old_state = qp.ASSETS_DIR, qp.DATA_DIR, qp.STATE_FILE
        self._old_pace = qp._pace_gemini_call
        qp.ASSETS_DIR = self.tmp / "assets"
        qp.DATA_DIR = self.tmp / "data"
        qp.STATE_FILE = self.tmp / "state.json"
        qp._pace_gemini_call = lambda: None
        (qp.ASSETS_DIR / "questions" / "PSY").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.ASSETS_DIR, qp.DATA_DIR, qp.STATE_FILE = self._old_assets, self._old_data, self._old_state
        qp._pace_gemini_call = self._old_pace
        qp._RENDER_CACHE.clear()

    # -- 1. Q-pass activation via OCR question anchors ---------------------
    def test_s_window_with_question_anchors_forces_q_pass(self):
        pdf = str(self.tmp / "x.pdf")
        orig_render, orig_ocr = qp.render_page_png, qp.ocr_page_anchors
        qp.render_page_png = lambda *a, **k: (Image.new("RGB", (300, 400)), 2.0, 400.0)
        qp.ocr_page_anchors = lambda *a, **k: [("question", 1, 700.0), ("question", 2, 500.0)]
        try:
            self.assertTrue(qp.window_has_question_content(pdf, [204, 205], {1: {}, 2: {}}))
        finally:
            qp.render_page_png, qp.ocr_page_anchors = orig_render, orig_ocr

    def test_s_window_pure_solutions_has_no_question_content(self):
        pdf = str(self.tmp / "x.pdf")
        orig_render, orig_ocr = qp.render_page_png, qp.ocr_page_anchors
        qp.render_page_png = lambda *a, **k: (Image.new("RGB", (300, 400)), 2.0, 400.0)
        # solution headers only, and the lone numbered line sits BELOW them
        qp.ocr_page_anchors = lambda *a, **k: [
            ("solution", 1, 600.0), ("question", 1, 300.0)]
        try:
            self.assertFalse(qp.window_has_question_content(pdf, [209], {1: {}}))
        finally:
            qp.render_page_png, qp.ocr_page_anchors = orig_render, orig_ocr

    def test_should_run_q_pass_still_skips_when_no_ocr_signal(self):
        # the OLD behavior stays for windows with NO question-content signal:
        # a pure-solution S window must not get Q-passed (run-12 protection)
        pdf = str(self.tmp / "x.pdf")
        orig_render, orig_ocr = qp.render_page_png, qp.ocr_page_anchors
        qp.render_page_png = lambda *a, **k: (Image.new("RGB", (300, 400)), 2.0, 400.0)
        qp.ocr_page_anchors = lambda *a, **k: []   # OCR finds nothing
        try:
            self.assertFalse(qp.window_has_question_content(pdf, [220], {1: {}}))
        finally:
            qp.render_page_png, qp.ocr_page_anchors = orig_render, orig_ocr

    # -- 2. export-gate orphan accounting ----------------------------------
    def test_export_gate_flags_meaningful_unresolved_orphan(self):
        recs = {1: {"q_no": 1, "question_text": "stem",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": "B", "solution_text": "sol"}}
        orphan = {"chapter_id": "PAY-007", "pdf_pages": [105],
                  "item": {"q_no": None, "question_text": None,
                           "options": {"A": "Cannabis-induced psychosis",
                                       "B": "Amphetamine-induced psychosis",
                                       "C": "Alcohol-induced psychosis",
                                       "D": "Cocaine-induced psychosis"},
                           "correct_option": None, "solution_text": None}}
        vio = qp._export_gate_violations(recs, {}, [], "PAY-007", (),
                                         unresolved_orphans=[orphan])
        kinds = {k for k, _q, _d in vio}
        self.assertIn("orphan_unresolved", kinds)   # the ch7 q23-26 class

    def test_export_gate_ignores_empty_orphan_fragment(self):
        recs = {1: {"q_no": 1, "question_text": "stem",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": "B", "solution_text": "sol"}}
        orphan = {"chapter_id": "PAY-017", "pdf_pages": [218],
                  "item": {"q_no": None, "question_text": None, "options": None,
                           "correct_option": None, "solution_text": None,
                           "tables": None}}
        vio = qp._export_gate_violations(recs, {}, [], "PAY-017", (),
                                         unresolved_orphans=[orphan])
        self.assertEqual(vio, [])   # empty junk fragment is not data loss

    # -- 3. stem quarantine ------------------------------------------------
    def test_sweep_quarantines_suspect_stem_instead_of_deleting(self):
        rec = {"q_no": 1, "question_text": "The correct answer is B because the "
               "patient presents with psychosis and this is managed by "
               "antipsychotics as the first line of treatment.",
               "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
               "correct_option": "B",
               "solution_text": "The correct answer is B because the patient "
               "presents with psychosis and this is managed by antipsychotics "
               "as the first line of treatment.",
               "_prov": {"question_text": "Q_PASS"}}
        stats = {}
        qp.chapter_integrity_sweep({1: rec}, {}, "PSY", 1, stats)
        # data preserved (never stripped to None) + quarantine flag set
        self.assertTrue((rec.get("question_text") or "").strip())
        self.assertTrue(rec.get("_stem_suspect_reason"))
        # and the export gate reports suspect_stem (never silently clean)
        vio = qp._export_gate_violations({1: rec}, {}, [], "PSY-001")
        self.assertIn("suspect_stem", {k for k, _q, _d in vio})

    def test_fill_only_merge_replaces_quarantined_suspect_stem(self):
        existing = {1: {"q_no": 1, "question_text": "The correct answer is B "
                       "because the patient presents with psychosis ... suspect",
                        "options": None, "correct_option": "B",
                        "solution_text": "The correct answer is B ...",
                        "tables": [], "_prov": {},
                        "has_figure_in_question": False,
                        "has_figure_in_solution": False,
                        "_stem_suspect_reason": "opens with explanation-style language"}}
        item = {"q_no": 1, "question_text": "Which antipsychotic is first-line "
                "for acute psychosis with agitation?", "options": None,
                "correct_option": "B", "solution_text": None, "tables": [],
                "_prov": "Q_RETRY"}
        stats = {"duplicates_merged": 0, "conflicts": 0}
        qp.merge_question_records(existing, [item], stats, fill_only=True)
        self.assertIn("Which antipsychotic", existing[1]["question_text"])
        self.assertIsNone(existing[1].get("_stem_suspect_reason"))  # cleared

    # -- 4. vision never silently skips ------------------------------------
    def test_full_page_vision_logs_when_positions_missing(self):
        pdf = self.tmp / "no_pos.pdf"
        _write_test_pdf(pdf, [("1. Stem", 72, 700, 12)], [(7, "Im7", 300, 600)])
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        model = object()   # must never be called (no labels to ask about)
        with redirect_stdout(buf):
            claimed, still, _ = qp.full_page_vision_ownership(
                model, pdf, 1, ["PSY/PSY-p1-7.webp"], {}, "PSY", 1, {1: {}},
                "PSY-001", {"calls_today": 0}, {}, dpi=72)
        out = buf.getvalue()
        self.assertEqual(still, ["PSY/PSY-p1-7.webp"])
        self.assertIn("NO parsed image positions", out)   # loud, not silent

    # -- 5. prompt forbids within-page q_no:null continuations -------------
    def test_qpass_prompt_has_continuation_qno_clause(self):
        self.assertIn("CONTINUATION-WITHIN-PAGE", qp.SCHEMA_PROMPT_Q)
        self.assertIn("repeat that q_no", qp.SCHEMA_PROMPT_Q)

    # -- 6. OCR anchor fallback across psm modes ---------------------------
    def test_ocr_anchors_retries_psm_modes(self):
        calls = []
        def fake_image_to_data(png, config=None, output_type=None):
            calls.append(config)
            if config == "--psm 6":
                return {"text": ["", ""], "left": [0, 0], "top": [0, 0],
                        "width": [0, 0], "height": [0, 0], "conf": [0, 0]}
            return {"text": ["1.", "Stem"], "left": [100, 150], "top": [100, 100],
                    "width": [30, 70], "height": [20, 20], "conf": [92, 95]}
        orig_which, orig_td = qp.shutil.which, qp.pytesseract.image_to_data
        qp.shutil.which = lambda *a, **k: "/usr/bin/tesseract"
        qp.pytesseract.image_to_data = fake_image_to_data
        try:
            anchors = qp.ocr_page_anchors(Image.new("RGB", (1500, 2000)), 150 / 72, 2000)
            self.assertEqual(calls, ["--psm 6", "--psm 4"])
            self.assertEqual([(k, qn) for k, qn, _y in anchors if k == "question"],
                             [("question", 1)])
        finally:
            qp.shutil.which, qp.pytesseract.image_to_data = orig_which, orig_td


class Run14PersistentProblemFixesTests(unittest.TestCase):
    """run-14 (2nd pass) fixes driven by the OUTPUT DATA from the fresh PAY
    run (Drive folder Output):

    A. Phantom solution-only records (PAY-002-025/026): S-pass spilled the
       PREVIOUS chapter's "Solution to Question 25/26" into ch2's page range
       -> records with ONLY solution (no stem/options/answer), triple gate
       violations forever. Dropped + preserved to dropped_phantom_records.
    B. Stem == solution verbatim (PAY-007-023/025): question_text copied the
       solution text; the run-12 question-shape narrowing let it through
       because the prose contains "which"/"is". Reverse-containment now
       catches identical fields no matter the shape.
    C. Orphan verified duplicates (PAY-033 p356): a carry re-sent q8's option
       D as a q_no-less fragment -> consumed as a verified duplicate instead
       of lingering as orphan_unresolved.
    D. Q-pass coverage safety net: pages the Q-pass never ran on get Q-passed
       unless OCR proves pure-solution pages."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_assets, self._old_data, self._old_state = qp.ASSETS_DIR, qp.DATA_DIR, qp.STATE_FILE
        self._old_pace = qp._pace_gemini_call
        qp.ASSETS_DIR = self.tmp / "assets"
        qp.DATA_DIR = self.tmp / "data"
        qp.STATE_FILE = self.tmp / "state.json"
        qp._pace_gemini_call = lambda: None
        (qp.ASSETS_DIR / "questions" / "PAY").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.ASSETS_DIR, qp.DATA_DIR, qp.STATE_FILE = self._old_assets, self._old_data, self._old_state
        qp._pace_gemini_call = self._old_pace
        qp._RENDER_CACHE.clear()

    # -- A. phantom solution-only records ---------------------------------
    def test_phantom_solution_only_record_dropped_and_preserved(self):
        # PAY-002-025 class: record created by S-pass spill (solution only),
        # whose q_no + solution ALREADY shipped in ch1 (prior row)
        recs = {25: {"q_no": 25, "question_text": None, "options": None,
                     "correct_option": None,
                     "solution_text": "Hysteria develops due to fixation in "
                     "the phallic stage of development, not the genital stage.",
                     "tables": [], "_prov": {"solution_text": "S_PASS"}},
                1: {"q_no": 1, "question_text": "A patient is mute...",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": "B", "solution_text": "Stupor...",
                    "tables": [], "_prov": {"question_text": "Q_PASS"}}}
        prior = [{"q_no": 25, "solution_text":
                  "Hysteria develops due to fixation in the phallic stage of "
                  "development, not the genital stage. The phallic stage is "
                  "the third psychosexual stage."}]
        stats = {}
        dropped = qp.drop_phantom_solution_only_records(recs, "PAY-002", stats,
                                                        prior_rows=prior)
        self.assertEqual(dropped, [25])                 # phantom dropped
        self.assertEqual(stats.get("phantom_solution_dropped"), 1)
        self.assertIn(1, recs)                           # real record kept
        # full record preserved in the ledger (never silently lost)
        ledger = (qp.DATA_DIR / "dropped_phantom_records.jsonl")
        self.assertTrue(ledger.exists())
        entry = json.loads(ledger.read_text().splitlines()[0])
        self.assertEqual(entry["q_no"], 25)
        self.assertIn("Hysteria", entry["solution_text"])

    def test_solution_only_record_without_prior_duplicate_kept(self):
        # a real lost question whose solution was extracted (no prior
        # duplicate anywhere) must NEVER be dropped -- it stays flagged by the
        # gate instead of silently disappearing
        recs = {11: {"q_no": 11, "question_text": None, "options": None,
                     "correct_option": None,
                     "solution_text": "Impulse control disorders include "
                     "intermittent explosive disorder, kleptomania, and "
                     "pyromania.", "tables": [],
                     "_prov": {"solution_text": "S_PASS"}}}
        stats = {}
        dropped = qp.drop_phantom_solution_only_records(recs, "PAY-024", stats,
                                                        prior_rows=[])
        self.assertEqual(dropped, [])                    # kept (no dup proof)

    def test_q_pass_record_with_solution_only_not_dropped(self):
        # a Q-pass record (real question content prov) must never be dropped,
        # even if stem/options are empty at this instant
        recs = {7: {"q_no": 7, "question_text": None, "options": None,
                    "correct_option": None,
                    "solution_text": "some solution", "tables": [],
                    "_prov": {"solution_text": "Q_PASS"}}}
        stats = {}
        dropped = qp.drop_phantom_solution_only_records(recs, "PAY-007", stats)
        self.assertEqual(dropped, [])                    # Q-pass prov -> kept

    # -- B. stem == solution verbatim contamination ------------------------
    def test_stem_identical_to_solution_rejected_verbatim(self):
        # PAY-007-023 class: "The patient has developed acute muscular
        # dystonia ... within 1-5 days of drug intake." in BOTH fields; the
        # text contains "which" so the old question-shape narrowing let it
        # through. Reverse containment now catches it.
        stem = ("The patient has developed acute muscular dystonia (spasm of "
                "muscles of tongue, face, neck, and back) which is an "
                "extrapyramidal side effect of haloperidol. This occurs within "
                "1-5 days of drug intake.")
        reason = qp._stem_reject_reason(stem, {"solution_text": stem})
        self.assertIsNotNone(reason)
        self.assertIn("verbatim", reason)

    def test_real_stem_restated_in_solution_not_rejected(self):
        # PAY-026 q1 class: real short question whose solution restates it --
        # reverse containment fails (solution is much longer) -> NOT rejected
        stem = "The acts that a person says or does to disclose himself as having the status of boy or man is called _______?"
        solution = ("The acts that a person says or does to disclose himself "
                    "as having the status of boy or man is called gender role. "
                    "Gender role is the public manifestation of gender identity. "
                    "It includes behavior, dress, and mannerisms that are "
                    "culturally associated with masculinity or femininity.")
        reason = qp._stem_reject_reason(stem, {"solution_text": solution})
        self.assertIsNone(reason)                         # real stem survives

    # -- C. orphan verified duplicate --------------------------------------
    def test_orphan_option_tail_duplicate_consumed(self):
        # PAY-033 p356 class: q8's option D re-sent as a q_no-less fragment
        recs = {8: {"q_no": 8, "question_text": "Which statement is true?",
                    "options": {"A": "a", "B": "b", "C": "c",
                                "D": "(d) the person has recently shown, or is "
                                     "showing, an inability to care for "
                                     "themselves to a degree that places them "
                                     "at risk of harm."},
                    "correct_option": "D", "solution_text": "sol", "tables": []}}
        orphan = {"chapter_id": "PAY-033", "batch_start": 356, "pdf_pages": [356],
                  "new_pages": [356], "carry_q_no": None, "cut_part": None,
                  "last_qn_in_batch": None, "pass": "Q",
                  "item": {"q_no": None,
                           "question_text": "(d) the person has recently shown "
                                            "or is showing, an inability to "
                                            "care for themselves to a degree "
                                            "that places them at risk of harm.",
                           "options": None, "correct_option": None,
                           "solution_text": None, "tables": [],
                           "has_figure_in_question": False,
                           "has_figure_in_solution": False, "_prov": "Q_PASS"}}
        stats = {}
        remaining = qp.recover_orphans([orphan], recs, "PAY", 33, stats)
        self.assertEqual(remaining, [])                    # consumed
        self.assertEqual(stats.get("orphans_recovered"), 1)
        self.assertEqual(recs[8]["question_text"], "Which statement is true?")  # unchanged

    # -- D. Q-coverage: page-level question content ------------------------
    def test_page_has_question_content_uses_ocr(self):
        pdf = str(self.tmp / "x.pdf")
        orig_render, orig_ocr = qp.render_page_png, qp.ocr_page_anchors
        qp.render_page_png = lambda *a, **k: (Image.new("RGB", (300, 400)), 2.0, 400.0)
        qp.ocr_page_anchors = lambda *a, **k: [("question", 1, 700.0)]
        try:
            self.assertTrue(qp.page_has_question_content(pdf, 24, {1: {}}))
        finally:
            qp.render_page_png, qp.ocr_page_anchors = orig_render, orig_ocr

    def test_page_has_question_content_false_on_pure_solutions(self):
        pdf = str(self.tmp / "x.pdf")
        orig_render, orig_ocr = qp.render_page_png, qp.ocr_page_anchors
        qp.render_page_png = lambda *a, **k: (Image.new("RGB", (300, 400)), 2.0, 400.0)
        qp.ocr_page_anchors = lambda *a, **k: [("solution", 5, 700.0),
                                               ("question", 5, 300.0)]
        try:
            self.assertFalse(qp.page_has_question_content(pdf, 22, {5: {}}))
        finally:
            qp.render_page_png, qp.ocr_page_anchors = orig_render, orig_ocr


class Run16MemoryAndResumeTests(unittest.TestCase):
    """run-16 SIGKILL/OOM investigation (independent of extraction quality):

    PROVEN cause: _RENDER_CACHE was an unbounded module-global dict holding a
    full-page PIL RGB render per page (~6.3 MB at 150 dpi). Q-activation OCR,
    L2 OCR geometry, L3 full-page vision and its context pages rendered ~150
    pages by chapter 11 of a 33-chapter book (~950 MB) -> the Railway
    container's kernel OOM-killed the gunicorn worker (SIGKILL pid 3).

    Fixes under test:
    - render cache is a BOUNDED LRU (_RENDER_CACHE_MAX) + clear_render_cache()
    - PyMuPDF documents closed; pdftoppm temp dirs removed
    - full-page vision draws on a COPY (never mutates the cached render)
    - questions.jsonl is rewritten ATOMICALLY per chapter (rewrite_questions_file)
      so a worker death at any point leaves the file = last committed chapter;
      a resume can never duplicate records (old append-mode deduped only at
      the end of a full book -> mid-book deaths left duplicates)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_assets, self._old_data, self._old_state = qp.ASSETS_DIR, qp.DATA_DIR, qp.STATE_FILE
        self._old_max, self._old_pace = qp._RENDER_CACHE_MAX, qp._pace_gemini_call
        qp.ASSETS_DIR = self.tmp / "assets"
        qp.DATA_DIR = self.tmp / "data"
        qp.STATE_FILE = self.tmp / "state.json"
        qp._pace_gemini_call = lambda: None
        (qp.ASSETS_DIR / "questions" / "PSY").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        qp.ASSETS_DIR, qp.DATA_DIR, qp.STATE_FILE = self._old_assets, self._old_data, self._old_state
        qp._RENDER_CACHE_MAX, qp._pace_gemini_call = self._old_max, self._old_pace
        qp.clear_render_cache()

    def _mkpdf(self, n_pages, target=1):
        pdf = self.tmp / f"pages{n_pages}_{target}.pdf"
        _write_test_pdf_pages(pdf, [("1. Stem", 72, 700, 12)],
                              [(7, "Im7", 300, 600)], target_page=target,
                              total_pages=n_pages)
        return pdf

    # -- 1. render cache is bounded LRU (the OOM fix) ----------------------
    def test_render_cache_bounded_lru_evicts_oldest(self):
        qp._RENDER_CACHE_MAX = 3
        pdf = self._mkpdf(10)
        for p in range(1, 11):
            qp.render_page_png(pdf, p, dpi=36)     # tiny renders, real path
        self.assertLessEqual(len(qp._RENDER_CACHE), 3)     # bounded, always
        self.assertNotIn((str(pdf), 1, 36), qp._RENDER_CACHE)   # oldest evicted
        self.assertIn((str(pdf), 10, 36), qp._RENDER_CACHE)     # newest kept

    def test_render_cache_stress_200_pages_stays_bounded(self):
        qp._RENDER_CACHE_MAX = 10
        pdf = self._mkpdf(200)
        for p in range(1, 201):
            qp.render_page_png(pdf, p, dpi=36)
        self.assertLessEqual(len(qp._RENDER_CACHE), 10)
        # the FULL-BOOK scenario that OOM-killed the worker stays tiny
        total_bytes = 0
        for key, (img, _s, _h) in qp._RENDER_CACHE.items():
            if img is not None:
                total_bytes += img.width * img.height * 3
        self.assertLess(total_bytes, 10 * 1024 * 1024)   # < 10 MB at dpi 36

    def test_clear_render_cache_empties(self):
        qp._RENDER_CACHE_MAX = 100
        pdf = self._mkpdf(5)
        for p in range(1, 6):
            qp.render_page_png(pdf, p, dpi=36)
        self.assertEqual(qp.render_cache_size(), 5)
        qp.clear_render_cache()
        self.assertEqual(qp.render_cache_size(), 0)

    # -- 2. full-page vision must not mutate the cached render -------------
    def test_full_page_vision_draws_on_copy_not_cache(self):
        pdf = self._mkpdf(4, target=4)      # stem+image ON page 4
        rel = "PSY/PSY-p4-7.webp"
        (qp.ASSETS_DIR / "questions" / "PSY" / "PSY-p4-7.webp").write_bytes(b"x" * 3000)
        pos = qp.image_positions_on_page(pdf, 4)
        render = qp.render_page_png(pdf, 4, dpi=36)
        cached_img = render[0]
        before = cached_img.tobytes()
        class _M:
            def __init__(self):
                self.parts = None
            def generate_content(self, parts, **kw):
                self.parts = parts
                class _R:
                    candidates = [object()]
                    text = json.dumps({"IMG-1": {"q_no": 1, "slot": "question",
                                                 "confidence": "high",
                                                 "evidence": "below Q1"}})
                return _R()
        model = _M()
        owned = {}
        claimed, still, _ = qp.full_page_vision_ownership(
            model, pdf, 4, [rel], pos, "PSY", 1, {1: {}}, "PSY-001",
            {"calls_today": 0}, owned, dpi=36)
        self.assertEqual(still, [])
        self.assertEqual([c[1] for c in claimed], ["PSY-001-001"])
        # the model received a highlighted COPY, not the cached object
        self.assertIsNot(model.parts[1], cached_img)
        # the cached render is byte-identical to before the vision call
        self.assertEqual(cached_img.tobytes(), before)

    # -- 3. atomic per-chapter questions rewrite (resume safety) -----------
    def test_rewrite_removes_partial_chapter_and_dedups(self):
        path = qp.DATA_DIR / "questions.jsonl"
        ch1_rows = [{"id": "PAY-001-001", "chapter_id": "PAY-001",
                     "question": {"text": "s1"}},
                    {"id": "PAY-001-002", "chapter_id": "PAY-001",
                     "question": {"text": "s2"}}]
        partial = [{"id": "PAY-002-025", "chapter_id": "PAY-002",
                    "question": {"text": "partial-1"}},
                   {"id": "PAY-002-026", "chapter_id": "PAY-002",
                    "question": {"text": "partial-2"}}]
        qp.rewrite_questions_file(path, "PAY-001", ch1_rows)
        qp.rewrite_questions_file(path, "PAY-002", partial)   # SIGKILL after
        # resume re-runs ch2 fully -> rewrite replaces the partial rows
        new_ch2 = [{"id": "PAY-002-025", "chapter_id": "PAY-002",
                    "question": {"text": "full-25"}},
                   {"id": "PAY-002-026", "chapter_id": "PAY-002",
                    "question": {"text": "full-26"}}]
        qp.rewrite_questions_file(path, "PAY-002", new_ch2)
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        ids = [r["id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))            # no duplicates
        self.assertIn("PAY-001-001", ids)                    # ch1 kept
        self.assertEqual(rows[ids.index("PAY-002-025")]["question"]["text"],
                         "full-25")                          # latest wins
        self.assertNotIn("partial-1", [r["question"]["text"] for r in rows])
        self.assertFalse(path.with_suffix(".jsonl.tmp").exists())  # tmp cleaned

    def test_rewrite_twice_same_chapter_no_duplication(self):
        path = qp.DATA_DIR / "questions.jsonl"
        ch2 = [{"id": "PAY-002-001", "chapter_id": "PAY-002",
                "question": {"text": "a"}}]
        qp.rewrite_questions_file(path, "PAY-002", ch2)
        qp.rewrite_questions_file(path, "PAY-002", ch2)      # resume re-run
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)                       # exactly once

    # -- 4. render temp cleanup (pdftoppm dirs + fitz close) ---------------
    def test_render_temp_dirs_are_removed(self):
        qp._RENDER_CACHE_MAX = 100
        pdf = self._mkpdf(6)
        before = set(Path("/tmp").glob("qbank_render_*")) if Path("/tmp").exists() else set()
        for p in range(1, 7):
            qp.render_page_png(pdf, p, dpi=36)
        after = set(Path("/tmp").glob("qbank_render_*")) if Path("/tmp").exists() else set()
        self.assertEqual(after, before)   # no leaked temp render dirs


class Run17CodeAuditTests(unittest.TestCase):
    """run-17 full-code audit: real bug fixes + dead/duplicate code removal.

    BUG FIX under test: routed_pages (recitation-sensitive solution pages
    whose solutions were already OCR-recovered via PREFLIGHT_OCR) used to be
    excluded from EVERY Gemini pass -- a mixed page with QUESTIONS + sensitive
    solutions silently lost its questions (no drain either: the page never
    "failed"). Now only the S-pass skips them; Q and A still run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        qp._RENDER_CACHE.clear()

    def test_routed_pages_skipped_only_by_s_pass(self):
        batch = [Path("page-100.jpg"), Path("page-101.jpg")]
        routed = {Path("page-100.jpg")}
        # S-pass excludes the routed page (solutions already OCR-recovered)
        self.assertEqual(qp._batch_after_routing("S", batch, routed),
                         [Path("page-101.jpg")])
        # Q-pass and A-pass keep EVERY page -- questions on a mixed
        # sensitive page must not be silently lost
        self.assertEqual(qp._batch_after_routing("Q", batch, routed), batch)
        self.assertEqual(qp._batch_after_routing("A", batch, routed), batch)

    def test_no_routed_pages_batch_unchanged(self):
        batch = [Path("page-1.jpg")]
        self.assertEqual(qp._batch_after_routing("S", batch, set()), batch)
        self.assertEqual(qp._batch_after_routing("Q", batch, set()), batch)


class ZipResetIsolationTests(unittest.TestCase):
    """run-18: after a reset the export ZIP must contain ONLY the new run's
    output. The old reset archived only data/assets/state.json and left the
    per-subject bundle (subjects/<SUB>/chapters/*.jsonl + questions.jsonl)
    behind, so a previous book's JSONs leaked into the next zip via
    make_zip()'s rglob. Fix: reset archives EVERYTHING except _archive, and
    make_zip() skips _archive / healer backups / the zip itself."""

    def setUp(self):
        import app as appmod
        self.app = appmod
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        qp.clear_render_cache()

    # -- reset archives EVERYTHING except the archive itself ---------------
    def test_entries_to_archive_includes_subjects(self):
        root = self.tmp / "out"
        (root / "data").mkdir(parents=True)
        (root / "assets").mkdir()
        (root / "subjects" / "PAY" / "chapters").mkdir(parents=True)
        (root / "subjects" / "PAY" / "questions.jsonl").write_text("{}")
        (root / "state.json").write_text("{}")
        (root / "_archive" / "old").mkdir(parents=True)   # previous archive
        entries = self.app._entries_to_archive(root)
        names = {n for n, _p in entries}
        self.assertEqual(names, {"data", "assets", "subjects", "state.json"})
        self.assertNotIn("_archive", names)   # the archive itself survives

    def test_reset_moves_subjects_into_archive(self):
        root = self.tmp / "out"
        (root / "subjects" / "PAY" / "chapters").mkdir(parents=True)
        (root / "subjects" / "PAY" / "questions.jsonl").write_text("old-json")
        (root / "data").mkdir()
        stamp = "20260806-000000"
        arch = root / "_archive" / stamp
        for name, src in self.app._entries_to_archive(root):
            arch.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(arch / name))
        # fresh output root has NO stale subjects/ -- next run starts clean
        self.assertFalse((root / "subjects").exists())
        self.assertFalse((root / "data").exists())
        self.assertTrue((arch / "subjects" / "PAY" / "questions.jsonl").exists())

    # -- make_zip skip rules ----------------------------------------------
    def test_zip_skip_excludes_archive_backups_and_self(self):
        self.assertTrue(self.app._zip_skip(Path("_archive/x/data/questions.jsonl")))
        self.assertTrue(self.app._zip_skip(Path("data/row.bak-2026-08-06.jsonl")))
        self.assertTrue(self.app._zip_skip(Path("output_results.zip")))
        # current-run content must NOT be skipped
        self.assertFalse(self.app._zip_skip(Path("data/questions.jsonl")))
        self.assertFalse(self.app._zip_skip(Path("subjects/PAY/questions.jsonl")))
        self.assertFalse(self.app._zip_skip(Path("state.json")))

    def test_make_zip_excludes_stale_subjects_after_reset(self):
        # simulate: old subjects/ left behind (pre-fix bug) -> _zip_skip
        # must keep it out ONLY when it lives under _archive; a REAL stale
        # subjects/ at the top level is removed by reset (test above). The
        # zip writer uses _zip_skip, so verify end-to-end by building a zip
        # from a root that has archive + a fresh subjects/.
        root = self.tmp / "out"
        (root / "_archive" / "old" / "subjects" / "PAY").mkdir(parents=True)
        (root / "_archive" / "old" / "subjects" / "PAY" / "questions.jsonl").write_text("stale")
        (root / "subjects" / "PAY").mkdir(parents=True)
        (root / "subjects" / "PAY" / "questions.jsonl").write_text("fresh")
        (root / "data").mkdir()
        (root / "data" / "questions.jsonl").write_text("fresh-master")
        zip_path = self.tmp / "out.zip"
        import zipfile
        with zipfile.ZipFile(zip_path, "w") as zf:
            for f in root.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(root)
                if self.app._zip_skip(rel):
                    continue
                zf.write(f, rel.as_posix())
        names = zipfile.ZipFile(zip_path).namelist()
        self.assertFalse(any("_archive" in n for n in names))     # no stale
        self.assertTrue(any(n.endswith("subjects/PAY/questions.jsonl") for n in names))
        self.assertTrue(any(n.endswith("data/questions.jsonl") for n in names))


class Run21ImageAttributionTests(unittest.TestCase):
    """run-21 (2026-08-11): the three real-run defects found on a live ANA
    chapter-9 pass over the MARROW ED8 book (46 MB, garbled text layer).

    1. OCR anchor line-grouping: the y-average grouper split
       "Solution to Question 10:" and the tail fragment "10:" matched the
       QUESTION regex -- every solution-page figure was attributed to the
       question slot of the wrong q_no.
    2. Flat over-attribution cap refused a 4th figure the model had DECLARED
       for q12 (ANA-009-012), dumping a real figure into unmatched_images.
    3. A continuation page with zero printed headings (p158) bailed out of
       the deterministic OCR pass entirely instead of using the open block.
    """

    def setUp(self):
        qp._declared_image_allowance.clear()

    tearDown = setUp

    # --- 1. anchor classification ------------------------------------
    def _data(self, lines):
        """Fake pytesseract image_to_data dict; each line is its own line_num."""
        d = {k: [] for k in ("text", "left", "top", "height", "conf",
                             "block_num", "par_num", "line_num")}
        for li, (y, text) in enumerate(lines):
            for wi, w in enumerate(text.split()):
                d["text"].append(w)
                d["left"].append(50 + wi * 40)
                d["top"].append(y)
                d["height"].append(12)
                d["conf"].append(90)
                d["block_num"].append(1)
                d["par_num"].append(1)
                d["line_num"].append(li)
        return d

    def test_solution_header_is_not_read_as_question_anchor(self):
        data = self._data([(100, "Solution to Question 10:"),
                           (400, "Solution to Question 11:")])
        anchors = qp._ocr_anchors_from_data(data, scale=2.0, img_h=1000)
        kinds = {(k, qn) for k, qn, _y in anchors}
        self.assertIn(("solution", 10), kinds)
        self.assertIn(("solution", 11), kinds)
        # the regression: NEITHER may appear as a question anchor
        self.assertNotIn(("question", 10), kinds)
        self.assertNotIn(("question", 11), kinds)

    def test_real_question_stem_still_anchors_as_question(self):
        data = self._data([(100, "Question 14:"), (400, "Question 15:")])
        anchors = qp._ocr_anchors_from_data(data, scale=2.0, img_h=1000)
        kinds = {(k, qn) for k, qn, _y in anchors}
        self.assertEqual(kinds, {("question", 14), ("question", 15)})

    def test_grouper_falls_back_when_line_ids_absent(self):
        data = self._data([(100, "Question 7:")])
        for k in ("block_num", "par_num", "line_num"):
            data.pop(k)
        anchors = qp._ocr_anchors_from_data(data, scale=2.0, img_h=1000)
        self.assertEqual([(k, qn) for k, qn, _y in anchors], [("question", 7)])

    # --- 2. dynamic image cap ----------------------------------------
    def test_positional_claim_keeps_the_strict_cap(self):
        self.assertEqual(qp.image_cap_for("ANA", 9, 12, "question"),
                         qp.MAX_QUESTION_IMAGES)
        self.assertEqual(qp.image_cap_for("ANA", 9, 12, "solution"),
                         qp.MAX_SOLUTION_IMAGES)

    def test_model_declared_owner_raises_cap_and_is_remembered(self):
        key = qp._allowance_key("ANA", 9, 12, "question")
        qp._declared_image_allowance[key] = 4
        # the sweep and any later claim must both see the RAISED cap, else
        # the trim silently undoes the fix a few hundred lines later
        self.assertEqual(qp.image_cap_for("ANA", 9, 12, "question"), 4)
        self.assertEqual(qp.image_cap_for("ANA", 9, 13, "question"),
                         qp.MAX_QUESTION_IMAGES)   # scoped per question

    def test_ceilings_are_finite_and_above_the_soft_caps(self):
        self.assertGreater(qp.IMAGE_CAP_CEILING_QUESTION, qp.MAX_QUESTION_IMAGES)
        self.assertGreater(qp.IMAGE_CAP_CEILING_SOLUTION, qp.MAX_SOLUTION_IMAGES)
        self.assertLessEqual(qp.IMAGE_CAP_CEILING_QUESTION, 8)

    def test_model_claim_sources_cover_all_three_model_passes(self):
        self.assertEqual(qp.MODEL_CLAIM_SOURCES,
                         frozenset({"figure_map", "full_page_vision",
                                    "isolated_crop_vision"}))

    # --- 3. isolated-crop page context -------------------------------
    def test_isolated_crop_prompt_has_page_context_placeholders(self):
        for token in ("{PAGE_NO}", "{ANCHORS}", "{PAGE_QNOS}"):
            self.assertIn(token, qp.IMAGE_ATTRIBUTION_PAGE_CONTEXT)
        # the crop pass must now ask for a confidence it can log
        self.assertIn("confidence", qp.IMAGE_ATTRIBUTION_PROMPT)

    def test_attribute_orphan_image_accepts_page_context_args(self):
        import inspect
        sig = inspect.signature(qp.attribute_orphan_image)
        self.assertIn("pdf_path", sig.parameters)
        self.assertIn("file_page", sig.parameters)
        # optional -- existing callers must keep working unchanged
        self.assertIsNone(sig.parameters["pdf_path"].default)
        self.assertIsNone(sig.parameters["file_page"].default)

    def test_verdict_confidence_reads_the_l4_verdict(self):
        v = {"q_no": 12, "slot": "question", "confidence": "high"}
        self.assertEqual(qp._verdict_confidence({"x.webp": v}, "x.webp"), "high")
        self.assertIsNone(qp._verdict_confidence({"x.webp": {}}, "x.webp"))


class Run21bCarrySeedAndCarryCapTests(unittest.TestCase):
    """run-21 §2.1(b)/(c) -- the two image defects that survived the first
    round of run-21 fixes and were caught by diffing the post-fix ANA ch.9
    run against its baseline.

    (b) CARRY SEEDING. Batch windows overlap, and a window skips pages the
        PREVIOUS window already imaged (`pages_imaged`). So the ch.9 window
        "160-163" actually iterates images from p161 onward, while the block
        owning p161's top figure was opened by the heading printed on p160 --
        a page this window never visits. The per-page carry advance therefore
        could never see it, the pass-level carry was empty ("carry-in: -"),
        and a deterministically-ownable figure fell through to full-page
        vision, which answered "q16, high confidence" when the printed page
        says the figure belongs to q15.

    (c) CARRY CLAIMS vs THE FLAT CAP. q11's solution genuinely spans three
        figures (p158 x2 + p159). The flat solution cap of 2 refused the
        third *even though* it came from the deterministic cross-page carry,
        and the refused file went to vision, which misfiled it under q12.
        A carry claim is page evidence, not the "stacking a neighbour's
        figures" pattern the flat cap exists to stop, so it may reach the
        model ceiling.
    """

    def setUp(self):
        qp._declared_image_allowance.clear()

    def tearDown(self):
        qp._declared_image_allowance.clear()

    # --- (b) carry seeding -------------------------------------------
    def test_carry_seed_lookback_is_a_small_positive_window(self):
        self.assertIsInstance(qp.CARRY_SEED_LOOKBACK_PAGES, int)
        self.assertGreaterEqual(qp.CARRY_SEED_LOOKBACK_PAGES, 1)
        self.assertLessEqual(qp.CARRY_SEED_LOOKBACK_PAGES, 5,
                             "a long lookback would resurrect stale blocks")

    def test_carry_seeding_block_present_in_window_loop(self):
        src = Path(qp.__file__).read_text()
        self.assertIn("carry seeded from page", src,
                      "the window loop must seed active_block from the page "
                      "before its first imaged page")
        self.assertIn("CARRY_SEED_LOOKBACK_PAGES", src)

    def test_carry_seed_only_runs_when_no_pass_carry_exists(self):
        src = Path(qp.__file__).read_text()
        # run-22: anchor on the actual print STATEMENT, not on any occurrence
        # of the phrase. This asserts on raw source text, so a COMMENT that
        # quotes the log line (the D3 chapter-clamp rationale did) silently
        # moves the anchor and the test fails on unchanged code.
        i = src.index('f"  [IMG] carry seeded from page')
        window = src[max(0, i - 1800):i]
        # run-25: the page list this guard reads was renamed window_rows ->
        # window_seq (window_seq also carries the IMAGE-LESS pages, so the
        # carry advances across them -- Defect B). The invariant under test is
        # unchanged: seed ONLY when there is no pass-level carry.
        self.assertRegex(window,
                         r"if active_block is None and window_(rows|seq):",
                         "seeding must never override a real pass-level carry")

    # --- (c) carry claims and the cap --------------------------------
    def test_carry_claim_source_is_distinct_from_plain_positional(self):
        self.assertNotEqual(qp.CARRY_CLAIM_SOURCE, "positional")
        self.assertNotIn(qp.CARRY_CLAIM_SOURCE, qp.MODEL_CLAIM_SOURCES,
                         "a carry is deterministic, not a model claim")

    def test_carry_claim_may_exceed_the_flat_solution_cap(self):
        """q11 (p158 x2 + p159) -- the exact ch.9 refusal."""
        entry = {"question": [], "solution": ["a.webp", "b.webp"]}
        self.assertEqual(len(entry["solution"]), qp.MAX_SOLUTION_IMAGES)
        cap = qp.image_cap_for("ANA", 9, 11, "solution")
        raised = max(cap, min(len(entry["solution"]) + 1,
                              qp.IMAGE_CAP_CEILING_SOLUTION))
        self.assertGreater(raised, qp.MAX_SOLUTION_IMAGES)
        self.assertLessEqual(raised, qp.IMAGE_CAP_CEILING_SOLUTION)

    def test_plain_positional_claim_still_stops_at_the_flat_cap(self):
        cap = qp.image_cap_for("ANA", 9, 11, "solution")
        self.assertEqual(cap, qp.MAX_SOLUTION_IMAGES,
                         "same-page positional stacking must stay capped")

    def test_both_positional_claim_sites_tag_the_carry_source(self):
        src = Path(qp.__file__).read_text()
        self.assertEqual(
            src.count("if owner is active_block"), 2,
            "claim_page_images and claim_block_images_ocr must BOTH tag a "
            "carry-derived owner so the cap can tell it from same-page "
            "positional stacking")

    def test_carry_source_reaches_rename_guard(self):
        src = Path(qp.__file__).read_text()
        i = src.index("def _rename_for_slot")
        body = src[i:i + 6000]
        self.assertEqual(
            body.count("claim_source == CARRY_CLAIM_SOURCE"), 2,
            "both the question-side and solution-side guards must honour it")


class Run22GarbledHeaderAndOrphanInferenceTests(unittest.TestCase):
    """run-22 (D1/D2/D3): defects found by the ch. 38 held-out validation run.

    Ch. 38 ("Brachial Plexus and Nerves", pp. 666-702) was NOT one of the
    chapters the round-2 image fixes were tuned on. Its image attribution came
    out 38/38 correct with zero vision calls, but it exposed three unrelated
    defects, all reproduced verbatim below from the real page text.
    """

    # --- D1: OCR mangles the colon after "Question N" -------------------
    # PDF p670 prints "Question 13:"; tesseract read "Question 13, ~\".
    # The old terminator class [.:\-\u2013)] had no comma, so NO branch matched
    # and q13's four options + answer were never bound to the record, while
    # its stem and solution arrived from other passes -- a record that looks
    # complete but silently is not.
    GARBLED = "Question 13, ~\\"

    def test_garbled_comma_terminator_still_reads_as_a_stem_heading(self):
        m = qp.QSTEM_HEADING_RE.match(self.GARBLED)
        self.assertIsNotNone(m, "OCR ',' for ':' must not hide a question heading")
        self.assertEqual(int(m.group(1)), 13)

    def test_other_common_ocr_terminator_misreads(self):
        for line, qn in (("Question 13,", 13), ("Question 13;", 13),
                         ("Question 7\u00b7", 7), ("13]", 13),
                         ("Question 12:", 12), ("13.", 13), ("13)", 13),
                         ("13 -", 13)):
            with self.subTest(line=line):
                m = qp.QSTEM_HEADING_RE.match(line)
                self.assertIsNotNone(m, f"{line!r} must match")
                self.assertEqual(int(m.group(1)), qn)

    def test_widened_class_does_not_swallow_prose(self):
        # The class got LOOSER, so guard the false-positive side explicitly:
        # a stray match costs one redundant Q-pass, but matching prose would
        # invent headings on every page.
        for line in ("in 13 patients with ulnar palsy", "the 13 nerves listed",
                     "A 48-year-old construction worker presented", "",
                     "Froment test negative"):
            with self.subTest(line=line):
                self.assertIsNone(qp.QSTEM_HEADING_RE.match(line))

    def test_per_qno_helper_agrees_with_the_generic_one(self):
        self.assertTrue(qp.qstem_heading_re_for(13).search(self.GARBLED))
        self.assertFalse(qp.qstem_heading_re_for(14).search(self.GARBLED))

    def test_single_source_of_truth_for_the_heading_pattern(self):
        # The pattern used to be copy-pasted into six places and they drifted.
        src = Path(qp.__file__).read_text()
        self.assertNotIn(r'(\d{1,3})\s*[.:\-\u2013)]', src,
                         "no call site may re-inline the old terminator class; "
                         "use QSTEM_HEADING_RE / qstem_heading_re_for()")

    # --- D3: carry seed must not cross a chapter boundary ----------------
    def test_carry_seed_lookback_is_clamped_to_chapter_start(self):
        src = Path(qp.__file__).read_text()
        i = src.index('f"  [IMG] carry seeded from page')
        window = src[max(0, i - 1800):i]
        self.assertIn('_floor = max(1, int(ch.get("file_start") or 1))', window,
                      "the walk-back must start from the chapter's own first "
                      "page; ch. 38 (starts p666) seeded off ch. 37's p665")
        self.assertIn("if _prev < _floor:", window,
                      "the loop must break at the chapter floor, not at page 1")

    # --- D2: infer a q_no-less fragment's owner from page position -------
    def _orphan(self, frag, pages, last_qn, tables=None):
        return {"chapter_id": "ANA-038", "batch_start": pages[0],
                "pdf_pages": list(pages), "new_pages": list(pages),
                "carry_q_no": None, "cut_part": None,
                "last_qn_in_batch": last_qn, "pass": "S",
                "item": {"q_no": None, "question_text": None,
                         "solution_text": frag, "options": None,
                         "correct_option": None, "tables": tables or [],
                         "has_figure_in_question": False,
                         "has_figure_in_solution": False}}

    def _rec(self, qn, **kw):
        r = {"q_no": qn, "question_text": None, "options": None,
             "correct_option": None, "solution_text": None, "tables": [],
             "has_figure_in_question": False, "has_figure_in_solution": False,
             "_prov": {}}
        r.update(kw)
        return r

    def _stats(self):
        return {"orphans_recovered": 0, "foreign_fragments_blocked": 0,
                "carry_merges": 0, "contaminated_stems_blocked": 0,
                "chapter_id": "ANA-038"}

    def test_position_inference_beats_naive_last_qn(self):
        """THE ch. 38 trap: last_qn_in_batch was 13, the true owner was 11.

        Orphan text "Distal to pisiform it gives 2 terminal branches" is the
        continuation of q11's solution, which ends "...lateral to pisiform
        called Guyon's ulnar tunnel". A last_qn-based guess would have glued
        q11's anatomy onto q13. Page position says the last heading printed
        at/before p687 opened q11, so q11 wins.
        """
        frag = ("\u2022 Distal to pisiform it gives 2 terminal branches. One "
                "superficial terminal and another deep terminal")
        # VERBATIM from the ch. 38 run: q11's solution stops at "...ulnar
        # tunnel" with NO dangling colon and no full stop. That is why this
        # needed _solution_block_is_open() rather than
        # looks_truncated_solution(), which only fires on an explicit
        # dangling lead-in and would have left this fragment orphaned.
        recs = {11: self._rec(11, solution_text=(
                    "The course of the ulnar nerve: it passes superficial to "
                    "flexor retinaculum but under a fascial band lateral to "
                    "pisiform called Guyon's ulnar tunnel")),
                13: self._rec(13, solution_text=(
                    "The given clinical scenario is suggestive of an injury to "
                    "the ulnar nerve before its division. Given below is the "
                    "Froment test used to test adductor pollicis."))}
        orphans = [self._orphan(frag, [686, 687], last_qn=13)]
        stats = self._stats()
        with mock.patch.object(qp, "last_block_on_page",
                               side_effect=lambda _p, page, **kw:
                                   ("solution", 11) if page == 687 else None):
            remaining = qp.recover_orphans(orphans, recs, "ANA", 38, stats,
                                           pdf_path="/tmp/book.pdf")
        self.assertIn("pisiform it gives 2 terminal branches",
                      recs[11]["solution_text"] or "")
        self.assertNotIn("2 terminal branches", recs[13]["solution_text"] or "",
                         "must NOT be glued onto last_qn_in_batch's record")
        self.assertEqual(remaining, [])

    def test_open_block_predicate_is_not_the_retry_predicate(self):
        """_solution_block_is_open must be LOOSER than
        looks_truncated_solution: the ch. 38 q11 tail has no dangling marker,
        so reusing the retry predicate here would drop the continuation. But
        it must still refuse a solution that ends in a full stop."""
        q11_tail = ("The course of the ulnar nerve: it passes superficial to "
                    "flexor retinaculum but under a fascial band lateral to "
                    "pisiform called Guyon's ulnar tunnel")
        self.assertFalse(qp.looks_truncated_solution(q11_tail),
                         "guard the assumption this test rests on")
        self.assertTrue(qp._solution_block_is_open(q11_tail))
        self.assertTrue(qp._solution_block_is_open(""))
        self.assertTrue(qp._solution_block_is_open("The steps are:"))
        for closed in ("Ends with a full stop.", "Ends with a question mark?",
                       "Froment test used to test adductor pollicis."):
            with self.subTest(closed=closed):
                self.assertFalse(qp._solution_block_is_open(closed))

    def test_inference_never_overwrites_a_complete_solution(self):
        """Owner found, but its solution already ends cleanly -> attach
        nothing, and say so instead of the misleading 'could not determine
        owner'. Reuses the same completeness guard as rule 3."""
        recs = {11: self._rec(11, solution_text=(
            "The ulnar nerve supplies the palmar interossei and the course "
            "is fully described here, ending in a complete sentence."))}
        orphans = [self._orphan("Some extra aside material.", [686, 687],
                                last_qn=11)]
        stats = self._stats()
        with mock.patch.object(qp, "last_block_on_page",
                               return_value=("solution", 11)):
            remaining = qp.recover_orphans(orphans, recs, "ANA", 38, stats,
                                           pdf_path="/tmp/book.pdf")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].get("inferred_owner"), 11)
        self.assertEqual(remaining[0].get("blocked_reason"),
                         "owner solution already complete")
        self.assertNotIn("aside material", recs[11]["solution_text"])

    def test_position_inference_never_places_a_bare_table(self):
        """REGRESSION (found by re-running ch. 38 with the D2 fix in place):

        the first cut of this fix also accepted table-only fragments, and
        promptly glued the chapter-wide ANSWER KEY ("| 5 | c | 6 | a |
        7 | b | ...", the answers to q5-q16) plus a sympathetic vs
        parasympathetic comparison table onto q7's solution -- q7 was merely
        the last heading printed before them. A table spanning many q_nos
        belongs to the chapter, not to the preceding block. Position
        inference is for PROSE continuations only.
        """
        key_table = {"type": "table",
                     "markdown": "| 5 | c |\n|---|---|\n| 6 | a |\n| 7 | b |"}
        recs = {7: self._rec(7, solution_text="Q7 solution ends cleanly.")}
        orphans = [self._orphan(None, [677, 681], last_qn=None,
                                tables=[key_table])]
        stats = self._stats()
        with mock.patch.object(qp, "last_block_on_page",
                               return_value=("solution", 7)):
            remaining = qp.recover_orphans(orphans, recs, "ANA", 38, stats,
                                           pdf_path="/tmp/book.pdf")
        self.assertEqual(len(remaining), 1,
                         "a bare table must stay an orphan, not join q7")
        self.assertEqual(recs[7].get("tables"), [],
                         "the chapter answer key must never land on one question")

    def test_inference_ignores_owners_outside_this_chapter(self):
        recs = {11: self._rec(11, solution_text=None)}
        orphans = [self._orphan("Fragment text here.", [686], last_qn=None)]
        stats = self._stats()
        with mock.patch.object(qp, "last_block_on_page",
                               return_value=("solution", 99)):
            remaining = qp.recover_orphans(orphans, recs, "ANA", 38, stats,
                                           pdf_path="/tmp/book.pdf")
        self.assertEqual(len(remaining), 1, "q99 is not in this chapter")
        self.assertIsNone(recs[11]["solution_text"])

    def test_inference_is_skipped_without_a_pdf_path(self):
        """Callers that cannot supply the PDF (unit tests, older paths) must
        keep the pre-fix behaviour rather than crash."""
        recs = {11: self._rec(11, solution_text=None)}
        orphans = [self._orphan("Fragment text here.", [686], last_qn=None)]
        remaining = qp.recover_orphans(orphans, recs, "ANA", 38, self._stats())
        self.assertEqual(len(remaining), 1)
        self.assertIsNone(recs[11]["solution_text"])


class Run22OptionsManualReviewFlagTests(unittest.TestCase):
    """run-22: options harvested off ANOTHER question's solution page are
    FLAGGED for manual review -- never auto-corrected, never rejected.

    Ground truth (ch. 38 q13): the printed options on p670 are anatomical
    structures; what shipped were the "Option A/B/D:" commentary lines from
    p689, which explain q12. The genuine fix is unknowable from the record
    alone, so the contract is: keep the text byte-identical, set a review
    flag, surface it in the gate and the validator."""

    def _q13(self):
        """The real ch. 38 q13 option set, verbatim from out_ch38_final."""
        return {
            "options": {
                "A": "It is the radial groove where radial nerve runs along "
                     "with profunda brachii artery.",
                "B": "It is the lateral epicondyle. Fracture of this part may "
                     "lead to injury of the radial nerve in some cases.",
                "C": "Palmar cutaneous branch of ulnar nerve",
                "D": "It is the neck of humerus where axillary nerve runs "
                     "around.",
            },
            "solution_text": "The deep branch of the ulnar nerve is purely "
                             "motor and supplies the interossei.",
        }

    def test_ch38_q13_is_flagged(self):
        recs = {13: self._q13()}
        why = qp.detect_options_harvested_from_solution(13, recs[13], recs)
        self.assertIsNotNone(why, "known-bad ch38 q13 must be flagged")
        self.assertIn("commentary", why)
        for letter in ("A", "B", "D"):
            self.assertIn(letter, why)

    def test_flagging_never_mutates_the_record(self):
        """The whole point: the reviewer sees exactly what was extracted."""
        recs = {13: self._q13()}
        before = copy.deepcopy(recs[13])
        qp.detect_options_harvested_from_solution(13, recs[13], recs)
        self.assertEqual(before, recs[13],
                         "detector must be read-only -- no auto-correction")

    def test_healthy_options_are_not_flagged(self):
        recs = {1: {"options": {"A": "Deep branch of ulnar nerve",
                                "B": "Ulnar nerve before its division into "
                                     "superficial and deep branch",
                                "C": "Palmar cutaneous branch of ulnar nerve",
                                "D": "Superficial terminal branch of ulnar "
                                     "nerve"},
                    "solution_text": "Answer is A."}}
        self.assertIsNone(
            qp.detect_options_harvested_from_solution(1, recs[1], recs))

    def test_single_letter_diagram_labels_are_not_flagged(self):
        """ch. 38 q12's own options legitimately ARE 'A'/'B'/'C'/'D'."""
        recs = {12: {"options": {k: k for k in "ABCD"},
                     "solution_text": "Medial epicondyle, marked as C."}}
        self.assertIsNone(
            qp.detect_options_harvested_from_solution(12, recs[12], recs))

    def test_one_uncorroborated_commentary_option_is_not_enough(self):
        """A genuine option may read 'It is the ...'; one alone stays quiet."""
        recs = {5: {"options": {"A": "It is the only muscle supplied by the "
                                     "anterior interosseous nerve",
                                "B": "Flexor carpi ulnaris",
                                "C": "Pronator teres",
                                "D": "Supinator"},
                    "solution_text": "A is correct."}}
        self.assertIsNone(
            qp.detect_options_harvested_from_solution(5, recs[5], recs))

    def test_one_commentary_option_IS_flagged_when_another_q_claims_it(self):
        recs = {
            6: {"options": {"A": "It is the radial groove where the radial "
                                 "nerve runs with profunda brachii",
                            "B": "Ulnar nerve", "C": "Median nerve",
                            "D": "Axillary nerve"},
                "solution_text": "B is correct."},
            7: {"options": {}, "solution_text":
                "It is the radial groove where the radial nerve runs with "
                "profunda brachii artery, hence option A."},
        }
        why = qp.detect_options_harvested_from_solution(6, recs[6], recs)
        self.assertIsNotNone(why)
        self.assertIn("q7", why)

    def test_option_quoting_its_own_solution_is_not_proof(self):
        """Self-match must never count -- solutions restate their options."""
        recs = {8: {"options": {"A": "It is the radial groove carrying the "
                                     "radial nerve",
                                "B": "Ulnar", "C": "Median", "D": "Axillary"},
                    "solution_text": "It is the radial groove carrying the "
                                     "radial nerve, so A."}}
        self.assertIsNone(
            qp.detect_options_harvested_from_solution(8, recs[8], recs))

    def test_option_line_prefix_shape_is_detected(self):
        recs = {9: {"options": {"A": "Option A: the radial groove where the "
                                     "radial nerve travels",
                                "B": "Option B: the lateral epicondyle of the "
                                     "humerus bone",
                                "C": "Median nerve", "D": "Axillary nerve"},
                    "solution_text": "C is correct."}}
        self.assertIsNotNone(
            qp.detect_options_harvested_from_solution(9, recs[9], recs))

    def test_export_row_carries_options_suspect_and_manual_review(self):
        src = inspect.getsource(qp)
        self.assertIn('"options_suspect": rec.get("_options_suspect_reason")',
                      src)
        i = src.index('"manual_review": bool(')
        tail = src[i:i + 260]
        self.assertIn("_stem_suspect_reason", tail)
        self.assertIn("_options_suspect_reason", tail)

    def test_gate_reports_options_suspect_without_deleting(self):
        src = inspect.getsource(qp)
        self.assertIn('violations.append(("options_suspect"', src)
        i = src.index('rec.get("_options_suspect_reason")')
        window = src[i:i + 700]
        for banned in ("del rec[", "rec[\"options\"] =", "chapter_records.pop"):
            self.assertNotIn(banned, window,
                             "options_suspect path must not modify the record")

    def test_sweep_sets_the_flag_and_leaves_options_intact(self):
        stats = {}
        recs = {13: self._q13(), 12: {"options": {k: k for k in "ABCD"},
                                      "solution_text": "Medial epicondyle."}}
        original = copy.deepcopy(recs[13]["options"])
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(qp, "DATA_DIR", pathlib.Path(td)):
                qp.chapter_integrity_sweep(recs, {}, "ANA", 38, stats)
        self.assertTrue(recs[13].get("_options_suspect_reason"))
        self.assertEqual(original, recs[13]["options"],
                         "sweep must ship the options exactly as extracted")
        self.assertIsNone(recs[12].get("_options_suspect_reason"))

    def test_validator_emits_a_high_severity_review_flag(self):
        src = inspect.getsource(qv)
        self.assertIn('row.get("options_suspect")', src)
        i = src.index('row.get("options_suspect")')
        window = src[i:i + 500]
        self.assertIn("MANUAL REVIEW", window)
        self.assertIn("HIGH", window)


class Run22TocTruncationTests(unittest.TestCase):
    """run-22: the contents-table scan defaulted to pages 1-3, which found
    only chapters 1-42 of the MARROW Anatomy book's 63. Chapters 43-63 were
    never handed to process_pdf, so a full-book run silently skipped a third
    of the book with no error and no gate flag."""

    def test_default_scan_reaches_past_page_three(self):
        self.assertGreaterEqual(qp.TOC_SCAN_LAST_PAGE, 5)
        # run-23 replaced the pinned default window with a growing scan, so
        # the default is now None ("grow it") rather than a fixed tuple.
        sig = inspect.signature(qp.extract_toc_chapters)
        self.assertIsNone(sig.parameters["toc_page_range"].default)

    def test_contiguous_table_is_kept_whole(self):
        toc = [{"chapter_no": i, "chapter_title": f"Ch {i}",
                "start_printed_page": i * 10} for i in range(1, 64)]
        self.assertEqual(len(qp._longest_toc_run(toc)), 63)

    def test_body_text_noise_is_skipped_without_ending_the_table(self):
        """Past the real table, body pages have '<n> <words> <n>' lines. An
        out-of-sequence line is SKIPPED, not treated as the end of the table:
        a stray match in the middle of a real contents table must not cost us
        the chapters listed after it."""
        toc = [{"chapter_no": 1, "chapter_title": "Intro", "start_printed_page": 5},
               {"chapter_no": 2, "chapter_title": "Bones", "start_printed_page": 20},
               {"chapter_no": 3, "chapter_title": "Joints", "start_printed_page": 40},
               # noise: out-of-sequence number, page jumps backwards
               {"chapter_no": 9, "chapter_title": "see table 9 on", "start_printed_page": 3},
               {"chapter_no": 4, "chapter_title": "Muscles", "start_printed_page": 60}]
        kept = qp._longest_toc_run(toc)
        self.assertEqual([c["chapter_no"] for c in kept], [1, 2, 3, 4])
        self.assertNotIn("see table 9 on", [c["chapter_title"] for c in kept])

    def test_run_must_start_at_chapter_one(self):
        self.assertEqual(qp._longest_toc_run(
            [{"chapter_no": 7, "chapter_title": "Orphan", "start_printed_page": 9}]), [])

    def test_duplicate_listing_keeps_first(self):
        toc = [{"chapter_no": 1, "chapter_title": "A", "start_printed_page": 5},
               {"chapter_no": 1, "chapter_title": "A again", "start_printed_page": 5},
               {"chapter_no": 2, "chapter_title": "B", "start_printed_page": 9}]
        kept = qp._longest_toc_run(toc)
        self.assertEqual([c["chapter_no"] for c in kept], [1, 2])
        self.assertEqual(kept[0]["chapter_title"], "A")

    def test_backwards_page_breaks_the_run(self):
        toc = [{"chapter_no": 1, "chapter_title": "A", "start_printed_page": 50},
               {"chapter_no": 2, "chapter_title": "B", "start_printed_page": 10}]
        self.assertEqual([c["chapter_no"] for c in qp._longest_toc_run(toc)], [1])

    def test_empty_input_is_safe(self):
        self.assertEqual(qp._longest_toc_run([]), [])


class Run23GrowingTocScanTests(unittest.TestCase):
    """run-23: a FIXED TOC scan window is a book-specific constant in
    disguise. 8 pages fits a 63-chapter book with short front matter, but a
    110-chapter book needs ~11 contents pages and would be truncated exactly
    as silently as run-22's 42-of-63. The scan now grows until it stops
    finding chapters."""

    def test_ceiling_is_above_the_first_window(self):
        self.assertGreater(qp.TOC_SCAN_MAX_PAGE, qp.TOC_SCAN_LAST_PAGE)
        self.assertGreater(qp.TOC_SCAN_GROW_STEP, 0)

    def test_explicit_range_is_honoured_exactly(self):
        """Callers and tests that pin a window must not get a grown scan."""
        seen = []

        def fake_scan(pdf, first, last):
            seen.append((first, last))
            return [{"chapter_no": 1, "chapter_title": "A", "start_printed_page": 1}]

        with mock.patch.object(qp, "_scan_toc_candidates", fake_scan):
            qp.extract_toc_chapters("x.pdf", toc_page_range=(1, 3))
        self.assertEqual(seen, [(1, 3)])

    def test_scan_grows_while_it_keeps_finding_chapters(self):
        def fake_scan(pdf, first, last):
            # a contents table that needs ~16 pages: 10 chapters per page
            n = min(last * 10, 110)
            return [{"chapter_no": i, "chapter_title": f"C{i}",
                     "start_printed_page": i * 9} for i in range(1, n + 1)]

        with mock.patch.object(qp, "_scan_toc_candidates", fake_scan):
            got = qp.extract_toc_chapters("x.pdf")
        self.assertEqual(len(got), 110)

    def test_scan_stops_as_soon_as_widening_adds_nothing(self):
        calls = []

        def fake_scan(pdf, first, last):
            calls.append(last)
            return [{"chapter_no": i, "chapter_title": f"C{i}",
                     "start_printed_page": i} for i in range(1, 21)]

        with mock.patch.object(qp, "_scan_toc_candidates", fake_scan):
            got = qp.extract_toc_chapters("x.pdf")
        self.assertEqual(len(got), 20)
        # first window, then ONE probe that found nothing new, then stop
        self.assertEqual(len(calls), 2)

    def test_scan_never_exceeds_the_ceiling(self):
        seen = []

        def fake_scan(pdf, first, last):
            seen.append(last)
            return [{"chapter_no": i, "chapter_title": f"C{i}",
                     "start_printed_page": i} for i in range(1, last * 10)]

        with mock.patch.object(qp, "_scan_toc_candidates", fake_scan):
            qp.extract_toc_chapters("x.pdf")
        self.assertLessEqual(max(seen), qp.TOC_SCAN_MAX_PAGE)

    def test_body_noise_past_the_table_cannot_extend_the_run(self):
        """Widening reaches body pages whose lines match the TOC regex."""
        def fake_scan(pdf, first, last):
            toc = [{"chapter_no": i, "chapter_title": f"C{i}",
                    "start_printed_page": i * 9} for i in range(1, 13)]
            if last > 8:      # body pages now in range
                toc += [{"chapter_no": 3, "chapter_title": "patients were given",
                         "start_printed_page": 40},
                        {"chapter_no": 1, "chapter_title": "result was", "start_printed_page": 5}]
            return toc

        with mock.patch.object(qp, "_scan_toc_candidates", fake_scan):
            got = qp.extract_toc_chapters("x.pdf")
        self.assertEqual([c["chapter_no"] for c in got], list(range(1, 13)))


class Run23BlankOptionHealTests(unittest.TestCase):
    """run-23: ch. 30 q16's option D was printed at the top of the NEXT page,
    so the Q-pass emitted {"D": None}. targeted_retry used setdefault(), which
    only fills ABSENT keys -- a present-but-empty option could never be
    healed. The model returned the right text in both rounds and both were
    silently discarded; the round then scored 0 fixes, tripping the "no
    progress -- stopping" rule, which ALSO cancelled the retry for an
    unrelated solution gap in the same chapter."""

    @staticmethod
    def _apply(rec_options, fix_options):
        """The patch-apply block from targeted_retry, in isolation."""
        rec = {"options": dict(rec_options)}
        filled_here = 0
        for k, v in fix_options.items():
            if not (v and str(v).strip()):
                continue
            key = str(k).strip().upper()
            if not (rec["options"].get(key) or "").strip():
                rec["options"][key] = v
                filled_here += 1
        return rec["options"], filled_here

    def test_blank_option_is_healed(self):
        opts, n = self._apply(
            {"A": "Superior thyroid artery", "B": "Facial artery",
             "C": "Ascending pharyngeal artery", "D": None},
            {"D": "Lingual artery"})
        self.assertEqual(opts["D"], "Lingual artery")
        self.assertEqual(n, 1)

    def test_whitespace_only_option_is_healed(self):
        opts, n = self._apply({"A": "x", "B": "y", "C": "z", "D": "   "},
                              {"D": "Lingual artery"})
        self.assertEqual(opts["D"], "Lingual artery")
        self.assertEqual(n, 1)

    def test_absent_option_still_added(self):
        opts, n = self._apply({"A": "x", "B": "y", "C": "z"}, {"D": "w"})
        self.assertEqual(opts["D"], "w")
        self.assertEqual(n, 1)

    def test_real_option_text_is_never_overwritten(self):
        """A retry is a patch, not permission to rewrite good data."""
        opts, n = self._apply({"A": "correct text", "B": "y", "C": "z", "D": "w"},
                              {"A": "MODEL HALLUCINATION"})
        self.assertEqual(opts["A"], "correct text")
        self.assertEqual(n, 0)

    def test_empty_patch_value_is_not_counted_as_progress(self):
        opts, n = self._apply({"A": "x", "B": "y", "C": "z", "D": None}, {"D": None})
        self.assertIsNone(opts["D"])
        self.assertEqual(n, 0)

    def test_blank_option_is_reported_as_incomplete(self):
        """The detector was already right -- keep it that way."""
        recs = {16: {"question_text": "Q?", "correct_option": "C",
                     "solution_text": "S", "tables": [],
                     "options": {"A": "a", "B": "b", "C": "c", "D": None}}}
        missing = dict(qp.find_incomplete_records(recs))
        self.assertIn("options", missing.get(16, []))


class Run24PositionalOwnerTiebreakTests(unittest.TestCase):
    """P3: a q_no-less fragment must not be owned by the window's last
    question when its own words point at a different one (ch.60 q7 -> q15)."""

    def test_ch60_q7_fragment_is_not_given_to_q15(self):
        """The real miss: fragment about the trochlear nerve / superior
        oblique belongs to q7, but the Q-pass window ended on q15."""
        recs = {
            7: {"question_text": "Which nerve supplies the superior oblique muscle?",
                "solution_text": "The trochlear nerve supplies the superior oblique",
                "tables": []},
            15: {"question_text": "Which artery supplies the stomach fundus?",
                 "solution_text": "The short gastric arteries arise from the splenic artery "
                                  "and supply the fundus of the stomach completely.",
                 "tables": []},
        }
        frag = ("trochlear nerve emerges dorsally and decussates before "
                "supplying the superior oblique muscle of the orbit")
        verdict, better, detail = qp._positional_owner_contested(frag, 15, recs)
        self.assertNotEqual(verdict, "ok", "positional guess must be challenged")
        self.assertEqual(better, 7)
        self.assertIn("q7", detail)

    def test_veto_when_true_owner_solution_is_already_complete(self):
        recs = {
            7: {"question_text": "Trochlear nerve question?",
                "solution_text": "The trochlear nerve supplies the superior oblique muscle "
                                 "and emerges dorsally from the brainstem.",
                "tables": []},
            15: {"question_text": "Gastric artery question?",
                 "solution_text": "Splenic artery supplies the fundus.", "tables": []},
        }
        frag = "trochlear nerve emerges dorsally supplying the superior oblique muscle"
        verdict, better, _ = qp._positional_owner_contested(frag, 15, recs)
        self.assertEqual(verdict, "veto")
        self.assertEqual(better, 7)

    def test_redirect_when_true_owner_solution_is_truncated(self):
        recs = {
            7: {"question_text": "Trochlear nerve question?",
                "solution_text": "The trochlear nerve supplies the superior oblique:",
                "tables": []},
            15: {"question_text": "Gastric artery question?",
                 "solution_text": "Splenic artery supplies the fundus.", "tables": []},
        }
        frag = "trochlear nerve emerges dorsally supplying the superior oblique muscle"
        verdict, better, _ = qp._positional_owner_contested(frag, 15, recs)
        self.assertEqual(verdict, "redirect")
        self.assertEqual(better, 7)

    def test_agreeing_positional_owner_is_left_alone(self):
        """No regression: when position and content agree, say nothing."""
        recs = {15: {"question_text": "Which artery supplies the fundus?",
                     "solution_text": "The short gastric arteries supply the fundus.",
                     "tables": []}}
        frag = "short gastric arteries arising from the splenic artery supply the fundus"
        verdict, better, _ = qp._positional_owner_contested(frag, 15, recs)
        self.assertEqual(verdict, "ok")
        self.assertIsNone(better)

    def test_short_fragment_carries_no_evidence(self):
        recs = {7: {"question_text": "Q7", "solution_text": "trochlear nerve", "tables": []},
                15: {"question_text": "Q15", "solution_text": "splenic artery", "tables": []}}
        verdict, _, _ = qp._positional_owner_contested("see below:", 15, recs)
        self.assertEqual(verdict, "ok")

    def test_generic_fragment_below_floor_does_not_steal(self):
        """A fragment sharing only boilerplate must not beat the position."""
        recs = {7: {"question_text": "Q7 stem", "solution_text": "Therefore option A.",
                    "tables": []},
                15: {"question_text": "Q15 stem", "solution_text": "Hence option C.",
                     "tables": []}}
        verdict, _, _ = qp._positional_owner_contested(
            "xylophone quasar tessellation buttress", 15, recs)
        self.assertEqual(verdict, "ok")

    def test_no_qno_item_is_returned_for_orphan_recovery_not_dropped(self):
        """P2: the 'no q_no' path routes content onward -- it is not a drop."""
        existing = {}
        stats = {"duplicates_merged": 0, "conflicts": 0, "foreign_chapter_qno_dropped": 0}
        item = {"q_no": None, "solution_text": "orphaned continuation text"}
        _, skipped = qp.merge_question_records(existing, [item], stats)
        self.assertEqual(skipped, [item])


class Run24LeadInFlagParityTests(unittest.TestCase):
    """P4: the validator must honour the same figure lead-in exemption the
    pipeline sweep already applies (ch.60 q3/q21 double-flagged)."""

    def _flags(self, sol):
        row = {"id": "ANA-060-003", "chapter_id": "ANA-060", "q_no": 3,
               "question": {"text": "Q?", "options": [
                   {"label": "A", "text": "a"}, {"label": "B", "text": "b"},
                   {"label": "C", "text": "c"}, {"label": "D", "text": "d"}]},
               "correct_options": ["A"], "solution": sol}
        return [f["kind"] for f in qv.check_row(row, "/nonexistent-assets")]

    def test_dangling_colon_with_figure_is_not_truncated(self):
        kinds = self._flags({"text": "The muscles of mastication are shown below:",
                             "tables": [], "images": [{"file": "fig1.png"}]})
        self.assertNotIn("truncated_solution", kinds)

    def test_dangling_colon_with_table_still_exempt(self):
        kinds = self._flags({"text": "The values are listed below:",
                             "tables": [{"markdown": "| a |"}], "images": []})
        self.assertNotIn("truncated_solution", kinds)

    def test_dangling_colon_with_nothing_is_still_flagged(self):
        kinds = self._flags({"text": "The muscles of mastication are shown below:",
                             "tables": [], "images": []})
        self.assertIn("truncated_solution", kinds)

    def test_real_mid_flow_cut_with_a_figure_is_still_flagged(self):
        """The exemption is for ':' lead-ins only -- a genuine mid-sentence
        cut must not be excused just because a figure happens to be attached."""
        kinds = self._flags({"text": "The nerve travels through the foramen and then \u2014",
                             "tables": [], "images": [{"file": "fig1.png"}]})
        self.assertIn("truncated_solution", kinds)


class Run24KeyRotationBatchLossTests(unittest.TestCase):
    """User-reported audit: when a key exhausts and the pool rotates, is any
    batch skipped? Two real defects were found in the MAIN batch loop's 429
    handler and are locked down here."""

    def _replay(self, scenario, keys_after_rotation):
        """Mirror of the main loop's 429 handler AS FIXED (qbank_pipeline
        ~:6520-6595). Returns (raw_items, n_model_calls, pages_sent, log)."""
        calls, pages_sent, log = {"n": 0}, [], []

        def call_gemini(which, pages):
            calls["n"] += 1
            pages_sent.append((which, tuple(pages)))
            outcome = scenario.pop(0)
            log.append(f"{which}:{outcome}")
            if outcome == "ok":
                return ["ITEM"]
            raise Exception(outcome)

        def handle_429():
            return keys_after_rotation.pop(0) if keys_after_rotation else False

        def salvage(pages):
            log.append("salvage")
            pages_sent.append(("salvage", tuple(pages)))
            return ["SALVAGED"]

        batch = ["p1", "p2", "p3"]          # raw window
        pass_batch = ["p1", "p2"]           # p3 routed away (PREFLIGHT_OCR)
        raw_items = None
        try:
            call_gemini("first", pass_batch)
        except Exception as e:
            if "429" in str(e):
                try:
                    raw_items = call_gemini("post-backoff", pass_batch)
                except Exception as e2:
                    t2, rotated_ok = str(e2), False
                    if "429" in t2:
                        if handle_429():
                            log.append("ROTATED")
                            try:
                                raw_items = call_gemini("post-rotation", pass_batch)
                                rotated_ok = True
                            except Exception:
                                raw_items = salvage(pass_batch)
                                rotated_ok = True
                        else:
                            log.append("EXIT")
                            return None, calls["n"], pages_sent, log
                    if not rotated_ok:
                        log.append("failed-differently")
                        raw_items = salvage(pass_batch)
        return raw_items, calls["n"], pages_sent, log

    def test_successful_rotation_keeps_its_data(self):
        """BUG 1: the post-backoff handler ran the 'failed differently' branch
        UNCONDITIONALLY, so a SUCCESSFUL post-rotation call had its items
        thrown away and replaced by a page-by-page re-ask of the same pages."""
        items, n_calls, _pages, log = self._replay(
            ["429 quota", "429 quota", "ok"], [True])
        self.assertEqual(items, ["ITEM"], "rotated-key data must be kept")
        self.assertNotIn("salvage", log, "must not re-ask pages it already has")
        self.assertEqual(n_calls, 3, "no wasted calls on the fresh key")

    def test_retry_path_never_sends_routed_away_pages(self):
        """BUG 2: the backoff/rotation calls sent `batch` (raw window) instead
        of `pass_batch`, re-asking recitation-sensitive pages that
        _batch_after_routing had deliberately removed for this pass."""
        _items, _n, pages_sent, _log = self._replay(
            ["429 quota", "429 quota", "ok"], [True])
        for which, pages in pages_sent:
            self.assertNotIn("p3", pages,
                             f"{which} re-sent a routed-away page")

    def test_batch_is_never_silently_skipped_on_rotation(self):
        """The user's actual question: can a batch be dropped when the key
        switches? No -- every path either returns items or salvages."""
        for scenario, keys in (
                (["429 quota", "429 quota", "ok"], [True]),          # rotate, ok
                (["429 quota", "429 quota", "429 quota"], [True]),   # rotate, still bad
                (["429 quota", "500 server error"], [True]),         # non-429
        ):
            items, _n, _p, log = self._replay(list(scenario), list(keys))
            self.assertTrue(items, f"batch produced nothing: {log}")

    def test_exhausted_pool_exits_before_marking_chapter_done(self):
        """When every key is spent the run exits; the chapter must NOT be in
        chapters_done, so a resume re-processes it whole (no silent gap)."""
        items, _n, _p, log = self._replay(["429 quota", "429 quota"], [False])
        self.assertIn("EXIT", log)
        self.assertIsNone(items)

    def test_page_by_page_retry_rotates_and_continues(self):
        """retry_batch_page_by_page must rotate and RETRY the same page, not
        move on to the next one (that would drop the page's content)."""
        src = inspect.getsource(qp.retry_batch_page_by_page)
        rot = src.index("handle_429")
        self.assertIn("continue", src[rot:rot + 700],
                      "must re-attempt the SAME page after rotation")
        self.assertIn("failed even alone", src,
                      "an unrecoverable page must still be queued for drain")


class Run24QuotaDayBoundaryTests(unittest.TestCase):
    """Google resets Gemini RPD at midnight US/Pacific, not UTC midnight.
    Rolling over early makes the pool think spent keys are fresh, 429 on the
    first call, and park all 6 keys as exhausted for nothing."""

    def test_stamp_uses_pacific_not_container_clock(self):
        import datetime as _dt
        from zoneinfo import ZoneInfo
        expected = _dt.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        self.assertEqual(qp.today_stamp(), expected)

    def test_stamp_differs_from_utc_inside_the_danger_window(self):
        """Between UTC midnight and 07/08:00 UTC the two dates MUST differ --
        that is exactly the window the old code got wrong."""
        import datetime as _dt
        from zoneinfo import ZoneInfo
        utc = _dt.datetime.now(_dt.timezone.utc)
        pac = utc.astimezone(ZoneInfo("America/Los_Angeles"))
        if utc.strftime("%Y-%m-%d") != pac.strftime("%Y-%m-%d"):
            self.assertEqual(qp.today_stamp(), pac.strftime("%Y-%m-%d"))
            self.assertNotEqual(qp.today_stamp(), utc.strftime("%Y-%m-%d"))

    def test_tz_is_overridable(self):
        self.assertTrue(qp.QUOTA_RESET_TZ)
        self.assertIsNotNone(qp._quota_tz())

    def test_fallback_offset_is_never_early(self):
        """If tzdata is missing we fall back to fixed UTC-8. During US DST
        that is one hour LATE, which is safe; it must never be EARLY."""
        import datetime as _dt
        from zoneinfo import ZoneInfo
        real = _dt.datetime.now(ZoneInfo("America/Los_Angeles"))
        fb = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-8)))
        self.assertLessEqual(fb, real.replace(tzinfo=fb.tzinfo) + _dt.timedelta(seconds=1))

    def test_rollover_resets_pool_counters(self):
        state = {"calls_today": 99, "day_stamp": "1999-01-01"}
        qp.reset_daily_counter_if_needed(state)
        self.assertEqual(state["calls_today"], 0)
        self.assertEqual(state["day_stamp"], qp.today_stamp())

    def test_same_day_does_not_reset(self):
        state = {"calls_today": 42, "day_stamp": qp.today_stamp()}
        qp.reset_daily_counter_if_needed(state)
        self.assertEqual(state["calls_today"], 42, "must not wipe today's count")


class Run24SelfLabeledSolutionHeadTests(unittest.TestCase):
    """ch7 q19/q20 (pp.129-130): a "Solution to Question 19:" heading sat at
    the BOTTOM of p129 and its body ran onto p130, so the model emitted that
    body as q20's solution. q19 exported EMPTY while its 767-char solution
    sat glued to the head of q20, ahead of a self-labeled
    "Solution to Question 20:" header. Sweep 2b only looked at headers naming
    a DIFFERENT question, so this fell through."""

    @staticmethod
    def _rec(sol):
        return {"question_text": "stem",
                "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                "correct_option": "A", "_prov": {}, "tables": [],
                "solution_text": sol}

    GLUED = ("Failure of fusion of the dorsal and ventral pancreatic buds leads "
             "to pancreas divisum.\nThe developing pancreatic ducts usually fuse "
             "so the dorsal drains into the ventral duct.\n"
             "Solution to Question 20:\n"
             "The spleen develops from the mesoderm in the dorsal mesogastrium.\n"
             "The mesenchymal cells form lobular masses called spleniculi.")

    def test_head_is_returned_to_the_solutionless_owner(self):
        recs = {19: self._rec(""), 20: self._rec(self.GLUED)}
        qp.chapter_integrity_sweep(recs, {}, "ANA", 7, {})
        self.assertIn("divisum", recs[19]["solution_text"])
        self.assertNotIn("spleen", recs[19]["solution_text"])
        self.assertIn("spleen", recs[20]["solution_text"])
        self.assertNotIn("divisum", recs[20]["solution_text"])

    def test_split_is_recorded_in_stats(self):
        recs = {19: self._rec(""), 20: self._rec(self.GLUED)}
        stats = {}
        qp.chapter_integrity_sweep(recs, {}, "ANA", 7, stats)
        self.assertEqual(stats.get("solution_heads_reassigned"), 1)

    def test_never_overwrites_a_question_that_has_a_solution(self):
        """The other three real occurrences in ch1/ch7 had a previous question
        that already owned its solution -- those must be left alone."""
        keep = "q19 already owns a perfectly good solution and must keep it."
        recs = {19: self._rec(keep), 20: self._rec(self.GLUED)}
        qp.chapter_integrity_sweep(recs, {}, "ANA", 7, {})
        self.assertEqual(recs[19]["solution_text"], keep)

    def test_donates_to_the_NEAREST_preceding_empty_question(self):
        recs = {15: self._rec(""), 19: self._rec(""), 20: self._rec(self.GLUED)}
        qp.chapter_integrity_sweep(recs, {}, "ANA", 7, {})
        self.assertIn("divisum", recs[19]["solution_text"], "nearest, not earliest")
        self.assertEqual(recs[15]["solution_text"], "")

    def test_retry_guard_accepts_the_rescue_fragment(self):
        """_solution_fragment_foreign vetoed the correct retry with 'first line
        exists verbatim in q20's solution' -- true, but q20 was the thief."""
        ch = {19: {"solution_text": ""}, 20: {"solution_text": self.GLUED}}
        frag = ("Failure of fusion of the dorsal and ventral pancreatic buds leads "
                "to pancreas divisum.")
        self.assertIsNone(
            qp._solution_fragment_foreign(frag, 19, {"options": {}}, ch))

    def test_retry_guard_still_blocks_genuine_contamination(self):
        """Donor is NOT a glued block -> the old proof must still fire."""
        line = "The spleen develops from the mesoderm in the dorsal mesogastrium."
        ch = {19: {"solution_text": ""}, 20: {"solution_text": line + " More."}}
        self.assertIsNotNone(
            qp._solution_fragment_foreign(line, 19, {"options": {}}, ch))

    def test_header_naming_another_question_still_trims_the_tail(self):
        """Sweep 2b must be unaffected by 2c."""
        recs = {1: self._rec("q1 solution body that is long enough to be real.\n"
                             "Solution to Question 2:\nq2 body glued on."),
                2: self._rec("q2 owns its own solution already, proving redundancy.")}
        qp.chapter_integrity_sweep(recs, {}, "ANA", 11, {})
        self.assertNotIn("q2 body glued on", recs[1]["solution_text"])


class Run25SlicedFigureStitchTests(unittest.TestCase):
    """Defect A: one printed figure stored as N abutting image XObjects was
    saved as N files, and the attribution pass then scattered those fragments
    over N different questions. Real case (DER p11): the infant-back photo
    came out as 3 strips, all 3 attached to q15 while its true owner q22 got
    none. Slices must be regrouped by drawn geometry and stitched."""

    # the real DER p7 geometry: (y, x, draw_idx, w, h), bottom-left origin
    P7 = {17: (406.9, 161.5, 1, 224.9, 108.3),
          18: (298.8, 385.9, 2, 64.6, 216.5),
          19: (298.8, 161.5, 3, 224.9, 108.4)}
    WATERMARK = {2393: (-1.3, -1.5, 0, 614.9, 794.7)}

    def _rects(self, pos):
        return {k: qp._rect_from_position(v) for k, v in pos.items()}

    def test_three_slices_of_one_figure_group_together(self):
        groups = qp._group_slice_rects(self._rects(self.P7))
        self.assertEqual(len(groups), 1, "p7's 3 strips are ONE printed figure")
        self.assertCountEqual(groups[0], [17, 18, 19])

    def test_full_page_watermark_does_not_swallow_the_figures(self):
        """A page-sized backdrop contains every figure; without the
        containment guard union-find merged the whole page into one blob."""
        pos = dict(self.P7); pos.update(self.WATERMARK)
        groups = qp._group_slice_rects(self._rects(pos))
        self.assertEqual(len(groups), 2)
        self.assertIn([2393], [sorted(g) for g in groups])

    def test_two_separate_figures_on_one_page_stay_separate(self):
        """DER p18 prints two distinct photos, 3 slices each -> 2 figures."""
        pos = {53: (600.0, 161.5, 1, 224.9, 108.0),
               54: (600.0, 386.4, 2, 64.6, 108.0),
               55: (491.0, 161.5, 3, 289.5, 108.0),
               # second figure, a clear gutter below the first
               56: (300.0, 161.5, 4, 224.9, 108.0),
               57: (300.0, 386.4, 5, 64.6, 108.0),
               58: (191.0, 161.5, 6, 289.5, 108.0)}
        groups = [sorted(g) for g in qp._group_slice_rects(self._rects(pos))]
        self.assertEqual(len(groups), 2)
        self.assertCountEqual(groups, [[53, 54, 55], [56, 57, 58]])

    def test_side_by_side_figures_with_a_real_gutter_are_not_merged(self):
        pos = {1: (500.0, 60.0, 0, 200.0, 150.0),
               2: (500.0, 330.0, 1, 200.0, 150.0)}   # 70 pt gutter
        self.assertEqual(len(qp._group_slice_rects(self._rects(pos))), 2)

    def test_positions_report_the_union_rect_under_the_lead_slice_id(self):
        """Every consumer looks geometry up by the object id parsed out of the
        saved filename, so the lead id must resolve to the WHOLE figure."""
        merged = {}
        rects = self._rects(self.P7)
        for g in qp._group_slice_rects(rects):
            merged[qp._group_lead(g, rects)] = g
        self.assertEqual(list(merged), [17], "topmost slice names the file")

    def test_stitched_canvas_spans_the_union_and_places_each_slice(self):
        rects = self._rects(self.P7)
        members = [(k, Image.new("RGB", (int(self.P7[k][3]) * 2,
                                         int(self.P7[k][4]) * 2), col))
                   for k, col in ((17, "red"), (18, "green"), (19, "blue"))]
        out = qp._stitch_slices(members, rects)
        # union is 289.0 x 216.7 pt at 2 px/pt
        self.assertAlmostEqual(out.width / out.height, 289.0 / 216.7, places=1)
        self.assertEqual(out.getpixel((5, 5)), (255, 0, 0))          # top-left
        self.assertEqual(out.getpixel((out.width - 5, out.height - 5)),
                         (0, 128, 0))                                 # right col
        self.assertEqual(out.getpixel((5, out.height - 5)), (0, 0, 255))

    def test_single_image_page_is_returned_untouched(self):
        one = Image.new("RGB", (720, 540), "white")
        self.assertIs(qp._stitch_slices([(22, one)],
                                        {22: (162.0, 56.0, 450.0, 272.0)}), one)


class Run25CarryAdvancesOnImagelessPagesTests(unittest.TestCase):
    """Defect B: `if not imgs: continue` dropped image-less pages from the
    window list, and the carry advance lived in the claim loop over that same
    list -- so a text-only page's headings never moved active_block and the
    next page's top figure was attributed to a block that had closed pages
    earlier (29/210 carries wrong on DER ch1-9, 2 of them crossing the
    question->solution boundary)."""

    def _window_loop_src(self):
        src = Path(qp.__file__).read_text()
        i = src.index("window_seq = []")
        return src[i:src.index("FIGURE-MAP pass", i)]

    def test_imageless_pages_stay_in_the_page_sequence(self):
        body = self._window_loop_src()
        head = body[:body.index("pos = image_positions_on_page")]
        self.assertIn("window_seq.append((file_page_num, []))", head,
                      "an image-less page must still enter the sequence")

    def test_claim_loop_iterates_the_full_sequence_not_just_imaged_pages(self):
        self.assertIn("for file_page_num, rels in window_seq:",
                      self._window_loop_src())

    def test_carry_advances_before_skipping_an_imageless_page(self):
        body = self._window_loop_src()
        i = body.index("for file_page_num, rels in window_seq:")
        guard = body[i:body.index("leftover = claim_page_images", i)]
        self.assertIn("last_block_on_page(pdf_path, file_page_num)", guard)
        self.assertLess(guard.index("active_block = _last"),
                        guard.rindex("continue"),
                        "advance the carry BEFORE continuing past the page")

    def test_figure_map_still_only_sees_pages_that_have_images(self):
        """window_rows (imaged pages only) must keep feeding the fallbacks --
        an empty row would break the exact-count guard."""
        body = self._window_loop_src()
        self.assertIn("window_rows.append((file_page_num, ordered))", body)


if __name__ == "__main__":
    unittest.main()
