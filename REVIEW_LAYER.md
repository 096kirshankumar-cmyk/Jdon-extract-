# REVIEW_LAYER.md — human review + edit + final export (post-extraction)

## Flow (locked order)
```
1. Extraction finishes (Gemini done)   → content FROZEN (pipeline nevers edits it again)
2. /review (dashboard)                 → human edits + approve/ignore, saved instantly to /data
3. Queue fully resolved                → /download-final unlocks
4. final_export.zip                    → converter project reads it (see FORMAT.md)
```

## Rules this layer guarantees (each backed by a test)
- Queue = UNION of every flag file (`export_gate`, `integrity_flags`, `orphans`,
  `still_incomplete_after_retry`, `stem_conflicts`, `unmatched_images`, per-row
  `qa_status`, split `unresolved_qids`, completeness catch-all) — never from
  digest text alone. A flag in ANY file appears on screen.
- Watchdog: an unknown `data/*.jsonl` locks the final zip with a loud warning
  (a future unwired flag file can never be silently ignored).
- Catch-all: completeness counts vs queue rows per chapter — a count gap
  becomes its own BLOCKER row.
- Persistence: decisions (`review_decisions.jsonl`) and edits
  (`human_edit_ledger.jsonl`) are append-only on the volume. Refresh/redeploy
  loses nothing. If row CONTENT changes after a decision, the decision goes
  STALE and the flag re-opens (fingerprinted).
- Edits: stem/options/answer(A-D dropdown enforced)/solution/tables across ALL
  copies (master + by_chapter + subjects bundle ×2 + correct split file) with
  read-back verification — "saved" is only shown when disk matches submission.
  Broken markdown tables (uneven columns) are refused before any write.
- Images: only owner mapping changes (move/detach/attach-from-unclaimed pool).
  Shared multi-draw files keep other owners intact. Cross-chapter moves
  refused. Nothing is ever file-deleted here — detach only unlinks.
- Final zip is HARD-locked until 0 pending BLOCKER + 0 pending REVIEW +
  0 watchdog warnings. Receipt inside (`REVIEW_RECEIPT.json`).
- Pipeline-level protection: while a run/fix is processing, /review is
  read-only (409 on edits) — no mid-extraction content surgery.

## Files
- `review_queue.py` — queue + decisions + edits + image ops + zip gate/builder
- routes in `app.py`: `/review`, `/review/edit`, `/review/image`,
  `/review/decision`, `/review/image-file`, `/download-final`
- `FORMAT.md` — the converter contract carried inside the final zip
- tests: `test_review_queue.py` (layer), `test_review_routes.py` (routes)
