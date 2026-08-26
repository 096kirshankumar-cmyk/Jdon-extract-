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

**OPH-001 is correct.** Compared line-by-line against the source:

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
