#!/usr/bin/env python3
"""
Per-book REVIEW DIGEST: one short, sorted page per book for the human
reviewer. The pipeline's flags/gates/statuses are already evidence-backed;
this tool collapses them into the few items that actually need eyes, in the
right order:

  BLOCKER  -- answer/solution letter flips, strong missing-figure gaps,
              wrong-owner image suspects, INCOMPLETE rows
  REVIEW   -- model-claimed images, solution cross-mixes, quarantined stems
  NOISE    -- orphan fragments, weak declared-figure advisories,
              recitation-dump headers (already handled), async retries

Run:  python3 tools/review_digest.py <output_root> [--book SUBJECT]
Writes: <output_root>/review_digest/<SUBJECT>.md  (one per book)
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qbank_pipeline as qp

BLOCKER_KINDS = {"qa_incomplete", "answer_suspect"}
REVIEW_KINDS = {"qa_review_needed", "figure_page_mismatch", "image_owner_gate_miss",
                "foreign_option_head", "option_solution_disagree",
                "foreign_solution_segment", "suspect_stem", "options_suspect"}
NOISE_KINDS = {"orphan_unresolved", "solution_recitation_dump", "solution_header_furniture",
               "declared_figure_missing", "image_unclaimed", "unmatched_image",
               "stray_answer_key_table", "duplicate_table", "short_bare_solution",
               "truncated_solution_suppressed_by_image", "over_attributed_images",
               "over_attributed_solution_images", "truncated_solution", "suspect_density"}


def _load_jsonl(p):
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _printed_answer_keys(pdf_path, first, last):
    """{qn: letter} from 'Answer Key' pages + the continuation page carrying
    overflow rows (chains until 'Detailed Explanations'/'Solution to'
    appears). Zero-token; used to separate a PROVEN flip (key disagrees)
    from a distractor-mentioned-in-prose weak hit."""
    import subprocess
    out = {}
    in_key = False
    for p in range(first, last + 1):
        try:
            txt = subprocess.run(["pdftotext", "-f", str(p), "-l", str(p),
                                  "-layout", pdf_path, "-"],
                                 capture_output=True, text=True,
                                 timeout=60).stdout or ""
        except Exception:
            txt = ""
        if re.search(r"Answer Key|Correct Option", txt, re.I):
            in_key = True
        if not in_key:
            continue
        for m in re.finditer(r"(?m)^\s*(\d{1,3})\s{2,}([A-Da-d])\s*$", txt):
            out.setdefault(int(m.group(1)), m.group(2).upper())
        for m in re.finditer(r"\|\s*(\d{1,3})\s*\|\s*([A-Da-d])\s*\|", txt):
            out.setdefault(int(m.group(1)), m.group(2).upper())
        # stop AFTER a page that already carries key rows -- explanations
        # follow the table on the same page on this book (p199 proves it)
        if re.search(r"Detailed Explanations|Solution to Question", txt, re.I):
            in_key = False
    return out


def build_digest(output_root, subject=None, book_pdf=None, book_ranges=None):
    out = Path(output_root)
    rows = _load_jsonl(out / "data" / "questions.jsonl")
    vr = json.load(open(out / "data" / "validation_report.json")) \
        if (out / "data" / "validation_report.json").exists() else {}
    gates = _load_jsonl(out / "data" / "export_gate.jsonl")
    advisory = _load_jsonl(out / "data" / "export_gate_advisory.jsonl")

    by_book = defaultdict(list)
    for r in rows:
        if subject and r.get("subject") != subject:
            continue
        by_book[r.get("subject", "?")].append(r)

    _keys = {}
    if book_pdf and book_ranges:
        for ch_no, (first, last) in book_ranges:
            for qn, letter in _printed_answer_keys(book_pdf, first, last).items():
                _keys[(ch_no, qn)] = letter

    digests = {}
    for sub, rows_ in sorted(by_book.items()):
        cid = None
        blockers, review, noise = [], [], []
        for r in rows_:
            qn = r["id"].rsplit("-", 1)[-1]
            cid = r.get("chapter_id")
            status = r.get("qa_status") or ("INCOMPLETE" if any(
                not str(x or "").strip()
                for x in [r["question"]["text"]]) else "READY")
            reasons = r.get("qa_reasons") or []
            answer_flip = any("answer_suspect" in x for x in reasons)
            weak_img = any("model-only evidence" in x for x in reasons)
            gate_hit = any(x.startswith("export-gate figure_page_mismatch")
                           or x.startswith("export-gate missing_declared_figure")
                           for x in reasons)

            # the deterministic answer<->solution letter check runs at READ
            # time too, so OLD exports (pre-qa_status) still surface flips
            option_rows = r.get("options") or []
            correct_id = (r.get("correct_options") or [None])[0]
            if correct_id:
                flip = qp._answer_option_mismatch(
                    {"solution_text": (r.get("solution") or {}).get("text") or ""},
                    correct_id, option_rows)
                if flip:
                    # THE KEY IS THE JUDGE: if the printed answer key agrees
                    # with the extraction, a distractor merely appears in the
                    # prose -> weak hit (OPH-009-018 class). Disagreement with
                    # the printed key = proven flip (OPH-009-002 class).
                    ch_no = int(r["chapter_id"].split("-")[1]) if \
                        r.get("chapter_id") else None
                    printed = _keys.get((ch_no, qn_int(r)))
                    if printed and printed != correct_id:
                        blockers.append((r["id"], "answer flip",
                                         f"{flip}; printed key chain says "
                                         f"'{printed}', extracted '{correct_id}'"))
                    else:
                        review.append((r["id"], "answer_suspect (weak)",
                                       flip))
            if status == "INCOMPLETE":
                blockers.append((r["id"], "INCOMPLETE",
                                 "; ".join(reasons) or "structural fields missing"))
            if answer_flip:
                blockers.append((r["id"], "answer_suspect",
                                 "; ".join(x for x in reasons
                                           if "answer_suspect" in x)))
            if gate_hit:
                review.append((r["id"], "gate wrong-owner/missing-figure",
                               "; ".join(x for x in reasons
                                          if x.startswith("export-gate"))))
            elif weak_img:
                review.append((r["id"], "model-claimed image",
                               "; ".join(x for x in reasons
                                          if "model-only evidence" in x)))
            elif status == "REVIEW_NEEDED" and not (answer_flip or gate_hit):
                review.append((r["id"], "review", "; ".join(reasons)[:200]))

        # validator flags feed noise/blockers too
        vflags = []
        for chap, fl in (vr.get("chapters") or {}).items():
            for f in fl:
                if subject and not chap.startswith(sub):
                    continue
                kind = f.get("kind")
                qn = f.get("q_no")
                label = f"{chap}-{qn}" if qn is not None else chap
                entry = (label, kind, str(f.get("detail", ""))[:160])
                if kind in REVIEW_KINDS and f.get("severity") == "high":
                    review.append(entry)
                elif kind in NOISE_KINDS or severity_low(f):
                    noise.append(entry)
                else:
                    review.append(entry)

        # gate/advisory sidecars that were NOT already absorbed
        for g in gates:
            if g.get("chapter_id", "").startswith(sub) and \
                    g.get("kind") in REVIEW_KINDS:
                review.append((f"{g.get('chapter_id')}-{g.get('q_no')}",
                                g["kind"], str(g.get("detail"))[:160]))
        for a in advisory:
            if a.get("chapter_id", "").startswith(sub):
                noise.append((f"{a.get('chapter_id')}-{a.get('q_no')}",
                              a.get("kind", "advisory"), str(a.get("detail"))[:120]))

        def _dedupe(items):
            seen = set()
            out_ = []
            for it in items:
                k = (it[0], it[1])
                if k in seen:
                    continue
                seen.add(k)
                out_.append(it)
            return out_
        blockers, review, noise = _dedupe(blockers), _dedupe(review), _dedupe(noise)
        text = [f"# REVIEW DIGEST -- {sub}",
                "",
                f"questions: {len(rows_)} | BLOCKER: {len(blockers)} | "
                f"REVIEW: {len(review)} | NOISE: {len(noise)}",
                "", "## BLOCKER (action must be taken)"]
        for qid, kind, detail in blockers:
            text.append(f"- **{qid}** [{kind}] {detail}")
        text += ["", "## REVIEW (eyes needed, no auto-fix)"]
        for qid, kind, detail in review:
            text.append(f"- {qid} [{kind}] {detail}")
        text += ["", "## NOISE (safe to ignore unless you're already in the page)"]
        for qid, kind, detail in noise:
            text.append(f"- {qid} [{kind}] {detail}")
        digests[sub] = "\n".join(text)

    outdir = out / "review_digest"
    outdir.mkdir(exist_ok=True)
    for sub, body in digests.items():
        (outdir / f"{sub}.md").write_text(body, encoding="utf-8")
    return digests


def severity_low(f):
    return f.get("severity") == "low"


def qn_int(r):
    return int(r["id"].rsplit("-", 1)[-1])


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: review_digest.py <output_root> [--book SUBJECT] "
              "[--book-pdf PATH --range-spec CH:FIRST:LAST,...]")
        sys.exit(1)
    book = None
    pdf = None
    ranges = None
    if "--book" in sys.argv:
        book = sys.argv[sys.argv.index("--book") + 1]
    if "--book-pdf" in sys.argv:
        pdf = sys.argv[sys.argv.index("--book-pdf") + 1]
        if "--range-spec" in sys.argv:
            spec = sys.argv[sys.argv.index("--range-spec") + 1]
            ranges = []
            for part in spec.split(","):
                a = part.split(":")
                if len(a) == 3:
                    ranges.append((int(a[0]), (int(a[1]), int(a[2]))))
    res = build_digest(sys.argv[1], book, pdf, ranges)
    for sub, body in res.items():
        print(body)
        print()
