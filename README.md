# QBank Extractor

Scanned MCQ question banks (PDF) → clean structured JSONL for a DB converter.
Built for the MARROW ED8 medical series: garbled text layers, figures printed
above their own stems, solutions in a separate zone from the questions.

**Design rule: never guess, never auto-correct.** Every field is either proven
from the page or flagged for a human. An item that cannot be proven is
discarded and reported, not shipped as a plausible guess — the output runs to
18,000+ questions with no capacity to review it.

## Pipeline

```
TOC                    chapter page ranges, from the book's own contents table
 └─ zones              Q pages / answer-key page / solution pages
     └─ header index   render + OCR; finds "Question N:" and "Solution to
                       Question N:" bands
         └─ crops      one crop per block, header → next header, stitched
                       across pages as labelled "part i/N" composites
             ├─ text   PDF text layer first; Gemini for anything not CLEAN
             ├─ key    answer-key table, read twice, escalates on disagreement
             └─ images every figure owned by the crop interval it sits inside
```

Text and figures are attributed by the **same** geometry. A figure whose
(page, y) lies inside a block's interval belongs to that block — including the
common case where the figure is printed above its stem and the heading sits at
the bottom of the previous page.

## Running it

```bash
apt-get install -y poppler-utils tesseract-ocr tzdata
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Both `poppler` and `tesseract` are required, not optional.

Then `python3 app.py` and use the dashboard, or run a book directly through
`qbank_pipeline.main()`. Gemini keys come from `GEMINI_API_KEYS` (or
`GEMINI_API_KEY_1..N`) — each key needs its **own Google project**, since
quota is enforced per project, not per key.

`QBANK_CROP_BATCH` (default 3) controls how many crops share a Gemini call.
Only text-only, single-page question crops are ever batched; solutions and
anything with a figure go one-per-call so a neighbour cannot bleed into them.

`QBANK_CROP_DUMP=/some/dir` writes every crop image the model is sent. That is
the only way to tell a wrong block boundary from a model that invented text.

## Output

`FORMAT.md` is the converter contract — it is the only file the downstream DB
project reads, and a copy travels inside every `final_export.zip`.

`REVIEW_LAYER.md` describes the human review layer: the flag queue, in-place
edits, and the hard-locked final export.

## Verification

```bash
.venv/bin/python -m pytest -q
```

Expect 503 passed / 0 failed / 17 skipped with tesseract installed (502 / 1 /
17 without — the one failure needs tesseract). Two tests are `skipUnless`
guarded because they need developer-machine fixtures.

`HANDOFF_PROMPT.md` is the current engineering brief: architecture, the
verified baseline, what is already done, and the open tasks in priority order.
Read it before changing anything.
