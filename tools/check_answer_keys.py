#!/usr/bin/env python3
"""Answer-key accuracy checker -- printed key vs pipeline answers. Zero tokens.

For each chapter range, reads the book's printed 'Answer Key' table straight
from the text layer (pdftotext -layout, no Gemini) and compares it against the
pipeline's answers.jsonl in a run's split output. Reports per chapter:
  keyrows   rows the reader recovered from the printed table
  strays    rows numbered ABOVE the chapter's question count (phantom rows
            would mean response.text noise is leaking into the key chain)
  missing   questions with no printed-key row recovered
  match/flip how many pipeline answers agree/disagree with the printed key

Usage:
  python tools/check_answer_keys.py <book.pdf> <output_root> \
      --range "3=48-65,9=185-223"        # ch_no=file_start-file_end

Never writes anything; never calls Gemini. Exit 1 if any flip is found so it
can gate CI/manual review.
"""
import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import review_digest as rd  # noqa: E402


def parse_ranges(spec):
    out = {}
    for part in spec.split(","):
        ch, rng = part.split("=")
        a, b = rng.split("-")
        out[int(ch)] = (int(a), int(b))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("book_pdf")
    ap.add_argument("output_root")
    ap.add_argument("--range", dest="ranges", required=True,
                    help="'3=48-65,9=185-223' (file pages)")
    args = ap.parse_args()
    ranges = parse_ranges(args.ranges)

    counts = {}
    for f in glob.glob(str(Path(args.output_root) / "split" / "*" / "*" /
                            "chapter_completeness.json")):
        c = json.load(open(f))
        counts[c["chapter_no"]] = c["question_records"]

    any_flip = False
    print(f"{'ch':>3} {'qs':>4} {'keyrows':>7} {'strays':>6} {'missing':>7} "
          f"{'match':>5} {'flip':>4}")
    for ch, (a, b) in sorted(ranges.items()):
        keys = rd._printed_answer_keys(args.book_pdf, a, b)
        nq = counts.get(ch)
        strays = {k: v for k, v in keys.items() if nq and k > nq}
        missing = ((nq or 0) - len([k for k in keys if nq and k <= nq])
                   if nq else " ")
        match = 0
        flips = []
        for f in glob.glob(str(Path(args.output_root) / "split" / "*" /
                               f"*-{ch:03d}" / "answers.jsonl")):
            for line in open(f):
                r = json.loads(line)
                got = (r.get("correct_option") or "").upper()
                pr = keys.get(r["q_no"])
                if pr is None:
                    continue
                if got == pr:
                    match += 1
                else:
                    flips.append((r["q_id"], pr, got))
        any_flip |= bool(flips)
        print(f"{ch:>3} {str(nq):>4} {len(keys):>7} {str(len(strays)):>6} "
              f"{str(missing):>7} {match:>5} {len(flips):>4}")
        for qid, pr, got in flips:
            print(f"      FLIP {qid}: printed '{pr}' vs extracted '{got}' "
                  f"-- needs manual review, never auto-corrected")
        if strays:
            print(f"      STRAY key rows beyond question count: {strays} "
                  f"-- possible key-chain contamination, review before trusting "
                  f"flip judgements")
    sys.exit(1 if any_flip else 0)


if __name__ == "__main__":
    main()
