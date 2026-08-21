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

0. **Boundary detect** — poore chapter ke pages dekh kar QUESTIONS /
   ANSWER-KEY / SOLUTIONS blocks ki exact page boundaries (+ low-confidence
   par ek re-check).
1. **Question phase** — sirf question block ke pages; sirf stem + options.
   Answer/explanation is phase me allowed hi nahi.
2. **Verify loop** — extracted JSON + same pages dobara de kar
   line-by-line compare; mismatch ho to 3 targeted re-asks tak fix-loop.
3. **Answer-key phase** — sirf key table ke pages; sirf number → letter.
   Answer *sirf* isi phase se aata hai (isolation rule; test-locked).
4. **Answer-key verify.**
5. **Solutions phase** — solution block ke pages, **bleed-anchor rule** ke
   saath (naya solution sirf explicit number-marker par khulta hai; image ke
   aage/pichhe ka text usi question ka hissa rehta hai).
6. **Solutions verify (+ bleed check line).**
7. **Whole-chapter cross-check** + deterministic count guards (AI ko counts
   par bharosa nahi — alag code guard hai).
8. **LOCK / commit** — rows, split trio, images, state — sab wahi format jo
   review queue / validator / zip pehle se jaante hain.

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
→ **323 OK (1 intentional skip)**. Purane method ke ~150 tests usi ke saath
retire ho gaye; `test_boundary_phased.py` ab engine ke real write-through ko
cover karta hai (locked commit, unlocked blocker, zero-question abort,
answer-phase isolation, quota-pause, driver pause).
