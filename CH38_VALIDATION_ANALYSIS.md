# Ch. 38 validation run — held-out test of the round-2 image-attribution fixes (2026-08-11)

Run `chapter-38-validation-run-f7e0606c`, exit 0.
Log `/home/user/run/ch38.log`, output `/home/user/run/out_ch38/`.
Code = `433a222` (pushed). Harness unchanged: `test_v2_chapter.py book.pdf ANA 38 0 out_ch38`.

## Why ch. 38 and not a chapter from the review

The user asked for ch. 38. Two caveats worth stating plainly:

- **Ch. 38 was NOT in the original 14-chapter review** (that covered ch. 7–20, and the
  over-attribution cap fired in ch. 9, 11, 12, 14, 19).
- Ch. 38 = **"Brachial Plexus and Nerves", PDF pages 666–702** (37 pages, ~2x ch. 9).
  Boundaries confirmed by OCR: p666 = ch. 38 q1, p703 = ch. 39 "Muscles - Upper Limb" q1.
  `page_offset = 0` still holds.

This turns out to be the *better* test anyway: the fixes were tuned on ch. 9, so ch. 9 is
no longer held-out data. Ch. 38 is unseen and figure-dense (38 image rows vs ch. 9's 16),
so it actually exercises generalization.

## Headline result

| metric | ch. 9 (fixed2) | **ch. 38** |
|---|---|---|
| questions | 18 | **31** |
| missing stem / answer / solution | 0 / 0 / 1* | **0 / 0 / 0** |
| image rows | 16 | **38** |
| unmatched images | 0 | **0** |
| **vision fallback calls** | **0** | **0** |
| export-gate violations | 1 | 5 |
| validator flags | 4 | 9 |

\* ch. 9's missing solution was S-pass nondeterminism, already dismissed.

**Text extraction is again 100% complete** (0 missing stems/answers/solutions), consistent
with the user's original finding that every failure is image- or boundary-related.

## The fixes hold up on unseen data

**Zero vision calls again**, on a chapter with 2.4x the figures:

    grep -c "full_page_vision\|isolated_crop\|fourth pass\|figure map" ch38.log  ->  0

All 38 image rows resolved deterministically: **25 `active-block carry` + 13 `block position`**.

### Independent verification: 38/38 correct

Counts improving does not prove attribution is right, so every one of the 38 `[IMG]` claims
was re-derived from OCR page anchors (`ocr_page_anchors` + `image_positions_on_page`, decoy
full-page XObject `oid 4392` filtered) and compared with what the pipeline wrote:

    TOTAL claims checked: 38 | consistent: 38 | MISMATCH: 0

### The dynamic cap did real work — and was right

Ch. 38 pushed the cap harder than ch. 9 ever did: **`ANA-038-010` took 4 solution images**,
raised twice past the old flat cap of 2.

    [IMG] dynamic cap: ANA-038-010 solution images raised to 3 (positional_carry declared this owner)
    [IMG] dynamic cap: ANA-038-010 solution images raised to 4 (positional_carry declared this owner)

Under the pre-fix code these two figures would have been refused and dumped into
`unmatched_images.jsonl`. Verified against ground truth — q10's solution genuinely spans
pages 685→688:

| page | oid | y | anchors on page | verdict |
|---|---|---|---|---|
| 685 | 3819 / 3818 | 520 / 69 | `sol 10 @336` | 3818 below anchor -> q10; 3819 above -> carry q10 |
| 686 | 3819 | 460 | none | carry -> q10 |
| 687 | 3820 | 486 | none | carry -> q10 |
| 688 | 1943 | 520 | `sol 12 @465` | image **above** the q12 anchor -> still q10 |

p688 is the interesting one: the page *does* carry a `Solution to Question 12` header, but
the figure sits above it (y=520 > 465), so it belongs to the q10 block that started three
pages earlier. The deterministic rule got this right with no vision call.

### Carry seeding fired across a chapter boundary

    [IMG] carry seeded from page 665: active block solution q7 (window starts at page 666)

Page 665 is **ch. 37**. The seed pulled a foreign `solution q7` into ch. 38's opening window.
It caused no damage here (p666's image was claimed by `block position` -> `ANA-038-001`,
verified correct), but the lookback is not chapter-aware. See defect D3.

## NEW DEFECTS FOUND (this is why we ran it)

### D1 — REAL CONTENT LOSS: q13 options+answer dropped to a garbled header

`bad_options 13: options=['A','B','C','D']` and `q13` has `options=None, answer=None`.
The `[CRITIQUE]` pass even said *"question 13 is not present anywhere in the text"* — wrong.

Ground truth, PDF p670, options are plainly there:

    Question 13, ~\                        <-- OCR of the header
    Which of the following is injured in a patient presenting with atrophy of hypothenar
    eminence and numbness in the palmar aspect of the little finger?
    a) Deep branch of ulnar nerve
    b) Ulnar nerve before it's division into superficial and deep branch
    c) Palmar cutaneous branch of ulnar nerve
    d) Superficial terminal branch of ulnar nerve

**Root cause: the header reads `Question 13,` — the colon OCR'd as a comma**, so the
question-header regex missed it and the four options were never bound to q13. The stem and
the 612-char solution both survived (they came from other passes), so this is a *partial*
record, which is worse than a clean miss because it looks complete.

Fix: the header regex must tolerate a garbled terminator — accept `[:;,.]` or even trailing
punctuation noise after `Question\s+\d+`. Cheap, deterministic, no quota cost.
Then `[RETRY]`/`[RESCUE]` would not have burned a call for nothing either
(`[RETRY] round 1: filled 0 field(s)`, `[RESCUE] page 670: q13 -> 0 field(s) filled`).

### D2 — `no q_no` drop is confirmed to be losing real content (user's item 5, still unfixed)

Four `[WARN] Gemini returned an item with no q_no, skipping` events, all four ending as
`orphan_unresolved` export-gate violations. Two are clearly recoverable:

| orphan | pages | pass | `last_qn` | content |
|---|---|---|---|---|
| 0 | 677–681 | Q | – | answer-key table (`\| 5 \| c \|`, `\| 6 \| a \|` …) |
| 1 | 677–681 | Q | – | sympathetic vs parasympathetic ganglia table |
| 2 | 686–690 | S | **13** | ulnar nerve branches — *"Distal to pisiform it gives 2 terminal branches…"* |
| 3 | 686–690 | S | **13** | *"Wartenberg's sign… weakness of palmar interossei"* + high/low ulnar lesion table |

Orphans 2 and 3 are **ulnar-nerve solution content on the same pages as q13**, whose stem is
the ulnar-nerve question, and the batch's `last_qn_in_batch` is literally 13. The nearest-
preceding-`q_no` inference the user asked for would have recovered both. Orphan 0 is an
answer-key table that belongs to the chapter, not to one question — it should route to a
chapter-level asset rather than be dropped.

**4 of the 5 export-gate violations in this chapter are this one unfixed defect.**

### D3 — carry seed lookback crosses chapter boundaries

`CARRY_SEED_LOOKBACK_PAGES=3` looks back unconditionally, so ch. 38's first window seeded
from ch. 37's p665. Harmless this run, but on a chapter that opens with a figure above its
first anchor it would attach a previous chapter's block. The lookback should clamp to the
current chapter's start page.

## NON-defects (checked, do not "fix")

- **`bad_options` on q12 is a FALSE POSITIVE.** The PDF genuinely prints
  `a) A  b) B  c) C  d) D` — they are labels pointing at sites on a diagram, and q12's
  figure is correctly attached. The validator should not flag single-letter options when the
  question has an image.
- **504 timeout self-healed.** `[GEMINI_ERROR] status=504` on the 686–690 S-pass, then
  `[INFO] single-page retry: 5/5 pages recovered`. The isolate-and-retry path works.
- **`[SWEEP]` saved a retry** again: `q7: dangling end but 1 solution image(s) attached`.
- 30/31 questions have real 4-option sets; 31/31 have answers and solutions.

## Verdict

The three shipped fixes (dynamic cap, carry seeding, deterministic-first chain) **generalize
to unseen data** — 38/38 attributions correct, zero vision calls, zero unmatched images, on a
chapter with more than twice the figures of the one they were tuned on. The cap fix is doing
strictly more work here than it ever did on ch. 9.

Ch. 38's higher flag count is **not a regression**: 4 of 5 gate violations are the *known,
not-yet-implemented* no-`q_no` inference (user's item 5), 1 is a false positive, and the one
genuinely new bug (D1, garbled `Question 13,` header) is a text-extraction regex issue
unrelated to image attribution — and it is a cheap deterministic fix.

Recommended next order: **D1** (regex, trivial, recovers real data) -> **item 5 / D2**
(orphan `q_no` inference, clears 4 gate violations) -> **D3** (clamp lookback) ->
validator false-positive guard for lettered options on figure questions.
