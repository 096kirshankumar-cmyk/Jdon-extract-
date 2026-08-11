# Json-extract — Full Pipeline Working Process
*(Hand-over document for technical/AI review. Written 2026-07-27, code @ commit `9dbadc6`, branch `arena/019f92d5-json-extract`. Every behavior below is verified from the actual code, not aspirational.)*

---

## 1. What the system does

Converts scanned/printed **medical MCQ textbook PDFs** (question booklets with questions, options, answer-key tables, and printed explanations) into a **single `questions.jsonl`** dataset using **Gemini Vision** (`gemini-3.1-flash-lite-preview`, free tier). One JSON object per question:

```json
{
  "id": "PSY-001-012",                       // SUBJECT-CHAPTER(3d)-Q_NO(3d)
  "subject": "PSY",
  "chapter_id": "PSY-001",
  "question":   { "text": "...", "images": [{"type": "figure", "file": "PSY/PSY-001-012_Q_01.webp"}] },
  "options":    [ {"id": "A", "text": "...", "images": []}, ... ],
  "correct_options": ["B"],
  "solution":   { "text": "...", "images": [...], "tables": [{"type": "...", "markdown": "|...|", "file": null}] },
  "tags": []
}
```

Golden rules the whole pipeline is built around:
- **Never paraphrase/summarize** — verbatim text only (enforced via prompt + post-checks).
- **Never invent a q_no** — fragments with no visible number go to an orphan pool, not guessed.
- **Never fabricate** missing values — leave null and re-ask with a small focused prompt.
- **Never silently discard** anything — every unresolved item lands in a sidecar JSONL for review.
- **Fill-only merges** — later/recovery passes may append/fill, never overwrite existing text (except provably-wrong content stripped by the integrity sweep with a ledger entry).

End goal: feed ~20 books through this, consume the JSONL in a separate exam/quiz app.

---

## 2. Components & deployment

| Component | File | Role |
|---|---|---|
| Extraction engine | `qbank_pipeline.py` (~2,580 lines) | All PDF→JSONL logic; also runnable standalone (`main()`) |
| Offline validator | `qbank_validator.py` (~1,020 lines) | Deterministic rule-based QA over the output; optional LLM-audit mode (flagged rows only) |
| One-shot healer | `fix_output.py` (~315 lines) | Evidence-gated patches P1–P13 for specific proven defects; idempotent |
| Dashboard server | `app.py` (~750 lines, Flask) | Non-technical user's ONLY interface: run/upload, validate, fix, recovery, backup/restore, zip download |
| Container | `Dockerfile` | python:3.11-slim + poppler-utils; `ENV OUTPUT_DIR=/data/qbank_output`, `CMD python3 app.py` |
| Hosting | Railway + **Volume mounted at `/data`** + public domain | State/output survive redeploys/restarts/crashes |
| LLM | google-generativeai SDK | `google-genai` style `model.generate_content(parts)`; **stateless** (continuity injected manually) |

Env vars: `GEMINI_API_KEY`, `OUTPUT_DIR` (default `./qbank_output`; `/data/qbank_output` in Docker), `PORT` (8080), optional `DRIVE_FOLDER_ID`, `DRIVE_API_KEY` (for restore-from-Drive), `GEMINI_MODEL` not env — model is a constant in `qbank_pipeline.py`.

**Hard volume guard**: if `OUTPUT_DIR` lives under `/data` but `/data` is not a real mount, the dashboard shows a red banner and **blocks** run/recover/fix/validate/restore (400) — never burn Gemini quota writing to ephemeral container fs.

---

## 3. End-to-end lifecycle (one book)

```
User taps "Run" → download PDF (Drive link or upload)
  → per chapter: [render pages → batch loop {Gemini call → merge → carry → images}
                  → orphan recovery → image claims passes 2/3/4 → failed-page drain
                  → orphan re-recovery → persist orphans → integrity sweep
                  → targeted retry → build final JSON rows → write questions.jsonl
                  → checkpoint state]
  → all chapters done → (user) 🔍 Validate → (optional) 🩹 Fix → 📦 zip download
Crash/quota-exit at ANY point → re-run RESUMES from state.json (per-chapter + calls_today)
```

---

## 4. Stage details

### 4.1 Input & orchestration (`app.py`)

- `POST /run-url`: accepts Google Drive share links (converted to `uc?export=download&id=...`), direct URLs; handles Drive's virus-scan interstitial (`confirm=` token); validates `%PDF` magic bytes; then starts the pipeline thread. Marks status=processing **before** download to prevent double-tap duplicate runs.
- `POST /run`: direct file upload.
- Both call `run_pipeline_thread(subject, pdf_path, page_offset)` which injects a one-PDF config into `PDFS` (`pipeline.PDFS[:] = [{...}]`) and calls `qbank_pipeline.main()` in a daemon thread. All progress goes to an in-memory log ring buffer shown on the dashboard (`GET /status` JSON polls).
- After `main()` returns: zero-token deterministic validation runs automatically (`run_hybrid(audit=False)`, summary logged) and `make_zip()` rebuilds the downloadable zip. Quota `SystemExit` → status "paused" (zip still rebuilt) so tomorrow's resume taps just Run.
- Other routes: `/recover` (recovery plan JSON), `/fix`, `/validate`, `/data-status` (deep fs scan), `/restore-drive` (pull a previous output folder back from Google Drive: webp→assets, listed data files→data/, state.json), `/restore-zip`, `/download` (zip of `/data` subtree).

### 4.2 PDF preparation (`process_pdf`)

1. **Watermark identification** — `find_watermark_object_id()`: scans sample pages' XObjects; an image object id reused across many pages = watermark → excluded from figure extraction.
2. **TOC parsing** — `extract_toc_chapters()`: reads printed TOC pages (default pages 1–3) via `pdftotext`, regexes chapter number/title/printed-page.
3. **Page ranges** — `compute_page_ranges()`: printed page + `page_offset` (per-book constant, typically −1 or −2 because PDF file pages lead printed pages by front matter) → contiguous `[file_start, file_end]` per chapter.
4. **Rendering** — `pdftoppm -jpeg -r 150` per chapter into `/tmp/{SUBJECT}_ch{NNN}/page-*.jpg`. 150 DPI chosen as readably-cheap; failed-page recovery re-renders targets ±1 neighbour at higher DPI.

### 4.3 Batching & continuity (`SCHEMA_PROMPT`, carry-forward)

- **Batch size** = 6 pages/call (`PAGES_PER_GEMINI_CALL`), **overlap** = 2 pages (`BATCH_OVERLAP_PAGES`) → step 4 new pages/call. Rationale: a question/solution split across a boundary is seen WHOLE (with its printed q_no) in at least one call; q_no-keyed merge makes re-extraction idempotent. Trailing all-overlap window is skipped (saves a call).
- **Stateless continuity**: every batch returns one extra control object `_batch_meta` `{last_q_no, ends_mid_content, cut_part, tail_text}`. `compute_carry()` turns that into a carry payload; `build_carry_context()` injects it as a text prefix into the NEXT call ("CONTEXT FROM PREVIOUS BATCH … use only to continue the referenced item under its original q_no").
- **Stale-carry guard #1** — `enforce_carry_expiry()`: a carry whose split never resolved within the chapter expires (banned from respawning).
- **Stale-carry guard #2** — `detect_section_boundary()`: first batch hitting the printed **Answers/Solutions section HARD-RESETS all carry context** so question-section text can never bleed into solutions and cross-merge.
- The main `SCHEMA_PROMPT` (see §6) additionally covers: one-JSON-entry-**per-row** for answer-key tables (the classic catastrophic failure: model summarizes a 20-row key as prose), null-q_no fragments ("never invent"), overlap handling, and verbatim-only rule.

### 4.4 Gemini call layer & retry ladders

Two ladders protecting two call sites:

**Ladder A — main batch loop (`call_gemini_on_pages` + `retry_batch_page_by_page`)**
1. Non-STOP finish_reason (safety/RECITATION=4/token limit) or exception → retry **each page ALONE** (a single poisoned page must not sink 6 pages' worth of data; proven in prod).
2. Page failing even alone → persisted to `state["failed_pages"]` with provenance (subject/chapter/true_page/reason) for the chapter-end drain.
3. 429/quota burst → 65 s backoff, one retry; still limited → clean save + `sys.exit(0)` (free tier: 15 RPM bursts ≠ daily cap; disambiguated by the backoff).

**Ladder B — focused asks (`gemini_json_call_splitting`)** used by targeted retry, drain, image attribution: whole set → halves → singles. Transient 500/503 → one 20 s-backoff retry; 429 → 65 s then save+exit; deterministic per-page failure costs only that page. *Origin: a 14-page whole-chapter retry failed as a unit in run-4 (recitation on one page wiped the ask).*

**Quota accounting**: `state["calls_today"]` incremented on EVERY call (main, retry ladder, drain, attribution, recovery) — a run-4 blind spot (recovery calls weren't counted) that was fixed. Daily brake `MAX_CALLS_PER_DAY = 1400` (self-imposed, ~7 % under the free 1500/day). On brake: save + exit; next run resumes mid-book. `reset_daily_counter_if_needed()` rolls the counter by server date.

**Safety settings**: `BLOCK_ONLY_HIGH` on all four categories — medical text routinely contains clinical violence/self-harm content; default thresholds false-blocked legitimate pages.

### 4.5 Merge engine (`merge_question_records`, q_no-keyed, fill-only)

- Record per q_no: `{question_text, options{A..D}, correct_option, solution_text, tables[], has_figure_*}`. New items fill nulls; non-null collisions resolved by rules:
  - **Exact/near-duplicate stems** (SequenceMatcher ≥ 0.95 on overlap re-reads) → counted, not duplicated.
  - **Answer conflicts** (same q_no, different correct_option, case-normalized) → keep FIRST, drop item, count conflict.
  - **Stem conflicts** (sim < 0.95): `_stem_payload_coherence()` scores how well each stem variant coheres with the record's own options+solution; winner kept; loser + both texts logged to `stem_conflicts.jsonl`. In fill-only (recovery) mode the existing stem NEVER loses.
  - **Stale carry-merge guard**: a "stem" that looks like solution-style prose (`looks_like_solution_style_stem`) is rejected as stem but its other fields still merge.
  - `_frag_mostly_present()` (0.85 containment) dedupes appended continuation fragments.
- **q_no = null items are NOT merged** — they go to the orphan pool with full provenance (batch window, carry owner, last q_no in batch).

### 4.6 Orphan recovery (`recover_orphans`) — confidence ladder

0. **Answer-key-table orphan** → parse markdown rows deterministically; fill-only `correct_option`s; rows that all match existing answers = consumed as "verified"; **disagreeing rows → `integrity_flags.jsonl`** (free wrong-answer alarm); key referencing only foreign q_nos → kept with `blocked_reason`, explicitly NOT merged (prevents foreign-key glue-onto-local bug).
1. **Self-labelled fragment** ("Solution to Question 3:") → parse owner from text.
2. **Carry-forward owner** captured when the fragment arrived.
3. **Last-q_no heuristic**: highest-numbered question in the same batch window missing exactly the field the fragment provides; partial owners get **append** of leading-new text.
4. Otherwise → persist to `orphans.jsonl` (AFTER the drain's second recovery pass, so healed fragments never get a stale "unresolved" entry).

### 4.7 Image/figure pipeline

- `extract_real_images()`: pypdf XObject walk per page, skip watermark id, save `webp` under `assets/questions/{SUBJECT}/` with temp names `PSY-p{page}-{seq}.webp`.
- `image_positions_on_page()`: 2D affine CTM composition (`_mat_mult`) → y-positions, so images on a page can be ordered against question numbers printed on the same page (`pdftotext`).
- **Solution-block mapping (claim pass 0, `claim_solution_page_images`)**: every figure drawn under a printed `Solution to Question N:` header is assigned to THAT solution by real PDF y-position — the CLOSEST header whose baseline sits above the figure's bottom edge (`solution_headers_on_page` locates headers with pypdf's text visitor, same bottom-left coordinate space as the image positions; no extra subprocess). When a header cannot be located, the figure is left unclaimed instead of guessed. This replaces the old single-owner shortcut, which attached EVERY image of a page to the ONE header the (partially garbled) text layer happened to decode — the "7 figures collapsed into 2 solutions" user report.
- **Claim pass 1** (`claim_page_images_one_to_one`, during batches): leftover figures from pass 0; one-to-one assignment by page-order vs the questions already known; respects per-question caps.
- **Claim pass 2** (chapter end): retry leftovers now that all records exist (plates often print just before their question).
- **Claim pass 3** (0 tokens): if `pdftotext` shows EXACTLY ONE of this chapter's questions printed on the image's page → that question is owner (side picked by "fig/diagram" in stem vs solution presence); rename via `_rename_for_slot` (collision-proof `_Q_01/_Q_02…` suffixes, tiny-file guard, cap guard).
- **Claim pass 4** (Gemini, ONE image/call): `attribute_orphan_image()` returns `{q_no, slot}` or `decorative:true`; decorative → `decorative_images.jsonl`; undecided → stays unmatched; quota brake stops gracefully.
- **Guards everywhere**: `MIN_IMAGE_BYTES = 1500` (<1.5 KB webp ≈ broken crop — ref dropped everywhere, never shipped broken), `MAX_QUESTION_IMAGES = 3` (over-attribution sweep de-references extras to `unmatched` with reason — fired 4× in prod), `MAX_SOLUTION_IMAGES = 2` (solution-side cap at the `_rename_for_slot` choke point + sweep step 4b — a solution block citing >2 figures means under-detected headers stacked neighbours' figures onto it), `IMG_PATH_RE` filename validation, missing-file ref drop at build time.
- Naming convention (locked): `assets/questions/{SUBJECT}/{SUBJECT}-{CH:03d}-{Q:03d}_{Q|SOL|OPT_A..|TABLE}_{NN}.webp`.
- Still-unclaimed → `unmatched_images.jsonl` for human review (current known residual: 3 page-groups in the trial book).

### 4.8 Failed-page drain (`drain_failed_pages`)

Chapter-end second chance for recitation/refusal pages **before** targeted retry:
1. Re-render target ±1 neighbour at **higher DPI**.
2. Re-ask with a re-framed prompt (different phrasing than the first attempt — recitation triggers are prompt-sensitive).
3. If still blocked → **crop ladder**: page → horizontal halves → quarters (12 % overlap so clipped content is whole in ≥1 crop; overlap re-extraction is merge-safe because all consumers are fill-only/deduped).
4. Healed pages are removed from `state["failed_pages"]`; drained null-q_no fragments join the orphan pool and go through `recover_orphans` again.

### 4.9 Integrity sweep (`chapter_integrity_sweep`) — zero-token deterministic proofs

Runs before targeted retry so anything it strips is re-asked **in the same run**:
- **duplicated wrong-owner stems**: two records sharing a stem → coherence resolver strips the provably-foreign one.
- **foreign `Option` heads in solutions** (`_foreign_option_line`): a solution containing another question's "Option X:" rebuttal block is clipped at the foreign marker.
- **truncated solutions** (`looks_truncated_solution`): dangling-colon/mid-flow endings; **suppressed if the row has tables/images** that legitimately continue the text (book layout = "…listed below:" + table). Mid-flow truncation (trailing-space/joiner evidence) stays REAL.
- **over-attributed images** (>3 question-side, or >2 solution-side) → de-reference extras with ledger entry (`question_images_trimmed` / `solution_images_trimmed`).
- Outputs `forced_solution_qns` → targeted retry re-asks those solutions even if the 60 % gate wouldn't.
- Sweep findings → `integrity_flags.jsonl` with `kind`, evidence, `matched` bool.

### 4.10 Targeted retry (`find_incomplete_records` + `build_targeted_retry_prompt` + `targeted_retry`)

- Finds rows missing `correct_option`/`options`/`solution_text` **after** the whole chapter processed.
- **Solution gate** (`SOLUTION_GATE_MIN_SHARE = 0.6`): if ≥60 % of the chapter already has solutions, the book provably prints explanations here → the rest are extraction losses, retry-eligible. Below the gate, solution gaps are respected (answer-key-only chapters exist) — gate added after a run proved nondeterministic model drops on identical pages.
- Retry prompt = narrow, listing ONLY the specific missing fields with their page images (via Ladder B halves→singles), `TARGETED_RETRY_MAX_ROUNDS = 2` rounds.
- Fields filled are merged fill-only; sweep-forced q_nos bypass the gate.
- Remaining gaps → `still_incomplete_after_retry.jsonl` with `chapter_id` (added after the review found entries were chapter-less), consumed by the validator as `answer_key_only_suppressed` where the gate explains them.

### 4.11 Final build & persistence (`build_final_question` + chapter write)

- `sanitize_solution_text()`: strips leaked prompt/self-reference furniture, header junk; emits notes.
- `_is_printed_answer_key()`: root-level strip of printed answer-key markdown that leaked into a solution's `tables` (validator also flags leftovers as `stray_answer_key_table`; future books never carry them).
- `_dedupe_tables()`, table schema normalization, image ref validation (see §4.7).
- Rows appended to `data/questions.jsonl` (flushed per row); `chapters.json` rewritten incrementally per chapter; `state.json` saved after every batch & chapter (`chapters_done`, `calls_today`, `day_stamp`, `failed_pages`).
- **Resume semantics**: chapters in `chapters_done` are skipped entirely; a quota-exit mid-chapter redoes only that chapter under the SAME output file (chapter rows are written only after the chapter fully completes — no partial writes).

### 4.12 Recovery mode (`/recover` → `recover_pages`)

- Takes a JSON plan `{"PSY-016": {"pages": [217], "reason": "..."}}`, folds existing JSONL rows back into records (`final_q_to_record`), re-extracts ONLY those pages (high-DPI render, ±1 neighbour context), merges fill-only, then runs detection-only integrity sweep + targeted retry (including sweep-forced truncated solutions — added in the review; recovery previously could never heal truncations).
- Quota counted & braked like the main loop; stem resolver runs in fill-only mode (kept-old verdict logged to `stem_conflicts.jsonl`).
- Writes a NEW questions.jsonl atomically (backup of previous kept).

### 4.13 Offline validation (`qbank_validator.py`)

`run_hybrid(output_root, audit=False)`:
- **Deterministic layer (0 tokens)** over rows + chapters + sidecars. Flag kinds (severity high/low): `truncated_solution`, `suspect_truncated_table`, `foreign_solution_segment` (shingle-8 overlap ≥400 chars between sibling solutions → suspect = longer row), `option_solution_disagree`, `answer_mismatch`, `duplicate_text` (cross-row stem dup with coherence suspect), `duplicate_id`, `duplicate_table`, `empty_question`, `bad_options`, `missing_answer`, `missing_solution`, `numbering_gap`, `numbering_start`, `source_gap`, `short_bare_solution`, `solution_header_furniture`, `solution_recitation_dump`, `foreign_option_head`, `image_ref_missing`, `suspicious_tiny_image`, `over_attributed_images`, `over_attributed_solution_images`, `image_unclaimed`, `stray_answer_key_table`, `orphan_unresolved`, `answer_key_only_suppressed`, `suspect_density` (per-chapter anomaly density).
- **Audit mode** (opt-in, capped calls): sends only flagged rows (+ context) to Gemini for semantic verdicts (`audit_missing_question`, `audit_ghost_question`, `audit_component_missing`, `verified_clean`, etc.) — human review funnel without full-dataset cost.
- Output: `data/validation_report.json` (generated_at, flags_total, by kind, by chapter) + dashboard log box rendering.

### 4.14 Healing (`fix_output.py`, dashboard 🩹 Fix)

- `patch_all(questions, ...) -> (rows, actions, archive)`: patches **P1–P13**, each **evidence-gated** (a patch only fires when its specific defect signature is present — safe to run on any dataset; skip ≠ failure), **idempotent**.
- Examples: P4/P10 strip stray printed Answer-Key tables/dupes from solutions; P11 completes a truncated Erikson-stages table **verbatim** from the sibling question that prints the same table; P12 trims a proven foreign solution tail; P13 relabels a mis-addressed "Option C:" line → "Option D:".
- Guardrails: timestamped `.bak-YYYYMMDD-HHMMSS` backup, `fix_output_archive.jsonl` ledger of every action, auto re-validate + zip rebuild after patching.

---

## 5. Data artifacts inventory (Volume `/data/qbank_output/`)

| Path | Writer | Content |
|---|---|---|
| `state.json` | pipeline | `calls_today`, `day_stamp`, `pdf_progress.{SUBJECT}.chapters_done/current`, `failed_pages[]` |
| `data/questions.jsonl` | pipeline | final dataset, one row/question |
| `data/chapters.json` | pipeline | chapter registry (incremental) |
| `data/orphans.jsonl` | pipeline | unresolved fragments with provenance (`batch_start`, `pdf_pages`, `carry_q_no`, `blocked_reason`) |
| `data/unmatched_images.jsonl` | pipeline | extracted figures no question claimed |
| `data/decorative_images.jsonl` | pipeline | model-confirmed decorative figures |
| `data/integrity_flags.jsonl` | pipeline+sweep | answer-key disagreements, sweep strip evidence, truncated retries |
| `data/stem_conflicts.jsonl` | merge engine | both stem variants + verdict when coherence couldn't decide |
| `data/still_incomplete_after_retry.jsonl` | targeted retry | gaps left after retry rounds, with `chapter_id` |
| `data/fix_output_archive.jsonl` | healer | every patch action |
| `data/validation_report.json` | validator | latest report |
| `assets/questions/{SUBJECT}/*.webp` | pipeline | all figures (single folder convention — no solutions/options/tables splits) |
| `data/*.bak-*` | healer | pre-patch backups |

---

## 6. Prompts (what Gemini is actually asked)

1. **Main extraction (`SCHEMA_PROMPT`)** — strict JSON array per question; VERBATIM; answer-key tables = one entry/row (worked 3-row example embedded); solutions-only pages → solution-only items; split options → only what's visible; markdown for any table; null-q_no fragments allowed ("never invent a number"); context-block semantics (continue-only, never re-emit); mandatory `_batch_meta` trailer `{last_q_no, ends_mid_content, cut_part, tail_text}`; bare-JSON output.
2. **Targeted retry** — "here are rows with specific missing fields; fill ONLY these" with the chapter's page images; small/narrow beats big/general (per-call error-rate finding).
3. **Failed-page drain** — re-framed extraction ask (different wording to dodge recitation triggers) + neighbour context.
4. **Image attribution** — single image + chapter's question list → `{q_no, slot}` / `decorative:true` / abstain.

---

## 7. Config constants (current, tuned on the PSY trial book)

| Const | Value | Why |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.1-flash-lite-preview` | free-tier, confirmed working |
| `PAGES_PER_GEMINI_CALL` | 6 | accuracy/cost balance |
| `BATCH_OVERLAP_PAGES` | 2 | fallback fixed-window overlap (used only when the text layer can't be read) |
| `QUESTIONS_CHUNK_PAGES` | 10 | whole questions section in 1-2 calls (all questions share one context) |
| `SOLUTIONS_CHUNK_PAGES` | 5 | recitation-safe solution chunks (long verbatim spans = finish_reason=4) |
| `SECTION_OVERLAP_PAGES` | 1 | intra-section overlap only; overlap waste 33% -> 10% |
| `TARGETED_RETRY_MAX_ROUNDS` | 2 | diminishing returns after |
| `SOLUTION_GATE_MIN_SHARE` | 0.6 | distinguishes "prints solutions" vs answer-only chapters |
| `MAX_CALLS_PER_DAY` | 1400 | 7 % buffer under 1500/day free tier |
| `MAX_QUESTION_IMAGES` | 3 | >3 question-side figures ≈ mis-attribution |
| `MAX_SOLUTION_IMAGES` | 2 | >2 solution-side figures ≈ under-detected headers stacking neighbours' figures |
| `MIN_IMAGE_BYTES` | 1500 | <1.5 KB webp ≈ broken crop |
| Render DPI | 150 (recovery: higher) | readability/cost |
| Backoffs | 65 s (429), 20 s (transient 5xx) | RPM-burst vs daily-cap disambiguation |
| Stem dedup sim | 0.95 SequenceMatcher | overlap re-read tolerance |
| Fragment containment | 0.85 | append dedupe |
| Foreign-segment detector | shingle-8, ≥400 chars | copy-paste tails between sibling solutions |
| Safety | BLOCK_ONLY_HIGH ×4 | clinical content must pass |

---

## 8. Failure & quota semantics (state machine)

- **429 (burst)** → 65 s → retry once → still 429 ⇒ daily cap ⇒ save + `sys.exit(0)`. Next container start resumes.
- **Transient 5xx / high-demand** → 20 s → retry once → fail ⇒ split halves → singles.
- **finish_reason ≠ STOP (RECITATION=4 etc.)** → page-by-page isolation → per-page persist to `failed_pages` → chapter-end drain (re-frame + crops) → recovery plan remains as last resort.
- **Any unexpected exception per batch** → page-by-page isolation (never lose 6 pages to 1 fault).
- **Daily brake / quota exit** → `state.json` + `chapters.json` + flushed `questions.jsonl` are always consistent; resume is idempotent per chapter.

---

## 9. Proven production behaviour (PSY trial book — 33 chapters, 434 questions)

- Full book extracted in 1 run + 1 same-day resume across a mid-run API-key swap; each chapter closed "0 missing answer, 0 missing solution".
- Guards observed firing in real logs: over-attribution (4×), stem-conflict kept-old (1), answer-key consume-verified (7 rows), foreign-key block, sweep forced-solution re-asks, 65 s 429-burst backoffs, RECITATION page quarantined instead of sinking batches.
- Post-run deterministic validation: 52 flags, all triaged — majority were **false positives later fixed in the validator** (dangling-colon + table rows), 39 cosmetic stray answer-key tables (root-fix shipped so future books never carry them), 3 real content issues healed by healer patches, 3 image-groups left for human screenshots.
- **Known residual**: page 217 (PSY-016) recitation-sensitive — reprompt+crops did not clear it; chapter is nonetheless complete (17/17 with solutions from neighbours/overlap), page sits in `failed_pages` for a future recovery attempt.

---

## 10. Known limitations / open design questions (for reviewers)

1. **RECITATION on dense verbatim textbook pages** — the crop ladder mitigates but doesn't eliminate; would Gemini 2.5/3.x non-lite, a "transcribe as data, document ID" framing, or a two-model fallback help?
2. **Chapter discovery depends on printed TOC** (pages 1–3) + a manual `page_offset` per book. Fragile for books without clean TOCs — worth a robust page-label/printed-number detector?
3. **Answer-key formats vary by publisher** — `ANSWER_KEY_ROW_RE` + prompt example handle the trial publisher; generalization strategy for the next ~19 books?
4. **Concurrency = 1 call at a time** (stateless sequential batches). RPM headroom exists (15 RPM free); a small parallel pool with per-call retry could cut wall-clock 3–4×, at the cost of reordering-safe merge (q_no-keyed, so mostly safe) — worth it?
5. **Detection-vs-repair asymmetry** — validator flags exceed healer patches (P1–P13 are defect-specific). A generic "flag→auto-re-ask" loop (v2 recovery driven by validator output) is an obvious next iteration.
6. **No offline regression dataset** — heuristics were tuned/verified on one book's outputs; a frozen mini-corpus (3 chapters ×3 books) with expected JSON would de-risk future prompt edits.
7. **Single-volume persistence** — restore-from-Drive exists but there's no automatic post-run Drive upload/backups.
8. **Image side-assignment heuristic** in claim pass 3 (`fig/diagram` keywords in stem vs solution presence) is coarse — better visual/layout priors possible?
9. **Token/context budget per call** is unmanaged — 6 page images + long context string per call; trimming carry context (tail_text 25 words) helps; measure actual utilization?
10. **Validator severity model is flat (high/low)** — a confidence×impact scoring would prioritize human review better at 20-book scale.

---

### 4.16 Changelog — 2026-07-27 (external-audit round 2, 26 tests green)

Triggered by an independent zip audit of a duplicate test-run dataset (same
trial book under two subject codes = 868 rows; 47 flags):

1. **Foreign dump-tail trim (sweep step 2b)** — `chapter_integrity_sweep` now
   trims an embedded `Solution to Question N:` tail when the numbered sibling
   record in the same chapter already owns a non-empty solution (redundancy
   proven). Donor-less tails kept + review-flagged. Origin: sanitize only
   trimmed tails duplicating the record's OWN text; neighbours' unique text
   was conservatively kept → the chapter's first solutions record could ship
   a whole-page blob (was 27/47 flags in the audit).
2. **still_incomplete ledger rewrite** — `_prune_still_incomplete(chapter_id)`
   runs before every chapter's ledger write; healed rows can no longer leave
   stale "missing" entries (was 12 stale entries in the audit).
3. **Zip hygiene** — `make_zip` excludes healer `.bak-*` snapshots and the
   `_archive/` tree.
4. **Healer generalized** — P12/P13 are now content-signature gated for ANY
   subject (not PSY-locked); NEW **P14** (generic embedded dump-tail trim with
   donor guard, any subject) and **P15** (generic same-header sibling-table
   completion, Erikson-class).
5. **Dashboard 🧹 /reset** — Danger-zone button (requires typing RESET):
   archives `data/`, `assets/`, `state.json` into `_archive/<ts>/` INSIDE the
   volume (nothing deleted), giving each new book a clean slate. Reset is
   400-blocked without a mounted Volume or while a run is processing.

---

### 4.17 Changelog — 2026-08-02 (solutions-page figure mapping fix)

Triggered by the user report: a solutions page holding 7 figures came back
with all of them mapped into just 2 solutions.

1. **Position-based solution-block mapping** — the header-binding added
   earlier treated a page's ONE decoded `Solution to Question N:` header as
   the page's owner and attached EVERY figure on the page to it (the text
   layer of scanned books decodes headers sporadically, so a 7-block page
   often surfaced a single header). New `claim_solution_page_images` assigns
   each figure to the CLOSEST header drawn above it using real PDF
   y-positions (`solution_headers_on_page` via pypdf's text visitor — same
   bottom-left coordinate space as `image_positions_on_page`, no extra
   subprocess, no coordinate conversion). Figures with no locatable header
   above them are left unclaimed (model/manual pass) instead of guessed.
   Applied in `process_pdf` and in `--recover` mode.
2. **`MAX_SOLUTION_IMAGES = 2`** — solution-side over-attribution cap at the
   `_rename_for_slot` choke point (mirrors `MAX_QUESTION_IMAGES = 3`): when
   headers are under-detected, only the first two figures under a header are
   auto-claimed; the rest flow to the model/manual passes instead of stacking
   a whole page on one solution. Integrity sweep step 4b heals rows from runs
   before the cap; validator gains `over_attributed_solution_images`.

### 4.18 Changelog — 2026-08-02 (large-scale hardening, run-5 evidence)

Triggered by the first full-book production run (33 chapters, 46 validator
flags) -- fixing the defect classes that would multiply across 20 books:

1. **Solution-gate bypass via printed headers** — `find_incomplete_records`
   now accepts `printed_solution_qns`: a q_no whose "Solution to Question N:"
   header exists in the chapter's text layer (`chapter_printed_solution_qns`,
   zero-token, one pdftotext pass) is retry-eligible even when the chapter
   sits below the 60% gate. Run-5 proof: ch25 at 7/12 (58%) had 5 REAL
   solutions suppressed by the gate.
2. **Page-focused rescue pass** — `rescue_incomplete_records` runs AFTER
   targeted retry: records still missing answer/options/question/solution
   get ONE focused call PER PAGE where their q_no is actually printed
   (`locate_missing_record_pages` — question stem and/or solution header,
   text layer). Targeted retry re-sends the whole chapter and stalls
   ("filled 0 field(s)"); the rescue's small-page ask is what the 9
   persistent gaps (ch2 q25/26, ch18 q13, ch19 q11/12, ch24 q12/13, ch27
   q11, ch33 q9) needed. Merges fill-only, respects quota, rewrites the
   still-incomplete ledger to post-rescue truth.
3. **Anchorless-record drop** — rows with NO stem/options/solution after
   batch + retry + rescue are phantom answer-key rows from a table spanning
   chapters (ch24 q12/13 class). Dropped from output with a ledger entry
   (`data/dropped_anchorless.jsonl`); never silently lost.
4. **Malformed-JSON quota guard** — a batch whose Gemini response fails to
   parse now gets ONE same-batch re-ask (1 call) before descending into
   page-by-page salvage (was 6 calls every time; run-5 hit this twice).
5. **`--auto-recover`** — one command heals a whole book: builds a
   recover_pages plan from the ledgers (still-incomplete records with their
   pages located in the text layer, unresolved orphans' pages, unmatched
   images' pages) and runs it. No more hand-writing plan.json at scale.
6. **questions.jsonl dedupe** — `_dedupe_questions_by_id` runs at the end of
   `main()`: surgical re-runs append duplicate rows; the newest row per id
   wins (idempotent across 20 books).
7. **Summary line** now reports `rescue: N filled / M calls` and
   `anchorless dropped: N` per chapter.

---

### 4.19 Changelog — 2026-08-02 (RPM + token-efficiency, run-6 user ask)

User report: the free tier's **15 requests/minute** was being exceeded, and
tokens were being wasted (the daily call budget died with context budget
left over). Two root causes + the section-aware batching they asked for:

1. **RPM burst root cause (PROVEN from the run log)** — `attribute_orphan_image`
   (the 4th-pass image attributor) called `generate_content` DIRECTLY,
   bypassing the 5s pacing every other path enforces. The log shows three
   attribution calls in the SAME microsecond (14:08:48.0293 ×3) and several
   ~1.1s apart (14:14:36-40) — exactly the burst that trips the 15 RPM
   window. Now paced like every other call.
2. **Section-aware batching (the user's idea: "pehle questions ek saath,
   phir answer table, phir solutions")** — `build_section_windows` reads the
   chapter's text layer ONCE, finds the Solutions-section start (≥2
   "Solution to Question N:" headers), and sends the chapter in
   section-sized windows:
   * the whole questions+answers stretch in QUESTIONS_CHUNK_PAGES=10 windows
     with 1-page overlap (1-2 calls per chapter instead of 3-6; every
     question shares one context → no boundary splits, no cross-window
     option drops);
   * the Solutions section in SOLUTIONS_CHUNK_PAGES=5 windows (recitation-
     safe — long verbatim spans are what trigger finish_reason=4).
   Overlap-token waste drops from 2/6 (33%) to 1/10 (10%), and fewer calls
   mean the 15 RPM window and the daily quota both last longer.
   **Safety:** pass activation is UNCHANGED (probe-based + extraction
   boundary, exactly like the old fixed windows) — the section labels only
   SIZE the windows and hard-reset the carry context at the Solutions
   boundary; a page the text layer mislabels can never have its Q-pass
   skipped (ch1 had 3 questions tailing into the first solution pages).
   Scanned-only chapters (no readable text layer) fall back to the old
   6-page fixed windows unchanged.
3. **Malformed-JSON + fallback windows** — the fixed-window fallback keeps
   its original 2-page overlap (fixed a regression where the new
   section-aware overlap logic zeroed it).

---

### 4.20 Changelog — 2026-08-02 (crash fix + model figure map)

1. **Crash fix** — "cannot access local variable 'batch_start'" after the
   section-aware rewire: the section loop no longer had the fixed-window
   index variable, but the 429-path, batch-failure log and orphan records
   still referenced it. `batch_start` is now the window's first PDF page.
2. **Model figure map (user ask: "bta ye image kis question ki h")** — the
   extraction prompts (Q/A/S) now require a `_figure_map` control object:
   one `{q_no, slot}` entry per visible figure in top-to-bottom reading
   order, page by page. `extract_batch_meta` peels it, and a new
   `claim_figure_map_images` pass attaches every image of a window to the
   question the model DECLARED (with an exact-count guard: if the declared
   count differs from the extracted count, the pass is skipped so a
   misalignment can never mis-attribute — those images still flow to the
   positional + 4th-pass attribution). This directly reduces the
   "unclaimed" images the user kept seeing.
3. **Model verdicts recorded** — when the 4th pass declares an owner but a
   guard (tiny-crop / over-attribution cap) refuses the rename, the model's
   verdict is now written to `unmatched_images.jsonl` (`model_verdicts`)
   and logged, so an image is never silently "unclaimed" — the reviewer
   sees exactly which question the model said it belongs to.

---

### 4.21 Changelog — 2026-08-02 (cross-field contamination hardening, run-7 audit)

Audit finding: on OCR/recovery pages, a recovered SOLUTION fragment could be
written into `question_text`, and OCR garbage (page numbers, watermarks,
footers) could enter `solution_text` -- the record still passed because
"field is populated" was treated as "field is valid". All 7 hardening points
implemented:

1. **Question-field protection** — `merge_question_records` is now
   provenance-aware: an item tagged `S_*`/`A_*` (solution/answer pass,
   retry, or OCR recovery) has its `question_text`/`options` DROPPED before
   anything merges. A solution recovery can never populate a stem.
2. **Patch-only recovery by field** — `_RECOVERY_SCOPE` per pass
   (Q→question/options, A→answer, S→solution/tables [+ the "Ans: B" line
   printed inside the solution block]). `drain_failed_pages` applies the
   scope of the pass that FAILED (persisted on the failed-page entry) to
   every drained/OCR item before merge; `targeted_retry` already patched
   only requested fields and now records provenance per patch.
3. **Cross-field contamination validator** — `qbank_validator.check_row`
   flags `contaminated_question` (HIGH): question_text that opens with
   explanation language or has ≥80% of its tokens inside the row's own
   solution is not a stem.
4. **Provenance tracking** — every record keeps `_prov` = {field: source}
   (`Q_PASS`, `S_PASS`, `A_PASS`, `Q_RETRY`/`A_RETRY`/`S_RETRY`,
   `OCR_S`/`OCR_Q`, `DRAIN_*`, `ORPHAN_*`, `RESCUE`, `RECOVER`). An
   `OCR_S`/`S_*` fragment is structurally unable to populate a stem (see
   #1); the sweep and `find_incomplete_records` use the recorded source when
   they strip/retry.
5. **OCR cleanup** — `_clean_ocr_text` (inside `ocr_fallback_text`, one
   choke point) strips whole-line page numbers, "Page N of M", urls,
   copyright lines and short repeated headers BEFORE anything merges;
   medical prose is preserved verbatim.
6. **Semantic completeness** — `find_incomplete_records` treats a
   contaminated stem as missing (`question`+`answer`+`options` re-ask), and
   the integrity sweep (step 5) strips contaminated stems so the retry
   refills them from the pages; the validator flags a non-empty solution
   carrying OCR noise (`ocr_noise_solution`).
7. **Regression tests** — `CrossFieldContaminationTests` (S/OCR-S can't fill
   stems, S-orphan blocked via recover_orphans, OCR cleanup, contaminated
   stem rejected at merge + treated missing, valid stem survives S-pass) and
   `ValidatorContaminationTests` (contaminated_question, explanation-opening,
   ocr_noise_solution, clean row). 53 tests total.

---

### 4.25 Changelog — 2026-08-04 (forensic hardening pass, run-11)

Full-book run ended `Validation: 96 flag(s) across 27/33 chapters`. A
log-forensics + code pass (54-chunk Railway log parsed programmatically)
found these root causes; the failure matrix is at `debug/root_cause_report.json`:

1. **RC-1 STALE-PATH IMAGE LIFECYCLE (proven, dominant false "unmatched
   image" source)** — the figure-map pass renames (moves) each claimed temp
   file to its final slot name, but the run-9 window loop only updated
   `leftover_by_page` for pages present in `fig_leftover`. A page whose
   images were ALL claimed kept its STALE TEMP NAMES in the leftover list →
   they flowed to `unmatched_images` → the 4th pass threw
   `FileNotFoundError` (`attribution call failed ... No such file`) for
   images that were ALREADY owned. Fix: every page fed to the figure-map is
   cleared to its post-map leftover ([] when fully claimed);
   `attribute_orphan_image` returns `already_claimed` for relocated files
   (no Gemini call); the 4th pass drops already-claimed refs.
2. **RC-3 missing-stem invisibility** — the contaminated-stem guard
   correctly rejects solution-prose-as-stem, but when the real stem was never
   captured the retry/rescue kept offering filtered text → records shipped
   STEM-LESS while the summary printed "0 missing answer / 0 missing
   solution". Chapter summary now reports `missing stem` + `bad options`.
3. **RC-4 answer reconciliation** — A-pass can return fewer key rows than
   the chapter's question count (ch7: 14 questions, 9 A-pass items → 4
   missing answers). `locate_missing_record_pages` now also locates
   ANSWER-KEY pages for answer-missing records, and rescue uses an
   ANSWER-ONLY prompt (`answer_rescue_prompt`) for answer gaps.
4. **RC-5 rescue 0-fields** — broad prompt + wrong target page + scope/
   contamination filters. Field-specific rescue prompts + key-page targeting.
5. **RC-6/13 structured page-pass status + page ledger** — every
   window-pass attempt is classified SUCCESS / EXPECTED_EMPTY / PARTIAL /
   RETRYABLE_FAILURE / UNRESOLVED and written to `data/page_ledger.jsonl`;
   a zero-item pass on its own section is PARTIAL (possible FAILED_ZERO),
   not silently SUCCESS.
6. **RC-8 section boundary re-fire** — `solutions_section_announced` makes
   the text-layer S boundary announce + carry-reset ONCE per chapter (was
   re-logged on every S window).
7. **EXPORT GATE** — `_export_gate_violations` checks before export:
   zero missing stems/options/answers/solutions, zero broken asset refs,
   zero unresolved passes; violations → `data/export_gate.jsonl` + loud
   `[GATE]` log. "0 missing answer / 0 missing solution" is no longer
   sufficient for a clean chapter.

Tests: `Run11ForensicHardeningTests` (8 cases). 90 total, all green.
Run artifacts: `debug/root_cause_report.json` (machine-readable matrix).

---

### 4.32 Changelog — 2026-08-08 (export-zip isolation: reset archives EVERYTHING)

**Symptom:** Railway's download ZIP contained PREVIOUS run's output JSONs even
after pressing Reset — the fresh run's zip still had old chapters/data.

**Root cause:** `reset_output()` archived only `data/`, `assets/` and
`state.json`. The per-subject bundle written by `build_subject_bundle()`
(`subjects/<SUB>/chapters/*.jsonl` + `questions.jsonl`) was LEFT BEHIND, so
a previous book's JSONs survived the reset and `make_zip()`'s `rglob("*")`
packaged them into the next export.

**Fix (`app.py`):**
- `_entries_to_archive(out)` — a reset now archives EVERYTHING under the
  output root except `_archive` itself (data/, assets/, subjects/, state.json,
  any strays). Fresh run starts truly empty; nothing from a previous run can
  leak into the zip.
- `_zip_skip(rel)` — belt-and-suspenders in `make_zip()`: never export
  `_archive/`, healer `*.bak-*` files, or `output_results.zip` itself.
- `reset_output()` uses `_entries_to_archive`; `make_zip()` uses `_zip_skip`.

Tests: `ZipResetIsolationTests` (+4): reset archives subjects too / excludes
the archive itself; zip skip rules (archive + backups + self excluded,
current content kept); end-to-end zip build has no `_archive` entries and
keeps fresh data + subjects.

Note: this pass builds on the user's manual `qbank_pipeline.py` updates
(commits `b3e543b` / `16cc70d` on GitHub — run-18 regex/tiebreaker
improvements) — merged cleanly, suite green. Suite: **145 tests OK**.

### 4.31 Changelog — 2026-08-06 (full-code audit: bug fixes + dead/duplicate code removal)

Full audit of all pipeline code (qbank_pipeline 6324→6260 lines; validator,
app, healer, tests reviewed). Fixes:

**Real bugs:**
- **routed_pages skipped EVERY pass** (`PREFLIGHT_OCR` recitation routing):
  a mixed sensitive page (questions + sensitive solutions) silently lost its
  QUESTIONS -- the page was excluded from Q/A/S and never "failed", so the
  drain never ran either. Now only the S-pass skips routed pages (their
  solutions were already OCR-recovered); Q and A always receive them.
- **Validator false positives on GOOD stems**: `qbank_validator`'s
  `_stem_contamination_reason` still used the pre-run-12 naive token
  containment rule, so it flagged real question-shaped stems that the
  pipeline now accepts (ch26 q1 class "...is called ___") as
  `contaminated_question` on every re-run. Synced to
  `_stem_reject_reason` semantics: question-shape narrowing + reverse
  containment (stem == own solution verbatim is always contamination).

**Dead code removed (zero references anywhere):** `answer_key_rows_seen`
(empty stub), `looks_like_solution_style_stem` (+ its only use of
`SOLUTION_STYLE_STEM_RE`), `claim_solution_page_images` (pre-run-9 compat
wrapper), `_record_chapter_ledger` (unused since ledger rows append inline).

**Duplicates removed:** the second copy of the image `order_key` (now reuses
`_order_imgs_by_position`); the separate `repair_option_labels` (the label
correction already runs inline inside `build_final_question`, the only
builder both the main path and `--recover` use).

**Cleanups:** `_page_crops` closes its PIL handle; unused `wdt` variable
removed. pyflakes-clean on all production files.

Tests: `Run17CodeAuditTests` (+2: routed-pages skip S-only) and
`ValidatorContaminationTests` (+2: real restated stem NOT flagged, verbatim
stem flagged). Suite: **141 tests OK**.

### 4.30 Changelog — 2026-08-06 (SIGKILL/OOM investigation + bounded-memory architecture)

**Symptom:** the fresh Railway run terminated around Chapter 11 with
`Worker (pid:3) was sent SIGKILL! Perhaps out of memory?` (gunicorn's guess).

**Verdict: CONFIRMED OOM (kernel OOM-killer), not a gunicorn assumption.**
`_RENDER_CACHE` was an UNBOUNDED module-global dict: every
`render_page_png` call stored a full-page PIL RGB render (~6.3 MB at
150 dpi letter). Q-activation OCR (every S-window), L2 OCR geometry and L3
full-page vision (+2 context pages per call) rendered ~150 pages by chapter
11 (~950 MB) — a Railway free container (512 MB) dies right there. The
timeline matches exactly.

**Fixes (bounded-memory architecture):**
1. `_RENDER_CACHE_MAX = 10` — the render cache is now a bounded LRU
   (oldest-evicted); `render_cache_size()`/`clear_render_cache()` helpers.
2. `clear_render_cache()` + `gc.collect()` at chapter end + a `[MEM]` peak-RSS
   telemetry line per chapter, so Railway logs show memory without waiting
   for a SIGKILL.
3. `full_page_vision_ownership` draws its highlight boxes on `page_img.copy()`
   — it used to mutate the CACHED render (re-renders returned highlighted
   images, and the drawn copy stayed resident).
4. `render_page_png` closes the PyMuPDF document in `finally` and removes the
   pdftoppm temp dir after loading (a ~6 MB PNG per page was leaking on disk).
5. **Crash-safe resume:** `questions.jsonl` is now rewritten ATOMICALLY per
   chapter (`rewrite_questions_file`: drop this chapter's old rows, append the
   fresh ones, dedupe keep-LAST by id, `os.replace`). `main()`, `app.py`
   (`/v2-test`) and `test_v2_chapter.py` pass the PATH instead of an append
   handle. The old design deduped only at the END of a full book, so a
   mid-book worker death after a re-run left duplicate rows behind; now ANY
   death point leaves the file equal to the last committed chapter.

**Resume verification:** `process_pdf` skips chapters in
`progress["chapters_done"]` (saved AFTER the atomic rewrite). A death between
rewrite and state-save re-runs the chapter, which REPLACES its rows (no dup);
a death after the save skips it. Both paths are duplication-free. Unit-tested.

Tests: `Run16MemoryAndResumeTests` (+7): bounded LRU eviction, 200-page
stress stays ≤10 entries / <10 MB, clear empties, vision never mutates the
cache (byte-identical), atomic rewrite removes partial chapters + dedupes
keep-last + tmp cleaned, rewrite-twice is exactly-once, no leaked temp dirs.
Suite: **137 tests OK**.

### 4.29 Changelog — 2026-08-06 (output-data audit of the fresh PAY run: 90 flags / 25 of 33)

The user's Drive `Output` folder (chapters.json, export_gate.jsonl,
image_ownership.jsonl, integrity_flags.jsonl, orphans.jsonl, page_ledger.jsonl,
PAY-001..033.jsonl) was audited against the code. Verified:

- **chapters.json clean**: 33 chapters, all PAY, no stale PSY artifacts.
- **image_ownership.jsonl**: 17 records, all high confidence with full
  provenance (page-4 → PAY-001-001 question via full_page_vision; p213 →
  deterministic_ocr_geometry). Run-13 image architecture confirmed working.
- **export_gate.jsonl**: 33 violations across 9 chapters (matches the log).
- **page_ledger.jsonl (the smoking gun)**: PAY-007 has ZERO Q-pass rows; PAY-002
  has no Q-pass on pages 22-30; PAY-016/18/19/24/25/28/30/32 are Q-skipped on
  their question pages. The text-layer solutions detector fired on each
  chapter's FIRST pages (previous chapter's solution tail) → whole chapter
  labeled "S" → Q-pass never ran → stems/options only via fragile retry.
- **PAY-007 data loss quantified**: records = q1-10 + q23-26 = 14;
  **q11-22 (12 questions) missing entirely** — A-pass only covered q1-10 and
  targeted retry only fixes EXISTING incomplete records, so never-created
  records are never retried.
- **PAY-002-025/026**: solution-only phantom records — ch1's q25/26 solutions
  spilled into ch2's page range and created phantom records.
- **PAY-007-023/025**: `question_text == solution_text` verbatim — the run-12
  question-shape narrowing let it through because the prose contains "which".
- **PAY-026 q1**: a REAL stem ("...is called ___") rejected 3× as
  "contaminated" (retry ×2 + rescue) → missing_stem.
- **orphans.jsonl**: 4 meaningful fragments (PAY-011 p149 tables, PAY-017 p218
  DRAIN options A/C/D, PAY-033 p356 option-D tail of q8).

**New methods added (each test-first, full suite green):**

1. **Q-pass coverage safety net** (`q_covered_pages` per chapter +
   `page_has_question_content`/`window_has_question_content`): a window with
   never-Q-covered pages runs the Q-pass whenever rendered-page OCR finds
   question-stem anchors (or when OCR is unavailable). This is what finally
   prevents the "whole chapter mislabeled S" silent question loss at the
   SOURCE (ch2/7/16/18/19/24/25/28/30/32), instead of relying on the fragile
   targeted retry.
2. **`drop_phantom_solution_only_records`**: solution-only records (no
   stem/options/answer provenance) are dropped ONLY with cross-chapter
   duplicate proof (same q_no + ≥50% solution similarity in prior
   questions.jsonl rows — ch1 q25/26 exists → ch2 phantom dropped). Without
   that proof the record is kept and gate-flagged (a real lost question is
   never silently deleted). Full records preserved in
   `data/dropped_phantom_records.jsonl`.
3. **Stem == solution verbatim rejection** (reverse-containment in
   `_stem_reject_reason`): a would-be stem that is (near-)identical to its own
   solution is contamination regardless of "which"/"is" question-shape words
   (ch7 q23/25); real stems restated in longer solutions still pass (ch26 q1).
4. **Orphan verified-duplicate rule** (recover_orphans rule 5): a q_no-less
   fragment whose text duplicates an existing record's option/question content
   (PAY-033 p356 option-D tail) is consumed deterministically, not orphaned.

Tests: `Run14PersistentProblemFixesTests` (+8). Suite: **130 tests OK**.

### 4.28 Changelog — 2026-08-06 (final audit of the fresh PAY run: 90 flags / 25 of 33 chapters)

Fresh run on current head (`PAY.pdf`, 33 chapters): validator reported
`90 flag(s) across 25/33 chapters`; export gate listed 33 violations across
8 chapters (ch2/7/16/18/19/24/25/26/28) + 5 unresolved images. Full log
(14 chunks) + code tracing. Full matrix: `debug/root_cause_report.json`.

**RC-14A — Q-pass skipped on real question pages (DOMINANT, ~all mass
stem/option loss).** `build_section_windows` labels every page >= the
text-layer `solutions_start` as "S"; `_should_run_q_pass` then returns False
for every S window without overlap/carry, and the sticky
`solutions_section_seen`/`probe["solutions"]` keeps it off. On chapters whose
first pages show solution headers (previous chapter's solution tail inside
the page range, or an interleaved answer-key page), the WHOLE chapter got
labeled "S" and the Q-pass never ran on the question pages: ch2/7/11/16/18/
19/24/25/28/30/32 needed 9-27 records each of [question]+[options] targeted
retry, and the tails (ch2 q25-26, ch7 q23-26, ch18 q13, ch19 q11-12, ch24
q12-13) were lost after 2 retry rounds. **Fix:** `window_has_question_content`
-- OCR question-stem headings on the RENDERED pages (immune to the garbled
body-font text layer, same filter as block_headers_on_page) force `do_q=True`
on S windows. Also robustified `ocr_page_anchors`: psm 6 → psm 4 → psm 11
fallback + relaxed confidence floor for digit tokens.

**RC-14B — Export gate printed CLEAN with unresolved orphans (accounting
inconsistency).** ch11/17/33 printed `orphans: N unresolved` right next to
`[GATE] ... CLEAN`; `_export_gate_violations` never consulted orphans.jsonl.
**Fix:** the gate now adds `orphan_unresolved` violations for meaningful
(any content field) unclaimed fragments; empty junk fragments stay silent.

**RC-14C — Sweep deleted stems the retry could not refill (data loss).** ch26
q1 / ch7 q24-26: sweep stripped the stem to None ("contaminated"), retry
blocked every re-ask ("blocked contaminated stem ... still missing"), record
shipped `missing_stem`. **Fix (stem quarantine):** the sweep now keeps the
text and sets `_stem_suspect_reason` (flagged, preserved for review); a
passing retry candidate replaces it even in fill_only recovery; the gate and
validator report `suspect_stem` instead of silently accepting or deleting.

**RC-14D — L3 full-page vision silently skipped → isolated-crop "decorative".**
p104/p209-454/p291/p319/p316 images fell to the isolated fallback (no log
line explaining why). **Fix:** render-unavailable and no-parsed-position paths
now log loudly, and unresolved entries carry method
`vision_skipped_no_position` / `all_levels_failed` for the audit trail.

**RC-14E — q_no=None tail fragments.** ch7 q23-26 options arrived as a
q_no-less fragment (orphan) even when Q-pass ran on the tail window. **Fix:**
`SCHEMA_PROMPT_Q` now instructs the model to repeat the q_no of a
within-page continuation (options under a figure / split by a table) instead
of emitting `q_no: null` for content whose number is visible on the page.

Tests: `Run13FinalAuditFixesTests` (+10): Q-activation on S windows with OCR
anchors, pure-solution windows stay skipped, no-OCR windows stay skipped,
gate flags meaningful orphans / ignores empty fragments, sweep quarantine
keeps data + gate flag, fill-only merge replaces a quarantined stem, vision
logs when positions are missing, prompt clause present, OCR psm fallback.
Suite: 122 tests OK.

### 4.27 Changelog — 2026-08-06 (unified image-ownership architecture, page-4 class)

Fresh production run: page 4's figure (`PSY/PSY-p4-7.webp`, source-verified
Q1 question image) still printed `ambiguous printed owners -`, stayed
unclaimed, and the 4th pass called it "decorative" -> `unresolved_images.jsonl`
while `[GATE] chapter 1: export gate CLEAN`. The run-9 geometry-first fix was
supposed to solve this class; it did not on the REAL page. Architecture-level
investigation (full report: `ROOT_CAUSE_ANALYSIS.md` section 13):

**Root cause — the entire deterministic system depends on the PDF TEXT LAYER.**
`question_headers_on_page` reads block headings from pypdf's text visitor;
`qns_printed_on_page`/`one_to_one` read them from pdftotext. On this book's
QUESTION pages the body-font text layer is garbled/absent (broken ToUnicode),
so BOTH text tools return nothing for question headings: L1 geometry finds no
header above the figure, the one-to-one matcher sees no printed q_no
("ambiguous printed owners -"), and the figure falls to a Gemini call that is
shown ONLY the isolated crop (no page layout, no printed anchors) — it
guessed "decorative" for a real figure. The synthetic tests passed because
they build PDFs with clean Helvetica text; none modelled (a) an unreadable
text layer, (b) Form-wrapped figures, or (c) an isolated-crop 4th pass.

**New unified ownership ladder (each level sees only the previous level's
leftovers; every assignment records provenance to
`data/image_ownership.jsonl`):**
- **L1 deterministic text-layer geometry** (run-9, unchanged) — closest
  question/solution heading above the image + cross-page carry. `image_positions_on_page`
  now also recurses into **Form XObjects** (masked/clipped figures were
  invisible to the old flat content-stream walk) and returns the drawn
  **w/h** (needed for the L3 bbox overlay).
- **L2 deterministic OCR-anchored geometry (NEW)** — same closest-heading-above
  rule, but headings come from **tesseract on the RENDERED page** (poppler
  `pdftoppm` or PyMuPDF), so it is immune to text-layer garble. Zero Gemini
  calls. Runs in the window loop before the model figure-map.
- **L3 full-page vision (NEW)** — for leftovers, the page is rendered at
  150 dpi, every leftover's drawn bbox is highlighted + labeled (IMG-1...),
  and Gemini answers ONE question per call (all page leftovers batched):
  which printed anchor (question number / option letter / solution header)
  owns the highlighted figure. Layout-only — the prompt forbids inferring
  from medical content. Adjacent pages are attached when a figure touches a
  page edge. Replaces the isolated-crop 4th pass as the primary fallback.
- **L4 unresolved_images.jsonl** — conservative, never discarded; the export
  gate now flags every entry (unless deterministic_junk: broken crop below
  MIN_IMAGE_BYTES). A single model "decorative" verdict no longer clears a
  chapter.

**Validator:** `_export_gate_violations` gained `unresolved_image` violations
(the old gate only inspected CLAIMED assets, so CLEAN could print while a real
Q1 figure sat unresolved); `qbank_validator.py` gained the `image_unresolved`
flag on the audit side.

**Tests (UnifiedImageOwnershipTests, +8):** Form-XObject position parsing with
drawn size; page-4 class end-to-end (garbled text layer -> vision claims Q1,
provenance written, model receives the rendered page); vision cannot override
deterministic geometry; OCR-anchor geometry with dead text layer; OCR line
matching + coordinate conversion without the tesseract binary; gate flags
unresolved images; broken crops excluded; opt-in real-PDF fixture test
(`fixtures/PSY.pdf` — dumps pypdf words/coords + bboxes for real page 4 and
asserts `PSY-p4-7` has a parsed position). Suite: 112 tests green (1 skipped
until the real PDF is dropped into `fixtures/`).

### 4.26 Changelog — 2026-08-05 (run-12: contaminated-stem dead-end + recovery targeting)

Second full-book run ended `89 flag(s) across 23/33 chapters`. Log forensics
(18 chunks) + code inspection. Full matrix: `debug/root_cause_report.json`.

**RC-12A — Contaminated-stem dead-end (dominant, ~all missing stems).**
Two compounding bugs:
1. `_stem_reject_reason` flagged ANY text whose tokens are ≥80% contained in
   the record's own solution. Medical solutions RESTATE the stem ("The
   correct answer is B. The patient presents with..."), so short
   QUESTION-SHAPED stems legitimately passed the threshold and were stripped
   as "contaminated" (false positive) — ch1 q3/q4/q10, ch2 q25, ch7
   q1/q23-26, ch11 q1/q17, ch16 q2/q10.
2. When a real contaminated re-read arrived, the stem-conflict coherence
   resolver preferred it (solution-prose coheres perfectly with the
   solution payload) and the generic merge loop overwrote — so a GOOD stem
   was replaced, the sweep stripped it, and retry/rescue then looped the
   same blocked text ("blocked contaminated stem ... still stem-missing",
   rescue 0 fields, export-gate missing_stem).
**Fix:** the token-containment rule now only fires for DECLARATIVE or
>250-char text (question-shaped short stems are kept); merge never lets a
contaminated stem replace a valid one (stem-conflict + generic write
guards); after one contamination block, retry switches to a STEM-REGION-ONLY
prompt; and the Q-pass no longer runs over pure-solution windows (the
upstream contamination source) — with 1-page cross-section overlap at the
Q/S boundary so boundary-spanning question tails are preserved.

**RC-12B — Answer rescue targeting.** `locate_missing_record_pages` only
matched pipe-format key rows on pages with the "Answer Key" header, so
answer-missing records' rescue asked the question page (ch15 q15 -> page 194
-> 0 fields). Answer rows are now matched on every page in pipe / list
("13. B") / dash ("13 - B") formats, header optional.

**RC-12C — Quota brake.** The real free-tier limit for this model is 500 RPD
(log: "limit: 500, model: gemini-3.1-flash-lite"); `MAX_CALLS_PER_DAY`
1400->480 so the daily stop is graceful instead of a hard 429 mid-run.

**RC-12D — Ledger false UNRESOLVED.** A successful same-batch re-ask after
malformed JSON left `pass_recovered=False` -> the pass was marked UNRESOLVED
(ch13 flagged `unresolved_page_A [175-179]` even though 14 items came back).
Now `pass_recovered=True` on the re-ask.

Tests: `Run12StemContaminationTests` (8) + `test_locate_answer_rows_without_probe_header`
+ SectionWindowTests boundary-overlap updates. 100 total, all green.

---

### 4.24 Changelog — 2026-08-04 (option-level image ownership, run-10)

Investigation (user asked: "do images that belong to MCQ options A/B/C/D get
preserved correctly?"). Finding:

- The output schema ALREADY had `options[].images` (per-option array) and
  `IMG_PATH_RE` already permitted `_OPT_A_01.webp` names -- but nothing ever
  populated them. `build_final_question` hardcoded `"images": []`.
- After run-9's geometry-first claimer, an image under option A was claimed
  by Q5's QUESTION block -> `Q5.question_images = [IMG A..D]`. Option-level
  association was LOST even though question ownership was correct.

Fix (deterministic, no new Gemini calls):

1. `image_positions_on_page` now returns `(y, x, draw_idx)` -- x was added
   for horizontal / 2x2 option rows (all 3 call sites updated).
2. `_page_word_lines` -- shared pypdf text-visitor word collector (no
   pdftotext); refactored `question_headers_on_page` and
   `solution_headers_on_page` onto it.
3. `option_anchors_in_block` -- option labels ("A." etc.) INSIDE a question
   block's vertical extent only (never in solution prose / tables / bullets).
   Detects line-start labels AND embedded word-start labels (horizontal rows)
   AND standalone label words.
4. `_assign_option` -- conservative geometry: an image belongs to the closest
   option-label row ABOVE it; a single-anchor row -> that option (vertical);
   a multi-anchor row -> nearest by x, only when unambiguous (margin
   `_OPTION_X_MARGIN`); a shared/equidistant figure or one above all rows ->
   stays QUESTION-LEVEL (never guessed, never dropped).
5. `claim_block_images` -- for question-kind owners, computes the block
   extent, finds option anchors, and assigns to `option` bucket
   (`image_files_by_q[qn]["option"][letter]`); `_rename_for_slot` gained
   kind="option" (`{QID}_OPT_{L}_{NN}.webp`). Solution blocks are NEVER
   option-scanned (an "Option A:" line in solution prose can't steal a
   figure).
6. `build_final_question` fills `options[].images`; `final_q_to_record`
   round-trips them (recovery preserves option ownership).
7. Gemini still runs only on leftovers and can never override a
   deterministic option assignment.

Edge cases verified by tests: normal stem image -> question-level; image
under option A -> option A; 4 vertical option images -> A/B/C/D; 2x2
horizontal -> correct via x+y; two images in one option -> both preserved;
image between stem and option A -> question-level; solution image with
"Option A:" prose -> solution image; shared/ambiguous figure -> question-
level (unresolved, not dropped); Gemini disagreement can't override;
JSON round-trip preserves option images; schema backward compatible
(options still have id/text, question images unchanged).

Tests: `OptionImageOwnershipTests` (13 cases). 82 total, all green.

---

### 4.23 Changelog — 2026-08-04 (geometry-first image ownership, run-9)

User evidence: the SAME extracted figure (PSY-p4-7.webp) was mapped to
PSY-001-001 in one run and then "decorative" in another; a single Gemini
verdict is not reliable enough to discard a real image.

**Why page 4 failed while page 33 works (investigated, PROVEN):**
- Page 33 (solution figure): `solution_headers_on_page` locates headings via
  **pypdf's text visitor** (works on this book) and assigns the closest
  header above each image by PDF y -- pure geometry, no gates.
- Page 4 (question figure): the question-side path `qns_printed_on_page`
  used the **pdftotext CLI** (garbled body text on this book) AND needed
  Gemini's `has_figure_in_question` flag and exactly-one printed q_no. Both
  failed → "ambiguous printed owners -" → the 4th-pass model said
  "decorative" → the image was permanently logged to decorative_images.jsonl.
- So the deterministic positional system existed ONLY for solution-side
  figures; question-side figures depended on the broken tool + a flag + a
  single unreliable model verdict.

**Fix (geometry-first, generalized):**
1. `question_headers_on_page` -- locates question-stem headings ("1.",
   "Q1.") via the SAME pypdf text visitor as solutions (positions in the
   same coordinate space).
2. `block_headers_on_page` -- merged (kind, q_no, y) for question AND
   solution headings, top-first. Question headings BELOW the first solution
   header on a page are dropped (a "1." line there is a list item in
   solution prose, not a stem).
3. `claim_block_images` -- every image belongs to the CLOSEST heading above
   it (question or solution), or to the carried `active_block` (cross-page
   continuation). Replaces the solution-only mapper; `claim_solution_page_images`
   kept as a compatibility wrapper.
4. Window loop is now GEOMETRY-FIRST: deterministic block ownership runs
   per page BEFORE the Gemini figure-map; the figure-map and 4th pass only
   see leftovers and can never override a deterministic assignment.
   `active_block` is captured from the carry state at WINDOW START (the
   block open at the end of the previous window).
5. **Conservative decorative** -- a single Gemini "decorative" verdict no
   longer discards an image: it is recorded to `data/unresolved_images.jsonl`
   (with the model verdict) and kept on disk for review. Only strong
   deterministic evidence (the watermark object id, already excluded at
   extraction) may permanently classify decorative.
6. Priority implemented (run-9 #5): A/B strong same-page block ownership →
   C cross-page carry → D Gemini fallback → E unresolved_images.jsonl.

Regression tests: `GeometryFirstImageTests` (9 cases: question-block image,
solution-block image, between-headings, multiple figures one block, multiple
questions by position, cross-page carried owner, cross-page without carry
stays unclaimed, watermark excluded at extraction, ambiguous → unresolved
not decorative, Gemini figure-map cannot override geometry). 69 total.

---

### 4.22 Changelog — 2026-08-04 (orphan-fragment root cause, run-8)

User asked to investigate why `q_no=None` / orphan fragments keep appearing
despite overlap pages being sent. Investigation findings:

- **Overlap WAS being passed** — both as re-sent page images AND as a text
  note ("OVERLAP from the previous batch... combine both sides into ONE
  complete item"). The user's hypothesis was PARTIALLY correct: the note was
  too weak and the prompts CONTRADICTED it.
- **Prompt contradiction** — Q/S passes said "NEVER invent a question
  number... return it as one item with q_no: null" for unnumbered
  continuations, directly contradicting the overlap-combine rule. Gemini
  defaulted to q_no=null.
- **Real root cause (PROVEN)** — `compute_carry`'s no-meta fallback required
  `rec.get("question_text")`, but S-pass records fill `solution_text` only.
  When Gemini omitted `_batch_meta` (common), the S-pass carry was NEVER
  created → logs showed `carry-in: -` → the next window had no continuity
  context naming the open question → the unnumbered continuation came back
  `q_no=null`.

Fixes:

1. **`compute_carry` S-pass fallback** — detects the pass shape from the
   items (S-shaped = solution_text present, no question_text). For S-pass,
   a non-empty solution on the window's highest q_no that `looks_truncated`
   proves the page ended mid-solution → carry that q_no as a "solution" cut.
   Q-pass fallback unchanged. This restores `carry-in` for solutions
   crossing page boundaries.
2. **Explicit overlap semantics in the generated context** —
   `build_carry_context(carry, overlap_pages, new_pages)` now emits
   `OVERLAP / CONTEXT PAGES:` and `NEW PAGES TO EXTRACT:` sections plus
   OWNERSHIP RULES (continuation belongs to the question whose heading is on
   the overlap page; keep assigning until a new heading; only q_no=null when
   ownership genuinely cannot be established). One master prompt, generated
   per batch — no hand-made prompts.
3. **Prompt contradiction removed** — Q and S passes now say: first use the
   OVERLAP/CONTEXT pages to determine the owner; only return q_no=null when
   ownership cannot be established.
4. **`recover_orphans` rule 3 tightened** — the PARTIAL owner append now
   requires the owner's existing solution to LOOK TRUNCATED (same signal as
   the carry fallback). Appending a low-overlap fragment to a COMPLETE
   solution was a wrong-owner guess; such fragments stay unassigned for
   review instead.
5. **Field separation preserved** — the run-7 provenance/scope hardening
   still guarantees an S-pass/OCR continuation never populates question_text.

Regression tests: `ContinuationOwnershipTests` (6 cases: heading-on-overlap
assigns owner; continuation-then-next-heading stays separate; unowned stays
unassigned; overlap re-extraction doesn't duplicate; S continuation never
enters question_text; existing content never overwritten). 59 tests total.

---

## 11. What feedback is wanted from the reviewer

- Correctness bugs / race conditions / data-loss paths in `qbank_pipeline.py` (merge, resume, quota exits).
- Prompt-engineering improvements for `SCHEMA_PROMPT`/retry/drain prompts (verbatim fidelity vs recitation).
- Architecture: is the sequential state-machine + sidecar-JSONL design right for ~20 books, or should anything be restructured (DB? task queue?) — keep in mind the user is non-technical and deploys only through the dashboard.
- Validator: missing deterministic checks; better heuristics for truncation/foreign-content detection without false positives.
- Cost/quota strategy on the free tier (parallelism, caching, model choice).
- Any security/robustness issues in the Flask dashboard (path handling, uploads, restores).
