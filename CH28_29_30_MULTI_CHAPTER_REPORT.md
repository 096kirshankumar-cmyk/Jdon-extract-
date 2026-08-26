# Chapters 28–30 back-to-back run — run-23

Three **consecutive** chapters into ONE output dir sharing ONE `state.json`,
so any bleed between adjacent chapters would surface. Total ~25 min, all three
`EXIT=0`.

| | ch. 28 | ch. 29 | ch. 30 |
|---|---|---|---|
| pages | 471–500 | 501–520 | 521–555 |
| questions | 27 (q1–27) | 15 (q1–15) | 24 (q1–24) |
| **printed in the book** | **27** | **15** | **24** |
| export gate | **CLEAN** | **CLEAN** | 4 violations |
| missing stem/answer/solution | 0/0/0 | 0/0/0 | 0/0/0 |
| bad options | 0 | 0 | 1 |
| unmatched images | 0 | 0 | 0 |
| image manifest rows | 40 | 22 | 46 |
| vision calls | 0 | 0 | 0 |
| batches | 7 | 4 | 8 |
| foreign-chapter drops | 0 | 0 | 0 |

## Chapter binding: clean

This was the point of the test, and it passed.

- **Counts match the book.** I OCR'd every page of all three chapters and took
  the highest printed `Question N` heading: **27 / 15 / 24** — exactly what was
  extracted. Nothing was lost at a chapter cut.
- **Contiguous numbering**, q1..N in each, **no gaps, no duplicates**.
- **Zero page bleed**: no record's `source_pages` reach into a neighbouring
  chapter, in either direction.
- **`foreign-chapter q_nos dropped: 0`** in all three — the failure mode from
  the ch. 2/4 review did not reappear across two live chapter boundaries.
- Shared `state.json` across the three runs caused no cross-contamination.

## The one real data defect: ch. 30 q16 option D

`bad_options 16: options=['A','B','C','D']` — four option slots, but **D was
`null`**.

Ground truth (OCR p529/p530): the question is **split across a page break**.
p529 ends after `c) Ascending pharyngeal artery`; **`d) Lingual artery` is the
first line of p530.** The Q-pass emitted `{"A":…, "B":…, "C":…, "D": None}`.

`find_incomplete_records` correctly flagged it, and `targeted_retry` asked for
q16's options in **both** rounds. The patch was thrown away both times:

```python
rec["options"].setdefault(str(k).strip().upper(), v)   # ← key "D" already exists
```

`setdefault` only fills **absent** keys. `"D"` was present with value `None`,
so the model's correct `"Lingual artery"` was silently discarded — twice.

The blast radius was wider than one option. Because the round scored **0
fixes**, the "no progress this round -- stopping" rule fired and **cancelled
the retry for an unrelated q7 solution gap in the same chapter**. One
unfillable field disabled retries for everything else.

**Fixed:** fill a slot when it is missing **or blank**, never overwrite real
text, and don't count an empty patch value as progress.
`Run23BlankOptionHealTests` (6) covers heal-blank, heal-whitespace, still-add-
absent, never-overwrite-good, empty-patch-is-not-progress, and the detector.

## Also fixed: TOC scan now grows (your 110-chapter PDF)

You asked about a 110-chapter book. The run-22 fix hard-coded an 8-page scan,
which is a book-specific constant in disguise — it fits 63 chapters with short
front matter and nothing more.

I built a synthetic 110-chapter PDF (4 pages front matter, 11-page contents
table, then body pages containing `<n> <words> <n>` noise lines) and ran both
versions:

```
OLD (pinned 8 pages):   40 chapters   <-- 70 chapters silently lost
NEW (growing scan):    110 chapters   contiguous 1..110, noise rejected
```

The scan now starts at `TOC_SCAN_LAST_PAGE` (8) and widens by
`TOC_SCAN_GROW_STEP` (8) until the chapter count **stops growing**, capped at
`TOC_SCAN_MAX_PAGE` (40). It costs no API calls — `pdftotext` on 40 pages is
~0.2 s — and `_longest_toc_run` still rejects body-text noise, so widening is
safe. It also warns loudly if it hits the ceiling, instead of truncating
quietly. On the Anatomy book: still 63 chapters, ranges unchanged
(ch9 146–163, ch28 471–500, ch29 501–520, ch30 521–555, ch38 666–702,
ch60 1142–1169).

`Run23GrowingTocScanTests` (6) covers grow-while-productive, stop-immediately-
when-not, honour-an-explicit-window, respect-the-ceiling, and reject body noise.

## Transient API errors — handled, worth watching

Two Google-side failures hit ch. 30, both recovered:

- `503 model is currently experiencing high demand` on the Q-pass, batch 521
- `504 Deadline expired` on the S-pass, batch 550

No data was lost — the split-and-retry logic absorbed both. But note the 503
retry came back as a **10-page** batch (521–530) instead of the usual 6, which
is the same batch that produced q16's split-page miss and two `no q_no`
orphans. Wider batch = more chance of a boundary miss. Worth capping the
post-failure retry at `PAGES_PER_GEMINI_CALL`.

## Remaining ch. 30 flags (no data loss)

7 unresolved orphans, all recovered elsewhere — I checked each fragment's text
against `questions.jsonl`: the "Retraction" option set, the "Abducens nerve"
option set and the facial-artery branches table are all present in real
questions. These are duplicate views from the overlap window, not lost content.
The remaining validator flags (`truncated_solution` 3,
`solution_recitation_dump` 4, `foreign_solution_segment` 3) are review hints,
not missing data — every one of the 66 questions has a stem, four options
(after the q16 fix), an answer and a solution.

`[CRITIQUE] q16: cannot verify from available page(s)` is itself a symptom of
the same page-split: the critique pass was handed the page range that does not
contain the full question.

## Open items

1. Cap the post-5xx retry batch at `PAGES_PER_GEMINI_CALL` (the 10-page batch
   above).
2. A question split across a page break is a recurring weak spot — the Q-pass
   should treat "options run off the bottom of the page" as a continuation
   signal, the way solutions already do.
3. Still open from before: orphan owner inference uses the batch's page span
   rather than the fragment's page; wider chapter-boundary stitching; merged
   image-resolution calls; ch. 11/12/14/19 never re-validated after the cap fix.
