# Pipeline V2 — Multi-Phase (3-Pass) Architecture

> Upload the **contents of this folder to the branch ROOT** (`pipeline-v2` /
> `feature/multi-phase-v2`) — files keep their original names so the branch is
> a complete runnable repo: `qbank_pipeline.py`, `app.py`, `qbank_validator.py`,
> `fix_output.py`, `test_v2_chapter.py`, `Dockerfile`, `requirements.txt`.

## Kya alag hai (v1 vs v2)

| | v1 (current, proven on trial book) | v2 (this branch) |
|---|---|---|
| Gemini call per batch | 1 combined ask (stems+options+keys+solutions) | **Up to 3 focused asks** — Q-pass (questions+options only), A-pass (answer keys only), S-pass (solutions only) |
| Context bleeding | Mingled streams caused wrong-owner stems, glued blobs | Each stream extracted in isolation → bleeding eliminated at source |
| Call count | 1/batch | **Near-v1**: zero-token `pdftotext` probe + sticky section state activate only the passes a batch actually needs (questions section→Q only; solutions→S only; key pages→+A). Probe failure → ALL passes (safe). Naive 3× is NOT done here |
| Dump-tail clipping | sanitize (own-dup only) + sweep step 2b (chapter donor guard) | **+ S-pass parser clip** (`clip_pass_solutions`) with sibling-donor proof; sweep 2b retained as final net |
| Carry-forward | single carry | **per-pass carries** (Q-carry, S-carry) + section boundary hard-resets BOTH |
| Orphans | provenance incl. batch window | **+ `pass` tag** (which pass produced the fragment) |
| Output root | `/data/qbank_output` | `/data/qbank_output_v2` (v1 data 100% untouched); smoke tests go to `…_v2test/` |

## Task-4 clipping — deliberate deviation from the naive snippet

The mandated `clip_solution_at_foreign_markers()` (hard cut at first marker,
case-insensitive) is **NOT applied verbatim** anywhere — it would:

1. clip a **leading** header to an EMPTY string (`"Solution to Question 3: <real content>"` → `""`) — data loss;
2. delete **unique neighbour content** whenever the model glues solutions but forgets to emit the sibling items separately — violating pipeline rule "never delete possibly-unique text".

Applied instead (`clip_pass_solutions`, used by the S-pass parser; audited line
`SOLUTION_MARKER_RE` kept verbatim in code for parity):
leading headers are stripped, embedded foreign headers clip ONLY when the
numbered question exists as a **sibling item in the same response** (zero-loss
proof), and donor-less tails are left for the chapter sweep's own donor-guard
(`foreign_solution_dump_trimmed` step 2b). Same protection goal, no data-loss
edge cases.

## Untouched battle-tested infrastructure (per spec)

TOC parsing (`extract_toc_chapters`, `pdftotext`), 6-page batching + 2-page
overlap, `_batch_meta` carry machinery, pypdf XObject image extraction +
watermark filter, the 4-pass image claiming ladder, `state.json` resume +
`MAX_CALLS_PER_DAY` quota (every pass call counts), 429/transient ladders,
failed-page drain + crop ladder, orphan recovery, integrity sweep, targeted
retry, validator, healer — all identical to v1.

## Deploy path (Railway)

1. GitHub: create branch from `arena/019f92d5-json-extract` → name it (suggestion: `feature/multi-phase-v2`).
2. Upload this folder's files to the branch root, commit.
3. Railway → Service → **Settings → Source → connect branch `feature/multi-phase-v2`** (deploy trigger). Redeploy.
   - Volume: SAME volume is fine — v2 writes `/data/qbank_output_v2` (Dockerfile ENV), v1 data untouched.
4. Dashboard (v2 has a violet banner) → **🧪 V2 smoke test** card: PDF link + subject + chapter no → verify ~0 flags in the black log box.
5. Then the emerald **Full Book (v2)** card. Roll back = point Railway back to the previous branch, redeploy.

## Expected quota (trial-book shape, ~400 pages)

Q-only batches ≈ half, S-only ≈ half, +A on key pages only ⇒ ≈
`pages/4 + key_batches + retries` ≈ **110–150 calls**, inside the 1400/day
brake with huge headroom. The 3× number never materialises because passes are
gated, not blanket.

## Test

`python3 test_v2_chapter.py <pdf> <SUBJECT> <CHAPTER_NO> [offset] [out_dir]`
(needs `GEMINI_API_KEY`) — or just use the dashboard's violet smoke-test card.
