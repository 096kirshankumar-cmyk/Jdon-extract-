"""
test_psy007_merge_q23_q26_phantom.py
====================================

Regression test for the RUN-20 upstream fix (2026-08-08) of the PSY-007
Q23-Q26 phantom-record bug.

Bug:
  The S-pass on PSY-007 pages 105-107 returned 9 items including
  q_no = 23, 24, 25, 26. The headers "Solution to Question 23:" etc.
  ARE printed on these pages -- but they are the PREVIOUS chapter's
  (PSY-006) solution tail that falls inside PSY-007's page range, NOT
  in-chapter solutions. The pre-fix merge_question_records accepted
  every q_no Gemini returned, creating chapter_records[23..26] as
  phantom records whose real questions are in another chapter.

Fix (in qbank_pipeline.py):
  1. New helper: chapter_printed_question_qns(pdf_path, page_files)
     scans the chapter's text layer for "Question N:" / "1." / "N)" /
     "Q1." stem headers and returns the set of q_nos the text layer
     confirms are printed questions in this chapter.

  2. merge_question_records gained two optional keyword params:
     - known_chapter_qns: the union of (chapter_printed_question_qns)
       and the q_nos the Q-pass already produced in this chapter.
     - carry_q_nos: the q_nos currently open in carry-forward state
       (allowed even if not in known_chapter_qns; legitimate cross-
       page continuation).

  3. When known_chapter_qns is not None and an incoming item's q_no
     is NEITHER in the allowed set NOR in carry_q_nos, the item is
     dropped at the merge step (appended to the skipped list with
     stats["foreign_chapter_qno_dropped"]++). The caller routes the
     dropped item to orphans with reason="foreign_chapter_qno".

  4. The main pipeline call site (process_pdf) computes
     known_chapter_qns once per chapter from
     chapter_printed_question_qns(...) and threads it into every
     merge_question_records call. known_chapter_qns grows as the
     Q-pass-anchor set grows, so a Q1 from the Q-pass on window 1
     makes Q1 accepted on every later window even if the text layer
     is garbled.

  5. _stem_reject_reason gained a phantom-record shape guard: a
     non-empty question_text + non-empty solution_text + empty
     options + no correct_option is rejected (real MCQ records have
     options + answer). This catches the run-19 critique's
     hallucinated stems on phantom records.

This test:
  - Builds a synthetic chapter_records + 4 S-pass items for Q23-Q26
    that mirror the real Railway run's exact shape (with the
    critique-hallucinated question_text + solution-only fragment).
  - Calls merge_question_records with the same parameters the
    pipeline would (known_chapter_qns={1..10} from the text layer +
    Q-pass anchors, carry_q_nos=[], stats=...).
  - Asserts the 4 phantom items are in `skipped` and NOT in
    chapter_records.
  - Asserts the stats["foreign_chapter_qno_dropped"] counter is 4.
  - Also asserts the _stem_reject_reason phantom-record shape guard
    catches the same case (defense-in-depth).
  - Pre-fix this test fails (chapter_records gets 14 records, not
    10; skipped is empty). Post-fix it passes.

Run:
    cd /path/to/Json-extract
    python3 tools/test_psy007_merge_q23_q26_phantom.py

Exit 0 on success, 1 on any assertion failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Import the pipeline module WITHOUT triggering google.generativeai (which
# is not installed in the test env). The module imports google.generativeai
# at top level, so we need to stub it before importing.
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

# Now we can import the pipeline module.
import qbank_pipeline as qp


# ============================================================================
# 1. Build the test data: 10 Q1-Q10 (genuine) + 4 Q23-Q26 S-pass items
#    (the phantom-producing items from the real Railway run).
# ============================================================================

def build_chapter_records_after_qpass():
    """chapter_records dict AFTER the Q-pass on pages 100-104 has
    completed for PSY-007 in the real run -- Q1-Q10 are present with
    real content, the Q-pass returned no other q_nos."""
    records = {}
    for qn in range(1, 11):
        records[qn] = {
            "question_text": f"PSY-007 question {qn} stem (verbatim from the page).",
            "options": {
                "A": f"Option A for q{qn}",
                "B": f"Option B for q{qn}",
                "C": f"Option C for q{qn}",
                "D": f"Option D for q{qn}",
            },
            "correct_option": "A",
            "solution_text": f"PSY-007 solution {qn} (verbatim from the page).",
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
    return records


def build_s_pass_items_105_107():
    """The 9 items the S-pass on pages 105-107 returned in the real
    run, including the 4 phantom Q23-Q26 items whose only anchor is a
    'Solution to Question N:' header on PSY-007's pages, but whose
    real question is in a different chapter (PSY-008 onward)."""
    items = []
    # 5 real solutions for Q5-Q10 (Q1-Q4 solutions were on pages 100-104)
    for qn in (5, 6, 7, 8, 9):
        items.append({
            "q_no": qn,
            "question_text": None,
            "options": None,
            "correct_option": None,
            "solution_text": f"PSY-007 solution for q{qn} (verbatim from page 105-107).",
            "tables": [],
            "has_figure_in_question": False,
            "has_figure_in_solution": False,
            "_prov": "S_PASS",
        })
    # Q10's solution was on the S-pass window 100-104 (Q_PASS batch);
    # on the S-pass window 105-107 the S-pass can re-emit a duplicate
    # of Q10 with the rest of the solution. The real run saw this as
    # the [RETRY] round 1 fill: "q10[solution]" was missing before
    # the retry re-fetched it.
    items.append({
        "q_no": 10,
        "question_text": None,
        "options": None,
        "correct_option": None,
        "solution_text": "PSY-007 solution for q10 (continued, missing tail on 100-104).",
        "tables": [],
        "has_figure_in_question": False,
        "has_figure_in_solution": False,
        "_prov": "S_PASS",
    })
    # 4 phantom Q23-Q26 items. These are the bug.
    for qn in (23, 24, 25, 26):
        items.append({
            "q_no": qn,
            "question_text": None,
            "options": None,
            "correct_option": None,
            "solution_text": (
                f"PSY-007 page actually contains 'Solution to Question {qn}: ...' "
                f"prose, but the question itself is in another chapter's pages."
            ),
            "tables": [],
            "has_figure_in_question": False,
            "has_figure_in_solution": False,
            "_prov": "S_PASS",
        })
    return items


# ============================================================================
# 2. The chapter's known_chapter_qns: text-layer headers + Q-pass anchors.
# ============================================================================

def build_known_chapter_qns_text_layer():
    """The text-layer scan finds '1.' / '2.' / ... '10.' as the question
    stem headers on PSY-007's pages 100-104. The 'Solution to Question
    23:' header on page 106 is detected by chapter_printed_solution_qns
    (used elsewhere) but NOT by chapter_printed_question_qns (which
    looks for question stem headers, not solution headers). So the
    text-layer's question-stem set is {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}.

    In the real run, the Q-pass on pages 100-104 returns 10 items
    (Q1-Q10). The Q-pass on pages 105-107 returns 0 items. The
    chapter's Q-pass-anchor set is therefore {1..10}."""
    return set(range(1, 11))


# ============================================================================
# 3. Run merge_question_records with the upstream fix and assert.
# ============================================================================

def test_foreign_chapter_qno_dropped_at_merge_step():
    print("\n=== RUN-20 upstream fix: foreign-chapter q_no drop at merge step ===\n")

    chapter_records = build_chapter_records_after_qpass()
    s_pass_items = build_s_pass_items_105_107()
    known_chapter_qns = build_known_chapter_qns_text_layer()
    carry_q_nos = []
    stats = {"duplicates_merged": 0, "conflicts": 0,
             "foreign_chapter_qno_dropped": 0}

    # 10 records before the merge
    assert len(chapter_records) == 10, f"setup: expected 10 records, got {len(chapter_records)}"
    assert sorted(chapter_records.keys()) == list(range(1, 11)), \
        f"setup: expected Q1-Q10, got {sorted(chapter_records.keys())}"

    new_records, skipped = qp.merge_question_records(
        chapter_records, s_pass_items, stats,
        known_chapter_qns=known_chapter_qns, carry_q_nos=carry_q_nos,
    )

    n_ok = 0
    n_total = 0
    failed = []

    def check(label, cond, detail=""):
        nonlocal n_ok, n_total
        n_total += 1
        if cond:
            n_ok += 1
            print(f"  ok:   {label}{(' (' + detail + ')') if detail else ''}")
            return True
        print(f"  FAIL: {label}{(' (' + detail + ')') if detail else ''}")
        failed.append(label)
        return False

    # The 10 Q1-Q10 records must still be in chapter_records.
    check("chapter_records still has exactly 10 records (Q1-Q10) after the S-pass merge",
          len(new_records) == 10,
          f"got {len(new_records)}: {sorted(new_records.keys())}")
    check("chapter_records keys are exactly {1..10} (no Q23-Q26)",
          sorted(new_records.keys()) == list(range(1, 11)),
          f"got {sorted(new_records.keys())}")

    # Q5-Q10 must have their solution_text populated from the S-pass.
    # The S-pass items in build_s_pass_items_105_107 use "PSY-007 solution
    # for qN" wording; the Q10 item is a continuation fragment. The merge
    # overwrites the existing solution_text with the new S-pass text, so
    # we check for the S-pass item's wording, not the seed's "verbatim"
    # marker (which only existed in the pre-merge Q1-Q4 records).
    for qn in (5, 6, 7, 8, 9, 10):
        rec = new_records.get(qn)
        check(f"Q{qn} has its S-pass solution_text",
              rec is not None and rec.get("solution_text") and f"PSY-007 solution for q{qn}" in rec.get("solution_text", ""),
              f"solution_text starts: {rec.get('solution_text', '')[:60]!r}" if rec else "missing record")

    # Q1-Q4 must be untouched by the S-pass merge (they had solutions
    # from the earlier 100-104 S-pass window; the new items don't carry
    # a duplicate Q1-Q4, so the existing solution_text is preserved).
    for qn in (1, 2, 3, 4):
        rec = new_records.get(qn)
        check(f"Q{qn} solution_text preserved from the earlier S-pass",
              rec is not None and "PSY-007 solution" in rec.get("solution_text", ""),
              f"solution_text: {rec.get('solution_text', '')[:60]!r}" if rec else "missing record")

    # The 4 phantom Q23-Q26 items must be in `skipped`, NOT in
    # chapter_records, NOT in any of the 10 Q1-Q10 records.
    skipped_qns = []
    for it in skipped:
        try:
            skipped_qns.append(int(it.get("q_no")))
        except (TypeError, ValueError):
            continue
    check("All 4 Q23-Q26 phantom items are in `skipped`",
          sorted(skipped_qns) == [23, 24, 25, 26],
          f"got {sorted(skipped_qns)}")

    # The 5 legitimate Q5-Q9 + Q10 items are NOT in `skipped` (they merged).
    false_skipped = [qn for qn in skipped_qns if qn in (5, 6, 7, 8, 9, 10)]
    check("Q5-Q10 S-pass items merged (none in `skipped`)",
          not false_skipped,
          f"unexpectedly in skipped: {false_skipped}")

    # stats counter
    check("stats['foreign_chapter_qno_dropped'] == 4",
          stats.get("foreign_chapter_qno_dropped") == 4,
          f"got {stats.get('foreign_chapter_qno_dropped')}")

    # Final shape: 10 Q1-Q10 with options+answer+question+solution
    for qn in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
        rec = new_records.get(qn)
        check(f"Q{qn} has stem + options + answer + solution (well-formed)",
              rec is not None
              and rec.get("question_text")
              and rec.get("options") and len(rec["options"]) == 4
              and rec.get("correct_option")
              and rec.get("solution_text"),
              "well-formed" if rec else "missing record")

    print(f"\n=== merge-question-records upstream-fix test: {n_ok}/{n_total} assertions passed ===")
    if failed:
        print(f"FAILED: {failed}")
        return 1
    return 0


# ============================================================================
# 4. The phantom-record shape guard in _stem_reject_reason.
# ============================================================================

def test_phantom_record_shape_guard():
    print("\n=== RUN-20 phantom-record shape guard in _stem_reject_reason ===\n")

    # Case 1: a phantom Q23-style record (only question_text + solution_text;
    # options={}, correct_option=None). The hallucinated question_text
    # is question-shaped and the solution is non-empty. PRE-FIX this
    # passed; POST-FIX it must reject.
    phantom = {
        "question_text": (
            "Hallucinated stem for q23 (the critique pass derived this from "
            "the solution prose on the page; the real Q23 is in a different chapter)."
        ),
        "options": {},
        "correct_option": None,
        "solution_text": (
            "PSY-007 page actually contains 'Solution to Question 23: ...' "
            "prose, but the question itself is in another chapter's pages."
        ),
    }
    reason = qp._stem_reject_reason(phantom["question_text"], phantom)
    if reason is None:
        print(f"  FAIL: _stem_reject_reason did not catch the phantom-record shape")
        return 1
    if "phantom" not in reason.lower() and "shape" not in reason.lower():
        print(f"  FAIL: _stem_reject_reason caught but with wrong reason: {reason!r}")
        return 1
    print(f"  ok:   _stem_reject_reason catches the phantom shape: {reason!r}")

    # Case 2: a real Q1-style record (stem + options + answer + solution).
    # POST-FIX it must NOT be rejected -- the guard is shape-only, not
    # content-only.
    real = {
        "question_text": "A real question stem with 4 options and an answer.",
        "options": {"A": "x", "B": "y", "C": "z", "D": "w"},
        "correct_option": "A",
        "solution_text": "A real solution that may share tokens with the stem if the solution restates the question.",
    }
    reason = qp._stem_reject_reason(real["question_text"], real)
    if reason is not None:
        print(f"  FAIL: _stem_reject_reason incorrectly rejected a real record: {reason!r}")
        return 1
    print(f"  ok:   _stem_reject_reason does NOT reject a well-formed record")

    # Case 3: a real solution-only record (the S-pass fragment of Q1
    # where question_text is None). POST-FIX it must NOT be rejected --
    # the guard only fires when t (the question_text) is non-empty.
    solution_only = {
        "question_text": None,
        "options": {},
        "correct_option": None,
        "solution_text": "Solution prose for Q1.",
    }
    reason = qp._stem_reject_reason(solution_only["question_text"], solution_only)
    if reason is not None:
        print(f"  FAIL: _stem_reject_reason incorrectly rejected a solution-only record: {reason!r}")
        return 1
    print(f"  ok:   _stem_reject_reason does NOT reject a solution-only record (question_text=None)")

    # Case 4: a record with options but no answer (one of the gaps the
    # targeted_retry pass is supposed to fill). The phantom-shape guard
    # only fires when BOTH options-empty AND no-answer are present;
    # this case has options so it passes through to other checks.
    options_only = {
        "question_text": "A real question stem with 4 options, but answer is not yet extracted.",
        "options": {"A": "x", "B": "y", "C": "z", "D": "w"},
        "correct_option": None,
        "solution_text": "Solution prose.",
    }
    # The shape guard does NOT fire (options are present). Other guards
    # may or may not fire depending on token overlap. We only check that
    # the shape guard itself doesn't fire.
    # The shape guard is `if rec and t and sol: ... if opts_empty and no_answer:`;
    # since opts is not empty, opts_empty is False, so the guard returns None.
    # But other guards (the explanation-opener or token-containment) might
    # still fire. We accept any non-"phantom-record shape" reason as OK.
    reason = qp._stem_reject_reason(options_only["question_text"], options_only)
    if reason is not None and "phantom" in reason.lower():
        print(f"  FAIL: shape guard fired on a record with options: {reason!r}")
        return 1
    print(f"  ok:   shape guard does NOT fire on a record with options present (reason: {reason!r})")

    print("\n=== phantom-record shape guard test: 4/4 assertions passed ===")
    return 0


# ============================================================================
# 5. Run all tests.
# ============================================================================

def main():
    rc1 = test_foreign_chapter_qno_dropped_at_merge_step()
    rc2 = test_phantom_record_shape_guard()
    return 0 if (rc1 == 0 and rc2 == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
