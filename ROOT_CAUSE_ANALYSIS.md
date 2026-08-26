# Root-Cause Analysis — PSY production run (33 chapters, 434 questions)

Date: 2026-07-24. Evidence: Railway deploy log excerpt (12:40–13:11 UTC, chapters
1–21 + crash at 22), `qbank_pipeline.py` source at the time of the run, and the
reported ID lists. Every claim below is tagged **[PROVEN]** (log line and/or code
path demonstrates it), **[PATTERN]** (data signature strongly implies, needs one
visual check), or **[VERIFY]** (decisive check provided).

--------------------------------------------------------------------------------
## 1. Pipeline trace (as run)

```
PDF → pdftoppm (per chapter, 150dpi)
    → 6-page non-overlapping batches (process_pdf: range(0, len, PAGES_PER_GEMINI_CALL))
    → Gemini vision call per batch
    → JSON items
    → merge_question_records (chapter-wide dict, keyed by int(q_no))
    → image extraction per page + claim against records SO FAR (single pass)
    → build_final_question (IMG_PATH_RE validation, no content filtering)
    → questions.jsonl append
```

Field-level loss points inspected, in order:

| Stage | Can it lose `solution_text`? | Verdict |
|---|---|---|
| pdftoppm | no (rasterizes every page in range, filenames carry true page number) | clean |
| Batching | **YES** — 6-page windows have **no overlap**; Gemini sees only its own window | **LOSS POINT A** |
| Gemini response | yes (RECITATION aborts whole response; model may omit content) | **LOSS POINT B** |
| Parser (`json.loads`) | no — any parse failure raises → batch WARN (visible in log, none seen for ok batches) | clean |
| `q_no` guard in `merge_question_records` | **YES** — drops the WHOLE ITEM, including a solution-bearing fragment, when `q_no` is null/non-numeric | **LOSS POINT C** |
| Merge logic itself | no — chapter-global keyed by `q_no`; `solution_text` merges via truthy-overwrite; order-independent across batches | clean |
| Image attach | n/a for text fields | — |
| `build_final_question` / validation | no — passes `solution_text` through verbatim; only validates image *paths* | clean |
| JSONL write | no filter | clean |

--------------------------------------------------------------------------------
## 2. Root causes of the missing solutions

### RC-1 — Whole-batch RECITATION loss **[PROVEN]** → explains ch16 (and ch17's missing answers)

Log:
```
12:55:58 [WARN] Gemini call failed on PSY ch16 batch 12: Response did not finish
 normally (finish_reason=4). Likely safety-blocked or hit token limit
12:55:58 [PSY] chapter 16 ... done -> 17 questions (0 missing answer, 17 missing solution)
12:56:18 [WARN] Gemini call failed on PSY ch17 batch 0: ... finish_reason=4
12:56:24 [PSY] chapter 17 ... done -> 8 questions (8 missing answer, 0 missing solution)
```
`finish_reason=4` = RECITATION (verbatim text filter). Under the code that ran,
a failed batch was `continue`d with NO retry → all content on those chapter pages
lost. ch16 answers survived because they came from an answer-key table in a
different, surviving batch. Positions: ch16 `batch 12` ⇒ chapter pages 13–18
(absolute PDF pages = `ch16.file_start+12 .. +17`); ch17 `batch 0` ⇒ pages 1–6.
Your list has 15 IDs for ch16 while the log said 17 ⇒ 2 records were recovered by
a later reprocessing; **[VERIFY]** `grep -c '"PSY-016-' questions.jsonl` — duplicates
will confirm reprocessing happened.

### RC-2 — `q_no`-null fragment dropped WITH its content **[PROVEN]** → explains ≥1 ch4 loss, mechanism behind boundary splits

Log, immediately before "chapter 4 done (9 missing solution)":
```
12:51:21 [WARN] Gemini returned an item with no q_no, skipping:
 {'q_no': None, ..., 'solution_text': 'Option D: Displacement - Feelings that
  are connected with one person are displaced onto another person. ...
```
The item CARRIED REAL solution text and was discarded by the `q_no` guard
(`merge_question_records`). A second instance at 12:57:32 (a full "somnambulism"
QUESTION with `q_no: None`) proves it is a repeating failure mode, not a one-off.
(Per handoff rule #3 the skip itself was correct — never invent numbers — but the
content was dropped into a log line instead of being recovered.)

### RC-3 — Cross-batch split with no context **[PROVEN as mechanism]** → matches the singles (PSY-001-013; PSY-012-001 pending the ch12 anomaly check)

- Batches are hard windows `page_files[bs:bs+6]`, zero overlap ⇒ every page ≡ 6k
  boundary can split a question from its answer/solution.
- The continuation page IS sent — in the next request, WITHOUT the earlier pages.
  Gemini cannot number it ⇒ emits `q_no: null` (or omits it) ⇒ RC-2 discards it.
- The parser does NOT discard partial solutions; the merge is batch-agnostic.
  The single concrete loss point is: model-side context cut → fragment → guard.
- Singles like PSY-001-013 fit: one question sitting at a batch tail. **ch12
  anomaly:** the earlier run reported ch12 with 0 missing solutions, yet
  PSY-012-001 is missing now ⇒ its line in `questions.jsonl` was re-appended by a
  LATER run with null solution (reprocessing adds, never replaces).
  **[VERIFY]** `grep -c '"id": "PSY-012-001"' questions.jsonl` → >1 proves a
  duplicate; the older line likely still holds the solution.

### RC-4 — Source chapter gives answers without explanations **[PATTERN]** → most consistent for the consecutive blocks

PSY-004-001..009, PSY-006-001..006, PSY-011-001..005, PSY-032-001..008 are exact
consecutive runs starting at q001, and every one of those questions HAS its
correct answer (those chapters logged "0 missing answer"). Batch loss cannot
produce clean 1..N runs with answers intact; boundary splits don't prefer low
consecutive numbers. The consistent explanation: these MCQs are covered by an
answer-key table with no printed explanations (common in such books), i.e.
`solution_text` is null because THE BOOK has none — not because the pipeline lost
it. RC-2 proves ch4 also lost ≥1 REAL solution, so ch4 may be mixed.
**[VERIFY]** one look at the answer pages of ch4 q1–9 (or 1 manual Gemini call):
if no explanation text exists there, RC-4 confirmed and those IDs need NO
pipeline fix — they need "solution: none in source".

--------------------------------------------------------------------------------
## 3. Table 1 — missing solutions

| Question ID | PDF page | Gemini returned solution? | Lost stage | Root cause | Required fix |
|---|---|---|---|---|---|
| PSY-016-001..013, 016, 017 | chapter pp. 13–18 (= file_start+12..+17) | NO — response aborted (finish_reason=4) [PROVEN] | Gemini batch call | RC-1 whole-batch RECITATION, no retry in old code | Page-by-page retry (implemented) + reprocess ch16 |
| PSY-001-013 | single question at a batch tail (ch1) | partial/none surviving [PATTERN] | `q_no` guard at merge entry | RC-3 boundary split → RC-2 fragment drop | 2-page batch overlap + orphan salvage (implemented) + reprocess ch1 |
| PSY-012-001 | ch12 (see anomaly note) | unknown | — | **[VERIFY]** duplicate-line check above; likely overlap-loss in re-run | same as above |
| PSY-004-001..009 | answer section (~pp. 56–66 window) | YES for ≥1 fragment [PROVEN]; rest [PATTERN] | `q_no` guard (RC-2) + possibly source (RC-4) | RC-2 proven for the "Displacement" fragment; RC-4 suspected for the consecutive block | overlap + orphan salvage (implemented); visual check of ch4 answer pages decides RC-4 share |
| PSY-006-001..006 | answer-key area (ch6) | NO evidence any was emitted | likely nothing lost | RC-4 [PATTERN] | verify source pages; no code fix if confirmed |
| PSY-011-001..005 | answer-key area (ch11) | same | same | RC-4 [PATTERN] | same |
| PSY-032-001..008 | answer-key area (ch32, extracted in final run) | same | same | RC-4 [PATTERN] | same |

--------------------------------------------------------------------------------
## 4. Root causes of the unmatched images (Table 2)

Code facts [PROVEN]: (a) every non-watermark image ≥5000px on every page is
extracted — including illustrations that belong to NO question; (b) claiming is
single-pass: a page's images are offered ONLY to questions already in
`chapter_records` that Gemini flagged `has_figure_*` and that lack that kind yet;
(c) there is NO later re-check → a figure whose owning question is introduced by
a LATER batch can never be claimed; (d) if Gemini never sets the flag (imaging
questions it "read past"), no claimant can ever exist; (e) watermark detection
samples only the first 30 pages — a recurring decoration that starts late in the
book is never excluded.

| Image file | PDF page | Image type | Expected owner | Why unmatched | Required fix |
|---|---|---|---|---|---|
| PSY-p56-127.webp | 56 (ch4 window) | textbook illustration — likely the 720×980 grayscale portrait the handoff documented as a REAL (non-watermark) object [PATTERN, open file to confirm] | none — not an MCQ figure | extracted correctly; no flagged claimant exists by design | quarantine + manifest (implemented); no further fix |
| PSY-p62-141.webp | 62 (ch4/5 window) | textbook illustration [PATTERN] | none | same as above | same |
| PSY-p126-279.webp | 126 (ch9 Dementia window, log 12:53:31) | probable MCQ figure (imaging question) or chapter illustration [VERIFY: open file] | a ch9 question IF the file shows a scan | owner never received `has_figure_in_question=true` from Gemini (flag miss) OR illustration with no owner | manifest (implemented) + manual attach if it is an MCQ figure |
| PSY-p272-596.webp | 272 (ch22 window) | content image [VERIFY: open file] | a ch22 question if figure | claimant absent at extraction time (owner batch later) or flag miss | second-pass matching (implemented) covers the "owner later" case |
| PSY-p272-970.webp | 272 (ch22 window) | recurring object #970 — the same object aliased twice on p264 (earlier crash); now seen on ≥2 pages ⇒ recurring decorative element/stamp more likely than an MCQ figure [PATTERN] | likely none | recurs late in book ⇒ outside the 30-page watermark sample; extracted, never claimed | manifest (implemented); optional: extend watermark sampling beyond page 30 |

Component blame summary: parser NO, validation NO, merge NO (for images),
alias-dedupe NO (it prevented the crash; unrelated to ownership). YES:
claim-time ordering (single pass), Gemini flag misses, watermark-sample scope,
and content that simply has no owner (design gap = nowhere to file it).

--------------------------------------------------------------------------------
## 5. Cross-batch verdict (your 6 questions)

1. *Does batching assume atomic containment within one call?* YES at the
   model-call level (hard 6-page windows); the merge was batch-agnostic, but the
   model never saw both sides of a cut.
2. *Can a question/solution span two batches?* YES — any page ≡ 6k boundary.
3. *Is the continuation page ignored?* Not ignored — sent without context, so it
   comes back as `q_no: null` fragments.
4. *Does the parser discard incomplete solutions?* NO — truthy-merge keeps any
   partial text.
5. *Does merge fail on the second half?* It never receives it — dropped upstream
   at the `q_no` guard (RC-2).
6. *Could this explain ch1/4/6/11/12/16/32?* ch16 = RC-1 (batch abort).
   ch4 = RC-2 proven + RC-4 suspected. ch1-013 / ch12-001 = boundary-split
   signature. ch6/11/32 consecutive blocks = RC-4 signature (source has no
   explanations), not boundary loss.

--------------------------------------------------------------------------------
## 6. Implemented fixes (same commit as this document)

1. **2-page batch overlap** (`BATCH_OVERLAP_PAGES = 2`): windows step by 4, so
   every interior page appears in TWO calls with full context — any split ≤2
   pages is seen WHOLE in at least one call. Cost: ~+50% calls/book (~60→~90,
   far under the 1400/day self-brake; free tier is ~1500 RPD per project plus a
   ~15 RPM per-minute cap, so burst 429s now get a 65s backoff retry before the
   run is declared over). Not "just bigger batches": page count per call
   stays 6.
2. **Idempotent merge for tables** (dedupe by markdown) — overlap re-extraction
   can no longer duplicate tables.
3. **Orphan salvage** — items Gemini returns with null/invalid `q_no` are no
   longer log-and-forget: they're appended to `data/orphans.jsonl` with their
   chapter, batch offset and exact PDF pages, and the prompt now explicitly asks
   Gemini to EMIT numberless continuation fragments as `q_no: null` items so they
   reach the salvage file instead of vanishing inside the model.
4. **Second-pass image claiming** at chapter end — figures extracted before
   their owner question's batch are retried against the complete chapter records;
   permanent leftovers are logged to `data/unmatched_images.jsonl` (the locked
   assets/questions/{SUBJECT}/ convention is unchanged).
5. Image extraction skips overlap pages (`pages_imaged`) — no duplicate work.

Recovery path for the already-damaged chapters (needs state surgery, do after
the book finishes): remove PSY-001/004/012/016 (and 006/011/032 ONLY if the
visual RC-4 check shows explanations DO exist) from `chapters_done`, delete their
lines from questions.jsonl, re-run. The new code re-extracts with overlap+retry.

--------------------------------------------------------------------------------
## Run-4 audit RCA (2026-07-26 full-output audit) — wrong-owner, truncation,
## image & orphan classes; fixes shipped in this changeset

Evidence: final questions.jsonl (434 rows), validation_report.json (63 flags),
orphans.jsonl (7), run-4 log. Every class below now has a deterministic
PREVENTION in the pipeline and/or a DETECTOR in the validator.

### A4-1 — Wrong-owner stem (PSY-012-001 carried PSY-012-013's chart stem) **[PROVEN]**
Two batches extracted q1 with different stems (log: "question text for q1 differs
between batches (similarity 0.25)"); the merge let LAST WRITE WIN silently. The
record's own solution described a different (mania) patient -> duplicate_text
flag (similarity 1.000) and a confusing same-stem/two-answers pair.
**Fix:** merge-time STEM CONFLICT resolver -- keeps the variant whose text
coheres with the record's OWN options+solution (_stem_payload_coherence);
undecidable conflicts keep the first variant AND both variants go to
data/stem_conflicts.jsonl (never a silent pick again). fill_only mode never
overwrites. Plus chapter_integrity_sweep strips a provably-wrong duplicate stem
pre-retry so targeted retry (Gap-1 anchor) refills it in the SAME run.
Validator duplicate_text now reports suspect_id via the same coherence score.

### A4-2 — Foreign "Option X:" fragment glued onto a solution head (PSY-009-007) **[PROVEN]**
An orphan fragment beginning "Option C: Catharsis is..." (belonging to
PSY-009-006's explanation tail) was appended to PSY-009-007's solution by the
carry-forward owner rule. Owner option C is "Dementia with Lewy bodies" -- the
fragment provably cannot belong.
**Fix:** _foreign_option_line guard in recover_orphans: an "Option X:"-headed
solution fragment only merges when the owner's option X text appears in the
fragment head; blocked fragments stay in orphans.jsonl with blocked_reason.
Sweep additionally strips such a head when the SAME line exists verbatim on
another record of the chapter (stray-duplicate proof); otherwise it flags for
review and destroys nothing. Validator: foreign_option_head (HIGH).

### A4-3 — Truncated solutions (PSY-023-007 mid-word "• During ", PSY-006-009 "criteria:") **[PROVEN]**
**Fix:** looks_truncated_solution (dangling connector/colon, raw trailing
space after a word = mid-flow cut, short-and-bare) drives the integrity sweep's
forced re-ask list; targeted_retry REPLACES a forced record's solution only
with a LONGER verbatim re-ask; the still-incomplete log re-judges live (a
healed record is not logged as missing). The validator's noisy
"ends-without-terminal-punctuation" heuristic (53/55 false positives against
this book's bullet endings) is replaced by the same strict patterns, and now
also catches short_bare_solution (LOW), solution_header_furniture (LOW,
"Solution to Question N:" leading), solution_recitation_dump (HIGH).

### A4-4 — Options polluted by neighbouring explanation prose (PSY-008-007) **[PROVEN]**
Options A/B/D held CAGE/CHAT/GAD explanation sentences; the real options are
visible in the record's own solution option-lines. **Fix:** validator
option_solution_disagree (HIGH): a solution "Option X" line whose following
segment shares no tokens with the row's option X text. fix_output.py P2 heals
the shipped row from its own solution lines (verbatim).

### A4-5 — Recitation-block dumps inside a solution (PSY-032-003; header leaks 032-001/002) **[PROVEN]**
**Fix:** sanitize_solution_text at build_final_question: strips leading
"Solution to Question N:" furniture and truncates an embedded header ONLY
when the chunk immediately after it restates this solution's own earlier
content (dump proof); anything else is kept and reported, never destroyed.
Validator flags both kinds.

### A4-6 — Broken/tiny figures shipped (PSY-003-014_Q_01 = 414 bytes) **[PROVEN]**
A sub-1.5KB webp cannot hold a real MCQ figure. **Fix:** _rename_for_slot
refuses auto-claim of < MIN_IMAGE_BYTES (the leftover goes to the model
fourth-pass, which decides on actual content); build_final_question drops
missing/tiny refs so a broken figure can never ship. Validator:
suspicious_tiny_image (HIGH).

### A4-7 — Over-attributed images (PSY-022-003: SEVEN question-side figures) **[PROVEN]**
Each individual model attribution was reasonable; the SUM was nonsense.
**Fix:** MAX_QUESTION_IMAGES=3 cap inside _rename_for_slot (covers the greedy,
one-to-one, third-pass and fourth-pass paths at one choke point); the sweep
trims older rows' excess to unmatched_images.jsonl. Validator:
over_attributed_images (LOW).

### A4-8 — Duplicate tables inside one solution (PSY-012-008 x2, PSY-009-005 x3) **[PROVEN]**
Overlap re-reads with squished source whitespace bypassed the exact-key
dedupe. **Fix:** build_final_question dedupes by whitespace-insensitive
markdown key; validator duplicate_table (LOW).

### A4-9 — Orphan ledger noise (5 answer-key tables + 2 duplicate scraps) **[REFUTED as data loss]**
All 7 run-4 orphans were benign: five printed answer keys whose rows were
already filled (73/73 rows across 11 key-checked chapters matched the output),
one stem dup of PSY-006-013, one options dup of PSY-006-007.
**Fix:** recover_orphans consumes fully-verified keys (0 new fills) instead of
persisting them, consumes verbatim duplicate scraps, and writes
answer_key_disagrees rows to data/integrity_flags.jsonl (free wrong-answer
alarm). Keys referencing q_nos outside the chapter are still kept.

### A4-10 — Answer-key table parked in a random question's solution (cosmetic)
005-014, 009-001, 011-030, 017-001, 021-001, 030-001. Harmless (it is the
book's printed key; we used it as ground truth). Validator:
stray_answer_key_table (LOW, informational).

### Page-by-page PDF-vs-JSON QA (user request)
The hybrid validator's stage-2 witness (--audit) enumerates every printed
question/component per chapter pages and CODE diffs it against the JSONL --
that IS the page-by-page comparison; it now also diffs the figure component
(a figure the witness saw but JSON lacks -> audit_component_missing).

--------------------------------------------------------------------------------
## 6. Run-7 audit — cross-field contamination (2026-08-02)

### A7-1 — Recovered SOLUTION fragment written into question_text **[PROVEN mechanism]**
On pages where normal extraction fails (crop-ladder / OCR restructure), a
solution fragment can come back inside `question_text` (Gemini fills both
fields, or an S-pass/OCR item carries a stray stem). The old merge wrote any
non-empty `question_text` into the record, so a populated-but-wrong stem
shipped and passed the completeness validator ("field is populated" == "field
is valid").
**Fix:** provenance-aware merge — every item carries `_prov` (Q_PASS/S_PASS/
A_PASS, *_RETRY, OCR_*, DRAIN_*, ORPHAN_*, RESCUE, RECOVER); S/A items have
`question_text`/`options` dropped before merging; `recover_orphans` gates
stem/option fills to Q-pass fragments; drain/OCR applies the failed pass's
field scope (`_RECOVERY_SCOPE`); targeted retry rejects contaminated stems
and records provenance per patch. Validator: `contaminated_question` (HIGH).

### A7-2 — OCR garbage (page numbers / watermarks / footers) in solution **[PROVEN mechanism]**
Tesseract text was merged raw; standalone page numbers, "Page N of M",
urls and copyright lines could land inside solution_text and still pass
non-empty.
**Fix:** `_clean_ocr_text` strips whole-line page noise at the
`ocr_fallback_text` choke point before any merge/splice (prose preserved
verbatim); validator flags leftovers as `ocr_noise_solution`.

### A7-3 — Semantic completeness (stem that is really a solution)
`find_incomplete_records` treats a contaminated stem as missing
(question+answer+options re-ask) and the integrity sweep (step 5) strips it
for same-run refill; `_stem_reject_reason` = explanation-style opener OR
>=80% token containment in the record's own solution (>=60 chars).

--------------------------------------------------------------------------------
## 7. Run-8 audit — recurring q_no=None / orphan fragments (2026-08-04)

### A8-1 — S-pass carry NEVER created when _batch_meta is absent **[PROVEN]**
Log signature: `overlap: [17] | carry-in: - | last-open: -` while a
`Solution to Question 10:` heading sits at the bottom of the overlap page.
`compute_carry`'s no-meta fallback required `rec.get("question_text")` --
but S-pass records fill `solution_text` ONLY, so the fallback never fired
for the S-pass. Result: the next window had no continuity context naming the
open question, the prompt's "NEVER invent a question number" rule won over
the weak overlap note, and the unnumbered continuation came back q_no=null
(orphan).
**Fix:** pass-shape detection (S-shaped = solution_text without
question_text); for S-pass, a non-empty solution on the window's highest
q_no that `looks_truncated` proves the page ended mid-solution -> carry that
q_no ("solution" cut). Q-pass fallback unchanged. Plus: explicit
OVERLAP/CONTEXT + NEW PAGES + OWNERSHIP RULES in the generated context
(`build_carry_context`), and the Q/S prompts now say to resolve the owner
from the overlap pages before falling back to q_no=null. The user's overlap
hypothesis was partially correct (overlap WAS sent; the carry gap + prompt
contradiction were the real culprits).

### A8-2 — recover_orphans rule 3 wrong-owner guess **[PROVEN mechanism]**
The PARTIAL owner append fired on ANY low-overlap fragment even when the
owner's existing solution was COMPLETE -- a blind guess that could glue a
neighbour's or a new question's text onto a finished solution.
**Fix:** the append now requires the owner's existing solution to LOOK
TRUNCATED (same deterministic signal as the carry fallback). Unowned
fragments stay unassigned for review instead of being guessed into a record.

--------------------------------------------------------------------------------
## 8. Run-9 audit — geometry-first image ownership (2026-08-04)

### A9-1 — Question-side figures had NO deterministic positional system **[PROVEN]**
PSY-p4-7.webp mapped to PSY-001-001 in one run, then "decorative" in another.
Investigation: the SOLUTION-side mapper (page 33 -> PSY-002-014) locates
headings via pypdf's text visitor and assigns the closest header above each
image by PDF y. The QUESTION-side path used the pdftotext CLI (garbled body
text on this book) + Gemini's has_figure flag + exactly-one-q_no gate; when
those failed, a single 4th-pass Gemini "decorative" verdict permanently
discarded the image.
**Fix:** `question_headers_on_page` (pypdf visitor, "1."/"Q1." stems) +
`block_headers_on_page` (merged question+solution headings; question
headings below the first solution header are dropped) + `claim_block_images`
(closest heading above each image, or the carried active_block for cross-page
continuations). Window loop is geometry-first; the Gemini figure-map and 4th
pass only run on leftovers and cannot override deterministic ownership.

### A9-2 — Single Gemini "decorative" verdict discarded real images **[PROVEN]**
**Fix:** conservative decorative -- a "decorative" verdict now records the
image to data/unresolved_images.jsonl (with the model verdict) and keeps it
on disk for review; only strong deterministic evidence (watermark object id,
already excluded at extraction) may permanently classify decorative.

--------------------------------------------------------------------------------
## 9. Run-10 audit — option-level image ownership (2026-08-04)

### A10-1 — Option-associated images collapsed into question-level **[PROVEN]**
`build_final_question` shipped `options[].images = []` (hardcoded) even
though the schema and the `_OPT_{L}_NN.webp` filename convention already
existed; run-9's block claimer put every image inside Q5's question block
into `Q5.question_images = [A,B,C,D]`. Option ownership was lost at
build time.
**Fix:** `option_anchors_in_block` (question-block-restricted label anchors
via the pypdf visitor) + `_assign_option` (closest label row above; x-geometry
for horizontal/2x2 rows, unambiguous only) wired into `claim_block_images`;
`image_files_by_q[qn]["option"]` bucket + `_rename_for_slot` kind="option";
`build_final_question` populates `options[].images`; `final_q_to_record`
round-trips. Ambiguous figures stay question-level (never guessed, never
dropped); solution blocks are never option-scanned.

--------------------------------------------------------------------------------
## 5. Run-5 audit — solutions-page figure mapping (2026-08-02)

### A5-1 — Whole solutions page dumped onto ONE decoded header (user report:
### "maps 7 figures into 2 solutions") **[PROVEN mechanism]**
The header-binding shortcut (`sol_owners == 1` → attach every image of the
page to that solution) fired whenever the text layer decoded exactly ONE
`Solution to Question N:` header. The PSY book's body-page text layer is
partially garbled (documented in §1 of this file), so a page with SEVEN
solution blocks routinely surfaced a single header; two such pages → 7
figures collapsed into 2 solutions. Also reachable via the one-to-one
matcher when a stray line-start number made `qns_printed_on_page` return a
single q_no on a solutions page.
**Fix:** `claim_solution_page_images` — each figure is assigned to the
CLOSEST header whose baseline sits above its bottom edge, using real PDF
y-positions from pypdf's text visitor (`solution_headers_on_page`) and the
existing content-stream image positions (same bottom-left coordinate space,
no extra subprocess, no coordinate conversion). Figures with no locatable
header above them are left unclaimed → model/manual pass (never guessed).
Applied in process_pdf AND in --recover mode. `MAX_SOLUTION_IMAGES = 2`
caps the deterministic path at the `_rename_for_slot` choke point; sweep
step 4b trims older rows' excess; validator: over_attributed_solution_images
(LOW). Regression tests: test_pipeline_json.py::SolutionFigureMappingTests.

--------------------------------------------------------------------------------
## 10. Run-11 audit — forensic hardening (2026-08-04, 96 flags / 27 chapters)

Full-book run ended 96 flags. Log forensics (54-chunk Railway log) + code
inspection. Full matrix: debug/root_cause_report.json.

### A11-1 — STALE-PATH IMAGE LIFECYCLE (false "unmatched image") **[PROVEN]**
figure-map claims rename temp files to final slot names, but the caller kept
the stale temp names in the leftover list for fully-claimed pages →
4th-pass `FileNotFoundError` ("attribution call failed ... No such file") →
false unmatched count. Seen for p67-150/151, p83-186, p104-229, p119-260,
p120-263. Fix: clear leftover for map-fed pages; `already_claimed` guard;
4th pass drops relocated refs.

### A11-2 — Missing-stem invisibility **[PROVEN]**
Contaminated-stem guard correct, but stems stripped and never refilled
shipped while "0 missing answer / 0 missing solution" printed. Fix: summary
+ export gate count missing stems/options.

### A11-3 — Answer reconciliation gap **[PROVEN mechanism]**
A-pass fewer rows than questions (ch7: 14 q, 9 key rows, 4 missing answers).
Fix: locate answer-key pages for answer gaps + answer-only rescue prompt +
export gate missing_answer.

### A11-4 — Structured page-pass status + page ledger **[PROVEN mechanism]**
Zero-item passes and recovered-after-error passes were indistinguishable.
Fix: SUCCESS/EXPECTED_EMPTY/PARTIAL/RETRYABLE_FAILURE/UNRESOLVED per
window-pass → data/page_ledger.jsonl; chapter-end gate surfaces UNRESOLVED.

### A11-5 — Section-boundary re-fire **[PROVEN]**
"[SECTION] solutions section begins" fired on every S window. Fix:
solutions_section_announced (announce/reset once; Q-pass activation unchanged).

### A11-6 — Export gate **[NEW]**
Deterministic pre-export check (missing stems/options/answers/solutions,
broken asset refs, unresolved passes) → data/export_gate.jsonl + loud log.

--------------------------------------------------------------------------------
## 11. Run-12 audit — contaminated-stem dead-end + recovery targeting (2026-08-05)

Second full-book run ended 89 flags / 23 chapters. Log forensics (18 chunks)
+ code. Full matrix: debug/root_cause_report.json.

### A12-1 — Contaminated-stem dead-end **[PROVEN mechanism + false positive]**
Good question-shaped stems stripped: solutions RESTATE the stem, so short
question-shaped stems pass the >=80% token-containment check and were
destroyed. Additionally, when a genuinely contaminated re-read arrived, the
stem-conflict coherence resolver preferred it (solution-prose coheres with
the solution payload) and the generic merge overwrote the good stem. Retry
then re-asked the same solution pages, the guard blocked the same text every
round, rescue returned 0 fields, and export-gate flagged missing_stem.
Fix: guard narrowed to declarative/>250-char text; merge never lets a
contaminated stem replace a valid one; retry switches to a stem-region-only
prompt after one block; Q-pass no longer runs on pure-solution windows
(upstream contamination source) with 1-page cross-overlap at the Q/S
boundary preserving boundary-spanning tails.

### A12-2 — Answer rescue targeted the wrong page **[PROVEN mechanism]**
Answer rows only matched pipe format on pages with the "Answer Key" header;
a key page with "13. B" rows and no header was missed, so answer rescue
asked the question page (ch15 q15 -> page 194 -> 0 fields). Fix: answer-row
matchers run on every page in pipe/list/dash formats, header optional.

### A12-3 — Quota brake never fired (500 RPD real limit) **[PROVEN]**
MAX_CALLS_PER_DAY=1400 vs the model's actual 500 RPD -> hard 429 mid-run +
2 wasted backoff calls. Fix: 480 (graceful daily stop, resume via state).

### A12-4 — Malformed-JSON recovery falsely UNRESOLVED **[PROVEN]**
pass_recovered only set in the salvage-fail branch; a successful same-batch
re-ask left it False -> ch13 gate flagged unresolved_page_A [175-179] even
though 14 items came back. Fix: pass_recovered=True on the re-ask.

--------------------------------------------------------------------------------
## 12. Z-AI verification pass (2026-08-05) — log commit attribution + pattern verdicts

The independently-reviewed log was generated by commit **a6b12d7** (run-11
build): it shows the OLD contaminated-stem retry message (no stem-region-only
switch), the OLD MAX_CALLS_PER_DAY=1400 (hard 500-RPD quota stop), and Q-pass
running on pure-solution windows. The two genuine bug classes it exhibits
were already fixed in 0fcd8c5. Pattern verdicts (full matrix:
debug/root_cause_report.json):

- A. Contaminated-stem loop: CONFIRMED as OLD-RUN bug; ALREADY FIXED in
  current code (guard narrowing + merge protection + stem-region-only retry +
  Q-pass skip on solution windows + Q/S cross-overlap).
- B. q_no=None options/tables: PARTIALLY CONFIRMED — fragments preserved in
  orphans.jsonl (never deleted), never attached without ownership proof;
  answer-key TABLE orphans consumed deterministically (rule 0). Option-only
  fragments (ch7 q23-26) stay unresolved because no ownership evidence exists
  in the current data model. Z-AI's "nearest question" fix is UNSAFE
  (adversarial Layout 1/2) and was NOT implemented.
- C. Export gate: intentional audit-only soft warning (log + export_gate.jsonl
  included in ZIP); hard-blocking would break the resume workflow. Not a bug.
- D. DRAIN/OCR merge: FALSE — every drain/OCR merge is fill_only=True and the
  OCR splice is append-only (_novel_solution_tail); no overwrite path exists
  (ch17 p218: 2 crop items + 1 OCR item merged, chapter gate CLEAN).
- E. Figure-map count mismatch: by-design safe-skip (exact-count guard); the
  map is skipped rather than mis-aligned; images resolve via positional +
  4th pass or stay unresolved — never discarded.

Production-shaped regression tests added (Z-AI scenarios 1-3 + adversarial
layouts 1-2): ZAIVerificationTests (4). Suite: 104/104 green.

## 13. Run-13 audit — image-ownership architecture (page-4 class) (2026-08-06)

**Symptom.** A fresh production run on current head still fails to attach the
source-verified Q1 question figure `PSY/PSY-p4-7.webp`: `[IMG] page 4:
ambiguous printed owners -; not auto-attaching image(s)` → unclaimed →
4th pass "model says decorative" → `unresolved_images.jsonl` → `[GATE]
chapter 1: export gate CLEAN`. The run-9 geometry-first fix (ade7921) claimed
this class solved; on the real page it was not.

### A13-1 — The whole deterministic system depends on the PDF text layer **[PROVEN mechanism]**

Trace of the page-4 path in current code:

1. `claim_block_images` (L1) → `block_headers_on_page` →
   `question_headers_on_page` → `_page_word_lines` (pypdf text visitor).
   On this book's QUESTION pages the body-font text layer is garbled/absent
   (broken ToUnicode CMap — the same garble that broke the old pdftotext
   path, which is why run-9 swapped tools, but a broken font map defeats
   EVERY text tool). Result: no question headings → `headers == []`.
2. First window of ch1 → `active_block is None` (no carry yet) → the
   `(not headers and active_block is None)` guard returns the image
   unclaimed before any position logic runs.
3. `claim_page_images_one_to_one` → `qns_printed_on_page` (pdftotext) →
   also garbled → `[]` → "ambiguous printed owners -".
4. Figure-map: exact-count guard skips on any mismatch (2 declared vs 1
   extracted) — by design.
5. 4th pass: `attribute_orphan_image` sends the ISOLATED crop + a text list
   of stems. No page layout, no printed anchors, no option letters, no
   neighboring question numbers → for a clinical figure the model has only
   medical content to judge → "decorative".
6. Conservative record → `unresolved_images.jsonl`; the export gate never
   read that file (it only checked CLAIMED assets) → "CLEAN". False negative.

**Why the synthetic tests missed it.** `_write_test_pdf` builds pages with
clean Helvetica text (pypdf visitor decodes "1. First question stem"
perfectly), images drawn directly in the page content stream below the
heading. Every run-9 test therefore exercised the happy path: headers found,
position parsed, geometry attached. None modelled (a) an unreadable/garbled
text layer, (b) an image wrapped in a Form XObject (mask/clip wrappers the
flat content-stream walk silently skips → no position), or (c) the
isolated-crop 4th pass. Production failed precisely because all three held
at once.

### A13-2 — Form-XObject-wrapped images have no position (second latent bug) **[PROVEN in code]**

`image_positions_on_page` walked only the page's own content stream and
recorded positions ONLY for `/Subtype /Image` `Do`s. Figures wrapped in
Form objects (very common for masked/clipped plates) resolved to
`/Subtype /Form` → skipped → `pos.get(oid)` missing → the image can never be
geometrically owned, even on a page with a perfect text layer. Fixed by
recursing into Form content with CTM composition (cycle-guarded) and by
recording the drawn width/height from the cm scale (also required for the L3
bbox overlay).

### A13-3 — Isolated-crop "decorative" verdicts are unreliable **[PROVEN in code]**

`attribute_orphan_image` builds parts = `[prompt, Image.open(img_file)]` —
one cropped figure, no page. The model cannot see "1." above it or "A. B."
beside it, so a "decorative" verdict is evidence-free. The run-9 conservative
rule already refused to DISCARD on that verdict; run-13 goes further and
removes the information deficit: L3 shows the full rendered page with the
figure's bbox highlighted, so ownership is decided from printed anchors.

### A13-4 — Export gate could print CLEAN with an unresolved relevant image **[PROVEN in code]**

`_export_gate_violations` checked stems/options/answers/solutions/claimed
asset refs/unresolved page-ledger rows — never `unresolved_images.jsonl`.
Fixed: every non-junk unresolved entry is now a gate violation
(`unresolved_image`), and the post-run validator flags them
(`image_unresolved`). Deterministically non-relevant = watermark object id
(already excluded at extraction) and broken crops below MIN_IMAGE_BYTES.

**Fix (chosen architecture).** Hierarchical, deterministic-first:
L1 text-layer geometry → L2 OCR-anchored geometry on the rendered page
(tesseract, zero API cost) → L3 full-page vision (rendered page, highlighted
bboxes, layout-only, one call per page, adjacent pages on edge figures) →
L4 unresolved + gate flag. Every automatic assignment writes
`data/image_ownership.jsonl` (owner, slot, method, evidence, confidence).
See `PIPELINE_WORKFLOW.md` 4.27. Tests: `UnifiedImageOwnershipTests` (+8,
112 total green; opt-in real-PDF fixture test in `RealPdfOptInFixtureTests`).

## 14. Run-14 audit — fresh PAY production run (90 flags / 25 chapters) (2026-08-06)

Fresh run on current head (subject code PAY, same 3988 KB book, 33 chapters).
Validator summary: `90 flag(s) across 25/33 chapters`. The run-13 image
architecture WORKED (page-4 figure owned via full-page vision
`PAY-001-001 (question) [high]`; ch1 gate CLEAN; 4 of 5 unresolved images
now carry explicit gate violations). Remaining damage clusters below.

### A14-1 — Q-pass skipped on real question pages (mass stem/option loss) **[PROVEN mechanism, dominant]**

Chapters where the text-layer solutions detector fired on the chapter's
FIRST pages (previous chapter's solution tail inside the page range) had the
whole chapter labeled "S": `build_section_windows` → every window "S" →
`_should_run_q_pass` False (no overlap/carry) → the Q-pass never ran on the
question pages. The A/S passes still ran (answer-key rows + leftover
solutions), so records existed with answers but NO stems/options; only the
fragile 2-round targeted retry recovered them (ch11 27/27, ch16 17/17, ch25
12/12, ch30 10/10, ch32 9/9 healed; ch2 q25-26, ch7 q23-26, ch18 q13, ch19
q11-12, ch24 q12-13 lost). Log proof: every such chapter shows
`[GEMINI:Q] pages X-Y` MISSING on the question windows, followed by
`RETRY round 1: N question(s) still incomplete (q1[question] ... )`.
Fix: `window_has_question_content` (rendered-page OCR question anchors above
the first solution header) forces `do_q=True` on S windows; `ocr_page_anchors`
gained psm 6→4→11 fallback + digit confidence floor.

### A14-2 — Export gate CLEAN while orphans unresolved **[PROVEN inconsistency]**

ch11 (1 unresolved), ch17 (3 unresolved), ch33 (1 unresolved) printed
`orphans: N unresolved` in the chapter summary AND `[GATE] ... CLEAN`.
`_export_gate_violations` never read orphans.jsonl. Fix: meaningful unclaimed
fragments (any of question_text/options/correct_option/solution_text/tables)
now block CLEAN as `orphan_unresolved`.

### A14-3 — Sweep deleted stems retry could not refill **[PROVEN data loss]**

ch26 q1: `[SWEEP] q1: stripped contaminated stem` → retry blocked every
candidate (`blocked contaminated stem for q1 ... next round ... still
missing`, rescue 0 fields) → `missing_stem 1`. ch7 q24/q26 same loop. The
run-12 guard narrowing was not enough for these cases; the strip-to-None was
irreversible. Fix: quarantine — keep the text + `_stem_suspect_reason`,
retry may replace it (fill_only conflict path now replaces quarantined
stems), gate/validator report `suspect_stem`. No stem is ever deleted
without a replacement again.

### A14-4 — L3 vision silently skipped for some images **[PROVEN in code]**

p104/p209-454/p291/p319/p316 reached the isolated-crop fallback (which
labeled some "decorative") with no log line from the vision level. Root:
`image_positions_on_page` returned no drawn bbox for those figures (Form /
unusual content-stream placement), and `full_page_vision_ownership` returned
silently when `labels` was empty. Fix: loud skip logs + unresolved method
`vision_skipped_no_position`. NOTE: the under-lying position-parse gap for
those pages needs the real PDF to debug (see REMAINING UNCERTAINTY).

### A14-5 — q_no=None tail fragments (ch7 q23-26 options) **[PARTIALLY PROVEN]**

Even with Q-pass running on the tail window (pages 104-107), Gemini returned
the q23-26 options as ONE q_no-less fragment → orphan → options=[] for
q23-26 forever. Fix: prompt clause forces within-page continuations to
repeat the visible q_no. Full deterministic orphan alignment (ordinal
provenance per page) remains a design item (see REMAINING UNCERTAINTY).

**Tests:** `Run13FinalAuditFixesTests` (+10). Suite 122 OK.

## 15. Run-15 audit — output-data verification of the fresh PAY run (2026-08-06)

The user's Drive `Output` folder was audited directly (chapters.json,
export_gate.jsonl, image_ownership.jsonl, integrity_flags.jsonl,
orphans.jsonl, page_ledger.jsonl, PAY-001..033.jsonl).

### A15-1 — Q-pass coverage gap, PROVEN from page_ledger (ch7: 12 questions missing)

PAY-007 has ZERO `pass: "Q"` rows in page_ledger.jsonl. The text-layer
solutions detector fired on page 100 (ch6's solution tail), the whole chapter
was labeled "S", and `_should_run_q_pass` returned False for every window. The
A-pass returned 10 answer rows (q1-10), the S-pass 13 solution fragments. The
final ch7 file contains records q1-10 + q23-26 = 14 records; **q11-22 never
existed as records** (A-pass did not cover them, Q never ran, and targeted
retry only re-asks EXISTING incomplete records). Same ledger signature for
ch2 (no Q on pages 22-30), ch16/18/19/24/25/28/30/32 (Q absent or overlap-only).
Fix: run-14/15 Q-coverage safety net (q_covered_pages + rendered-page OCR
question-anchor check) forces the Q-pass on never-covered question pages.

### A15-2 — Phantom solution-only records (ch2 q25/26) **[PROVEN]**

PAY-002-025/026: question null, options [], answer [], solution present
("Hysteria develops due to fixation in the phallic stage..." / "Big five
personality traits..."). These are ch1's q25/26 SOLUTIONS (ch1 output has
q25/26 complete; topics are ch1's). The S-pass read ch1's solution headers
inside ch2's page range and created phantom records. Fix: phantom drop with
cross-chapter duplicate proof + full preservation ledger.

### A15-3 — Stem == solution verbatim slipped the guard (ch7 q23/25) **[PROVEN]**

PAY-007-023 question_text == solution_text ("The patient has developed acute
muscular dystonia ... within 1-5 days of drug intake."); PAY-007-025 same
("Clozapine is the only drug ..."). Both contain "which"/"is", so the run-12
question-shape narrowing let them through the containment rule. Fix:
reverse-containment — a stem (near-)identical to its own solution is rejected
verbatim; real stems restated in longer solutions still pass.

### A15-4 — Real stem rejected 3x as contaminated (ch26 q1) **[PROVEN]**

integrity_flags: `contaminated_stem_stripped` q1 (PAY-026), then retry ×2 and
rescue all rejected "The acts that a person says or does to disclose himself
as having the status of boy or man is called ___." — a REAL stem whose solution
restates it. Fixed by run-14 stem quarantine (kept + flagged) + the
reverse-containment rule (real stem now passes).

### A15-5 — Orphan tails (PAY-033 p356) **[PROVEN]**

orphans.jsonl: "(d) the person has recently shown..." duplicates q8's option D.
Fix: recover_orphans rule 5 verified-duplicate consumption.

**Packaging:** chapters.json has only PAY chapters (no stale PSY); export gate,
orphans, ledger all present in the folder. No stale-artifact leak found.

## 16. Run-16 audit — SIGKILL / OOM (bounded-memory architecture) (2026-08-06)

**Symptom.** The fresh Railway run terminated around Chapter 11:
`Worker (pid:3) was sent SIGKILL! Perhaps out of memory?` — gunicorn's guess.

### A16-1 — Unbounded render cache = CONFIRMED OOM **[PROVEN]**

`_RENDER_CACHE` was a module-global dict with no eviction. Every
`render_page_png` call stored a full-page PIL RGB image (letter @ 150 dpi =
1275×1650×3 ≈ 6.3 MB). Call sites render per page:
- `window_has_question_content` / `page_has_question_content` (Q-activation
  OCR on every S-window's new pages),
- `claim_block_images_ocr` (L2 OCR geometry on every page with leftover
  images),
- `full_page_vision_ownership` (L3 page + up to 2 adjacent context pages).

By chapter 11 of the 33-chapter PAY run (~pages 3..160), ~130-150 renders
were cached ≈ 850 MB-1 GB. Railway free containers have 512 MB → the kernel
OOM-killer SIGKILLed pid 3 exactly around chapter 11. The gunicorn message is
a *guess*; the code + the run's page ledger prove the mechanism.

Additional leaks found while tracing:
- `full_page_vision_ownership` MUTATED the cached render (drew red boxes on
  the cached PIL object) — re-renders returned already-highlighted images and
  the drawn copy stayed resident.
- PyMuPDF `doc` was never explicitly closed (native memory freed only when
  the refcount happened to drop).
- pdftoppm temp dirs (each holding a ~6 MB PNG) were never removed.

### A16-2 — Resume after worker death could duplicate records **[PROVEN]**

`main()` opened `questions.jsonl` in append mode; `_dedupe_questions_by_id`
ran only at the very END of a full book. A SIGKILL mid-chapter leaves
partially-flushed rows for a chapter NOT in `chapters_done`; on resume the
chapter re-runs and appends again → duplicates persist until a COMPLETE run
finishes. Fixed: per-chapter atomic rewrite (`rewrite_questions_file`) with
keep-LAST-by-id dedupe and `os.replace`; `process_pdf` commits the chapter
file before `save_state`, so every death point leaves the file = last
committed chapter and resume is duplication-free (unit-tested, both
orderings).

**Bounded-memory architecture:** `_RENDER_CACHE_MAX=10` LRU + chapter-end
`clear_render_cache()`/`gc.collect()` + `[MEM]` peak-RSS telemetry per
chapter; PyMuPDF `doc.close()` in `finally`; pdftoppm temp-dir cleanup;
vision draws on a copy. Tests: `Run16MemoryAndResumeTests` (+7). Suite
137 OK. See `PIPELINE_WORKFLOW.md` 4.30.
