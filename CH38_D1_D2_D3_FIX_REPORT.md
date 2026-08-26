# D1 / D2 / D3 fixes — implementation + re-test on ch. 38 (2026-08-11)

Base commit `2c8d176`. Three runs on the SAME chapter (ANA ch. 38, pp. 666-702):

| run | output | what it tests |
|---|---|---|
| orig | `out_ch38/` | pre-fix code (`433a222`) |
| run2 | `out_ch38_fixed/` | first cut of D1+D2+D3 — **exposed a D2 regression** |
| final | `out_ch38_final/` | D2 narrowed to prose-only |

## Scorecard

| | orig | run2 | **final** |
|---|---|---|---|
| questions | 31 | 31 | 31 |
| missing stem / answer / solution | 0/0/0 | 0/0/0 | **0/0/0** |
| `bad_options` | **1** | 0 | **0** |
| export-gate violations | 5 | 2 | 5 |
| validator flags | 9 | 11 | 9 |
| image rows | 38 | 38 | 38 |
| image attribution vs verified baseline | — | identical | **identical** |
| vision fallback calls | 0 | 0 | **0** |
| ch. 37 cross-boundary seed | 1 | **0** | **0** |

Image attribution was **byte-identical across all three runs** — the D1/D2/D3
work did not disturb the round-2 image fixes.

## D1 — garbled question header (FIXED, verified)

`Question 13:` on p670 OCR'd as `Question 13, ~\`. The terminator class
`[.:\-–)]` had no comma, so no branch matched and q13's options never bound.

The same pattern was **copy-pasted in six places**, so it is now one shared
definition (`QSTEM_HEADING_RE`, `QSTEM_HEADING_MULTILINE_RE`,
`qstem_heading_re_for()`); the widened class adds `, ; · ]`. A test asserts no
call site re-inlines the old literal.

Result: `bad_options` 1 -> **0**, and the wasted `[RETRY]`/`[RESCUE]` calls
that chased q13 in the orig run are gone (`rescue: 0 filled / 0 calls`).

## D3 — carry seed crossed a chapter boundary (FIXED, verified)

The lookback now clamps to `ch["file_start"]`. The
`carry seeded from page 665` line (p665 = ch. 37) is **gone** in both new runs,
while legitimate in-chapter seeds (p677, p681, p685, p689, p694) still fire.

## D2 — orphan owner inference (FIXED, but deliberately narrow)

### The trap that makes the naive version wrong

The obvious implementation — "use `last_qn_in_batch`" — is **wrong**, and ch. 38
proves it. Orphan on pp.686-690 carried `last_qn_in_batch=13`, but its text
("Distal to pisiform it gives 2 terminal branches") continues **q11**'s
solution, which ends "...lateral to pisiform called Guyon's ulnar tunnel".
`last_qn` would have glued q11's anatomy onto q13.

So the fix reads the printed headings on the fragment's own pages and takes the
**last block opened at or before it** — the same OCR geometry the image
attributor uses.

It also needed a new predicate. `looks_truncated_solution()` only fires on an
explicit dangling lead-in (`:`, `--`) because it gates an expensive re-ask;
q11's tail has no such marker. `_solution_block_is_open()` is looser (open =
no sentence-terminal punctuation) and is safe here because **nothing is
re-asked** — the text already exists.

### The regression run2 exposed, and why it is now prose-only

run2's gate looked great (5 -> 2) but it had quietly **corrupted a record**:

    [ORPHAN] Recovered orphan: page=677 assigned_to=ANA-038-007
             reason=page-position inference ... (tables only)

That fragment is the **chapter-wide answer key** (`| 5 | c | 6 | a | 7 | b |
...`, i.e. the answers to q5-q16) plus a sympathetic/parasympathetic
comparison table. It landed on q7's solution purely because q7's heading was
the last one printed before it — q7 went from 0 tables to 2 foreign ones.

A table spanning many q_nos belongs to the CHAPTER, not to the preceding
block. Position inference is now **prose-only**; the "tables only" branch was
removed and a regression test pins it. `final` confirms q7 back to 0 tables.

This is why `final`'s gate (5) is higher than run2's (2): run2 "resolved"
orphans by mis-assigning them. **A lower gate count achieved by corrupting a
record is worse than an honest orphan**, so final is the better run despite
the worse number.

## Tests

`Run22GarbledHeaderAndOrphanInferenceTests` — 12 tests + 16 subtests:
garbled terminators, false-positive prose guard, single-source-of-truth
assertion, D3 clamp, `last_qn` trap, open-vs-retry predicate, complete-solution
guard, foreign-chapter guard, no-pdf_path fallback, **bare-table regression**.

Suite: **9 failed / 165 passed / 1 skipped** — the 9 are the documented
pre-existing baseline (identical on a stashed clean tree), unchanged.

Note: `test_carry_seed_only_runs_when_no_pass_carry_exists` had to be
re-anchored. It does `src.index("carry seeded from page")` on raw source, and
my D3 comment quoted that log line, moving the anchor. It now anchors on the
actual print statement — the test was brittle, the code was fine.

## STILL BROKEN — a separate, older bug (NOT introduced here)

**q13's options are wrong in every run, including the original.** They are not
q13's options at all: A/B/D carry the text of **q12's solution** option
explanations, read off p689:

    Option A: It is the radial groove where radial nerve runs...      <- p689
    Option D: It is the neck of humerus where axillary nerve...       <- p689
    Option B: It is the lateral epicondyle. Fracture of this part...  <- p689

Ground truth (p670) is completely different:

    a) Deep branch of ulnar nerve
    b) Ulnar nerve before it's division into superficial and deep branch
    c) Palmar cutaneous branch of ulnar nerve
    d) Superficial terminal branch of ulnar nerve

On p689 those `Option A/B/D:` lines sit **above** the `Solution to Question 13`
header, so they belong to q12 — whose own options are the diagram labels
`a) A b) B c) C d) D`. The model attached q12's solution commentary to q13 as
options.

D1 fixed the *header detection* (that is why `bad_options` cleared and slot C
filled in), but the content bound to q13 is still from the wrong page. **The
`bad_options` flag clearing is therefore partly a false reassurance** — worth
being explicit about rather than reporting "0 bad options" as a clean win.

Suggested next fix: when the A-pass/Q-pass supplies options for q_no N,
validate them against the stem page for N (`qstem_heading_re_for(N)` now finds
it reliably) and reject option text that was harvested from a *solution* page.

## Remaining orphans in `final` (5 gate violations)

Two answer-key/ganglia tables (pp.677-681) — correctly left alone by the new
prose-only rule; these want a chapter-level asset route, not a question.
One prose fragment (pp.686-690) whose inferred owner q13 already has a complete
solution — correctly refused, now logged with the reason instead of the
misleading "could not determine owner". Two more on pp.690-694.

Genuine remaining work, but none of it is silent corruption.
