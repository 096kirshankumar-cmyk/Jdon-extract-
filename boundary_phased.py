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


class ModelBlocked(Exception):
    """Gemini returned NO usable content (finish_reason 4: 'reciting from
    copyrighted material' / prohibited-content refusal). _gen retries once,
    then raises this typed exception; every caller converts it into a ledger
    UNRESOLVED row + a 'phase_unresolved' note so the chapter FLAGS instead
    of dying (OBG-007 live: a raw ValueError from resp.text killed the whole
    chapter mid-question-phase)."""


MODEL_BLOCK_SLEEP = 15      # backoff before the one bounded re-ask of a block


def _resp_text(resp):
    """resp.text raises ValueError when the candidate carries no Part
    (finish_reason 4). Normalize that shape to the typed ModelBlocked."""
    try:
        return resp.text
    except ValueError:
        raise ModelBlocked()


def _png_bytes(pdf_path, page, dpi=110):
    pre = Path(tempfile.mkdtemp(prefix="bph_")) / "p"
    subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r",
                    str(dpi), "-png", "-singlefile", str(pdf_path), str(pre)],
                   capture_output=True, timeout=45)
    out = pre.with_suffix(".png")
    return out.read_bytes() if out.exists() else None


def _chunk_pages(pages, n):
    return [pages[i:i + n] for i in range(0, len(pages), n)]


def _ocr_page_text(pdf_path, page, dpi=300):
    """Deterministic OCR of one PDF page (tesseract). Used when the model's
    recitation filter blocks the page IMAGE (finish_reason 4): the printed
    TEXT is still available and can be structured -- every such item is
    flagged REVIEW_NEEDED downstream, never silently trusted."""
    pre = Path(tempfile.mkdtemp(prefix="bph_ocr_")) / "p"
    subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r",
                    str(dpi), "-png", "-singlefile", str(pdf_path), str(pre)],
                   capture_output=True, timeout=90)
    png = pre.with_suffix(".png")
    if not png.exists():
        return None
    try:
        r = subprocess.run(["tesseract", str(png), "-", "--psm", "6"],
                           capture_output=True, text=True, timeout=120)
        return r.stdout or None
    except Exception:
        return None


def _phase_chunks(pages, size=PAGE_CHUNK, overlap=1):
    """Phase-extraction chunking with a 1-page overlap. A printed header at
    the BOTTOM of a chunk's last page otherwise loses its body (OPH-001 live:
    'Solution to Question 15:' sat at the foot of p18 = chunk [12-18]'s edge;
    chunk [19-23] held the body, which the bleed-anchor rule then refused to
    attach). With the overlap every header is mid-chunk in at least one call;
    _merge_phase_items dedupes the shared page's items."""
    pages = list(pages)
    if len(pages) <= size:
        return [pages]
    step = max(1, size - overlap)
    chunks = [pages[i:i + size] for i in range(0, len(pages), step)]
    return [c for c in chunks if c]


_Q_NO_ALIASES = ("q_no", "question_number", "question_no", "qno",
                 "q_number", "number")


def _item_qn(it):
    """q_no arrives under whichever key the model fancied this call
    (live finding: a re-ask answered 'question_number' and the item was
    silently discarded). Identity-field aliases only -- content fields stay
    spec-strict."""
    if not isinstance(it, dict):
        return None
    for k in _Q_NO_ALIASES:
        if it.get(k) is not None:
            return _norm_q_no(it.get(k))
    return None


def _item_qno_raw(it):
    if not isinstance(it, dict):
        return None
    for k in _Q_NO_ALIASES:
        if it.get(k) is not None:
            return it.get(k)
    return None


# ---------------------------------------------------------------------------
# OPTIONS SHAPE NORMALIZATION
# ---------------------------------------------------------------------------
# LIVE finding (OBG ch2, 2026-08-22): the model answered the question phase
# with "options" as a LIST (e.g. ["a) ...", "b) ..."]) instead of the spec's
# {"A": ..., "B": ...} object. _build_records did {}.items() on it and the
# WHOLE CHAPTER died with AttributeError 'list' object has no attribute
# 'items'. Options are now normalized here; letters are taken from the model's
# own output when present, and position-assigned ONLY with an explicit
# suspect flag -- content is never dropped and never silently trusted.

_OPT_LETTER_RE = re.compile(
    r"^[\(（\[]?\s*([A-Ha-h])\s*[\)\]\.、:：\-–—>]\s*(.*)$", re.S)
_OPT_TEXT_KEYS = ("text", "value", "content", "option_text", "label_text",
                  "description", "option")


def _norm_options(raw):
    """-> (opts_dict {LETTER: text}, issue_note).

    issue_note is "" when the shape was the spec dict (or a fully lettered
    list). Non-empty notes mean the row MUST be flagged (options_suspect /
    review_reasons) -- this function never invents letters silently.
    """
    if raw is None:
        return {}, "options field missing"
    if isinstance(raw, dict):
        opts, issues = {}, []
        for k, v in raw.items():
            letter = str(k).strip().upper()
            if re.fullmatch(r"[A-H]", letter):
                opts.setdefault(letter, str(v or "").strip())
            else:
                issues.append(f"non-letter key {str(k)[:14]!r} ignored")
        if not opts:
            return {}, "options dict has no A-H keys"
        return opts, "; ".join(issues)
    if isinstance(raw, list):
        opts, issues = {}, []
        for i, item in enumerate(raw):
            letter, text = None, None
            if isinstance(item, str):
                m = _OPT_LETTER_RE.match(item.strip())
                if m:
                    letter, text = m.group(1).upper(), m.group(2).strip()
                else:
                    text = item.strip()
                    issues.append(f"option {i + 1} has no leading letter")
            elif isinstance(item, dict):
                for k in ("letter", "label", "option", "key", "id"):
                    if item.get(k) is not None:
                        mv = _OPT_LETTER_RE.match(str(item[k]).strip())
                        if mv:
                            letter = mv.group(1).upper()
                        elif re.fullmatch(r"[A-Ha-h]",
                                          str(item[k]).strip()):
                            letter = str(item[k]).strip().upper()
                        break
                for k in _OPT_TEXT_KEYS:
                    if item.get(k):
                        text = str(item[k]).strip()
                        break
                if text is None:
                    for k, v in item.items():
                        if re.fullmatch(r"[A-Ha-h]", str(k).strip()):
                            letter = str(k).strip().upper()
                            text = str(v or "").strip()
                            break
                if text is None:
                    issues.append(f"option {i + 1} dict has no text field")
                    continue
            else:
                issues.append(f"option {i + 1} is {type(item).__name__} "
                              f"(not str/dict)")
                continue
            if letter is None:
                letter = chr(ord("A") + i) if i < 8 else f"X{i}"
                issues.append(f"option {i + 1} unlettered -> position {letter}")
            if letter in opts and opts[letter] and opts[letter] != text:
                issues.append(f"duplicate option letter {letter}")
            opts[letter] = text or ""
        return opts, "; ".join(issues)
    return {}, f"options field is {type(raw).__name__} (unexpected)"


# ---------------------------------------------------------------------------
# CONTENT-FIELD ALIAS INTAKE (per phase)
# ---------------------------------------------------------------------------
# LIVE finding (OBG ch2, 2026-08-22): a targeted-fix re-ask came back as
# {"solutions": [{"question_number": "1", "text": "...",
# "image_description": ...}]} -- q_no ALIAS + content under "text" instead of
# "solution_text". Whole-item replacement then WIPED the good solution_text
# from 11 rows (INCOMPLETE + no LOCK). Values are the model's own words under
# a different key; we re-key only -- never rewrite, never invent. The SPEC
# key always wins when both exist.

_SOL_TEXT_ALIASES = ("solution_text", "text", "explanation", "solution",
                     "answer_explanation", "explanation_text",
                     "detailed_solution", "content", "answer_text")
_Q_STEM_ALIASES = ("stem", "question_text", "text", "question")
_A_ANS_ALIASES = ("correct_option", "answer", "correct_answer", "option",
                  "answer_letter", "correct_letter")
_HAS_FIG_ALIASES = ("has_figure", "has_image", "figure_present")
_FIG_LOC_ALIASES = ("figure_location", "image_location")


def _alias_field(it, aliases, canon):
    """Move the first alias value into `canon` when canon is empty; the
    alias keys are then removed so downstream never double-reads them."""
    for k in aliases[1:]:
        if k in it:
            if not it.get(canon):
                it[canon] = it[k]
            del it[k]


def _normalize_phase_item(phase_name, it):
    """Re-key model content into the spec's field names per phase. Leaves
    everything else untouched; unknown junk keys survive (harmless)."""
    if not isinstance(it, dict):
        return it
    if phase_name == "Solution":
        _alias_field(it, _SOL_TEXT_ALIASES, "solution_text")
        _alias_field(it, _HAS_FIG_ALIASES, "has_figure")
        _alias_field(it, _FIG_LOC_ALIASES, "figure_location")
    elif phase_name == "Question":
        _alias_field(it, _Q_STEM_ALIASES, "stem")
        _alias_field(it, _HAS_FIG_ALIASES, "has_figure")
        _alias_field(it, _FIG_LOC_ALIASES, "figure_location")
    elif phase_name == "Answer-key":
        _alias_field(it, _A_ANS_ALIASES, "correct_option")
    return it


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
- options hamesha OBJECT (map) me do: {{"A": "...", "B": "...", "C": "...", 
  "D": "..."}} — LIST kabhi nahi. Printed letters lowercase (a/b/c/d) hain to 
  bhi unhe uppercase letter keys me daalo.
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

# INLINE-ANSWER micro-phase (spec extension, zero-guessing): ONLY used when
# neither the model boundary nor the printed probes found an answer-key
# table -- i.e. books that print 'Ans: B' right under the options. Without
# it those chapters must NOT lock (every correct_option would be None).
INLINE_ANSWER_PROMPT = """Tumhe {chapter_name} ke pages {start}–{end} diye jayenge. Is chapter me 
har question ke options ke neeche/paas answer INLINE printed hai (jaise 
"Ans: B" ya "Answer - C") — koi alag answer-key table nahi hai.

Task: SIRF wo printed inline answer markers nikaalo — har question ka number 
aur uske paas printed letter.

STRICT rules:
- Jo letter page pe physically printed hai, SIRF wahi lo. Content padh kar 
  khud answer mat nikaalo — printed marker nahi dikhe to us question ko 
  list me hi mat daalo.
- "low_confidence: true" jab marker dhundhla/adhoora ho; guess mat thopo.

ONLY respond in this JSON format:
[
  {{"q_no": "<as printed>", "correct_option": "A/B/C/D", "low_confidence": true/false}}
]"""

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


_WRAPPER_KEYS = ("solutions", "items", "results", "questions", "answers",
                 "entries")


def _unwrap_items(v):
    """Phase responses must be a JSON ARRAY. The model sometimes wraps them
    (OBG ch2 live: {"solutions": [{"question_number": ...}]}). _parse_json
    returns the whole dict in that case; here we extract the array from
    known wrapper keys -- structural only, zero guessing."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        for k in _WRAPPER_KEYS:
            if isinstance(v.get(k), list):
                return v[k]
    return None


def _safe_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


_QNO_NUM_RE = re.compile(r"\d+")


def _norm_q_no(v):
    """Model q_nos arrive as printed ('Question 4:', '4.', 'Q4'). The NUMBER
    is the identity; the decoration is not."""
    if v is None:
        return None
    m = _QNO_NUM_RE.search(str(v))
    return _safe_int(m.group(0)) if m else None


def _is_continuation_marker(v):
    """'4 (cont.)' / 'Question 4 (continued)' = the SAME question's spill
    onto the next page, not a new question."""
    return bool(re.search(r"\(\s*cont(?:inued|\.|\))", str(v or ""), re.I))


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
        self._zones = None             # set by run(): {"q":[], "a":[], "s":[]}
        self._printed_q_hdrs = {}      # page -> {qn}: printed Question headers
        self._printed_q_max = None     # highest PRINTED 'Question N:' of this
                                       # chapter (FIX B phantom-q_no guard)
        self._printed_s_hdrs = {}      # page -> {qn}: printed Solution headers

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
            return _resp_text(resp)
        except ModelBlocked:
            time.sleep(MODEL_BLOCK_SLEEP)      # one bounded retry: a
            resp = self.model.generate_content(   # recitation/safety block
                files, safety_settings=qp.SAFETY_SETTINGS)  # is prompt-roll bound
            qp.note_call(self.state)
            return _resp_text(resp)            # still blocked -> raise through
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
                return _resp_text(resp)
            raise

    def _call_pages(self, pages, prompt, dpi=110):
        """One call with page images attached. Every call leads with the
        FILE-page order in plain text -- without it the model answered
        chunk-local indexes (live-run finding: the boundary detector said
        'page 1-7' for what were file pages 286+)."""
        files = [f"PAGE ORDER (in this batch, images follow in the exact same "
                 f"order; these are REAL file-page numbers the model must use in "
                 f"its answer): {pages}"]
        for p in pages:
            b = _png_bytes(self.pdf, p, dpi=dpi)
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
                try:
                    raw = self._call_pages(chunk, BOUNDARY_PROMPT)
                except ModelBlocked:
                    self._ledger("BOUNDARY", chunk, qp.PASS_STATUS_UNRESOLVED,
                                 0, "model blocked (recitation/safety), "
                                    "retried once")
                    self.notes.append(f"boundary detect unresolved: model "
                                      f"blocked pages {chunk[0]}-{chunk[-1]}")
                    continue
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
    def _extract_phase(self, pages, prompt_tmpl, label, pass_name, dpi=110):
        out = []
        for chunk in _phase_chunks(pages):
            p = prompt_tmpl.format(chapter_name=self.chapter_id,
                                   start=chunk[0], end=chunk[-1])
            items = None
            for reask in range(2):          # one bounded re-ask on bad JSON
                try:
                    raw = self._call_pages(chunk, p, dpi=dpi)
                except ModelBlocked:
                    self._ledger(pass_name, chunk, qp.PASS_STATUS_UNRESOLVED,
                                 0, "model blocked (recitation/safety), "
                                    "retried once")
                    self.notes.append(f"{pass_name} phase unresolved: model "
                                      f"blocked chunk {chunk[0]}-{chunk[-1]}")
                    # ROOT FIX (OBG ch7 live): the block is on the page IMAGE,
                    # not the content. OCR the same pages (deterministic) and
                    # structure the text with the same phase rules. Items are
                    # marked _ocr -> REVIEW_NEEDED rows, never trusted silent.
                    ocr_kept = self._ocr_fallback(chunk, prompt_tmpl, label,
                                                  pass_name)
                    if ocr_kept:
                        out.extend(ocr_kept)
                        items = ocr_kept
                    break
                items = _unwrap_items(_parse_json(raw))
                if isinstance(items, list):
                    break
                self.notes.append(f"{pass_name} chunk {chunk[0]}-{chunk[-1]}: "
                                  f"unparsable JSON, re-ask {reask + 1}/1")
            if isinstance(items, list):
                kept, dropped = [], 0
                for it in items:
                    qn0 = _item_qn(it)
                    if isinstance(it, dict) and qn0 is not None:
                        _normalize_phase_item(label, it)
                        it["_qn"] = qn0
                        it["_continuation"] = _is_continuation_marker(
                            _item_qno_raw(it))
                        # FIX B: phantom q_no (RAD-002-026 live -- page number
                        # misread as a question number). Only applies when the
                        # text layer PROVED the chapter's printed question
                        # ceiling. Never silently dropped: orphaned + noted.
                        if pass_name == "Q" \
                                and self._printed_q_max is not None \
                                and qn0 > self._printed_q_max:
                            dropped += 1
                            self.orphan_items.append({
                                "chapter_id": self.chapter_id,
                                "batch_start": chunk[0],
                                "pdf_pages": list(chunk),
                                "reason": f"phantom_q_no {qn0} > printed "
                                          f"question max "
                                          f"{self._printed_q_max}",
                                "pass": "Q_PHANTOM_FILTER",
                                "item": it})
                            self.notes.append(
                                f"Q phase: dropped phantom q_no {qn0} "
                                f"(chapter prints only up to "
                                f"{self._printed_q_max}) -- moved to "
                                f"orphans for review")
                            continue
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
                # STRICT: an unparsable phase chunk is NEVER a silent zero,
                # even after the one re-ask.
                self._ledger(pass_name, chunk, qp.PASS_STATUS_UNRESOLVED, 0,
                             "phase chunk returned no parseable JSON array "
                             "(1 re-ask also failed)")
        return self._merge_phase_items(out)

    def _ocr_fallback(self, chunk, prompt_tmpl, label, pass_name):
        """Model recitation-blocked the page IMAGEs of `chunk` -> OCR the
        pages with tesseract (deterministic, no model) and structure the OCR
        TEXT with the SAME phase rules + JSON format. Every item is marked
        `_ocr`; `_build_records` turns that into a review reason so the row
        is REVIEW_NEEDED and the chapter never silently trusts OCR text.
        Returns [] when nothing usable came out (page left unresolved)."""
        texts = {}
        for p in chunk:
            t = _ocr_page_text(self.pdf, p)
            if t and t.strip():
                texts[p] = t.strip()
        if not texts:
            self.notes.append(f"{label} OCR fallback produced no text for "
                              f"pages {chunk[0]}-{chunk[-1]}")
            return []
        pages = sorted(texts)
        if "ONLY respond" in prompt_tmpl:
            rules, fmt = prompt_tmpl.split("ONLY respond", 1)
        else:
            rules, fmt = prompt_tmpl, ""
        ocr_prompt = (
            f"NOTE: Pages {pages[0]}-{pages[-1]} ke images is call me NAHI "
            f"hain (model content-filter ne block kiya) -- neeche unka OCR "
            f"text diya hai. Task wahi hai. OCR galat letter/word de sakta "
            f"hai: jo clearly sahi nahi lagta hai usko "
            f"\"text_confidence\": \"low\" mark karo, skip mat karo.\n\n"
            + rules.strip()
            + f"\n\nOCR TEXT (pages {pages[0]}-{pages[-1]}):\n"
            + "\n\n".join(f"--- page {p} ---\n{texts[p]}" for p in pages)
            + "\n\nONLY respond" + fmt)
        try:
            raw = self._gen([ocr_prompt])
        except ModelBlocked:
            self.notes.append(f"{label} OCR fallback ALSO blocked for pages "
                              f"{chunk[0]}-{chunk[-1]} -- left unresolved")
            return []
        items = _unwrap_items(_parse_json(raw))
        if not isinstance(items, list):
            self.notes.append(f"{label} OCR fallback unparsable for pages "
                              f"{chunk[0]}-{chunk[-1]}")
            return []
        kept = []
        for it in items:
            if not isinstance(it, dict):
                continue
            qn0 = _item_qn(it)
            if qn0 is None:
                self.orphan_items.append({
                    "chapter_id": self.chapter_id, "batch_start": chunk[0],
                    "pdf_pages": list(chunk),
                    "reason": "ocr_fallback_item_no_q_no",
                    "pass": f"OCR_{pass_name}", "item": it})
                continue
            _normalize_phase_item(label, it)
            it["_qn"] = qn0
            it["_ocr"] = True
            it["_continuation"] = _is_continuation_marker(_item_qno_raw(it))
            kept.append(it)
        self._ledger(f"OCR_{pass_name}", pages, qp.PASS_STATUS_PARTIAL,
                     len(kept),
                     "image blocked (recitation filter); structured from "
                     "OCR text -- rows flagged for manual review")
        self.notes.append(f"{label} phase: images blocked on {pages} -> OCR "
                          f"fallback used ({len(kept)} item(s)); verify "
                          f"text manually")
        return kept

    @staticmethod
    def _merge_phase_items(items):
        """Duplicate/continuation safety at intake: two chunks (or an honest
        '4 (cont.)' spill) can name the same q_no. MERGE them fill-only --
        existing non-empty fields win, options extend, pages union. No value
        is ever silently overwritten (conflicts go to the verify loop via
        text_confidence); nothing is dropped."""
        by_qn = {}
        order = []
        for it in items:
            qn = it.get("_qn")
            if qn is None:
                continue
            if qn not in by_qn:
                by_qn[qn] = dict(it)
                order.append(qn)
                continue
            base = by_qn[qn]
            for k, v in it.items():
                if k in ("_qn", "_continuation"):
                    continue
                if k == "options":
                    merged, _ = _norm_options(base.get("options"))
                    for letter, text in _norm_options(v)[0].items():
                        if not merged.get(letter):
                            merged[letter] = text
                    base["options"] = merged
                elif k == "source_page_range":
                    lo = (base.get("source_page_range") or [None])[0]
                    hi = (base.get("source_page_range") or [None, None])[-1]
                    for p in (v or []):
                        lo = p if lo is None else min(lo, p)
                        hi = p if hi is None else max(hi, p)
                    base["source_page_range"] = [x for x in (lo, hi)
                                                 if x is not None]
                elif not base.get(k) and v not in (None, "", [], {}):
                    base[k] = v                     # fill-only
            if it.get("_continuation"):
                base["_seen_continuation"] = True
        return [by_qn[q] for q in order]

    # -- Verify helper (Steps 2/4/6 share this) --------------------------------
    def _verify_phase(self, phase_name, items, pages, dpi=110):
        if not pages:
            return items, True
        bleed_line = BLEED_LINE if phase_name == "Solution" else ""
        prompt = VERIFY_PROMPT.format(phase_name=phase_name, bleed_line=bleed_line)
        payload = json.dumps(items, ensure_ascii=False, default=str)
        last = None
        for attempt in range(MAX_FIX_ATTEMPTS):
            files = [payload]
            for p in pages:
                b = _png_bytes(self.pdf, p, dpi=dpi)
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
        # keep the ORIGINAL phase rules in view -- a bare 'same rules' re-ask
        # quietly shape-drifts (live finding: fixed items came back with
        # different fields when the format imprint was missing)
        phase_hint = {"Question": QUESTION_PROMPT, "Answer-key": ANSWER_KEY_PROMPT,
                      "Solution": SOLUTION_PROMPT}.get(phase_name, "")
        fix_pages = sorted(self._phase_pages_of(phase_name, items))
        # FULL original prompt stays in view -- JSON template included. A
        # re-ask that drops the format block shape-drifts (OBG ch2 live: came
        # back {"solutions": [{"question_number": 1, "text": ...}]} which
        # then WIPED 11 good solution_texts on whole-item replacement).
        for chunk in _chunk_pages(fix_pages, PAGE_CHUNK):
            if not chunk:
                continue
            re_prompt = (
                phase_hint.format(chapter_name=self.chapter_id,
                                  start=chunk[0], end=chunk[-1])
                + f"\n\nNOTE: Ab SIRF in question numbers ko dobara extract "
                  f"karo: {qns}. Baaki sab ignore karo. WAHI JSON array "
                  f"format use karo jo upar diya hai.")
            try:
                raw = self._call_pages(chunk, re_prompt)
            except ModelBlocked:
                self.notes.append(f"{phase_name} targeted-fix unresolved: "
                                  f"model blocked pages {chunk[0]}-{chunk[-1]}")
                continue                 # keep pre-fix items; flag wins later
            items_fixed = _unwrap_items(_parse_json(raw))
            if isinstance(items_fixed, list):
                by_no = {str(i.get("_qn")): i for i in items}
                for it in items_fixed:
                    if not isinstance(it, dict):
                        continue
                    _normalize_phase_item(phase_name, it)
                    it["_qn"] = _item_qn(it)
                    if it["_qn"] is None:
                        continue
                    by_no[str(it["_qn"])] = it
                items = [by_no[k] for k in by_no]
        return self._merge_phase_items(items)

    def _phase_pages_of(self, phase_name, items):
        """Which FILE pages a phase's items actually live on (phase-aware:
        questions carry source_page, solutions carry source_page_range,
        answer-key rows know neither so they fall back to the A zone)."""
        pages = set()
        for i in items:
            sp = _safe_int(i.get("source_page"))
            if sp is not None:
                pages.add(sp)
            for p in (i.get("source_page_range") or [])[:2]:
                if _safe_int(p) is not None:
                    pages.add(_safe_int(p))
        if pages:
            return pages
        return set(self._zones.get("a", [])) if self._zones else set()

    # -- Step 7: chapter cross-check (with one bounded targeted-fix round) ----
    def _cross_check(self, q_items, a_items, s_items, pages):
        def pack_now():
            return json.dumps({"chapter": self.chapter_id,
                               "questions": q_items, "answer_key": a_items,
                               "solutions": s_items},
                              ensure_ascii=False, default=str)[:12000]
        phase_map = {"question": (q_items, "Question"),
                     "answer_key": (a_items, "Answer-key"),
                     "solution": (s_items, "Solution"),
                     "solutions": (s_items, "Solution")}
        halves = [pages[:len(pages) // 2], pages[len(pages) // 2:]] \
            if len(pages) > 12 else [pages]
        for _ in range(MAX_FIX_ATTEMPTS):
            all_locked = True
            issues_seen = []
            for half in halves:
                if not half:
                    continue
                files = [pack_now()]
                for p in half:
                    b = _png_bytes(self.pdf, p)
                    if b:
                        files.append({"mime_type": "image/png", "data":
                                      base64.b64encode(b).decode()})
                files.append(CHAPTER_FINAL_PROMPT.format(chapter_name=self.chapter_id))
                try:
                    raw = self._gen(files)
                except ModelBlocked:
                    self.notes.append(f"cross-check unresolved: model blocked "
                                      f"(recitation/safety) on pages "
                                      f"{half[0]}-{half[-1]}")
                    all_locked = False
                    continue
                v = _parse_json(raw)
                # STRICT: ONLY an explicit "LOCKED" counts. A parse failure,
                # an empty dict, or any missing/other status are all NO-VOTE
                # -- a chapter never locks on silence (live finding: the
                # '{}' empty-response shape silently 'passed' a half).
                if not isinstance(v, dict) or v.get("status") not in (
                        "LOCKED", "NEEDS_FIX"):
                    all_locked = False
                    continue
                if v.get("status") == "NEEDS_FIX":
                    all_locked = False
                    for iss in (v.get("issues") or []):
                        if isinstance(iss, dict) and iss.get("issue"):
                            issues_seen.append(iss)
                            self.notes.append(
                                f"cross-check q{iss.get('q_no')}: {iss['issue']}")
            if all_locked:
                return True
            if not issues_seen:
                continue            # inconclusive parse(s) -> just re-ask
            # fix-loop: re-ask ONLY the flagged q_nos inside their own phase
            # (mutation in place so the caller's lists stay the same objects)
            for iss in issues_seen:
                pair = phase_map.get(str(iss.get("block") or "").lower())
                qn = _norm_q_no(iss.get("q_no"))
                if not pair or qn is None:
                    continue
                items, phase_name = pair
                fixed = self._targeted_fix(phase_name, list(items),
                                           [{"q_no": str(qn),
                                             "issue": iss["issue"]}])
                items[:] = fixed
        return False            # any NEEDS_FIX or repeated inconclusive = NOT locked

    # -- Deterministic zone validation (ZERO tokens, printed evidence) --------
    # The boundary MODEL zones the chapter, but pages are ground truth. The
    # OBG ch3 live run: the model read an interleaved chapter as clean blocks
    # and zoned the answer key 10 pages late (said p62; the grid is on p52).
    # Rule: when the text layer PRINTS question/solution headers or a key
    # grid, those PRINTED spans override the model's zones; when the text
    # layer is empty (scanned book), the model's zones are the only signal
    # and stand.
    _QH_TXT = re.compile(r"(?im)^\s*question\s+(\d{1,3})\s*[:\.]")
    _SH_TXT = re.compile(r"(?im)solution\s+to\s+question\s+(\d{1,3})"
                         r"|^\s*detailed\s+explanations")
    _KEYROW_TXT = re.compile(r"(?im)^\s*(\d{1,3})\s*[\.\)\:\-]?\s*"
                             r"\(?([A-Ea-e])\)?\s*$")
    # the answer-key table's printed header lines -- required proof before a
    # LONE row on the previous page is trusted as the grid's start (FIX A).
    # NOTE: books print 'Question No.   Correct Option' on ONE line, so the
    # 'correct option' probe must not be anchored to a line start.
    _KEYHEAD_QNO_TXT = re.compile(r"(?i)\bquestion\s+no\.?")
    _KEYHEAD_COR_TXT = re.compile(r"(?i)\bcorrect\s+option\b")
    _INLINE_ANS_TXT = re.compile(r"(?im)\bans(?:wer)?\b\s*[:\-\.]?\s*"
                                 r"\(?([A-Ea-e])\)?\b")

    def _printed_zones(self, ch_first, ch_last):
        q_pages, s_pages, key_cands = set(), set(), []
        key_max = {}
        # per-QUESTION printed headers too: page -> {qn}. This is what makes a
        # model DROPPED block recoverable deterministically (the page PROVES
        # 'Solution to Question 15' exists even when the model skipped q15).
        q_hdrs, s_hdrs = {}, {}
        read_pages = 0
        for p in range(ch_first, ch_last + 1):
            try:
                txt = qp.pdftotext_page(self.pdf, p) or ""
            except Exception:
                txt = ""
            if not txt.strip():
                continue                      # scanned page: no opinion
            read_pages += 1
            qh = {int(m.group(1)) for m in self._QH_TXT.finditer(txt)}
            sh = {int(m.group(1)) for m in self._SH_TXT.finditer(txt)
                  if m.group(1)}
            if qh:
                q_pages.add(p)
                q_hdrs[p] = qh
            if sh or self._SH_TXT.search(txt):
                s_pages.add(p)
                if sh:
                    s_hdrs[p] = sh
            rows = [(int(m.group(1)), m.group(2).upper())
                    for m in self._KEYROW_TXT.finditer(txt)]
            nums = [n for n, _ in rows]
            # a key grid = MANY sequential rows (a two-item list is not one)
            if len(rows) >= 6 and len(set(nums)) >= 6 \
                    and max(nums) - min(nums) <= 3 * len(nums):
                key_cands.append(p)
                key_max[p] = max(nums)
        # OBG ch2 live: the key grid's rows 1-14 sit on p37 and its last row
        # "15 b" prints at the TOP of the NEXT page (p38) before 'Detailed
        # Explanations'. One lone row is not a grid -- but a row numbered
        # EXACTLY max(grid)+1 as the next page's first content line is the
        # grid's continuation; include that page.
        if key_cands and key_cands[-1] < ch_last:
            nxt = key_cands[-1] + 1
            try:
                nxt_txt = qp.pdftotext_page(self.pdf, nxt) or ""
            except Exception:
                nxt_txt = ""
            first = next((ln.strip() for ln in nxt_txt.splitlines()
                          if ln.strip()), "")
            m = self._KEYROW_TXT.match(first) if first else None
            if m is not None and int(m.group(1)) == key_max.get(
                    key_cands[-1], -1) + 1:
                key_cands.append(nxt)
        # BACKWARD: a grid can also START mid-page on the page just BEFORE
        # the first "big" grid page (OBG ch8 live: p149 = Question 15 + grid
        # header + rows 1-4; p150 = rows 5-15 then solutions). P-1 holding
        # consecutive rows 1..k whose next number k+1 is the FIRST row of the
        # big grid page is the grid's start page -- include it or the first
        # k questions lose their answers.
        # FIX A (RAD-002 / ANAT-001 live): k can be as small as ONE -- the
        # header + row '1 <letter>' sits at the foot of the last question
        # page, rows 2..N on the next page. A lone row is only trusted when
        # that page also prints the key-table header line (Question No. /
        # Correct Option), so an unrelated '1 a' line can't be misread.
        if key_cands:
            first_grid = key_cands[0]
            prev = first_grid - 1
            if prev >= ch_first:
                try:
                    prev_txt = qp.pdftotext_page(self.pdf, prev) or ""
                except Exception:
                    prev_txt = ""
                prow = sorted({int(m.group(1)) for m in
                               self._KEYROW_TXT.finditer(prev_txt)})
                if prow and prow == list(range(1, len(prow) + 1)) \
                        and len(prow) >= 1:
                    if len(prow) == 1 and not (
                            self._KEYHEAD_QNO_TXT.search(prev_txt)
                            and self._KEYHEAD_COR_TXT.search(prev_txt)):
                        prow = []          # lone row without table header: no
                                           # opinion (avoid false positives)
                    try:
                        cur_txt = qp.pdftotext_page(self.pdf,
                                                    first_grid) or ""
                    except Exception:
                        cur_txt = ""
                    cnums = [int(m.group(1)) for m in
                             self._KEYROW_TXT.finditer(cur_txt)]
                    if prow and cnums and min(cnums) == max(prow) + 1:
                        key_cands.insert(0, prev)
                        key_max[prev] = max(prow)
        if read_pages == 0:
            return None                       # scanned book: no printed signal
        self._printed_q_hdrs = q_hdrs
        self._printed_s_hdrs = s_hdrs
        # FIX B phantom-q_no guard: the highest number this chapter actually
        # prints as a 'Question N:' header is the ceiling of the real
        # question set. Anything the model reports ABOVE it (RAD-002-026
        # live: the page number '26' read as a question number) cannot
        # exist in the chapter and is orphaned at intake, never shipped.
        self._printed_q_max = max((max(v) for v in q_hdrs.values()),
                                  default=None) if q_hdrs else None
        return {"q": q_pages, "s": s_pages, "keys": key_cands}

    def _printed_header_reask(self, phase_name, items, zone_pages, hdr_attr,
                              prompt_tmpl, dpi=110):
        """A phase's model read can silently DROP a question/solution whose
        header is PRINTED (OPH-001 live: q15's solution exists on the page
        but never came back, twice). The text-layer header is hard proof of
        existence, so re-ask EXACTLY those q_nos on EXACTLY those pages once.
        Zero guessing: this only ever ADDS a block the book provably prints."""
        hdrs = getattr(self, hdr_attr, None)
        if not hdrs:
            return items
        # OBG-010-021 live: the model returned ONLY the printed header
        # ("Solution to Question 21:"), which sanitize strips to '' -- the
        # row shipped READY with no solution. So a header-printed q_no whose
        # item content is EMPTY is also "missing" and gets the same re-ask.
        content_key = {"Solution": "solution_text",
                       "Question": "stem"}.get(phase_name)

        def _content_ok(it):
            if content_key is None:
                return True          # answer-key re-ask: existence is enough
            val = str(it.get(content_key) or "").strip()
            if content_key == "solution_text":
                # emptiness appears only AFTER sanitize: a header-only answer
                # ('Solution to Question 21:') is non-empty raw but ships ''
                val, _ = qp.sanitize_solution_text(val, own_qn=it.get("_qn"))
                val = val.strip()
            return bool(val)

        have = {i.get("_qn") for i in items if _content_ok(i)}
        missing_pages = {}
        for p in zone_pages:
            for qn in (hdrs.get(p) or set()):
                if qn not in have:
                    missing_pages.setdefault(qn, p)
        if not missing_pages:
            return items
        qns = sorted(missing_pages)
        pages = sorted({p for qn in qns
                        for p in range(missing_pages[qn],
                                       min(missing_pages[qn] + 1,
                                           max(zone_pages)) + 1)})
        self.notes.append(f"{phase_name}: printed headers prove missing/"
                          f"empty block(s) q{qns} -- targeted re-ask on "
                          f"{pages}")
        self._ledger(f"REASK_{phase_name[0]}", pages, qp.PASS_STATUS_PARTIAL,
                     0, f"printed headers prove q{qns} exist; re-asking")
        # FULL prompt (JSON template included): a template-less re-ask
        # shape-drifts (OBG ch2 live: {"solutions": [...], "text": ...}).
        for chunk in _chunk_pages(pages, PAGE_CHUNK):
            re_prompt = (
                prompt_tmpl.format(chapter_name=self.chapter_id,
                                   start=chunk[0], end=chunk[-1])
                + f"\n\nNOTE: Ab SIRF in question numbers ko extract karo: "
                  f"{qns}. Baaki sab ignore karo. WAHI JSON array format use "
                  f"karo jo upar diya hai. Ye blocks page par PRINTED hain -- "
                  f"dhundh kar nikaalo, skip mat karo.")
            try:
                raw = self._call_pages(chunk, re_prompt, dpi=dpi)
            except ModelBlocked:
                self._ledger(f"REASK_{phase_name[0]}", chunk,
                             qp.PASS_STATUS_UNRESOLVED, 0,
                             "model blocked (recitation/safety), retried once")
                self.notes.append(f"{phase_name} re-ask unresolved: model "
                                  f"blocked pages {chunk[0]}-{chunk[-1]}")
                continue
            fixed = _unwrap_items(_parse_json(raw))
            if not isinstance(fixed, list):
                self._ledger(f"REASK_{phase_name[0]}", chunk,
                             qp.PASS_STATUS_UNRESOLVED, 0,
                             "re-ask returned no parseable JSON array")
                continue
            for it in fixed:
                qn0 = _item_qn(it)
                if qn0 is None:
                    self.orphan_items.append({
                        "chapter_id": self.chapter_id, "batch_start": chunk[0],
                        "pdf_pages": list(chunk),
                        "reason": "reask_item_unparseable_qno (schema drift)",
                        "pass": f"REASK_{phase_name[0]}", "item": it})
                    continue
                _normalize_phase_item(phase_name, it)
                it["_qn"] = qn0
                existing = next((i for i in items if i.get("_qn") == qn0),
                                None)
                if existing is not None and _content_ok(existing):
                    continue
                it["_reasked"] = True
                if existing is None:
                    items = list(items) + [it]
                else:
                    # replace the EMPTY item with the recovered one: same
                    # q_no, same phase, printed header is the proof
                    items = [it if i.get("_qn") == qn0 else i for i in items]
                have.add(qn0)
        return self._merge_phase_items(items)

    def _inline_answers_present(self, q_pages):
        hits = 0
        for p in q_pages:
            try:
                t = qp.pdftotext_page(self.pdf, p) or ""
            except Exception:
                t = ""
            if self._INLINE_ANS_TXT.search(t):
                hits += 1
        return hits >= 1 and hits >= max(1, len([p for p in q_pages]) // 3)

    def _resolve_zones(self, bounds, ch_first, ch_last):
        """-> (q_pages, a_pages, s_pages). STRICT per spec: no question zone
        or no solution zone at all = abort (the caller writes the blocker)."""
        qb = bounds.get("question_block") or {}
        ab = bounds.get("answer_key_block") or {}
        sb = bounds.get("solution_block") or {}
        qb_start = _safe_int(qb.get("start_page"))
        sb_start = _safe_int(sb.get("start_page"))
        ab_start = _safe_int(ab.get("start_page"))
        if qb_start is None or sb_start is None:
            raise RuntimeError(
                f"{self.chapter_id}: boundary detect incomplete "
                f"(q_start={qb.get('start_page')}, s_start={sb.get('start_page')}) -- "
                "no extraction attempted, chapter left for review")
        q_end = (ab_start or sb_start) - 1
        if q_end < qb_start:
            q_end = qb_start
        m_q = list(range(qb_start, min(q_end, ch_last) + 1))
        m_a = list(range(ab_start, min(_safe_int(ab.get("end_page"))
                                       or ab_start, ch_last) + 1)) \
            if ab_start else []
        m_s = list(range(sb_start, min(_safe_int(sb.get("end_page"))
                                       or ch_last, ch_last) + 1))

        printed = None
        try:
            printed = self._printed_zones(ch_first, ch_last)
        except Exception as e:
            self.notes.append(f"printed-zone probe failed ({e}) -- "
                              "model zones kept")
        if not printed:
            return m_q, m_a, m_s
        q_pages = m_q
        if printed["q"]:
            q_pages = list(range(min(printed["q"]),
                                 min(max(printed["q"]) + 1, ch_last) + 1))
        s_pages = m_s
        if printed["s"]:
            s_pages = list(range(min(printed["s"]),
                                 min(max(printed["s"]) + 1, ch_last) + 1))
        a_pages = m_a
        if printed["keys"]:
            a_pages = sorted(set(printed["keys"]))
        if q_pages != m_q or s_pages != m_s or a_pages != m_a:
            self.notes.append(
                f"zones corrected by PRINTED headers (model said "
                f"Q{m_q[0]}-{m_q[-1]}/A{m_a}/S{m_s[0]}-{m_s[-1]}; printed says "
                f"Q{q_pages[0]}-{q_pages[-1]}/A{a_pages}/S{s_pages[0]}-{s_pages[-1]})")
        return q_pages, a_pages, s_pages

    # -- Record assembly ------------------------------------------------------
    def _build_records(self, q_items, a_items, s_items):
        """phase JSON -> the canonical chapter_records shape the final-row /
        split writers consume. The answer comes ONLY from the answer-key
        phase; the solution ONLY from the solutions phase -- no phase ever
        reads outside its block (spec hard rule)."""
        amap = {}
        a_low = set()
        for a in a_items:
            qn = a.get("_qn") if a.get("_qn") is not None else _norm_q_no(a.get("q_no"))
            if qn is None:
                continue
            amap[qn] = (a.get("correct_option") or "").strip().upper() or None
            if a.get("low_confidence"):
                a_low.add(qn)
        smap = {}
        for s in s_items:
            qn = s.get("_qn") if s.get("_qn") is not None else _norm_q_no(s.get("q_no"))
            if qn is not None:
                smap[qn] = s
        records = {}
        qn_source_pages = {}
        # OCR fallback markers (model recitation-blocked the page IMAGE; rows
        # stay REVIEW_NEEDED so nobody silently trusts OCR text)
        ocr_q = {i.get("_qn") for i in q_items if i.get("_ocr")}
        ocr_s = {i.get("_qn") for i in s_items if i.get("_ocr")}
        ocr_a = {i.get("_qn") for i in a_items if i.get("_ocr")}
        for q in q_items:
            qn = q.get("_qn") if q.get("_qn") is not None else _norm_q_no(q.get("q_no"))
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
            if qn in ocr_q:
                reasons.append("question phase: page image blocked by model "
                               "filter -> OCR fallback used; verify text "
                               "manually (OCR may misread)")
            if qn in ocr_s:
                reasons.append("solution phase: page image blocked by model "
                               "filter -> OCR fallback used; verify text "
                               "manually (OCR may misread)")
            if qn in ocr_a:
                reasons.append("answer-key phase: page image blocked by model "
                               "filter -> OCR fallback used; verify letter "
                               "manually (OCR may misread)")
            opts, opt_issue = _norm_options(q.get("options"))
            if opt_issue:
                # shape/letter drift must NEVER crash the chapter and must
                # NEVER pass silently: flag the row for human review.
                reasons.append("options shape flagged: " + opt_issue)
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
            if opt_issue:
                records[qn]["_options_suspect_reason"] = (
                    "options shape/letters flagged: " + opt_issue)
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
        # STRICT (spec): a missing question/solution zone means the chapter
        # was never safely zoned -- do NOT run it half-way.
        q_pages, a_pages, s_pages = self._resolve_zones(bounds, ch_first, ch_last)
        self._zones = {"q": q_pages, "a": a_pages, "s": s_pages}
        print(f"[BPH] {self.chapter_id}: Q pages {q_pages[0]}-{q_pages[-1]}"
              f" | A pages {a_pages or '-'} "
              f"| S pages {s_pages[0]}-{s_pages[-1]}")

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
        q_items = self._printed_header_reask("Question", q_items, q_pages,
                                             "_printed_q_hdrs", QUESTION_PROMPT)
        if q_ok is not True:
            self.notes.append(f"question phase unresolved: {q_ok}")
        qp.save_state(self.state)
        if a_pages:
            a_items = self._extract_phase(a_pages, ANSWER_KEY_PROMPT,
                                          "Answer-key", "A", dpi=170)
            a_items, a_ok = self._verify_phase("Answer-key", a_items, a_pages,
                                               dpi=170)
        elif self._inline_answers_present(q_pages):
            # no key TABLE, but answers print inline next to each question --
            # dedicated zero-guess micro-phase (spec extension)
            self.notes.append("no key table; inline answer markers found -- "
                              "running the inline-answer micro-phase")
            a_items = self._extract_phase(q_pages, INLINE_ANSWER_PROMPT,
                                          "Inline-answer", "A")
            a_items, a_ok = self._verify_phase("Answer-key", a_items, q_pages)
        else:
            a_items, a_ok = [], True
            self.notes.append("no answer-key table and no inline markers -- "
                              "answers stay empty + flagged (never guessed)")
        if a_ok is not True:
            self.notes.append(f"answer-key phase unresolved: {a_ok}")
        s_items = self._extract_phase(s_pages, SOLUTION_PROMPT, "Solution", "S")
        s_items, s_ok = self._verify_phase("Solution", s_items, s_pages)
        s_items = self._printed_header_reask("Solution", s_items, s_pages,
                                             "_printed_s_hdrs", SOLUTION_PROMPT)
        if s_ok is not True:
            self.notes.append(f"solutions phase unresolved: {s_ok}")
        qp.save_state(self.state)

        # BLOCK-FAIL: a zone that EXISTS (per boundary/print) but yielded ZERO
        # items means the phase failed wholesale -- do not write a
        # half-shell chapter. Leave it UNDONE (retried next run) + blocker.
        block_fail = []
        if not q_items:
            block_fail.append("question")
        if a_pages and not a_items:
            block_fail.append("answer-key")
        if s_pages and not s_items:
            block_fail.append("solutions")
        if block_fail:
            detail = ("zone(s) existed but the phase extracted 0 items: "
                      + ", ".join(block_fail)
                      + " -- nothing written, chapter retried on the next run")
            qp._append_jsonl(qp.DATA_DIR / "export_gate.jsonl", {
                "chapter_id": self.chapter_id, "kind": "chapter_not_locked",
                "q_no": None, "detail": detail,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
            qp.save_state(self.state)
            print(f"[BPH] {self.chapter_id}: BLOCK-FAIL ({detail})")
            return {"locked": False, "committed": False,
                    "chapter_id": self.chapter_id, "questions": len(q_items),
                    "answers": len(a_items), "solutions": len(s_items),
                    "notes": self.notes}

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
