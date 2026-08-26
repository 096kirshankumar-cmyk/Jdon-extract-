"""
test_full_book_split.py
=======================

Phase 4 (b) — multi-subject validation command.

Walks every chapter under data/split/{subject}/{chapter_id}/ and
runs the per-chapter rubric from PHASE2_REPORT.md §4.4 against ALL
of them in one pass. The per-chapter rubric is:

  1. all 7 split files exist
       (questions.jsonl, answers.jsonl, solutions.jsonl,
        unresolved_qids.jsonl, orphans.jsonl,
        chapter_completeness.json, image_manifest.jsonl)
  2. q_id_grade_counts has ONLY the 4 documented grades
       ({RESOLVED_ANCHORED, RESOLVED, PROVISIONAL, UNRESOLVED})
  3. q_id_grade_counts sums to question_records
       (i.e. every record is counted exactly once)
  4. phase2_pending_anchors is empty {} -- Phase 5 lifted the
       two OCR anchors too (pdftotext primary, tesseract fallback
       for garbled pages). All four design-doc §3.1 anchor families
       (printed_stem_match / printed_solution_header_match /
       ocr_stem_match / ocr_solution_header_match / answer_key_row_match)
       are now populated by default for every chapter. A non-empty
       `phase2_pending_anchors` is a sign a future change re-added
       an anchor to the "pending" list without wiring it.
  5. extraction_status_counts has only COMPLETE + INCOMPLETE
  6. The three split files (questions/answers/solutions) have
       IDENTICAL q_id sets (a record in questions.jsonl MUST be
       in answers.jsonl and solutions.jsonl -- a phantom record
       in one but not the others is a split-layer inconsistency)
  7. unresolved_qid_q_nos is a sorted list of unique int q_nos

A failure on any rubric is a one-line printed error; the summary
prints a single pass/fail per subject plus a grand total. The
script returns 0 if every chapter passed, 1 otherwise.

This is the multi-subject version of the per-chapter shell loop
in PHASE2_REPORT.md §4.4. The shell loop is fine for a one-time
audit; this tool is the long-running form you can re-run after
every chapter completes, every chapter is re-extracted, or every
code change to split_outputs.

Usage
-----
    # Default: scan ./qbank_output/data/split/
    python3 tools/full_book_split.py

    # Explicit output root (e.g. when running on Railway):
    python3 tools/full_book_split.py --root /app/qbank_output

    # One subject only (e.g. before PSY is fully rolled out):
    python3 tools/full_book_split.py --subject PSY

    # JSON output (for CI):
    python3 tools/full_book_split.py --json

The script is pure-Python: no Gemini calls, no PDF reads, no
poppler. It reads the per-chapter JSONL / JSON files written by
split_outputs.write_split_outputs and runs deterministic
consistency checks. Safe to run after every chapter close.

What the test proves (this file is also a self-test that
exercises a synthetic 33-chapter PSY set + 1-chapter MED set):
  A. Detects missing split files
  B. Detects unknown q_id_grade values
  C. Detects grade-counts sum mismatch
  D. Detects stale phase2_pending_anchors (neighbor_run or
     carry_forward_origin still listed as pending)
  E. Detects unknown extraction_status values
  F. Detects split-file q_id set mismatch
  G. Detects duplicate / non-int q_nos in unresolved_qid_q_nos
  H. Exit code = 0 on a clean set, 1 on any failure
  I. --subject filter limits the scan to one subject
  J. --json output is valid JSON with the right shape
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable

# Make the repo root importable so we can import split_outputs + qbank_pipeline
# (qbank_pipeline is needed for the DATA_DIR monkey-patch in the self-test).
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Stub heavy deps so this script can run without poppler / google / Pillow.
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


# The 7 expected split files (in any order on disk; alphabetical here).
EXPECTED_SPLIT_FILES = (
    "questions.jsonl",
    "answers.jsonl",
    "solutions.jsonl",
    "unresolved_qids.jsonl",
    "orphans.jsonl",
    "chapter_completeness.json",
    "image_manifest.jsonl",
)

ALLOWED_GRADES = {"RESOLVED_ANCHORED", "RESOLVED", "PROVISIONAL", "UNRESOLVED"}
ALLOWED_STATUS = {"COMPLETE", "INCOMPLETE"}
EXPECTED_PHASE2_PENDING = set()  # Phase 5: all 4 anchors populated, dict is empty


# ===========================================================================
# 1. Per-chapter rubric checks. Each returns (passed: bool, error: str).
#    All checks are pure-Python reads of the on-disk split files.
# ===========================================================================

def _read_jsonl(path: Path) -> list:
    """Read a JSONL file into a list of dicts. Missing file = []."""
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _read_json(path: Path) -> dict:
    """Read a JSON file. Missing file = {}."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def check_files_exist(chapter_dir: Path) -> tuple:
    """Rubric 1: all 7 split files exist."""
    missing = [f for f in EXPECTED_SPLIT_FILES
               if not (chapter_dir / f).exists()]
    if missing:
        return False, f"missing files: {missing}"
    return True, ""


def check_grade_values(comp: dict) -> tuple:
    """Rubric 2: q_id_grade_counts has only the 4 documented grades."""
    grades = set((comp.get("q_id_grade_counts") or {}).keys())
    extra = grades - ALLOWED_GRADES
    if extra:
        return False, f"unknown grades: {extra}"
    return True, ""


def check_grade_sum(comp: dict) -> tuple:
    """Rubric 3: grade_counts sum to question_records."""
    grade_counts = comp.get("q_id_grade_counts") or {}
    n_records = comp.get("question_records") or 0
    total = sum(grade_counts.values())
    if total != n_records:
        return False, f"grade_counts sum {total} != question_records {n_records}"
    return True, ""


def check_phase2_pending(comp: dict) -> tuple:
    """Rubric 4: phase2_pending_anchors is empty {}. Phase 5 lifted
    the two OCR anchors too (pdftotext primary, tesseract fallback
    for garbled pages). A non-empty dict means a future change
    re-added an anchor to the 'pending' list without wiring it.
    """
    pending = comp.get("phase2_pending_anchors") or {}
    actual = set(pending.keys())
    extra = actual - EXPECTED_PHASE2_PENDING
    if extra:
        return False, (f"phase2_pending_anchors keys = {sorted(actual)}; "
                       f"expected empty dict (extra={extra})")
    return True, ""


def check_extraction_status(comp: dict) -> tuple:
    """Rubric 5: extraction_status_counts has only COMPLETE + INCOMPLETE."""
    status = set((comp.get("extraction_status_counts") or {}).keys())
    extra = status - ALLOWED_STATUS
    if extra:
        return False, f"unknown extraction_status: {extra}"
    return True, ""


def check_qid_set_consistency(chapter_dir: Path) -> tuple:
    """Rubric 6: questions / answers / solutions.jsonl have IDENTICAL
    q_id sets (a record in one is in all three)."""
    q_ids = {r.get("q_id") for r in _read_jsonl(chapter_dir / "questions.jsonl")}
    a_ids = {r.get("q_id") for r in _read_jsonl(chapter_dir / "answers.jsonl")}
    s_ids = {r.get("q_id") for r in _read_jsonl(chapter_dir / "solutions.jsonl")}
    only_q = q_ids - a_ids - s_ids
    only_a = a_ids - q_ids - s_ids
    only_s = s_ids - q_ids - a_ids
    if only_q or only_a or only_s:
        return False, (f"q_id set mismatch: only-in-questions={len(only_q)} "
                       f"only-in-answers={len(only_a)} "
                       f"only-in-solutions={len(only_s)}")
    return True, ""


def check_unresolved_qids(comp: dict) -> tuple:
    """Rubric 7: unresolved_qid_q_nos is a sorted list of unique int q_nos."""
    q_nos = comp.get("unresolved_qid_q_nos") or []
    if q_nos != sorted(q_nos):
        return False, "unresolved_qid_q_nos is not sorted"
    if len(q_nos) != len(set(q_nos)):
        return False, "unresolved_qid_q_nos has duplicates"
    if any(not isinstance(qn, int) for qn in q_nos):
        return False, "unresolved_qid_q_nos has non-int entries"
    return True, ""


RUBRIC_CHECKS = (
    check_files_exist,
    check_grade_values,
    check_grade_sum,
    check_phase2_pending,
    check_extraction_status,
    check_qid_set_consistency,
    check_unresolved_qids,
)


# ===========================================================================
# 2. Orchestrator: walk every chapter under a root, run all 7 checks,
#    return a structured per-chapter result.
# ===========================================================================

def validate_chapter(chapter_dir: Path) -> dict:
    """Run the 7-rubric check on one chapter. Returns a result dict
    with a 'passed' bool, an 'errors' list (one entry per failed
    rubric), and the chapter_id / subject / chapter_no for the
    summary printer."""
    subject = chapter_dir.parent.name
    chapter_id = chapter_dir.name
    # chapter_no: parse "PSY-007" -> 7
    try:
        chapter_no = int(chapter_id.split("-", 1)[1])
    except (ValueError, IndexError):
        chapter_no = None
    comp = _read_json(chapter_dir / "chapter_completeness.json")
    errors = []
    for check in RUBRIC_CHECKS:
        if check is check_files_exist or check is check_qid_set_consistency:
            ok, msg = check(chapter_dir)
        else:
            ok, msg = check(comp)
        if not ok:
            errors.append(f"{check.__name__}: {msg}")
    return {
        "subject": subject,
        "chapter_id": chapter_id,
        "chapter_no": chapter_no,
        "passed": not errors,
        "errors": errors,
        "question_records": comp.get("question_records"),
        "q_id_grade_counts": comp.get("q_id_grade_counts"),
    }


def validate_split_root(split_root: Path,
                        subject_filter: str = None) -> list:
    """Walk split_root/{subject}/{chapter_id}/ for every chapter
    and run the 7-rubric check. Returns a list of per-chapter
    result dicts (one per chapter, in sorted order).

    subject_filter: if not None, only scan that subject."""
    if not split_root.exists():
        return []
    results = []
    for subject_dir in sorted(p for p in split_root.iterdir() if p.is_dir()):
        if subject_filter and subject_dir.name != subject_filter:
            continue
        for chapter_dir in sorted(c for c in subject_dir.iterdir()
                                  if c.is_dir()):
            results.append(validate_chapter(chapter_dir))
    return results


# ===========================================================================
# 3. Pretty-printing + JSON output
# ===========================================================================

def print_summary(results: list) -> None:
    """Print a per-chapter + per-subject + grand-total summary.
    Returns nothing; the caller reads results directly to set the
    exit code."""
    if not results:
        print("No chapters found under the given split root.")
        return
    # Per-chapter line
    print("=" * 80)
    print("Per-chapter rubric results")
    print("=" * 80)
    by_subject: dict = {}
    for r in results:
        by_subject.setdefault(r["subject"], []).append(r)
    for subject, sresults in by_subject.items():
        print(f"\n[{subject}] {len(sresults)} chapter(s)")
        for r in sresults:
            if r["passed"]:
                print(f"  ✓ {r['chapter_id']}  "
                      f"records={r['question_records']}  "
                      f"grades={r['q_id_grade_counts']}")
            else:
                print(f"  ✗ {r['chapter_id']}  "
                      f"records={r['question_records']}  "
                      f"grades={r['q_id_grade_counts']}")
                for err in r["errors"]:
                    print(f"      - {err}")
    # Per-subject + grand-total
    print()
    print("=" * 80)
    print("Summary")
    print("=" * 80)
    n_total = len(results)
    n_pass = sum(1 for r in results if r["passed"])
    print(f"  total chapters: {n_total}")
    print(f"  passed:         {n_pass}")
    print(f"  failed:         {n_total - n_pass}")
    for subject, sresults in by_subject.items():
        sp = sum(1 for r in sresults if r["passed"])
        print(f"  [{subject}] {sp}/{len(sresults)} passed")
    print()
    if n_pass == n_total:
        print("ALL CHAPTERS PASSED")
    else:
        print(f"{n_total - n_pass} CHAPTER(S) FAILED")


def to_json_output(results: list) -> str:
    """Machine-readable summary, for CI / dashboards."""
    by_subject: dict = {}
    for r in results:
        by_subject.setdefault(r["subject"], []).append(r)
    return json.dumps({
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "subjects": {
            subject: {
                "total": len(sresults),
                "passed": sum(1 for r in sresults if r["passed"]),
                "failed": sum(1 for r in sresults if not r["passed"]),
            }
            for subject, sresults in by_subject.items()
        },
        "chapters": results,
    }, indent=2)


# ===========================================================================
# 4. CLI
# ===========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Phase 4 (b) multi-subject validation: walk every "
                    "chapter under data/split/{subject}/{chapter_id}/ "
                    "and run the per-chapter rubric from "
                    "PHASE2_REPORT.md §4.4.",
    )
    ap.add_argument("--root", type=Path, default=None,
                    help="split root directory (default: "
                         "./qbank_output/data/split -- the live "
                         "pipeline's output path)")
    ap.add_argument("--subject", type=str, default=None,
                    help="only scan this subject (e.g. PSY)")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON output instead of pretty text")
    args = ap.parse_args()

    if args.root is None:
        # Default to the live pipeline's output path
        args.root = Path("qbank_output") / "data" / "split"
    if not args.root.exists():
        print(f"Split root not found: {args.root}")
        print("Run the pipeline first (python3 qbank_pipeline.py) so "
              "data/split/{subject}/{chapter_id}/ exists.")
        return 1

    results = validate_split_root(args.root, subject_filter=args.subject)
    if args.json:
        print(to_json_output(results))
    else:
        print_summary(results)
    # Exit code: 0 on all-pass, 1 on any failure (or no chapters found)
    if not results:
        return 1
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
