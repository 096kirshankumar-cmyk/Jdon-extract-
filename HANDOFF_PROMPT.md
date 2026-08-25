# HANDOFF — QBank extraction pipeline

You have the full environment (poppler, tesseract, Gemini keys, the source
PDF). The previous agent did not, so two investigations are open that it could
only reason about. Both are listed under **YOUR TASK**. Read this whole file
first; the constraints at the bottom are product rules, not style preferences.

## Where the code is

- Repo: `https://github.com/096kirshankumar-cmyk/Jdon-extract-`
- Branch: `arena/01a03242-jdon-extract`
- Start from commit `21183cd` ("feat: crop intervals own the figures")

## Environment

```bash
apt-get update && apt-get install -y poppler-utils tesseract-ocr tzdata
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest PyMuPDF
```

**poppler is not optional.** `pdftotext_page` (`qbank_pipeline.py`) catches only
`subprocess.TimeoutExpired` — with `pdftotext` missing it raises
`FileNotFoundError`, and callers swallow that as "empty text layer". Without
poppler every diagnosis you make about the text layer will be wrong.

Gemini keys come from env (`GEMINI_API_KEYS` or `GEMINI_API_KEY_1..N`). Never
print them.

## Test baseline — do not chase these

```
.venv/bin/python -m pytest -q
→ 480 passed, 21 failed
```

All 21 are pre-existing and environmental:

- 16 × `test_boundary_phased.py::EngineCase::*` and
  `test_review_queue.py::TestQueueCacheAndPdfReader::test_pdf_reader_reused_per_book`
  — need the gitignored fixture `/home/user/book2/book.pdf`
- `test_resume_relink_regressions.py::test_real_obg_ch3_ledger_replays`
  — needs `/home/user/audit/run_obg_ch3/data/image_ownership.jsonl`
- `test_pipeline_json.py::UnifiedImageOwnershipTests::test_ocr_geometry_claims_when_text_layer_garbled`
  — needs tesseract
- `test_image_ownership_audit_regressions.py::TestSSectionBareListFilter::test_keyword_question_heading_survives_in_s_section`
  — passes alone, fails only in the full suite (test pollution)
- `test_pipeline_json.py::SolutionFigureMappingTests::test_under_detected_headers_no_longer_swallow_the_page`
  — **fails in isolation too (`AssertionError: 0 != 3`). This one is a genuine
    un-investigated pre-existing bug**, not environmental.

If your run shows more than 21, you broke something.

## Architecture

One engine. `qbank_pipeline.main()` (`qbank_pipeline.py:4190`) is a shim over
`boundary_phased.run_all()`. The old multi-pass `process_pdf` is gone.

Flow, in order:

1. **TOC → chapter page ranges** — `boundary_phased.py:3201`,
   `extract_toc_chapters()` + `compute_page_ranges()`. Verified correct against
   the book's own contents table.
2. **Zones inside the chapter** — `_resolve_zones`. Q pages / answer-key pages /
   S pages, clamped to `ch_last` by `_zone_pages_from_headers`.
3. **Visual header index** — `header_index.scan_chapter` (render + OCR). This is
   the authority when it finds Q/S/key bands.
4. **Crops** — `header_index.intervals` (`header_index.py:256`): one crop per
   block, "this header → next furniture header", spanning pages as strips.
   Multi-page blocks become labelled `INTERVAL part i/N … START/CONTINUES`
   composites.
5. **Crop → text** — `_extract_from_crops`. PDF text layer first
   (`_geom_item_from_interval`); anything not CLEAN goes to Gemini. On this
   book the text layer is GARBLED, so **every** crop goes to Gemini
   (`why: text_garbled_to_gemini=23`).
6. **One crop per Gemini call** — `CROP_BATCH_SIZE = 1`. See the constant's
   comment for why.
7. **Answer key** — whole pages, dual Gemini read. Verified 23/23 correct
   against the source key table.
8. **Images** — `_claim_images_by_interval` runs FIRST and owns every figure
   whose (page, y) lies inside a crop interval. The per-page passes
   (`claim_page_images`, `claim_block_images_ocr`, carry) are now the fallback.
9. **Gate + commit** — `_export_gate_violations`, then `build_final_question`,
   then `split_outputs.write_split_outputs`.

## Current state (OPH chapter 1, last verified run)

```
USED zones Q 4-11 | A [12] | S 12-22
Q crops=23 geom_ok=0 gemini_crops=23 | why: text_garbled_to_gemini=23
Q sending 23 isolated crop(s), one Gemini call each (CROP_BATCH_SIZE=1)
S crops=23 geom_ok=0 gemini_crops=23 | why: text_garbled_to_gemini=23
Solution verify verdict was stale -- q[3] recovered by the re-ask, cleared
[SANITIZE] OPH-001-004: dropped 696 chars of the PREVIOUS question's solution
             that bled into this crop
[GATE] OPH-001: export gate CLEAN
[IMG] OPH-001: attribution 17 crop-interval / 0 block-position / 0 carry /
               0 model / 0 unclaimed | carry share 0% of 17 claimed
qa_status: 18 READY, 5 REVIEW_NEEDED, 0 INCOMPLETE | lock=yes
```

Answers: **23/23 match the source key**. Text: clean (an earlier OCR-based path
produced garbage and was removed). Figures: 17/17 interval-proven, zero carry,
zero unclaimed. Gate CLEAN, `lock=yes`.

## YOUR TASK

### 1. q4's crop bleed — find the actual root cause (highest priority)

`OPH-001-004`'s solution still arrives with **696 characters** of the previous
question's explanation in front of it:

```
"Uveal melanomas arise from uveal melanocytes...        <- q3's text
 Solution to Question 4:
 The macula is fully developed by 4-6 months of age..."
```

`sanitize_solution_text` now drops that head (the embedded header names this
question, so the head cannot be this question's), so the shipped row is
correct. **But that treats the symptom.** Two candidate causes and only one is
real:

- **(a) the crop boundary is cut in the wrong place** — the image sent to
  Gemini already contains q3's text. Then the fix belongs in
  `header_index.intervals` / the header y-detection. Note
  `strips.append({"page": p0, "y_hi": y0 + 14, ...})` at `header_index.py:273,276,279`
  pads the crop 14pt ABOVE the header, which is ~1 line of the previous block.
  696 chars is far more than one line, so this alone does not explain it.
- **(b) the model recited** — the crop is clean and Gemini added text. Then the
  fix belongs in the prompt.

**`CROP_BATCH_SIZE=1` did not stop it**, which rules out neighbour-image
contamination. Settle it by looking at the crop:

```bash
QBANK_CROP_DUMP=/tmp/crops   # then run OPH chapter 1
# inspect /tmp/crops/OPH-001_q4_p13.png
```

Report which of (a) or (b) it is, with the crop image as evidence, then fix
that one. Do not fix both speculatively.

### 2. The 5 REVIEW_NEEDED rows — identify every one

The last run reported `5 REVIEW_NEEDED` and nothing in the log says why. Read
the reasons from the master file (the split rows do not carry `qa_reasons`):

```bash
python3 - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ.get("OUTPUT_DIR", "./qbank_output"))
for line in (root / "data" / "questions.jsonl").read_text().splitlines():
    r = json.loads(line)
    if r.get("qa_status") != "READY":
        print(r["id"], r["qa_status"])
        for reason in r.get("qa_reasons") or []:
            print("   -", reason)
PY
```

Then classify each as a **real defect** (fix it) or a **false positive** (fix
the check). Carry claims are already at zero, so none of the 5 is a carry.

### 3. Only if 1 and 2 are clean — reduce Gemini calls

`geom_ok=0` on all 46 crops means the deterministic text-layer parse never
fires on this book, so every crop costs a Gemini call (~46/chapter). Before
touching this, find out **why** the text layer reads GARBLED — `header_index.py`'s
`text_layer_health` returns GARBLED when `weird/n > 0.08` or `letters/n < 0.35`.
Print those ratios for a few pages. If the layer is genuinely garbled, there is
nothing to win here. If the threshold is too strict, relaxing it is a large
saving.

**Do not reintroduce an OCR fallback for body text.** One was added and removed
in this branch's history: it took `geom_ok` from 0 to 22/23 and cut calls from
46 to ~2, and the recovered text was unusable — page footers inside sentences
(`"12 Sold by @itachibot"`), one solution reduced to `". X"`, stems like
`"A ascarid presen GR the abnormality"`. All of it shipped as `READY`. Tesseract
is fine for finding header anchors; it is not fine for body copy on this book.

## Constraints — these are product rules

- **Never auto-correct content.** Flag it (`_review_reasons`, `qa_status`) and
  let a human decide. Signed-off by the product owner.
- **Never weaken a check to reduce the flag count.** The owner's goal is zero
  *wrong data*, not zero flags. This branch already proved both directions:
  q4 once shipped wrong data with zero flags, and six carry flags turned out to
  mark correct attributions. Add evidence instead of removing checks — that is
  how the carry flags legitimately reached zero.
- **A deterministic claim must never be overridden by a model verdict.**
- **Ship nothing you cannot prove.** An item that fails validation is discarded
  and left missing (the gate names it by `q_no`) rather than shipped as a
  plausible guess. The owner runs 18,000+ questions with no review capacity.
- **Add a regression test for every fix.** The suites that matter are the
  `test_*_regressions.py` files.

## Definition of done

1. q4's bleed fixed at its real root cause, with the crop image as evidence for
   which cause it was.
2. All 5 REVIEW_NEEDED rows explained — each either fixed or shown to be a
   false positive in the check itself.
3. `pytest -q` still shows exactly 21 failures.
4. A fresh OPH chapter 1 run showing `export gate CLEAN`, `lock=yes`, and a
   REVIEW_NEEDED count you can account for line by line.
