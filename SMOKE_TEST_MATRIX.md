# Required pre-full-book smoke-test matrix

A chapter-1 pass is necessary but not sufficient. Run each test with a fresh
`_v2test` output and save its test ZIP + Railway log before starting a full run.

| Coverage | Recommended chapter | Required checks |
|---|---:|---|
| Dense drug/numeric tables | 4, 13, or 14 | no partial/duplicate tables; numeric columns and units retained |
| Multiple figures / diagrams | 6 or 9 | every PDF figure is assigned once, assets exist, no over-attribution |
| Clinical safety-sensitive material | 27 or 33 | inspect `[SAFETY_BLOCKED]` summary; manually audit each listed page even if recovery succeeds |
| Long overlap-heavy chapter | 11 or 21 | numbering coverage, duplicate stems/tables, carry and retry ledgers |

## Release gate

Do **not** start the full book until all selected smoke tests have:

1. no `still_incomplete_after_retry.jsonl` entries;
2. no unreviewed `[SAFETY_BLOCKED]` pages;
3. no high-severity validator flags;
4. complete image asset references; and
5. a saved ZIP/log that can be compared to the source pages.

## Log ordering note

`process_pdf` is sequential: each Q/A/S response is parsed and merged before
that pass returns, and chapter summary/bundle writing occur only after the batch
loop exits. Railway may display stdout records out of arrival order when it
combines Gunicorn worker output with application output. Treat pipeline event
content, not Railway wall-clock ordering alone, as the source of truth; if a
future source-level asynchronous executor is introduced, it must add an
explicit join/barrier before chapter summary emission.
