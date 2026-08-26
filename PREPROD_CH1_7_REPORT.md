# Pre-production validation — chapters 1–7

Final check before you start the production run. Three things were done:
the quota-day timezone fix, a full 7-chapter run against ground truth, and a
code review of everything that ships in the image.

**Verdict: ready.** 132 records extracted, one real defect found, root-caused,
fixed, and re-verified live.

---

## 1. Quota day boundary (TZ fix)

`today_stamp()` used `time.strftime("%Y-%m-%d")` — the *container* clock.
Google resets Gemini RPD at **midnight US/Pacific**. On a UTC host the two
disagree for eight hours every day, and this workspace demonstrated it live:

```
today_stamp() = 2026-08-11      <- Pacific (correct)
UTC date      = 2026-08-12      <- what the old code used
UTC hour      = 4
```

In that window the pipeline believed all 6 keys were fresh while Google still
counted them against the previous day. The first call on each key 429'd, the
key was parked `exhausted`, and the pool burned all six for nothing — with no
key left, the run exits.

**Fixed:** the day is now stamped in `America/Los_Angeles`, overridable via
`QUOTA_RESET_TZ`. If the image has no tzdata, it falls back to a fixed UTC-8
offset — during US DST that is one hour *late*, which is the safe direction
(late means we under-count; we never think a spent key is fresh).
`tzdata` added to the Dockerfile and `QUOTA_RESET_TZ` set as an ENV.

6 regression tests.

---

## 2. Run: chapters 1–7 (pages 5–131)

All seven into one output dir, so cross-chapter binding is exercised too.
Wall clock 04:39 → 05:34 UTC (~55 min).

| ch | title | got | OCR ground truth | gate |
|---|---|---|---|---|
| 1 | Gametogenesis | 19 | 19 | CLEAN |
| 2 | Pre-Embryonic Phase | 13 | 13 | CLEAN |
| 3 | Embryonic Phase | 16 | 16 † | 2 orphans (benign) |
| 4 | Placenta, Fetal Membranes | 14 | 14 | CLEAN |
| 5 | Pharyngeal arches, Skeletal & Muscular | 24 | 24 | CLEAN |
| 6 | Cardiovascular and Respiratory | 25 | 25 | CLEAN |
| 7 | Alimentary, Hepatobiliary, Pancreas | 21 | 21 | 1 real defect → fixed |

**132 records, every count matching ground truth.**

† OCR read only up to q15 on p45; the page actually prints
`Solution to Question 16` and tesseract dropped the last digit. The record is
real and correct — the usual OCR-undercount trap, so gaps were checked rather
than totals.

### Binding and ordering

```
export order == sorted order: True
ANA-001: 19 q, 1..19, gaps=none, foreign=none
ANA-002: 13 q, 1..13, gaps=none, foreign=none
ANA-003: 16 q, 1..16, gaps=none, foreign=none
ANA-004: 14 q, 1..14, gaps=none, foreign=none
ANA-005: 24 q, 1..24, gaps=none, foreign=none
ANA-006: 25 q, 1..25, gaps=none, foreign=none
ANA-007: 21 q, 1..21, gaps=none, foreign=none
```

No gaps, no foreign chapter q_nos, no misordering across seven consecutive
chapters. 110 images attached; solution-image distribution
`{0:62, 1:61, 2:7, 3:1, 4:1}` — the dynamic cap is passing 4-image solutions
that the old hardcoded 3 would have refused.

### Field audit — 132 records, 1 defect

Everything else clean: no blank stems, all option sets exactly 4, no blank
option text, every answer present and pointing at a real option, no
`stem_suspect` / `options_suspect` flags, every record has `source_pages`.

### Chapter 3's two orphans are benign

Both are option-sets with no `q_no`. Checked against the export:

- `{'C': '3 - 2 - 4 - 1', 'D': '1 - 2 - 4 - 3'}` → already owned by `ANA-003-004`
- `{Lateral plate / Intermediate / Neural crest / Paraxial mesoderm}` → already
  owned by `ANA-003-007`

Duplicates from the batch overlap window, correctly refused rather than
double-attached. No content lost. The gate flags them because it cannot prove
ownership — correct conservative behaviour.

---

## 3. The one real defect: ch7 q19 (pp. 129–130)

`ANA-007-019` exported with an **empty solution**. Not a model failure — a
page-boundary ownership bug.

The heading `Solution to Question 19:` sits at the very **bottom of p129**;
its body starts at the **top of p130**. The model emitted that body as q20's
solution, so q20 shipped 1486 chars containing *both* solutions glued
together, with the literal marker still in the middle:

```
Failure of fusion of the dorsal and ventral pancreatic buds ...   <- q19's
The developing pancreatic ducts usually fuse ...                  <- q19's
Solution to Question 20:                                          <- the seam
The spleen develops from the mesoderm in the dorsal mesogastrium. <- q20's
```

Then the recovery made it worse. The targeted retry re-read the page and came
back with the **correct** q19 text — and the wrong-owner guard rejected it:

```
[RETRY] blocked foreign solution fragment for q19 (empty solution):
        first line exists verbatim in q20's solution
```

The guard was right that the text was duplicated and wrong about who owned it.
It assumes the sibling already holding the text is its rightful owner; here the
sibling was the thief.

### Two fixes

**Sweep 2c — foreign solution HEAD.** The mirror of the existing 2b. 2b only
looked at headers naming a *different* question, so a record naming **itself**
part-way through fell straight through. Now: split at the self-header, keep the
tail (correctly labeled as its own), donate the head to the nearest preceding
question that has **no** solution. It can only ever write into an empty field,
so it cannot overwrite good data. No eligible owner → strip only if verbatim
elsewhere, else keep and flag.

**Retry guard.** `_solution_fragment_foreign` now checks whether the donor is
itself a glued double-block; if the matched text sits *before* the donor's own
self-label, the fragment is the rescue rather than the contamination and is
accepted. Genuine contamination is still blocked.

This pattern appeared in 4 of 132 records (`ANA-001-019`, `ANA-007-011`,
`ANA-007-018`, `ANA-007-020`) but only cost data in the one case where the
preceding question had nothing of its own. The other three are left untouched
by design.

### Re-run of ch7 with the fix

```
ANA-007-019: solution 766 chars -> 'Failure of fusion of the dorsal and ventral...'
ANA-007-020: solution 694 chars -> 'The spleen develops from the mesoderm...'
defects: ZERO
q_nos 1..21, gaps: none
no bleed (q19 lacks spleen, q20 lacks divisum): True
```

`21 questions (0 missing answer, 0 missing solution, 0 missing stem, 0 bad
options)` — was 1 missing solution. Gate violations 4 → 1 (the remaining one is
the same benign unclaimed-fragment class as ch3).

Note the sweep did **not** need to fire on the re-run — the model split the
pages correctly this time. That is the point: the defect is non-deterministic,
so the deterministic sweep is what makes it safe at 110-chapter scale.

---

## 4. Code review

- **Syntax/import:** all 9 shipped modules compile.
- **Docker COPY whitelist:** 8 modules copied. The 2 not copied
  (`cleanup_extracted_tables.py`, `test_pipeline_json.py`) are dev-only and
  verified not imported by anything in the image.
- **Secret scan:** no API keys, PATs, or tokens in any tracked file.
- **Tests:** **235 total, 9 pre-existing failures, 0 new.** The 9 need
  poppler/tesseract and fail identically on a clean checkout.

---

## Known-open, none blocking

1. **Unclaimed q_no-less fragments** (ch3 ×2, ch7 ×1) — flagged, not dropped;
   all verified as overlap duplicates already owned. The gate cannot prove that
   automatically, so it reports them. Expect a couple per chapter.
2. **Throughput, not correctness.** `_pace_gemini_call()` uses one global
   timestamp, so the 5-second spacing applies across all keys — ~12 RPM
   regardless of key count. ~8 min/chapter observed. For 110 chapters budget
   roughly 15 hours of wall clock. You said call volume is fine; this is only
   about elapsed time. Per-key pacing was offered and declined.
3. **`ANA-009-005`** empty solution — pre-existing, unrelated to this run.

## Production checklist

- Set all 6 keys (`GEMINI_API_KEY_1..6` or `GEMINI_API_KEYS` comma-separated).
  The pool logs `N key(s) loaded` at startup — check that line says 6.
- Keys must be from **separate Google projects**, or they share one quota.
- Daily budget 6 × 480 = 2880 calls; day rolls at midnight Pacific
  (12:30 PM IST).
- Resume is safe: a chapter is marked done only after it completes fully, so
  an interrupted run re-processes that chapter whole. Just re-run the same
  command with the same output dir.
