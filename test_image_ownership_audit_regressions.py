#!/usr/bin/env python3
"""
Regression suite for the AUDIT FIXES in qbank_pipeline.py (post-audit branch).

These tests assert the CORRECT behavior on exactly the scenarios that the
audit proved broken on the pre-fix code (same harness: real claim chain,
only the lowest-level page readers stubbed). Pair with
test_image_ownership_audit.py (the pre-fix bug proofs) for before/after.

Run:  python3 test_image_ownership_fixed.py     (repo must be on sys.path)
"""
import json
import os
import sys
import tempfile
import shutil
import random
import unittest
from pathlib import Path

# Portable: the checkout containing THIS test file (was hardcoded to a local
# dev path /home/user/repo -- that pinned imports to a stale sibling copy and
# shadowed the real app module in combined suite runs).
REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="auditfix_env_"))
os.environ["OUTPUT_DIR"] = str(_TMP / "out")

import qbank_pipeline as qp  # noqa: E402

SUBJECT = "OPH"
CH_NO = 28
CH_ID = f"{SUBJECT}-{CH_NO:03d}"
PDF = "/dev/null-stubbed.pdf"


def _noise_webp(path, seed=42, w=220, h=220):
    from PIL import Image
    rnd = random.Random(seed)
    im = Image.frombytes("RGB", (w, h),
                         bytes(rnd.getrandbits(8) for _ in range(w * h * 3)))
    im.save(path, "WEBP", quality=95)


def _full_rec(qn, fig=True):
    return {"q_no": qn, "question_text": f"Stem of question {qn}?",
            "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
            "correct_option": "A", "solution_text": f"Solution text of q{qn}.",
            "tables": [], "has_figure_in_question": fig,
            "has_figure_in_solution": False}


class AuditEnv(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="auditfix_case_"))
        self._saved = (qp.ASSETS_DIR, qp.DATA_DIR, qp.STATE_FILE)
        qp.ASSETS_DIR = self.root / "assets"
        qp.DATA_DIR = self.root / "data"
        qp.STATE_FILE = self.root / "state.json"
        (qp.ASSETS_DIR / "questions" / SUBJECT).mkdir(parents=True)
        qp.DATA_DIR.mkdir(parents=True)
        qp._declared_image_allowance.clear()
        qp._OCR_ANCHOR_CACHE.clear()
        qp._OCR_ANCHOR_XY.clear()
        # union_block_headers_on_page caches per (pdf, page, dpi, section,
        # rec_sig); a previous test's stubbed scan for the SAME key would
        # otherwise be replayed here (order-dependent failures).
        qp._UNION_HEADER_CACHE.clear()
        qp._UNION_DROP_LOGGED.clear()
        self._patches = {}

    def tearDown(self):
        qp.ASSETS_DIR, qp.DATA_DIR, qp.STATE_FILE = self._saved
        for name, orig in self._patches.items():
            setattr(qp, name, orig)
        shutil.rmtree(self.root, ignore_errors=True)

    def stub(self, name, func):
        self._patches.setdefault(name, getattr(qp, name))
        setattr(qp, name, func)

    def make_temp_images(self, page, oids, seed=42):
        rels = []
        for oid in oids:
            fname = f"{SUBJECT}-p{page}-{oid}.webp"
            _noise_webp(qp.ASSETS_DIR / "questions" / SUBJECT / fname,
                        seed=seed + oid)
            rels.append(f"{SUBJECT}/{fname}")
        return rels

    def ledger(self):
        p = qp.DATA_DIR / "image_ownership.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ============================================================================
# FIX A1/A2: partial text-layer (only "6." decodes), OCR fills "7."/"8."
# -> each figure attaches to its TRUE owner, and every claim is ledgered.
# ============================================================================
class TestCh28Fixed(AuditEnv):
    def test_each_figure_goes_to_its_true_owner(self):
        records = {6: _full_rec(6), 7: _full_rec(7), 8: _full_rec(8)}
        # broken text layer: only Q6's heading decodes (as on the book)
        self.stub("_page_word_lines", lambda pdf, page: [(800.0, [(50.0, "6.")])])
        # OCR of the rendered page reads the headings the text layer lost
        self.stub("_ocr_anchors_for_page", lambda pdf, page, dpi=150: [
            ("question", 7, 600.0), ("question", 8, 300.0)])
        self.stub("image_positions_on_page", lambda pdf, page: {
            101: (700.0, 100.0, 0, 200.0, 90.0),
            102: (450.0, 100.0, 1, 200.0, 90.0),
            103: (150.0, 100.0, 2, 200.0, 90.0)})
        imgs = self.make_temp_images(482, [101, 102, 103])
        by_q = {}
        leftover = qp.claim_page_images(imgs, PDF, 482, SUBJECT, CH_NO,
                                        records, by_q, active_block=None)
        got = {q: len((by_q.get(q) or {}).get("question", [])) for q in (6, 7, 8)}
        self.assertEqual(got, {6: 1, 7: 1, 8: 1},
                         f"union anchors must split the figures per owner: {got}")
        self.assertEqual(leftover, [])
        # provenance: EVERY L1 claim row now exists with page + final file
        rows = [r for r in self.ledger() if r.get("outcome") == "claimed"]
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(r["page"], 482)
            self.assertTrue(r.get("final_file", "").endswith(".webp"))
            self.assertEqual(r["method"], "positional")

        # and the gate is quiet when anchors + pages agree
        anch = {6: {482}, 7: {482}, 8: {482}}
        owner_map = qp._ownership_page_map(CH_ID)
        v = qp._export_gate_violations(records, by_q, [], CH_ID, [], [],
                                       anchor_pages=anch,
                                       ownership_pages=owner_map)
        self.assertFalse(any(x[0].startswith("figure_page_mismatch") or
                             x[0].startswith("missing_declared_figure")
                             for x in v), v)


class TestCh28GateCatchesWrongOwner(AuditEnv):
    def test_gate_fires_when_image_page_is_far_from_anchor(self):
        records = {6: _full_rec(6), 7: _full_rec(7)}
        # claim an image for q6 (legit)
        self.stub("_page_word_lines", lambda pdf, page: [(800.0, [(50.0, "6.")])])
        self.stub("_ocr_anchors_for_page", lambda pdf, page, dpi=150: [])
        self.stub("image_positions_on_page", lambda pdf, page: {
            101: (700.0, 100.0, 0, 200.0, 90.0)})
        imgs = self.make_temp_images(482, [101])
        by_q = {}
        qp.claim_page_images(imgs, PDF, 482, SUBJECT, CH_NO, records, by_q)
        self.assertEqual(len(by_q[6]["question"]), 1)

        # q7 declares a figure but got none. With NO unclaimed image near its
        # anchor page this is advisory-only (model over-declaration noise);
        # with one it is a REAL gap and must block the export.
        anch = {6: {"pages": {482}, "question": {482}, "solution": set()},
                7: {"pages": {485}, "question": {485}, "solution": set()}}
        v = qp._export_gate_violations(records, by_q, [], CH_ID, [], [],
                                       anchor_pages=anch,
                                       ownership_pages=qp._ownership_page_map(CH_ID))
        self.assertNotIn("missing_declared_figure_question", [x[0] for x in v],
                         "declared-only noise must not block a clean export")
        adv = qp.DATA_DIR / "export_gate_advisory.jsonl"
        self.assertTrue(adv.exists() and
                        any("q_no\": 7" in l or '"q_no": 7' in l
                            for l in adv.read_text().splitlines()),
                        "weak declarations must stay visible as advisories")

        unres = [{"subject": SUBJECT, "chapter_id": CH_ID, "page": 485,
                  "file": "OPH/OPH-p485-999.webp", "method": "all_levels_failed"}]
        v = qp._export_gate_violations(records, by_q, [], CH_ID, unres, [],
                                       anchor_pages=anch,
                                       ownership_pages=qp._ownership_page_map(CH_ID))
        kinds = [x[0] for x in v]
        self.assertIn("missing_declared_figure_question", kinds,
                      "an unclaimed figure on q7's anchor page must trip the gate")

        # now forge a wrong-owner state: an image claimed from a page far
        # outside q6's anchor neighbourhood
        rows = self.ledger()
        for r in rows:
            if r.get("outcome") == "claimed":
                r["page"] = 499     # pretend it was harvested 17 pages away
        (qp.DATA_DIR / "image_ownership.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
        v = qp._export_gate_violations(records, by_q, [], CH_ID, [], [],
                                       anchor_pages=anch,
                                       ownership_pages=qp._ownership_page_map(CH_ID))
        kinds = [x[0] for x in v]
        self.assertIn("figure_page_mismatch", kinds,
                        "an image attached 17 pages from its anchor must trip "
                        "the gate")


# ============================================================================
# FIX A2: record missing at claim time -> figure waits for the second pass
# instead of gluing onto the previous question.
# ============================================================================
class TestMissingRecordNoSteal(AuditEnv):
    def test_figure_waits_instead_of_stealing(self):
        records = {6: _full_rec(6), 8: _full_rec(8)}        # q7 missing (yet)
        self.stub("_page_word_lines", lambda pdf, page: [
            (800.0, [(50.0, "6.")]), (600.0, [(50.0, "7.")]),
            (300.0, [(50.0, "8.")])])
        self.stub("_ocr_anchors_for_page", lambda pdf, page, dpi=150: [])
        self.stub("image_positions_on_page", lambda pdf, page: {
            101: (700.0, 100.0, 0, 200.0, 90.0),
            102: (450.0, 100.0, 1, 200.0, 90.0),
            103: (150.0, 100.0, 2, 200.0, 90.0)})
        imgs = self.make_temp_images(482, [101, 102, 103])
        by_q = {}
        leftover = qp.claim_page_images(imgs, PDF, 482, SUBJECT, CH_NO,
                                        records, by_q)
        got = {q: len((by_q.get(q) or {}).get("question", [])) for q in (6, 7, 8)}
        self.assertEqual(got, {6: 1, 7: 0, 8: 1},
                         "q7's figure must stay UNCLAIMED (not attach to q6) "
                         "while q7's record is absent")
        self.assertEqual(leftover, ["OPH/OPH-p482-102.webp"])
        # chapter-end second pass (records now complete) resolves it to q7:
        records[7] = _full_rec(7)
        leftover2 = qp.claim_page_images(leftover, PDF, 482, SUBJECT, CH_NO,
                                         records, by_q)
        self.assertEqual(leftover2, [])
        self.assertEqual(len(by_q[7]["question"]), 1)
        self.assertEqual(len(by_q[6]["question"]), 1)   # q6 untouched


# ============================================================================
# FIX: one-to-one requires block-extent evidence (no more greedy swallow)
# ============================================================================
class TestOneToOneExtent(AuditEnv):
    def test_only_in_extent_images_attach(self):
        records = {6: _full_rec(6), 7: _full_rec(7), 9: _full_rec(9)}
        self.stub("_page_word_lines", lambda pdf, page: [])   # L1 finds nothing
        self.stub("_ocr_anchors_for_page", lambda pdf, page, dpi=150: [
            ("question", 6, 800.0), ("question", 9, 200.0)])
        self.stub("qns_printed_on_page", lambda pdf, page, recs: [6])
        records[7]["has_figure_in_question"] = True   # needy but not printed
        self.stub("image_positions_on_page", lambda pdf, page: {
            101: (700.0, 100.0, 0, 200.0, 90.0),   # inside q6 extent (200..800)
            102: (450.0, 100.0, 1, 200.0, 90.0),   # inside
            103: (850.0, 100.0, 2, 200.0, 90.0)})  # ABOVE q6's heading (a
                                                   # continuation figure) ->
                                                   # outside the extent
        imgs = self.make_temp_images(482, [101, 102, 103])
        by_q = {6: {"question": [], "solution": []},
                7: {"question": [], "solution": []},
                9: {"question": [], "solution": []}}

        leftover = qp.claim_page_images(imgs, PDF, 482, SUBJECT, CH_NO,
                                        records, by_q)
        self.assertEqual(len(by_q[6]["question"]), 2,
                         "only the two images inside q6's block extent may attach")
        self.assertEqual(leftover, ["OPH/OPH-p482-103.webp"],
                         "the image outside the extent must stay for explicit "
                         "attribution, not be swallowed by the lone slot")


# ============================================================================
# FIX A3: carry recency + kind correctness + flat-cap parity
# ============================================================================
class TestCarryFixed(AuditEnv):

    def test_carry_obeys_flat_cap(self):
        records = {6: _full_rec(6)}
        self.stub("_page_word_lines", lambda pdf, page: [])
        self.stub("_ocr_anchors_for_page", lambda pdf, page, dpi=150: [])
        self.stub("image_positions_on_page", lambda pdf, page: {
            oid: (700.0 - i * 100.0, 100.0, i, 200.0, 90.0)
            for i, oid in enumerate([101, 102, 103, 104])})
        imgs = self.make_temp_images(483, [101, 102, 103, 104])
        by_q = {}
        leftover = qp.claim_page_images(imgs, PDF, 483, SUBJECT, CH_NO,
                                        records, by_q,
                                        active_block=("question", 6))
        self.assertEqual(len(by_q[6]["question"]), 4,
                         "no hard image-count cap: all geometrically owned "
                         "figures ship")
        self.assertEqual(leftover, [])
        refused = [r for r in self.ledger() if r.get("outcome") == "refused_cap"]
        self.assertEqual(len(refused), 0)


# ============================================================================
# FIX B1: mixed question/solution page -- list items are not question anchors
# ============================================================================
class TestMixedPageFixed(AuditEnv):
    def test_solution_figure_stays_on_solution_side(self):
        records = {3: _full_rec(3), 4: _full_rec(4), 10: _full_rec(10)}
        self.stub("_page_word_lines", lambda pdf, page: [
            (800.0, [(50.0, "10.")]),
            (600.0, [(50.0, "Solution to Question 3:")]),
            (400.0, [(50.0, "3.")]),                      # list item in sol-3
            (200.0, [(50.0, "Solution to Question 4:")])])
        self.stub("_ocr_anchors_for_page", lambda pdf, page, dpi=150: [])
        self.stub("image_positions_on_page", lambda pdf, page: {
            101: (300.0, 100.0, 0, 200.0, 80.0)})
        imgs = self.make_temp_images(500, [101])
        by_q = {}
        qp.claim_page_images(imgs, PDF, 500, SUBJECT, CH_NO, records, by_q)
        self.assertEqual((by_q.get(3) or {}).get("question", []), [],
                         "the '3.' list item must NOT become a question anchor")
        self.assertEqual(len(by_q[3]["solution"]), 1,
                         "the figure inside solution q3's block belongs to the "
                         "solution side of q3")


# ============================================================================
# FIX B4: Form-XObject geometry + extraction (real synthesized PDF)
# ============================================================================
class TestFormXObjectFixed(AuditEnv):
    def _build_pdf(self, path):
        from pypdf import PdfWriter
        from pypdf.generic import (DecodedStreamObject, NameObject,
                                   NumberObject, ArrayObject, DictionaryObject)
        w = PdfWriter()
        page = w.add_blank_page(width=612, height=792)
        raw = bytes([200, 30, 30] * (100 * 100))
        img = DecodedStreamObject()
        img.set_data(raw)
        img.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(100),
            NameObject("/Height"): NumberObject(100),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8)})
        img_ref = w._add_object(img)
        form = DecodedStreamObject()
        form.set_data(b"q 100 0 0 100 50 50 cm /Im1 Do Q\n")
        form.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(0)] * 4),
            NameObject("/Resources"): DictionaryObject({
                NameObject("/XObject"): DictionaryObject({
                    NameObject("/Im1"): img_ref})})})
        form_ref = w._add_object(form)
        content = DecodedStreamObject()
        content.set_data(b"q 1 0 0 1 100 400 cm /Fm1 Do Q\n")
        content_ref = w._add_object(content)
        page[NameObject("/Contents")] = content_ref
        res = page["/Resources"]
        res[NameObject("/XObject")] = DictionaryObject({
            NameObject("/Fm1"): form_ref})
        with open(path, "wb") as fh:
            w.write(fh)

    def test_form_ctm_threaded_and_extracted(self):
        pdf = self.root / "form.pdf"
        self._build_pdf(str(pdf))
        pos = qp._image_positions_raw(str(pdf), 1)
        self.assertTrue(pos)
        (_y, _x, _d, _w, _h), = pos.values()
        self.assertAlmostEqual(_y, 450.0, delta=1.0,
                               msg="form matrix must compose onto the CTM")
        self.assertAlmostEqual(_x, 150.0, delta=1.0)
        saved = qp.extract_real_images(str(pdf), 1, frozenset(), SUBJECT,
                                       qp.ASSETS_DIR / "questions")
        self.assertEqual(len(saved), 1,
                         "form-wrapped figures must now be extracted")


# ============================================================================
# FIX B5: extraction-time byte dedupe (recovery / re-run safety)
# ============================================================================
class TestExtractionDedupe(AuditEnv):
    def test_owned_figure_is_not_reextracted(self):
        pdf = self.root / "one.pdf"
        TestFormXObjectFixed._build_pdf(self, str(pdf))
        first = qp.extract_real_images(str(pdf), 1, frozenset(), SUBJECT,
                                       qp.ASSETS_DIR / "questions")
        self.assertEqual(len(first), 1)
        # pretend claim moved temp -> final name
        src = qp.ASSETS_DIR / "questions" / first[0]
        final = qp.ASSETS_DIR / "questions" / SUBJECT / f"{CH_ID}_Q_01.webp"
        src.rename(final)
        hashes = qp.hash_owned_image_files(qp.ASSETS_DIR / "questions" / SUBJECT)
        second = qp.extract_real_images(str(pdf), 1, frozenset(), SUBJECT,
                                        qp.ASSETS_DIR / "questions",
                                        skip_hashes=hashes)
        self.assertEqual(second, [],
                         "identical figure bytes already owned -> no duplicate "
                         "extraction on re-run/recovery")


# ============================================================================
# FIX C-4: 4-letter subject codes no longer lose images at export
# ============================================================================
class TestSubjectLengthFixed(AuditEnv):
    def test_four_letter_subject_keeps_images(self):
        self.assertIsNotNone(
            qp.IMG_PATH_RE.match("MEDS/MEDS-028-006_Q_01.webp"))
        self.assertIsNotNone(
            qp.IMG_PATH_RE.match("PSY/PSY-001-014_SOL_02.webp"))
        self.assertIsNotNone(
            qp.IMG_PATH_RE.match("OPH/OPH-028-006_OPT_B_01.webp"))




# ============================================================================
# FIX (phantom anchors): texture/watermark OCR phantoms rejected at the union
# ============================================================================
class TestPhantomAnchorFilter(AuditEnv):
    def test_inside_figure_phantom_rejected(self):
        records = {i: _full_rec(i) for i in range(1, 24)}
        # text layer prints the real heading q20 (=383); phantom '1.' at
        # y=668 sits INSIDE the drawn figure rect (x 162..450, y 520..736)
        self.stub("_page_word_lines",
                  lambda pdf, page: [(383.0, [(50.0, "Question 20:")])])
        from PIL import Image as _I
        self.stub("render_page_png", lambda pdf, page, dpi=150: (
            _I.new("RGB", (612, 792), "white"), 1.0, 792.0))
        self.stub("ocr_page_anchors_xy",
                  lambda png, scale, h: [("question", 1, 668.0, 181.0, True),
                                         ("question", 20, 387.0, 50.0, True)])
        self.stub("image_positions_on_page", lambda pdf, page: {
            1827: (520.0, 162.0, 0, 288.0, 216.0)})
        # real wrapper runs the in-figure filter (no stubbing it away)
        headers = qp.union_block_headers_on_page(PDF, 617, records)
        self.assertNotIn(("question", 1, 668.0), headers,
                         "phantom inside a figure must never become an anchor")
        self.assertTrue(any(q == 20 for _k, q, _y in headers),
                        "the real q20 heading must survive")

    def test_weak_bare_token_without_text_slice_rejected(self):
        # bare '2.' phantom, NOT inside the figure rect (x far right), and the
        # page HAS a text layer (elsewhere) -> corroboration drops it.
        records = {i: _full_rec(i) for i in range(1, 24)}
        self.stub("_page_word_lines", lambda pdf, page: [
            (341.0, [(50.0, "Solution to Question 11:")]),   # only real line
        ])
        self.stub("_ocr_anchors_for_page", lambda pdf, page, dpi=150: [
            ("question", 2, 564.7),       # phantom (weak)
            ("solution", 11, 341.0)])     # strong real one
        qp._OCR_ANCHOR_XY[("/dev/null-stubbed.pdf", 627, 150)] = [
            ("question", 2, 564.7, 473.3, False),     # weak, outside figure
            ("solution", 11, 341.0, 45.0, True)]
        headers = qp.union_block_headers_on_page(PDF, 627, records)
        self.assertNotIn(("question", 2, 564.7), headers)
        self.assertIn(("solution", 11, 341.0), headers)

    def test_strong_ocr_anchor_survives_without_text_layer(self):
        # scanned-book page: NO text layer at all -> anchors stand (best
        # available evidence); corroboration must not nuke real headings.
        records = {6: _full_rec(6)}
        self.stub("_page_word_lines", lambda pdf, page: [])
        self.stub("_ocr_anchors_for_page", lambda pdf, page, dpi=150: [
            ("question", 6, 700.0)])
        headers = qp.union_block_headers_on_page(PDF, 99, records)
        self.assertEqual(headers, [("question", 6, 700.0)])

# ============================================================================
# LIVE-RUN regression (ch. 25 p571-573): bare-N numbered list items inside
# the SOLUTIONS section must not anchor figures onto the question side.
# ============================================================================
class TestSSectionBareListFilter(AuditEnv):
    def test_bare_number_list_items_dropped_in_s_section(self):
        records = {i: _full_rec(i) for i in range(1, 26)}
        # text layer: real "Solution to Question 12:" + bare numbered list
        # items "2. Vitreomacular traction ..." and "3. Vitreomacular ..."
        self.stub("_page_word_lines", lambda pdf, page: [
            (700.0, [(50.0, "Solution to Question 12:")]),
            (500.0, [(50.0, "2."), (70.0, "Vitreomacular traction...")]),
            (200.0, [(50.0, "3."), (70.0, "Vitreomacular adhesion...")])])
        self.stub("ocr_page_anchors_xy", lambda png, scale, h: [
            ("question", 4, 620.0, 172.0, 0)])     # bare OCR anchor, no keyword
        hdrs = qp.union_block_headers_on_page(PDF, 571, records, section="S")
        self.assertFalse(any(k == "question" for k, _q, _y in hdrs),
                         f"bare list items must not anchor in S section: {hdrs}")
        self.assertIn(("solution", 12, 700.0), hdrs)

    def test_keyword_question_heading_survives_in_s_section(self):
        records = {i: _full_rec(i) for i in range(1, 26)}
        self.stub("_page_word_lines", lambda pdf, page: [
            (700.0, [(50.0, "Question 13:")]),
            (500.0, [(50.0, "2."), (70.0, "list item")])])
        self.stub("ocr_page_anchors_xy", lambda png, scale, h: [])
        hdrs = qp.union_block_headers_on_page(PDF, 571, records, section="S")
        self.assertIn(("question", 13, 700.0), hdrs,
                      "an explicit 'Question N:' heading stays (the run-18 "
                      "activation case depends on it)")
        self.assertNotIn(("question", 2, 500.0), hdrs)

    def test_bare_question_heading_survives_in_q_section(self):
        records = {6: _full_rec(6)}
        self.stub("_page_word_lines", lambda pdf, page: [
            (800.0, [(50.0, "6."), (70.0, "Some stem text")])])
        self.stub("ocr_page_anchors_xy", lambda png, scale, h: [])
        hdrs = qp.union_block_headers_on_page(PDF, 607, records, section="Q")
        self.assertIn(("question", 6, 800.0), hdrs,
                      "bare-number headings are legitimate on question pages")




# ============================================================================
# STATUS SYSTEM (user ask): anything not provable must be flagged, never
# silently COMPLETE. build_final_question computes qa_status from the record,
# the ownership ledger (method/confidence per image) and gate notices.
# ============================================================================
class TestQAStatusRollup(AuditEnv):
    def _healthy_rec(self, qn=4):
        return {"q_no": qn, "question_text": f"Stem text of q{qn}?",
                "options": {"A": "alpha", "B": "beta", "C": "gamma",
                            "D": "delta"},
                "correct_option": "C",
                "solution_text": f"Solution text of q{qn} ending properly.",
                "tables": [], "has_figure_in_question": False,
                "has_figure_in_solution": False}

    def _mk_image(self, name):
        (qp.ASSETS_DIR / "questions" / SUBJECT).mkdir(parents=True, exist_ok=True)
        _noise_webp(qp.ASSETS_DIR / "questions" / SUBJECT / name, seed=len(name))

    def test_complete_row_is_ready(self):
        row = qp.build_final_question(SUBJECT, CH_ID, CH_NO, 4,
                                      self._healthy_rec(), {"question": [], "solution": []})
        self.assertEqual(row["qa_status"], "READY")
        self.assertEqual(row["qa_reasons"], [])
        self.assertFalse(row["manual_review"])

    def test_missing_answer_is_incomplete_not_ready(self):
        rec = self._healthy_rec()
        rec["correct_option"] = None
        row = qp.build_final_question(SUBJECT, CH_ID, CH_NO, 4, rec,
                                      {"question": [], "solution": []})
        self.assertEqual(row["qa_status"], "INCOMPLETE")
        self.assertTrue(any("correct_option" in r for r in row["qa_reasons"]))
        self.assertTrue(row["manual_review"])

    def test_model_only_image_marks_review_needed(self):
        self._mk_image(f"{CH_ID}-004_Q_01.webp")
        # ledger: image claimed only by isolated_crop_vision
        qp._record_image_ownership(SUBJECT, CH_ID, 482, f"{SUBJECT}/OPH-p482-101.webp",
                                   "OPH-028-004", "question",
                                   "isolated_crop_vision", "model crop verdict",
                                   confidence="high", outcome="claimed",
                                   final_file=f"OPH/OPH-028-004_Q_01.webp")
        row = qp.build_final_question(SUBJECT, CH_ID, CH_NO, 4,
                                      self._healthy_rec(),
                                      {"question": [f"OPH/OPH-028-004_Q_01.webp"],
                                       "solution": []})
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        self.assertTrue(any("model-only" in r for r in row["qa_reasons"]))

    def test_positional_image_stays_ready(self):
        self._mk_image(f"{CH_ID}-004_Q_02.webp")
        qp._record_image_ownership(SUBJECT, CH_ID, 482, f"{SUBJECT}/OPH-p482-102.webp",
                                   "OPH-028-004", "question",
                                   "positional", "closest heading above image",
                                   confidence="high", outcome="claimed",
                                   final_file=f"OPH/OPH-028-004_Q_02.webp")
        row = qp.build_final_question(SUBJECT, CH_ID, CH_NO, 4,
                                      self._healthy_rec(),
                                      {"question": [f"OPH/OPH-028-004_Q_02.webp"],
                                       "solution": []})
        self.assertEqual(row["qa_status"], "READY")

    def test_gate_notice_marks_review_needed(self):
        row = qp.build_final_question(SUBJECT, CH_ID, CH_NO, 4,
                                      self._healthy_rec(),
                                      {"question": [], "solution": []},
                                      gate_notices=[("figure_page_mismatch",
                                                     "image from p99, anchors [482]")])
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        self.assertTrue(any("figure_page_mismatch" in r for r in row["qa_reasons"]))

    def test_answer_vs_solution_letter_flip_is_flagged_never_autocorrected(self):
        rec = self._healthy_rec()
        rec["correct_option"] = "B"           # false flip
        # solution opens on option C's content
        rec["solution_text"] = ("Gamma ray bursts are the brightest. " + rec["solution_text"])
        rec["options"]["C"] = "gamma ray bursts are the brightest explosions"
        row = qp.build_final_question(SUBJECT, CH_ID, CH_NO, 4, rec,
                                      {"question": [], "solution": []})
        self.assertEqual(row["correct_options"], ["B"],
                         "the mismatch is NEVER auto-corrected")
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        self.assertTrue(any("answer_suspect" in r for r in row["qa_reasons"]))




# ============================================================================
# MULTI-DRAW SHARING (OBGYN ed8 p54/p63 user report): one image object drawn
# twice on a page (a continuation copy + a copy under the next printed
# heading) must attach BOTH usages -- not silently keep only the last one.
# ============================================================================
class TestMultiDrawSharing(AuditEnv):
    def _pdf_two_draws(self, path):
        from pypdf import PdfWriter
        from pypdf.generic import (DecodedStreamObject, NameObject,
                                   NumberObject, DictionaryObject)
        import random as _r
        w = PdfWriter()
        page = w.add_blank_page(width=612, height=792)
        rnd = _r.Random(7)
        raw = bytes(rnd.getrandbits(8) for _ in range(110 * 110 * 3))
        img = DecodedStreamObject(); img.set_data(raw)
        img.update({NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(110),
                    NameObject("/Height"): NumberObject(110),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8)})
        ref = w._add_object(img)
        content = DecodedStreamObject()
        content.set_data(
            b"BT /F1 14 Tf 162 740 Td (Question 12:) Tj ET\n"
            b"q 220 0 0 110 196 500 cm /Im1 Do Q\n"
            b"BT /F1 14 Tf 162 400 Td (Question 13:) Tj ET\n"
            b"q 220 0 0 110 196 150 cm /Im1 Do Q\n")
        cref = w._add_object(content)
        page[NameObject("/Contents")] = cref
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): ref}),
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica")})})})
        with open(path, "wb") as fh:
            w.write(fh)

    def test_both_printings_are_owned(self):
        pdf = self.root / "dup.pdf"
        self._pdf_two_draws(pdf)
        saved = qp.extract_real_images(str(pdf), 1, frozenset(), SUBJECT,
                                       qp.ASSETS_DIR / "questions")
        self.assertEqual(len(saved), 1,
                         "same object on one page = one extracted file")
        pos = qp._image_positions_raw(str(pdf), 1)
        alias = [k for k in pos if isinstance(k, str) and "@d" in k]
        self.assertTrue(alias, "extra draws must surface as alias keys")

        recs = {12: _full_rec(12), 13: _full_rec(13)}
        by_q = {}
        leftover = qp.claim_page_images(saved, str(pdf), 1, SUBJECT, CH_NO,
                                        recs, by_q, active_block=None)
        self.assertEqual(leftover, [])
        q12 = (by_q.get(12) or {}).get("question", [])
        q13 = (by_q.get(13) or {}).get("question", [])
        self.assertTrue(q12 and q13, f"both owners must hold the shared ref: "
                                     f"q12={q12} q13={q13}")
        self.assertEqual(q12[0], q13[0],
                         "sharing = same file reference under both owners")
        outs = [r["outcome"] for r in self.ledger()]
        self.assertIn("claimed", outs)
        self.assertIn("shared", outs)
        self.assertTrue(any(r["method"] == "multi_draw_geometry"
                            for r in self.ledger()))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTableDedupeNearVariants(unittest.TestCase):
    """User report (production screenshot, OBG-012-017): 3 near-identical
    tables on one question, 2 with spacing drift ('at4 cmof' vs 'at 4 cm of').
    Root cause: recover_orphans deduped by EXACT markdown; the same table
    arriving per-pass with spacing drift survived N times."""

    def test_spacing_variants_collapse_to_one(self):
        base = ("| WHO modified partograph | WHO Labour Care Guide |\n|---|---|\n"
                "| Labour progression begins at 4 cm. | Begins at 5 cm. |")
        drift = base.replace("at 4 cm", "at4 cm").replace("Begins at 5", "Begins at5")
        out = qp._dedupe_tables([{"type": "t", "markdown": base},
                                 {"type": "t", "markdown": drift}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["markdown"], base)   # fuller/cleaner wins

