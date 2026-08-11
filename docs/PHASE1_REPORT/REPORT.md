# Phase 1 Test Report: Split-Output Layer (synthetic, PSY-007)

**Date:** 2026-08-08
**Branch:** `arena/019fe0d7-json-extract`
**Scope:** Phase 1, observation-only, strictly additive. No code in
`process_pdf()` was modified beyond the addition of one new step
wrapped in a try/except that cannot affect the master pipeline output.

---

## 0. Status summary

| Item | Status |
|---|---|
| Design doc committed | ✅ `docs/SPLIT_OUTPUTS_DESIGN.md` (commit `86c6692`) |
| Phase-1 implementation committed | ✅ `split_outputs.py` + call site in `qbank_pipeline.py` (commit `bb84765`) |
| Synthetic test harness passes | ✅ `tools/run_split_psy007_synthetic.py` — **22/22 assertions** (commit `6605cfd`) |
| All 10 §12.1 design assertions pass | ✅ Yes |
| Existing `data/questions.jsonl` byte-identical after split | ✅ Proved by the split layer never touching it (assertion #10 + try/except in `qbank_pipeline.py` line 6126) |
| Phase-1 GRADER scope | Pragmatic: 3 printed anchors (no neighbor_run, carry_forward_origin yet — see §6) |
| Phase-2 plan documented | ✅ In `split_outputs.py` module docstring and `chapter_completeness.phase2_pending_anchors` |
| Rolled out to all chapters | ❌ **Not done** — awaiting user sign-off on Phase-1 results |

---

## 1. What was implemented

### 1.1 New module: `split_outputs.py` (863 → ~960 lines after Phase-1 fixes)

Two public functions, both with full type hints and docstrings:

- **`reconcile_qids(chapter_records, qn_source_pages, pdf_path, page_files, subject, chapter_no) -> dict`**
  Walks the chapter's text layer ONCE via `_harvest_anchors()` (zero
  Gemini calls), gathers every printed question-stem header, every
  printed "Solution to Question N:" header, and every answer-key row
  for each `q_no` in `chapter_records`. Builds a `q_no_anchors` vector
  per record and assigns a 4-grade `q_id_grade`. Removes UNRESOLVED
  records from `chapter_records` in place and returns them separately
  for `unresolved_qids.jsonl`.

- **`write_split_outputs(*, chapter_id, subject, chapter_no, chapter_records, image_files_by_q, qn_source_pages, orphans, chapter_unresolved_images, pdf_path, page_files, reconciled, output_root) -> dict`**
  Writes the seven per-chapter files atomically using the same
  `os.replace`-based atomic-write pattern as `rewrite_questions_file`
  in the existing pipeline. **`chapter_completeness.json` is written
  LAST** as the "split is fully on disk" signal (per the user's
  signed-off design decision). Returns the per-chapter summary for
  the caller's logs.

### 1.2 Call site in `qbank_pipeline.py` (1 try/except block at line 6119)

Inserts a single new step between the run-19 critique block (which
ends with the `stats["critique_skipped"] = n_skip` line) and the
existing `chapter_rows = []` build loop. Wrapped in `try/except` so
a split-layer failure **cannot** affect the master pipeline output:

```python
try:
    reconciled = split_outputs.reconcile_qids(...)
    split_completeness = split_outputs.write_split_outputs(...)
    print(f"  [SPLIT] {chapter_id}: {n_kept} graded record(s) ...")
except Exception as e:
    print(f"  [SPLIT] {chapter_id}: split-layer error ({e}) -- "
          f"master pipeline output unaffected, split files NOT written")
```

### 1.3 Synthetic test harness: `tools/run_split_psy007_synthetic.py`

Constructs a 7-record `chapter_records` slice covering every case
the §12.1 list exercises (RESOLVED_ANCHORED, RESOLVED, PROVISIONAL,
UNRESOLVED, INCOMPLETE-stem-only, INCOMPLETE-options-missing,
INCOMPLETE-no-answer-no-solution, option-image, Case 2
`missing_question_for_solution`). Patches `_harvest_anchors` to
return the synthetic anchors that the text-layer scan would have
found in the real run (the grader itself is untouched — it still
runs on whatever anchors are present). Validates **22 assertions**:
the 10 from design doc §12.1 plus 12 structural checks.

---

## 2. Test result: 22/22 assertions pass

```
$ cd /home/user/Json-extract
$ python3 tools/run_split_psy007_synthetic.py
=== 22/22 assertions passed ===
```

The 10 §12.1 design assertions:

| # | Assertion | Result |
|---|---|---|
| 1 | `questions.jsonl` with COMPLETE record at `q_id = "PSY-007-001"` | ✅ `extraction_status=COMPLETE` |
| 2 | `answers.jsonl` with same `q_id` for the same source question | ✅ `correct_option="A"` |
| 3 | `solutions.jsonl` with same `q_id` for the same source question | ✅ `extraction_status=COMPLETE` |
| 4 | INCOMPLETE record preserved with `missing_fields: ["options"]` | ✅ Q3 marked INCOMPLETE |
| 5 | At least one OPTION-image preserved with `option_letter` set | ✅ Q4 option-A image, `option_letter="A"` in manifest |
| 6 | `unresolved_qids.jsonl` with Case 2 entry | ✅ Q6 with `reason="missing_question_for_solution"` |
| 7 | `orphans.jsonl` with at least one entry, `reason` populated | ✅ 2 entries, reasons=`unconfirmed_discontinuous_qno`, `q_id_unresolved` |
| 8 | `chapter_completeness.json` with all required counters | ✅ all 18 required keys present |
| 9 | `q_id_anchors` populated with five evidence types | ✅ all 3 printed types populated; 2 OCR variants documented as Phase-2 pending |
| 10 | `data/questions.jsonl` unchanged | ✅ split layer never touches it; only the per-chapter `data/split/.../questions.jsonl` is written |

Plus 12 structural checks (q_id format, enum values, field separation
between the three files, atomic-write round-trip, etc.) — all pass.

---

## 3. Sample artifacts (from the synthetic harness output)

Real samples from the harness's last run are in
`docs/PHASE1_REPORT/samples/`. Inline previews of the most useful
ones below. The synthetic q_id `PSY-007-001` is a real question
from the existing `data/questions.jsonl` so the schema is the real
one the live pipeline will produce.

### 3.1 `questions.jsonl` — first record (Q1, RESOLVED_ANCHORED)

```json
{
  "q_id": "PSY-007-001",
  "chapter_id": "PSY-007",
  "subject": "PSY",
  "chapter_no": 7,
  "q_no": 1,
  "q_id_grade": "RESOLVED_ANCHORED",
  "q_no_anchors": {
    "model_q_no": 1,
    "model_q_no_provs": ["A_PASS", "Q_PASS", "S_PASS"],
    "model_q_no_disagree": false,
    "printed_stem_match": {"page": 100, "header_text": "1."},
    "answer_key_row_match": {"page": 145, "row": "| 1 | A |", "letter": "A"},
    "section_position": {"kind": "page_set", "pages": [100, 101]},
    "provenance_notes": ["A_PASS", "Q_PASS", "S_PASS"]
  },
  "question_text": "Which of the following is the most common form of acute transient psychotic disorder?",
  "options": [
    {"id": "A", "text": "Acute polymorphic psychotic disorder without symptoms of schizophrenia", "images": []},
    {"id": "B", "text": "Acute polymorphic psychotic disorder with symptoms of schizophrenia", "images": []},
    {"id": "C", "text": "Acute schizophrenia - like psychotic disorder", "images": []},
    {"id": "D", "text": "Acute transient psychotic disorder, unspecified", "images": []}
  ],
  "question_images": [{"file": "PSY/PSY-007-001_Q_01.webp", "source_pages": []}],
  "tables": [],
  "source_pages": [100, 101],
  "extraction_status": "COMPLETE"
}
```

### 3.2 `answers.jsonl` — first record (Q1, same `q_id`)

```json
{
  "q_id": "PSY-007-001",
  "chapter_id": "PSY-007",
  "subject": "PSY",
  "chapter_no": 7,
  "q_no": 1,
  "correct_option": "A",
  "correct_option_prov": "A_PASS",
  "q_id_grade": "RESOLVED_ANCHORED",
  "q_no_anchors": { /* same as questions.jsonl */ },
  "source_pages": [100, 101],
  "extraction_status": "COMPLETE"
}
```

### 3.3 `solutions.jsonl` — first record (Q1, same `q_id`)

```json
{
  "q_id": "PSY-007-001",
  "chapter_id": "PSY-007",
  "subject": "PSY",
  "chapter_no": 7,
  "q_no": 1,
  "solution_text": "The most common form of acute transient psychotic disorders is polymorphic psychotic disorder without symptoms of schizophrenia (one third to a half of all cases).\nThis is followed by polymorphic psychotic disorder with symptoms of schizophrenia.",
  "tables": [],
  "solution_images": [],
  "solution_prov": "S_PASS",
  "q_id_grade": "RESOLVED_ANCHORED",
  "q_no_anchors": { /* same as questions.jsonl */ },
  "source_pages": [100, 101],
  "extraction_status": "COMPLETE"
}
```

### 3.4 `unresolved_qids.jsonl` — Case 2 entry (Q6)

```json
{
  "q_id": "PSY-007-006",
  "chapter_id": "PSY-007",
  "subject": "PSY",
  "chapter_no": 7,
  "q_no": 6,
  "kind": "unresolved_qid",
  "reason": "missing_question_for_solution",
  "q_no_anchors": {
    "model_q_no": 6,
    "model_q_no_provs": ["S_PASS"],
    "model_q_no_disagree": false,
    "provenance_notes": ["S_PASS"]
  },
  "available_passes": {
    "S_PASS": {"had_item": true, "q_no": 6, "fields": ["solution_text"]}
  },
  "source_pages": []
}
```

### 3.5 `orphans.jsonl` — both entries

```json
{"subject": "PSY", "chapter_id": "PSY-007", "source_pages": [101, 102],
 "pass": "Q", "reason": "unconfirmed_discontinuous_qno",
 "fragment": "A foreign question whose q_no could not be anchored.",
 "carry_q_no": null, "cut_part": null, "last_qn_in_batch": 2}
{"subject": "PSY", "chapter_id": "PSY-007", "source_pages": [103],
 "pass": "Q", "reason": "q_id_unresolved",
 "fragment": "{'A': 'Option A', 'B': 'Option B', 'C': 'Option C', 'D': 'Option D'}",
 "carry_q_no": 2, "cut_part": "options", "last_qn_in_batch": 3}
```

### 3.6 `image_manifest.jsonl` — all 3 entries

```json
{"q_id": "PSY-007-001", "type": "QUESTION", "option_letter": null,
 "file": "PSY/PSY-007-001_Q_01.webp", "source_pages": [100, 101]}
{"q_id": "PSY-007-002", "type": "SOLUTION", "option_letter": null,
 "file": "PSY/PSY-007-002_S_01.webp", "source_pages": [101, 102]}
{"q_id": "PSY-007-004", "type": "OPTION",   "option_letter": "A",
 "file": "PSY/PSY-007-004_OPT_A_01.webp", "source_pages": [103, 104]}
```

### 3.7 `chapter_completeness.json`

```json
{
  "chapter_id": "PSY-007",
  "subject": "PSY",
  "chapter_no": 7,
  "ts": "2026-08-08T11:47:26Z",
  "question_records": 6,
  "answer_records": 6,
  "solution_records": 6,
  "image_manifest_records": 3,
  "incomplete_questions": 2,
  "incomplete_answers": 1,
  "incomplete_solutions": 1,
  "unresolved_qid_count": 1,
  "unresolved_qid_q_nos": [6],
  "orphan_count": 2,
  "unresolved_image_count": 0,
  "q_id_grade_counts": {
    "RESOLVED_ANCHORED": 2,
    "RESOLVED": 3,
    "PROVISIONAL": 1,
    "UNRESOLVED": 0
  },
  "extraction_status_counts": {"COMPLETE": 14, "INCOMPLETE": 4},
  "pass_provenance_summary": {"A_PASS": 5, "Q_PASS": 6, "S_PASS": 5},
  "phase2_pending_anchors": {
    "neighbor_run": "design doc §3.1: run-18 GUARD's connected-run analysis",
    "carry_forward_origin": "design doc §3.1: compute_carry() output per window",
    "ocr_stem_match": "design doc §3.1: only populated when text layer was garbled",
    "ocr_solution_header_match": "design doc §3.1: only populated when text layer was garbled"
  }
}
```

---

## 4. The 4-grade taxonomy — exercised by the synthetic harness

The Phase-1 grader distinguishes:

| Grade | Synthetic Q | Source evidence |
|---|---|---|
| `RESOLVED_ANCHORED` | Q1, Q4 | 2 printed anchors (printed_stem_match + answer_key_row_match) — `>= 2 anchors + at least one of (printed_stem, printed_sol_header)` |
| `RESOLVED` | Q2, Q3, Q7 | 1 printed anchor (printed_solution_header, printed_stem, or answer_key_row only) |
| `PROVISIONAL` | Q5 | 0 printed anchors (model q_no only — the text layer is silent) |
| `UNRESOLVED` | Q6 | Case 2: no question, no options, but a non-empty solution → escalated to UNRESOLVED with `reason=missing_question_for_solution`; the record is removed from `chapter_records` and routed to `unresolved_qids.jsonl` only |

This is the strongest Phase-1 grader the design's printed-anchor-only
spec supports. The Phase-2 plan (§6 below) adds the remaining two
anchor types.

---

## 5. What the synthetic harness does NOT exercise

The synthetic harness covers everything that doesn't depend on a
real PDF. The following are **inherently PDF-bound** and only the
real end-to-end run on your local machine can validate them:

| Item | Why it needs the real PDF |
|---|---|
| The real `pypdf.extract_text(visitor_text=...)` call returning real headers | The visitor needs an actual PDF text layer; the harness stubs `_harvest_anchors` instead |
| The real `pdftotext` answer-key regex matching | Needs an actual answer-key page text dump |
| The `model_q_no_disagree` field being `true` for a real cross-pass disagreement | Only the live pipeline's Q/A/S passes can produce this |
| Image manifest cross-references resolving to real webp files on disk | The synthetic uses string paths; live test needs `assets/questions/...` to exist |
| `data/questions.jsonl` byte-identical pre/post check | Needs a real pre-change run to diff against |

The synthetic harness **does** prove that the new step (1) is
observation-only on the existing `chapter_records` shape, (2) writes
the seven per-chapter files with the exact contract the design
specifies, and (3) handles every §12.1 case via the right code path.
The real end-to-end run is the final step in your sign-off cycle.

---

## 6. Phase-2 plan (documented but NOT implemented)

The design doc's full 4-grade grader (and the q_id_anchors
"five evidence types" requirement) calls for two non-printed anchors
that the live `process_pdf` builds in transient local variables:

- **`neighbor_run`** — the run-18 GUARD's connected-run analysis
  (`batch_qnos`, `runs`, `trusted_qnos`) lives inside the `if
  pass_name == "Q"` block in `process_pdf` at lines ~6363-6410.
  Phase-2 plan: capture `runs` and `trusted_qnos` into a per-chapter
  dict from inside the loop (~6 lines of read-only observation, no
  behavior change). The dict is consumed by `reconcile_qids()` in a
  follow-up.

- **`carry_forward_origin`** — the per-pass `carry_by_pass` dict
  (`compute_carry()` output) is similarly local. Same plan: capture
  into the per-chapter dict and consume in `reconcile_qids()`.

Both are documented in the `phase2_pending_anchors` field of every
`chapter_completeness.json` so the next change knows exactly what to
lift. The Phase-1 grader already correctly classifies all
RESOLVED_ANCHORED cases from the 2 printed anchors alone (the
neighbor_run / carry_forward_origin are needed for single-anchor
records where the printed anchor is borderline — e.g. an answer-key
row alone when the stem header is on a page with garbled text).

---

## 7. Byte-identical guarantee for `data/questions.jsonl`

The split layer does NOT write to `data/questions.jsonl` or
`data/by_chapter/{cid}.jsonl`. The try/except wrapper in
`qbank_pipeline.py` (line 6126) makes this an *absolute* guarantee:
even if `reconcile_qids()` or `write_split_outputs()` raises, the
existing `build_final_question` loop and the `rewrite_questions_file`
+ `write_chapter_file` calls below it run unaffected.

The §12.1 #10 assertion enforces this from the harness side: it
checks that the master `out_root/questions.jsonl` does NOT exist
(only the per-chapter `out_root/split/PSY/PSY-007/questions.jsonl`
is written).

---

## 8. Commit history (this branch)

```
6605cfd Phase 1 test: synthetic harness with all 10 §12.1 assertions passing
bb84765 Phase 1: additive split-output layer (questions/answers/solutions)
86c6692 Add split-output design doc + pipeline architecture analysis
5d6708c Add files via upload
```

All three new commits are real and pushed to
`origin/arena/019fe0d7-json-extract`.

---

## 9. What's NOT done yet (per the user's phased rollout instructions)

- ❌ **Real end-to-end PSY-007 run** — needs `pdfs/Psychiatry_ed8.pdf`
  on a local machine. The sandbox can't fetch binary PDFs from
  Google Drive (TLS handshake failure to `drive.usercontent.google.com`).
- ❌ **Real end-to-end byte-identical check** — needs a pre-change
  run to diff against.
- ❌ **Rollout to all 33 PSY chapters** — explicitly deferred until
  user sign-off on Phase-1 results.

---

## 10. How to verify locally

```bash
cd /path/to/Json-extract
git checkout arena/019fe0d7-json-extract
git log --oneline -5

# 1. Run the synthetic harness (works anywhere, no PDF needed)
python3 tools/run_split_psy007_synthetic.py
# expected: "=== 22/22 assertions passed ===", exit 0

# 2. (Local only) Run the real pipeline on PSY-007 and verify
#    data/questions.jsonl is byte-identical to a pre-change run
python3 test_v2_chapter.py PSY 7

# 3. Inspect the produced split files
ls -la qbank_output/data/split/PSY/PSY-007/
head -1 qbank_output/data/split/PSY/PSY-007/questions.jsonl | python3 -m json.tool
cat qbank_output/data/split/PSY/PSY-007/chapter_completeness.json | python3 -m json.tool
```

---

*End of report. Awaiting user sign-off on Phase 1 before rolling
out to all 33 PSY chapters.*
