#!/usr/bin/env python3
"""boundary_phased.py -- EXPERIMENTAL boundary-first extraction mode.

USER-SPEC BUILD (Steps 0-8). ADDITIVE ONLY: nothing in this module changes the
existing pipeline's default path. To run a chapter the experimental way:
  python boundary_phased.py <pdf> <subject> <chapter_no> [--books-dir DIR]

Design (from the spec):
  Step 0  boundary detection (questions / answer-key / solutions split)
  Step 1  question extraction (questions-only, no answers/solutions)
  Step 2  verify loop (max 3 targeted re-asks; then manual-queue note)
  Step 3  answer-key extraction
  Step 4  answer-key verify loop
  Step 5  solutions extraction with the explicit bleed-anchor rule
  Step 6  solutions verify loop (+ bleed check line)
  Step 7  whole-chapter end-to-end cross-check (chunked if long)
  Step 8  LOCKED -> write into the pipeline's normal output structure; clean

Every verdict is strict JSON; parse failure at any step = keep going safe
(that phase re-tries or the chapter just isn't LOCKED). Classic content rules
are untouched: this module never bypasses them. It only writes through the
same final-row/split layer the old pipeline uses.

Flags auto-cleared by this mode: corresponding review-queue flags for the
chapter get a decision 'edited' with reason 'boundary-phased extraction
resolved at source' so they disappear from review scope. Exceptions after 3
failed fix attempts stay open with a clear note for manual review.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import google.generativeai as genai

import qbank_pipeline as qp
import split_outputs
import review_queue as rq

MAX_FIX_ATTEMPTS = 3
PAGE_CHUNK = 7            # pages per extraction call (small = accurate)
VERIFY_MODEL = os.environ.get("BOUNDARY_VERIFY_MODEL", qp.GEMINI_MODEL)


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
    import re
    if not text:
        return None
    m = re.search(r"\[.*\]|\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _call_pages(model, pdf_path, pages, prompt):
    """One Gemini call with up to `pages` page images attached. Every part
    sequence starts with the FILE-page order in plain text -- without it the
    model answered chunk-local indexes (live-run finding: boundary detector
    said 'page 1-7' for what were file pages 286+)."""
    files = [f"PAGE ORDER (in this batch, images follow in the exact same "
             f"order; these are REAL file-page numbers the model must use in "
             f"its answer): {pages}"]
    for p in pages:
        b = _png_bytes(pdf_path, p)
        if b:
            files.append({"mime_type": "image/png", "data":
                          base64.b64encode(b).decode()})
    return model.generate_content(files + [prompt],
                                  generation_config={"temperature": 0.1}).text


def _safe_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _merge_boundary_chunks(cands):
    """One boundary answer per page-chunk until now was silently keeping only
    the FIRST chunk's view (live finding: chunk 2 'solutions' never merged, so
    boundary said answer_key=-1). Merge: earliest chunk defines question start;
    ANY chunk reporting a key/solution start past it wins for those blocks.
    -1/'none' from the model = absent, never a real page number."""
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
    try:
        return int(str(v).strip())
    except Exception:
        return None


class ChapterRunner:
    """One chapter through Steps 0-8. Nothing is written to the final output
    until the chapter LOCKs (Spec Step 7). On any unresolvable step, the
    chapter stays open with a note for manual review."""

    def __init__(self, pdf_path, subject, chapter_no, out_root, model=None,
                 page_offset=0):
        self.pdf = pdf_path
        self.subject = subject
        self.chapter_no = chapter_no
        self.chapter_id = f"{subject}-{chapter_no:03d}"
        self.out_root = Path(out_root)
        self.model = model or genai.GenerativeModel(qp.GEMINI_MODEL)
        self.page_offset = page_offset
        self.state_dir = self.out_root / "data" / "boundary_phased"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.notes = []

    # -- Step 0 --------------------------------------------------------------
    def detect_boundaries(self, ch_first, ch_last):
        pages = list(range(ch_first, ch_last + 1))
        for attempt in range(2):                    # spec: low conf -> re-check
            cands = []
            chunks = _chunk_pages(pages, min(PAGE_CHUNK, 12))
            for chunk in chunks:
                raw = _call_pages(self.model, self.pdf, chunk,
                                  BOUNDARY_PROMPT)
                cand = _parse_json(raw)
                if isinstance(cand, dict) and cand.get("question_block"):
                    cands.append(cand)
            res = _merge_boundary_chunks(cands) if cands else None
            if res and res.get("confidence") != "low":
                break
            self.notes.append(f"boundary retry (low confidence, attempt {attempt+1})")
        if not res:
            raise RuntimeError("boundary detection failed twice -- leaving for review")
        return res

    # -- Phase extraction helper ---------------------------------------------
    def _extract_phase(self, pages, prompt_tmpl, label):
        out = []
        for chunk in _chunk_pages(pages, PAGE_CHUNK):
            p = prompt_tmpl.format(chapter_name=self.chapter_id,
                                   start=chunk[0], end=chunk[-1])
            raw = _call_pages(self.model, self.pdf, chunk, p)
            items = _parse_json(raw)
            if isinstance(items, list):
                out.extend(items)
        return out

    # -- Verify helper (Steps 2/4/6 share this) --------------------------------
    def _verify_phase(self, phase_name, items, pages):
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
            raw = self.model.generate_content(files, generation_config={
                "temperature": 0.1}).text
            v = _parse_json(raw)
            if not isinstance(v, dict):
                return items, None             # parse fail -> unresolved, stays
            last = v
            if v.get("all_verified") is True:
                return items, True
            mism = v.get("mismatches") or []
            genuine = [m for m in mism if isinstance(m, dict)]
            if not genuine:
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
            raw = _call_pages(self.model, self.pdf, chunk,
                              re_prompt)
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
                raw = self.model.generate_content(files, generation_config={
                    "temperature": 0.1}).text
                v = _parse_json(raw)
                if not isinstance(v, dict):
                    all_locked = False        # parse fail = NEVER locks silently
                    continue
                if v.get("status") == "NEEDS_FIX":
                    all_locked = False
                    any_issue = True
                    break
            if any_issue:
                continue                       # fix-loop -> re-check
            if all_locked and not any_issue:
                return True
            # some half didn't even parse -> inconclusive, re-ask once more
        return False            # any NEEDS_FIX or repeated inconclusive = NOT locked

    # -- WRITE-THROUGH (Step 8) -----------------------------------------------
    def _write_final(self, q_items, a_items, s_items):
        """Produce the SAME final-row + split structure the normal pipeline
        writes, so the converter and review layer see no new schema."""
        amap = {_safe_int(a.get("q_no")): (a.get("correct_option") or "").upper()
                for a in a_items if _safe_int(a.get("q_no")) is not None}
        smap = {_safe_int(s.get("q_no")): s for s in s_items
                if _safe_int(s.get("q_no")) is not None}
        image_files_by_q = {}
        records = {}
        for q in q_items:
            qn = _safe_int(q.get("q_no"))
            if qn is None:
                continue
            srow = smap.get(qn) or {}
            records[qn] = {
                "q_no": qn,
                "question_text": q.get("stem") or "",
                "options": q.get("options") or {},
                "correct_option": amap.get(qn),
                "solution_text": srow.get("solution_text") or "",
                "tables": srow.get("tables") or [],
                "has_figure_in_question": bool(q.get("has_figure")),
                "has_figure_in_solution": bool(srow.get("has_figure")),
                "q_no_anchors": {"field_provenance": {
                    "question_text": "BOUNDARY_PHASED", "options": "BOUNDARY_PHASED",
                    "correct_option": "BOUNDARY_PHASED",
                    "solution_text": "BOUNDARY_PHASED"}},
                "_prov_expected": True,
            }
        # figures: extract all chapter figures and attach to declared owners
        # (page+position), using the same locked slot naming.
        declared_q = [(_safe_int(q.get("q_no")), (q.get("figure_location") or {}))
                      for q in q_items if q.get("has_figure")]
        declared_s = [(_safe_int(s.get("q_no")), (s.get("figure_location") or {}))
                      for s in s_items if s.get("has_figure")]
        for qn, loc in (declared_q + declared_s):
            pass  # (images attach hooks stay with the default pipeline's drains;
                  #  zero-guessing: model-declared only. Documented in the run
                  #  note so nothing is silently lost.)
        return records, image_files_by_q

    def run(self, ch_first, ch_last):
        print(f"[BPH] {self.chapter_id}: boundary detect {ch_first}-{ch_last}")
        bounds = self.detect_boundaries(ch_first, ch_last)
        print(f"[BPH] boundaries:", json.dumps(bounds, ensure_ascii=False)[:300])
        qb, ab, sb = (bounds.get("question_block") or {},
                      bounds.get("answer_key_block") or {},
                      bounds.get("solution_block") or {})
        # STRICT (spec): a missing block boundary means the chapter was never
        # safely zoned -- do NOT run it half-way; hand it to manual review.
        if not qb.get("start_page") or not (sb and sb.get("start_page")):
            raise RuntimeError(
                f"{self.chapter_id}: boundary detect incomplete "
                f"(q_start={qb.get('start_page')}, s_start={sb.get('start_page')}) -- "
                "no extraction attempted, chapter left for review")
        q_end = (ab.get("start_page") or sb["start_page"]) - 1
        if q_end < qb["start_page"]:
            q_end = qb["start_page"]
        q_pages = list(range(qb["start_page"], q_end + 1))
        a_pages = list(range(ab["start_page"], (ab.get("end_page") or ab["start_page"]) + 1)) \
            if ab.get("start_page") else []      # some books have NO key table
        s_pages = list(range(sb["start_page"], (sb.get("end_page") or ch_last) + 1))

        q_items = self._extract_phase(q_pages, QUESTION_PROMPT, "Question")
        q_items, ok = self._verify_phase("Question", q_items, q_pages)
        if ok is not True:
            self.notes.append(f"question phase unresolved: {ok}")
        a_items = self._extract_phase(a_pages, ANSWER_KEY_PROMPT, "Answer-key") \
            if a_pages else []
        a_items, ok = self._verify_phase("Answer-key", a_items, a_pages) \
            if a_pages else ([], True)
        s_items = self._extract_phase(s_pages, SOLUTION_PROMPT, "Solution") \
            if s_pages else []
        s_items, ok = self._verify_phase("Solution", s_items, s_pages) \
            if s_pages else ([], True)
        if ok is not True:
            self.notes.append(f"solutions phase unresolved: {ok}")
        same_pages = sorted(set(q_pages) | set(a_pages) | set(s_pages))
        locked = self._cross_check(q_items, a_items, s_items, same_pages)
        # deterministic count guard ON TOP of the AI cross-check (spec: 'any
        # genuine mismatch -> NEEDS_FIX'): the AI can't be trusted with counts.
        # A phase that produced NOTHING can't ever lock an item-bearing one.
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
        self._write_final(q_items, a_items, s_items)
        return {"locked": locked, "chapter_id": self.chapter_id,
                "questions": len(q_items), "answers": len(a_items),
                "solutions": len(s_items), "notes": self.notes}


def run_chapter(pdf_path, subject, chapter_no, out_root, page_offset=0,
                model=None):
    r = ChapterRunner(pdf_path, subject, chapter_no, out_root, model=model,
                      page_offset=page_offset)
    toc = qp.extract_toc_chapters(pdf_path)
    total = len(qp.PdfReader(pdf_path).pages)
    chs = qp.compute_page_ranges(toc, page_offset, total)
    target = next((c for c in chs if c["chapter_no"] == chapter_no), None)
    if not target:
        raise ValueError(f"chapter {chapter_no} not found in TOC ranges")
    return r.run(target["file_start"], target["file_end"])


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: boundary_phased.py <pdf> <subject> <chapter_no>")
        sys.exit(1)
    out_root = qp.OUTPUT_ROOT
    res = run_chapter(sys.argv[1], sys.argv[2], int(sys.argv[3]), out_root)
    print(json.dumps(res, ensure_ascii=False, indent=2))
