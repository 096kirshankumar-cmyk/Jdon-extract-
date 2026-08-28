# HANDOFF — QBank extraction pipeline

Read this whole file before touching code. The **constraints** section is
product policy, not style preference. The **already done** section exists so
you do not redo work that is already on this branch.

- Repo: `https://github.com/096kirshankumar-cmyk/Jdon-extract-`
- Branch: `arena/01a03242-jdon-extract`
- Start from: **`1bf236f`**

## Environment

```bash
apt-get update && apt-get install -y poppler-utils tesseract-ocr tzdata
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest PyMuPDF pyflakes
```

**poppler and tesseract are both required.** `pdftotext_page` catches only
`subprocess.TimeoutExpired`, so a missing `pdftotext` raises `FileNotFoundError`
and callers swallow it as "empty text layer" — every conclusion you draw about
the text layer would be wrong. `_ocr_page_text` shells out to `pdftoppm`.

Gemini keys from env (`GEMINI_API_KEYS` or `GEMINI_API_KEY_1..N`). Never print
them.

## Test baseline

```
.venv/bin/python -m pytest -q
→ 502 passed, 1 failed, 17 skipped        (without tesseract)
→ 503 passed, 0 failed, 17 skipped        (with tesseract)
```

The single failure without tesseract is
`test_pipeline_json.py::UnifiedImageOwnershipTests::test_ocr_geometry_claims_when_text_layer_garbled`.
Two more are `skipUnless`-guarded because they need developer-machine fixtures
(`/home/user/book2/book.pdf`, `/home/user/audit/run_obg_ch3/...`).

If you see more than that, you broke something. Run the failing test and read
its actual error before classifying it — an earlier revision of this file
labelled 19 stale-fixture failures as "environmental" and that cost a round
trip.

## Architecture

One engine. `qbank_pipeline.main()` is a shim over `boundary_phased.run_all()`.
The old multi-pass `process_pdf` is gone.

1. **TOC → chapter page ranges** — `extract_toc_chapters()` +
   `compute_page_ranges()`. Verified against the book's own contents table.
2. **Zones** — `_resolve_zones`: Q pages / answer-key pages / S pages, clamped
   to `ch_last` by `_zone_pages_from_headers`.
3. **Visual header index** — `header_index.scan_chapter` (render + OCR).
4. **Crops** — `header_index.intervals`: one crop per block, "this header →
   next furniture header", spanning pages as strips. Multi-page blocks become
   labelled `INTERVAL part i/N … START/CONTINUES` composites.
5. **Crop → text** — `_extract_from_crops`. PDF text layer first; anything not
   CLEAN goes to Gemini. On the MARROW books the layer is GARBLED, so every
   crop goes to Gemini (`why: text_garbled_to_gemini=23`).
6. **Batching** — `CROP_BATCH_SIZE` (env `QBANK_CROP_BATCH`, default 3). Only
   `_crop_is_batchable()` admits an item: text-only, single-page QUESTION
   crops. Solutions and anything with a figure are always one-per-call.
7. **Answer key** — whole pages, dual Gemini read, escalates only on
   disagreement.
8. **Images** — `_claim_images_by_interval` runs FIRST and owns every figure
   whose (page, y) lies inside a crop interval. The per-page passes
   (`claim_page_images`, `claim_block_images_ocr`, carry) are the fallback.
9. **Gate + commit** — `_export_gate_violations` → `build_final_question` →
   `split_outputs.write_split_outputs`.

## Already done — do not redo

| Commit | What |
|---|---|
| `6a07f38` | carry claims raise `REVIEW_NEEDED`; `qa_status` propagates into `split/` and `final_export.zip` |
| `47a4522` | crop-vs-page decision is logged |
| `c4da008` | 21 dead files removed |
| `b059ad3` | zone span no longer absorbs the next chapter; unread crops discarded, never shipped |
| `4af9f02` | OCR body-text fallback **removed** (see below); furniture strip; noise detection |
| `94c095a` | stale verify verdict re-checked after the re-ask |
| `0aaa9fa` | a previous question's solution that bled into a crop is dropped |
| `095fac2` | carry claims corroborated by block-interval geometry are not flagged |
| `e6af3d0` | `QBANK_CROP_DUMP` crop-image dump |
| `21183cd` | crop intervals own the figures (carry 6 → 0 on OPH-001) |
| `d0202ad` | app.py NameError ×3; `_ledger_lock` phase_unresolved guard; `_solution_dup_pairs` call site |
| `35011b8` | live-run audit applied; 19 stale tests repaired; `_ocr_fallback` guarded |

Verified live on OPH chapter 1: answers **23/23 correct**, text clean, figures
**17/17 crop-interval owned, 0 carry, 0 unclaimed**, `export gate CLEAN`,
`lock=yes`.

**Do not reintroduce an OCR fallback for body text.** One was added and removed
on this branch. It took `geom_ok` from 0 to 22/23 and cut Gemini calls from 46
to ~2 — and the recovered text was unusable: page footers inside sentences
(`"12 Sold by @itachibot"`), one solution reduced to `". X"`, stems like
`"A ascarid presen GR the abnormality"`. All of it shipped as `READY`.
Tesseract is correct for header anchors; it is not correct for body copy on
these books.

## YOUR TASKS — in priority order

### 1. q4's crop bleed: find the actual root cause

`OPH-001-004`'s solution arrives with **696 characters** of the previous
question's explanation in front of it. `sanitize_solution_text` drops it, so
the shipped row is correct — but that is treating the symptom.

`CROP_BATCH_SIZE=1` did **not** stop it, which rules out neighbour-image
contamination. So it is exactly one of:

- **(a) the crop boundary is cut in the wrong place** — the image already
  contains the previous text. Then fix `header_index.intervals` / header
  y-detection. Note `y_hi = y0 + 14` (`header_index.py`) pads the crop 14pt
  above the header — about one line. 696 chars is far more than one line, so
  this alone does not explain it.
- **(b) the model recited** — the crop is clean and Gemini added text. Then fix
  the prompt.

**Settle it before changing code:**

```bash
QBANK_CROP_DUMP=/tmp/crops   # then run OPH chapter 1
# look at /tmp/crops/OPH-001_q4_p13.png
```

Report which of (a) or (b), with the crop image as evidence, then fix that one.
Do not fix both speculatively.

### 2. Confirm the batch default did not reintroduce bleed

`CROP_BATCH_SIZE` now defaults to 3. It was verified once on OPH-001, but
before the `_ocr_fallback` guard and the `review_queue` cleanup landed. Re-run
OPH chapter 1 at `35011b8` and report:

- the `[IMG] OPH-001: attribution …` line
- the `qa_status` counts
- whether any solution contains another question's text

### 3. Identify the 5 REVIEW_NEEDED rows

The last run reported `5 REVIEW_NEEDED` and nothing in the log says why. Carry
is at zero, so none of them is a carry. The split rows do not carry
`qa_reasons`; read the master file:

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

Classify each as a **real defect** (fix it) or a **false positive in the check**
(fix the check). Do not silence either.

### 4. Stress a different layout

Run one chapter from a different book (OBG or ANA). OPH-001 has no
near-duplicate solutions, so the `duplicate_solution` blocker has never fired
in a live run — a different layout is the only way to exercise it and the
batch path together.

### 5. Full-book quota strategy

At the default batch size a 600+ page bank needs roughly 1,600–1,700 Gemini
calls against a 480/day budget. Three options, all documented; the choice is
the product owner's:

- (a) multi-key pool — each key needs its **own Google project**, since quota
  is per project, not per key
- (b) let the daily-quota resume carry a 3–4 day run (`state.json` supports it)
- (c) batch solutions too — **not recommended**; it reintroduces exactly the
  q4-class bleed the isolation exists to prevent

## Constraints — product policy

- **Never auto-correct content.** Flag it (`_review_reasons`, `qa_status`) and
  let a human decide.
- **Never weaken a check to reduce the flag count.** The goal is zero *wrong
  data*, not zero flags. This branch has proved both directions of that
  mistake: q4 once shipped wrong data with zero flags, and six carry flags
  turned out to mark correct attributions. Add evidence instead of removing
  checks — that is how the carry flags legitimately reached zero.
- **A deterministic claim must never be overridden by a model verdict.**
- **Ship nothing you cannot prove.** An item that fails validation is discarded
  and left missing (the gate names it by `q_no`) rather than shipped as a
  plausible guess. The owner runs 18,000+ questions with no review capacity.
- **Add a regression test for every fix.** The `test_*_regressions.py` suites
  are where they go.
- **Commit on this branch, on top of `35011b8`.** Do not rewrite history or
  force-push.

## Cleanup candidates — ask before deleting

- `tools/check_answer_keys.py` — zero references anywhere.
- `tools/full_book_split.py` — referenced only in a comment in
  `split_outputs.py`, never imported.
- `docs/PHASE1_REPORT/` — an old report plus sample JSONL.
- `EXTRACTION_V2.md`, `V2_README.md`, `PIPELINE_WORKFLOW.md`,
  `REVIEW_LAYER.md` — older design docs, partly superseded by this file.
- **Keep `FORMAT.md`** — `review_queue.build_final_zip` reads it and writes it
  into `final_export.zip`. Deleting it ships every delivery package without
  its schema document.

## Definition of done

1. q4's bleed fixed at its real root cause, with the crop image as evidence for
   which cause it was.
2. A fresh OPH-001 run at the default batch size showing `export gate CLEAN`,
   `lock=yes`, and a REVIEW_NEEDED count you can account for line by line.
3. One chapter from a second book, same reporting.
4. `pytest -q` at 503 passed / 0 failed.


---

# CURRENT STATE — read this first, it supersedes the older sections above

Full-book run analysed: 631 questions, 122 BLOCKER / 350 REVIEW / 252 NOISE.
Complete cause taxonomy is in `OPH_FULLBOOK_BUG_REPORT.md`. Read that before
anything else.

## The one bug worth fixing first: intermittent answer-key failure

**~110 of the 122 BLOCKERs are this single cause.** But it is NOT uniform:

| Chapter | Result | |
|---|---|---|
| OPH-001 | `key dual Gemini agree=23/23` | worked |
| OPH-006 | 15 READY, 0 flagged | worked |
| OPH-007 | `key dual Gemini agree=25/25`, key on 2 pages | worked |
| OPH-004 / 008 / 016 / 022 | every question `missing_answer` | failed |
| OPH-018 | `key evidence for 5 row(s) (key_table_ocr)` vs 14 questions | failed |

So "the key reader is broken" is the wrong diagnosis. It is intermittent,
which points at the key-table crop or the table's printed position on specific
pages. OPH-007's key spanned two pages and still worked, so multi-page tables
are not the trigger.

**YOUR FIRST TASK.** Run ONE failing chapter (OPH-004 or OPH-018) and report
these four lines:

```bash
grep "key evidence for\|WARNING -- the dual\|key dual Gemini\|CONFLICT rows\|USED zones" <log>
```

Commit `bbe4221` added the per-method breakdown and the explicit warning
precisely because the old log could not distinguish the three sub-causes:
the crop did not render; it rendered and Gemini returned nothing; it rendered
and the two reads disagreed. **Do not guess which — get the line first.**

## Second: the solution bleed is model recitation, not a crop error

```
[SANITIZE] OPH-006-003: dropped 284 chars of the PREVIOUS question's solution
```

The dropped amount has varied 696 → 121 → 0 → 284 across runs on the same
input. A fixed crop cannot produce different amounts, so the model is writing
text that is not in the image. `sanitize_solution_text` catches every instance
so nothing bad has ever shipped, but cleanup is not a fix. `b0b7889` added a
prompt rule; it reduces the bleed but does not eliminate it.

## Third: small, distinct, real

- **3 unresolved figures** the book prints: OPH-002 p23, OPH-005 p101,
  OPH-006 p117.
- **`contaminated_question`** — question_text holding its own solution's text
  (OPH-001-007 at 89%, OPH-001-017 at 80%, OPH-002-029 at 94%).
- **Option-text fabrication** — a fallback invents option text from the
  solution's opening sentence. Decide whether this fallback should exist.

## Correction to an earlier claim in this file

An earlier revision said OPH-001 was correct. Only q1-q6 were compared against
the source PDF, and those are correct. OPH-001-007 and OPH-001-017 both carry
`contaminated_question`, so OPH-001 is not clean.

---

# RUN-49 (2026-08-27) — the disappearing question: root cause found and fixed

## The user's first report, and what the run log actually says

"OPH-013-018 was skipped entirely." Verified from the 2026-08-26 full-book run
log (41 chunks), chapter OPH-013:

```
[GATE] OPH-013: 7 export-gate violation(s) -- NOT a clean export
- unresolved_page_Q [306]: UNRESOLVED
- unresolved_page_REASK_Q [306, 307]: UNRESOLVED
- unresolved_image 307: OPH/OPH-p307-2037.webp (method=none confidence=?)
- chapter_not_locked: ... Question: printed headers prove missing/empty
  block(s) q[18] -- targeted re-ask on [306, 307]; Question re-ask unresolved:
  model blocked pages 306-307; ledger lock refused: extracted Q
  [1..17, 19..35] != key rows [1..35]
[SPLIT] OPH-013: 34 questions / 34 answers / 34 solutions
```

Not a header-detection failure and not a tall-crop timeout. Gemini's
**recitation filter refused the page IMAGE** (finish_reason 4 → `ModelBlocked`)
for p306–307, and:

1. `_gemini_crop_batch` (the path every Question/Solution takes on this book)
   had **no escape** — one ledger row, `break`, question gone. The page path
   has had an OCR escape since RUN-42; the crop path never called it.
2. `_printed_header_reask` — the last resort, which fires *precisely* because
   the printed header proves q18 exists — re-sent the **same pages as images**
   to the **same filter**, got the **same refusal**, and `continue`d.
3. `qbank_validator.check_chapter` computes `qns`/`s` at chapter scope, but the
   `numbering_gap` / `numbering_start` appends were **indented inside the
   `suspect_truncated_table` loop**, so they only ran for a chapter with ≥2
   solutions sharing a table header. OPH-013 has none → the 1..17,19..35 hole
   was never flagged anywhere. The user found it by eye.
4. `/review/lookup` answered `koi row nahi mili: 013-018` with no hint: the
   hint's row pool came from `lookup_questions(out, "", None)`, and an empty
   term returns `[]` by contract — the pool was always empty. `013-018` also
   failed the letter-prefixed chapter regex, so the chapter never resolved.

**This is not unique to q18.** The same log shows OPH-028 with
`model blocked pages 610-611; ledger lock refused: extracted Q [1..6, 8..23]
!= key rows [1..23]` → **OPH-028-007 is missing the same way**. Expect more;
after this fix the validator names every one of them by q_no.

## What changed (all four defects)

| # | file | change |
|---|------|--------|
| 1 | `boundary_phased.py` | new `_crop_ocr_recovery`: a blocked crop OCRs its own strip pages and keeps **only that crop's q_nos**; anything else on the page goes to `orphan_items`, never into the row |
| 2 | `boundary_phased.py` | `_printed_header_reask` gets the same OCR escape on `ModelBlocked` |
| 3 | `qbank_validator.py` | `numbering_gap` / `numbering_start` moved out of the table loop — unconditional |
| 4 | `boundary_phased.py` | `_ledger_lock` now says `MISSING q[18]` / `EXTRA q[99]` instead of only dumping both sets |
| 5 | `app.py` | lookup hint reads the real `questions.jsonl` and resolves `013-018` → `OPH-013` |

Recovered text stays `_ocr` → the row is **REVIEW_NEEDED**, never silently
trusted. If OCR is unavailable or the text is refused too, the item is left
**missing and named by q_no** — the honest outcome is preserved, it just stops
being invisible.

Tests: `test_missing_question_recovery_regressions.py` (17 tests). Suite:
**533 passed, 1 failed, 17 skipped** — the failure is
`test_ocr_geometry_claims_when_text_layer_garbled`, which also fails on the
pristine `3689388` checkout (verified with the changes stashed) because this
sandbox has no `tesseract`/`pdftoppm`. The Railway image installs both
(`Dockerfile`: `poppler-utils tesseract-ocr`), so the OCR escape is live there.

## Still open (unchanged by RUN-49)

- Solution bleed (model recitation) — `sanitize_solution_text` catches it.
- `contaminated_question` on OPH-001-007/-017, OPH-002-029.
- 3 unresolved printed figures: OPH-002 p23, OPH-005 p101, OPH-006 p117.
- The 2026-08-26 run stopped at chapter 28 ("paused at daily limit"): 25
  chapter files, `🧪 Validation: 214 flags`, digest 13 blocker / 125 review /
  228 noise. Those numbers predate this fix; the gap flags were not firing.

---

# RUN-50 (2026-08-27) — phantom questions: the answer key is the census

User verified from the PDF (screenshot, book p267): OPH-011's block ends at
**Question 23**, then the printed Answer Key. The run log agrees:
`ledger lock refused: extracted Q [1..25, 30, 33, 34, 134] != key rows [1..23]`.
But the pipeline still SHIPPED rows 24,25,30,33,34,134 (deployed review screen
showed `OPH-011-024` stem + four EMPTY options and `OPH-011-134` = the same
stem with the real options -- one printed question split across two rows).

Root cause: the VISUAL header index invented `Question N` bands on p262-267
(reading the key table / page numbers as headers) and `_printed_header_reask`
imported them. The old phantom guard only ran when `_printed_q_max` was proven
and never at commit.

Fix (all PDF-grounded, no guessing):
1. `_q_number_ceiling()`: when the answer key is a contiguous 1..N series it is
   the chapter census -> N is the ceiling. (Non-contiguous/absent key falls
   back to weaker maxima; nothing proven -> None -> never deletes.)
2. `_quarantine_phantom_questions(...)`: at commit, any Q/A/S item with q_no >
   ceiling goes to orphans.jsonl, never becomes a row.
3. `_question_shape_ok` + `_crop_item_shippable`/`_crop_item_ok`: four option
   LETTERS is not completeness -- every option must carry text (OPH-011-024).

Tests: `TestKeyCensusQuarantinesPhantoms`, `TestEmptyOptionsDoNotShip` in
test_missing_question_recovery_regressions.py (now 25 tests).

# RUN-51 (2026-08-27) — contaminated_question false positive on fill-in-the-blank stems

Live `OPH-001-007` (verified on deployed lookup, qa=READY, CORRECT):
stem "After conception, the canal of Schlemm appears by ____." solution
restates it. Flagged contaminated_question (89% of stem tokens in its own
solution) ONLY because `_QUESTION_SHAPED_RE` had no branch for a trailing
blank -- a fill-in stem has no "?" and no question word, so the shape test
failed and the token-overlap rule fired.

Fix: added `_{3,}` (a run of underscores = a blank) to `_QUESTION_SHAPED_RE`.
This is NOT weakening: real contamination (stem that IS the solution, or opens
with explanation language) has no trailing blank and still trips the reverse-
containance / explanation-opener branches (both regression-tested).

---

# RUN-52 (2026-08-27) — make ONE final re-run actually reach the blocked chapters

`unlock_gated_chapters` unlocked only missing_*/bad_options/duplicate_solution,
so OPH-013 (q18) and OPH-028 (q7) -- whose gate kinds are `unresolved_page_Q` /
`unresolved_page_REASK_Q` (the recitation-blocked pages) -- stayed in
chapters_done and a resume SKIPPED them, meaning the RUN-49 OCR escape would
never have run. Added those two kinds to the unlock set so a single final
re-run re-extracts them. `chapter_not_locked` is deliberately NOT added (it
would re-run every chapter forever). Test:
TestFinalRerunReachesBlockedChapters.

---

# RUN-53 (2026-08-28) — a successful OCR recovery must not keep the chapter blocked

OPH-013 live (post-RUN-49 run): q18 WAS recovered via the OCR escape, but the
chapter still showed BLOCKER `phase_unresolved: Q crop unresolved (blocked)`,
because the blocking "unresolved" note was appended BEFORE/unconditionally of
the recovery, and `_ledger_lock` refuses on ANY note containing "unresolved".
So the recovery was invisible to the lock.

Fix: in `_gemini_crop_batch` and `_printed_header_reask`, attempt OCR recovery
first; append the blocking "unresolved" note ONLY when recovery yields nothing.
A successful recovery leaves a non-blocking note; the recovered rows stay
`_ocr` -> REVIEW_NEEDED (honest), but the chapter is no longer hard-blocked.
Test: TestRecoveredCropDoesNotBlockLock.

---

# RUN-54 (2026-08-28) — blank options crashed the WHOLE chapter (OPH-020 lost)

Live log traceback: build_final_question -> _answer_option_mismatch ->
_overlap_letter -> `max(scores)` with scores built from `opt_toks`, which is
EMPTY when every option text is blank (the OPH-020-007 bad_options row).
ValueError propagated to run_all's per-chapter crash handler, so the entire
chapter was dropped instead of being flagged.

Fix: `if not opt_toks: return None` in _overlap_letter -- with no option text
there is nothing to compare, so no mismatch; the row keeps its bad_options
REVIEW flag instead of taking the chapter down.
Test: TestBlankOptionsDoNotCrashChapter.
