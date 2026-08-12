# Run-24 — ranked open items, fixed in order

Ranking published in `OPEN_ITEMS_RANKED.md`. This is what got done, in that
order, and what the evidence says afterwards.

## Two corrections to the ch. 28–30 report

Before the fixes, two claims I previously made turned out to be wrong. Both
were pointing effort at the wrong place.

**"The 503 made the retry come back as a 10-page batch instead of 6."**
Wrong. `build_section_windows` uses `QUESTIONS_CHUNK_PAGES = 10` on purpose —
big windows for the questions section, `SOLUTIONS_CHUNK_PAGES = 5` for
solutions. Ch. 28, which exported clean, used a 10-page window too. There was
no runaway batch and nothing to cap.

**"q16 lost option D because the question straddles a page break."**
The straddle is real (a/b/c on p529, d on p530) but it is not the cause. The
Q-pass window was 521–530, so **both pages were in the same call**; even after
the 503 forced a half-split (521–525 | 526–530) they stayed together. The
model simply returned `{"D": None}` with both pages in front of it.

So the only thing that actually mattered there was the **heal path** — which
run-23 already fixed. Items P7 (cap post-5xx batches) and P8 (page-break
continuation signal) are therefore **closed, not deferred**.

## P1 — chapters 43–63 had never been run  ✅ ran two

Highest-ranked item because 1045 of 1217 pages had never been executed and 20
of those chapters were unreachable until three commits ago. This is where an
unknown-unknown would live.

Picked the two nastiest shapes in the band:

| | ch. 63 — Skin, Connective Tissue (pp. 1205–1217) | ch. 44 — Diaphragm (pp. 806–814) |
|---|---|---|
| why | **last chapter in the book** (`file_end` == final page — the boundary case that never had a test) | smallest chapter in the band (9 pp), so batching has the least context to work with |
| OCR ground truth | 13 questions | 9 questions |
| extracted | **13** | **9** |
| gate | **CLEAN** | 1 × `orphan_unresolved` |
| validator | **0 flags** | 0 flags |
| field defects | **0 / 13** | **0 / 9** |

Ch. 63 is the **first chapter in this whole effort to come back with a clean
gate and a zero-flag validator report at the same time**. The last-chapter
boundary — the thing most likely to run off the end of the page range — held.

Ch. 44's single gate flag is a fragment on pp. 806–809 whose options are
literally `1`, `2`, `3`, `4` (a counting question's option block, re-extracted
by an overlapping window). All four probes were verified present in the
exported questions — it's a duplicate scrap, not lost content.

Both chapters audited field-by-field: 22 records, 4 options each, no blanks,
no missing stems/answers/solutions.

## P2 — `no q_no` drops  ✅ the "loss" was a lying log line

Ranked #2 as the highest-frequency warning (26 across 8 logs). Tracing all six
`merge_question_records` call sites: **every one of them consumes the returned
`skipped` list** and routes it to the orphan buffer or `integrity_flags.jsonl`.
Nothing was ever dropped. The message said `skipping`, which is what sent two
separate log audits chasing a phantom.

Fixed the message rather than the plumbing, since the plumbing was right:

```
[WARN] Gemini item has no q_no -> routed to orphan recovery
       (content: solution_text | EMPTY shell, no content): {...}
```

It now names the destination and says whether the item carries any payload —
so a future audit can tell an empty shell from real content at a glance. The
user's original "≥3 complete solutions lost (ch. 12, 14, 15)" remains
**unreproducible** on current code.

## P3 — positional orphan ownership  ✅ fixed, and it fired in production

The real bug behind the ch. 60 q7→q15 miss: rule 3 hands a `q_no`-less
fragment to `last_qn_in_batch` — the highest question number in the window —
which is a guess about layout, not content. Ch. 60 was only saved by the
"solution already complete" guard refusing the write. That's luck; the same
miss landing on an *incomplete* solution would silently graft one question's
text onto another.

Added `_content_overlap()` + `_positional_owner_contested()`: before a
positional owner is accepted, the fragment's own distinct content tokens are
scored against every record. Three verdicts — `ok` (position and content
agree, or nothing is discriminative), `redirect` (another record clearly wins
*and* can safely take it), `veto` (contested, no safe home).

**It fired on the very first new chapter run**, in ch. 44:

```
[ORPHAN] positional owner q9 is contested: content points to q8 (43% overlap)
         not the window's last question q9 (14%); that record is already
         complete -- deferring to the wrong-owner guards below
```

Same shape as the ch. 60 miss, now caught and logged instead of silently
guessed. The fragment then went to the correct owner q8 by the stem-less rule.

One thing worth flagging: my first version of `veto` **cleared** the owner,
which broke `OrphanForeignGuardTests`. Clearing it skipped the existing
wrong-owner guards that already refuse the append *and* attach a
`blocked_reason` + bump `foreign_fragments_blocked` — so my "safer" version
gave the reviewer strictly *less* information. The veto path now just logs its
evidence and defers to those guards.

## P4 — sweep/validator disagreement  ✅ fixed, verified live

The pipeline sweep excuses a dangling `:` when a solution figure is attached
("lead-in explained by figure, retry skipped"). The validator's exemption
covered `tables` but **not** `images`, so it re-raised the same solution as a
HIGH `truncated_solution` — that's how ch. 60 q3 and q21 got flagged with the
figure sitting right there in `solution.images`.

One rule in both places now: `:` + (table **or** image) = a lead-in.

Verified on ch. 63: **3 sweep suppressions (q1, q7, q12) → 0
`truncated_solution` validator flags.** Before this change that chapter would
have shipped 3 false HIGH flags, and it would not have been the clean run
reported above.

## Deferred, with reasons

- **P5 merged image-resolution calls** — 0 vision calls in the last 5 runs
  (ch. 63 made 1). The waste this targets isn't occurring.
- **P6 wider chapter-boundary stitching** — 0 foreign-chapter drops across
  ch. 28/29/30 (two live boundaries) and now ch. 44/63. Not reproducing.
- **P7, P8** — closed, see the corrections above.

## Tests

`Run24PositionalOwnerTiebreakTests` (7) — includes the ch. 60 q7→q15 miss as a
regression test, plus veto/redirect/agreement/short-fragment/below-floor
cases, and one asserting the `no q_no` path returns items for recovery.
`Run24LeadInFlagParityTests` (4) — figure lead-in exempt, table lead-in still
exempt, bare dangling `:` still flagged, and a genuine mid-flow cut still
flagged even with a figure attached.

Suite: **217 tests, 9 pre-existing failures, 0 new** (baseline was 9/206).
Diffed failure lists before and after to confirm.

## Files

- `qbank_pipeline.py` — `_content_overlap`, `_positional_owner_contested`,
  rule-3 wiring, honest `no q_no` log
- `qbank_validator.py` — figure lead-in exemption
- `test_pipeline_json.py` — 11 new tests
- run artifacts: `/home/user/run/out_ch63`, `/home/user/run/out_ch44`,
  logs `ch63.log`, `ch44.log`
