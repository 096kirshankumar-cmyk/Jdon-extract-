"""
test_phase4_provenance.py
=========================

Phase 4 (a) proof: the per-field provenance map (`field_provenance`)
and the deduped populated-pass list (`provenance_notes`) are wired
correctly into every q_no_anchors vector. The row builders read from
`field_provenance` (not from the raw `rec["_prov"]` dict) so a field
that was prov'd by the loop but later had its content cleared
(integrity sweep stripped a contaminated stem, etc.) correctly
reports `None` in the row's `*_prov` column.

Background
----------
Pre-Phase-4, `provenance_notes` was a short-form mirror of
`model_q_no_provs` (the deduped set of every prov label the loop
wrote, regardless of whether the value was still present). This
inflated `pass_provenance_summary` in chapter_completeness.json with
stale labels whose fields had been cleared, and gave consumers no
way to know which pass actually contributed which field.

Phase-4 changes:
  * `_collect_provs` now returns a per-field provenance map AND
    filters the populated-pass list to only include passes whose
    field has content right now.
  * `q_no_anchors.field_provenance` is the new canonical key
    (always present, possibly empty for records with no prov info).
  * `q_no_anchors.provenance_notes` is the deduped populated-pass
    list (Phase-4 semantics: filter stale labels).
  * `q_no_anchors.model_q_no_provs` is kept as a back-compat alias
    pointing at the same list.
  * Row builders use `field_provenance` for their `*_prov` columns
    so a field cleared by a later sweep shows `None`, not the
    stale prov label.

What this test proves
--------------------
  A. `_collect_provs` returns the new 4-tuple shape and
     `field_provenance` covers all 5 canonical fields
  B. A record with Q_PASS / A_PASS / S_PASS labels on populated
     fields surfaces all three in `provenance_notes` and the
     matching entries in `field_provenance`
  C. A record with a stale prov label (loop wrote "Q_PASS" then a
     later sweep cleared question_text) does NOT pollute
     `provenance_notes` -- the label is filtered out because the
     field has no content. The matching `field_provenance[question_text]`
     is also None (consistent shape: "loop wrote a label and then
     cleared the field" == "no contribution").
  D. `field_provenance` is always present in `q_no_anchors`, even
     for records with no `_prov` dict at all
  E. The `q_no_anchors.model_q_no_provs` back-compat alias still
     points at the same list as `provenance_notes`
  F. Row builders use `field_provenance` (not `rec["_prov"]`):
     questions.jsonl / answers.jsonl / solutions.jsonl get the
     `*_prov` column from the sweep-filtered map
  G. `pass_provenance_summary` in chapter_completeness.json uses
     the new `provenance_notes` list -- a stale prov label does
     NOT inflate the chapter's pass counts
  H. Edge case: a record with prov labels but ALL fields cleared
     (e.g. anchorless-dropped fragment) reports empty
     `provenance_notes` and `field_provenance` full of None
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Stub heavy deps
import types
if "google" not in sys.modules:
    google_mod = types.ModuleType("google")
    genai_mod = types.ModuleType("google.generativeai")
    genai_mod.configure = lambda **kw: None
    genai_mod.GenerativeModel = lambda *a, **kw: None
    sys.modules["google"] = google_mod
    sys.modules["google.generativeai"] = genai_mod
if "PIL" not in sys.modules:
    pil_mod = types.ModuleType("PIL")
    pil_mod.Image = types.SimpleNamespace(open=lambda *a, **kw: None)
    pil_mod.ImageDraw = types.SimpleNamespace(Draw=lambda *a, **kw: None)
    sys.modules["PIL"] = pil_mod
if "pypdf" not in sys.modules:
    pypdf_mod = types.ModuleType("pypdf")
    pypdf_mod.PdfReader = lambda *a, **kw: None
    sys.modules["pypdf"] = pypdf_mod
if "pytesseract" not in sys.modules:
    sys.modules["pytesseract"] = types.ModuleType("pytesseract")

import qbank_pipeline as qp
import split_outputs


# ===========================================================================
# Fixtures
# ===========================================================================

def build_record(qn, *, stem="", options=None, answer="", solution="",
                 prov=None, default_options=True):
    """Build a chapter_records-shaped record. prov is the loop's
    per-field _prov dict; None means "no prov at all".

    default_options=True (default) sets options to a non-empty stub
    so the record passes the basic "has content" check. Pass
    default_options=False to keep options=None (the record is
    question-side empty, like an anchorless-dropped fragment)."""
    if default_options and options is None:
        options = {"A": "a", "B": "b", "C": "c", "D": "d"}
    rec = {
        "q_no": qn,
        "question_text": stem,
        "options": options,
        "correct_option": answer,
        "solution_text": solution,
        "tables": [],
        "has_figure_in_question": False,
        "has_figure_in_solution": False,
    }
    if prov is not None:
        rec["_prov"] = prov
    return rec


# ===========================================================================
# Tests
# ===========================================================================

def main():
    n_ok = 0
    n_total = 0
    failed = []

    def check(label, cond, detail=""):
        nonlocal n_ok, n_total
        n_total += 1
        if cond:
            n_ok += 1
            print(f"  ok:   {label}"
                  + (f" ({detail})" if detail else ""))
        else:
            print(f"  FAIL: {label}"
                  + (f" (got {detail})" if detail else ""))
            failed.append(label)

    # ----------------------------------------------------------------
    # A. _collect_provs: 4-tuple shape + field_provenance covers 5 fields
    # ----------------------------------------------------------------
    print("=" * 80)
    print("A. _collect_provs returns 4-tuple (field_prov, populated_passes, model_q_no, disagree)")
    print("=" * 80)
    rec = build_record(1, stem="Q?", answer="A", solution="Because A.",
                       prov={"question_text": "Q_PASS", "options": "Q_PASS",
                             "correct_option": "A_PASS", "solution_text": "S_PASS"})
    field_prov, populated_passes, model_q_no, disagree = split_outputs._collect_provs(rec)
    check("returns 4-tuple", len((field_prov, populated_passes, model_q_no, disagree)) == 4)
    check("model_q_no == 1", model_q_no == 1, f"got {model_q_no!r}")
    check("disagree is False", disagree is False, f"got {disagree!r}")
    check("field_provenance covers all 5 canonical fields",
          set(field_prov.keys()) == {"question_text", "options",
                                     "correct_option", "solution_text", "tables"},
          f"got {set(field_prov.keys())}")
    check("question_text -> Q_PASS", field_prov.get("question_text") == "Q_PASS",
          f"got {field_prov.get('question_text')!r}")
    check("options -> Q_PASS", field_prov.get("options") == "Q_PASS",
          f"got {field_prov.get('options')!r}")
    check("correct_option -> A_PASS", field_prov.get("correct_option") == "A_PASS",
          f"got {field_prov.get('correct_option')!r}")
    check("solution_text -> S_PASS", field_prov.get("solution_text") == "S_PASS",
          f"got {field_prov.get('solution_text')!r}")
    check("tables -> None (no prov given)", field_prov.get("tables") is None,
          f"got {field_prov.get('tables')!r}")
    print()

    # ----------------------------------------------------------------
    # B. Populated-pass list includes every pass whose field is present
    # ----------------------------------------------------------------
    print("=" * 80)
    print("B. populated_passes includes Q_PASS + A_PASS + S_PASS for a clean record")
    print("=" * 80)
    rec = build_record(2, stem="What is X?", answer="B", solution="X is B.",
                       prov={"question_text": "Q_PASS", "options": "Q_PASS",
                             "correct_option": "A_PASS", "solution_text": "S_PASS"})
    _, populated_passes, _, _ = split_outputs._collect_provs(rec)
    check("populated_passes contains Q_PASS", "Q_PASS" in populated_passes,
          f"got {populated_passes}")
    check("populated_passes contains A_PASS", "A_PASS" in populated_passes,
          f"got {populated_passes}")
    check("populated_passes contains S_PASS", "S_PASS" in populated_passes,
          f"got {populated_passes}")
    check("populated_passes is deduped + sorted",
          populated_passes == sorted(set(populated_passes)),
          f"got {populated_passes}")
    print()

    # ----------------------------------------------------------------
    # C. Stale prov label (loop wrote Q_PASS then sweep cleared stem)
    #    does NOT pollute populated_passes; field_provenance is None
    # ----------------------------------------------------------------
    print("=" * 80)
    print("C. Stale prov label filtered out (loop wrote Q_PASS, sweep cleared stem)")
    print("=" * 80)
    rec = build_record(3, stem="",  # stem CLEARED
                       answer="C", solution="C is correct.",
                       prov={"question_text": "Q_PASS",  # stale: field is empty
                             "options": "Q_PASS", "correct_option": "A_PASS",
                             "solution_text": "S_PASS"})
    field_prov, populated_passes, _, _ = split_outputs._collect_provs(rec)
    # The stale Q_PASS on question_text is filtered out, BUT Q_PASS is
    # still in populated_passes because options still has content and
    # was also prov'd Q_PASS. Q_PASS appears in the deduped list
    # exactly once (the dedupe is what populates populated_passes).
    check("Q_PASS IS in populated_passes (options field has Q_PASS label and content)",
          "Q_PASS" in populated_passes,
          f"got {populated_passes}")
    check("A_PASS in populated_passes (answer has content)",
          "A_PASS" in populated_passes, f"got {populated_passes}")
    check("S_PASS in populated_passes (solution has content)",
          "S_PASS" in populated_passes, f"got {populated_passes}")
    check("populated_passes is deduped (Q_PASS appears once, not twice)",
          populated_passes.count("Q_PASS") == 1,
          f"got {populated_passes}")
    check("field_provenance[question_text] is None (consistent shape)",
          field_prov.get("question_text") is None,
          f"got {field_prov.get('question_text')!r}")
    check("field_provenance[options] is Q_PASS (field has content)",
          field_prov.get("options") == "Q_PASS",
          f"got {field_prov.get('options')!r}")
    print()

    # ----------------------------------------------------------------
    # D. field_provenance is always present in q_no_anchors, even when
    #    the record has no _prov dict at all
    # ----------------------------------------------------------------
    print("=" * 80)
    print("D. field_provenance is always present in q_no_anchors")
    print("=" * 80)
    rec = build_record(4, stem="What is Y?", answer="A", solution="Y is A.")
    # no prov passed
    anchors_full = split_outputs._build_q_no_anchors(rec, 4, {}, [])
    check("'field_provenance' key is in q_no_anchors",
          "field_provenance" in anchors_full,
          f"keys: {list(anchors_full)}")
    check("'provenance_notes' key is in q_no_anchors",
          "provenance_notes" in anchors_full,
          f"keys: {list(anchors_full)}")
    # field_provenance always has all 5 keys (consistent shape), all None
    check("field_provenance has all 5 keys with None values when no prov given",
          anchors_full["field_provenance"] == {
              "question_text": None, "options": None,
              "correct_option": None, "solution_text": None, "tables": None,
          },
          f"got {anchors_full['field_provenance']}")
    check("provenance_notes is empty list when no prov was given",
          anchors_full["provenance_notes"] == [],
          f"got {anchors_full['provenance_notes']}")
    print()

    # ----------------------------------------------------------------
    # E. model_q_no_provs is a back-compat alias for provenance_notes
    # ----------------------------------------------------------------
    print("=" * 80)
    print("E. model_q_no_provs is a back-compat alias for provenance_notes")
    print("=" * 80)
    rec = build_record(5, stem="What is Z?", answer="A", solution="Z is A.",
                       prov={"question_text": "Q_PASS", "options": "Q_PASS",
                             "correct_option": "A_PASS", "solution_text": "S_PASS"})
    anchors_full = split_outputs._build_q_no_anchors(rec, 5, {}, [])
    check("model_q_no_provs is present (back-compat)",
          "model_q_no_provs" in anchors_full,
          f"keys: {list(anchors_full)}")
    check("model_q_no_provs == provenance_notes (alias)",
          anchors_full["model_q_no_provs"] == anchors_full["provenance_notes"],
          f"got provs={anchors_full['model_q_no_provs']} notes={anchors_full['provenance_notes']}")
    print()

    # ----------------------------------------------------------------
    # F. Row builders use field_provenance (not rec["_prov"]): the
    #    *_prov column on a stale-label record is None, not the label
    # ----------------------------------------------------------------
    print("=" * 80)
    print("F. Row builders use field_provenance (sweep-filtered, not raw rec['_prov'])")
    print("=" * 80)
    # Simulate a record that already went through reconcile_qids
    # (so it has q_id_grade + q_no_anchors + the field_provenance map)
    rec = build_record(6, stem="",  # stem CLEARED by sweep
                       answer="B", solution="B is correct.",
                       prov={"question_text": "Q_PASS",  # stale
                             "options": "Q_PASS", "correct_option": "A_PASS",
                             "solution_text": "S_PASS"})
    tmp = tempfile.mkdtemp(prefix="phase4_f_")
    original_DATA_DIR = qp.DATA_DIR
    qp.DATA_DIR = qp.Path(tmp)
    try:
        original_harvest = split_outputs._harvest_anchors
        split_outputs._harvest_anchors = lambda *a, **kw: {qn: {} for qn in {6}}
        try:
            reconciled = split_outputs.reconcile_qids(
                {6: rec}, {}, "/tmp/PSY_ch007_fake.pdf", [], "PSY", 7,
                chapter_anchor_observations=None)
            kept = reconciled["kept"]
            qrow = split_outputs._build_question_row(6, kept[6], "PSY-007", "PSY", 7, {})
            arow = split_outputs._build_answer_row(6, kept[6], "PSY-007", "PSY", 7)
            srow = split_outputs._build_solution_row(6, kept[6], "PSY-007", "PSY", 7, {})
        finally:
            split_outputs._harvest_anchors = original_harvest
    finally:
        qp.DATA_DIR = original_DATA_DIR
    # questions.jsonl row: question_text_prov should be None (stale label)
    check("question_text_prov is None on the question row (sweep cleared stem)",
          qrow.get("question_text_prov") is None,
          f"got {qrow.get('question_text_prov')!r}")
    check("options_prov is Q_PASS on the question row (options has content)",
          qrow.get("options_prov") == "Q_PASS",
          f"got {qrow.get('options_prov')!r}")
    # answers.jsonl row: correct_option_prov should be A_PASS
    check("correct_option_prov is A_PASS on the answer row",
          arow.get("correct_option_prov") == "A_PASS",
          f"got {arow.get('correct_option_prov')!r}")
    # solutions.jsonl row: solution_prov should be S_PASS
    check("solution_prov is S_PASS on the solution row",
          srow.get("solution_prov") == "S_PASS",
          f"got {srow.get('solution_prov')!r}")
    print()

    # ----------------------------------------------------------------
    # G. pass_provenance_summary in chapter_completeness.json uses the
    #    new provenance_notes list -- stale labels are filtered out
    # ----------------------------------------------------------------
    print("=" * 80)
    print("G. pass_provenance_summary uses provenance_notes (stale labels filtered)")
    print("=" * 80)
    tmp = tempfile.mkdtemp(prefix="phase4_g_")
    original_DATA_DIR = qp.DATA_DIR
    qp.DATA_DIR = qp.Path(tmp) / "data"
    try:
        # Two records: Q1 clean (Q/A/S all populated), Q2 stale stem
        chapter = {
            1: build_record(1, stem="What is X?", answer="A", solution="X is A.",
                            prov={"question_text": "Q_PASS", "options": "Q_PASS",
                                  "correct_option": "A_PASS", "solution_text": "S_PASS"}),
            2: build_record(2, stem="",  # sweep cleared
                            answer="B", solution="B is B.",
                            prov={"question_text": "Q_PASS", "options": "Q_PASS",
                                  "correct_option": "A_PASS", "solution_text": "S_PASS"}),
        }
        original_harvest = split_outputs._harvest_anchors
        split_outputs._harvest_anchors = lambda *a, **kw: {
            1: {"printed_stem_match": {"page": 100, "header_text": "1."}},
            2: {"printed_stem_match": {"page": 101, "header_text": "2."}},
        }
        try:
            reconciled = split_outputs.reconcile_qids(
                chapter, {1: [100], 2: [101]}, "/tmp/PSY_ch007_fake.pdf",
                [], "PSY", 7)
            comp = split_outputs.write_split_outputs(
                chapter_id="PSY-007", subject="PSY", chapter_no=7,
                chapter_records=chapter, image_files_by_q={},
                qn_source_pages={1: [100], 2: [101]}, orphans=[],
                chapter_unresolved_images=[], pdf_path="/tmp/PSY_ch007_fake.pdf",
                page_files=[], reconciled=reconciled, output_root=tmp)
        finally:
            split_outputs._harvest_anchors = original_harvest
    finally:
        qp.DATA_DIR = original_DATA_DIR
    # Q_PASS should be counted exactly TWICE: once for Q1 (Q1's
    # question_text + options both come from Q_PASS but the summary
    # counts unique (q_id, prov_label) pairs so Q1 = 1), once for Q2
    # (Q2's options only, since Q2's question_text was cleared by the
    # sweep). So Q_PASS = 2.
    summary = comp.get("pass_provenance_summary") or {}
    check("pass_provenance_summary is a dict",
          isinstance(summary, dict), f"got {type(summary).__name__}")
    check("Q_PASS count == 2 (Q1's stem+options + Q2's options; Q2's stem was filtered)",
          summary.get("Q_PASS") == 2, f"got {summary.get('Q_PASS')!r}")
    check("A_PASS count == 2 (both records have a correct_option)",
          summary.get("A_PASS") == 2, f"got {summary.get('A_PASS')!r}")
    check("S_PASS count == 2 (both records have a solution_text)",
          summary.get("S_PASS") == 2, f"got {summary.get('S_PASS')!r}")
    # Stale prov label (Q2's Q_PASS on question_text) is NOT counted as
    # an extra Q_PASS -- exactly 2 (one for Q1, one for Q2's options),
    # not 3.
    print()

    # ----------------------------------------------------------------
    # H. All fields cleared -> empty provenance_notes, field_provenance
    #    full of None
    # ----------------------------------------------------------------
    print("=" * 80)
    print("H. Anchorless-dropped record: all fields cleared, populated_passes is empty")
    print("=" * 80)
    rec = build_record(7, stem="", options=None, answer="", solution="",
                       prov={"question_text": "Q_PASS", "options": "Q_PASS",
                             "correct_option": "A_PASS", "solution_text": "S_PASS"},
                       default_options=False)
    field_prov, populated_passes, _, _ = split_outputs._collect_provs(rec)
    check("populated_passes is empty (all fields cleared)",
          populated_passes == [], f"got {populated_passes}")
    check("field_provenance[question_text] is None",
          field_prov.get("question_text") is None,
          f"got {field_prov.get('question_text')!r}")
    check("field_provenance[options] is None",
          field_prov.get("options") is None,
          f"got {field_prov.get('options')!r}")
    check("field_provenance[correct_option] is None",
          field_prov.get("correct_option") is None,
          f"got {field_prov.get('correct_option')!r}")
    check("field_provenance[solution_text] is None",
          field_prov.get("solution_text") is None,
          f"got {field_prov.get('solution_text')!r}")
    print()

    print("=" * 80)
    print(f"Phase 4 (a) provenance_notes test: {n_ok}/{n_total} assertions passed")
    print("=" * 80)
    if failed:
        print(f"FAILED: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
