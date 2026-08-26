# OPH Full-Book Bug Report — cross-verified

Source data: `data/questions.jsonl`, the REVIEW DIGEST (631 questions,
122 BLOCKER / 350 REVIEW / 252 NOISE), the OPH-018 run log, and the source PDF
(MARROW ED8 Ophthalmology) fetched from Drive.

**Verification level is stated per bug.** Chapter 1 was compared line-by-line
against the source PDF. The rest is from the digest and the log — full
PDF-level cross-verification of 631 questions is not feasible in one pass.

---

## BUG 1 — Answer-key table extraction fails per chapter (DOMINANT)

**Impact: roughly 80 of the 122 BLOCKERs. This is the bug to fix first.**

Chapters where **every** question lost its `correct_option`:

| Chapter | Questions affected |
|---|---|
| OPH-004 | q1–q20 (all 20) |
| OPH-008 | q1–q26 (all 26) |
| OPH-016 | q1–q22 (all 22) |
| OPH-018 | q1–q14 (all 14) |
| OPH-011 | q24, q25, q30, q33, q34 (partial) |

**Evidence (OPH-018 log, verified):**

```
[BPH] OPH-018: key evidence for 5 row(s) (key_table_ocr)
[BPH] OPH-018: ledger lock=False (extracted Q [1..14] != key rows [1, 3, 4, 5, 14])
```

Compare OPH-001, which worked: `key dual Gemini agree=23/23 mismatch=-`.

**Mechanism.** `_attach_key_evidence` (`boundary_phased.py:2770`) has two
sources: `header_index.ocr_key_table()` → `key_table_ocr`, and the dual
Gemini read → `key_dual_gemini`. On OPH-018 the dual read contributed nothing
and OCR returned 5 of 14 rows.

**Correctly detected, not silently shipped.** The ledger lock refused and the
chapter blocked with `14 question(s) without an answer-key row`. No wrong
answer was shipped. The detection works; the extraction does not.

**Diagnostic added in `bbe4221`** — the next run prints the per-method
breakdown and warns when the dual read is absent:

```
key evidence for 5 row(s): key_table_ocr=5 | key pages [404]
WARNING -- the dual Gemini key read contributed NOTHING; every answer came
from key_table_ocr=5. Check the key-table crop and the table's printed
position on pages [404].
```

**Not yet fixed.** Three candidate causes and this line separates them:
the key-table crop did not render; it rendered and Gemini returned nothing;
or it rendered and the two reads disagreed. **One run with `bbe4221` in place
will say which.**

---

## BUG 2 — Options not extracted; a fallback fabricates option text

**Instances:** OPH-002-003, OPH-002-008, OPH-011-025, OPH-011-030,
OPH-011-033, OPH-011-034

```
missing structural fields: ['options']; export-gate bad_options: options=['A','B','C','D']
option B text reconstructed from the solution's opening sentence
```

The letters exist but the text is empty, and a fallback then **invents** the
option text from the solution's first sentence. That is fabrication, not
extraction — the flag is correct and the fallback should be reviewed, because
a plausible-looking fabricated option is worse than an obviously empty one.

Some of these also carry `question phase marked text_confidence=low`
(OPH-011-025, -033), meaning the model itself reported low confidence.

---

## BUG 3 — Answer letter contradicts the printed explanation

**Instances:** OPH-007-006, OPH-008-021, OPH-008-024

```
OPH-007-006  answer_suspect: solution opens on option-D content but
             correct_option is B (key-letter flip suspect)
OPH-008-021  printed 'Option C:' explanation line points at option D's content
OPH-008-024  printed 'Option A:' explanation line points at option B's content;
             printed 'Option C:' explanation line points at option B's content
```

This is a real correctness risk — a flipped answer key. The detector is
working. The underlying cause is likely upstream of these chapters' answer-key
failure (Bug 1), since OPH-008's key is missing entirely.

---

## BUG 4 — Solution misassignment between adjacent questions

**Instances:** OPH-002-002, OPH-008-002, OPH-018-014, OPH-019-001

```
OPH-008-002  duplicate_solution: q2 and q3 ship near-identical solution text
             (similarity 1.00 >= 0.85) -- label misassignment suspect
OPH-018-014  C1: q14 received the segment folded into q15's answer
             (printed header boundary) -- verify manually
OPH-002-002  solution text split at printed 'Solution to Question N:' header
             boundary (deterministic C1 backstop)
```

**Similarity 1.00 means q2 and q3 shipped byte-identical solutions.** This was
caught by the `duplicate_solution` gate that commit `5177207` had reduced to
dead code and that `d0202ad` restored — it fired here for the first time and
caught a real 100% duplicate that verify and the set cross-check both passed
silently. **The restored gate is doing exactly what it was for.**

---

## BUG 5 — Figure problems (several distinct kinds)

**(a) Dangling `[IMG]` marker** — text references a figure nobody owns:
```
OPH-004-003, -005, -013, -016  |  OPH-008-009, -026  |  OPH-016-018
OPH-018-014  |  OPH-004-010 (2 tokens, 0 files)
img_placeholder_count_mismatch (solution): text has N [IMG] token(s),
interval owns 0 file(s)
```
Either the model invented the marker or the figure was lost. Both are real.

**(b) Count mismatch** — `OPH-016-016`: 1 token, 2 files. Ordering ambiguous.

**(c) Wrong-owner figure** —
```
OPH-002-002  figure_page_mismatch: solution image OPH/OPH-002-002_SOL_01.webp
             extracted from page 44 but q2's printed anchors are on [24, 34]
```
The figure is 10 pages away from the question it is attached to.

**(d) Lost figure** —
```
OPH-008-001  missing_declared_figure_question: model declared
             has_figure_in_question but no question image is attached
             (printed figure exists on [155] but exhausted all ownership levels)
```
A figure the book prints on page 155 was not claimed by any owner.

---

## BUG 6 — Solutions missing or truncated

```
OPH-018-015  Solution text is completely empty in the JSON while the source
             text exists on page 420-422   (widened-crop retry also failed)
OPH-004-020, OPH-011-033, OPH-011-034  missing_solution: header-only model
             answer stripped by sanitize
```

OPH-018 q15 is the clearest case: the book has the text on pages 420–422, the
crop was cut, and the model returned only the header. The retry widened the
crop to p420-422 and still failed.

---

## What is NOT broken (verified against the source PDF)

**CORRECTION.** An earlier revision of this report said "OPH-001 is correct".
That was based on comparing only q1-q6. The digest shows OPH-001-007 and
OPH-001-017 both carry `contaminated_question`, so OPH-001 is NOT clean. Only
q1-q6 were verified, and they are correct.

**OPH-001 q1-q6, verified line-by-line against the source:**

| Check | Result |
|---|---|
| q1–q6 stems | exact match to source |
| q1–q6 options | exact match |
| q1–q6 answers | 6/6 match the source key table (1→B, 2→D, 3→D, 4→A, 5→B, 6→C) |
| q1–q6 solutions | exact match, including q3's two-sentence answer |
| q4 bleed | **gone** — solution starts at "The macula is fully developed by 4-6 months", no q3 text |
| Figures | 17/17 crop-interval owned, 0 carry, 0 unclaimed |

Earlier OCR-based runs produced garbage here (`"A ascarid presen GR the
abnormality"`, `"oe SS «\ni i ity?"`). The Gemini crop path fixed it.

---

## Priority order

1. **Bug 1** — answer-key table. ~80 of 122 BLOCKERs. Needs one run with
   `bbe4221` to identify which of the three causes it is.
2. **Bug 2** — the option-text fabrication fallback. Fabrication is worse than
   an obvious gap; review whether it should exist at all.
3. **Bug 4** — solution misassignment. The gate catches it now, but the cause
   is upstream.
4. **Bugs 3, 5, 6** — smaller counts, mostly downstream of Bugs 1 and 4.

---

## Not verified

- Only OPH-001 was compared against the source PDF. The other 27 chapters are
  assessed from the digest and the OPH-018 log only.
- Only 2 of 14 digest chunks and 1 of 17 log chunks were readable — the Drive
  proxy failed intermittently. Bug counts above are therefore lower bounds.
- The remaining ~42 BLOCKERs (122 total minus the ~80 from Bug 1) were not
  individually enumerated.


---

# Complete cause taxonomy

Every distinct reason string in the digest, grouped. Counts are instances
observed across the readable chunks (2 of 14 digest chunks, 1 of 17 log
chunks), so they are lower bounds.

## BLOCKER causes

| # | Reason | Cause | Instances seen |
|---|---|---|---|
| 1 | `missing_answer` | Answer-key table not read. `_attach_key_evidence` got only `key_table_ocr` and the dual Gemini read contributed nothing | OPH-004 q1-20, OPH-008 q1-26, OPH-016 q1-22, OPH-018 q1-14, OPH-022 q1-24, OPH-011 partial. **~110 instances, the dominant cause** |
| 2 | `bad_options` + "option X text reconstructed from the solution's opening sentence" | Options not extracted (letters present, text empty), then a fallback **fabricates** text from the solution's first sentence | OPH-002-003, -008, OPH-004-*, OPH-011-025/-030/-033/-034, OPH-020-007, OPH-025-002, OPH-027-009 |
| 3 | `missing_solution` | Solution empty or header-only; `sanitize` strips "Solution to Question N:" to nothing | OPH-002-002, OPH-004-020, OPH-011-033/-034, OPH-018-015, OPH-019-011/-012 |
| 4 | `duplicate_solution` | Two questions ship near-identical solutions. Caught by the gate restored in `d0202ad` | OPH-008-002 (**similarity 1.00**), OPH-022-001 (0.95) |
| 5 | `img_placeholder_count_mismatch` | `[IMG]` token count disagrees with owned file count | many, both sides |
| 6 | `figure_page_mismatch` | Figure attached to a question many pages away | OPH-002-002 (p44 vs anchors [24,34]), OPH-019-012 (p439 vs [425,435]) |
| 7 | `missing_declared_figure_question` | Model declared a figure, none attached, and the book prints one | OPH-008-001 (figure on p155) |
| 8 | `answer_suspect` / key-letter flip | Solution opens on one option's content but `correct_option` is a different letter | OPH-007-006 (D vs B), OPH-008-021 (C vs D), OPH-008-024 (A vs B, C vs B), OPH-021-014 (A vs D), OPH-023-016 (C vs D) |
| 9 | `solution_too_short` | Solution under `MIN_SOLUTION_CHARS` | OPH-007-010 (5 chars: `'[IMG]'`) |
| 10 | C1 split at printed header boundary | A solution contained another question's header, so it was split | OPH-002-002, OPH-018-014, OPH-019-001/-011/-012, OPH-022-019 |
| 11 | `verify exceeded attempts after one widened-crop retry` | The model could not produce usable text even after a widened crop | OPH-004-019, OPH-011-033 |

## REVIEW causes

| # | Reason | Cause | Instances seen |
|---|---|---|---|
| 1 | `img_placeholder_count_mismatch (solution): 1 [IMG] token, 0 files` | Solution text references a figure that nobody owns. **The dominant REVIEW cause.** Either the figure is owned by the question side and the cross-reference allowance did not cover it, or the model invented the marker | OPH-002-001/-006/-014/-020, OPH-005-015/-029, OPH-007-010/-019/-024, OPH-004-003/-005/-013, OPH-008-009/-026, OPH-016-018, OPH-022-003/-018 |
| 2 | `contaminated_question` | `question_text` is not a stem — 80-94% of its tokens also appear in the same row's solution. Cross-field contamination | **OPH-001-007 (89%), OPH-001-017 (80%)**, OPH-002-029 (94%) |
| 3 | `image_unresolved` | A figure the book prints was not claimed by any owner after every ownership level | OPH-002 p23, OPH-005 p101, OPH-006 p117 |
| 4 | `ocr_noise_solution` | Solution contains page-level OCR noise lines | OPH-007-001 (`['141']`) |
| 5 | `image_owner_gate_miss` | Export-gate wrong-owner suspect surfaced into review | OPH-002-002 |
| 6 | `qa_incomplete` / `missing_answer` / `missing_solution` / `bad_options` | The BLOCKER causes re-surfaced as review rows | one row per affected question |

## What the counts mean

The 122 BLOCKER / 350 REVIEW totals are **not 472 independent problems**. They
collapse into the causes above, and one cause dominates:

- **~110 of the 122 BLOCKERs are cause 1** — the answer-key table. Fixing it
  removes roughly 90% of the blockers in one change.
- **Most of the 350 REVIEW rows are cause 1 re-surfaced** (each missing answer
  also creates a review row) plus REVIEW cause 1 (the `[IMG]` token mismatch).
- The genuinely distinct review problems are causes 2, 3 and 4:
  **contaminated questions (3 seen), unresolved figures (3 seen), OCR noise
  (1 seen).** That is a small number, and each is a different defect.

## Priority

1. **Answer-key table** — one fix removes ~110 blockers and most of the review
   rows. Needs one run with `bbe4221` to identify which of the three
   sub-causes it is.
2. **`contaminated_question`** — question_text holding solution text. Three
   instances including two in OPH-001. This is a real extraction defect, not a
   false positive.
3. **Option-text fabrication** — a fallback invents option text from the
   solution. Fabrication should probably not exist at all.
4. **`image_unresolved`** — three figures the book prints were not owned.
5. **`[IMG]` token mismatch** — needs a decision on whether a solution
   referencing the question's figure should be explained automatically
   (RUN-44 does this when the question owns a figure; these cases are where it
   does not).
