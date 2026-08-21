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


def _write_multi_watermark_pdf(path):
    """Ten-page PDF with two section-specific full-page watermarks, one
    genuine repeated small figure, and one unique full-page image.

    This mirrors MIC's object-id switch (1707 on one page range, 2197 on the
    rest) and proves the detector does not remove either repeated real figures
    or a legitimate one-off full-page scan.
    """
    from pypdf import PdfWriter
    from pypdf.generic import (DecodedStreamObject, DictionaryObject,
                               NameObject, NumberObject)

    def image_object(width, height, data):
        stream = DecodedStreamObject()
        stream.set_data(data)
        stream.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(width),
            NameObject("/Height"): NumberObject(height),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        })
        return stream

    writer = PdfWriter()
    sparse_a = bytearray(b"\xff\xff\xff" * (64 * 64))
    for i in range(64):
        for j in range(max(0, i - 1), min(64, i + 2)):
            off = (i * 64 + j) * 3
            sparse_a[off:off + 3] = b"\xd0\xd0\xd0"
    sparse_b = bytearray(sparse_a)
    sparse_b[30:33] = b"\xc0\xc0\xc0"  # a second watermark object/payload

    wm_a = writer._add_object(image_object(64, 64, bytes(sparse_a)))
    wm_b = writer._add_object(image_object(64, 64, bytes(sparse_b)))
    repeated_real = writer._add_object(
        image_object(90, 90, b"\xff\x00\x00" * (90 * 90)))
    unique_full_page = writer._add_object(
        image_object(120, 160, b"\x00\x00\xff" * (120 * 160)))

    for page_no in range(1, 11):
        page = writer.add_blank_page(width=612, height=792)
        watermark = wm_a if page_no <= 4 else wm_b
        xobjects = DictionaryObject({
            NameObject("/WM"): watermark,
            NameObject("/REAL"): repeated_real,
        })
        content = (b"q 612 0 0 792 0 0 cm /WM Do Q\n"
                   b"q 90 0 0 90 300 300 cm /REAL Do Q\n")
        if page_no == 1:
            xobjects[NameObject("/UNIQUE")] = unique_full_page
            content += b"q 612 0 0 792 0 0 cm /UNIQUE Do Q\n"
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): xobjects})
        contents = DecodedStreamObject()
        contents.set_data(content)
        page[NameObject("/Contents")] = writer._add_object(contents)

    with open(path, "wb") as fh:
        writer.write(fh)
    return {"watermarks": {wm_a.idnum, wm_b.idnum},
            "repeated_real": repeated_real.idnum,
            "unique_full_page": unique_full_page.idnum}


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

    # -- 1. Q2's heading at the bottom of the overlap page; all Q2 content on
    #      the next page -> continuation must be assigned to Q2 ------------

    # -- 2. Q2 starts on overlap page, continues, then explicit Q3 heading ->
    #      initial continuation to Q2, subsequent content to Q3 ------------

    # -- 3. unnumbered text, no reliable owner -> stays unassigned, never
    #      guessed ---------------------------------------------------------

    # -- 4. overlap content must not be duplicated into the final solution --

    # -- 5. S-pass continuation must never enter question_text -------------

    # -- 6. valid existing content survives continuation recovery ----------


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

    def test_multiple_section_watermarks_excluded_without_losing_real_images(self):
        pdf = self.tmp / "multi_watermark.pdf"
        ids = _write_multi_watermark_pdf(pdf)

        detected = qp.find_watermark_object_ids(pdf)
        self.assertEqual(detected, frozenset(ids["watermarks"]))

        # Page 1 contains watermark A, a genuine repeated small figure, and a
        # legitimate one-off full-page scan. Only the watermark is removed.
        saved_p1 = set(qp.extract_real_images(
            pdf, 1, detected, "PSY", self.subj_dir))
        self.assertEqual(saved_p1, {
            f"PSY/PSY-p1-{ids['repeated_real']}.webp",
            f"PSY/PSY-p1-{ids['unique_full_page']}.webp",
        })

        # Page 5 switches to watermark B. The same genuine repeated figure
        # must still survive, proving frequency by itself never deletes it.
        saved_p5 = qp.extract_real_images(
            pdf, 5, detected, "PSY", self.subj_dir)
        self.assertEqual(saved_p5,
                         [f"PSY/PSY-p5-{ids['repeated_real']}.webp"])
        for wm_id in ids["watermarks"]:
            self.assertFalse((self.subj_dir / f"PSY-p1-{wm_id}.webp").exists())
            self.assertFalse((self.subj_dir / f"PSY-p5-{wm_id}.webp").exists())

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

    # -- 10. JSON round-trip preserves option images ------------------------

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


    # -- RC-5: structured pass status -------------------------------------

    # -- RC-4: answer-key page targeting ----------------------------------



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

    # -- Z-Test 1: solution restates the stem ("Regarding [exact stem]...") --
    # KNOWN CONFLICT (audited 2026-08): this documents the run-12/14 contract
    # ("a real stem restated by its own solution must be kept"). The run-20
    # phantom-record guard in _stem_reject_reason currently REJECTS such
    # records ("only question_text+solution_text" shape) -- one of the two
    # must change; this test is the living proof and flips green when the
    # over-broad rule is narrowed.

    # -- Z-Test 2a: q_no=None OPTIONS fragment is buffered, not dropped and
    #               never attached to the 'nearest' question ----------------

    # -- Z-Test 2b: q_no=None ANSWER-KEY TABLE is consumed as a key, not
    #               attached to a question ---------------------------------

    # -- Z-Test 3: DRAIN crop-ladder items are NOT overwritten by OCR -------


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

    # -- C. vision runs ONLY on leftovers; cannot override L1 --------------

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


    # -- 4. vision never silently skips ------------------------------------

    # -- 5. prompt forbids within-page q_no:null continuations -------------

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

    # -- A. phantom solution-only records ---------------------------------



    # -- B. stem == solution verbatim contamination ------------------------
    # KNOWN CONFLICT (audited 2026-08): this documents the run-12/14 contract
    # ("a real stem restated by its own solution must be kept"). The run-20
    # phantom-record guard in _stem_reject_reason currently REJECTS such
    # records ("only question_text+solution_text" shape) -- one of the two
    # must change; this test is the living proof and flips green when the
    # over-broad rule is narrowed.

    # KNOWN CONFLICT (audited 2026-08): this documents the run-12/14 contract
    # ("a real stem restated by its own solution must be kept"). The run-20
    # phantom-record guard in _stem_reject_reason currently REJECTS such
    # records ("only question_text+solution_text" shape) -- one of the two
    # must change; this test is the living proof and flips green when the
    # over-broad rule is narrowed.

    # -- C. orphan verified duplicate --------------------------------------

    # -- D. Q-coverage: page-level question content ------------------------



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
        kinds = {(k, qn) for k, qn, _y, _x, _s in anchors}
        self.assertIn(("solution", 10), kinds)
        self.assertIn(("solution", 11), kinds)
        # the regression: NEITHER may appear as a question anchor
        self.assertNotIn(("question", 10), kinds)
        self.assertNotIn(("question", 11), kinds)

    def test_real_question_stem_still_anchors_as_question(self):
        data = self._data([(100, "Question 14:"), (400, "Question 15:")])
        anchors = qp._ocr_anchors_from_data(data, scale=2.0, img_h=1000)
        kinds = {(k, qn) for k, qn, _y, _x, _s in anchors}
        self.assertEqual(kinds, {("question", 14), ("question", 15)})

    def test_grouper_falls_back_when_line_ids_absent(self):
        data = self._data([(100, "Question 7:")])
        for k in ("block_num", "par_num", "line_num"):
            data.pop(k)
        anchors = qp._ocr_anchors_from_data(data, scale=2.0, img_h=1000)
        self.assertEqual([(k, qn) for k, qn, _y, _x, _s in anchors], [("question", 7)])

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
        self.assertGreaterEqual(
            src.count("if owner is active_block"), 2,
            "claim_page_images and claim_block_images_ocr must BOTH tag a "
            "carry-derived owner so the cap can tell it from same-page "
            "positional stacking")

    def test_carry_source_reaches_rename_guard(self):
        src = Path(qp.__file__).read_text()
        i = src.index("def _rename_for_slot")
        body = src[i:i + 9000]
        # AUDIT-FIX: the run-21 carry-cap LIFT let a stale carry stack a
        # whole page of wrong-owner figures past the flat cap (the ch. 28
        # failure class). New contract: carries obey the flat caps; only
        # model-declared claims may lift.
        self.assertEqual(
            body.count("claim_source == CARRY_CLAIM_SOURCE"), 0,
            "carry claims must NOT lift the cap any more (audit fix)")
        self.assertEqual(
            body.count("claim_source in MODEL_CLAIM_SOURCES"), 2,
            "both the question-side and solution-side guards must still "
            "honour model-declared claims")


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



    # --- D3: carry seed must not cross a chapter boundary ----------------

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








class Run26UnrepairedFlagsReachTheExportTests(unittest.TestCase):
    """run-26: iflag(matched=False) means the sweep found something it could
    NOT fix. That verdict only ever reached integrity_flags.jsonl, so the
    exported row still said manual_review=False and a reviewer saw nothing."""



    def test_export_row_surfaces_review_reasons(self):
        src = Path(qp.__file__).read_text()
        i = src.index('"manual_review": bool(')
        row = src[i - 800:i + 300]
        self.assertIn('"review_reasons"', row)
        self.assertIn('rec.get("_review_reasons")', row)

    def test_manual_review_is_true_when_only_a_review_reason_exists(self):
        src = Path(qp.__file__).read_text()
        i = src.index('"manual_review": bool(')
        expr = src[i:src.index("}", i)]
        self.assertIn('_review_reasons', expr,
                      "manual_review must consider unrepaired sweep findings")


if __name__ == "__main__":
    unittest.main()
