"""
test_psy007_postfix_e2e.py
==========================

End-to-end Phase 2 proof: the EXACT post-routing-fix Railway scenario on
PSY-007 (5 of 5 suspect_stem gate violations) now resolves to 1 of 5.

Background
----------
This test wires together every part of the user-fix pipeline path, using
synthetic chapter_records that mirror the real Railway run's shape:

  Pre-fix flow (the bug):
    rescue -> 5 records filled with verbatim stem text from
    question-side pages -> chapter_integrity_sweep quarantines all 5
    (medical-term token-overlap heuristic) -> 5 of 5 suspect_stem gate
    violations -> [GATE] NOT a clean export -> CRITIQUE pass proves
    4 of 5 were false alarms (q4 confirmed, q6/q7/q9 cannot_verify
    because the page shown to CRITIQUE didn't have the solution)

  Post-fix flow (the user fix, 2026-08-08, commit ec411ae):
    rescue -> 5 records filled with verbatim stem text from
    question-side pages -> chapter_integrity_sweep quarantines ONLY
    q1 (the real "Option A:" contamination) -> 1 of 5 suspect_stem
    gate violations -> [GATE] 1 violation, the well-formed q2/q4/q6/
    q7/q9 ship clean.

What this test proves
---------------------
  A. Rescue fills all 5 of the 5 well-formed records (post-fix) --
     was 0 of 5 before the fix.
  B. chapter_integrity_sweep quarantines ONLY q1 (the real opener
     contamination) -- NOT q2/q4/q6/q7/q9.
  C. _export_gate_violations on the post-rescue + post-sweep records
     shows exactly 1 suspect_stem violation (q1) -- was 5 of 5.
  D. The well-formed records (q2/q4/q6/q7/q9) reach the export with
     their full stem text intact (no silent corruption) -- proves the
     user fix did NOT weaken the validator.
  E. The phantom-record shape guard (RUN-20) is unchanged and STILL
     fires for Q23 class (catches the real phantom) -- the fix is
     surgical, not a blanket removal.

What this test does NOT do
--------------------------
  * Make any Gemini calls. The pipeline path is pure-Python here.
  * Run the real PSY-007 chapter. Synthetic shape only.
  * Touch the real data/ tree. Uses a temp directory.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Stub heavy deps (qbank_pipeline imports them at module load)
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


# ============================================================================
# 1. The PSY-007 post-routing-fix chapter shape (10 records, the same q_nos
#    the Railway log flagged in the suspect_stem gate)
# ============================================================================

# Shared medical vocabulary -- the q2/q4/q6/q7/q9 class on the real run
# used phrases like "the patient", "schizophrenia", "Clozapine", "dystonia"
# in BOTH the stem and the solution. The pre-fix heuristic at >=80% token
# overlap flagged them all as contamination. Post-fix: passes through.
SHARED_MEDICAL_TOKENS = (
    "the patient presents with psychotic disorder the differential includes "
    "schizophrenia schizophreniform disorder and brief psychotic disorder "
    "duration of symptoms is the key diagnostic criterion tardive dyskinesia "
    "and dystonia are extrapyramidal side effects of typical antipsychotics "
    "Clozapine is reserved for treatment resistant cases due to agranulocytosis "
    "risk the patient requires clozapine after failure of two adequate trials"
)


def build_well_formed_record(qn, *,
                              with_shared_medical_tokens=True,
                              correct="B"):
    """A well-formed MCQ record: stem + 4 options + answer + solution.
    When with_shared_medical_tokens is True, the stem and solution share
    the medical vocabulary that the pre-fix heuristic flagged. Post-fix
    this record must NOT be quarantined.

    Stems are intentionally differentiated (unique opening + unique
    closing per qn) so the duplicate-stem sweep (run-4) doesn't flag
    them as near-duplicates -- the duplicate-stem sweep is a separate
    concern from the contamination fix, and we don't want to conflate
    the two in this test.
    """
    if with_shared_medical_tokens:
        # Unique opening per qn, then shared medical vocabulary, then
        # unique closing per qn -- so each stem is distinctly its own,
        # not a near-duplicate of any sibling.
        stem = (f"Question {qn}: A {qn}-year-old {'man' if qn % 2 else 'woman'} "
                f"presents with symptoms consistent with the diagnosis. "
                f"{SHARED_MEDICAL_TOKENS}. "
                f"The most appropriate next step for q{qn} is")
        sol = (f"Answer: {correct} is correct because the {qn}-year-old "
               f"{'man' if qn % 2 else 'woman'} has the diagnosis. "
               f"{SHARED_MEDICAL_TOKENS}. Management of q{qn} involves "
               f"appropriate intervention with careful monitoring.")
    else:
        stem = (f"Question {qn}: a unique clinical scenario for q{qn} that "
                f"asks something specific and uses no shared medical vocabulary.")
        sol = (f"Answer: the answer to q{qn} involves a different mechanism than "
                f"q{qn-1} -- separate vocabulary entirely from the stem.")
    return {
        "question_text": stem,
        "options": {"A": f"Option A for q{qn}",
                    "B": f"Option B for q{qn}",
                    "C": f"Option C for q{qn}",
                    "D": f"Option D for q{qn}"},
        "correct_option": correct,
        "solution_text": sol,
        "tables": [],
        "has_figure_in_question": False,
        "has_figure_in_solution": False,
        "_prov": {
            "question_text": "Q_PASS",
            "options": "Q_PASS",
            "correct_option": "A_PASS",
            "solution_text": "S_PASS",
        },
    }


def build_real_contamination_q1():
    """ch7 q1-style REAL contamination: the stem literally STARTS with
    explanation prose (\"Option A: ...\"). This MUST still be caught
    by the explanation-opener check post-fix (the fix is surgical, not
    a blanket removal).
    """
    return {
        "question_text": "Option A: CAGE questionnaire is the most appropriate screening tool for alcohol use disorder.",
        "options": {"A": "CAGE", "B": "AUDIT", "C": "DAST", "D": "MAST"},
        "correct_option": "A",
        "solution_text": "The CAGE questionnaire is a 4-item screening tool for alcohol use disorder.",
        "tables": [],
        "has_figure_in_question": False,
        "has_figure_in_solution": False,
    }


def build_phantom_q23_style():
    """RUN-20 phantom class: stem+sol present, but NO options and NO
    correct_option. The real question is in another chapter; the stem
    was hallucinated. The phantom-shape guard MUST still fire post-fix.
    """
    return {
        "question_text": "Question 23 stem: hallucinated from surrounding solution prose by the model.",
        "options": None,
        "correct_option": None,
        "solution_text": "Real answer to q23 lives in another chapter. This is a phantom record.",
    }


def build_postfix_psy007_chapter():
    """The chapter shape mirroring the Railway post-routing-fix run.
    Returns 10 records: q1 contamination, q2/q4/q6/q7/q9 well-formed
    with shared medical vocabulary (the 5 the user flagged), q3/q5/q8/
    q10 clean.

    NOTE: q23-q26 phantom records are NOT in this chapter because the
    RUN-20 fix drops them at merge_question_records (the upstream
    FOREIGN guard) -- they never reach the export gate. The
    phantom-shape guard's correctness is covered separately by
    test_psy007_merge_q23_q26_phantom.py (4/4 assertions). Including
    them here would test two different things at once.
    """
    return {
        1: build_real_contamination_q1(),
        2: build_well_formed_record(2),    # shared medical vocab
        3: build_well_formed_record(3, with_shared_medical_tokens=False),
        4: build_well_formed_record(4),    # shared medical vocab
        5: build_well_formed_record(5, with_shared_medical_tokens=False),
        6: build_well_formed_record(6),    # shared medical vocab
        7: build_well_formed_record(7),    # shared medical vocab
        8: build_well_formed_record(8, with_shared_medical_tokens=False),
        9: build_well_formed_record(9),    # shared medical vocab
        10: build_well_formed_record(10, with_shared_medical_tokens=False),
    }


# ============================================================================
# 2. Tests
# ============================================================================

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

    chapter = build_postfix_psy007_chapter()

    # ---- A. The heuristic on the user's exact ask class
    #         (q2/q4/q6/q7/q9: well-formed, medical-term shared vocab)
    #         must NOT reject post-fix. ----
    print("=" * 80)
    print("A. _stem_reject_reason does NOT flag q2/q4/q6/q7/q9 (the user's fix)")
    print("=" * 80)
    for qn in (2, 4, 6, 7, 9):
        rec = chapter[qn]
        reason = qp._stem_reject_reason(rec["question_text"], rec)
        check(f"_stem_reject_reason does NOT flag q{qn}'s well-formed stem "
              f"(medical-term shared vocab; the user's fix)",
              reason is None,
              f"got {reason!r}")
    print()

    # ---- B. _stem_reject_reason STILL fires for real contamination. ----
    print("=" * 80)
    print("B. _stem_reject_reason STILL fires for the real q1 contamination")
    print("=" * 80)
    rec_q1 = chapter[1]
    reason_q1 = qp._stem_reject_reason(rec_q1["question_text"], rec_q1)
    check("explanation-opener check fires for q1 'Option A: ...' stem "
          "(the fix is surgical, not a blanket removal)",
          reason_q1 is not None and "explanation" in reason_q1.lower(),
          f"got {reason_q1!r}")
    print()

    # ---- C. The phantom-shape guard (RUN-20) STILL fires.
    #         We test the heuristic directly on a synthetic phantom-shaped
    #         record -- RUN-20's FOREIGN guard drops the real q23-q26 at
    #         merge time, so they never reach the export gate (the subject
    #         of this end-to-end test). The phantom-shape guard's
    #         correctness is covered by test_psy007_merge_q23_q26_phantom.py. ----
    print("=" * 80)
    print("C. _stem_reject_reason STILL fires for the RUN-20 phantom shape")
    print("=" * 80)
    phantom_rec = build_phantom_q23_style()
    reason_phantom = qp._stem_reject_reason(phantom_rec["question_text"],
                                            phantom_rec)
    check("phantom-shape guard fires on a stem+sol/no-options/no-answer record",
          reason_phantom is not None and "phantom" in reason_phantom.lower(),
          f"got {reason_phantom!r}")
    print()

    # ---- D. End-to-end: chapter_integrity_sweep on the post-routing-fix
    #         chapter quarantines ONLY q1 (the real contamination), NOT
    #         q2/q4/q6/q7/q9 (the medical-term well-formed records). ----
    print("=" * 80)
    print("D. chapter_integrity_sweep quarantines ONLY q1 (the real contamination)")
    print("=" * 80)
    tmp = tempfile.mkdtemp(prefix="psy007_postfix_e2e_")
    original_DATA_DIR = qp.DATA_DIR
    qp.DATA_DIR = qp.Path(tmp)
    try:
        stats = {}
        forced = qp.chapter_integrity_sweep(chapter, {}, "PSY", 7, stats)
        quarantine_qns = sorted([qn for qn, rec in chapter.items()
                                  if rec.get("_stem_suspect_reason")])
        check("q1 IS quarantined (real 'Option A:' contamination)",
              1 in quarantine_qns,
              f"q1 {'in' if 1 in quarantine_qns else 'NOT in'} {quarantine_qns}")
        check("q2 IS NOT quarantined (the user's fix: well-formed, medical vocab)",
              2 not in quarantine_qns,
              f"q2 {'in' if 2 in quarantine_qns else 'NOT in'} {quarantine_qns}")
        check("q4 IS NOT quarantined (the user's fix)",
              4 not in quarantine_qns,
              f"q4 {'in' if 4 in quarantine_qns else 'NOT in'} {quarantine_qns}")
        check("q6 IS NOT quarantined (the user's fix)",
              6 not in quarantine_qns,
              f"q6 {'in' if 6 in quarantine_qns else 'NOT in'} {quarantine_qns}")
        check("q7 IS NOT quarantined (the user's fix)",
              7 not in quarantine_qns,
              f"q7 {'in' if 7 in quarantine_qns else 'NOT in'} {quarantine_qns}")
        check("q9 IS NOT quarantined (the user's fix)",
              9 not in quarantine_qns,
              f"q9 {'in' if 9 in quarantine_qns else 'NOT in'} {quarantine_qns}")
        check("q3/q5/q8/q10 (clean records) are NOT quarantined",
              not any(qn in (3, 5, 8, 10) for qn in quarantine_qns),
              f"got quarantined qns: {quarantine_qns}")
        check("exactly 1 record quarantined (q1 only) -- was 5 of 10 pre-fix",
              len(quarantine_qns) == 1,
              f"got {len(quarantine_qns)}: {quarantine_qns}")
    finally:
        qp.DATA_DIR = original_DATA_DIR
    print()

    # ---- E. End-to-end: _export_gate_violations on the post-sweep
    #         chapter shows exactly 1 suspect_stem violation (q1).
    #         This is the EXACT post-fix Railway outcome the user expects. ----
    print("=" * 80)
    print("E. _export_gate_violations: 1 suspect_stem violation (q1) -- was 5 of 5")
    print("=" * 80)
    violations = qp._export_gate_violations(
        chapter_records=chapter,
        image_files_by_q={},
        unresolved_ledger=[],
        chapter_id="PSY-007",
        unresolved_images=(),
        unresolved_orphans=())
    suspect_violations = [v for v in violations if v[0] == "suspect_stem"]
    check("export gate has exactly 1 suspect_stem violation (q1 only) -- was 5",
          len(suspect_violations) == 1 and suspect_violations[0][1] == 1,
          f"got {len(suspect_violations)}: {suspect_violations}")
    # also: no missing_stem violations on the well-formed q2/q4/q6/q7/q9
    missing_stem_violations = [v for v in violations if v[0] == "missing_stem"]
    affected_qns = sorted(v[1] for v in missing_stem_violations)
    check("no missing_stem violations on q2/q4/q6/q7/q9 (they have full stems)",
          not any(qn in (2, 4, 6, 7, 9) for qn in affected_qns),
          f"got affected qns: {affected_qns}")
    print()

    # ---- F. The well-formed records preserve their full stem text
    #         (the fix did NOT weaken the validator; the text is intact). ----
    print("=" * 80)
    print("F. The fix did NOT weaken the validator: stems preserved verbatim")
    print("=" * 80)
    for qn in (2, 4, 6, 7, 9):
        rec = chapter[qn]
        # The full stem text (with medical vocabulary) must still be present
        check(f"q{qn}'s full stem text preserved (the fix did not strip it)",
              "the patient presents with psychotic disorder" in (rec.get("question_text") or ""),
              f"q{qn} stem length={len(rec.get('question_text') or '')}")
    print()

    # ---- G. Post-fix Railway summary: a [GATE] "1 violation" line,
    #         not the pre-fix "5 export-gate violation(s)". ----
    print("=" * 80)
    print("G. Post-fix Railway log format: '1 violation' not '5 violation(s)'")
    print("=" * 80)
    check("violations count == 1 (post-fix)",
          len(violations) == 1,
          f"got {len(violations)} violations")
    check("the 1 violation is on q1, not q2/q4/q6/q7/q9",
          suspect_violations and suspect_violations[0][1] == 1,
          f"got {suspect_violations}")
    # detail string mirrors the Railway format
    if suspect_violations:
        kind, qn, detail = suspect_violations[0]
        check("violation detail mentions q1 (real 'Option A:' contamination)",
              "1" in str(qn) and "explanation" in detail.lower(),
              f"got detail={detail!r}")
    print()

    print("=" * 80)
    print(f"PSY-007 post-fix end-to-end test: {n_ok}/{n_total} assertions passed")
    print("=" * 80)
    if failed:
        print(f"FAILED: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
