#!/usr/bin/env python3
"""
V2 single-chapter smoke test (Task 6).

Runs the full 3-pass pipeline (Q/A/S + carry + probes + clip + image ladder +
orphans + sweep + targeted retry) on ONE chapter only, into an ISOLATED
output root (default ./v2_test_output) so real data is never touched.

Usage (on the server, or anywhere with GEMINI_API_KEY):
    python3 test_v2_chapter.py PATH/TO/book.pdf PSY 11            # chapter 11
    python3 test_v2_chapter.py book.pdf ACH 11 -1 /data/v2_test  # explicit offset + out dir

Then it prints a summary + runs the deterministic validator on the result.
"""
import json
import os
import sys
from pathlib import Path

import google.generativeai as genai

import qbank_pipeline as qp


def main():
    if len(sys.argv) < 4:
        print("usage: python3 test_v2_chapter.py <pdf> <SUBJECT> <CHAPTER_NO> [page_offset] [out_dir]")
        sys.exit(1)
    pdf_path = sys.argv[1]
    subject = sys.argv[2].strip().upper()
    chapter_no = int(sys.argv[3])
    page_offset = int(sys.argv[4]) if len(sys.argv) > 4 else -1
    out_dir = Path(sys.argv[5]) if len(sys.argv) > 5 else Path("./v2_test_output")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Set GEMINI_API_KEY first.")
        sys.exit(1)

    # Point the whole pipeline at the isolated test root BEFORE any paths resolve.
    out_dir.mkdir(parents=True, exist_ok=True)
    qp.OUTPUT_ROOT = out_dir
    qp.DATA_DIR = out_dir / "data"
    qp.ASSETS_DIR = out_dir / "assets"
    qp.STATE_FILE = out_dir / "state.json"
    qp.DATA_DIR.mkdir(parents=True, exist_ok=True)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(qp.GEMINI_MODEL)

    state = qp.load_state()
    cfg = {"subject": subject, "path": pdf_path, "page_offset": page_offset}
    chapters_out = []
    q_path = qp.DATA_DIR / "questions.jsonl"
    print(f"=== V2 3-pass single-chapter test: {subject} chapter {chapter_no} "
          f"(offset {page_offset}) -> {out_dir} ===")
    # run-16: per-chapter atomic rewrite inside process_pdf -- pass the PATH
    qp.process_pdf(cfg, state, model, chapters_out, q_path,
                   only_chapter_no=chapter_no)

    rows = [json.loads(l) for l in q_path.read_text().splitlines() if l.strip()] \
        if q_path.exists() else []
    n = len(rows)
    missing_ans = sum(1 for r in rows if not r.get("correct_options"))
    missing_sol = sum(1 for r in rows if not (r.get("solution") or {}).get("text"))
    print(f"\n=== RESULT: {n} questions | missing answer: {missing_ans} | "
          f"missing solution: {missing_sol} ===")

    try:
        import qbank_validator
        rep = qbank_validator.run_hybrid(out_dir, audit=False)
        s = rep["summary"]
        print(f"=== VALIDATOR: {s['flags_total']} flag(s) across "
              f"{s['flagged_chapters']}/{s['chapters']} chapters "
              f"(report: {qp.DATA_DIR / 'validation_report.json'}) ===")
        for f in rep.get("flags", [])[:20]:
            print(f"  - [{f.get('severity')}] {f.get('kind')} "
                  f"{f.get('chapter_id')} q{f.get('q_no')}: {str(f.get('detail'))[:110]}")
    except Exception as e:
        print(f"(validator summary skipped: {e})")

    print(f"\nInspect: {q_path}")
    print("If clean, the v2 pipeline is ready for a full-book run.")


if __name__ == "__main__":
    main()
