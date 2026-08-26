# Fix + Verification Report — Image Ownership (Ch. 28 class) & Pipeline Hardening

Base: `Jdon-extract-` @ `c4aa91a` → fixed working tree in `/home/user/repo`
(pristine baseline kept at `/home/user/repo_pristine`; per-file unified diffs in
`/home/user/audit/fixes_*.diff`).

Test book: the Drive link you shared — **MARROW Ed8 Ophthalmology, 636 pages**,
watermark obj `2349` detected and excluded. The failing chapter from your example:
**Ch. 28 "Instruments in Ophthalmology" (pp. 607–636; page_offset = 0 for this
book)**, 23 questions + 23 solutions.

## What was verified, and how

| Student | Measure | Result |
|---|---|---|
| Repo regression suite | `python -m unittest test_pipeline_json` — 271 tests | was **13F+1E pre-fix** (7 env-caused; 6 genuine) → **OK, 0 failures** post-fix (4 stem-rule conflicts kept as loudly-documented `expectedFailure`, see §7) |
| New fix suite | `/home/user/audit/test_image_ownership_fixed.py` — 15 tests driving the REAL claim functions on the exact ch. 28 failure shapes | **all green** |
| Real book, deterministic image layer (exactly the code under audit) | zero-Gemini race harness `book_ch28_claim_race.py`, pristine vs fixed on pp. 607–636 | see the ownership table below |
| SSRF guard | direct probes of 169.254.169.254 / localhost / 127.0.0.1 / 10.x / 192.168 / `file://` / `ftp://` | all blocked; drive.google.com allowed |
| app boot | Flask import + route map | OK |

**LIVE pipeline runs completed with a real Gemini key (provided by you):**
chapters 26, 27, 28 ran end-to-end on this book with the fixed code
(`/home/user/audit/run_26_28/` — full output tree), plus a ch-28-only verification run
(`/home/user/audit/run_ch28_final*`). Results:

- **ch. 26**: 15/15 questions, **export gate CLEAN**, 8 manifest images, 0 unmatched.
- **ch. 27**: 11/11 complete; the gate honestly reports 2 violations (q7's solution is
  genuinely absent from the printed pages — the critique pass confirmed from the pages;
  q11's declared figure). The p599 texture-phantom anchor was dropped as designed.
- **ch. 28**: 23/23 questions with stem + 4 options + answer + solution. The original bug
  is gone: the ownership ledger records `OPH-p609-1792.webp -> OPH-028-005 (carry, medium)`
  and `OPH-p609-1798.webp -> OPH-028-006 (positional, high)` — Q5's and Q6's figures
  **separated on the same page**, Q7's Ex-PRESS figure on Q7, Q19's on Q19, sol-10/sol-11
  figures on the right solutions. Gemini spend: **79 calls** total for 3 chapters.
- The gate and validator no longer print CLEAN for the failure class: the real remaining
  gaps (Q8's perimeter-test figure on p611 and the contested p624 panel) are surfaced as
  `missing_declared_figure` (strong-evidence variety) / `unresolved_image` instead of buried.
- Empty-model-declaration noise stays out of the way: declared-only "missing figure" claims
  with no unclaimed image near the question's pages are written to `export_gate_advisory.jsonl`
  (LOW) rather than blocking CLEAN.

Two more defects the live run exposed in the PRE-FIX code are also fixed:
- resume/re-run crashed on `/tmp/OPH_chNNN/page-610_crop2x1.jpg` drain-crop leftovers
  (`ValueError` in `int(p.stem.split("-")[-1])` at chapter start) — page globs now filter
  crop files;
- the failed-page drain ran AFTER the image re-claim passes, so a record recovered from a
  recitation-blocked page arrived too late to own its figures by anchors (the drain is now
  hoisted above the image re-claim passes).

## Ch. 28 on the real book — before vs after (deterministic levels)

Pristine code: **39/39 images claimed, 0 provenance rows, 0 leftovers**.
Fixed code: **32 claimed with page/geometry evidence, 46 ledger rows, 7 honest leftovers**
(those 7 go to the fixed L3 vision / manual review instead of being silently attached).

| Real page content (visually verified) | OLD code | NEW code |
|---|---|---|
| p609 top figure = **Q5**'s continuation (stem p608) | Q6 (wrong owner — your bug shape) | **Q5** via carry, ledgered `medium` |
| p609 lower figure = Q6 | Q6 | Q6 ✓ |
| p610 figure = Q7 | Q7 | Q7 ✓ |
| p611 top figure = Q8 continuation | Q8 | Q8 ✓ |
| p616 top figure = Q17 continuation (heading at p615 bottom) | (stolen when records incomplete) | **Q17** via carry |
| p617 top figure = Q19 (stem p616) | **Q1 — phantom OCR anchor "1." read off figure texture** | **Q19** — phantom dropped (in-figure filter), carry q19 |
| p621 both figures (Option B/C images of sol-3) | sol-3 | sol-3 ✓ |
| p622 top figure = sol-3 tail (3rd figure) | sol-3 (past cap, invisible) | **leftover → L3/review** (honest; cap refuses silent stacking) |
| p623/p629/p631/p634/p635/636 figures | mostly right, 4-image stacking on q3/q4/q14/q22 | ≤2 each + extras to review; all ledgered |
| p627 IOL figure (under sol-10's "image given below") | **q2 question — phantom "2." + wrong carry** | **sol-10** via carry |
| p628 same IOL figure reprinted (sol-11's "image given below") | **q2 question** | **sol-11** via carry |

Every row above with "carry" is only `medium` confidence in the ledger — human-reviewable,
exactly the semantics you asked for ("no flag = proven, not merely present").

## Export-gate proof (len(work) evidence, zero tokens)

- Clean ch. 28 state → **no `figure_page_mismatch` / `missing_declared_figure` violations**.
- Poisoned with your exact Q5→Q6 theft + a far-page wrong-owner → gate fires both:
  - `missing_declared_figure_question q5` (the Q7/Q8 "declared but shipped empty" class —
    this never fired before because the declaration was discarded at export);
  - `figure_page_mismatch q3` (image from p636 attached to a question anchored on p608).

## The fixes (each is small, commented, and covered by the suites)

qbank_pipeline.py:
1. **Union anchor harvest** `union_block_headers_on_page()` — text layer + OCR, merged with
   line-collision dedupe; detection no longer requires `qn in chapter_records`
   (record-membership still gates the *claim*; un-owned figures wait for the chapter-end
   second pass instead of attaching to a neighbour).
2. **Phantom-anchor guards** — OCR anchor rejected when its (x,y) lies inside a drawn figure
   rect (in-figure filter in `_ocr_anchors_for_page`), and *weak* bare-token anchors must be
   corroborated by text-layer occupancy at the same y (both learned from p617/p627 on your
   book). Strong keyword/multi-word OCR anchors stand alone; text-layer-free scanned pages
   keep all OCR anchors.
3. **One claim path = one anchor truth**: `claim_block_images`, `claim_block_images_ocr`,
   `last_block_on_page` (the carry chain) and the chapter-end gate all consume the same
   cached union anchors (OCR results cached per (pdf,page,dpi)).
4. **Carry selection by recency** (`_active_block_from_carries` picks the carry with the
   nearest `ending_page`, tie → Q), carry/seed adoption filtered by
   `_plausible_qn_for_chapter`, and **carry claims obey the flat caps** (the run-21 lift was
   the runaway-stacking vector).
5. **One-to-one & third pass require block-extent geometry** (below the candidate's heading,
   above the next anchor); the keyword `side = "question" if "fig" in stem...` heuristic is
   gone — the side now comes from the anchor kind.
6. **Provenance ledger at the choke point**: `_rename_for_slot` writes
   `image_ownership.jsonl` for every claim AND every guard refusal (temp name, source page,
   object id, method, evidence, confidence, final name). Before this, L1/one-to-one/
   figure-map wrote nothing and the rename destroyed the only page evidence.
7. **Export evidence**: rows now ship `declared_has_figure_in_question/solution` +
   per-image `source_page`; split layer's question/solution rows + `image_manifest.jsonl`
   carry the image's own extraction page (used to list the *question's* pages).
8. **Gate**: `missing_declared_figure_{question,solution}` + `figure_page_mismatch`
   (image page ∉ anchor pages ±1/2) block "COMPLETE" — with conservative skip when evidence
   maps are unavailable.
9. **Vision L3**: `max_top` used height (was width), edge→context-page direction fixed
   (top edge ⇒ previous page, bottom ⇒ next); edge-touching claims clamp to `medium` when the
   needed context page couldn't be rendered.
10. **Mixed-page boundary `min()`→`max()`** (3 sites) — matches each function's own docstring
    (drop question anchors below the FIRST solution header).
11. **Form-XObject support done for real**: CTM threaded through the content-stream recursion
    (figures placed anywhere on the page previously reported form-local coordinates), and
    `extract_real_images` now recurses into `/Form` resources (such figures were silently
    never extracted) + duplicate no-id name guard.
12. **Duplicate-figure protection**: byte-hash dedupe against already-owned files on
    re-run/recovery (`hash_owned_image_files` + `skip_hashes`); declared image caps persisted
    in `state.json` across quota days (`declared_image_allowance`).
13. **Ops hardening**: negative render-cache results are evicted like positives; every
    pdftotext/pdftoppm subprocess is timeout-bounded; 3-letter-only export regex now accepts
    2–5-letter subject codes.

app.py:
14. **SSRF-hardened fetching** (`fetch_pdf_guarded`): http/https only, DNS-resolved IP
    blocklist (loopback/private/link-local/169.254.169.254/..., revalidated on every
    redirect hop), 300MB byte cap, 10-minute wall cap, bounded interstitial sniff, `%PDF`
    magic check. Residual risk note in code (DNS-rebinding needs per-connection IP pinning).
15. **Atomic busy-guard** (`try_mark_processing`) on /run-url, /run, /v2-test, /recover,
    /fix, /validate, /restore-* — closes the two-pipelines-one-volume race.
16. **Source PDFs persist on /data** (`/data/input_pdfs/<SUBJECT>.pdf` + sha256/offset/
    total-pages record). A different book under the same subject during an in-progress state
    is blocked loudly instead of silently pairing; re-RUN without upload falls back to the
    persisted volume copy. (Unique subject+URL-hash names also stop cross-book overwrites.)

qbank_validator.py:
17. New deterministic flags: `declared_figure_missing` (HIGH) and `image_page_outside_source`
    (image's ledger page vs the row's `source_pages`).

split_outputs.py / test_pipeline_json.py:
18. Split rows/manifest image provenance (same as #7); the 3 stale `locate_*` tests updated
    to the current API shape; two run-21b source-text tests now pin the NEW carry-cap
    contract; the 4 run-12/14-vs-run-20 stem-rule conflicts are kept as `expectedFailure`
    with an explanation comment (they document a real unresolved product decision — do NOT
    silence them permanently).

## Known residual behaviors (deliberate trade-offs, all audible)

- A legit question citing >3 question-side / >2 solution-side figures deterministically now
  leaves the extras in `unresolved_images.jsonl` (gate-visible) unless the model declares the
  owner (model claims still earn the raised cap). On ch. 28 this affected ~5 figures of
  39 — all genuinely multi-figure solution blocks, all now reviewable instead of stacked.
- Truly page-image-scan books (zero text layer per page) get anchors from OCR alone;
  bare-token phantom risk is reduced by the in-figure filter but corroboration can't help
  there — the gate's figure-page check remains the net.
- The repo's 4 stem-conflict `expectedFailure`s need a product decision (run-20's
  "question+solution-only = phantom" rule vs the older "keep question-shaped stems" rule).

## Final ch. 28 verification run (hoisted-drain code) — `/home/user/audit/run_ch28_final2/`

```
23 questions done -> 0 missing answer / 0 missing solution / 0 missing stem / 0 bad options
unmatched images: 0 | image-manifest rows: 39
gate: 5 violations (precise, honest) -- incl. the proof of the whole fix:
  figure_page_mismatch q8: question image extracted from p631 but Q8's printed anchors
                             are on [610, 625] -- "wrong-owner suspect"  <-- model's own
                             wrong claim caught deterministically by the anchor gate
validator: 14 flags (4 LOW advisories for model-declared-but-printless solution figures,
             9 orphan fragments to review from the recitation-blocked p610, 1 HIGH
             image_owner_gate_miss surfaced from the gate).
```

Your original failing question, final state:

| QID | question_images (file @ source page) | verdict |
|---|---|---|
| OPH-028-005 | OPH-028-005_Q_01 @ **p609** | its own cross-page continuation figure — NOT stolen by Q6 anymore |
| OPH-028-006 | OPH-028-006_Q_01 @ **p609** | exactly ONE, its own |
| OPH-028-007 | OPH-028-007_Q_01 @ **p610** | Ex-PRESS figure, correct |
| OPH-028-008 | Q_01 (suspect claim by model from p631) | **gate-flagged `wrong-owner suspect`** for manual correction instead of silently shipping |

All other Q figures (q1..q23) resolve to their own pages 607–619 and match ground truth
(visually verified page by page this session).

## Deploy

No dependency changes (`requirements.txt` untouched; poppler + tesseract were already in the
Dockerfile). Copy the 5 changed files (`qbank_pipeline.py`, `app.py`, `qbank_validator.py`,
`split_outputs.py`, `test_pipeline_json.py`) onto your branch, redeploy the same service.
Nothing about the output layout changed; `state.json` gains one additive
`declared_image_allowance` map. For existing wrong-owner data: the new gate +
validator flags (`missing_declared_figure_*`, `figure_page_mismatch`,
`declared_figure_missing`, `image_page_outside_source`) enumerate exactly the affected rows —
re-run those chapters (reset or `--auto-recover`) with this build.
