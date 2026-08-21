# EXTRACTION V2 — boundary-phased engine (2026-08-22 cutover)

## Kya badla (simple words)

**Purana method poori tarah DELETE kar diya gaya hai.** Ab sirf EK extraction
method hai: **boundary-phased engine** (`boundary_phased.py`). Purana
multi-pass system — `process_pdf()`, section windows, carry-forward merging,
targeted-retry / rescue / critique passes, `--recover` / `--auto-recover`
healers, vision-based image attribution (`claim_figure_map_images`,
`full_page_vision_ownership`) — sab code se hata diya. `qbank_pipeline.py`
9924 se ghis kar ~3850 lines reh gayi: ab wo sirf **shared infrastructure**
hai (state/quota, TOC page-ranges, PDF text/OCR anchors, watermark detection,
deterministic image claiming + ownership ledger, export gate, final-row/split
writers) — koi doosra extraction "tareeka" nahi.

## Naya method ka flow (Steps 0-8, per chapter)

0. **Boundary detect** — model zones karta hai, phir **printed-zone
   validation** (ZERO tokens): text layer ke asli printed "Question N:" /
   "Solution to Question N:" headers + answer-key grid probe (≥6 sequential
   `N → letter` rows) model ke zones ko verify/override karta hai. Live
   proof — OBG ch3 ko model ne clean 3-block padha jabki wo interleaved
   tha; printed evidence ne zones correct kiye (model Q48-61/A62/S62-64 →
   asli Q48-53/A52/S52-65). Scanned book (text layer empty) → model zones.
1. **Question phase** — sirf question zone ke pages; sirf stem + options.
   Answer/explanation is phase me allowed hi nahi. Chunking 7-page windows
   with 1-page OVERLAP (koi header chunk-edge pe kat-ta nahi) +
   dedupe/continuation merge at intake + `q_no` aliases
   (`question_number` etc.) tolerate.
2. **Verify loop** — extracted JSON + same pages dobara de kar
   line-by-line compare; mismatch ho to targeted re-asks (original phase
   rules prompt me re-format ho kar — shape drift nahi).
2b. **Printed-header re-ask** — text layer jahan ek header PRINTED prove
   karta hai par model ne block drop kar diya (live: OPH-001 q15), exactly
   un q_no/pages par ek bounded re-ask; drop report orphan ledger me.
3. **Answer-key phase** — sirf key grid ke page(s) par (170 dpi — chhota
   grid text); answer *sirf* isi phase se aata hai (isolation rule;
   test-locked). Na key table na inline marker? → answers empty + flagged,
   KABHI guess nahi. Inline "Ans: B" wali books ke liye alag
   zero-guess micro-phase hai (INLINE_ANSWER_PROMPT, spec extension).
4. **Answer-key verify.**
5. **Solutions phase** — solution zone, **bleed-anchor rule** ke saath.
6. **Solutions verify (+ bleed check line).**
7. **Whole-chapter cross-check** + deterministic count guards + strict
   verdict semantics (sirf explicit `"LOCKED"` count hota hai; `{}` /
   parse-fail / koi bhi missing status = NO lock). NEEDS_FIX issues per
   block targeted-fix ho kar ek bounded re-check paate hain.
8. **LOCK / commit** — rows, split trio, images, state — sab wahi format.

**BLOCK-FAIL**: jo zone EXIST karta hai par phase ne 0 items diye
(wholesale failure) → kuch nahi likha jata, chapter `chapters_done` me
nahi jata (agla Run retry karega), + `chapter_not_locked` BLOCKER.

## LOCK semantics (kab kya hota hai)

- **Boundary hi fail / questions 0 nikle** → chapter ko kuch nahi likha
  jata, `chapters_done` me nahi jata (aglaa Run dobara try karega), aur ek
  `chapter_not_locked` **BLOCKER** flag `/review` me dikh jata hai.
- **Content aaya par LOCK nahi mila** (cross-check NEEDS_FIX / count
  mismatch / verify loop exhausted) → rows phir bhi likhe jate hain
  (resumability + aap asli content review kar sakte ho), chapter done mark
  hota hai, par `chapter_not_locked` / `phase_unresolved` BLOCKER rows
  **Final zip ko locked rakhti hain** jab tak aap `/review` me decide na
  karo.
- **Purani extraction ke khule flags** naye rows likhne ke *baad*
  decision `edited` ("re-extracted at source") se band ho jate hain —
  append-only `review_decisions.jsonl` me poora audit trail rehta hai,
  aur jo cheez abhi bhi sach me kharab hai use isi run ka gate/validator
  dobara flag kar deta hai.

## Images (flag, don't fix — waisa hi)

Figures sirf **deterministic claim chain** se attach hote hain:
watermark-excluded extraction → closest-heading geometry (L1) → anchored L2, cross-page carry ke saath. Model ka
declared figure location sirf **evidence** hai export gate ke liye, override
nahi. Jo claim nahi hua wo `unmatched_images` / `unresolved_images` ledgers
me jata hai aur `/review` ke **Attach** flow se aap manually jodte ho. Image
ownership par **zero Gemini calls** — quota bachta hai, guess attachments
khatam.

## Ops notes

- Dashboard **Run** button = wahi, engine `qbank_pipeline.main()` →
  `boundary_phased.run_all(PDFS)` se chalta hai. Resume semantics same:
  `state.json → pdf_progress → chapters_done` per subject/chapter.
- **Test one chapter** button ab engine ka `run_chapter` use karta hai
  (isolated `_v2test` folder, asli data safe).
- Quota: `gemini_keys` multi-key pool + Pacific-day rollover + 5s pacing —
  sab unchanged. Pool khatam → graceful pause, kal Run dabao.
- `/recover` route **removed**. Chapter heal karna ho: chapter ko
  `chapters_done` se hatao (ya wo wahan hai hi nahi agar fail hua tha) aur
  Run dabao — engine use saaf se dobara banayega. Content corrections
  `/review` se.
- Validator ka `--audit` ab **sirf detect** karta hai — auto-apply/merge/
  delete retired (flag, don't fix); candidates human-queue me jate hain.
- Naye blocker kinds: `chapter_not_locked`, `phase_unresolved`
  (`review_queue.BLOCKER_KINDS`).
- Railway pe merge ke baad: **Redeploy with Clear build cache**, phir pehle
  ek chapter `[V2-TEST]` se smoke karo, phir full Run.

## Tests

`python -m unittest test_pipeline_json
test_image_ownership_audit_regressions test_resume_relink_regressions
test_review_queue test_review_routes test_flag_verifier test_boundary_phased`
→ **324 OK (1 intentional skip)**. Purane method ke ~150 tests usi ke saath
retire ho gaye; `test_boundary_phased.py` ab engine ke real write-through ko
cover karta hai (locked commit, unlocked blocker, block-fail abort,
answer-phase isolation, printed-zone override, header re-ask recovery,
quota-pause, driver pause).

## Live verification (2026-08-22, production Gemini, real books)

- **OBG ch3** (interleaved layout — har question ke neeche uska solution):
  LOCKED, gate CLEAN, **16/16 answers EXACT vs printed key**, solutions
  16/16, image ownership sahi (p55 q3/q4 aur p64 q15/q16 multi-draw shares
  dono deterministic), 12 calls.
- **OPH ch1** (sequential blocks): LOCKED, gate CLEAN, **23/23 answers
  identical to the previously hand-verified extraction**, solutions 23/23
  (q15 ka header printed-tha-par-drop-tha case overlap+re-ask se recover
  hua — text_confidence=low honestly REVIEW_NEEDED mark), images 18=18
  old-run se exact, 13 calls.
- Cost feel: ~12-25 calls/chapter (chapter size par). 480/day/key brake +
  multi-key rotation unchanged.
