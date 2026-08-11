"""
test_phase4_full_book_split.py
==============================

Phase 4 (b) self-test: builds a synthetic 33-chapter PSY set + a
1-chapter MED set under /tmp/phase4_full_book_test/, runs the
multi-subject validation command
(`tools/full_book_split.py`) against the synthetic root, and
proves every rubric check fires correctly.

The test covers:
  A. Build 34 chapters of clean split data (all 7 files per chapter,
     valid q_id_grade_counts, valid phase2_pending_anchors,
     matching q_id sets across the three split files).
  B. Run the validator on the clean set -- every chapter passes.
  C. Inject 5 different failure modes (one per chapter) and prove
     each is caught by the right rubric:
       * chapter C1: missing chapter_completeness.json
       * chapter C2: unknown grade value "HALLUCINATED" in
         q_id_grade_counts
       * chapter C3: grade_counts sum != question_records
       * chapter C4: phase2_pending_anchors still lists
         "neighbor_run" (stale -- Phase 2 lifted it)
       * chapter C5: questions.jsonl has a q_id the others don't
         (split-file q_id set mismatch)
  D. --subject filter limits the scan to one subject.
  E. --json output is valid JSON with the right shape (total,
     passed, failed, subjects, chapters).
  F. Exit code is 1 on any failure, 0 on all-pass.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import qbank_pipeline as qp
import split_outputs


TOOL = REPO_ROOT / "tools" / "full_book_split.py"


# ===========================================================================
# Fixtures: build a synthetic 33-chapter PSY set + 1-chapter MED set.
# ===========================================================================

def _build_clean_chapter(split_root: Path, subject: str, chapter_no: int,
                         n_records: int) -> Path:
    """Build one chapter with all 7 split files populated correctly.
    Returns the chapter directory path."""
    chapter_id = f"{subject}-{chapter_no:03d}"
    chapter_dir = split_root / subject / chapter_id
    chapter_dir.mkdir(parents=True, exist_ok=True)
    # Per-record data
    q_rows, a_rows, s_rows = [], [], []
    for qn in range(1, n_records + 1):
        q_id = f"{subject}-{chapter_no:03d}-{qn:03d}"
        q_rows.append({
            "q_id": q_id, "chapter_id": chapter_id, "subject": subject,
            "chapter_no": chapter_no, "q_no": qn,
            "q_id_grade": "RESOLVED_ANCHORED",
            "q_no_anchors": {},
            "question_text": f"Stem for q{qn}?",
            "options": [
                {"id": "A", "text": "opt A", "images": []},
                {"id": "B", "text": "opt B", "images": []},
                {"id": "C", "text": "opt C", "images": []},
                {"id": "D", "text": "opt D", "images": []},
            ],
            "question_images": [], "tables": [],
            "source_pages": [100 + qn], "extraction_status": "COMPLETE",
        })
        a_rows.append({
            "q_id": q_id, "chapter_id": chapter_id, "subject": subject,
            "chapter_no": chapter_no, "q_no": qn,
            "correct_option": "A", "correct_option_prov": "A_PASS",
            "q_id_grade": "RESOLVED_ANCHORED",
            "q_no_anchors": {},
            "source_pages": [100 + qn], "extraction_status": "COMPLETE",
        })
        s_rows.append({
            "q_id": q_id, "chapter_id": chapter_id, "subject": subject,
            "chapter_no": chapter_no, "q_no": qn,
            "solution_text": f"Solution for q{qn}.", "tables": [],
            "solution_images": [], "solution_prov": "S_PASS",
            "q_id_grade": "RESOLVED_ANCHORED",
            "q_no_anchors": {},
            "source_pages": [100 + qn], "extraction_status": "COMPLETE",
        })
    split_outputs._atomic_jsonl_write(chapter_dir / "questions.jsonl", q_rows)
    split_outputs._atomic_jsonl_write(chapter_dir / "answers.jsonl", a_rows)
    split_outputs._atomic_jsonl_write(chapter_dir / "solutions.jsonl", s_rows)
    split_outputs._atomic_jsonl_write(chapter_dir / "unresolved_qids.jsonl", [])
    split_outputs._atomic_jsonl_write(chapter_dir / "orphans.jsonl", [])
    split_outputs._atomic_jsonl_write(chapter_dir / "image_manifest.jsonl", [])
    # chapter_completeness.json: exactly the 4 documented grades, sum == n_records
    split_outputs._atomic_json_write(
        chapter_dir / "chapter_completeness.json", {
            "chapter_id": chapter_id, "subject": subject, "chapter_no": chapter_no,
            "ts": "2026-08-08T12:00:00Z",
            "question_records": n_records, "answer_records": n_records,
            "solution_records": n_records, "image_manifest_records": 0,
            "incomplete_questions": 0, "incomplete_answers": 0,
            "incomplete_solutions": 0,
            "unresolved_qid_count": 0, "unresolved_qid_q_nos": [],
            "orphan_count": 0, "unresolved_image_count": 0,
            "q_id_grade_counts": {
                "RESOLVED_ANCHORED": n_records,
                "RESOLVED": 0, "PROVISIONAL": 0, "UNRESOLVED": 0,
            },
            "extraction_status_counts": {"COMPLETE": 3 * n_records,
                                          "INCOMPLETE": 0},
            "pass_provenance_summary": {"Q_PASS": n_records, "A_PASS": n_records,
                                         "S_PASS": n_records},
            # Phase 5: all 4 anchor families populated by default,
            # so the pending dict is empty.
            "phase2_pending_anchors": {},
        })
    return chapter_dir


def _build_all_clean_chapters(split_root: Path) -> tuple:
    """Build 33 PSY chapters + 1 MED chapter, all clean. Returns
    (psy_chapters, med_chapter) as a (list, single) tuple."""
    psy = []
    for n in range(1, 34):  # 33 chapters
        n_records = 10 + n % 5  # varying sizes
        psy.append(_build_clean_chapter(split_root, "PSY", n, n_records))
    med = _build_clean_chapter(split_root, "MED", 1, 25)
    return psy, med


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

    tmp = Path(tempfile.mkdtemp(prefix="phase4_full_book_"))
    split_root = tmp / "data" / "split"
    split_root.mkdir(parents=True, exist_ok=True)
    print("=" * 80)
    print(f"Synthetic split root: {split_root}")
    print("=" * 80)

    # ----------------------------------------------------------------
    # A. Build clean set
    # ----------------------------------------------------------------
    print("\nA. Build 33 PSY + 1 MED clean chapters")
    psy_chapters, med_chapter = _build_all_clean_chapters(split_root)
    check("33 PSY chapters built", len(psy_chapters) == 33,
          f"got {len(psy_chapters)}")
    check("1 MED chapter built", med_chapter.exists(),
          f"got {med_chapter}")
    # Spot-check: every chapter has all 7 files
    all_chapters = psy_chapters + [med_chapter]
    missing_anywhere = []
    for ch in all_chapters:
        for f in split_outputs._atomic_jsonl_write.__globals__["EXPECTED_SPLIT_FILES"] \
                if False else (
                    "questions.jsonl", "answers.jsonl", "solutions.jsonl",
                    "unresolved_qids.jsonl", "orphans.jsonl",
                    "chapter_completeness.json", "image_manifest.jsonl",
                ):
            if not (ch / f).exists():
                missing_anywhere.append(f"{ch.name}/{f}")
    check("every chapter has all 7 split files", not missing_anywhere,
          f"missing: {missing_anywhere[:3]}")

    # ----------------------------------------------------------------
    # B. Run validator on clean set -- every chapter passes
    # ----------------------------------------------------------------
    print("\nB. Validator reports 0 failures on clean set")
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(split_root)],
        capture_output=True, text=True)
    check("validator exits 0 on clean set", result.returncode == 0,
          f"rc={result.returncode}")
    check("validator output contains 'ALL CHAPTERS PASSED'",
          "ALL CHAPTERS PASSED" in result.stdout)
    check("validator reports total=34", "total chapters: 34" in result.stdout,
          f"stdout last 200: {result.stdout[-200:]}")

    # ----------------------------------------------------------------
    # C. Inject 5 failure modes and prove each is caught
    # ----------------------------------------------------------------
    print("\nC. Inject 5 failure modes; each is caught by the right rubric")

    # C1: missing chapter_completeness.json
    psy_chapters[0].joinpath("chapter_completeness.json").unlink()
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(split_root)],
        capture_output=True, text=True)
    check("C1: missing chapter_completeness.json -> exit 1",
          result.returncode == 1, f"rc={result.returncode}")
    check("C1: error names check_files_exist",
          "check_files_exist" in result.stdout,
          f"stdout tail: {result.stdout[-200:]}")
    # Rebuild C1
    _build_clean_chapter(split_root, "PSY", 1, 11)

    # C2: unknown grade value "HALLUCINATED" in q_id_grade_counts
    comp_path = psy_chapters[1] / "chapter_completeness.json"
    comp = json.loads(comp_path.read_text())
    comp["q_id_grade_counts"]["HALLUCINATED"] = 0
    comp_path.write_text(json.dumps(comp, indent=2))
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(split_root)],
        capture_output=True, text=True)
    check("C2: unknown grade -> exit 1", result.returncode == 1,
          f"rc={result.returncode}")
    check("C2: error names check_grade_values",
          "check_grade_values" in result.stdout,
          f"stdout tail: {result.stdout[-200:]}")
    # Rebuild C2
    _build_clean_chapter(split_root, "PSY", 2, 12)

    # C3: grade_counts sum != question_records
    comp_path = psy_chapters[2] / "chapter_completeness.json"
    comp = json.loads(comp_path.read_text())
    comp["q_id_grade_counts"]["RESOLVED_ANCHORED"] -= 1  # off by one
    comp_path.write_text(json.dumps(comp, indent=2))
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(split_root)],
        capture_output=True, text=True)
    check("C3: grade_counts sum mismatch -> exit 1",
          result.returncode == 1, f"rc={result.returncode}")
    check("C3: error names check_grade_sum",
          "check_grade_sum" in result.stdout,
          f"stdout tail: {result.stdout[-200:]}")
    # Rebuild C3
    _build_clean_chapter(split_root, "PSY", 3, 13)

    # C4: phase2_pending_anchors is non-empty (Phase 5 contract:
    # the dict must be {} for a clean chapter -- all 4 anchor
    # families are populated by default). Inject a stale key to
    # prove the rubric catches it.
    comp_path = psy_chapters[3] / "chapter_completeness.json"
    comp = json.loads(comp_path.read_text())
    comp["phase2_pending_anchors"]["neighbor_run"] = \
        "stale: Phase 2 lifted this (and Phase 5 lifted the OCR anchors too)"
    comp_path.write_text(json.dumps(comp, indent=2))
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(split_root)],
        capture_output=True, text=True)
    check("C4: stale phase2 anchor -> exit 1",
          result.returncode == 1, f"rc={result.returncode}")
    check("C4: error names check_phase2_pending",
          "check_phase2_pending" in result.stdout,
          f"stdout tail: {result.stdout[-200:]}")
    # Rebuild C4
    _build_clean_chapter(split_root, "PSY", 4, 14)

    # C5: questions.jsonl has a q_id the answers/solutions don't
    q_path = psy_chapters[4] / "questions.jsonl"
    q_rows = [json.loads(ln) for ln in q_path.read_text().splitlines() if ln.strip()]
    q_rows.append({
        "q_id": "PSY-005-999", "chapter_id": "PSY-005", "subject": "PSY",
        "chapter_no": 5, "q_no": 999,
        "q_id_grade": "RESOLVED_ANCHORED", "q_no_anchors": {},
        "question_text": "Phantom record",
        "options": [
            {"id": "A", "text": "a", "images": []},
            {"id": "B", "text": "b", "images": []},
            {"id": "C", "text": "c", "images": []},
            {"id": "D", "text": "d", "images": []},
        ],
        "question_images": [], "tables": [],
        "source_pages": [999], "extraction_status": "COMPLETE",
    })
    q_path.write_text("\n".join(json.dumps(r) for r in q_rows) + "\n")
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(split_root)],
        capture_output=True, text=True)
    check("C5: split q_id mismatch -> exit 1",
          result.returncode == 1, f"rc={result.returncode}")
    check("C5: error names check_qid_set_consistency",
          "check_qid_set_consistency" in result.stdout,
          f"stdout tail: {result.stdout[-200:]}")
    # Rebuild C5
    _build_clean_chapter(split_root, "PSY", 5, 15)

    # Sanity: back to all clean
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(split_root)],
        capture_output=True, text=True)
    check("sanity: all-clean again -> exit 0",
          result.returncode == 0, f"rc={result.returncode}")

    # ----------------------------------------------------------------
    # D. --subject filter limits the scan
    # ----------------------------------------------------------------
    print("\nD. --subject filter limits the scan")
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(split_root), "--subject", "MED"],
        capture_output=True, text=True)
    check("--subject MED: exit 0", result.returncode == 0,
          f"rc={result.returncode}")
    check("--subject MED: 1 chapter reported",
          "total chapters: 1" in result.stdout,
          f"stdout tail: {result.stdout[-300:]}")

    # ----------------------------------------------------------------
    # E. --json output is valid JSON
    # ----------------------------------------------------------------
    print("\nE. --json output is valid JSON with the right shape")
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(split_root), "--json"],
        capture_output=True, text=True)
    check("--json: exit 0 on clean set",
          result.returncode == 0, f"rc={result.returncode}")
    try:
        j = json.loads(result.stdout)
        json_err = ""
    except json.JSONDecodeError as e:
        j = None
        json_err = f"{type(e).__name__}: {e}"
    check("--json: stdout parses as JSON",
          j is not None, f"err={json_err}")
    if j is not None:
        check("--json: top-level keys present",
              set(j.keys()) >= {"total", "passed", "failed",
                                "subjects", "chapters"},
              f"got {set(j.keys())}")
        check("--json: total == 34", j.get("total") == 34,
              f"got {j.get('total')}")
        check("--json: passed == 34", j.get("passed") == 34,
              f"got {j.get('passed')}")
        check("--json: failed == 0", j.get("failed") == 0,
              f"got {j.get('failed')}")
        check("--json: subjects dict has PSY and MED",
              set(j.get("subjects", {}).keys()) == {"PSY", "MED"},
              f"got {set(j.get('subjects', {}).keys())}")
        check("--json: chapters list has 34 entries",
              len(j.get("chapters", [])) == 34,
              f"got {len(j.get('chapters', []))}")

    # ----------------------------------------------------------------
    # F. Exit code is 1 on any failure, 0 on all-pass
    # ----------------------------------------------------------------
    print("\nF. Exit code behavior")
    # Already proven C1-C5 above (all exit 1); also test:
    # Inject all 5 failures at once -> exit 1
    psy_chapters[0].joinpath("chapter_completeness.json").unlink()
    comp_path = psy_chapters[1] / "chapter_completeness.json"
    comp = json.loads(comp_path.read_text())
    comp["q_id_grade_counts"]["HALLUCINATED"] = 0
    comp_path.write_text(json.dumps(comp, indent=2))
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(split_root)],
        capture_output=True, text=True)
    check("F: 2 simultaneous failures -> exit 1",
          result.returncode == 1, f"rc={result.returncode}")
    check("F: summary shows 2 chapters failed",
          "2 CHAPTER(S) FAILED" in result.stdout,
          f"stdout tail: {result.stdout[-200:]}")

    # Clean up
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 80)
    print(f"Phase 4 (b) full-book split test: {n_ok}/{n_total} assertions passed")
    print("=" * 80)
    if failed:
        print(f"FAILED: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
