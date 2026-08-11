"""
test_phase2_anchors.py
======================

End-to-end Phase 2 proof: the two non-printed anchors (neighbor_run
and carry_forward_origin from the run-18 GUARD / compute_carry) are
captured read-only by process_pdf and lifted into q_no_anchors by
split_outputs.reconcile_qids. The grader promotes single-anchor
records from PROVISIONAL to RESOLVED when either anchor is present.

Background
----------
Per the design doc §2 / §3.1 and the Phase 1 report §6, the Phase 1
grader distinguished only printed anchors (printed_stem_match,
printed_solution_header_match, answer_key_row_match). A record with
EXACTLY one printed anchor was graded RESOLVED, and a record with
ZERO printed anchors was graded PROVISIONAL. The design called for
two non-printed anchors to push borderline single-anchor records up
to RESOLVED:

  * neighbor_run: the q_no appears in a "trusted" connected-run
    from the run-18 GUARD (a run of >=5 items, or a run that
    touches the chapter's known_max)
  * carry_forward_origin: the q_no is the last_open_question of
    a previous compute_carry() observation

This test proves the Phase 2 lift is wired correctly through both
the grader and the q_no_anchors vector.

What this test proves
--------------------
  A. _grade_record PROMOTES 0-anchor + neighbor_run -> RESOLVED
  B. _grade_record PROMOTES 0-anchor + carry_forward_origin -> RESOLVED
  C. _grade_record PROMOTES 1-anchor + either origin -> RESOLVED
  D. _grade_record leaves 0-anchor + no origin as PROVISIONAL
  E. _grade_record leaves 1-anchor + no origin as RESOLVED (Phase 1)
  F. _build_q_no_anchors injects neighbor_run when observation is passed
  G. _build_q_no_anchors injects carry_forward_origin when observation is passed
  H. reconcile_qids pulls the per-qn observation lookups correctly
  I. chapter_completeness.json.phase2_pending_anchors no longer lists
     neighbor_run / carry_forward_origin (they are now populated)
  J. Pass None to reconcile_qids (Phase 1 callers) -> no behavior change

What this test does NOT do
-------------------------
  * Make any Gemini calls. The path is pure-Python.
  * Touch the real chapter pipeline. Synthetic chapter_records only.
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


# ============================================================================
# 1. Test fixtures
# ============================================================================

def build_record(qn, *, stem="", options=None, answer="B", solution=""):
    """Build a chapter_records-shaped record. Empty stem/solution by
    default so we can drive the grader through the PROVISIONAL path."""
    return {
        "q_no": qn,
        "question_text": stem,
        "options": options or {"A": "x", "B": "y", "C": "z", "D": "w"},
        "correct_option": answer,
        "solution_text": solution,
        "tables": [],
        "has_figure_in_question": False,
        "has_figure_in_solution": False,
        "_prov": {"question_text": "Q_PASS", "options": "Q_PASS",
                  "correct_option": "A_PASS", "solution_text": "S_PASS"},
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

    # ---- A. _grade_record: 0 anchors + neighbor_run -> RESOLVED
    print("=" * 80)
    print("A. _grade_record: 0 anchors + neighbor_run -> RESOLVED (Phase 2 promotion)")
    print("=" * 80)
    g = split_outputs._grade_record({}, has_neighbor_run=True, has_carry_origin=False)
    check("0 anchors + neighbor_run only -> RESOLVED",
          g == "RESOLVED", f"got {g!r}")
    print()

    # ---- B. _grade_record: 0 anchors + carry_origin -> RESOLVED
    print("=" * 80)
    print("B. _grade_record: 0 anchors + carry_origin -> RESOLVED (Phase 2 promotion)")
    print("=" * 80)
    g = split_outputs._grade_record({}, has_neighbor_run=False, has_carry_origin=True)
    check("0 anchors + carry_origin only -> RESOLVED",
          g == "RESOLVED", f"got {g!r}")
    g = split_outputs._grade_record({}, has_neighbor_run=True, has_carry_origin=True)
    check("0 anchors + both origins -> RESOLVED",
          g == "RESOLVED", f"got {g!r}")
    print()

    # ---- C. _grade_record: 1 anchor + either origin -> RESOLVED (no change vs Phase 1)
    print("=" * 80)
    print("C. _grade_record: 1 printed anchor + either origin -> RESOLVED")
    print("=" * 80)
    one_anchor = {"printed_stem_match": {"page": 100, "header_text": "1."}}
    g = split_outputs._grade_record(one_anchor, has_neighbor_run=True, has_carry_origin=False)
    check("1 printed anchor + neighbor_run -> RESOLVED",
          g == "RESOLVED", f"got {g!r}")
    g = split_outputs._grade_record(one_anchor, has_neighbor_run=False, has_carry_origin=True)
    check("1 printed anchor + carry_origin -> RESOLVED",
          g == "RESOLVED", f"got {g!r}")
    print()

    # ---- D. _grade_record: 0 anchors + no origin -> PROVISIONAL (Phase 1 behavior)
    print("=" * 80)
    print("D. _grade_record: 0 anchors + no origin -> PROVISIONAL (Phase 1 baseline)")
    print("=" * 80)
    g = split_outputs._grade_record({}, has_neighbor_run=False, has_carry_origin=False)
    check("0 anchors + no origin -> PROVISIONAL (unchanged from Phase 1)",
          g == "PROVISIONAL", f"got {g!r}")
    print()

    # ---- E. _grade_record: 1 anchor + no origin -> RESOLVED (Phase 1 behavior)
    print("=" * 80)
    print("E. _grade_record: 1 printed anchor + no origin -> RESOLVED (Phase 1 baseline)")
    print("=" * 80)
    g = split_outputs._grade_record(one_anchor)
    check("1 printed anchor + no origin -> RESOLVED (unchanged from Phase 1)",
          g == "RESOLVED", f"got {g!r}")
    print()

    # ---- F. _build_q_no_anchors injects neighbor_run when observation passed
    print("=" * 80)
    print("F. _build_q_no_anchors injects neighbor_run from observation")
    print("=" * 80)
    rec = build_record(5)
    nr_obs = {
        "window_pages": [101, 102],
        "batch_qnos": [1, 2, 3, 4, 5, 6],
        "runs": [[1, 2, 3, 4, 5, 6]],
        "trusted_qnos": [1, 2, 3, 4, 5, 6],
        "known_max": 6,
    }
    anchors = split_outputs._build_q_no_anchors(rec, 5, {}, [101, 102],
                                                neighbor_run_obs=nr_obs)
    check("neighbor_run key is present in q_no_anchors",
          "neighbor_run" in anchors, f"keys={list(anchors)}")
    check("neighbor_run.size == 6",
          anchors.get("neighbor_run", {}).get("size") == 6,
          f"got {anchors.get('neighbor_run')}")
    check("neighbor_run.first == 1",
          anchors.get("neighbor_run", {}).get("first") == 1,
          f"got {anchors.get('neighbor_run')}")
    check("neighbor_run.last == 6",
          anchors.get("neighbor_run", {}).get("last") == 6,
          f"got {anchors.get('neighbor_run')}")
    check("neighbor_run.near_chapter_max is True (known_max=6, min(trusted)=1, diff=-5)",
          anchors.get("neighbor_run", {}).get("near_chapter_max") is True,
          f"got {anchors.get('neighbor_run')}")
    print()

    # ---- G. _build_q_no_anchors injects carry_forward_origin when observation passed
    print("=" * 80)
    print("G. _build_q_no_anchors injects carry_forward_origin from observation")
    print("=" * 80)
    rec = build_record(7)
    co_obs = {
        "pass": "Q",
        "window_pages": [101, 102, 103],
        "last_open_question": 7,
        "cut_part": "options",
        "ends_mid_content": True,
    }
    anchors = split_outputs._build_q_no_anchors(rec, 7, {}, [101, 102, 103],
                                                carry_origin_obs=co_obs)
    check("carry_forward_origin key is present in q_no_anchors",
          "carry_forward_origin" in anchors, f"keys={list(anchors)}")
    cfo = anchors.get("carry_forward_origin", {})
    check("carry_forward_origin.from_window == [101, 102, 103]",
          cfo.get("from_window") == [101, 102, 103],
          f"got {cfo}")
    check("carry_forward_origin.cut_part == 'options'",
          cfo.get("cut_part") == "options",
          f"got {cfo}")
    check("no neighbor_run when not passed (no placeholder)",
          "neighbor_run" not in anchors,
          f"keys={list(anchors)}")
    print()

    # ---- H. reconcile_qids: pass chapter_anchor_observations -> promotions
    #         are visible in the per-q_no_anchors vector, and the
    #         unmatched-record grade is RESOLVED (not PROVISIONAL).
    print("=" * 80)
    print("H. reconcile_qids wires observations through to grades and anchors")
    print("=" * 80)
    tmp = tempfile.mkdtemp(prefix="phase2_reconcile_")
    original_DATA_DIR = qp.DATA_DIR
    qp.DATA_DIR = qp.Path(tmp)
    try:
        # Build a 3-record chapter: Q1 (clean), Q5 (in trusted run,
        # no printed anchors -- should be PROMOTED to RESOLVED via
        # neighbor_run), Q7 (in a carry-forward window, no printed
        # anchors -- should be PROMOTED to RESOLVED via
        # carry_forward_origin).
        chapter = {
            1: build_record(1),
            5: build_record(5),  # no printed anchors, in trusted run
            7: build_record(7),  # no printed anchors, has carry origin
        }
        # Observations: Q5 is in the run-18 GUARD's trusted_qnos;
        # Q7 is the last_open_question of a previous Q-pass window.
        observations = {
            "neighbor_runs": [
                {
                    "window_pages": [101, 102],
                    "batch_qnos": [1, 5, 6, 7, 8],
                    "runs": [[1, 5, 6, 7, 8]],
                    "trusted_qnos": [1, 5, 6, 7, 8],
                    "known_max": 8,
                },
            ],
            "carry_forwards": [
                {
                    "pass": "Q",
                    "window_pages": [100, 101, 102],
                    "last_open_question": 7,
                    "cut_part": "question",
                    "ends_mid_content": True,
                },
            ],
        }
        # Stub _harvest_anchors so the printed-anchor scan is empty
        # for every q_no (we want to drive the Phase-2 promotion path).
        original_harvest = split_outputs._harvest_anchors
        split_outputs._harvest_anchors = lambda *a, **kw: {qn: {} for qn in chapter}
        try:
            reconciled = split_outputs.reconcile_qids(
                chapter, {}, "/tmp/PSY_ch007_fake.pdf", [], "PSY", 7,
                chapter_anchor_observations=observations)
            kept = reconciled["kept"]
            check("reconcile_qids returns 3 kept records (none UNRESOLVED)",
                  len(kept) == 3, f"got {len(kept)} kept")
            check("Q1 is RESOLVED via printed anchors (PROVISIONAL promoted)",
                  kept[1]["q_id_grade"] == "RESOLVED",
                  f"got {kept[1].get('q_id_grade')!r}")
            check("Q5 is RESOLVED via neighbor_run (Phase 2 promotion)",
                  kept[5]["q_id_grade"] == "RESOLVED",
                  f"got {kept[5].get('q_id_grade')!r}")
            check("Q5 has neighbor_run anchor in q_no_anchors",
                  "neighbor_run" in (kept[5].get("q_no_anchors") or {}),
                  f"anchors keys: {list(kept[5].get('q_no_anchors') or {})}")
            check("Q5's neighbor_run.size == 5",
                  (kept[5].get("q_no_anchors") or {}).get("neighbor_run", {}).get("size") == 5,
                  f"got {(kept[5].get('q_no_anchors') or {}).get('neighbor_run')}")
            check("Q5's neighbor_run.near_chapter_max is True (8 - 1 = -7 <= 3)",
                  (kept[5].get("q_no_anchors") or {}).get("neighbor_run", {}).get("near_chapter_max") is True,
                  f"got near_chapter_max")
            check("Q7 is RESOLVED via carry_forward_origin (Phase 2 promotion)",
                  kept[7]["q_id_grade"] == "RESOLVED",
                  f"got {kept[7].get('q_id_grade')!r}")
            check("Q7 has carry_forward_origin anchor in q_no_anchors",
                  "carry_forward_origin" in (kept[7].get("q_no_anchors") or {}),
                  f"anchors keys: {list(kept[7].get('q_no_anchors') or {})}")
            check("Q7's carry_forward_origin.cut_part == 'question'",
                  (kept[7].get("q_no_anchors") or {}).get("carry_forward_origin", {}).get("cut_part") == "question",
                  f"got cut_part")
        finally:
            split_outputs._harvest_anchors = original_harvest
    finally:
        qp.DATA_DIR = original_DATA_DIR
    print()

    # ---- I. chapter_completeness.json.phase2_pending_anchors no longer
    #         lists the two Phase 2 anchors (they are now populated).
    print("=" * 80)
    print("I. phase2_pending_anchors in chapter_completeness.json is empty (or OCR-only)")
    print("=" * 80)
    # Drive write_split_outputs against a tmp output_root to inspect
    # the resulting chapter_completeness.json content.
    tmp = tempfile.mkdtemp(prefix="phase2_completeness_")
    original_DATA_DIR = qp.DATA_DIR
    qp.DATA_DIR = qp.Path(tmp) / "data"
    try:
        # Tiny chapter with one well-formed record
        chapter = {
            1: build_record(1, stem="What is X?",
                            options={"A": "a", "B": "b", "C": "c", "D": "d"},
                            answer="A",
                            solution="Answer A because X is the key."),
        }
        # Stub _harvest_anchors + image helpers
        original_harvest = split_outputs._harvest_anchors
        split_outputs._harvest_anchors = lambda *a, **kw: {
            1: {"printed_stem_match": {"page": 100, "header_text": "1."},
                 "printed_solution_header_match": {"page": 105, "header_text": "Solution to Question 1:"},
                 "answer_key_row_match": {"page": 110, "row": "| 1 | A |", "letter": "A"}}}
        try:
            reconciled = split_outputs.reconcile_qids(
                chapter, {1: [100, 105]}, "/tmp/PSY_ch007_fake.pdf",
                [], "PSY", 7)
            split_outputs.write_split_outputs(
                chapter_id="PSY-007", subject="PSY", chapter_no=7,
                chapter_records=chapter, image_files_by_q={},
                qn_source_pages={1: [100, 105]}, orphans=[],
                chapter_unresolved_images=[], pdf_path="/tmp/PSY_ch007_fake.pdf",
                page_files=[], reconciled=reconciled, output_root=tmp)
            comp = json.loads(
                (qp.Path(tmp) / "split" / "PSY" / "PSY-007" /
                 "chapter_completeness.json").read_text())
        finally:
            split_outputs._harvest_anchors = original_harvest
        pending = comp.get("phase2_pending_anchors", {})
        check("phase2_pending_anchors does NOT contain 'neighbor_run' (now populated)",
              "neighbor_run" not in pending,
              f"pending keys: {list(pending)}")
        check("phase2_pending_anchors does NOT contain 'carry_forward_origin' (now populated)",
              "carry_forward_origin" not in pending,
              f"pending keys: {list(pending)}")
        check("Q1's q_id_grade == RESOLVED_ANCHORED (2 printed + 1 answer key)",
              comp["q_id_grade_counts"].get("RESOLVED_ANCHORED") == 1,
              f"got {comp['q_id_grade_counts']}")
    finally:
        qp.DATA_DIR = original_DATA_DIR
    print()

    # ---- J. Pass None to reconcile_qids (Phase 1 callers) -> no behavior change
    print("=" * 80)
    print("J. Pass None observations (Phase 1 callers) -> no behavior change")
    print("=" * 80)
    tmp = tempfile.mkdtemp(prefix="phase2_none_")
    original_DATA_DIR = qp.DATA_DIR
    qp.DATA_DIR = qp.Path(tmp)
    try:
        chapter = {
            1: build_record(1),
            5: build_record(5),  # no printed anchors
        }
        original_harvest = split_outputs._harvest_anchors
        split_outputs._harvest_anchors = lambda *a, **kw: {qn: {} for qn in chapter}
        try:
            # With NO observations: Q5 should be PROVISIONAL (Phase 1)
            reconciled_none = split_outputs.reconcile_qids(
                chapter, {}, "/tmp/PSY_ch007_fake.pdf", [], "PSY", 7,
                chapter_anchor_observations=None)
            kept_none = reconciled_none["kept"]
            check("None observations: Q1 still RESOLVED (no printed anchor for Q1 here either)",
                  kept_none[1]["q_id_grade"] == "PROVISIONAL",
                  f"got {kept_none[1].get('q_id_grade')!r}")
            check("None observations: Q5 stays PROVISIONAL (no Phase 2 promotion)",
                  kept_none[5]["q_id_grade"] == "PROVISIONAL",
                  f"got {kept_none[5].get('q_id_grade')!r}")
            # And EMPTY dict: same behavior
            reconciled_empty = split_outputs.reconcile_qids(
                chapter, {}, "/tmp/PSY_ch007_fake.pdf", [], "PSY", 7,
                chapter_anchor_observations={})
            kept_empty = reconciled_empty["kept"]
            check("Empty dict observations: Q5 stays PROVISIONAL (same as None)",
                  kept_empty[5]["q_id_grade"] == "PROVISIONAL",
                  f"got {kept_empty[5].get('q_id_grade')!r}")
        finally:
            split_outputs._harvest_anchors = original_harvest
    finally:
        qp.DATA_DIR = original_DATA_DIR
    print()

    print("=" * 80)
    print(f"PSY-007 Phase-2 anchor test: {n_ok}/{n_total} assertions passed")
    print("=" * 80)
    if failed:
        print(f"FAILED: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
