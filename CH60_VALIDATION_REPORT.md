# Chapter 60 held-out validation — run-22

Second held-out chapter (after ch. 38), chosen blind. **ANA ch. 60 "Bones,
Joints and Cartilage", printed pages 1142–1169 (28 pages).**

Command: `python3 test_v2_chapter.py book.pdf ANA 60 0 out_ch60` — exit 0, ~3½ min,
6 batches, 9 Gemini calls, 1 key.

## Headline

| metric | ch. 38 final | **ch. 60** |
|---|---|---|
| gate violations | 5 | **1** |
| validator flags | 9 | **2** |
| questions | 31 | **30** (q1–30, no gaps, no dupes) |
| missing stem / answer / solution | 0 / 0 / 0 | **0 / 0 / 0** |
| bad options | 0 | **0** |
| image refs / broken / unmatched | 38 / 0 / 0 | **23 / 0 / 0** |
| vision fallback calls | 0 | **0** |
| rescue calls | 0 | **0** |
| peak RSS | — | 550 MB |

Cleanest chapter run so far.

## Did the run-22 work hold up?

**`options_suspect` / `manual_review` — no false positives.** Both fields ship
in every row (`stem_suspect`, `options_suspect`, `manual_review` all present in
the export schema). Detector fired on **0 of 30** records here, and still fires
on ch. 38 q13. That is the intended shape: loud on the known defect, silent on
clean data. Cumulative false-positive count is now **0 across 141 questions**
(ch. 9, three ch. 38 runs, ch. 60).

**D1 (garbled headings)** — 0 `bad_options`, 0 rescue calls.

**D2 (page-position orphan inference)** — behaved *correctly by refusing*:

```
[ORPHAN] page=1158: inferred owner q15 (page-position inference: last printed
solution heading at/before page 1162) but its solution is already complete --
fragment kept in orphans.jsonl, nothing overwritten
```

The guard earned its keep — see the real defect below.

**D3 (carry-seed lookback clamp)** — every seed in-chapter (1143, 1153, 1157,
1163, 1166); no ch. 59 bleed.

**Dynamic image cap** — raised to 4 once, for q8's question images, declared by
`positional_carry`. Correct: p1144 genuinely carries 4 figures for that question.

## Image attribution: 23/23 correct

All 23 `[IMG]` claims re-derived from `image_positions_on_page` + OCR geometry.
Two claims looked wrong and both check out:

- **p1158 → q7 (solution), ×2.** p1158 is not in q7's `source_pages` (which end
  at 1157). But OCR of p1158 shows `Solution to Question 8:` at y=175pt, and the
  two figures sit at y=452 and y=224 — **above** it. Everything above the topmost
  anchor belongs to the previous block, i.e. q7. Correct, and confirms the ch. 38
  rule generalises. `source_pages` tracks the *text* batch, so a figure spilling
  onto the next page will always look out-of-range; that is a reporting artefact,
  not a mis-attribution.
- **q8 with 4 question images** — the dynamic cap, as designed.

Sweep also correctly declined two truncation retries (q3, q21) because the
dangling `:` was explained by an attached image.

## The one real defect: orphan on p1158–1162 (pre-existing)

Gate's single violation. A solution fragment:

> "The lamellae of the cancellous/spongy bone are arranged in a meshwork, and
> hence do not form Haversian systems. The diaphysis and outer cortex of long
> bones are predominantly made of compact bone…"

**It belongs to q7.** OCR of p1158 shows it printed directly above
`Solution to Question 8:`, continuing q7's solution from p1157. q7 currently
ends at "…communicate with each other by Volkmann's canal." — a complete
sentence, so no truncation heuristic fires.

D2 inferred **q15**, which is wrong (q15 is about hairpin bends of end arteries
in the metaphysis). It inferred q15 because the fragment arrived in the batch
covering pages 1158–1162 and `last_block_on_page` resolved against the batch's
last page, 1162 — not the page the fragment was printed on, 1158.

**Nothing was corrupted**: the "solution already complete" guard blocked the
write, and the fragment is preserved in `orphans.jsonl`. This is the guard
working exactly as intended — it converted a silent data-corruption bug into a
visible, honest orphan. But the underlying inference is still resolving against
the wrong page.

Same class as ch. 38's `last_qn_in_batch` trap: **batch-level position is not
fragment-level position.** Fixing it needs the fragment's own page, which the
S-pass does not currently return. Logged, not fixed — a wrong-page guess that
happens to hit an *incomplete* solution would corrupt data, and the guard is
the only thing standing in the way.

## NEW BUG FOUND AND FIXED: silent chapter truncation

Setting this chapter up exposed a much bigger problem than anything in the run.

`extract_toc_chapters()` defaulted to `toc_page_range=(1, 3)`. The MARROW
Anatomy contents table spans **five** pages. So:

```
scan (1,3) -> 42 chapters detected, max chapter_no 42
scan (1,8) -> 63 chapters detected, max chapter_no 63
```

**Chapters 43–63 did not exist as far as the pipeline was concerned** — printed
pages ~800–1217, roughly a third of the book. `process_pdf` iterates the
detected list, so those chapters were never attempted. No error, no warning, no
gate flag: the gate can only report on chapters it knows about. A "successful"
full-book run would have quietly shipped two thirds of the book. Ch. 60 itself
was unreachable before this fix.

**Fix:** `TOC_SCAN_LAST_PAGE = 8` (new constant), plus `_longest_toc_run()`,
which keeps only the longest run starting at chapter 1 with strictly increasing
chapter numbers and non-decreasing start pages. Over-scanning is now safe:
body-text lines matching `<n> <title> <n>` are rejected because they break the
sequence, while an out-of-sequence line *inside* a real table is skipped rather
than ending it. Duplicate listings keep the first occurrence.

Verified no regression — ranges are byte-identical for already-validated chapters:

```
ch9  -> 146-163
ch38 -> 666-702   (unchanged)
ch60 -> 1142-1169 (newly reachable)
```

## Tests

`Run22TocTruncationTests` (7) — default reaches past page 3, 63-chapter table
kept whole, body noise skipped without truncating the table, run must start at
chapter 1, duplicates keep first, backwards page ends the run, empty input safe.

**Baseline: 9 failed / 184 passed / 1 skipped / 16 subtests.** The 9 failures
are the same pre-existing ones. (Install `poppler-utils` + `tesseract-ocr`
first or 4 unrelated tests fail spuriously.)

## Open items

1. **Orphan page-position inference resolves against the batch's last page, not
   the fragment's page** (above). Needs per-fragment page from the S-pass.
2. q7's solution is missing its last two sentences — the data-level symptom of 1.
3. Still open from before: wider chapter-boundary stitching, merged image
   resolution calls, and ch. 11/12/14/19 never re-validated after the cap fix.
4. Chapters 43–63 have now *never* been run. They were unreachable until this
   commit, so the "14-chapter review" covered a book the pipeline could only
   partly see.
