# Run-24 — key-rotation batch-loss audit

**Your question:** when a key hits its limit and the pool switches to the next
of the 6 keys, does the current batch get skipped — does it jump to the next
batch and lose that one?

**Short answer: no batch was ever skipped, but you were right to ask.** The
audit found **two real defects** on that exact code path. Neither silently
dropped a batch, but one of them threw away good data and paid for it twice,
and the other could re-trigger the failure it was recovering from. Both fixed.

---

## What I checked

There are three places a 429 can rotate a key:

| path | verdict |
|---|---|
| `gemini_json_call_splitting.attempt()` (targeted retry) | **clean** — rotates, retries the same page set, falls back to halves→singles |
| `retry_batch_page_by_page()` (per-page salvage) | **clean** — rotates then `continue`s on the *same* page, never advances past it |
| **main batch loop** (`process_pdf`, ~`:6520`) | **two bugs** |

I replayed the main loop's control flow line-for-line with the model call
stubbed, then confirmed each finding against the AST.

---

## Bug 1 — a successful rotation had its data thrown away

The `except` block ran like this:

```python
if "429" in t2:
    if handle_429(state, t2):          # rotate to the next key
        raw_items = call_gemini(...)   # SUCCEEDS on the fresh key
    else:
        sys.exit(0)
print("post-backoff call failed differently")   # <-- always runs
raw_items = retry_batch_page_by_page(...)       # <-- overwrites the good data
```

That `print` and the salvage below it were **not** in an `else`. AST confirms
they are top-level statements of the handler, so they executed even when the
rotation had just succeeded.

Replay of "key1 exhausted → rotate to key2 → key2 answers fine":

```
call#1 (first)        -> 429 quota
call#2 (post-backoff) -> 429 quota
ROTATED
call#3 (post-rotation) -> ok          <- good items in hand
!! post-backoff-failed-differently branch entered
page-by-page salvage ran
-> raw_items = ['SALVAGED']           <- the rotated call's items discarded
```

**Impact:** not a lost batch — the salvage re-asks the same pages, so the
content came back. But every rotation burned one extra call per page on the
fresh key (a 10-page window = 10 wasted calls out of the new key's 480), and
the content was re-extracted non-deterministically instead of using the answer
already received. On a 6-key run that's the difference between rotating 6 times
and rotating well before you should.

**Fix:** a successful rotation sets `rotated_ok` and the "failed differently"
branch is now `if not rotated_ok:`.

## Bug 2 — the retry path re-sent pages the pass had deliberately dropped

Every call inside the 429 handler passed **`batch`**, the raw window. The
original call passes **`pass_batch`** — the window *after*
`_batch_after_routing()` removes recitation-sensitive solution pages that
`PREFLIGHT_OCR` already recovered.

So the retry re-asked exactly the pages the pipeline routes away to avoid
`finish_reason=4`. The recovery attempt could fail on a page the pass was never
supposed to send, and the window would then be written off as unresolved.

**Fix:** all three calls in that handler now send `pass_batch`. Verified by
grep — no bare `batch` remains in the retry path.

---

## What is *not* broken

**Pool fully spent mid-chapter.** The run calls `save_state()` then
`sys.exit(0)`. `progress["chapters_done"].append(chapter_id)` only executes
*after* a chapter completes every stage, so an interrupted chapter is **not**
marked done and a resume re-processes it whole. Combined with
`rewrite_questions_file()`'s atomic per-chapter rewrite, resume can neither
duplicate nor skip. No silent gap.

**Per-page retry.** After a rotation it `continue`s on the same page. A page
that fails even alone goes to `state["failed_pages"]` for the chapter-end
drain — queued, not dropped.

**Rotation mechanics.** `_clear_tracked_model_clients()` wipes the cached
`_client` on every tracked model, so a rotated key actually takes effect — a
stale client would otherwise keep using the exhausted key.

---

## Live proof

Configured three keys — two deliberately invalid, the third real — and let the
pool rotate:

```
[KEYPOOL] 3 Gemini key(s) loaded; budget 480/key/day = 1440 calls/day total
attempt 1 on key1: FAILED (400 API_KEY_INVALID)
  [KEYPOOL] switching key1 -> key2 (invalid key); key2 used 0/480
attempt 2 on key2: FAILED (400 API_KEY_INVALID)
  [KEYPOOL] switching key2 -> key3 (invalid key); key3 used 0/480
attempt 3 on key3: SUCCESS -> 'OK'

RESULT: DATA SURVIVED ROTATION
```

The request walked through two dead keys and still returned its data on the
third.

---

## Tests

`Run24KeyRotationBatchLossTests` (5):

- `test_successful_rotation_keeps_its_data` — bug 1 regression
- `test_retry_path_never_sends_routed_away_pages` — bug 2 regression
- `test_batch_is_never_silently_skipped_on_rotation` — your question, as an
  assertion: rotate-and-succeed, rotate-and-still-fail, and non-429 failure all
  produce items
- `test_exhausted_pool_exits_before_marking_chapter_done` — no resume gap
- `test_page_by_page_retry_rotates_and_continues` — retries the same page

Suite: **222 tests, 9 pre-existing failures, 0 new.**

---

## Still open (not a rotation bug, but it limits the 6 keys)

`_pace_gemini_call()` uses a single module-global `_last_call_ts`, so the
5-second spacing is enforced **across all keys**, capping the whole run at
~12 RPM no matter how many keys are loaded. Rotating to a fresh key does not
reset it. The pool multiplies your **daily** quota (6 × 480 = 2880) but not
your **throughput** — a full-book run stays wall-clock bound. Worth making the
pacer per-key if you want the keys to buy speed as well as volume; say the word
and I'll do it.
