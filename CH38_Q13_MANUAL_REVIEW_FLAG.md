# ch. 38 q13 — manual review flag (run-22)

**Decision: flag, do not fix.** q13 ships exactly as extracted, carrying a
review marker. Nothing is rejected, nothing is rewritten.

## The defect

`ANA-038-013` options, as extracted (wrong in **every** run, including
pre-D1/D2/D3 — this is not a regression):

| opt | shipped text | where it really comes from |
|---|---|---|
| A | "It is the radial groove where radial nerve runs along with profunda brachii artery." | p689 `Option A:` commentary — explains **q12** |
| B | "It is the lateral epicondyle. Fracture of this part may lead to injury of the radial nerve…" | p689 `Option B:` commentary — explains **q12** |
| C | "Palmar cutaneous branch of ulnar nerve" | correct (p670) |
| D | "It is the neck of humerus where axillary nerve runs around." | p689 `Option D:` commentary — explains **q12** |

Ground truth printed on **p670**:

```
a) Deep branch of ulnar nerve
b) Ulnar nerve before it's division into superficial and deep branch
c) Palmar cutaneous branch of ulnar nerve
d) Superficial terminal branch of ulnar nerve
```

Root cause: those `Option A/B/D:` lines sit **above** p689's
`Solution to Question 13` header, so a solution-page harvest bound them to 13.
q12's own options are legitimately the diagram labels `a) A b) B c) C d) D`.

## Why flag instead of auto-correct or reject

- **Auto-correct is unsafe.** The right text is on a different page than the
  one the record was built from; re-asking risks replacing wrong-but-real text
  with a hallucination. We cannot prove a replacement is correct.
- **Reject is destructive.** The record is otherwise complete — stem, answer,
  solution and images are all fine. Deleting it loses good data to fix one field.
- So: ship it verbatim, mark it, let a human resolve it against p670.

## What was added

**`qbank_pipeline.py`**

- `detect_options_harvested_from_solution(qn, rec, chapter_records)` — read-only
  detector, returns a reason string or `None`.
  - *Signal 1*: option is ≥ 25 chars **and** opens `Option X:` or `It is the …`
    — the two shapes MARROW uses for per-option explanations.
  - *Signal 2*: the same text appears verbatim in **another** question's
    solution (self-match never counts — solutions restate their own options).
  - Fires on **two** signal-1 options, or **one** corroborated by signal 2.
    A lone uncorroborated one stays quiet: a genuine option may read
    "It is the only muscle supplied by …".
  - Signal 2 alone could not have caught q13 — the p689 commentary was never
    merged into q12's `solution_text`, so there is nothing to corroborate
    against. That is why the two-option rule exists.
- `chapter_integrity_sweep` step 6 — sets `rec["_options_suspect_reason"]`,
  bumps `stats["options_suspect"]`, writes an `options_suspect` entry to
  `integrity_flags.jsonl`, prints a `[SWEEP]` line. **Never touches the options.**
- Export row gains `options_suspect` (reason or `null`) and
  `manual_review` (`true` when the stem *or* the options are suspect).
- `_export_gate_violations` reports `options_suspect` so the chapter cannot
  read clean while an option set is unverified. Reporting only — export
  is not blocked, exactly like `suspect_stem`.

**`qbank_validator.py`** — `check_row` emits a **HIGH** `options_suspect` flag:
`"… options flagged for MANUAL REVIEW (…) -- record shipped as extracted,
verify against the source page"`.

## Verification

Detector run over the exported records of **4 runs / 111 questions**
(`out_ch38`, `out_ch38_fixed`, `out_ch38_final`, `out_fixed2` = ch. 9):

```
out_ch38_final  31 q  ->  q13 FLAGGED
out_ch38        31 q  ->  q13 FLAGGED
out_ch38_fixed  31 q  ->  q13 FLAGGED
out_fixed2      18 q  ->  (none)
total 111 records, 1 flagged, 0 false positives
```

Full sweep replayed on real ch. 38 data:

| check | result |
|---|---|
| q_nos flagged | `[13]` |
| `stats["options_suspect"]` | `1` |
| option text mutated | **False** |
| `integrity_flags.jsonl` | `options_suspect` entry written |
| validator on flagged row | HIGH `options_suspect` |
| validator on healthy row | `[]` |

**Tests:** new `Run22OptionsManualReviewFlagTests` — 12 tests, all passing.
Covers the verbatim q13 option set, read-only guarantee, diagram-label options
(q12), the one-option-is-not-enough rule, cross-question corroboration,
self-match rejection, `Option X:` prefix shape, export field wiring, gate
wiring with a no-mutation assertion, a real sweep on q13 + q12, and the
validator flag.

**Baseline unchanged: 9 failed / 177 passed / 1 skipped / 16 subtests**
(was 9F/165P — the 12 new tests are the delta; the 9 pre-existing failures are
untouched). Note: `poppler-utils` and `tesseract-ocr` must be installed or 4
extra unrelated tests fail spuriously.
