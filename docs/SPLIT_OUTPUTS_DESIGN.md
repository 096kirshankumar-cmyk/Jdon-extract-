# Split-Output Layer Design (Q / A / S → three JSONL files)

**Status:** DESIGN (no code written yet) · **Branch:** `arena/019fe0d7-json-extract` · **Date:** 2026-08-08

This document is the contract review for the requested change. It is a
**strictly-additive** write layer: nothing in the existing pipeline is
replaced, removed, or reordered. The internal extraction/recovery
safeguards (carry-forward, hallucination guards, orphan recovery,
image ownership, retries, integrity sweep, rescue, critique) keep running
exactly as today. The only new step is **three sidecar JSONL files written
atomically per chapter, after all in-pipeline reconciliations are done.**

The three sidecar files are joined by a common `q_id` and carry a
deterministic `q_id_anchors` provenance vector plus a four-level
`q_id_grade` (RESOLVED_ANCHORED / RESOLVED / PROVISIONAL / UNRESOLVED).

The existing `data/questions.jsonl` is **untouched** and remains the source
of truth for the dashboard and `qbank_validator.py`.

---

## 1. Output files

Per chapter, after `build_final_question` and the existing per-chapter
write of `data/by_chapter/{chapter_id}.jsonl` and the
`rewrite_questions_file` atomic rewrite of `data/questions.jsonl`, write:

```
data/split/{subject}/{chapter_id}/
  questions.jsonl      # question-side data only
  answers.jsonl        # correct-option data only
  solutions.jsonl      # solution/explanation data only
  unresolved_qids.jsonl   # records that could not be reliably reconciled
  orphans.jsonl          # fragments that could not be assigned a q_id
  chapter_completeness.json  # machine-readable summary
```

(The `unresolved_images` content already lives in
`data/unresolved_images.jsonl` as a global ledger; per-chapter scope is
added to the `chapter_completeness.json` summary. The existing
`data/orphans.jsonl` global ledger is **not duplicated**; per-chapter
`orphans.jsonl` is a chapter-scoped view used by the master-data builder
that joins on `q_id`.)

The three data files are **strictly separate** and **never** carry data
from the other side. Joining them is the consumer's responsibility via
`q_id`.

### 1.1 questions.jsonl

One record per question that has a question-side (stem/options/tables)
output. Records that are completely empty in every question field are
**NOT** emitted (they are already dropped by the existing
`drop_anchorless` step).

```jsonc
{
  "q_id": "PSY-007-023",
  "chapter_id": "PSY-007",
  "subject": "PSY",
  "chapter_no": 7,
  "q_no": 23,
  "q_id_grade": "RESOLVED_ANCHORED",   // one of four grades
  "q_no_anchors": { /* see §3 */ },

  "question_text": "…",                 // verbatim from the source
  "options": [
    {"id": "A", "text": "…", "images": []},
    {"id": "B", "text": "…", "images": []},
    {"id": "C", "text": "…", "images": []},
    {"id": "D", "text": "…", "images": []}
  ],
  "question_images": [
    {"file": "PSY/PSY-007-023_Q_01.webp", "source_pages": [123]}
  ],
  "tables": [
    {"type": "comparison", "markdown": "| … |", "file": null}
  ],
  "source_pages": [123, 124],

  "extraction_status": "COMPLETE"        // or "INCOMPLETE"
  // when INCOMPLETE, "missing_fields": ["options", "tables", ...]
}
```

### 1.2 answers.jsonl

One record per question that has a `correct_option` (or an unresolved
`correct_option`). A question that never produced an answer still gets a
row, with `correct_option: null` and `extraction_status: "INCOMPLETE"` —
this is the explicit "we know we don't know" path; we do not invent.

```jsonc
{
  "q_id": "PSY-007-023",
  "chapter_id": "PSY-007",
  "subject": "PSY",
  "chapter_no": 7,
  "q_no": 23,
  "correct_option": "B",                 // null when genuinely missing
  "correct_option_prov": "A_PASS",      // which pass produced it
  "q_id_grade": "RESOLVED_ANCHORED",
  "source_pages": [145],                 // the page the key was read from

  "extraction_status": "COMPLETE"
  // when INCOMPLETE: "missing_fields": ["correct_option"]
}
```

### 1.3 solutions.jsonl

One record per question that has solution data (or attempted solution
data). A question whose solution is missing still gets a row with
`solution_text: ""` and `extraction_status: "INCOMPLETE"`.

```jsonc
{
  "q_id": "PSY-007-023",
  "chapter_id": "PSY-007",
  "subject": "PSY",
  "chapter_no": 7,
  "q_no": 23,
  "solution_text": "…",                  // "" when missing
  "tables": [
    {"type": "comparison", "markdown": "| … |", "file": null}
  ],
  "solution_images": [
    {"file": "PSY/PSY-007-023_S_01.webp", "source_pages": [146]}
  ],
  "solution_prov": "S_PASS",             // which pass produced it
  "q_id_grade": "RESOLVED_ANCHORED",
  "source_pages": [146, 147],

  "extraction_status": "COMPLETE"
  // when INCOMPLETE: "missing_fields": ["solution"]
}
```

### 1.4 Common `q_id` semantics

`q_id = f"{subject}-{chapter_no:03d}-{q_no:03d}"` — same as today's
`build_final_question` produces for the master `questions.jsonl` row.

`q_id_grade` is the same string across all three files for a given source
question. A master-data builder joins the three files on `q_id` and uses
`q_id_grade` as the confidence signal. A consumer MUST NOT silently
override its own re-verification based on the grade; the grade is
advisory, derived from the pipeline's reconciliation, and the consumer
is free to re-verify against the source PDF.

---

## 2. Grading taxonomy

Four grades, computed deterministically from the in-pipeline evidence
collected during extraction:

| Grade | Condition |
|---|---|
| `RESOLVED_ANCHORED` | At least 2 anchors from the set `{printed_stem, printed_solution_header, answer_key_row, ocr_stem, ocr_solution_header}` agree on the q_no, AND at least one of `printed_stem` or `printed_solution_header` is set. |
| `RESOLVED` | (≥2 anchors agree) OR (1 anchor + a connected run of ≥3 q_nos that contains it) OR (1 anchor + a valid carry-forward origin). |
| `PROVISIONAL` | Only the model's q_no is available, no second anchor, no continuity. Preserved but flagged for downstream/manual verification. |
| `UNRESOLVED` | (a) No anchor at all, OR (b) two anchors disagree, OR (c) `model_q_no_disagree` is True (different passes emitted different q_nos that don't reconcile), OR (d) the only q_no is a known foreign / hallucination source. |

```python
# Pseudocode for the grader (see §3 for the data feeding it)
def grade_from_anchors(anchors):
    if anchors["model_q_no_disagree"]:
        return "UNRESOLVED"
    matches = sum(bool(a) for a in (
        anchors["printed_stem_match"],
        anchors["printed_solution_header_match"],
        anchors["answer_key_row_match"],
        anchors["ocr_stem_match"],
        anchors["ocr_solution_header_match"],
    ))
    if matches == 0:
        return "UNRESOLVED"
    if matches >= 2 and (anchors["printed_stem_match"]
                        or anchors["printed_solution_header_match"]):
        return "RESOLVED_ANCHORED"
    if matches >= 2:
        return "RESOLVED"
    # matches == 1
    if anchors["neighbor_run"] or anchors["carry_forward_origin"]:
        return "RESOLVED"
    return "PROVISIONAL"
```

`UNRESOLVED` records are **never** written to
`data/split/.../questions.jsonl` (because the question's q_id cannot be
trusted). They are written to `unresolved_qids.jsonl` only.

---

## 3. `q_no_anchors` — the deterministic provenance vector

Every `q_id` carries the same `q_no_anchors` object across the three
files. Only fields that are actually populated are present (JSON
`null` when missing).

```jsonc
"q_no_anchors": {
  "model_q_no": 23,                    // integer (always)
  "model_q_no_provs": ["Q_PASS"],      // list of pass labels (Q_PASS /
                                       //  A_PASS / S_PASS / Q_RETRY /
                                       //  A_RETRY / S_RETRY / RESCUE /
                                       //  RECOVER / OCR_S / DRAIN_Q / ...)
  "model_q_no_disagree": false,         // true when passes disagree

  "printed_stem_match": {              // from question_headers_on_page
    "page": 123,
    "header_text": "23."
  },
  "printed_solution_header_match": {   // from solution_headers_on_page
    "page": 146,
    "header_text": "Solution to Question 23:"
  },
  "answer_key_row_match": {            // from ANSWER_KEY_ROW_RE
    "page": 145,
    "row": "| 23 | B |"
  },
  "ocr_stem_match": {                  // from ocr_page_anchors
    "page": 123,
    "q_no": 23
  },
  "ocr_solution_header_match": {       // from ocr_page_anchors
    "page": 146,
    "q_no": 23
  },

  "neighbor_run": {                    // populated for Q-pass items in
                                       //  the run-18 GUARD (process_pdf
                                       //  L5495) — extracted unchanged
    "size": 12,
    "first": 18,
    "last": 29,
    "near_chapter_max": true
  },

  "carry_forward_origin": {            // populated by compute_carry
    "from_window": 3,
    "cut_part": "options",
    "resolved_in_window": 4
  },

  "section_position": {                // from block_headers_on_page
    "kind": "question",                //  "question" | "solution"
    "page": 123,
    "y_baseline": 423.5
  },

  "provenance_notes": [                // free-form short strings
    "Q_RETRY unchanged",
    "stem coherence 0.78"
  ]
}
```

### 3.1 Population strategy

`q_no_anchors` is **passively observed** during the existing extraction
loop and finalized at chapter close by a new `reconcile_qids(...)` step
that runs once, after every merge / orphan / retry / rescue / sweep /
critique has happened. It does not call Gemini; it reads in-memory
state only.

**During the batch loop (existing, unchanged):**
- Every time `merge_question_records` runs, the existing
  per-field `_prov` dictionary is already populated by the call site at
  process_pdf L5678 (`it["_prov"] = f"{pass_name}_PASS"`) and by the
  Q-pass GUARD's `run-18` neighbor-run analysis. The split layer simply
  reads these and **adds** the anchor fields; it does not change them.
- The run-18 GUARD's `batch_qnos` and `runs` analysis (the connected
  run used to determine `trusted_qnos`) feeds the `neighbor_run` anchor
  via a small observation pass added alongside.
- `qn_source_pages[qn]` (already populated by the existing
  `process_pdf` loop) is the source of `source_pages` in the output.

**At chapter close, before writes (new step):**
- `reconcile_qids(chapter_records, image_files_by_q, qn_source_pages, pdf_path, page_files, subject, chapter_no, printed_sol_qns)`
  - For each `qn` in `chapter_records`:
    - Gather `printed_stem_match` from `question_headers_on_page` for every
      page in `qn_source_pages[qn]` (read-only; does not add a Gemini
      call).
    - Gather `printed_solution_header_match` from
      `solution_headers_on_page` (same).
    - Gather `answer_key_row_match` from `chapter_printed_solution_qns`-style
      text-layer scan over the same pages (zero-token; same as the existing
      `locate_missing_record_pages`).
    - `ocr_*_match` are populated lazily: only when the text layer was
      garbled (the same condition the existing pipeline already routes to
      OCR). For v1 we set them to `null` and let the run-18 GUARD's
      `seen_here` cache be the source of truth for OCR-verified q_nos.
    - `model_q_no_provs` is collected from `rec.get("_prov", {})` keys
      ("question_text", "options", "correct_option", "solution_text",
      "tables").
    - `model_q_no_disagree` is True when the `q_no` in
      `rec.get("_prov")` differs from another pass's q_no for the same
      record (detected during merge via the `last_qn_in_batch` plus
      `carry_q_no` mechanism — we observe the existing signal, we do not
      re-run it).
  - The grader is applied per record.
  - `UNRESOLVED` records are removed from `chapter_records` BEFORE the
    split writes (so the split files don't carry untrustworthy q_ids).
  - The four per-chapter files (questions/answers/solutions/unresolved_qids)
    are written atomically per the existing per-chapter pattern.

### 3.2 Cross-chapter phantom handling

The existing `drop_phantom_solution_only_records` (process_pdf L6022)
already handles the cross-chapter solution-spill case (ch1 q25 → ch2
q25). It runs **before** `reconcile_qids`, so by the time we grade,
cross-chapter phantoms are already gone. The split writer therefore only
sees a clean `chapter_records`.

---

## 4. `extraction_status` field

Every record carries one of two values:
- `"COMPLETE"` — every required field for the file type is non-empty
  (questions: stem + 4 options; answers: correct_option; solutions:
  solution_text). `tables` and `images` are not required.
- `"INCOMPLETE"` — at least one required field is missing. A
  `missing_fields` list is added, e.g. `"missing_fields": ["options"]`.

We **never** replace missing content with placeholder text, we **never**
guess the answer, and we **never** delete a record because it's
incomplete. The contract is: `extraction_status` is a flag, not a
filter.

---

## 5. Option images — preserving per-option image ownership

The user's spec is explicit that options can contain their own images
and that these must not be confused with question/stem images or
solution images. The existing pipeline already supports this: the
`option` slot in `image_files_by_q` was added in run-10 and is populated
by `claim_block_images_ocr` and `full_page_vision_ownership` when the
option-anchor geometry says an image belongs to an option (slot =
`"option"`, plus an `option_letter`).

For the split:
- The image filename convention is already deterministic:
  `{subject}-{chapter_no:03d}-{q_no:03d}_OPT_{LETTER}_{NN}.webp` for
  option images. The existing `IMG_PATH_RE` regex pattern is preserved
  unchanged.
- In `questions.jsonl`, each `options[].images` lists the per-option
  images. The top-level `question_images` only carries stem / question-
  block figures. `solution_images` only carries solution-block figures.
  Three namespaces, never overlap.
- The user's "if an option contains an image INSTEAD of text, preserve
  both" requirement is honored: option rows can have `text: ""` and
  `images: [..]`, or `text: "..."` and `images: [..]`, or neither
  (dropped by the existing `valid_images` filter as broken-crop junk).
- The user's "if an option image cannot be confidently associated with
  an option, preserve it in the unresolved-image report" is satisfied by
  the existing `image_files_by_q` fallback path: an image that fails
  option-anchor geometry (L2/L3) is recorded to
  `data/unresolved_images.jsonl` with provenance (not deleted). The split
  writer reads `unresolved_images` and lists per-chapter counts in
  `chapter_completeness.json`.

### 5.1 Image directory layout (additive)

The existing image directory is:
```
assets/questions/{subject}/{subject}-p{page}-{obj_id}.webp
```

The new spec asks for an explicit question/option/solution sub-folder.
To avoid disrupting the existing pipeline (which uses the flat layout
with `image_files_by_q[qn]["option"][letter] = [...]` to remember option
images), we keep the existing layout **and** add a small convenience:
the split writer produces a manifest that maps each `q_id` to its
files with explicit `type` labels, so a master-data builder can
materialize the per-type folder layout downstream if it wants to.

```
data/split/{subject}/{chapter_id}/
  image_manifest.jsonl
```

Where each line is:
```jsonc
{
  "q_id": "PSY-007-023",
  "type": "QUESTION" | "OPTION" | "SOLUTION",
  "option_letter": "A" | null,
  "file": "PSY/PSY-007-023_Q_01.webp",
  "source_pages": [123]
}
```

The actual `assets/questions/...` files are NOT moved or copied. The
manifest is the cross-reference; the files live where they always have.

---

## 6. `unresolved_qids.jsonl` schema

One line per unresolved q_id (i.e. per record removed from
`chapter_records` because `q_id_grade == "UNRESOLVED"`):

```jsonc
{
  "q_id": "PSY-007-024",
  "chapter_id": "PSY-007",
  "subject": "PSY",
  "chapter_no": 7,
  "q_no": 24,
  "kind": "unresolved_qid",
  "reason": "model_q_no_disagree",          // see enum below
  "q_no_anchors": { /* the full anchors vector */ },
  "available_passes": {
    "Q_PASS": { "had_item": true,  "q_no": 23,  "fields": ["question_text","options"] },
    "S_PASS": { "had_item": true,  "q_no": 24,  "fields": ["solution_text"] }
  },
  "source_pages": [124]
}
```

`reason` enum (kept short, future-extensible):
- `"no_anchor_at_all"` — matches == 0
- `"model_q_no_disagree"` — different passes produced different q_nos
- `"conflicting_anchors"` — two printed anchors name different q_nos
- `"hallucinated_q_no"` — q_no has no printed anchor and is not in a
  trusted run
- `"solution_q_no_not_in_printed_header"` — S-pass/A-pass only
- `"answer_q_no_not_in_printed_key"` — A-pass only
- `"two_possible_questions"` — first-line sibling-donor proof fired
- `"question_continues_from_previous_page"` — carried but unresolved
- `"foreign_chapter_q_no"` — q_no from a different chapter

If a record's solution-side q_no is RELIABLE but its question-side
record is not (Case 2 of the spec), the question-side row goes to
`unresolved_qids.jsonl` and the solutions row is also dropped, with
`reason = "missing_question_for_solution"`. The downstream master-data
builder sees the explicit gap; it does not silently invent a question.

---

## 7. `orphans.jsonl` schema (chapter-scoped)

One line per fragment that arrived in `orphans` (the existing per-chapter
list) and was **not** successfully attached by `recover_orphans`:

```jsonc
{
  "subject": "PSY",
  "chapter_id": "PSY-007",
  "source_pages": [123, 124],
  "pass": "S_PASS",                       // which pass produced it
  "reason": "q_id_unresolved",             // see enum
  "fragment": "...",                       // truncated 600-char snippet
  "carry_q_no": null,
  "cut_part": null,
  "last_qn_in_batch": 22
}
```

The global `data/orphans.jsonl` ledger is unchanged and continues to
be appended to. The chapter-scoped `data/split/.../orphans.jsonl` is a
chapter view that the master-data builder joins on `chapter_id`.

`reason` values that are now possible (extending the existing
`unconfirmed_discontinuous_qno` etc.):
- `"q_id_unresolved"` — no anchor and not in a trusted run
- `"foreign_option_line_head"` — `_foreign_option_line` fired
- `"first_line_in_sibling_solution"` — `_solution_fragment_foreign` proof
- `"owner_attached_but_speculative"` — keep, but mark for review

---

## 8. `chapter_completeness.json` schema

One file per chapter. Machine-readable, no prose:

```jsonc
{
  "chapter_id": "PSY-007",
  "subject": "PSY",
  "chapter_no": 7,
  "ts": "2026-08-08T12:34:56Z",

  "question_records": 30,
  "answer_records": 30,
  "solution_records": 29,
  "image_manifest_records": 47,

  "incomplete_questions": 2,
  "incomplete_answers": 0,
  "incomplete_solutions": 1,

  "unresolved_qid_count": 1,
  "unresolved_qid_q_nos": [24],

  "orphan_count": 3,
  "unresolved_image_count": 1,

  "q_id_grade_counts": {
    "RESOLVED_ANCHORED": 27,
    "RESOLVED": 2,
    "PROVISIONAL": 0,
    "UNRESOLVED": 1
  },

  "extraction_status_counts": {
    "COMPLETE": 28,
    "INCOMPLETE": 2
  },

  "pass_provenance_summary": {
    "Q_PASS": 30,
    "A_PASS": 30,
    "S_PASS": 29,
    "Q_RETRY": 4,
    "A_RETRY": 1,
    "S_RETRY": 2,
    "RESCUE": 0,
    "DRAIN_Q": 0,
    "OCR_S": 0
  }
}
```

This is the single source of truth for the user's required "chapter
completeness report" item.

---

## 9. Atomic-write contract

Each of the per-chapter files (`questions.jsonl`, `answers.jsonl`,
`solutions.jsonl`, `unresolved_qids.jsonl`, `orphans.jsonl`,
`chapter_completeness.json`, `image_manifest.jsonl`) is written using
the existing `rewrite_questions_file` pattern:

```python
def _atomic_jsonl_write(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)   # atomic on POSIX
```

A crash mid-write leaves either the previous file (untouched) or the new
file (complete). The split writer writes the four data files in
sequence; if the writer dies after file 1 but before file 4, the
consumer sees file 1 = current, file 4 = stale. The completeness.json
file is written **last** as the "this chapter's split is fully on disk"
signal. (No additional locking is required because the existing
`process_pdf` is already single-threaded per chapter and the per-chapter
file path is unique.)

---

## 10. Insertion point in `process_pdf`

The split write happens **after** every existing in-pipeline step and
**before** the existing `build_final_question` / `rewrite_questions_file`
write loop. Specifically:

```python
# process_pdf(), just after the existing rescue/anchorless/phantom block,
# and just before the build_final_question loop:

# NEW: reconcile q_ids and grade every record
reconcile_qids(
    chapter_records, image_files_by_q, qn_source_pages,
    pdf_path, page_files, subject, ch["chapter_no"],
    printed_sol_qns,
)

# NEW: write the three split JSONL files (questions/answers/solutions)
# + unresolved_qids.jsonl + orphans.jsonl + chapter_completeness.json
# + image_manifest.jsonl. Atomically per file.
write_split_outputs(
    chapter_id=chapter_id,
    subject=subject,
    chapter_no=ch["chapter_no"],
    chapter_records=chapter_records,
    image_files_by_q=image_files_by_q,
    qn_source_pages=qn_source_pages,
    orphans=orphans,
    chapter_unresolved_images=chapter_unresolved_images,
    pdf_path=pdf_path,
    page_files=page_files,
)

# EXISTING (unchanged): build_final_question loop, rewrite_questions_file,
# write_chapter_file, state.json save, chapters.json save, etc.
```

The new step runs **after** all the existing checks (orphan recovery,
drain, integrity sweep, targeted retry, rescue, anchorless drop,
phantom drop, critique-and-repair) and **before** the existing
master `data/questions.jsonl` rewrite. This means the existing
`data/questions.jsonl` is **never** the source of truth for the split —
the split is built from the same `chapter_records` the master file is
built from. They are guaranteed consistent.

---

## 11. What does NOT change

- The Q/A/S Gemini call loop. Three calls per window, same prompts,
  same `_batch_meta` / `_figure_map` flow, same carry-forward.
- `chapter_records` shared state. The split layer is **read-only** on
  `chapter_records` for grading; it does not write fields that the
  extraction loop uses.
- All existing guards: run-18 GUARD, section-boundary, carry-expiry,
  integrity sweep, contamination guards, stem conflict handling,
  phantom drop, critique.
- `data/questions.jsonl` and `data/by_chapter/{chapter_id}.jsonl` —
  the existing master + per-chapter files are still written.
- `qbank_validator.py` — unchanged. It still reads `questions.jsonl`
  and derives q_no from `id`.
- `app.py` — unchanged. The split files are read-only outputs that
  the master-data builder consumes; the dashboard continues to read
  `questions.jsonl`.
- The existing `data/orphans.jsonl`, `data/unresolved_images.jsonl`,
  `data/integrity_flags.jsonl`, `data/stem_conflicts.jsonl`,
  `data/still_incomplete_after_retry.jsonl`, `data/export_gate.jsonl` —
  unchanged.

---

## 12. Test plan (PSY-007 first)

Per the user's "do not immediately perform a massive refactor"
instruction, the rollout is:

1. **Static check**: import the module in Python (`python3 -c "import
   qbank_pipeline"`) to ensure no syntax errors. No PDF processed.
2. **Single-chapter dry run**: run the existing test script
   `test_v2_chapter.py` on PSY-007. Confirm:
   - `data/questions.jsonl` and `data/by_chapter/PSY-007.jsonl`
     are byte-identical to a pre-change run.
   - The four new files are produced and well-formed.
3. **Show the user** the first 5 lines of each new file and the
   `chapter_completeness.json` summary before any further rollout.
4. **Run the validator** on the unchanged `questions.jsonl` and confirm
   no new flag kinds appear.
5. **Roll out to all chapters** only after the user signs off on the
   PSY-007 output.

### 12.1 What the PSY-007 test must demonstrate

From the spec, the test must show:

- [ ] `questions.jsonl` with at least one COMPLETE record carrying
      `q_id = "PSY-007-001"` (and ideally `PSY-007-023`).
- [ ] `answers.jsonl` with the same `q_id` for the same source question.
- [ ] `solutions.jsonl` with the same `q_id` for the same source question.
- [ ] An example INCOMPLETE record (e.g. a question with no options)
      preserved with `extraction_status: "INCOMPLETE"` and
      `missing_fields: ["options"]`.
- [ ] At least one OPTION-image preserved with `option_letter` set
      (if the PSY-007 source has any option images; if not, the
      field is `images: []` for all options and the test asserts that
      instead).
- [ ] `unresolved_qids.jsonl` with at least one entry (or empty file
      with comment), demonstrating the failure case.
- [ ] `orphans.jsonl` with at least one entry (or empty file), with
      `reason` field populated.
- [ ] `chapter_completeness.json` with all the required counters.
- [ ] `q_id_anchors` populated on at least one record, showing the
      five evidence types where they exist.
- [ ] The `questions.jsonl` record for any Q23 is **NOT** present in
      the split file (because the existing per-chapter file showed Q23
      was a tricky case); the absence is visible and documented.
- [ ] `data/questions.jsonl` is unchanged (byte-identical to a
      pre-change run of the same chapter).

---

## 13. Open questions for the user

Before I write code:

1. **Path layout**: `data/split/{subject}/{chapter_id}/...` matches the
   spec literally. Confirm or prefer `data/split/{subject}/{chapter_no}/...`?
2. **Image manifest**: should the manifest live in
   `data/split/.../image_manifest.jsonl` (added in this change) or
   be derived only from `data/image_ownership.jsonl`? The latter is
   already in the codebase. I propose BOTH: `image_ownership.jsonl`
   remains the source of truth; the split manifest is a
   chapter-scoped convenience. Confirm.
3. **Atomic order**: I propose writing
   `chapter_completeness.json` LAST as the "split is on disk" signal.
   Confirm or specify a different order.
4. **Field name `q_id_grade` vs `grade`**: spec uses `q_id_grade`.
   I'm using that. Confirm.
5. **`extraction_status` enum**: spec uses `COMPLETE` / `INCOMPLETE`.
   Confirm no other values are needed for v1.
6. **Reconciliation grade for question-only records that the Q-pass
   missed entirely** (Case 2 of the spec): I propose
   `UNRESOLVED` with `reason: "missing_question_for_solution"` and
   the solutions row also dropped to `unresolved_qids.jsonl` (so the
   join does not silently ship a solution without a question). The
   spec is consistent with this. Confirm.

---

*End of design. Awaiting your sign-off before any code is written.*
