"""
test_split_psy007_real_case.py
==============================

Regression test for the integration bug exposed by the real PSY-007
Railway run on 2026-08-08.

Bug summary (per user inspection of the source PDF):
  - PSY-007 "Other Psychotic Disorders" contains EXACTLY 10 questions
    (Q1-Q10) and an answer key for Q1-Q10.
  - The pipeline produced 14 records: Q1-Q10 PLUS Q23-Q26.
  - The Q23-Q26 records had:
      * question_text present
      * solution_text present
      * options = []
      * correct_options = []
    The "Solution to Question 23:" / "24" / "25" / "26" headers ARE
    printed on the page (they are cross-chapter solution references
    that fell inside PSY-007's page range), so the S-pass + the
    critique pass produced records for them. But the QUESTION text
    and OPTIONS for Q23-Q26 are NOT on PSY-007's pages -- they live
    in some other chapter. The critique pass hallucinated a
    question_text from the surrounding solution prose.

What the test reproduces:
  - 10 genuine Q1-Q10 records (full content from the page)
  - 4 phantom Q23-Q26 records that survived the existing
    extraction because:
      * they have a printed_solution_header_match anchor (the
        "Solution to Question 23:" header on the page is real)
      * they have a non-empty question_text (hallucinated by critique)
      * they have a non-empty solution_text (the real solution prose)
      * options = [] and correct_option = None
  - Expected after split_outputs.reconcile_qids:
      * kept (graded): Q1-Q10 = 10 records
      * unresolved: Q23-Q26 = 4 records with reason
        "missing_question_for_solution"
      * questions.jsonl: 10 records (Q1-Q10)
      * answers.jsonl: 10 records (Q1-Q10)
      * solutions.jsonl: 10 records (Q1-Q10)
      * unresolved_qids.jsonl: 4 records (Q23-Q26)
      * chapter_completeness.json.unresolved_qid_count = 4
      * chapter_completeness.json.q_id_grade_counts sums to 10

The test asserts the bug is fixed. Before the fix, the Q23-Q26
records grade as RESOLVED (printed_solution_header_match anchor)
and survive as graded questions, so:
  - questions.jsonl has 14 records
  - unresolved_qids.jsonl has 0 records
  - chapter_completeness.json.unresolved_qid_count = 0

That mismatch is the bug. The test asserts the CORRECT behavior.

Run:
    cd /home/user/Json-extract
    python3 tools/test_split_psy007_real_case.py

Exits 0 on success, 1 on any assertion failure. Output is written to
tempfile.mkdtemp() and NOT under the repo.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import split_outputs


# ----------------------------------------------------------------------------
# 1. Build the chapter_records dict that mirrors what process_pdf produces
#    for PSY-007 in the real Railway run.
#
#    For Q1-Q10: full content (stem, options, answer, solution, all the
#    real anchors). These are legitimate questions.
#
#    For Q23-Q26: solution_text + question_text are populated (the
#    critique pass hallucinated a stem from the surrounding solution
#    prose), options is empty, correct_option is None. The
#    "Solution to Question 23:" header is on the page, so the
#    printed_solution_header_match anchor IS present. No stem header
#    is on the page, so printed_stem_match is absent. No answer-key
#    row for Q23 is on PSY-007's page range, so answer_key_row_match
#    is absent.
# ----------------------------------------------------------------------------

def build_psy007_chapter_records():
    records = {}

    # Q1-Q10: genuine records with full content.
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

    # Q23-Q26: phantom records the critique pass produced.
    # The signature that triggers the bug:
    #   - printed_solution_header_match anchor IS present (the page
    #     does print "Solution to Question 23:" etc.)
    #   - printed_stem_match is ABSENT (the question text for Q23 is
    #     in a different chapter, not on PSY-007's page range)
    #   - answer_key_row_match is ABSENT (PSY-007's answer key only
    #     covers Q1-Q10)
    #   - question_text is non-empty (the critique pass hallucinated a
    #     stem from the surrounding solution prose -- this is the
    #     "the available pages contain solution text" outcome)
    #   - solution_text is non-empty (the real solution prose)
    #   - options = {} (the question's options are in a different chapter)
    #   - correct_option = None (the answer is in a different chapter)
    for qn in (23, 24, 25, 26):
        records[qn] = {
            # Critique-hallucinated stem: a sentence the model derived
            # from the solution prose. It is NOT the real question text.
            "question_text": (
                f"Hallucinated stem for q{qn} (the critique pass derived this "
                f"from the solution prose on the page; the real Q{qn} is in "
                f"a different chapter)."
            ),
            "options": {},
            "correct_option": None,
            "solution_text": (
                f"PSY-007 page actually contains 'Solution to Question {qn}: ...' "
                f"prose, but the question itself is in another chapter's pages."
            ),
            "tables": [],
            "has_figure_in_question": False,
            "has_figure_in_solution": False,
            "_prov": {
                "question_text": "CRITIQUE",  # tagged by the run-19 critique
                "solution_text": "S_PASS",
            },
        }

    return records


def build_qn_source_pages():
    """Q1-Q10 sit on pages 100-104. Q23-Q26's 'Solution to Question 23:'
    headers are on the same page range (105-107 is the solutions
    section). The phantom records have NO question_text page because
    their real questions are in a different chapter."""
    return {
        **{qn: {100, 101, 102, 103, 104} for qn in range(1, 11)},
        23: {106},  # the "Solution to Question 23:" header is on p106
        24: {106},
        25: {107},
        26: {107},
    }


# ----------------------------------------------------------------------------
# 2. Run the new step and assert the correct outcome.
# ----------------------------------------------------------------------------

def run_test():
    out_root = Path(tempfile.mkdtemp(prefix="split_psy007_real_case_"))
    chapter_id = "PSY-007"
    subject = "PSY"
    chapter_no = 7

    chapter_records = build_psy007_chapter_records()
    qn_source_pages = build_qn_source_pages()
    image_files_by_q = {}  # no images in this regression case
    orphans = []           # no orphans in this regression case
    chapter_unresolved_images = []

    # Patch _harvest_anchors to inject the synthetic anchors that the
    # text layer would have produced for this exact case. The grader
    # itself is untouched -- it still runs on whatever anchors are
    # present, so this test exercises the actual grader path.
    printed_anchors = {}
    # Q1-Q10: every anchor type is present (stem, answer-key, solution).
    # We use one anchor type (stem) so the grader returns RESOLVED
    # (single-anchor case). That's the most common case for an
    # extracted question; the bug is independent of whether the
    # question is RESOLVED vs RESOLVED_ANCHORED.
    for qn in range(1, 11):
        printed_anchors[qn] = {
            "printed_stem_match": {"page": 100 + (qn - 1) // 3,
                                   "header_text": f"{qn}."},
        }
    # Q23-Q26: ONLY printed_solution_header_match. This is the
    # signature that triggers the bug -- the S-pass found the
    # "Solution to Question 23:" header on the page, so the
    # anchor is populated, but no stem or answer-key row for these
    # q_nos is on PSY-007's pages.
    for qn in (23, 24, 25, 26):
        printed_anchors[qn] = {
            "printed_solution_header_match": {
                "page": 105 + (qn - 23) // 2,
                "header_text": f"Solution to Question {qn}:",
            },
        }
    def _synthetic_harvest(*args, **kwargs):
        return {qn: dict(anchors) for qn, anchors in printed_anchors.items()}
    split_outputs._harvest_anchors = _synthetic_harvest

    reconciled = split_outputs.reconcile_qids(
        chapter_records, qn_source_pages, pdf_path=None, page_files=None,
        subject=subject, chapter_no=chapter_no)

    # The fix must:
    #   1. Route Q23-Q26 to unresolved (NOT kept).
    #   2. Mark them with reason="missing_question_for_solution".
    completeness = split_outputs.write_split_outputs(
        chapter_id=chapter_id, subject=subject, chapter_no=chapter_no,
        chapter_records=reconciled["kept"],
        image_files_by_q=image_files_by_q,
        qn_source_pages=qn_source_pages,
        orphans=orphans,
        chapter_unresolved_images=chapter_unresolved_images,
        pdf_path=None, page_files=None,
        reconciled=reconciled,
        output_root=out_root,
    )

    chapter_dir = out_root / "split" / "PSY" / "PSY-007"
    questions = _read_jsonl(chapter_dir / "questions.jsonl")
    answers = _read_jsonl(chapter_dir / "answers.jsonl")
    solutions = _read_jsonl(chapter_dir / "solutions.jsonl")
    unresolved = _read_jsonl(chapter_dir / "unresolved_qids.jsonl")

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

    print(f"\n=== PSY-007 real-case regression test ({n_total} assertions so far) ===\n")

    # --- The bug: pre-fix, the Q23-Q26 records were kept. ---
    check(
        "kept records = 10 (Q1-Q10), NOT 14",
        len(reconciled["kept"]) == 10,
        f"got {len(reconciled['kept'])}: {sorted(reconciled['kept'].keys())}",
    )
    check(
        "unresolved records = 4 (Q23-Q26), NOT 0",
        len(reconciled["unresolved"]) == 4,
        f"got {len(reconciled['unresolved'])}: {sorted(reconciled['unresolved'].keys())}",
    )

    # --- Q23-Q26 are explicitly routed to unresolved_qids.jsonl ---
    q_ids_in_unresolved = sorted(r["q_id"] for r in unresolved)
    check(
        "unresolved_qids.jsonl contains all 4 phantom Q23-Q26",
        q_ids_in_unresolved == [
            "PSY-007-023", "PSY-007-024", "PSY-007-025", "PSY-007-026",
        ],
        f"got {q_ids_in_unresolved}",
    )

    # --- The 4 unresolved entries use the Case 2 reason ---
    reasons = [r.get("reason") for r in unresolved]
    check(
        "all 4 unresolved entries have reason='missing_question_for_solution'",
        all(r == "missing_question_for_solution" for r in reasons),
        f"reasons={reasons}",
    )

    # --- questions.jsonl has exactly 10 records, NO Q23-Q26 ---
    q_ids_in_questions = sorted(r["q_id"] for r in questions)
    check(
        "questions.jsonl has 10 records (Q1-Q10 only)",
        len(questions) == 10,
        f"got {len(questions)}: {q_ids_in_questions}",
    )
    check(
        "Q23-Q26 are ABSENT from questions.jsonl",
        not any(qn_id in q_ids_in_questions for qn_id in
                ("PSY-007-023", "PSY-007-024", "PSY-007-025", "PSY-007-026")),
        f"questions.jsonl q_ids: {q_ids_in_questions}",
    )

    # --- answers.jsonl: 10 records, NO Q23-Q26 ---
    q_ids_in_answers = sorted(r["q_id"] for r in answers)
    check(
        "answers.jsonl has 10 records (Q1-Q10 only)",
        len(answers) == 10,
        f"got {len(answers)}: {q_ids_in_answers}",
    )
    check(
        "Q23-Q26 are ABSENT from answers.jsonl",
        not any(qn_id in q_ids_in_answers for qn_id in
                ("PSY-007-023", "PSY-007-024", "PSY-007-025", "PSY-007-026")),
        f"answers.jsonl q_ids: {q_ids_in_answers}",
    )

    # --- solutions.jsonl: 10 records, NO Q23-Q26 ---
    q_ids_in_solutions = sorted(r["q_id"] for r in solutions)
    check(
        "solutions.jsonl has 10 records (Q1-Q10 only)",
        len(solutions) == 10,
        f"got {len(solutions)}: {q_ids_in_solutions}",
    )
    check(
        "Q23-Q26 are ABSENT from solutions.jsonl",
        not any(qn_id in q_ids_in_solutions for qn_id in
                ("PSY-007-023", "PSY-007-024", "PSY-007-025", "PSY-007-026")),
        f"solutions.jsonl q_ids: {q_ids_in_solutions}",
    )

    # --- chapter_completeness.json is accurate ---
    check(
        "chapter_completeness.json.question_records == 10",
        completeness["question_records"] == 10,
        f"got {completeness['question_records']}",
    )
    check(
        "chapter_completeness.json.answer_records == 10",
        completeness["answer_records"] == 10,
        f"got {completeness['answer_records']}",
    )
    check(
        "chapter_completeness.json.solution_records == 10",
        completeness["solution_records"] == 10,
        f"got {completeness['solution_records']}",
    )
    check(
        "chapter_completeness.json.unresolved_qid_count == 4",
        completeness["unresolved_qid_count"] == 4,
        f"got {completeness['unresolved_qid_count']}",
    )
    check(
        "chapter_completeness.json.unresolved_qid_q_nos == [23, 24, 25, 26]",
        completeness["unresolved_qid_q_nos"] == [23, 24, 25, 26],
        f"got {completeness['unresolved_qid_q_nos']}",
    )

    # --- q_id_grade_counts only counts the 10 kept records ---
    grade_total = sum(completeness["q_id_grade_counts"].values())
    check(
        "q_id_grade_counts sums to 10 (kept records only, not the 4 unresolved)",
        grade_total == 10,
        f"got {grade_total}: {completeness['q_id_grade_counts']}",
    )

    # --- No silent invention: the Q23-Q26 q_ids in unresolved_qids.jsonl ---
    # are the SAME q_ids the pipeline generated; we don't get to invent
    # them at a different number. The 14-record raw -> 10-graded split
    # is exactly what the user expects.
    check(
        "no Q23-Q26 in any kept file, and they are explicitly named in unresolved_qids.jsonl",
        all(("PSY-007-0" + str(qn)) in [r["q_id"] for r in unresolved]
            for qn in (23, 24, 25, 26)),
        "all four phantom q_ids explicitly listed",
    )

    print(f"\n=== {n_ok}/{n_total} assertions passed ===")
    if failed:
        print(f"FAILED: {failed}")
    return 0 if not failed else 1


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


if __name__ == "__main__":
    sys.exit(run_test())
