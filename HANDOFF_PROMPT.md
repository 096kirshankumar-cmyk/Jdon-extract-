# HANDOFF — QBank extraction pipeline

## Where the code is

- Repo: `https://github.com/096kirshankumar-cmyk/Jdon-extract-`
- Branch: `arena/01a03242-jdon-extract`
- Start from commit: `4af9f02` ("fix: [IMG] placeholder check must skip deterministically parsed text")

Work already done on that branch (do not redo it):

| commit | what |
|---|---|
| `6a07f38` | carry claims now raise `REVIEW_NEEDED`; `qa_status`/`qa_reasons` propagate into `split/` and `final_export.zip`; `image_attribution_summary()` |
| `47a4522` | crop-vs-page decision is logged; 5 dead functions + 2 dead files removed |
| `c4da008` | 21 superseded files removed (run reports, per-book CLI runners, duplicate tools) |
| `b059ad3` | zone span no longer absorbs the next chapter; unread crops are discarded, never shipped |
| `9c99b1b` | OCR fallback for the geometric crop parse; printed-header re-ask reaches past the chapter end |
| `4af9f02` | `[IMG]` placeholder check skips deterministically parsed text |

## Environment setup (do this first)

```bash
apt-get update && apt-get install -y poppler-utils tesseract-ocr tzdata
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest PyMuPDF
```

**poppler is not optional.** `pdftotext_page` (`qbank_pipeline.py:1090`) catches only
`subprocess.TimeoutExpired` — with `pdftotext` missing it raises `FileNotFoundError`
instead of returning `""`, and callers silently treat that as an empty text layer.
Without poppler you will misdiagnose everything as "text layer absent".

Gemini keys go in env (`GEMINI_API_KEYS`, or `GEMINI_API_KEY_1..N`). Never print them.

## Test baseline — know this before you change anything

```
.venv/bin/python -m pytest -q
→ 414 passed, 21 failed
```

All 21 failures are pre-existing and environmental. Do not chase them:

- 16 × `test_boundary_phased.py::EngineCase::*` and
  `test_review_queue.py::TestQueueCacheAndPdfReader::test_pdf_reader_reused_per_book`
  — need the gitignored fixture `/home/user/book2/book.pdf`
- `test_resume_relink_regressions.py::test_real_obg_ch3_ledger_replays`
  — needs `/home/user/audit/run_obg_ch3/data/image_ownership.jsonl`
- `test_pipeline_json.py::UnifiedImageOwnershipTests::test_ocr_geometry_claims_when_text_layer_garbled`
  — needs `tesseract`
- `test_image_ownership_audit_regressions.py::TestSSectionBareListFilter::test_keyword_question_heading_survives_in_s_section`
  — passes in isolation, fails only in the full suite (test pollution)
- `test_pipeline_json.py::SolutionFigureMappingTests::test_under_detected_headers_no_longer_swallow_the_page`
  — **fails in isolation too (`AssertionError: 0 != 3`). This one is a genuine
    un-investigated pre-existing bug**, not environmental. Worth a look if you
    have spare time.

If your run shows more than 21 failures, you broke something.

## Architecture, so you do not rediscover it

There is exactly one extraction engine. `qbank_pipeline.main()`
(`qbank_pipeline.py:4190`) is a shim that calls `boundary_phased.run_all()`.
The old multi-pass `process_pdf` engine is gone.

Flow, in order:

1. **TOC → chapter page ranges** — `boundary_phased.py:3201`,
   `extract_toc_chapters()` + `compute_page_ranges()`.
2. **Zones inside the chapter** — `_resolve_zones` (`boundary_phased.py:2334`).
   Q pages, answer-key pages, S pages. Clamped to `ch_last` by
   `_zone_pages_from_headers` (`boundary_phased.py:746`).
3. **Visual header index** — `header_index.scan_chapter` (`header_index.py:130`),
   render + OCR. This is the authority when it finds Q/S/key bands.
4. **Crops** — `header_index.intervals` (`header_index.py:256`): one crop per
   block, "this header → next furniture header". Cross-page blocks become N
   strips, labelled `INTERVAL part i/N … START/CONTINUES` on the image itself.
5. **Crop → text** — `_extract_from_crops` (`boundary_phased.py:1079`).
   Deterministic parse first (`_geom_item_from_interval`,
   `boundary_phased.py:989`) from the **PDF text layer only**. Gemini gets
   every crop whose text layer is not CLEAN.

   There was an OCR fallback here in run-32 and it was **removed in run-34**.
   On OPH-001 it took `geom_ok` from 0 to 22/23 and cut Gemini calls from 46
   to ~2 — and the recovered text was unusable: tesseract read the page
   furniture as body copy (`"12 Sold by @itachibot"` inside a solution, one
   solution reduced to `". X"`). Do not re-add it without a legibility gate.
6. **Answer key** — whole pages, dual Gemini read.
7. **Images** — geometric ownership, not model-guessed.
8. **Gate + commit** — `_export_gate_violations` (`qbank_pipeline.py`), then
   `build_final_question`, then `split_outputs.write_split_outputs`.

Diagnostic log lines already in place — read these before touching code:

```
[BPH] OPH-001: Q -> CROP path (23 Question crop(s) cut from the visual header index; pages 4-12)
[BPH] OPH-001: Q crops=23 geom_ok=22 (0 Gemini calls) gemini_crops=1 | why: ocr=22 text_garbled_ocr_parse_missed=1
[BPH] OPH-001: USED zones Q 4-11 | A [12] | S 12-22
[IMG] OPH-001: attribution 11 block-position / 6 carry / 0 model / 0 unclaimed | carry share 35% of 17 claimed
```

The `why:` breakdown keys mean:

| key | meaning |
|---|---|
| `text=N` | PDF text layer was CLEAN and `crop_parse` parsed it |
| `ocr=N` | text layer unusable, OCR recovered it |
| `text_clean_parse_missed=N` | text was readable but `crop_parse` could not parse it — a parser coverage gap, OCR will not help |
| `text_<health>_ocr_parse_missed=N` | OCR text also unparseable |
| `text_<health>_no_ocr=N` | tesseract missing or returned nothing |

## The two open problems

Book under test: **OPH**, chapter 1, file pages 3–22, 23 questions.
The last full run's numbers are below.

### Problem 1 — RESOLVED (run-34): the "5 truncated solutions" were a verifier false positive

An earlier run reported:

```
phase_unresolved: solutions phase unresolved: ('exceeded attempts', ...
  q3, q7, q15, q22, q23: 'Solution text is missing the full explanation present on the page.')
```

Reading the source PDF settled it: those explanations really are that short.
q7's entire solution is one line ("The canal of Schlemm appears by the 4th
month after conception.", 63 characters) and q3's is two. The Gemini verifier
invented missing content. Nothing was truncated.

Two permanent guards came out of it:

- `MIN_SOLUTION_CHARS = 20` in `qbank_pipeline.py`. OPH-001 q3 shipped `". X"`
  as its whole explanation and passed, because `missing_solution` only tested
  for an empty string. The book's shortest real explanation is 63 characters,
  so the margin is wide.
- The verifier is still the weak link for short answers. If it blocks chapters
  again on solutions that are genuinely short, the fix belongs in
  `_filter_verify_mismatches`, not in the extraction path.

### Problem 1b — page furniture and OCR noise reached the export (run-34)

OPH-001's `questions.jsonl`, checked against the source PDF:

```
q1 solution  "...leading to\n12 Sold by @itachibot\n\nhypermetropia."
q7 stem      "...appearsby_\n5 Sold by @itachibot"
q9 option D  "Middle cerebral artery 6 Sold by @itachibot PRunebdinn IN"
q9 solution  "...superior hypophyseal artery - ventral...\n©MARROW"
q2 stem      "oe SS «\ni i ity?\nAt what age would a child attain full a «"
q3 stem      "A ascarid presen GR the abnormality as shown below"
```

All shipped as `qa_status: READY` with `[GATE] CLEAN`. Fixed by:

- `strip_page_furniture()` — removes whole lines that are purely the reseller
  stamp, the publisher mark, or a page number that precedes a stamp. Applied in
  `_build_records` so the master row and the split row both get clean text.
  Deliberately conservative: a line carrying real text is never eaten, so
  q9's inline `"Middle cerebral artery 6 Sold by @itachibot"` survives intact.
- `_ocr_noise_note()` — flags (never rewrites) fields whose non-ASCII share or
  alphabetic share says "OCR damage, not prose". Thresholds are per FIELD, so a
  legitimate `"2.4 cm"` option is not called empty.
- The garbled watermark variants (`"Cmlistklianm Fm Pir tr rebkiana"`) are
  **not** pattern-matched — they have no stable spelling and guessing would
  risk eating real text. They surface through the noise flag instead.

### Problem 2 — 35% of figures are owned by cross-page carry

```
[IMG] OPH-001: attribution 11 block-position / 6 carry / 0 model / 0 unclaimed
               | carry share 35% of 17 claimed
```

Carry means "no heading was found above the figure on its own page, so it was
assigned to the block still open from the previous page". It is deterministic
but weaker than a same-page geometric match, and it is the class that
mis-attributes.

The 6 carry claims in that run:

| page | image | owner |
|---|---|---|
| 8  | `OPH-p8-18.webp`    | q12 question |
| 16 | `OPH-p16-1904.webp` | q10 solution |
| 17 | `OPH-p17-42.webp`   | q11 solution |
| 18 | `OPH-p18-1906.webp` | q13 solution |
| 20 | `OPH-p20-53.webp`   | q17 solution |
| 21 | `OPH-p21-56.webp`   | q18 solution |

Task: render those pages and check by eye whether each figure really belongs to
that owner. If they are correct, 35% carry is fine and should stop being
flagged. If they are wrong, the fix is heading-anchor recall in
`header_index.scan_chapter` — **not** reordering the claim priority. Block
position is already the primary path in `claim_block_images`
(`qbank_pipeline.py:2377`); carry only fires when it finds no heading above.
Promoting block position further cannot help, because a *missed* heading
produces a wrong **block position** claim at `confidence=high`, not a carry.

Note: object ids `1904` and `1906` are unusually high compared with the others
(7, 12, 15, 42, 47, 50, 53, 56, 57). Worth checking whether those two are real
figures or repeated form-XObject / watermark content.

## Rules for this codebase

- Never auto-correct content. Flag it (`_review_reasons`, `qa_status`) and let
  the human decide. This is a signed-off product rule, not a style preference.
- A deterministic claim must never be overridden by a model verdict.
- Do not weaken the export gate to make a chapter look clean. `REVIEW_NEEDED`
  and `[GATE] CLEAN` are deliberately independent systems: the gate is
  structural, `qa_status` is per-row. They coexist by design.
- Ship nothing you cannot prove. An item that fails validation is DISCARDED and
  left missing (the gate names it by `q_no`) rather than shipped as a plausible
  guess. Keep it that way — the owner runs 18,000+ questions with no capacity
  to review, so silent wrong data is the worst outcome.
- Add a regression test for every fix. The suites that matter are
  `test_geom_ocr_and_reask_regressions.py`, `test_zone_and_retry_regressions.py`,
  `test_review_visibility_regressions.py`, `test_crop_path_logging.py`.

## Definition of done for this task

1. q3/q7/q15/q22/q23 solutions come back complete, and the run prints
   `[GATE] OPH-001: export gate CLEAN`.
2. `pytest -q` still shows exactly 21 failures (the environmental baseline).
3. A short note saying which of cause (a) or (b) it was, with the evidence.
