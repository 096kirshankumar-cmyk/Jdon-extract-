# final_export.zip — Converter Input Contract (FROZEN)

This is the **only** input your DB-converter project reads. The pipeline
guarantees this shape; it never changes without a major-version note here.
Everything is post-review (the zip only builds when the review queue is
clear — see `REVIEW_RECEIPT.json`).

## Tree

```
final_export.zip
├── REVIEW_RECEIPT.json            # build receipt (counts, gate proof)
├── FORMAT.md                      # this file
├── split/<SUBJ>/<SUBJ>-<NNN>/     # one dir per chapter, e.g. split/OBG/OBG-003/
│   ├── questions.jsonl            # one row per question (see fields below)
│   ├── answers.jsonl
│   ├── solutions.jsonl
│   ├── image_manifest.jsonl
│   └── chapter_completeness.json
├── subjects/<SUBJ>/chapters.json  # chapter list/order/titles per subject
└── assets/questions/<SUBJ>/*.webp # ONLY the images referenced by manifests
```

## Field contract (stable)

### questions.jsonl — one row = one question
| field | type | note |
|---|---|---|
| q_id | str | `OBG-003-014` — globally unique (subject-chapter-qno) |
| subject | str | book code, e.g. `OBG` |
| chapter_no | int | |
| q_no | int | printed question number |
| question_text | str | full verbatim stem |
| options | list | `[{"id":"A","text":"...","images":[{"file":..., "source_pages":[int]}]}, ...]` — always A–D |
| question_images | list | same image-ref shape |
| tables | list | `[{"type": str, "markdown": str}]` |
| extraction_status | str | `COMPLETE` / `INCOMPLETE` |

### answers.jsonl
| q_id | q_no | correct_option (`"A".."D"`) | extraction_status |

### solutions.jsonl
| q_id | q_no | solution_text | tables | solution_images | extraction_status |

### image_manifest.jsonl — image → owner index (one row per reference)
| q_id | type (`QUESTION`/`SOLUTION`/`OPTION`) | option_letter | file | source_pages | extraction_page |

Notes the converter can rely on:
- A shared (multi-draw) figure appears as **multiple rows, same file** — copy the
  bytes once.
- Image `file` is relative to `assets/questions/`.
- All text is verbatim from the book unless the row was human-edited
  (`qa_human_edit: true` in master rows; not repeated in split).

## What is NOT in this zip (on purpose)
Ledgers, flags, digests, orphans, state — those are the *workshop*. The zip is
the *delivery package*. If you need review evidence, use MASTER_REVIEW.zip
from the dashboard instead.
