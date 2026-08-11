"""
test_psy007_stem_contamination_user_fix.py
=========================================

Regression test for the USER-FIX 2026-08-08: "yrr solutions ko questions se
verify nhi Krna h, agar koi suspicious h to Krna h rescue"

Background
----------
The contamination heuristic in `_stem_reject_reason` previously used
the record's own `solution_text` to flag stems that shared 80%+ tokens
with it. The user explicitly asked us to STOP this: medical terminology
IS shared between stems and solutions ("the patient", "schizophrenia",
"tardive dyskinesia", etc.), and the heuristic produced 5 false alarms
on the post-routing-fix Railway run (q2/q4/q6/q7/q9 of PSY-007), 4 of
which the CRITIQUE pass then proved were NOT contamination:

  - q2: CRITIQUE "corrected" (true contamination, model had stuffed
         solution prose as stem)
  - q4: CRITIQUE "confirmed correct despite flag" (false alarm)
  - q6, q7, q9: CRITIQUE "cannot_verify" (page coverage issue, NOT
         stem contamination -- the source page provided to CRITIQUE
         did not include the solution)

The new policy:
  1. STOP using solution text to verify stems (the user's exact ask)
  2. KEEP the explanation-opener check (catches ch7 q1-style
     "Option A: ..." real contamination)
  3. KEEP the phantom-record shape check (RUN-20, catches Q23-Q26
     class: stem+sol but no options/answer)
  4. Rescue pass now sends stem-only asks to QUESTION-side pages
     only (the routing fix), so the model can return its best
     extraction without the heuristic blocking it.

What this test proves (after the user fix):
  (A) The token-overlap heuristic NO LONGER fires on the q4/q6/q7/q9
      class (medical-term shared vocabulary -- the user's exact ask).
  (B) The explanation-opener check STILL fires for the ch7 q1-style
      real contamination (stems starting with "Option A:", "Solution
      to Question N:", etc.).
  (C) The phantom-record shape guard STILL fires for Q23-Q26 class.
  (D) The export gate has ZERO suspect_stem violations for a clean
      chapter (the goal of the fix).
  (E) The merge fix's FOREIGN guard is unchanged (still drops q23-26).

What this test does NOT do:
  - Run the real PSY-007 chapter (no PDF available in CI). Uses
    synthetic chapter_records.
  - Make any Gemini calls. The contamination heuristic is purely
    Python and deterministic.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Stub out heavy dependencies that qbank_pipeline imports at module load
# time. The test only exercises pure-Python pipeline functions.
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
# 1. Synthetic data mirroring the Railway run's Q1-Q10 chapter shape
# ============================================================================

# A "well-formed" record: stem and solution don't share 80%+ tokens.
# The Q-pass would extract a real stem; the S-pass would extract a
# real solution. Token overlap is incidental.
SHARED_MEDICAL_TOKENS = (
    "patient presents with acute psychotic disorder characterized by "
    "delusions hallucinations and disorganized speech the differential "
    "includes brief psychotic disorder schizophreniform disorder and "
    "schizophrenia duration symptoms is the key diagnostic criterion"
)


def build_q2_q4_q6_q7_q9_style(qn):
    """The q2/q4/q6/q7/q9 class: well-formed record (stem + 4 options +
    answer + solution) where stem and solution share medical vocabulary.
    Per the user's fix, this must NOT be quarantined.
    """
    return {
        "question_text": (f"A {qn}-year-old {('man' if qn % 2 else 'woman')} presents "
                          f"with symptoms consistent with the diagnosis. {SHARED_MEDICAL_TOKENS}. "
                          f"The most appropriate next step is"),
        "options": {"A": "Option A clinical answer",
                    "B": "Option B clinical answer",
                    "C": "Option C clinical answer",
                    "D": "Option D clinical answer"},
        "correct_option": "B",
        "solution_text": (f"Answer: B is correct because the {qn}-year-old "
                          f"{('man' if qn % 2 else 'woman')} has the diagnosis. "
                          f"{SHARED_MEDICAL_TOKENS}. "
                          f"Management involves appropriate intervention."),
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


def build_q1_q3_q5_q8_q10_style(qn):
    """A clean well-formed record: stem is genuinely a question, solution
    is genuinely an explanation. The user's fix says this passes through
    unchanged.
    """
    return {
        "question_text": f"Question {qn} stem: a unique clinical scenario for q{qn} that asks something specific.",
        "options": {"A": f"Option A for q{qn}",
                    "B": f"Option B for q{qn}",
                    "C": f"Option C for q{qn}",
                    "D": f"Option D for q{qn}"},
        "correct_option": "A",
        "solution_text": f"Answer: the answer to q{qn} involves a different mechanism than q{qn-1} -- separate vocabulary entirely.",
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


def build_real_contamination_q1_style():
    """ch7 q1-style REAL contamination: the stem literally STARTS with
    explanation prose ("Option A: ..." or "Solution to Question N: ...")
    This MUST still be caught by the explanation-opener check.
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


def build_phantom_q23_style(qn):
    """RUN-20 phantom class: stem+sol present, but NO options and NO
    correct_option. The real question is in another chapter; the
    "stem" was hallucinated by the run-19 critique pass.
    """
    return {
        "question_text": f"Question {qn} stem: hallucinated from surrounding solution prose by the model.",
        "options": None,
        "correct_option": None,
        "solution_text": f"Real answer to q{qn} lives in another chapter. This is a phantom record.",
    }


def build_chapter():
    """10 records: Q1/Q3/Q5/Q8/Q10 clean, Q2/Q4/Q6/Q7/Q9 well-formed
    with shared medical vocabulary, plus an extra Q1-style
    contamination record."""
    chapter = {
        1: build_real_contamination_q1_style(),
        2: build_q2_q4_q6_q7_q9_style(2),
        3: build_q1_q3_q5_q8_q10_style(3),
        4: build_q2_q4_q6_q7_q9_style(4),
        5: build_q1_q3_q5_q8_q10_style(5),
        6: build_q2_q4_q6_q7_q9_style(6),
        7: build_q2_q4_q6_q7_q9_style(7),
        8: build_q1_q3_q5_q8_q10_style(8),
        9: build_q2_q4_q6_q7_q9_style(9),
        10: build_q1_q3_q5_q8_q10_style(10),
    }
    return chapter


def build_chapter_with_phantom():
    """Same as build_chapter plus a phantom q23 record."""
    chapter = build_chapter()
    chapter[23] = build_phantom_q23_style(23)
    return chapter


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

    chapter = build_chapter()
    chapter_with_phantom = build_chapter_with_phantom()

    # ---- A. The token-overlap heuristic NO LONGER fires on the
    #         q4/q6/q7/q9 class (medical-term shared vocabulary). ----
    print("=" * 80)
    print("A. Token-overlap heuristic REMOVED -- q2/q4/q6/q7/q9 no longer quarantined")
    print("=" * 80)
    for qn in (2, 4, 6, 7, 9):
        rec = chapter[qn]
        reason = qp._stem_reject_reason(rec["question_text"], rec)
        check(f"_stem_reject_reason does NOT flag q{qn}'s well-formed stem "
              f"(the user's fix: 'yrr solutions ko questions se verify nhi Krna h')",
              reason is None,
              f"got {reason!r}")
    print()

    # ---- B. Clean records (Q3/Q5/Q8/Q10) are still not flagged. ----
    print("=" * 80)
    print("B. Clean records (Q3/Q5/Q8/Q10) are still not flagged")
    print("=" * 80)
    for qn in (3, 5, 8, 10):
        rec = chapter[qn]
        reason = qp._stem_reject_reason(rec["question_text"], rec)
        check(f"_stem_reject_reason does NOT flag q{qn}'s clean stem",
              reason is None,
              f"got {reason!r}")
    print()

    # ---- C. The explanation-opener check STILL fires for real
    #         contamination (ch7 q1 "Option A: ..." case). ----
    print("=" * 80)
    print("C. Explanation-opener check STILL fires for real contamination")
    print("=" * 80)
    rec_q1 = chapter[1]
    reason_q1 = qp._stem_reject_reason(rec_q1["question_text"], rec_q1)
    check("explanation-opener check fires for q1-style 'Option A: ...' stem",
          reason_q1 is not None and "explanation" in reason_q1.lower(),
          f"got {reason_q1!r}")

    # Also test the "Solution to Question N:" opener
    rec_sol_open = {
        "question_text": "Solution to Question 1: The answer is B. The patient has GAD.",
        "options": {"A": "A", "B": "B", "C": "C", "D": "D"},
        "correct_option": "B",
        "solution_text": "GAD is characterized by excessive worry.",
    }
    reason_sol = qp._stem_reject_reason(rec_sol_open["question_text"], rec_sol_open)
    check("explanation-opener check fires for 'Solution to Question N:' stem",
          reason_sol is not None and "explanation" in reason_sol.lower(),
          f"got {reason_sol!r}")
    print()

    # ---- D. Phantom-record shape guard (RUN-20) STILL fires for
    #         Q23-Q26 class. ----
    print("=" * 80)
    print("D. Phantom-record shape guard (RUN-20) STILL fires for Q23 class")
    print("=" * 80)
    rec_q23 = chapter_with_phantom[23]
    reason_q23 = qp._stem_reject_reason(rec_q23["question_text"], rec_q23)
    check("phantom-shape guard fires for q23 (stem+sol, no options/answer)",
          reason_q23 is not None and "phantom" in reason_q23.lower(),
          f"got {reason_q23!r}")
    print()

    # ---- E. chapter_integrity_sweep quarantines ONLY the real
    #         contamination (q1) and NOT the well-formed records.
    #         The 5 well-formed records (q2/q4/q6/q7/q9) are not
    #         quarantined because the medical-term token-overlap check
    #         was removed (the user's fix). ----
    print("=" * 80)
    print("E. chapter_integrity_sweep quarantines ONLY the real contamination (q1)")
    print("=" * 80)
    import tempfile
    tmp = tempfile.mkdtemp(prefix="psy007_user_fix_")
    original_DATA_DIR = qp.DATA_DIR
    qp.DATA_DIR = qp.Path(tmp)
    try:
        stats = {}
        forced = qp.chapter_integrity_sweep(chapter, {}, "PSY", 7, stats)
        quarantine_qns = [qn for qn, rec in chapter.items()
                          if rec.get("_stem_suspect_reason")]
        # q1 IS quarantined (real contamination, "Option A: ..." opener)
        # q2/q4/q6/q7/q9 are NOT quarantined (medical-term shared vocab, the fix)
        # q3/q5/q8/q10 are clean (no false alarm)
        check("q1 is quarantined (real contamination, opener check fires)",
              1 in quarantine_qns,
              f"q1 {'in' if 1 in quarantine_qns else 'NOT in'} {quarantine_qns}")
        check("q2/q4/q6/q7/q9 are NOT quarantined (medical-term shared vocab, the fix)",
              not any(qn in (2, 4, 6, 7, 9) for qn in quarantine_qns),
              f"got quarantined qns: {quarantine_qns}")
        check("q3/q5/q8/q10 are NOT quarantined (clean records)",
              not any(qn in (3, 5, 8, 10) for qn in quarantine_qns),
              f"got quarantined qns: {quarantine_qns}")
        check("exactly 1 record quarantined (q1 only) -- was 5 of 10 before the fix",
              len(quarantine_qns) == 1,
              f"got {len(quarantine_qns)}: {quarantine_qns}")
    finally:
        qp.DATA_DIR = original_DATA_DIR
    print()

    # ---- F. The export gate has 1 suspect_stem violation (q1) -- the
    #         well-formed records are NOT violations. ----
    print("=" * 80)
    print("F. Export gate: 1 suspect_stem violation (q1 real contamination) -- was 5")
    print("=" * 80)
    violations = qp._export_gate_violations(
        chapter_records=chapter,
        image_files_by_q={},
        unresolved_ledger=[],
        chapter_id="PSY-007",
        unresolved_images=(),
        unresolved_orphans=())
    suspect_violations = [v for v in violations if v[0] == "suspect_stem"]
    check("export gate has exactly 1 suspect_stem violation (q1 only) "
          "-- was 5 of 5 before the fix",
          len(suspect_violations) == 1 and suspect_violations[0][1] == 1,
          f"got {len(suspect_violations)}: {suspect_violations}")
    print()

    # ---- G. The merge fix's FOREIGN guard is unchanged for Q23-Q26. ----
    print("=" * 80)
    print("G. FOREIGN guard still drops Q23 (cross-chapter q_no)")
    print("=" * 80)
    foreign_item = {
        "q_no": 23,
        "question_text": None,
        "options": None,
        "correct_option": None,
        "solution_text": "Cross-chapter solution spill.",
        "tables": [],
        "has_figure_in_question": False,
        "has_figure_in_solution": False,
        "_prov": "S_PASS",
    }
    stats_foreign = {"duplicates_merged": 0, "conflicts": 0,
                     "foreign_chapter_qno_dropped": 0}
    new_records, skipped = qp.merge_question_records(
        chapter, [foreign_item], stats_foreign,
        known_chapter_qns=set(range(1, 11)), carry_q_nos=[])
    check("merge drops q23 as foreign (FOREIGN guard still fires)",
          23 not in new_records,
          f"q23 {'in' if 23 in new_records else 'NOT in'} records")
    check("foreign_chapter_qno_dropped counter == 1",
          stats_foreign["foreign_chapter_qno_dropped"] == 1,
          f"got {stats_foreign['foreign_chapter_qno_dropped']}")
    if skipped:
        check("dropped q23 tagged with _drop_reason='foreign_chapter_qno'",
              skipped[0].get("_drop_reason") == "foreign_chapter_qno",
              f"got {skipped[0].get('_drop_reason')!r}")
    print()

    print("=" * 80)
    print(f"stem-contamination user-fix test: {n_ok}/{n_total} assertions passed")
    print("=" * 80)
    if failed:
        print(f"FAILED: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
