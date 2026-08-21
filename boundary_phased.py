#!/usr/bin/env python3
"""boundary_phased.py -- THE extraction engine (boundary-first, phased).

This is now the ONLY extraction method in this repo. The old multi-pass
recovery architecture (process_pdf, section windows, carry-forward merging,
targeted retry / rescue / critique passes, --recover healers) is REMOVED.
Do not reintroduce it. What remains shared with qbank_pipeline.py is pure
infrastructure, not a second method: state/quota, TOC page ranges, PDF text/
OCR anchors, watermark gates, deterministic image claiming + ownership
ledger, the export gate, final-row/split writers. This module orchestrates
them the new way:

  Step 0  boundary detection (questions / answer-key / solutions split)
  Step 1  question extraction (questions-only, no answers/solutions)
  Step 2  verify loop (max 3 targeted re-asks; then manual-queue note)
  Step 3  answer-key extraction
  Step 4  answer-key verify loop
  Step 5  solutions extraction with the explicit bleed-anchor rule
  Step 6  solutions verify loop (+ bleed check line)
  Step 7  whole-chapter end-to-end cross-check (chunked if long)
  Step 8  LOCK -- then write into the pipeline's normal output structure

Every verdict is strict JSON; a parse failure at any step = that phase is
UNRESOLVED (page-ledger + export-gate rows), never a silent success.

LOCK semantics (documented decision):
  * Boundary detection fails / question phase yields NOTHING -> the chapter
    is NOT touched at all (no rows written, not marked done): a
    chapter_not_locked BLOCKER row lands in export_gate.jsonl and the next
    run retries the chapter. A half-zoned chapter never counts as done.
  * Phases produced content but the cross-check / count guards refuse LOCK
    -> the rows ARE still written (resumability + the human reviews real
    content, same philosophy the export gate always had), the chapter is
    marked done, and chapter_not_locked / phase_unresolved BLOCKER rows keep
    the Final zip locked until a human decides in /review.
  * Flags a PREVIOUS extraction of this chapter left open are closed with
    decision 'edited' ("re-extracted at source") only AFTER the new rows are
    on disk. The append-only review_decisions.jsonl keeps the full audit
    trail; anything still genuinely wrong gets re-flagged by this run's own
    gate/validator pass.

Images: figures go through the deterministic claim chain only
(extract_real_images -> claim_page_images -> claim_block_images_ocr, with
cross-page carry and resume-relink). Model-declared figure locations are
evidence for the export gate, never an override. Anything left unclaimed is
recorded to unmatched/unresolved ledgers for the human Attach flow --
zero-guessing, flag-don't-fix. By design this engine spends NO Gemini calls
on image ownership.
"""
import base64
import gc
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import google.generativeai as genai

import gemini_keys
import qbank_pipeline as qp
import review_queue as rq
import split_outputs

MAX_FIX_ATTEMPTS = 3
PAGE_CHUNK = 7            # pages per extraction call (small = accurate)

# Tests flip this off; production paces every Gemini request through
# qp._pace_gemini_call (free tier ~15 RPM).
PACE_CALLS = True


class QuotaPaused(SystemExit):
    """Raised when every key in the pool has spent its daily budget.

    Subclasses SystemExit so the dashboard's existing SystemExit handler
    keeps marking the run 'paused' with zero app-side changes."""


def _png_bytes(pdf_path, page, dpi=110):
    pre = Path(tempfile.mkdtemp(prefix="bph_")) / "p"
    subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r",
                    str(dpi), "-png", "-singlefile", str(pdf_path), str(pre)],
                   capture_output=True, timeout=45)
    out = pre.with_suffix(".png")
    return out.read_bytes() if out.exists() else None


def _chunk_pages(pages, n):
    return [pages[i:i + n] for i in range(0, len(pages), n)]


# ---------------------------------------------------------------------------
# THE EXACT PROMPTS FROM THE SPEC (do not paraphrase)
# ---------------------------------------------------------------------------
BOUNDARY_PROMPT = """Tumhe ek textbook chapter ke consecutive pages diye jayenge (images).
Chapter ka structure teen sequential blocks me hota hai: 
QUESTIONS → ANSWER-KEY (table) → SOLUTIONS.

Task: Har block ki exact page boundary batao. Agar boundary ek page ke 
beech me hai (upper-half vs lower-half), wo bhi specify karo.

Rules:
- Question block: numbered stems + options, koi solution/explanation text nahi.
- Answer-key block: sirf ek table/grid format (Q.no → correct option letter), 
  koi paragraph text nahi.
- Solution block: explanations, jo answer-key ke baad shuru hota hai.
- Agar answer-key chapter ke end me poori tarah alag section ho, usko clearly 
  solution se differentiate karo.

ONLY respond in this JSON format:
{
  "chapter_start_page": <int>,
  "chapter_end_page": <int>,
  "question_block": {"start_page": <int>, "start_position": "top/middle/bottom"},
  "answer_key_block": {"start_page": <int>, "start_position": "...", 
                         "end_page": <int>, "end_position": "..."},
  "solution_block": {"start_page": <int>, "start_position": "...", 
                       "end_page": <int>, "end_position": "..."},
  "confidence": "high/medium/low",
  "notes": "<anything ambiguous>"
}"""

QUESTION_PROMPT = """Tumhe {chapter_name} ke pages {start}–{end} diye jayenge (sirf QUESTION block).

Task: Har question ko extract karo — number, stem, saare options.

Rules:
- SIRF stem aur options nikaalo. Correct answer ya explanation is phase me 
  BILKUL mat nikaalo, chahe page pe dikh bhi jaye.
- Figure/diagram ho to location note karo (page + position), "has_figure: true" 
  mark karo.
- Numbering exactly page ke jaisa follow karo, skip/merge mat karo.
- Text unclear ho to best-guess do, "text_confidence: low" mark karo.

ONLY respond in this JSON format (array):
[
  {{
    "q_no": "<as printed>",
    "stem": "...",
    "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
    "has_figure": true/false,
    "figure_location": {{"page": <int>, "position": "top/middle/bottom"}} or null,
    "source_page": <int>,
    "text_confidence": "high/medium/low"
  }}
]"""

ANSWER_KEY_PROMPT = """Tumhe {chapter_name} ke pages {start}–{end} diye jayenge — ye SIRF answer-key 
table hai.

Task: Table ke andar ka data hi nikaalo.

STRICT rules:
- SIRF table ke andar jo dikh raha hai wahi lo — koi question-text, koi 
  solution-text is phase me nahi, chahe page pe koi aur text dikhe.
- Format: question number → correct option.
- Cell unclear ho to "low_confidence: true" mark karo, guess mat thopo.

ONLY respond in this JSON format:
[
  {{"q_no": "<as printed>", "correct_option": "A/B/C/D", "low_confidence": true/false}}
]"""

SOLUTION_PROMPT = """Tumhe {chapter_name} ke pages {start}–{end} diye jayenge — ye SOLUTION block hai.

CRITICAL rule (bleed prevention):
Solution format aksar: text → image → text. Second text-chunk ko AGLE question 
ke saath mat jodo, chahe visually next question ke paas dikhe.

Anchor rule: Naya solution TABHI shuru hota hai jab explicit next question-
number marker dikhe (jaise "Ans. 12" ya "12."). Jab tak agla number-marker 
na dikhe, saara text (image ke pehle aur baad dono) USI current question 
ka hissa hai — proximity/layout se decide mat karo, sirf number-marker se.

Task: Har solution extract karo — number, poora explanation (pre+post image 
text merged), figure location agar hai.

ONLY respond in this JSON format:
[
  {{
    "q_no": "<as printed>",
    "solution_text": "<merged text>",
    "has_figure": true/false,
    "figure_location": {{"page": <int>, "position": "top/middle/bottom"}} or null,
    "source_page_range": [<start>, <end>],
    "text_confidence": "high/medium/low"
  }}
]"""

VERIFY_PROMPT = """Tumhe do cheezein di ja rahi hain:
1. {phase_name} ka extracted structured output (JSON).
2. Wo exact source pages (images) jinse ye extract hua tha.

Task: Start se end tak line-by-line compare karo:
- Text page se match karta hai (wording normalize ho sakti hai)?
- has_figure: true hai to genuinely figure hai us location pe?
- has_figure: false hai to sach me koi figure miss nahi hui?
- Numbering sequence correct — koi skip/duplicate nahi?
{bleed_line}

Sirf mismatches report karo, sahi wale ke liye kuch mat likho.

ONLY respond in this JSON format:
{{
  "phase": "{phase_name}",
  "total_entries_checked": <int>,
  "all_verified": true/false,
  "mismatches": [
    {{"q_no": "...", "issue": "<specific, one line>", "severity": "genuine/minor"}}
  ]
}}"""

BLEED_LINE = ("Extra check: kya kisi solution ka text galti se agle question "
              "ke solution me chala gaya hai (bleed)? Agar koi solution "
              "suspiciously short lag raha hai aur agle wale me extra unrelated "
              "content lag raha hai, usko bhi mismatch me report karo.")

CHAPTER_FINAL_PROMPT = """{chapter_name} ke teeno phases individually verify ho chuke hain. Ab poore 
chapter ka final cross-check karo.

Diya ja raha hai:
1. Poora chapter ka structured data pack (questions + answer-key + solutions).
2. Poore chapter ke saare source pages (images).

Task: End-to-end verify karo:
- Har question ka answer-key entry hai (orphan/missing nahi)?
- Har question ka solution hai (orphan/missing nahi)?
- Numbers consistent (gap/mismatch nahi)?
- Figures sahi question/solution se attached hain?

ONLY respond in this JSON format:
{{
  "chapter": "{chapter_name}",
  "status": "LOCKED" or "NEEDS_FIX",
  "total_questions": <int>,
  "issues": [
    {{"q_no": "...", "issue": "<specific>", "block": "question/answer_key/solution"}}
  ]
}}

Ek bhi genuine mismatch ho to status "NEEDS_FIX" do — sirf sab clean hone par 
"LOCKED" bolo."""


def _parse_json(text):
    if not text:
        return None
    m = re.search(r"\[.*\]|\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _safe_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _merge_boundary_chunks(cands):
    """One boundary answer per page-chunk must not silently keep only the
    FIRST chunk's view (live finding: chunk 2's 'solutions' never merged, so
    boundary said answer_key=-1). Merge: earliest chunk defines question
    start; ANY chunk reporting a key/solution start past it wins for those
    blocks. -1/'none' from the model = absent, never a real page number."""
    if not cands:
        return None

    def clean_int(v):
        i = _safe_int(v)
        return i if (i is not None and i > 0) else None

    qb = None
    ab = None
    sb = None
    for cand in cands:
        if cand.get("question_block") and qb is None:
            qb = cand["question_block"]
        if cand.get("answer_key_block") and clean_int(cand["answer_key_block"].get("start_page")):
            ab = cand["answer_key_block"]
        if cand.get("solution_block") and clean_int(cand["solution_block"].get("start_page")):
            sb = cand["solution_block"]
    if qb is None:
        return None
    out = {"confidence": min((c.get("confidence") for c in cands if
                              c.get("confidence")), default="high"),
           "question_block": qb,
           "answer_key_block": ab,
           "solution_block": sb,
           "notes": "; ".join(c.get("notes", "") for c in cands if c.get("notes"))}
    return out


def _render_chapter_jpgs(pdf_path, ch_first, ch_last, subject, chapter_no):
    """One pdftoppm pass over the chapter's pages (150 dpi jpg) -- the
    zero-token layer the anchor harvester / split reconciler read. Returns
    (page_files, page_numbers)."""
    page_dir = Path(tempfile.mkdtemp(prefix=f"{subject}_ch{chapter_no:03d}_"))
    subprocess.run([
        "pdftoppm", "-jpeg", "-r", "150",
        "-f", str(ch_first), "-l", str(ch_last),
        str(pdf_path), str(page_dir / "page")
    ], timeout=900, capture_output=True)
    page_files = sorted(p for p in page_dir.glob("page-*.jpg")
                        if re.fullmatch(r"page-\d+", p.stem))
    page_numbers = [int(p.stem.split("-")[-1]) for p in page_files]
    return page_files, page_numbers


class ChapterRunner:
    """One chapter through Steps 0-8, then a real commit into the pipeline's
    normal output (Step 8 is WRITE-THROUGH, not a dry run).

    Every Gemini request funnels through _gen() -- the single choke point
    that paces calls, counts quota, rotates keys on 429 and raises
    QuotaPaused when the whole pool is spent. No other code path may call
    the model directly.
    """

    def __init__(self, pdf_path, subject, chapter_no, out_root=None,
                 model=None, page_offset=0, state=None):
        self.pdf = str(pdf_path)
        self.subject = subject
        self.chapter_no = chapter_no
        self.chapter_id = f"{subject}-{chapter_no:03d}"
        self.out_root = Path(out_root) if out_root else Path(qp.OUTPUT_ROOT)
        self.model = model or gemini_keys.track(genai.GenerativeModel(qp.GEMINI_MODEL))
        self.page_offset = page_offset
        self.state = state if state is not None else \
            {"calls_today": 0, "pdf_progress": {}}
        self.notes = []
        self.ledger_rows = []          # this chapter's page_ledger rows
        self.orphan_items = []         # phase items with no parseable q_no
        self.watermark_ids = None      # driver computes once per book

    # ------------------------------------------------------------------
    # THE model-call choke point
    # ------------------------------------------------------------------
    def _gen(self, files):
        """files: ordered parts (text strings and/or image dicts). Returns
        response text. Raises QuotaPaused when the day's pool is spent."""
        if PACE_CALLS:
            qp._pace_gemini_call()
        if qp.quota_exhausted(self.state):
            qp.save_state(self.state)
            raise QuotaPaused()
        try:
            resp = self.model.generate_content(
                files, safety_settings=qp.SAFETY_SETTINGS)
            qp.note_call(self.state)
            return resp.text
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower():
                qp.note_call(self.state)
                if qp.handle_429(self.state, err):
                    qp.save_state(self.state)
                    return self._gen(files)      # rotated key -> retry once-through
                qp.save_state(self.state)
                raise QuotaPaused()
            if qp._transient_gemini_err(err):
                time.sleep(20)                   # one bounded transient retry
                resp = self.model.generate_content(
                    files, safety_settings=qp.SAFETY_SETTINGS)
                qp.note_call(self.state)
                return resp.text
            raise

    def _call_pages(self, pages, prompt):
        """One call with page images attached. Every call leads with the
        FILE-page order in plain text -- without it the model answered
        chunk-local indexes (live-run finding: the boundary detector said
        'page 1-7' for what were file pages 286+)."""
        files = [f"PAGE ORDER (in this batch, images follow in the exact same "
                 f"order; these are REAL file-page numbers the model must use in "
                 f"its answer): {pages}"]
        for p in pages:
            b = _png_bytes(self.pdf, p)
            if b:
                files.append({"mime_type": "image/png", "data":
                              base64.b64encode(b).decode()})
        files.append(prompt)
        return self._gen(files)

    def _ledger(self, pass_name, pages, status, n_items, note=""):
        row = qp._ledger_pass(self.chapter_id, self.subject, self.chapter_no,
                              pass_name, pages or [], status, n_items, note)
        self.ledger_rows.append(row)
        return row

    # -- Step 0 --------------------------------------------------------------
    def detect_boundaries(self, ch_first, ch_last):
        pages = list(range(ch_first, ch_last + 1))
        res = None
        for attempt in range(2):                    # spec: low conf -> re-check
            cands = []
            chunks = _chunk_pages(pages, min(PAGE_CHUNK, 12))
            for chunk in chunks:
                raw = self._call_pages(chunk, BOUNDARY_PROMPT)
                cand = _parse_json(raw)
                if isinstance(cand, dict) and cand.get("question_block"):
                    cands.append(cand)
                self._ledger("BOUNDARY", chunk,
                             qp.PASS_STATUS_SUCCESS if isinstance(cand, dict)
                             else qp.PASS_STATUS_UNRESOLVED,
                             1 if isinstance(cand, dict) else 0,
                             f"attempt {attempt + 1}")
            res = _merge_boundary_chunks(cands) if cands else None
            if res and res.get("confidence") != "low":
                break
            self.notes.append(f"boundary retry (low confidence, attempt {attempt+1})")
        if not res:
            raise RuntimeError("boundary detection failed twice -- leaving for review")
        return res

    # -- Phase extraction helper ---------------------------------------------
    def _extract_phase(self, pages, prompt_tmpl, label, pass_name):
        out = []
        for chunk in _chunk_pages(pages, PAGE_CHUNK):
            p = prompt_tmpl.format(chapter_name=self.chapter_id,
                                   start=chunk[0], end=chunk[-1])
            raw = self._call_pages(chunk, p)
            items = _parse_json(raw)
            if isinstance(items, list):
                kept, dropped = [], 0
                for it in items:
                    if isinstance(it, dict) and _safe_int(it.get("q_no")) is not None:
                        kept.append(it)
                    else:
                        dropped += 1
                        self.orphan_items.append({
                            "chapter_id": self.chapter_id,
                            "batch_start": chunk[0],
                            "pdf_pages": list(chunk),
                            "reason": "no_parseable_q_no",
                            "pass": pass_name,
                            "item": it,
                        })
                out.extend(kept)
                self._ledger(pass_name, chunk,
                             qp.PASS_STATUS_SUCCESS if kept
                             else qp.PASS_STATUS_PARTIAL,
                             len(kept),
                             f"{dropped} item(s) w/o q_no -> orphans" if dropped else "")
            else:
                # STRICT: an unparsable phase chunk is NEVER a silent zero.
                self._ledger(pass_name, chunk, qp.PASS_STATUS_UNRESOLVED, 0,
                             "phase chunk returned no parseable JSON array")
        return out

    # -- Verify helper (Steps 2/4/6 share this) --------------------------------
    def _verify_phase(self, phase_name, items, pages):
        if not pages:
            return items, True
        bleed_line = BLEED_LINE if phase_name == "Solution" else ""
        prompt = VERIFY_PROMPT.format(phase_name=phase_name, bleed_line=bleed_line)
        payload = json.dumps(items, ensure_ascii=False)
        last = None
        for attempt in range(MAX_FIX_ATTEMPTS):
            files = [payload]
            for p in pages:
                b = _png_bytes(self.pdf, p)
                if b:
                    files.append({"mime_type": "image/png", "data":
                                  base64.b64encode(b).decode()})
            files.append(prompt)
            try:
                raw = self._gen(files)
            except QuotaPaused:
                raise
            except Exception as e:
                self._ledger(f"VERIFY_{phase_name[0]}", pages,
                             qp.PASS_STATUS_UNRESOLVED, 0,
                             f"verify call error: {e}")
                return items, ("verify-error", str(e))
            v = _parse_json(raw)
            if not isinstance(v, dict):
                return items, None             # parse fail -> unresolved, stays
            last = v
            if v.get("all_verified") is True:
                self._ledger(f"VERIFY_{phase_name[0]}", pages,
                             qp.PASS_STATUS_SUCCESS, len(items),
                             f"attempt {attempt + 1}")
                return items, True
            mism = v.get("mismatches") or []
            genuine = [m for m in mism if isinstance(m, dict)]
            if not genuine:
                self._ledger(f"VERIFY_{phase_name[0]}", pages,
                             qp.PASS_STATUS_SUCCESS, len(items),
                             "no specific mismatches")
                return items, True             # nothing specific -> treat clean
            items = self._targeted_fix(phase_name, items, genuine)
        return items, ("exceeded attempts", last)

    def _targeted_fix(self, phase_name, items, mismatches):
        qns = [str(m.get("q_no")) for m in mismatches if m.get("q_no")]
        if not qns:
            return items
        re_prompt = (f"Sirf in question numbers ko dobara extract karo pages se: "
                     f"{qns}. Same rules, same JSON format sirf unhi ke liye.")
        for chunk in _chunk_pages(
                sorted({i.get("source_page") for i in items
                        if i.get("source_page")}), PAGE_CHUNK):
            raw = self._call_pages(chunk, re_prompt)
            items_fixed = _parse_json(raw)
            if isinstance(items_fixed, list):
                by_no = {str(i.get("q_no")): i for i in items
                         if i.get("q_no") is not None}
                for it in items_fixed:
                    by_no[str(it.get("q_no"))] = it
                items = [by_no[k] for k in by_no]
        return items

    # -- Step 7: chapter cross-check -----------------------------------------
    def _cross_check(self, q_items, a_items, s_items, pages):
        pack = json.dumps({"chapter": self.chapter_id, "questions": q_items,
                           "answer_key": a_items, "solutions": s_items},
                          ensure_ascii=False)[:12000]
        halves = [pages[:len(pages) // 2], pages[len(pages) // 2:]] \
            if len(pages) > 12 else [pages]
        for _ in range(MAX_FIX_ATTEMPTS):
            all_locked = True
            any_issue = False
            for half in halves:
                if not half:
                    continue
                files = [pack]
                for p in half:
                    b = _png_bytes(self.pdf, p)
                    if b:
                        files.append({"mime_type": "image/png", "data":
                                      base64.b64encode(b).decode()})
                files.append(CHAPTER_FINAL_PROMPT.format(chapter_name=self.chapter_id))
                raw = self._gen(files)
                v = _parse_json(raw)
                if not isinstance(v, dict):
                    all_locked = False        # parse fail = NEVER locks silently
                    continue
                if v.get("status") == "NEEDS_FIX":
                    all_locked = False
                    any_issue = True
                    for iss in (v.get("issues") or []):
                        if isinstance(iss, dict) and iss.get("issue"):
                            self.notes.append(
                                f"cross-check q{iss.get('q_no')}: {iss['issue']}")
                    break
            if any_issue:
                continue                       # fix-loop -> re-check
            if all_locked and not any_issue:
                return True
            # some half didn't even parse -> inconclusive, re-ask once more
        return False            # any NEEDS_FIX or repeated inconclusive = NOT locked

    # -- Record assembly ------------------------------------------------------
    def _build_records(self, q_items, a_items, s_items):
        """phase JSON -> the canonical chapter_records shape the final-row /
        split writers consume. The answer comes ONLY from the answer-key
        phase; the solution ONLY from the solutions phase -- no phase ever
        reads outside its block (spec hard rule)."""
        amap = {}
        a_low = set()
        for a in a_items:
            qn = _safe_int(a.get("q_no"))
            if qn is None:
                continue
            amap[qn] = (a.get("correct_option") or "").strip().upper() or None
            if a.get("low_confidence"):
                a_low.add(qn)
        smap = {}
        for s in s_items:
            qn = _safe_int(s.get("q_no"))
            if qn is not None:
                smap[qn] = s
        records = {}
        qn_source_pages = {}
        for q in q_items:
            qn = _safe_int(q.get("q_no"))
            if qn is None:
                continue
            srow = smap.get(qn) or {}
            pages = set()
            if _safe_int(q.get("source_page")) is not None:
                pages.add(_safe_int(q.get("source_page")))
            spr = srow.get("source_page_range") or []
            for p in spr[:2]:
                if _safe_int(p) is not None:
                    pages.add(_safe_int(p))
            if len(spr) >= 2 and all(_safe_int(x) is not None for x in spr[:2]) \
                    and spr[1] > spr[0] and spr[1] - spr[0] <= 4:
                pages.update(range(_safe_int(spr[0]), _safe_int(spr[1]) + 1))
            qn_source_pages[qn] = pages
            reasons = []
            if str(q.get("text_confidence", "")).lower() == "low":
                reasons.append("question phase marked text_confidence=low")
            if str(srow.get("text_confidence", "")).lower() == "low":
                reasons.append("solution phase marked text_confidence=low")
            if qn in a_low:
                reasons.append("answer-key cell marked low_confidence by model")
            opts = {}
            for k, v in (q.get("options") or {}).items():
                letter = str(k).strip().upper()
                if letter:
                    opts[letter] = str(v or "").strip()
            records[qn] = {
                "q_no": qn,
                "question_text": str(q.get("stem") or "").strip(),
                "options": opts,
                "correct_option": amap.get(qn),
                "solution_text": str(srow.get("solution_text") or "").strip(),
                "tables": qp._dedupe_tables(srow.get("tables") or []),
                "has_figure_in_question": bool(q.get("has_figure")),
                "has_figure_in_solution": bool(srow.get("has_figure")),
                "_review_reasons": reasons,
                "q_no_anchors": {"field_provenance": {
                    "question_text": "BOUNDARY_PHASED", "options": "BOUNDARY_PHASED",
                    "correct_option": "BOUNDARY_PHASED",
                    "solution_text": "BOUNDARY_PHASED"}},
                "_prov_expected": True,
            }
        n_no_ans = sum(1 for r in records.values() if not r["correct_option"])
        n_no_sol = sum(1 for r in records.values() if not r["solution_text"])
        if n_no_ans:
            self.notes.append(f"{n_no_ans} question(s) without an answer-key row")
        if n_no_sol:
            self.notes.append(f"{n_no_sol} question(s) without a solution block")
        return records, qn_source_pages

    # -- Images: deterministic ownership only ---------------------------------
    def _image_pass(self, ch_first, ch_last, page_section, chapter_records,
                    image_files_by_q):
        """Walk the chapter's pages once, in order. L1 geometry claim -> L2
        OCR-anchored claim, with the cross-page carry advancing over every
        page (even image-less ones). Leftovers are recorded for human
        review -- the engine never lets a model guess an owner. Returns the
        chapter_unresolved_images ledger recs."""
        watermark_ids = self.watermark_ids
        if watermark_ids is None:
            watermark_ids = qp.find_watermark_object_ids(self.pdf)
        chapter_owned_hashes = qp.hash_owned_image_files(
            qp.ASSETS_DIR / "questions" / self.subject)
        unresolved = []
        active_block = None
        pages = list(range(ch_first, ch_last + 1))
        # Carry seed: a figure at the very top of the chapter's first imaged
        # page can belong to a block opened earlier. Seed from the nearest
        # printed heading before the first page -- CLAMPED at the chapter's
        # first page (the run-22 D3 fix: never seed from the previous
        # chapter), and plausibility-checked like any page anchor.
        for back in range(1, qp.CARRY_SEED_LOOKBACK_PAGES + 1):
            prev = ch_first - back
            if prev < max(1, ch_first):
                break
            try:
                seed = qp.last_block_on_page(self.pdf, prev,
                                             chapter_records=chapter_records,
                                             section=page_section.get(prev))
            except Exception:
                seed = None
            if seed is not None and not qp._plausible_qn_for_chapter(
                    seed[1], chapter_records):
                continue
            if seed is not None:
                active_block = seed
                break
        for page in pages:
            imgs = qp.extract_real_images(self.pdf, page, watermark_ids,
                                          self.subject,
                                          qp.ASSETS_DIR / "questions",
                                          skip_hashes=chapter_owned_hashes)
            if imgs:
                pos = qp.image_positions_on_page(self.pdf, page)
                ordered = qp._order_imgs_by_position(imgs, pos)
                leftover = qp.claim_page_images(
                    ordered, self.pdf, page, self.subject, self.chapter_no,
                    chapter_records, image_files_by_q,
                    active_block=active_block, section=page_section.get(page))
                leftover = qp.claim_block_images_ocr(
                    leftover, self.pdf, page, self.subject, self.chapter_no,
                    chapter_records, image_files_by_q, chapter_id=self.chapter_id,
                    active_block=active_block, section=page_section.get(page))
                for rel in leftover:
                    entry = {"chapter_id": self.chapter_id, "q_no": None,
                             "page": page, "file": rel,
                             "detail": "printed figure found no deterministic "
                                       "owner (boundary engine runs no vision "
                                       "guess pass by design) -- Attach it in "
                                       "/review, or Skip if decorative",
                             "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                    qp._append_jsonl(qp.DATA_DIR / "unmatched_images.jsonl", entry)
                    rec = qp._record_unresolved_image(
                        self.subject, self.chapter_id, page, rel,
                        "exhausted_deterministic_levels",
                        method="none", confidence=None)
                    if rec:
                        unresolved.append(rec)
            # The carry MUST advance over image-less pages too (run-25
            # Defect B: otherwise a header-less top figure resolves against
            # a block that closed several text-only pages earlier).
            try:
                last = qp.last_block_on_page(self.pdf, page,
                                             chapter_records=chapter_records,
                                             section=page_section.get(page))
            except Exception:
                last = None
            if last is not None and qp._plausible_qn_for_chapter(
                    last[1], chapter_records):
                active_block = last
        return unresolved

    # -- Previously-open flags for this chapter -------------------------------
    def _close_previous_flags(self, pre_rows):
        """Spec rule: flags a previous extraction of THIS chapter left open
        get decision 'edited' ("re-extracted at source") -- but only AFTER
        the new rows are on disk, and never the rows THIS run just wrote."""
        closed = 0
        for row in pre_rows:
            try:
                res = rq.record_decision(
                    self.out_root, row["flag_key"], "edited",
                    "boundary-phased engine re-extracted this chapter at "
                    "source; previous extraction's flag superseded",
                    q_id=row.get("q_id"))
                if res.get("ok"):
                    closed += 1
            except Exception:
                continue
        if closed:
            print(f"  [BPH] {self.chapter_id}: closed {closed} flag(s) from "
                  f"the previous extraction of this chapter")

    # -- WRITE-THROUGH (Step 8) -----------------------------------------------
    def _commit(self, ch_first, ch_last, page_section, q_items, a_items,
                s_items, locked, pre_rows):
        """Produce the SAME final-row + split + assets + state structure the
        system always wrote, so the converter, review layer, validator and
        zip gate see no new schema."""
        chapter_records, qn_source_pages = self._build_records(
            q_items, a_items, s_items)
        image_files_by_q = {}
        page_files, page_numbers = _render_chapter_jpgs(
            self.pdf, ch_first, ch_last, self.subject, self.chapter_no)
        chapter_unresolved_images = self._image_pass(
            ch_first, ch_last, page_section, chapter_records, image_files_by_q)
        ownership_pages = qp._ownership_page_map(self.chapter_id)
        # RESUME-RELINK: a crashed/paused previous attempt of THIS chapter
        # already claimed + renamed its figures; extraction skips them on the
        # re-run ('bytes already owned'), so re-attach from the append-only
        # ownership ledger -- evidence only, zero guessing.
        relink_notes = qp._relink_resume_owned_images(
            self.chapter_id, self.subject, self.chapter_no,
            chapter_records, image_files_by_q, ownership_pages)
        if relink_notes:
            print(f"  [IMG] resume-relink restored {len(relink_notes)} "
                  f"previously-claimed figure(s)")
        try:
            chapter_anchor_idx = qp.chapter_anchor_pages(
                self.pdf, page_numbers, chapter_records,
                page_sections=page_section)
        except Exception as anch_e:
            print(f"  [WARN] anchor-index build failed ({anch_e}) -- gate's "
                  f"figure-page checks skip this chapter (conservative)")
            chapter_anchor_idx = None

        # phase items with no q_no are never silently dropped
        for orph in self.orphan_items:
            qp._append_jsonl(qp.DATA_DIR / "orphans.jsonl", orph)

        unresolved_ledger = [r for r in self.ledger_rows
                             if r.get("status") == qp.PASS_STATUS_UNRESOLVED]
        violations = qp._export_gate_violations(
            chapter_records, image_files_by_q, unresolved_ledger,
            self.chapter_id, chapter_unresolved_images, [],
            anchor_pages=chapter_anchor_idx, ownership_pages=ownership_pages)
        if not locked:
            reasons = "; ".join(self.notes[-6:]) or "cross-check refused LOCK"
            violations.append(("chapter_not_locked", None,
                               f"final cross-check / count guards refused "
                               f"LOCK: {reasons}"))
        for phase_note in self.notes:
            if "unresolved" in phase_note:
                violations.append(("phase_unresolved", None, phase_note))
        for kind, qn, detail in violations:
            qp._append_jsonl(qp.DATA_DIR / "export_gate.jsonl",
                             {"chapter_id": self.chapter_id, "kind": kind,
                              "q_no": qn, "detail": detail,
                              "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        if violations:
            print(f"  [GATE] {self.chapter_id}: {len(violations)} export-gate "
                  f"violation(s) -- NOT a clean export")
            for kind, qn, detail in violations[:25]:
                print(f"    - {kind} {qn}: {detail}")
        else:
            print(f"  [GATE] {self.chapter_id}: export gate CLEAN "
                  f"(stems/options/answers/solutions/images/assets all "
                  f"accounted)")

        # Split layer (sidecar). A failure here must never hurt the master
        # rows -- same contract the pipeline always kept.
        try:
            reconciled = split_outputs.reconcile_qids(
                chapter_records, qn_source_pages, self.pdf, page_files,
                self.subject, self.chapter_no)
            split_completeness = split_outputs.write_split_outputs(
                chapter_id=self.chapter_id, subject=self.subject,
                chapter_no=self.chapter_no,
                chapter_records=chapter_records,
                image_files_by_q=image_files_by_q,
                qn_source_pages=qn_source_pages,
                orphans=list(self.orphan_items),
                chapter_unresolved_images=chapter_unresolved_images,
                pdf_path=self.pdf, page_files=page_files,
                reconciled=reconciled,
                output_root=qp.OUTPUT_ROOT,
                ownership_pages=ownership_pages)
            print(f"  [SPLIT] {self.chapter_id}: "
                  f"{split_completeness.get('question_records')} questions / "
                  f"{split_completeness.get('answer_records')} answers / "
                  f"{split_completeness.get('solution_records')} solutions")
        except Exception as e:
            print(f"  [SPLIT] {self.chapter_id}: split-layer error ({e}) -- "
                  f"master output unaffected, split files NOT written")

        gate_by_qn = {}
        for kind, qn_v, detail in violations:
            # NOTE: chapter/page-scope violations carry qn=None or even the
            # pages LIST (unresolved_page_* rows) -- only a true int is a
            # question number that row-level gate notices can attach to.
            if isinstance(qn_v, int):
                gate_by_qn.setdefault(qn_v, []).append((kind, detail))
        chapter_rows = []
        for qn, rec in sorted(chapter_records.items(), key=lambda x: x[0]):
            chapter_rows.append(qp.build_final_question(
                self.subject, self.chapter_id, self.chapter_no, qn, rec,
                image_files_by_q.get(qn, {"question": [], "solution": []}),
                source_pages=qn_source_pages.get(qn),
                ownership_pages=ownership_pages,
                gate_notices=gate_by_qn.get(qn, [])))
        questions_path = qp.DATA_DIR / "questions.jsonl"
        qp.rewrite_questions_file(questions_path, self.chapter_id, chapter_rows)
        qp.write_chapter_file(self.subject, self.chapter_id, chapter_rows)

        # rows are on disk NOW -> previous extraction's open flags for this
        # chapter are superseded (append-only decision trail, re-openable).
        self._close_previous_flags(pre_rows)

        progress = self.state.setdefault("pdf_progress", {}).setdefault(
            self.subject, {"chapters_done": [], "current": None})
        if self.chapter_id not in progress["chapters_done"]:
            progress["chapters_done"].append(self.chapter_id)
        qp._dump_declared_allowances(self.state)
        qp.save_state(self.state)

        qa_ready = sum(1 for r in chapter_rows if r.get("qa_status") == "READY")
        qa_rev = sum(1 for r in chapter_rows if r.get("qa_status") == "REVIEW_NEEDED")
        qa_inc = sum(1 for r in chapter_rows if r.get("qa_status") == "INCOMPLETE")
        print(f"[{self.subject}] chapter {self.chapter_no} done -> "
              f"{len(chapter_rows)} questions | qa_status: {qa_ready} READY, "
              f"{qa_rev} REVIEW_NEEDED, {qa_inc} INCOMPLETE "
              f"| lock={'yes' if locked else 'NO (flagged for review)'}")
        qp.clear_render_cache()
        gc.collect()
        return chapter_rows

    # -- Steps 0-8 -------------------------------------------------------------
    def run(self, ch_first, ch_last):
        print(f"[BPH] {self.chapter_id}: boundary detect {ch_first}-{ch_last}")
        bounds = self.detect_boundaries(ch_first, ch_last)
        print(f"[BPH] boundaries:", json.dumps(bounds, ensure_ascii=False)[:300])
        qb = bounds.get("question_block") or {}
        ab = bounds.get("answer_key_block") or {}
        sb = bounds.get("solution_block") or {}
        # STRICT (spec): a missing block boundary means the chapter was never
        # safely zoned -- do NOT run it half-way; hand it to manual review.
        if not qb.get("start_page") or not (sb and sb.get("start_page")):
            raise RuntimeError(
                f"{self.chapter_id}: boundary detect incomplete "
                f"(q_start={qb.get('start_page')}, s_start={sb.get('start_page')}) -- "
                "no extraction attempted, chapter left for review")
        qb_start = _safe_int(qb.get("start_page"))
        sb_start = _safe_int(sb.get("start_page"))
        ab_start = _safe_int(ab.get("start_page"))
        if qb_start is None or sb_start is None:
            raise RuntimeError(
                f"{self.chapter_id}: boundary pages not parseable "
                f"(q_start={qb.get('start_page')!r}, s_start={sb.get('start_page')!r}) "
                "-- chapter left for review")
        q_end = (ab_start or sb_start) - 1
        if q_end < qb_start:
            q_end = qb_start
        q_pages = list(range(qb_start, min(q_end, ch_last) + 1))
        a_pages = list(range(ab_start, min(_safe_int(ab.get("end_page"))
                                           or ab_start, ch_last) + 1)) \
            if ab_start else []           # some books have NO key table
        s_end = min(_safe_int(sb.get("end_page")) or ch_last, ch_last)
        s_pages = list(range(sb_start, s_end + 1))
        print(f"[BPH] {self.chapter_id}: Q pages {q_pages[0]}-{q_pages[-1]}"
              f" | A pages {a_pages[0]}-{a_pages[-1] if a_pages else '-'} "
              f"({len(a_pages)}) | S pages {s_pages[0]}-{s_pages[-1]}")

        # Snapshot this chapter's currently-open flags BEFORE we write
        # anything -- these belong to a PREVIOUS extraction and get closed
        # after the new rows land (never flags this run itself writes).
        try:
            pre_rows = [r for r in rq.collect_review_queue(self.out_root)["rows"]
                        if r.get("chapter_id") == self.chapter_id]
        except Exception:
            pre_rows = []

        q_items = self._extract_phase(q_pages, QUESTION_PROMPT, "Question", "Q")
        q_items, q_ok = self._verify_phase("Question", q_items, q_pages)
        if q_ok is not True:
            self.notes.append(f"question phase unresolved: {q_ok}")
        qp.save_state(self.state)
        a_items = self._extract_phase(a_pages, ANSWER_KEY_PROMPT, "Answer-key",
                                      "A") if a_pages else []
        a_items, a_ok = self._verify_phase("Answer-key", a_items, a_pages) \
            if a_pages else ([], True)
        if a_ok is not True:
            self.notes.append(f"answer-key phase unresolved: {a_ok}")
        s_items = self._extract_phase(s_pages, SOLUTION_PROMPT, "Solution", "S")
        s_items, s_ok = self._verify_phase("Solution", s_items, s_pages)
        if s_ok is not True:
            self.notes.append(f"solutions phase unresolved: {s_ok}")
        qp.save_state(self.state)

        same_pages = sorted(set(q_pages) | set(a_pages) | set(s_pages))
        locked = self._cross_check(q_items, a_items, s_items, same_pages)
        # deterministic count guards ON TOP of the AI cross-check (spec: 'any
        # genuine mismatch -> NEEDS_FIX'): the AI can't be trusted with
        # counts. A phase that produced NOTHING can't ever lock an
        # item-bearing one.
        if locked and s_items and len(s_items) > len(q_items) * 2:
            locked = False
            self.notes.append(f"count weirdness: {len(q_items)} questions vs "
                              f"{len(s_items)} solutions")
        if locked and q_items and (not a_items or len(a_items) * 2 < len(q_items)):
            locked = False
            self.notes.append(f"answer-key sparse: {len(q_items)} questions but "
                              f"only {len(a_items)} keyed rows -- 'inline answer' "
                              "chapters must attach per question, not lock")
        if locked and q_items and not s_items:
            locked = False
            self.notes.append("solutions phase produced nothing -- "
                              "never lock a question-only chapter")
        if locked and a_items and len(a_items) != len(q_items):
            locked = False
            self.notes.append(f"count mismatch: {len(q_items)} questions vs "
                              f"{len(a_items)} answers")
        print(f"[BPH] {self.chapter_id}: final cross-check lock={locked}")

        if not q_items:
            # nothing trustworthy to write -- leave the chapter UNDONE so the
            # next run retries it; the blocker row makes it visible meanwhile.
            qp._append_jsonl(qp.DATA_DIR / "export_gate.jsonl", {
                "chapter_id": self.chapter_id, "kind": "chapter_not_locked",
                "q_no": None,
                "detail": "question phase produced 0 parseable items -- "
                          "nothing written, chapter will be retried on the "
                          "next run",
                "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
            qp.save_state(self.state)
            return {"locked": False, "committed": False,
                    "chapter_id": self.chapter_id, "questions": 0,
                    "answers": len(a_items), "solutions": len(s_items),
                    "notes": self.notes}

        page_section = {p: "S" for p in s_pages}
        rows = self._commit(ch_first, ch_last, page_section,
                            q_items, a_items, s_items, locked, pre_rows)
        return {"locked": locked, "committed": True,
                "chapter_id": self.chapter_id, "questions": len(q_items),
                "answers": len(a_items), "solutions": len(s_items),
                "rows_written": len(rows), "notes": self.notes}


def run_chapter(pdf_path, subject, chapter_no, out_root=None, page_offset=0,
                model=None, state=None):
    """Extract ONE chapter with the engine (dashboard smoke-test button and
    CLI both go through here)."""
    r = ChapterRunner(pdf_path, subject, chapter_no, out_root, model=model,
                      page_offset=page_offset, state=state)
    toc = qp.extract_toc_chapters(str(pdf_path))
    total = len(qp.PdfReader(str(pdf_path)).pages)
    chs = qp.compute_page_ranges(toc, page_offset, total)
    target = next((c for c in chs if c["chapter_no"] == chapter_no), None)
    if not target:
        raise ValueError(f"chapter {chapter_no} not found in TOC ranges")
    return r.run(target["file_start"], target["file_end"])


def run_all(pdf_cfgs=None, state=None):
    """The whole-book driver -- this is what qbank_pipeline.main() (and so
    the dashboard's Run button) calls. Resumes at chapter granularity via
    state['pdf_progress'][subject]['chapters_done']; pauses gracefully on
    the daily quota (QuotaPaused -> SystemExit semantics)."""
    qp.DATA_DIR.mkdir(parents=True, exist_ok=True)
    qp.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    state = state if state is not None else qp.load_state()
    gemini_keys.init(state, qp.MAX_CALLS_PER_DAY)
    model = gemini_keys.track(genai.GenerativeModel(qp.GEMINI_MODEL))
    qp.reset_daily_counter_if_needed(state)

    chapters_path = qp.DATA_DIR / "chapters.json"
    chapters_out = json.loads(chapters_path.read_text()) \
        if chapters_path.exists() else []
    questions_path = qp.DATA_DIR / "questions.jsonl"

    for pdf_cfg in (pdf_cfgs if pdf_cfgs is not None else qp.PDFS):
        subject = pdf_cfg["subject"]
        pdf_path = pdf_cfg["path"]
        progress = state.setdefault("pdf_progress", {}).setdefault(
            subject, {"chapters_done": [], "current": None})
        qp._load_declared_allowances(state)  # survive daily-quota resumes

        watermark_ids = qp.find_watermark_object_ids(pdf_path)
        wm_label = ", ".join(str(x) for x in sorted(watermark_ids)) or "none"
        print(f"[{subject}] watermark object ids: {wm_label}")

        total_pages = len(qp.PdfReader(pdf_path).pages)
        toc = qp.extract_toc_chapters(pdf_path)
        chapters = qp.compute_page_ranges(toc, pdf_cfg["page_offset"],
                                          total_pages)
        for ch in chapters:
            chapter_id = f"{subject}-{ch['chapter_no']:03d}"
            if chapter_id in progress["chapters_done"]:
                continue
            if not any(c.get("chapter_id") == chapter_id
                       for c in chapters_out):
                chapters_out.append({
                    "chapter_id": chapter_id, "subject": subject,
                    "chapter_no": ch["chapter_no"],
                    "chapter_title": ch["chapter_title"]})
            runner = ChapterRunner(pdf_path, subject, ch["chapter_no"],
                                   qp.OUTPUT_ROOT, model=model,
                                   page_offset=pdf_cfg.get("page_offset", 0),
                                   state=state)
            runner.watermark_ids = watermark_ids
            try:
                res = runner.run(ch["file_start"], ch["file_end"])
            except QuotaPaused:
                qp.write_chapters(chapters_path, chapters_out)
                qp.save_state(state)
                print(f"[{subject}] daily quota spent -- paused at chapter "
                      f"{ch['chapter_no']}; re-run to resume tomorrow.")
                raise
            except Exception as e:
                # A failed chapter must never block the book: flag it as a
                # BLOCKER, leave it out of chapters_done (next run retries
                # it), and continue with the next chapter.
                traceback.print_exc()
                qp._append_jsonl(qp.DATA_DIR / "export_gate.jsonl", {
                    "chapter_id": chapter_id, "kind": "chapter_not_locked",
                    "q_no": None,
                    "detail": f"engine error: {e} -- nothing written for "
                              f"this chapter, retried on the next run",
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
                qp.write_chapters(chapters_path, chapters_out)
                qp.save_state(state)
                continue
            # crash-safe incremental checkpoint after EVERY chapter
            qp.write_chapters(chapters_path, chapters_out)
            qp.save_state(state)
            print(f"[{subject}] chapter {ch['chapter_no']} result: "
                  f"lock={res.get('locked')} committed={res.get('committed')} "
                  f"questions={res.get('questions')}")
        qp.build_subject_bundle(subject, chapters_out)

    qp.write_chapters(chapters_path, chapters_out)
    qp.save_state(state)
    # surgical re-runs can append duplicate rows; keep the newest per id.
    n_dups = qp._dedupe_questions_by_id(questions_path)
    if n_dups:
        print(f"Deduplicated {n_dups} stale duplicate row(s) from "
              f"questions.jsonl (newest extraction kept).")
    print("All done (or paused at daily limit -- just re-run this to resume).")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: boundary_phased.py <pdf> <subject> <chapter_no>")
        sys.exit(1)
    _state = qp.load_state()
    gemini_keys.init(_state, qp.MAX_CALLS_PER_DAY)
    res = run_chapter(sys.argv[1], sys.argv[2], int(sys.argv[3]),
                      qp.OUTPUT_ROOT, state=_state)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
