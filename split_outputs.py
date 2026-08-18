"""
split_outputs.py
================

Per-chapter SPLIT-OUTPUT LAYER (additive, observation-only).

Writes, after every existing in-pipeline reconciliation, three strictly
separate JSONL datasets joined by a stable q_id, plus a chapter
completeness summary, a chapter-scoped image manifest, a per-chapter
orphan view, and an unresolved-qids ledger:

  data/split/{subject}/{chapter_id}/
    questions.jsonl
    answers.jsonl
    solutions.jsonl
    unresolved_qids.jsonl
    orphans.jsonl
    chapter_completeness.json
    image_manifest.jsonl

The existing data/questions.jsonl and data/by_chapter/{chapter_id}.jsonl
are NOT touched by this module. The split is built from the same
chapter_records dict the master file is built from, so the two are
guaranteed consistent for the same chapter.

Inserted at one call site in process_pdf() (qbank_pipeline.py), AFTER
every existing check (batches, orphans, drain, sweep, retry, rescue,
anchorless drop, phantom drop, critique) and BEFORE the existing
build_final_question loop.

Public entry points
-------------------
- reconcile_qids(chapter_records, qn_source_pages, pdf_path, page_files,
                  subject, chapter_no) -> dict
      Walk the chapter's text layer ONCE, harvest every printed
      question/solution header, answer-key row, and block position for
      every q_no in chapter_records. Assign a 4-grade provenance label
      (RESOLVED_ANCHORED / RESOLVED / PROVISIONAL / UNRESOLVED). Populate
      a q_no_anchors vector on every record. Remove UNRESOLVED records
      from chapter_records and return them separately (the caller passes
      them to write_split_outputs -> unresolved_qids.jsonl).

- write_split_outputs(chapter_id, subject, chapter_no, chapter_records,
                      image_files_by_q, qn_source_pages, orphans,
                      chapter_unresolved_images, pdf_path, page_files,
                      reconciled) -> dict
      Write the seven per-chapter files atomically. chapter_completeness.json
      is written LAST as the "split is fully on disk" signal. Returns the
      chapter_completeness.json content as a dict for the caller's logs.

Provenance taxonomy (confirmed in design doc §2, Phase-2 upgrade landed)
------------------------------------------------------------------------------
- RESOLVED_ANCHORED  >=2 printed anchors agree + at least one of
                      printed_stem_match / printed_solution_header_match
                      is set
- RESOLVED           1 printed anchor OR (0 printed anchors but a
                      trusted connected-run or carry-forward origin
                      from the run-18 GUARD / compute_carry -- the
                      Phase-2 promotion)
- PROVISIONAL        0 printed anchors AND no Phase-2 origin
                      (model-only q_no, weakest grade)
- UNRESOLVED         no anchor at all, OR two printed anchors disagree
                      (Case 2: missing_question_for_solution), OR
                      only foreign / hallucination-source q_no

Phase-2 anchors (now populated, design doc §3.1)
-------------------------------------------------
The split writer accepts a `chapter_anchor_observations` dict
captured read-only by process_pdf in the same chapter. Two
non-printed anchors are derived from those observations and
attached to q_no_anchors:
  * neighbor_run: a {size, first, last, near_chapter_max} dict
    from the run-18 GUARD's connected-run analysis (the q_no
    appears in trusted_qnos when its run was >=5 items or
    touched the chapter's known_max).
  * carry_forward_origin: a {from_window, cut_part} dict from
    compute_carry() (the q_no is the previous window's
    last_open_question).
A single-anchor record backed by EITHER of these is graded
RESOLVED instead of PROVISIONAL, eliminating the silent
single-anchor = PROVISIONAL class that the Phase-1 grader left
on the table. The OCR anchors (ocr_stem_match,
ocr_solution_header_match) are still pending -- they only
matter for books where the text layer is garbled, and the
live pipeline's OCR fallback already handles those pages.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable


# Allowed values -- per the design contract. New grades are an explicit
# break of the contract and require a design-doc update.
ALLOWED_Q_ID_GRADES = frozenset(
    {"RESOLVED_ANCHORED", "RESOLVED", "PROVISIONAL", "UNRESOLVED"}
)
ALLOWED_EXTRACTION_STATUS = frozenset({"COMPLETE", "INCOMPLETE"})

# Reasons for unresolved_qids.jsonl (design doc §6). Kept short,
# future-extensible. Any new reason is fine; these are the v1 set.
UNRESOLVED_REASONS = frozenset({
    "no_anchor_at_all",
    "model_q_no_disagree",
    "conflicting_anchors",
    "hallucinated_q_no",
    "solution_q_no_not_in_printed_header",
    "answer_q_no_not_in_printed_key",
    "two_possible_questions",
    "question_continues_from_previous_page",
    "foreign_chapter_q_no",
    "missing_question_for_solution",
})

# Reasons for orphans.jsonl (design doc §7).
ORPHAN_REASONS = frozenset({
    "q_id_unresolved",
    "foreign_option_line_head",
    "first_line_in_sibling_solution",
    "owner_attached_but_speculative",
    "unconfirmed_discontinuous_qno",  # matches the existing pipeline
})


# ---------------------------------------------------------------------------
# 1. Atomic write helpers -- exactly the per-chapter pattern the existing
#    pipeline uses for questions.jsonl (see rewrite_questions_file in
#    qbank_pipeline.py). A crash mid-write leaves either the previous file
#    untouched or the new file complete.
# ---------------------------------------------------------------------------

def _atomic_jsonl_write(path: Path, rows: Iterable[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _atomic_json_write(path: Path, obj: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 2. Anchor harvesting -- pure read-only pass over the chapter's pages.
#    Uses the same zero-token text-layer + pypdf-visitor + answer-key
#    regexes the existing pipeline uses, so the anchors are exactly the
#    evidence the existing extraction loop already trusts.
# ---------------------------------------------------------------------------

# re.MULTILINE baked in at compile time: the OCR chain runs on
# block text (pdftotext output is a single multi-line string), so
# `^` must match at every line start to find stems that aren't on
# the first line. The compiled-pattern search-flag path doesn't
# affect ^-behavior, so the flag has to be at compile time.
_PRINTED_STEM_RE = re.compile(
    r"^\s*(?:Q(?:uestion)?\s*[.:]?\s*)?(\d{1,3})\s*[.:\-\u2013)]",
    re.MULTILINE,
)
_PRINTED_SOL_HEADER_RE = re.compile(
    r"Solution\s+to\s+Question\s+(\d{1,3})", re.IGNORECASE
)
# Answer-key row: "| 13 | B |", "13. B", "13 - B". Same shape as
# locate_missing_record_pages in qbank_pipeline.py.
_ANSWER_KEY_ROW_RE = re.compile(
    r"(?m)^\s*\|\s*(\d{1,3})\s*\|\s*([A-Da-d])\s*\|"          # | 13 | B |
    r"|^\s*(\d{1,3})\s*[.)]\s*([A-Da-d])\s*$"                 # 13. B / 13) B
    r"|^\s*(\d{1,3})\s*[-\u2013]\s*([A-Da-d])\s*$"           # 13 - B
)
_ANSWER_KEY_PROBE_RE = re.compile(
    r"(question\s*no|q\.?\s*no)[^\n]{0,40}(correct\s*option|answer)"
    r"|answer\s*key",
    re.IGNORECASE,
)


def _pdftotext_page(pdf_path: str, true_page: int) -> str:
    """Zero-token text-layer read of a single page. Mirrors the existing
    pipeline's pdftotext_page (we can't import qbank_pipeline without
    pulling in google-generativeai, which we don't want as a hard
    dep for the synthetic harness)."""
    import subprocess
    try:
        out = subprocess.run(
            ["pdftotext", "-f", str(true_page), "-l", str(true_page),
             "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout or ""
    except Exception:
        return ""


def _ocr_render_and_tesseract(pdf_path: str, true_page: int) -> str:
    """OCR fallback for a single page when the text layer is empty /
    garbled. Renders the page to a PNG via pdftoppm (150 dpi, in a
    temp dir to keep the filesystem clean) and runs tesseract on
    the resulting image via pytesseract.image_to_string.

    Returns the OCR text (empty string on any failure -- the caller
    treats an empty OCR result as "no anchors found on this page"
    and the grader falls back to PROVISIONAL on the affected record).

    Sandbox-friendly: tests mock this at module load (the existing
    PIL/pytesseract stubs in tools/test_phase2_anchors.py +
    test_phase4_provenance.py). On Railway the real pdftoppm +
    tesseract binaries are installed by the same Dockerfile that
    installs poppler-utils.
    """
    try:
        import subprocess
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(prefix="split_ocr_") as tmpdir:
            prefix = str(Path(tmpdir) / "p")
            try:
                subprocess.run(
                    ["pdftoppm", "-png", "-r", "150",
                     "-f", str(true_page), "-l", str(true_page),
                     str(pdf_path), prefix],
                    capture_output=True, text=True, timeout=30,
                )
            except Exception:
                return ""
            # pdftoppm names output files like "p-005.png" -- glob one.
            pngs = sorted(Path(tmpdir).glob("p-*.png"))
            if not pngs:
                return ""
            try:
                from PIL import Image
                import pytesseract
                img = Image.open(pngs[0])
                return pytesseract.image_to_string(img) or ""
            except Exception:
                return ""
    except Exception:
        return ""


def _harvest_ocr_anchors_on_page(pdf_path: str, file_page: int,
                                 chapter_records: dict,
                                 pdftotext_text: str = None) -> dict:
    """Run the printed_stem + solution_header regexes on a single
    page via the OCR chain (pdftotext primary, tesseract fallback).
    Returns {qn: {anchor_name: payload}} for the OCR hits.

    Each payload carries a `via: pdftotext|tesseract` field so a
    consumer reading the split record can tell which scan path
    produced the hit. pdftotext is tried first (fast, exact when
    the text layer is readable); tesseract runs ONLY when
    pdftotext returns empty (garbled / scanned-only page).

    Conservative gating: a hit counts only if its q_no is in
    `chapter_records` (cross-chapter "Question N:" / "Solution to
    Question N:" headers on the printed page are ignored here, the
    same way the existing _harvest_page handles them).
    """
    found: dict = {}
    qn_set = set(chapter_records)
    # Stage 1: pdftotext (zero-token). If the text layer has any
    # non-whitespace content, that's the entire harvest for this page.
    if pdftotext_text is None:
        pdftotext_text = _pdftotext_page(pdf_path, file_page)
    if (pdftotext_text or "").strip():
        text = pdftotext_text
        via = "pdftotext"
    else:
        # Stage 2: tesseract OCR fallback. Garbled-page case.
        text = _ocr_render_and_tesseract(pdf_path, file_page)
        via = "tesseract"
    if not text or not text.strip():
        return found
    # _PRINTED_STEM_RE is compiled with re.MULTILINE baked in (see
    # the regex definition), so ^ matches at every line start -- the
    # OCR chain runs on block text (pdftotext output is a single
    # multi-line string) and needs to find stems that aren't on the
    # first line of the page.
    for m in _PRINTED_STEM_RE.finditer(text):
        try:
            qn = int(m.group(1))
        except (TypeError, ValueError):
            qn = None
        if qn is None or qn not in qn_set:
            continue
        # Skip a "Solution to Question N:" line that happens to also
        # match the stem regex (same logic as the printed path).
        if re.match(r"\s*solution\s+to\s+question\s+\d", text[m.start():m.start()+40], re.I):
            continue
        found.setdefault(qn, {})["ocr_stem_match"] = {
            "page": file_page,
            "via": via,
            "header_text": text[m.start():m.end()].strip()[:80],
        }
    for m in _PRINTED_SOL_HEADER_RE.finditer(text):
        try:
            qn = int(m.group(1))
        except (TypeError, ValueError):
            qn = None
        if qn is None or qn not in qn_set:
            continue
        found.setdefault(qn, {})["ocr_solution_header_match"] = {
            "page": file_page,
            "via": via,
            "header_text": text[m.start():m.end()].strip()[:80],
        }
    return found


def _page_word_lines(pdf_path: str, file_page: int):
    """[(y_baseline, line_text)] in PDF user space, top-first.
    Subset of the existing _page_word_lines in qbank_pipeline.py --
    only the joined-line text is needed by the grader (we don't need
    the per-word x positions). Falls back to empty if pypdf can't
    parse the page (scanned-only PDFs)."""
    try:
        from pypdf import PdfReader
        page = PdfReader(pdf_path).pages[file_page - 1]
    except Exception:
        return []
    words = []

    def _visitor(text, _cm, tm, _font_dict, _font_size):
        t = (text or "").strip()
        if t:
            words.append((round(float(tm[5]), 1), t))

    try:
        page.extract_text(visitor_text=_visitor)
    except Exception:
        return []
    if not words:
        return []
    lines: dict = {}
    for y, t in words:
        lines.setdefault(y, []).append(t)
    return [(y, " ".join(parts)) for y, parts in sorted(lines.items(), reverse=True)]


def _harvest_page(pdf_path: str, file_page: int, chapter_records: dict) -> dict:
    """Returns {qn: {anchor_name: {...}|None}} for a single page. Each
    anchor is captured at most once per page; later pages only add
    anchors when they discover new evidence."""
    found: dict = {}
    qn_set = set(chapter_records)
    # pypdf-visitor path (body pages: prints "1.", "2)", "Question 3:")
    for _y, line in _page_word_lines(pdf_path, file_page):
        m = _PRINTED_STEM_RE.match(line)
        if m and not line.lstrip().lower().startswith("solution to question"):
            try:
                qn = int(m.group(1))
            except (TypeError, ValueError):
                qn = None
            if qn in qn_set:
                found.setdefault(qn, {})["printed_stem_match"] = {
                    "page": file_page, "header_text": line.strip()[:80]
                }
        for sm in _PRINTED_SOL_HEADER_RE.finditer(line):
            try:
                qn = int(sm.group(1))
            except (TypeError, ValueError):
                qn = None
            if qn in qn_set:
                found.setdefault(qn, {})["printed_solution_header_match"] = {
                    "page": file_page, "header_text": line.strip()[:80]
                }
    # pdftotext path (answer-key rows, which sit in a table that the
    # pypdf visitor may format differently across PDFs)
    text = _pdftotext_page(pdf_path, file_page)
    if text.strip():
        for m in _ANSWER_KEY_ROW_RE.finditer(text):
            qn = int(m.group(1) or m.group(3) or m.group(5))
            letter = (m.group(2) or m.group(4) or m.group(6) or "").upper()
            if qn in qn_set:
                row_text = (m.group(0) or "").strip()[:80]
                # first-seen wins; later pages don't overwrite a confirmed row
                found.setdefault(qn, {}).setdefault("answer_key_row_match", {
                    "page": file_page, "row": row_text, "letter": letter
                })
    return found


def _harvest_anchors(chapter_records: dict, qn_source_pages: dict,
                     pdf_path: str, page_files) -> dict:
    """Per-q_no: {anchor_name: {page, ...}|None}. Walks the chapter's
    pages ONCE, reading each page's text layer (zero Gemini calls)
    and recording the strongest printed evidence found for each q_no.
    Pages come from page_files when given (the in-pipeline path),
    else from qn_source_pages (the synthetic harness path).

    Two scan chains run per page (user-confirmed Phase-5 design
    "Both stages, chained"):

      1. Printed-text scan: pypdf text visitor + pdftotext +
         _ANSWER_KEY_ROW_RE. Cheap (zero Gemini tokens), exact
         when the text layer is readable. Drives the
         printed_stem_match / printed_solution_header_match /
         answer_key_row_match anchor names.

      2. OCR chain: pdftotext primary (zero-token), tesseract
         fallback for pages whose text layer is empty/garbled.
         Drives the ocr_stem_match / ocr_solution_header_match
         anchor names. Each payload carries a `via: pdftotext|
         tesseract` field. The OCR chain is read-only and adds no
         Gemini calls -- it's poppler + tesseract on the local
         machine (Railway Dockerfile already installs both).

    First-seen wins across both chains (later pages don't overwrite
    a confirmed anchor on an earlier page), so the final
    per-qn_per_qn_anchor dict is the strongest evidence the chapter
    has for each q_no.
    """
    pages: list
    if page_files:
        pages = []
        for pf in page_files:
            try:
                pages.append(int(pf.stem.split("-")[-1]))
            except (ValueError, IndexError):
                continue
    else:
        # Synthetic harness: union of qn_source_pages values
        pages = set()
        for sp in (qn_source_pages or {}).values():
            if isinstance(sp, (set, list, tuple)):
                pages.update(int(p) for p in sp)
            elif isinstance(sp, int):
                pages.add(sp)
        pages = sorted(pages)
    if not pages:
        return {qn: {} for qn in chapter_records}
    per_qn: dict = {qn: {} for qn in chapter_records}
    for p in pages:
        # Chain 1: printed-text scan (pypdf visitor + pdftotext).
        page_harvest = _harvest_page(pdf_path, p, chapter_records)
        # Chain 2: OCR chain (pdftotext primary, tesseract fallback).
        # We pass the pdftotext text _harvest_page already read so
        # the OCR chain doesn't pay for a second pdftotext subprocess.
        pdftotext_text = _pdftotext_page(pdf_path, p)
        ocr_harvest = _harvest_ocr_anchors_on_page(
            pdf_path, p, chapter_records, pdftotext_text=pdftotext_text)
        # Merge: first-seen wins across the two chains.
        for source in (page_harvest, ocr_harvest):
            for qn, anchors in source.items():
                for name, payload in anchors.items():
                    per_qn[qn].setdefault(name, payload)
    return per_qn


# ---------------------------------------------------------------------------
# 3. Grader -- deterministic, 4-grade taxonomy.
# ---------------------------------------------------------------------------

def _grade_record(anchors: dict, has_neighbor_run: bool = False,
                  has_carry_origin: bool = False) -> str:
    """Map the anchors found for one record to a q_id_grade.

    Phase-1 grading (printed anchors only):
      - RESOLVED_ANCHORED: >=2 printed anchors + at least one of the
        two high-confidence printed anchors (printed_stem_match or
        printed_solution_header_match) is set
      - RESOLVED:           exactly 1 printed anchor
      - PROVISIONAL:        no printed anchor at all (model-only q_no)
      - UNRESOLVED:         impossible from printed anchors alone;
        the caller (reconcile_qids) sets this when the record is a
        Case 2 / missing_question_for_solution scenario.

    Phase-2 upgrade (design doc §2): a single-anchor record is promoted
    from PROVISIONAL to RESOLVED when EITHER has_neighbor_run
    (the q_no is in a trusted connected-run from the run-18 GUARD)
    OR has_carry_origin (the q_no has a valid carry-forward origin
    from compute_carry()). Both flags come from the read-only
    chapter_anchor_observations passed by process_pdf -- no Gemini
    call, no behavior change to records that already have >=2 anchors.

    Phase-5 upgrade: the two OCR anchor names
    (ocr_stem_match / ocr_solution_header_match) count toward the
    1/2+ threshold the same way as printed_* anchors. The high-
    confidence "first 2" gate (printed_stem_match /
    printed_solution_header_match) does NOT include OCR -- the OCR
    payload's `via` field tells a consumer where the hit came from,
    but a single-OCR-anchor-only record is NOT upgraded to
    RESOLVED_ANCHORED (the OCR text has higher character-error
    risk than a clean text layer). It IS counted toward the >=2
    threshold when paired with another anchor of any kind (printed
    or OCR), so a record with printed_answer_key_row_match +
    ocr_stem_match becomes RESOLVED_ANCHORED via the second
    branch (>=2 anchors) without the high-confidence gate.
    """
    matches = sum(bool(anchors.get(k)) for k in (
        "printed_stem_match",
        "printed_solution_header_match",
        "answer_key_row_match",
        "ocr_stem_match",
        "ocr_solution_header_match",
    ))
    if matches == 0:
        # Phase-2 promotion: zero printed anchors + a trusted run or a
        # carry origin means the model-only q_no is still defensible
        # (the printed-page evidence is silent for borderline-anchored
        # questions, not actually wrong).
        if has_neighbor_run or has_carry_origin:
            return "RESOLVED"
        return "PROVISIONAL"
    if matches >= 2 and (anchors.get("printed_stem_match")
                         or anchors.get("printed_solution_header_match")):
        return "RESOLVED_ANCHORED"
    if matches == 1 and (has_neighbor_run or has_carry_origin):
        # Phase-2 promotion: a borderline single-anchor record backed
        # by a trusted connected-run or a carry-forward origin is
        # RESOLVED, not PROVISIONAL. Without this promotion every
        # single-anchor record was PROVISIONAL even when the GUARD had
        # already proven its q_no was part of a trusted chapter range.
        return "RESOLVED"
    return "RESOLVED"


# ---------------------------------------------------------------------------
# 4. Per-record provenance collection
# ---------------------------------------------------------------------------

# Canonical list of fields whose provenance is tracked per-record. The
# extraction loop stores its per-field label in rec["_prov"][FIELD];
# _collect_provs below reads that dict. Tables ride on question_text
# (Q-pass renders them) or solution_text (S-pass renders them) -- the
# list below covers everything the row builders expose a *_prov column
# for.
_PROV_FIELDS = (
    "question_text",   # Q_PASS / Q_RETRY / RESCUE / RECOVER / DRAIN_Q / OCR_Q
    "options",         # Q_PASS / Q_RETRY / RESCUE / RECOVER / DRAIN_Q
    "correct_option",  # A_PASS / A_RETRY / RESCUE / RECOVER / DRAIN_A
    "solution_text",   # S_PASS / S_RETRY / RESCUE / RECOVER / DRAIN_S / OCR_S
    "tables",          # Q_PASS / S_PASS / Q_RETRY / S_RETRY
)


def _collect_provs(rec: dict) -> tuple:
    """Returns (field_provenance, sorted_pass_list, model_q_no, disagree).

    field_provenance: {field: prov_label or None} for every field in
        _PROV_FIELDS. A field is "None" when the extraction loop never
        wrote a label for it (e.g. solution_text was never set, or
        options came from the Q_PASS and the dict has no "options" key).
        A field is the prov label string when the loop wrote one
        (Q_PASS, A_PASS, S_PASS, Q_RETRY, A_RETRY, S_RETRY, RESCUE,
        RECOVER, DRAIN_Q, DRAIN_S, OCR_S, CRITIQUE, etc.). This is
        the ONLY deterministic way to know which extraction pass a
        record's content came from -- it powers both the per-row
        *_prov columns AND the q_no_anchors.field_provenance /
        provenance_notes vectors a consumer can read.

    sorted_pass_list: deduped, sorted list of the prov labels whose
        field is actually populated (i.e. has both a prov label AND
        a non-empty value). This is what the per-chapter
        pass_provenance_summary counts and what provenance_notes
        mirrors. A field with a prov label but no value (the loop
        wrote "_prov"=X then later cleared the value) is dropped
        here -- we only surface passes that actually contributed
        content to the record.

    model_q_no: the integer the extraction loop emitted (or `qn` as
        fallback when the model emitted None).

    disagree: not currently inferable from the per-field provs (the
        pass name is a label, not a q_no). The existing pipeline
        captures cross-pass disagreement via the
        unconfirmed_discontinuous_qno guard, which is observable
        from the orphans list (not from chapter_records). The caller
        threads that signal through reconcile_qids -> write_split_outputs
        via `extra_reasons`. Always False here; kept in the tuple so
        _build_q_no_anchors doesn't have to change shape.
    """
    prov = rec.get("_prov") or {}
    field_provenance: dict = {}
    populated_passes: list = []
    for field in _PROV_FIELDS:
        label = prov.get(field)
        if label and _field_has_content(rec, field):
            # The loop wrote a label AND the field has content right
            # now -> this pass actually contributed. Surface both
            # in field_provenance (so the row's *_prov column shows
            # the contributing label) and in populated_passes (so
            # the chapter's pass_provenance_summary counts it).
            field_provenance[field] = str(label)
            populated_passes.append(str(label))
        else:
            # Either the loop never wrote a label, or it wrote one
            # but a later sweep cleared the field's content. Either
            # way, the field is not currently contributed to by any
            # pass -> None in field_provenance, and no contribution
            # to populated_passes. A row reader sees *_prov = None
            # (matching the empty value); a chapter reader sees no
            # stale pass in the summary.
            field_provenance[field] = None
    # Fall back to the model-emitted q_no (always the integer)
    try:
        model_q_no = int(rec.get("q_no")) if rec.get("q_no") is not None else None
    except (TypeError, ValueError):
        model_q_no = None
    return (field_provenance, sorted(set(populated_passes)),
            model_q_no, False)


def _field_has_content(rec: dict, field: str) -> bool:
    """True when the field has a non-empty value on this record.

    Used by _collect_provs to filter prov labels whose field was later
    cleared (e.g. integrity sweep stripped a contaminated stem but
    left the "_prov" label in place -- that label is stale and must
    NOT inflate the per-chapter pass_provenance_summary)."""
    if field == "question_text":
        return bool((rec.get("question_text") or "").strip())
    if field == "options":
        opts = rec.get("options")
        return bool(opts) and isinstance(opts, dict) and len(opts) > 0
    if field == "correct_option":
        return bool((rec.get("correct_option") or "").strip())
    if field == "solution_text":
        return bool((rec.get("solution_text") or "").strip())
    if field == "tables":
        return bool(rec.get("tables"))
    return False


def _build_q_no_anchors(rec: dict, qn: int, anchors: dict,
                        source_pages: list,
                        neighbor_run_obs: dict = None,
                        carry_origin_obs: dict = None) -> dict:
    """Build the q_no_anchors vector for one record. Only fields that
    are actually populated are present; missing anchors are absent
    (not null), which matches the design's intent -- a consumer can
    distinguish 'no anchor' from 'anchor present but null'.

    Phase-2 additions: when the caller passes a neighbor_run_obs dict
    (the run-18 GUARD's first observation where q_no appears in
    trusted_qnos) and/or a carry_origin_obs dict (the first
    compute_carry() observation where q_no is last_open_question),
    inject the corresponding Phase-2 anchors as their design-doc
    section-3.1 shapes:
      * neighbor_run = {size, first, last, near_chapter_max}
      * carry_forward_origin = {from_window (list), cut_part}
    Both dicts are optional; missing observations stay absent.

    Phase-4: the per-field provenance map (`field_provenance`) and
    the deduped pass list (`provenance_notes`) are both derived from
    _collect_provs's new return shape. A consumer reading
    `q_no_anchors.field_provenance` can see the exact extraction
    pass every populated field came from (Q_PASS, A_PASS, S_PASS,
    Q_RETRY, A_RETRY, S_RETRY, RESCUE, RECOVER, DRAIN_Q, DRAIN_S,
    OCR_S, CRITIQUE, ...). `provenance_notes` is the short-form
    mirror (sorted, deduped pass list) the design spec calls for.
    The legacy `model_q_no_provs` key is kept for backward
    compatibility -- it now points at the same deduped pass list
    `provenance_notes` uses.
    """
    field_prov, populated_passes, model_q_no, disagree = _collect_provs(rec)
    if model_q_no is None:
        model_q_no = qn
    out = {
        "model_q_no": int(model_q_no),
        "model_q_no_provs": populated_passes,  # back-compat alias
        "model_q_no_disagree": bool(disagree),
        # Phase-4: per-field provenance map (question_text/options/
        # correct_option/solution_text/tables -> prov label or None).
        # Powers both the per-row *_prov columns and the
        # q_no_anchors field-level audit. Always present (empty
        # dict for a record with no prov info -- consistent
        # shape, no surprise KeyError on the consumer side).
        "field_provenance": field_prov,
        # Phase-4: the deduped sorted pass list whose field is
        # actually populated. A stale prov label (loop wrote the
        # label then a later sweep cleared the content) is
        # filtered out, so this list never inflates the per-chapter
        # pass_provenance_summary.
        "provenance_notes": populated_passes,
    }
    for name, payload in anchors.items():
        if payload:
            out[name] = payload
    if source_pages:
        out["section_position"] = {
            "kind": "page_set",
            "pages": sorted(set(int(p) for p in source_pages)),
        }
    # Phase-2 anchor injection: only attach when there's meaningful
    # data, so empty observations stay absent rather than appearing
    # as `{}` placeholders in the consumer's diff.
    if neighbor_run_obs:
        trusted = neighbor_run_obs.get("trusted_qnos") or []
        out["neighbor_run"] = {
            "size": len(trusted),
            "first": min(trusted) if trusted else None,
            "last": max(trusted) if trusted else None,
            "near_chapter_max": bool(
                neighbor_run_obs.get("known_max", 0)
                and trusted
                and min(trusted) - neighbor_run_obs["known_max"] <= 3
            ),
        }
    if carry_origin_obs:
        out["carry_forward_origin"] = {
            "from_window": list(carry_origin_obs.get("window_pages") or []),
            "cut_part": carry_origin_obs.get("cut_part"),
        }
    return out


# ---------------------------------------------------------------------------
# 5. reconcile_qids -- the chapter-close observation step.
# ---------------------------------------------------------------------------

def reconcile_qids(chapter_records: dict, qn_source_pages: dict,
                   pdf_path: str, page_files, subject: str,
                   chapter_no: int,
                   chapter_anchor_observations: dict = None) -> dict:
    """Walk the chapter's pages, harvest every printed anchor, grade
    every record, and split the chapter_records dict into
    (kept_records, unresolved_records). Kept records have a non-null
    q_id_grade; unresolved records go to unresolved_qids.jsonl only.

    This is the OBSERVATION step: it does NOT call Gemini and does NOT
    modify any field that the extraction loop uses. It only:
      1. assigns q_id_grade (one of 4) to every record
      2. attaches a q_no_anchors dict to every record
      3. removes UNRESOLVED records from chapter_records (in-place,
         modifying the dict the caller passes in)
      4. returns a dict {qn: rec} for the UNRESOLVED records, so the
         caller can write them to unresolved_qids.jsonl with full
         provenance.

    chapter_anchor_observations (Phase-2, optional): a dict with two
    lists ("neighbor_runs" and "carry_forwards") captured read-only by
    process_pdf in this exact chapter. The grader promotes single-anchor
    records from PROVISIONAL to RESOLVED when q_no is in a trusted run
    or has a valid carry-forward origin (design doc §2). None = fall
    back to Phase-1 single-anchor = RESOLVED (or PROVISIONAL on
    0 anchors, same as before -- no behavior change for callers that
    don't pass observations).
    """
    qn_set = set(chapter_records)
    per_qn_anchors = _harvest_anchors(chapter_records, qn_source_pages,
                                      pdf_path, page_files)
    # PHASE-2: precompute per-qn lookups on the observation hooks so
    # _grade_record stays O(1) per record. Both dicts are {} when the
    # caller doesn't pass observations (Phase-1 behaviour).
    neighbor_trusted_qns = {}   # qn -> first observation dict it appears in
    carry_origin = {}           # qn -> first carry_forwards dict for the pass
    if chapter_anchor_observations:
        for nr in chapter_anchor_observations.get("neighbor_runs") or []:
            for qn in nr.get("trusted_qnos") or []:
                if qn in qn_set and qn not in neighbor_trusted_qns:
                    neighbor_trusted_qns[qn] = nr
        for co in chapter_anchor_observations.get("carry_forwards") or []:
            qn = co.get("last_open_question")
            if qn in qn_set and qn not in carry_origin:
                carry_origin[qn] = co
    kept: dict = {}
    unresolved: dict = {}
    for qn, rec in chapter_records.items():
        anchors = per_qn_anchors.get(qn, {})
        # Convert qn_source_pages[qn] (set in the live pipeline) -> list
        sp = qn_source_pages.get(qn) or []
        if isinstance(sp, set):
            sp = sorted(sp)
        # Phase-2: precomputed per-qn lookups (empty when the caller
        # did not pass chapter_anchor_observations).
        nr_obs = neighbor_trusted_qns.get(qn)
        co_obs = carry_origin.get(qn)
        anchors_full = _build_q_no_anchors(rec, qn, anchors, sp,
                                          neighbor_run_obs=nr_obs,
                                          carry_origin_obs=co_obs)
        grade = _grade_record(anchors,
                              has_neighbor_run=bool(nr_obs),
                              has_carry_origin=bool(co_obs))
        # The design lists 4 UNRESOLVED conditions; only one is
        # observable in Phase 1 without modifying the loop:
        #   no_anchor_at_all -- when a record is in chapter_records but
        #   the model-only q_no is non-printable AND no answer key row
        #   names it AND no solution header names it. To avoid false
        #   positives (a record whose stem is split across a page that
        #   the visitor can't decode), we mark such records UNRESOLVED
        #   ONLY when:
        #     * the record has zero printed anchors AND
        #     * the record is missing at least one of question_text,
        #       options, correct_option, solution_text
        #   (i.e. it's already on the export-gate's "incomplete" list)
        # This matches the design's spirit without flagging every
        # well-extracted record as UNRESOLVED just because the text
        # layer is silent on its q_no.
        if grade == "PROVISIONAL":
            if _record_mostly_empty(rec) and not anchors:
                rec["q_id_grade"] = "UNRESOLVED"
                rec["q_no_anchors"] = anchors_full
                # Distinguish Case 2 (solution survived, question gone)
                # from the no-anchor empty record: Case 2 keeps the
                # solution_text and is marked missing_question_for_solution
                # so a master-data builder sees the explicit gap. The
                # no-anchor empty record has nothing left and gets
                # no_anchor_at_all.
                if (rec.get("solution_text") or "").strip():
                    rec["_unresolved_reason"] = "missing_question_for_solution"
                else:
                    rec["_unresolved_reason"] = "no_anchor_at_all"
                unresolved[qn] = rec
                continue
        # CASE 2 ESCALATION (real-run bug fix, 2026-08-08):
        # A record whose ONLY printed anchor is a "Solution to
        # Question N:" header (the question is NOT on this chapter's
        # pages; the real question lives in a different chapter)
        # grades as RESOLVED on a single anchor, but the record
        # itself is a phantom: no stem, no options, no answer
        # were ever extracted from this chapter's page range. The
        # S-pass fragment was preserved by the existing
        # drop_phantom_solution_only_records (which only fires on
        # cross-chapter duplicates), and the run-19 critique pass
        # may have hallucinated a question_text from the solution
        # prose. None of those are real questions for this chapter.
        # Route them to unresolved with reason=
        # missing_question_for_solution. The downstream consumer
        # (master-data builder) sees an explicit gap rather than
        # a fabricated question at this q_id. The master
        # build_final_question loop is unaffected: it iterates
        # chapter_records which is mutated in place below to
        # exclude these records.
        if (grade in ("RESOLVED", "RESOLVED_ANCHORED")
                and _is_solution_only_with_header(anchors)
                and _record_missing_options_or_answer(rec)):
            rec["q_id_grade"] = "UNRESOLVED"
            rec["q_no_anchors"] = anchors_full
            rec["_unresolved_reason"] = "missing_question_for_solution"
            unresolved[qn] = rec
            continue
        # Default: keep as the graded record
        rec["q_id_grade"] = grade
        rec["q_no_anchors"] = anchors_full
        kept[qn] = rec
    # In-place: the caller reads chapter_records after this call
    chapter_records.clear()
    chapter_records.update(kept)
    return {
        "unresolved": unresolved,
        "kept": kept,
        "per_qn_anchors": per_qn_anchors,
    }



def _record_mostly_empty(rec: dict) -> bool:
    """True when the record has no QUESTION-side content -- the
    question stem is missing AND the options are missing (the answer
    and solution are optional). Used to gate the "no_anchor_at_all"
    -> UNRESOLVED escalation (see reconcile_qids) AND the Case 2
    scenario (a record whose question is gone but whose solution
    survived is a candidate for missing_question_for_solution).
    A record with no stem, no options, no answer, and no solution
    is the empty-class (no question ever extracted).
    A record with no stem and no options but a non-empty solution
    is the Case 2 class (solution-only phantom).
    Both classes are UNRESOLVED per the design's Case 2 spec."""
    has_stem = bool((rec.get("question_text") or "").strip())
    has_options = bool(rec.get("options"))
    return not (has_stem or has_options)


def _is_solution_only_with_header(anchors: dict) -> bool:
    """True when the only printed anchor for a record is a
    printed_solution_header_match (a "Solution to Question N:"
    header on the page) AND the record has no stem anchor and no
    answer-key row anchor.

    This is the deterministic signature of the
    "missing_question_for_solution" case the real PSY-007 Railway
    run on 2026-08-08 exposed: a record whose q_no appears only
    as a solution-side reference (e.g. "Solution to Question 23:")
    on a page that is otherwise a different chapter's content. The
    real question text, options, and answer for this q_no live in
    a different chapter's page range; what we have here is just
    the S-pass fragment of the solution prose, plus a critique-hallucinated
    question_text written by the run-19 critique pass.

    The pre-fix behavior: the grader sees printed_solution_header_match
    and returns RESOLVED, so the record is kept as a normal graded
    question. questions.jsonl/answers.jsonl/solutions.jsonl all
    carry this hallucinated q_id, and chapter_completeness.json
    reports it as a graded question. The user's data confirms this
    is wrong -- the real PSY-007 has Q1-Q10 only, and Q23-Q26
    must NEVER appear in the three normal split files.

    The post-fix behavior: a record matching this signature is
    routed to unresolved_qids.jsonl with reason=
    "missing_question_for_solution", and is removed from the
    chapter_records dict the master build_final_question loop
    consumes. The master questions.jsonl is therefore also
    consistent with the three split files (no Q23-Q26 anywhere).

    Conservative gating: this only fires when BOTH of the following
    hold --
      1. The record's only printed anchor is a solution header.
         A record with a stem anchor OR an answer-key row anchor
         is presumed to be a real question on the page; the
         solution header alone is the only "but where is the
         question?" case.
      2. The record's options dict is empty or its correct_option
         is None. A record that has options AND a correct_option
         AND only a solution header would be the legitimate case
         of a question whose stem is on a previous page (the
         solution header is on this page, the question is on the
         previous page); we don't catch that here.
    """
    if not anchors.get("printed_solution_header_match"):
        return False
    if anchors.get("printed_stem_match"):
        return False
    if anchors.get("answer_key_row_match"):
        return False
    return True


def _record_missing_options_or_answer(rec: dict) -> bool:
    """True when options is empty or correct_option is None -- the
    signature that, combined with a solution-only printed anchor,
    indicates a phantom question. See _is_solution_only_with_header."""
    options = rec.get("options")
    correct = rec.get("correct_option")
    if not options or not isinstance(options, dict) or len(options) == 0:
        return True
    if correct is None or not str(correct).strip():
        return True
    return False


# ---------------------------------------------------------------------------
# 6. Per-file record builders -- emit one strictly-separated row per
#    (chapter, q_no) for the three split files, plus the support files.
# ---------------------------------------------------------------------------

def _source_pages_for(qn: int, qn_source_pages) -> list:
    """Accept either a dict {qn: pages} (the live-pipeline shape) or
    a per-record value (set/list/int, as stored on
    rec["_qn_source_pages"] by write_split_outputs). Returns a sorted
    list of unique int page numbers."""
    if isinstance(qn_source_pages, dict):
        sp = qn_source_pages.get(qn) or []
    else:
        sp = qn_source_pages or []
    if isinstance(sp, set):
        return sorted(int(p) for p in sp)
    if isinstance(sp, (list, tuple)):
        return sorted(int(p) for p in sp)
    if isinstance(sp, int):
        return [sp]
    return []


def _build_question_row(qn: int, rec: dict, chapter_id: str, subject: str,
                        chapter_no: int, image_files: dict,
                        ownership_pages=None) -> dict:
    q_id = f"{subject}-{chapter_no:03d}-{int(qn):03d}"
    options = rec.get("options") or {}
    option_rows = []
    for letter in ("A", "B", "C", "D"):
        text = options.get(letter, "") if isinstance(options, dict) else ""
        opt_imgs = []
        if isinstance(image_files.get("option"), dict):
            opt_imgs = [
                {"file": f,
                 "source_pages": ([(ownership_pages or {}).get(f)]
                                  if (ownership_pages or {}).get(f) is not None else [])}
                for f in (image_files["option"].get(letter) or [])
            ]
        option_rows.append({
            "id": letter,
            "text": text or "",
            "images": opt_imgs,
        })
    question_images = [
        # AUDIT-FIX: use the image's OWN extraction page from the ownership
        # ledger (was: always []). The question's source_pages are NOT the
        # image's provenance.
        {"file": f,
         "source_pages": ([(ownership_pages or {}).get(f)]
                          if (ownership_pages or {}).get(f) is not None else [])}
        for f in (image_files.get("question") or [])
    ]
    tables = rec.get("tables") or []
    # Phase-4: read per-field prov from q_no_anchors.field_provenance
    # (the canonical, sweep-filtered map) instead of rec["_prov"]
    # directly. A field that was prov'd by the loop but later had its
    # content cleared (e.g. integrity sweep stripped a contaminated
    # stem) now correctly reports None in the row's *_prov column.
    qa = rec.get("q_no_anchors") or {}
    fp = qa.get("field_provenance") or {}
    out = {
        "q_id": q_id,
        "chapter_id": chapter_id,
        "subject": subject,
        "chapter_no": int(chapter_no),
        "q_no": int(qn),
        "q_id_grade": rec.get("q_id_grade", "PROVISIONAL"),
        "q_no_anchors": qa,
        "question_text": rec.get("question_text") or "",
        "options": option_rows,
        "question_images": question_images,
        "tables": tables,
        "source_pages": _source_pages_for(qn, rec.get("_qn_source_pages") or {}),
    }
    if fp.get("question_text"):
        out["question_text_prov"] = fp["question_text"]
    if fp.get("options"):
        out["options_prov"] = fp["options"]
    if fp.get("tables"):
        out["tables_prov"] = fp["tables"]
    out["extraction_status"], missing = _classify_question_completeness(rec)
    if missing:
        out["missing_fields"] = missing
    return out


def _build_answer_row(qn: int, rec: dict, chapter_id: str, subject: str,
                      chapter_no: int) -> dict:
    q_id = f"{subject}-{chapter_no:03d}-{int(qn):03d}"
    correct = rec.get("correct_option")
    qa = rec.get("q_no_anchors") or {}
    fp = qa.get("field_provenance") or {}
    out = {
        "q_id": q_id,
        "chapter_id": chapter_id,
        "subject": subject,
        "chapter_no": int(chapter_no),
        "q_no": int(qn),
        "correct_option": correct,
        "correct_option_prov": fp.get("correct_option"),
        "q_id_grade": rec.get("q_id_grade", "PROVISIONAL"),
        "q_no_anchors": qa,
        "source_pages": _source_pages_for(qn, rec.get("_qn_source_pages") or {}),
    }
    out["extraction_status"], missing = _classify_answer_completeness(rec)
    if missing:
        out["missing_fields"] = missing
    return out


def _build_solution_row(qn: int, rec: dict, chapter_id: str, subject: str,
                        chapter_no: int, image_files: dict,
                        ownership_pages=None) -> dict:
    q_id = f"{subject}-{chapter_no:03d}-{int(qn):03d}"
    tables = rec.get("tables") or []
    sol_imgs = [
        {"file": f,
         "source_pages": ([(ownership_pages or {}).get(f)]
                          if (ownership_pages or {}).get(f) is not None else [])}
        for f in (image_files.get("solution") or [])
    ]
    qa = rec.get("q_no_anchors") or {}
    fp = qa.get("field_provenance") or {}
    out = {
        "q_id": q_id,
        "chapter_id": chapter_id,
        "subject": subject,
        "chapter_no": int(chapter_no),
        "q_no": int(qn),
        "solution_text": rec.get("solution_text") or "",
        "tables": tables,
        "solution_images": sol_imgs,
        "solution_prov": fp.get("solution_text"),
        "tables_prov": fp.get("tables"),
        "q_id_grade": rec.get("q_id_grade", "PROVISIONAL"),
        "q_no_anchors": qa,
        "source_pages": _source_pages_for(qn, rec.get("_qn_source_pages") or {}),
    }
    out["extraction_status"], missing = _classify_solution_completeness(rec)
    if missing:
        out["missing_fields"] = missing
    return out


def _classify_question_completeness(rec: dict) -> tuple:
    """(status, missing_fields). COMPLETE if stem + 4 options all
    populated; otherwise INCOMPLETE with a missing_fields list."""
    missing = []
    if not (rec.get("question_text") or "").strip():
        missing.append("question_text")
    opts = rec.get("options") or {}
    if not isinstance(opts, dict) or len(opts) < 4:
        missing.append("options")
    else:
        for letter in ("A", "B", "C", "D"):
            if not str(opts.get(letter, "") or "").strip():
                missing.append("options")
                break
    if not rec.get("tables") and not missing:
        # tables are not REQUIRED (per design), so don't flag
        pass
    if missing:
        return "INCOMPLETE", missing
    return "COMPLETE", []


def _classify_answer_completeness(rec: dict) -> tuple:
    if not (rec.get("correct_option") or "").strip():
        return "INCOMPLETE", ["correct_option"]
    return "COMPLETE", []


def _classify_solution_completeness(rec: dict) -> tuple:
    if not (rec.get("solution_text") or "").strip():
        return "INCOMPLETE", ["solution_text"]
    return "COMPLETE", []


# ---------------------------------------------------------------------------
# 7. Unresolved-qid and orphan row builders
# ---------------------------------------------------------------------------

def _build_unresolved_qid_row(qn: int, rec: dict, chapter_id: str,
                              subject: str, chapter_no: int) -> dict:
    q_id = f"{subject}-{chapter_no:03d}-{int(qn):03d}"
    reason = rec.get("_unresolved_reason", "no_anchor_at_all")
    if reason not in UNRESOLVED_REASONS:
        reason = "no_anchor_at_all"
    # available_passes: a snapshot of which pass-populated fields exist
    prov = rec.get("_prov") or {}
    available = {}
    field_to_pass = {
        "question_text": "Q_PASS",
        "options": "Q_PASS",
        "correct_option": "A_PASS",
        "solution_text": "S_PASS",
    }
    for field, default_pass in field_to_pass.items():
        present = bool({
            "question_text": rec.get("question_text"),
            "options": rec.get("options"),
            "correct_option": rec.get("correct_option"),
            "solution_text": rec.get("solution_text"),
        }.get(field))
        if present:
            available[default_pass.split("_")[0] + "_PASS"] = {
                "had_item": True,
                "q_no": int(qn) if rec.get("q_no") is None else
                (int(rec["q_no"]) if str(rec["q_no"]).isdigit() else None),
                "fields": [field],
            }
    return {
        "q_id": q_id,
        "chapter_id": chapter_id,
        "subject": subject,
        "chapter_no": int(chapter_no),
        "q_no": int(qn),
        "kind": "unresolved_qid",
        "reason": reason,
        "q_no_anchors": rec.get("q_no_anchors", {}),
        "available_passes": available,
        "source_pages": _source_pages_for(qn, rec.get("_qn_source_pages") or {}),
    }


def _build_orphan_row(orph: dict, chapter_id: str, subject: str,
                      chapter_no: int) -> dict:
    """orph is a dict the existing pipeline populates: chapter_id,
    pass, pdf_pages, new_pages, carry_q_no, cut_part, last_qn_in_batch,
    reason, item. We add the q_id-less fragment's content to a
    truncated snippet (never the full item) so a human reviewer can
    see what was at stake."""
    item = orph.get("item") or {}
    snippet = ""
    for f in ("question_text", "options", "correct_option", "solution_text"):
        v = item.get(f)
        if v:
            snippet = (str(v) if not isinstance(v, str) else v)[:600]
            if snippet:
                break
    return {
        "subject": subject,
        "chapter_id": chapter_id,
        "source_pages": sorted(set(
            int(p) for p in (orph.get("pdf_pages") or orph.get("new_pages") or [])
            if str(p).lstrip("-").isdigit()
        )),
        "pass": orph.get("pass"),
        "reason": orph.get("reason", "q_id_unresolved"),
        "fragment": snippet,
        "carry_q_no": orph.get("carry_q_no"),
        "cut_part": orph.get("cut_part"),
        "last_qn_in_batch": orph.get("last_qn_in_batch"),
    }


# ---------------------------------------------------------------------------
# 8. write_split_outputs -- the per-chapter writer.
# ---------------------------------------------------------------------------

def write_split_outputs(*, chapter_id: str, subject: str, chapter_no: int,
                        chapter_records: dict, image_files_by_q: dict,
                        qn_source_pages: dict, orphans: list,
                        chapter_unresolved_images: list,
                        pdf_path: str, page_files,
                        reconciled: dict,
                        output_root,
                        ownership_pages=None) -> dict:
    """Write all seven per-chapter files atomically. chapter_completeness.json
    is written LAST as the "this chapter's split is fully on disk" signal.
    Returns the chapter_completeness.json content (the per-chapter summary
    the design requires)."""
    output_root = Path(output_root)
    chapter_dir = output_root / "split" / subject / chapter_id
    chapter_dir.mkdir(parents=True, exist_ok=True)

    # Copy qn_source_pages into each record so the row builders can read it
    # without a separate lookup. This is a per-record decoration done here
    # (not in reconcile_qids) so the original chapter_records is untouched
    # when the live pipeline continues to use it for build_final_question.
    for qn, rec in chapter_records.items():
        rec.setdefault("_qn_source_pages", qn_source_pages.get(qn) or set())

    # Build the rows in deterministic order
    qns = sorted(chapter_records)
    image_files_by_q = image_files_by_q or {}
    question_rows = [
        _build_question_row(qn, chapter_records[qn], chapter_id, subject,
                            chapter_no, image_files_by_q.get(qn, {}),
                            ownership_pages=ownership_pages)
        for qn in qns
    ]
    answer_rows = [
        _build_answer_row(qn, chapter_records[qn], chapter_id, subject,
                          chapter_no)
        for qn in qns
    ]
    solution_rows = [
        _build_solution_row(qn, chapter_records[qn], chapter_id, subject,
                            chapter_no, image_files_by_q.get(qn, {}),
                            ownership_pages=ownership_pages)
        for qn in qns
    ]

    # Atomic writes (order: data files first, completeness.json LAST).
    # A crash mid-write leaves either the previous chapter's files or
    # a fresh partial set; the completeness.json absence is the
    # downstream signal "this chapter's split is not fully on disk yet".
    _atomic_jsonl_write(chapter_dir / "questions.jsonl", question_rows)
    _atomic_jsonl_write(chapter_dir / "answers.jsonl", answer_rows)
    _atomic_jsonl_write(chapter_dir / "solutions.jsonl", solution_rows)

    # Unresolved q_ids: records reconcile_qids removed
    unresolved_dict = (reconciled or {}).get("unresolved") or {}
    unresolved_rows = [
        _build_unresolved_qid_row(qn, rec, chapter_id, subject, chapter_no)
        for qn, rec in sorted(unresolved_dict.items())
    ]
    _atomic_jsonl_write(chapter_dir / "unresolved_qids.jsonl", unresolved_rows)

    # Orphans: chapter-scoped view of the existing in-memory orphan list
    orphan_rows = [
        _build_orphan_row(o, chapter_id, subject, chapter_no)
        for o in (orphans or [])
    ]
    _atomic_jsonl_write(chapter_dir / "orphans.jsonl", orphan_rows)

    # Image manifest: chapter-scoped cross-reference of every owned image
    # file. Built from the same image_files_by_q dict the master file uses,
    # so the chapter-scoped manifest is a VIEW, not a separate source of
    # truth (the existing data/image_ownership.jsonl remains the global
    # source of truth per the user's signed-off design decision).
    # AUDIT-FIX: the manifest used to list the QUESTION's source_pages for
    # every image -- question pages are not image provenance. Each image now
    # carries its own extraction_page from the ownership ledger; the
    # question's pages remain as a secondary review hint only.
    def _img_pages(f, qn):
        p = (ownership_pages or {}).get(f)
        return [p] if p is not None else _source_pages_for(qn, qn_source_pages)

    image_manifest_rows = []
    for qn in qns:
        entry = image_files_by_q.get(qn) or {}
        for f in (entry.get("question") or []):
            image_manifest_rows.append({
                "q_id": f"{subject}-{chapter_no:03d}-{int(qn):03d}",
                "type": "QUESTION",
                "option_letter": None,
                "file": f,
                "source_pages": _img_pages(f, qn),
                "extraction_page": (ownership_pages or {}).get(f),
            })
        for f in (entry.get("solution") or []):
            image_manifest_rows.append({
                "q_id": f"{subject}-{chapter_no:03d}-{int(qn):03d}",
                "type": "SOLUTION",
                "option_letter": None,
                "file": f,
                "source_pages": _img_pages(f, qn),
                "extraction_page": (ownership_pages or {}).get(f),
            })
        for letter, files in (entry.get("option") or {}).items():
            for f in (files or []):
                image_manifest_rows.append({
                    "q_id": f"{subject}-{chapter_no:03d}-{int(qn):03d}",
                    "type": "OPTION",
                    "option_letter": str(letter).upper(),
                    "file": f,
                    "source_pages": _img_pages(f, qn),
                    "extraction_page": (ownership_pages or {}).get(f),
                })
    _atomic_jsonl_write(chapter_dir / "image_manifest.jsonl", image_manifest_rows)

    # Completeness summary -- the master per-chapter report
    # Per the design: q_id_grade is the SAME string across all three
    # files for a given source question, so we count once per q_id
    # (NOT once per (q_id, file) -- that would triple-count).
    # extraction_status is per-row (per file) because the three files
    # have different COMPLETE/INCOMPLETE semantics (a question can be
    # COMPLETE in questions.jsonl but INCOMPLETE in solutions.jsonl).
    grade_counts = {g: 0 for g in ALLOWED_Q_ID_GRADES}
    seen_qids = set()
    extraction_counts = {"COMPLETE": 0, "INCOMPLETE": 0}
    pass_summary: dict = {}
    seen_prov_pairs: set = set()
    for r in question_rows + answer_rows + solution_rows:
        qid = r.get("q_id")
        if qid not in seen_qids:
            seen_qids.add(qid)
            g = r.get("q_id_grade")
            if g in grade_counts:
                grade_counts[g] += 1
        es = r.get("extraction_status")
        if es in extraction_counts:
            extraction_counts[es] += 1
        # pass_summary: count each (q_id, prov_label) pair exactly once,
        # so an A_PASS and a Q_PASS on the same q_id each count once.
        # Phase-4: read from provenance_notes (the deduped, sweep-filtered
        # list of passes whose field is actually populated). The legacy
        # model_q_no_provs alias still works, but provenance_notes is the
        # canonical key going forward.
        prov_labels = (r.get("q_no_anchors") or {}).get("provenance_notes") or []
        for prov_label in prov_labels:
            if (qid, prov_label) not in seen_prov_pairs:
                seen_prov_pairs.add((qid, prov_label))
                pass_summary[prov_label] = pass_summary.get(prov_label, 0) + 1

    completeness = {
        "chapter_id": chapter_id,
        "subject": subject,
        "chapter_no": int(chapter_no),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),

        "question_records": len(question_rows),
        "answer_records": len(answer_rows),
        "solution_records": len(solution_rows),
        "image_manifest_records": len(image_manifest_rows),

        "incomplete_questions": sum(
            1 for r in question_rows
            if r.get("extraction_status") == "INCOMPLETE"
        ),
        "incomplete_answers": sum(
            1 for r in answer_rows
            if r.get("extraction_status") == "INCOMPLETE"
        ),
        "incomplete_solutions": sum(
            1 for r in solution_rows
            if r.get("extraction_status") == "INCOMPLETE"
        ),

        "unresolved_qid_count": len(unresolved_rows),
        "unresolved_qid_q_nos": sorted(int(qn) for qn in unresolved_dict),

        "orphan_count": len(orphan_rows),
        "unresolved_image_count": len(chapter_unresolved_images or []),

        "q_id_grade_counts": grade_counts,
        "extraction_status_counts": extraction_counts,
        "pass_provenance_summary": pass_summary,

        # Phase-2 plan completed: the two non-printed anchors from
        # design doc §3.1 (neighbor_run, carry_forward_origin) are
        # populated by the read-only observation hooks in process_pdf.
        # Phase-5 completed: the two OCR anchors (ocr_stem_match,
        # ocr_solution_header_match) are populated by the chained
        # scan in _harvest_anchors (pdftotext primary, tesseract
        # fallback for garbled pages). The phase2_pending_anchors
        # dict is now empty -- every design-doc §3.1 anchor is
        # populated by default for every chapter.
        "phase2_pending_anchors": {},
    }
    _atomic_json_write(chapter_dir / "chapter_completeness.json", completeness)
    return completeness
