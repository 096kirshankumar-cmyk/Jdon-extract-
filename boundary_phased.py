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

# Sibling modules (header_index, crop_parse, …) live next to this file.
# `python /abs/path/boundary_phased.py` from another cwd otherwise fails
# with ModuleNotFoundError: header_index.
_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import google.generativeai as genai

import gemini_keys
import qbank_pipeline as qp
import review_queue as rq
import split_outputs
import header_index
import crop_parse

MAX_FIX_ATTEMPTS = 3
PAGE_CHUNK = 7            # boundary detect only; Q/S extract uses crops
KEY_TABLE_DPI = 300       # answer-key table crop only (dense grid)

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


def _crop_strip_png(pdf_path, page, y_hi, y_lo, dpi=130):
    """Render one page and crop [y_hi .. y_lo] (PDF-ish y, larger=higher).

    Isolated evidence for Gemini: one question/solution strip, not 7 pages.
    """
    try:
        png, scale, ph = qp.render_page_png(pdf_path, page, dpi=dpi)
    except Exception:
        png = None
        scale, ph = 1.0, None
    if png is None:
        raw = _png_bytes(pdf_path, page, dpi=dpi)
        if not raw:
            return None
        try:
            from PIL import Image
            import io
            png = Image.open(io.BytesIO(raw)).convert("RGB")
            scale = png.height / float(ph or 792)
        except Exception:
            return raw
    w, h = png.size
    scale = scale or (h / 792.0)
    # PDF y -> image y from top
    top = int(max(0, h - (float(y_hi) * scale)))
    bot = int(min(h, h - (float(y_lo) * scale)))
    if bot <= top + 8:
        bot = min(h, top + 40)
    crop = png.crop((0, top, w, bot))
    import io
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def _stitch_interval_png(pdf_path, iv, dpi=130):
    """One composite image: header → next header, all strips stacked.

    Multi-page intervals are ONE Gemini part (labeled continues), not
    separate page crops that truncate mid-item.
    """
    from PIL import Image, ImageDraw
    import io
    parts = []
    for i, st in enumerate(iv.get("strips") or []):
        raw = _crop_strip_png(pdf_path, st["page"], st["y_hi"], st["y_lo"],
                              dpi=dpi)
        if not raw:
            continue
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        label = (f"INTERVAL part {i + 1}/{len(iv.get('strips') or [])}  "
                 f"file page {st['page']}  "
                 f"{'CONTINUES' if i else 'START'}  q{iv.get('n')}")
        bar_h = 22
        canvas = Image.new("RGB", (im.width, im.height + bar_h), (20, 20, 20))
        canvas.paste(im, (0, bar_h))
        ImageDraw.Draw(canvas).text((6, 4), label, fill=(255, 255, 0))
        parts.append(canvas)
    if not parts:
        return None
    w = max(p.width for p in parts)
    h = sum(p.height for p in parts) + 4 * (len(parts) - 1)
    out = Image.new("RGB", (w, h), (255, 255, 255))
    y = 0
    for p in parts:
        if p.width != w:
            pad = Image.new("RGB", (w, p.height), (255, 255, 255))
            pad.paste(p, (0, 0))
            p = pad
        out.paste(p, (0, y))
        y += p.height + 4
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


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
- Crop mein figure JAISAA printed hai waisa hi dikhega — mat ignore. Jahan
  image visually padti hai wahan text mein exactly `[IMG]` token daalo
  (reading order). Coordinates se merge mat karo. `[IMG]` count = jitni
  figures is interval mein dikhti hain.
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
- Figure crop ke andar printed jagah pe text mein `[IMG]` token daalo
  (reading order). Image hata ke alag se mat socho. Multi-page interval
  = part 1..N same item. `[IMG]` count = us interval ki figures.

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

VERIFY RULES (OBG-001 live):
- FINAL verdict only. Chain-of-thought / "wait / let's check / but image shows"
  mat likho. issue = ONE short factual sentence.
- severity "minor" = spelling/punctuation only (does NOT block LOCK).
- severity "genuine" = missing item, wrong q_no, wrong letter vs printed
  answer-key GRID, or text that is actually absent from the JSON.
- Printed answer-key table / text-layer jeetega visual re-read pe.
  Image se "mujhe B dikha" tabhi genuine hai jab GRID bhi B bole.

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


_QUOTE_RE = re.compile(r"['\"]([^'\"]{3,80})['\"]")
_LETTER_CLAIM_RE = re.compile(
    r"(?:shows?|says?|lists?|image\s+(?:shows?|says?)|correct option\s+(?:is|as))\s+"
    r"(?:option\s+)?['\"]?([A-Ea-e])['\"]?\b",
    re.I)
_CONTRA_MARKERS = re.compile(
    r"\b(wait|but |however|let's check|contradict)\b", re.I)


def _quoted_snippets(issue):
    return [m.group(1).strip() for m in _QUOTE_RE.finditer(issue or "")]


def _visual_letter_claim(issue):
    ms = list(_LETTER_CLAIM_RE.finditer(issue or ""))
    if not ms:
        return None
    return ms[-1].group(1).upper()


def _issue_self_contradicts(issue):
    """True when the issue text argues two different letters / flips mid-way
    (OBG-001-025: 'so Sampson's won't be affected' then 'image shows b')."""
    t = issue or ""
    if not _CONTRA_MARKERS.search(t):
        return False
    letters = re.findall(r"\b(?:option\s+)?([A-Ea-e])\b", t)
    letters = [x.upper() for x in letters]
    return len(set(letters)) >= 2


def _inclusive_pages(pages, ch_last):
    """File-page list from min(pages) through max(pages), clamped at ch_last.

    Python range is exclusive at the end, so the only +1 is that exclusive
    bound. A second +1 (OBG-001) included the page AFTER the last printed
    Question header in the Q phase.
    """
    pages = [p for p in pages if p is not None]
    if not pages:
        return []
    lo = min(pages)
    hi = min(max(pages), ch_last)
    if hi < lo:
        return []
    return list(range(lo, hi + 1))


def _zone_pages_from_headers(pages, ch_last):
    """Contiguous page span for a zone, ignoring headers past the chapter end.

    RUN-31 (OPH-001 live). detect_boundaries scans ch_first..ch_last+2 ON
    PURPOSE, so a block that continues past the chapter's last page is still
    seen and cropped. But those extra pages belong to the NEXT chapter, and
    spanning min..max over the whole set let them stretch the zone:

        visual Question headers  {4,5,6,7,8,9,10,11, 24}
        _inclusive_pages(..., ch_last=22)  ->  4..22      <-- WRONG

    One next-chapter header on p24 turned a correct Q 4-11 into Q 4-22,
    swallowing the answer-key page and overlapping the whole solution zone
    (the run logged Q 4-22 and S 12-22 at once). This was not Gemini
    hallucinating -- the boundary JSON was right (Q 4-11 | A 12 | S 12-22);
    the min..max span discarded it.

    Dropping pages beyond ch_last before spanning fixes it without breaking
    legitimate internal gaps (a question spanning pages 6-8 still leaves no
    header on 7). Crop intervals keep using file_end=_scan_last, so
    continuation content past ch_last is still extracted -- only the ZONE
    stays inside the chapter.

    Returns [] when every header is past ch_last; the caller then keeps the
    model-derived zone rather than getting an empty one."""
    inside = [p for p in (pages or []) if p is not None and int(p) <= int(ch_last)]
    if not inside:
        return []
    return _inclusive_pages(inside, ch_last)


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
        self._printed_key = {}         # qn -> letter from printed key grid
        self._visual_headers = []      # pixel/OCR header index (not pdftotext)
        self._key_evidence = {}        # qn -> {letter, method, pages}
        self._key_evidence_required = False

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

    def _call_crops(self, ivals, prompt, dpi=130):
        """Gemini on isolated header-interval crops only (not 7-page windows).

        Multi-page interval = one stitched composite (part 1 continues…).
        """
        pages = [iv["start_page"] for iv in ivals]
        files = [f"ISOLATED INTERVAL CROPS (header → next header; file pages "
                 f"{pages}). One image per printed item, possibly stitched "
                 f"across pages. Transcribe ONLY that interval. Do not "
                 f"invent from chapter context."]
        for iv in ivals:
            b = _stitch_interval_png(self.pdf, iv, dpi=dpi)
            if not b:
                for st in iv.get("strips") or []:
                    b = _crop_strip_png(self.pdf, st["page"], st["y_hi"],
                                        st["y_lo"], dpi=dpi)
                    if b:
                        files.append({"mime_type": "image/png", "data":
                                      base64.b64encode(b).decode()})
                continue
            files.append({"mime_type": "image/png", "data":
                          base64.b64encode(b).decode()})
        files.append(prompt)
        return self._gen(files)

    def _visual_intervals(self, label):
        recs = self._visual_headers or []
        if not recs:
            return []
        file_end = getattr(self, "_scan_last", None) or getattr(
            self, "_ch_last", None)
        if label == "Question":
            return header_index.intervals(recs, header_index.T_QUESTION,
                                          file_end=file_end)
        if label == "Solution":
            return header_index.intervals(recs, header_index.T_SOLUTION,
                                          file_end=file_end)
        return []

    def _ledger(self, pass_name, pages, status, n_items, note=""):
        row = qp._ledger_pass(self.chapter_id, self.subject, self.chapter_no,
                              pass_name, pages or [], status, n_items, note)
        self.ledger_rows.append(row)
        return row

    # -- Step 0 --------------------------------------------------------------
    def detect_boundaries(self, ch_first, ch_last):
        """Gemini boundary only if visual header index is empty."""
        try:
            self._ch_last = int(ch_last)
            self._ch_first = int(ch_first)
            scan_last = int(ch_last)
            try:
                total = len(qp.PdfReader(self.pdf).pages)
                scan_last = min(int(ch_last) + 2, int(total))
            except Exception:
                pass
            self._scan_last = scan_last
            self._visual_headers = header_index.heal_visual_headers(
                header_index.scan_chapter(self.pdf, ch_first, scan_last))
        except Exception as e:
            self.notes.append(f"visual header index failed ({e})")
            self._visual_headers = []
        vis = header_index.index_sets(self._visual_headers)
        if vis["q_ns"] and vis["s_ns"]:
            q_lo = min(vis["q_pages"])
            s_lo = min(vis["s_pages"]) if vis["s_pages"] else None
            k_pages = header_index.key_region_pages(self._visual_headers)
            k_lo = min(k_pages) if k_pages else (
                min(vis["key_pages"]) if vis["key_pages"] else None)
            k_hi = max(k_pages) if k_pages else k_lo
            print(f"[BPH] {self.chapter_id}: skip Gemini BOUNDARY "
                  f"(visual Q{sorted(vis['q_ns'])} "
                  f"S{sorted(vis['s_ns'])} key_region={k_pages})")
            return {
                "confidence": "high",
                "notes": "zones from visual header index; no Gemini boundary",
                "question_block": {"start_page": q_lo,
                                   "start_position": "top"},
                "answer_key_block": (
                    {"start_page": k_lo, "start_position": "middle",
                     "end_page": k_hi,
                     "end_position": "bottom"} if k_lo else None),
                "solution_block": {"start_page": s_lo,
                                   "start_position": "top",
                                   "end_page": ch_last,
                                   "end_position": "bottom"} if s_lo else None,
            }
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
    _HEALTH_RANK = {"CLEAN": 3, "DEGRADED": 2, "GARBLED": 1, "EMPTY": 0}

    def _geom_item_from_interval(self, iv, label):
        """Deterministic parse of one crop from the PDF TEXT LAYER only.
        Returns the parsed item or None, and tallies the outcome on
        self._geom_stats so the phase log says why a crop went to Gemini.

        RUN-34: an OCR fallback was added here in run-32 and is now REMOVED.
        On OPH-001 it took geom_ok from 0 to 22/23 and cut Gemini calls from
        46 to ~2 -- and the recovered text was unusable. Tesseract read the
        page furniture as body copy:

            q1 solution  "...leading to\\n12 Sold by @itachibot\\n\\nhypermetropia."
            q3 solution  ". X"                    (whole explanation lost)
            q6 solution  "...neural crest.\\nCmlistklianm Fm Pir tr rebkiana WM"
            q2 stem      "oe SS «\\ni i ity?\\nAt what age would a child attain full a «"

        Every one shipped as qa_status=READY, because _crop_item_shippable
        checks structure (stem present + 4 options), not legibility. Gemini
        reads the crop IMAGE and does not make these mistakes. Cheap-and-wrong
        is the wrong trade for a book extracted 18,000 questions at a time
        with nobody reviewing the output, so a crop whose text layer is not
        CLEAN now falls through to Gemini exactly as it did before run-32."""
        stats = getattr(self, "_geom_stats", None)
        if stats is None:
            stats = self._geom_stats = {}

        def _tally(key):
            stats[key] = stats.get(key, 0) + 1

        strips = iv.get("strips") or []
        if not strips:
            _tally("no_strips")
            return None

        # Worst health across the strips decides usability -- one garbled page
        # in a multi-page block makes the joined blob unusable anyway.
        worst, blobs = "CLEAN", []
        for st in strips:
            try:
                txt = qp.pdftotext_page(self.pdf, st["page"]) or ""
            except Exception:
                txt = ""
            h = header_index.text_layer_health(txt)
            if self._HEALTH_RANK.get(h, 0) < self._HEALTH_RANK.get(worst, 3):
                worst = h
            blobs.append(txt)

        if worst != "CLEAN":
            # Unreadable text layer -> hand the crop to Gemini, which reads
            # the rendered image. Tally the health so a book that always
            # lands here is visible in the log instead of silently spending
            # a Gemini call per crop.
            _tally(f"text_{worst.lower()}_to_gemini")
            return None

        if label == "Question":
            it = crop_parse.parse_question_text("\n".join(blobs), iv["n"])
        elif label == "Solution":
            it = crop_parse.parse_solution_text("\n".join(blobs), iv["n"])
        else:
            return None
        if it and self._crop_item_shippable(it, label):
            _tally("text")
            return it
        # Readable text but no complete item: a crop_parse coverage gap.
        # Gemini gets the crop.
        _tally("text_clean_parse_missed")
        return None

    def _extract_from_crops(self, ivals, prompt_tmpl, label, pass_name, dpi=130):
        """Geometric CLEAN parse first; Gemini only leftover crops (max 4)."""
        # RUN-32: the tally is per-phase, so reset it before this phase's
        # crops are read and report it with the split.
        self._geom_stats = {}
        out = []
        leftover = []
        for iv in ivals:
            it = self._geom_item_from_interval(iv, label)
            if it and (
                    (label == "Question" and it.get("stem")
                     and len(_norm_options(it.get("options"))[0]) >= 4)
                    or (label == "Solution" and it.get("solution_text"))):
                it["source_page"] = iv["start_page"]
                it["source_page_range"] = [iv["start_page"], iv["end_page"]]
                out.append(it)
            else:
                leftover.append(iv)
        # Always report the split (run-30): it used to print only when
        # something needed Gemini, so a fully geometric phase -- the best
        # possible outcome, zero tokens -- looked identical to a phase that
        # never ran the crop path at all. RUN-32 adds the WHY: geom_ok=0 told
        # us nothing was being parsed deterministically but not whether the
        # text layer was missing, unreadable, or merely unparseable.
        _why = " ".join(f"{k}={v}" for k, v in
                        sorted((self._geom_stats or {}).items())) or "-"
        print(f"[BPH] {self.chapter_id}: {pass_name} crops={len(ivals)} "
              f"geom_ok={len(out)} (0 Gemini calls) "
              f"gemini_crops={len(leftover)} | why: {_why}")
        batchable, singles = [], []
        for iv in leftover:
            if self._crop_is_batchable(iv, label, leftover):
                batchable.append(iv)
            else:
                singles.append(iv)
        work = list(_chunk_pages(batchable, 4)) + [[iv] for iv in singles]
        retry_singles = []
        def _keep_shippable(kept, tag):
            """RUN-31: one place that decides what may ship, so the batch
            path, the single-crop path and the post-retry path cannot drift.
            Anything structurally incomplete is DROPPED here -- it is then
            missing (named by q_no in the export gate) instead of shipping as
            a plausible hallucination that every downstream check accepts."""
            good = [it for it in kept if self._crop_item_shippable(it, label)]
            bad = len(kept) - len(good)
            if bad:
                msg = (f"{pass_name}: {tag} DISCARDED {bad} unusable item(s) "
                       f"-- left missing rather than shipped as a guess")
                self.notes.append(msg)
                print(f"[BPH] {self.chapter_id}: {msg}")
            return good

        for batch in work:
            kept = self._gemini_crop_batch(batch, prompt_tmpl, label,
                                           pass_name, dpi)
            by_n = {it.get("_qn"): it for it in kept if it.get("_qn") is not None}
            if len(batch) > 1:
                for iv in batch:
                    it = by_n.get(iv["n"])
                    if not self._crop_item_ok(it, label):
                        retry_singles.append(iv)
                    else:
                        out.append(it)
            else:
                # Was `out.extend(kept)`: a single crop was appended with no
                # check at all, so the unbatched path -- the majority on a
                # book whose text layer is not CLEAN -- could ship anything.
                out.extend(_keep_shippable(kept, f"q{batch[0]['n']}"))
        for iv in retry_singles:
            self.notes.append(
                f"{pass_name}: batch miss/low-conf q{iv['n']} -> single crop")
            kept = self._gemini_crop_batch([iv], prompt_tmpl, label,
                                           pass_name, dpi)
            out.extend(_keep_shippable(kept, f"q{iv['n']} after retry"))
        return self._merge_phase_items(out)

    @staticmethod
    def _crop_is_batchable(iv, label, leftover):
        """Text-only mid-chapter same-page Q only. Figures/boundary = single."""
        if label != "Question":
            return False
        ns = [x.get("n") for x in (leftover or []) if x.get("n")]
        if ns and iv.get("n") == max(ns):
            return False
        pages = {st["page"] for st in (iv.get("strips") or [])}
        if len(pages) > 1:
            return False
        return True

    @staticmethod
    def _crop_item_ok(it, label):
        if not isinstance(it, dict):
            return False
        if str(it.get("text_confidence") or "").lower() == "low":
            return False
        if it.get("has_figure"):
            return False
        if label == "Question":
            opts, _ = _norm_options(it.get("options"))
            return bool(str(it.get("stem") or "").strip()) and len(opts) >= 4
        if label == "Solution":
            return bool(str(it.get("solution_text") or "").strip())
        return True

    @staticmethod
    def _crop_item_shippable(it, label):
        """RUN-31: may this item SHIP? Not the same question as
        _crop_item_ok, which asks "should we retry this crop?".

        The difference is has_figure: _crop_item_ok rejects a figured item
        because a figure can hide text, so it is worth another read. But a
        figure is NOT a text defect -- the image pass attaches it separately.
        Using _crop_item_ok as the ship test would throw away every question
        with a diagram, which on this book is most of them.

        For an 18k-question book with no human review capacity the rule is:
        ship only content that is structurally complete, otherwise leave the
        item missing. A missing item is reported by q_no in the export gate;
        a plausible hallucination looks clean to every downstream check."""
        if not isinstance(it, dict):
            return False
        if label == "Question":
            opts, _ = _norm_options(it.get("options"))
            return bool(str(it.get("stem") or "").strip()) and len(opts) >= 4
        if label == "Solution":
            return bool(str(it.get("solution_text") or "").strip())
        return True

    def _gemini_crop_batch(self, batch, prompt_tmpl, label, pass_name, dpi):
        if not batch:
            return []
        lo = batch[0]["start_page"]
        hi = batch[-1]["end_page"]
        p = prompt_tmpl.format(chapter_name=self.chapter_id,
                               start=lo, end=hi)
        p = (p + "\n\nNOTE: Images are CROPS of a single printed item "
             "(header → next header). Extract THAT item only. "
             f"Expected q_nos in this batch: "
             f"{[iv['n'] for iv in batch]}.")
        items = None
        for reask in range(2):
            try:
                raw = self._call_crops(batch, p, dpi=dpi)
            except ModelBlocked:
                self._ledger(pass_name, [iv["start_page"] for iv in batch],
                             qp.PASS_STATUS_UNRESOLVED, 0,
                             "model blocked on crop")
                self.notes.append(f"{pass_name} crop unresolved (blocked)")
                break
            items = _unwrap_items(_parse_json(raw))
            if isinstance(items, list):
                break
        if not isinstance(items, list):
            self._ledger(pass_name, [iv["start_page"] for iv in batch],
                         qp.PASS_STATUS_UNRESOLVED, 0,
                         "crop batch unparsable")
            return []
        expect = {iv["n"] for iv in batch}
        kept = []
        for it in items:
            qn0 = _item_qn(it)
            if not isinstance(it, dict) or qn0 is None:
                continue
            _normalize_phase_item(label, it)
            it["_qn"] = qn0
            it["_header_n"] = qn0 if qn0 in expect else None
            it["_continuation"] = _is_continuation_marker(_item_qno_raw(it))
            if pass_name == "Q" and self._printed_q_max is not None \
                    and qn0 > self._printed_q_max:
                continue
            for iv in batch:
                if iv["n"] == qn0:
                    it["source_page"] = iv["start_page"]
                    it["source_page_range"] = [iv["start_page"],
                                               iv["end_page"]]
                    break
            kept.append(it)
        self._ledger(pass_name, [iv["start_page"] for iv in batch],
                     qp.PASS_STATUS_SUCCESS if kept
                     else qp.PASS_STATUS_PARTIAL, len(kept),
                     "crop extract")
        return kept

    def _extract_phase(self, pages, prompt_tmpl, label, pass_name, dpi=110):
        ivals = self._visual_intervals(label)
        if ivals and label in ("Question", "Solution"):
            # Final rule: stem/options/solution from THAT crop only.
            # No 7-page fallback — empty crop stays empty + flagged.
            print(f"[BPH] {self.chapter_id}: {pass_name} -> CROP path "
                  f"({len(ivals)} {label} crop(s) cut from the visual header "
                  f"index; pages {min(i['start_page'] for i in ivals)}-"
                  f"{max(i['end_page'] for i in ivals)})")
            cropped = self._extract_from_crops(
                ivals, prompt_tmpl, label, pass_name, dpi=max(dpi, 130))
            if label == "Solution":
                cropped = self._c1_split_solutions(cropped or [])
            if not cropped:
                self.notes.append(
                    f"{pass_name}: crop extract empty (no 7-page fallback)")
            return cropped or []
        # WHY the page path ran must be visible (run-30). Crops are the
        # designed path for Question/Solution; reaching here means either the
        # phase is the Answer-key table (whole pages are correct there) or the
        # visual header scan found no headers of this type, so there was
        # nothing to cut a crop from. The old code fell through silently, so a
        # whole book could extract by page images with nothing in the log to
        # say the crop path never engaged.
        if label in ("Question", "Solution"):
            _hdr_n = len(self._visual_headers or [])
            print(f"[BPH] {self.chapter_id}: {pass_name} -> PAGE path "
                  f"(FALLBACK: the visual header index produced 0 {label} "
                  f"crops -- {_hdr_n} header(s) scanned total). Fix the header "
                  f"scan to get crops back; page images are the degraded "
                  f"path.")
            self.notes.append(
                f"{pass_name}: no {label} crops from the visual header index "
                f"({_hdr_n} headers scanned) -- degraded to whole-page "
                f"extraction")
        else:
            print(f"[BPH] {self.chapter_id}: {pass_name} -> PAGE path "
                  f"(answer key is a page-spanning table; crops are cut for "
                  f"Question/Solution blocks only)")
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
        merged = self._merge_phase_items(out)
        # C1 BACKSTOP: solution texts that carry another question's printed
        # header inside them (page-fold bleed, ANAT-001 live) are split
        # deterministically BEFORE verify sees them -- this often removes
        # the need for the fix loop entirely.
        if label == "Solution":
            merged = self._c1_split_solutions(merged)
        return merged

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
    def _item_completeness(it):
        """Higher is more complete. Used to pick a winner instead of first-seen."""
        opts, _ = _norm_options(it.get("options") if it else None)
        stem = str((it or {}).get("stem") or (it or {}).get("solution_text") or "")
        conf = str((it or {}).get("text_confidence") or "").lower()
        score = 0
        score += min(len(opts), 8) * 10          # 4 options >> 3
        score += 3 if len(stem) > 40 else (1 if stem.strip() else 0)
        score += 2 if conf == "high" else (1 if conf == "medium" else 0)
        score += 4 if it and it.get("_header_n") else 0
        return score

    @staticmethod
    def _merge_phase_items(items):
        """Duplicate/continuation merge by COMPLETENESS, not first-seen.

        Fill-only (old) made a 3-option first chunk permanently beat a later
        4-option crop (Q24 option D on the next page). Winner is now the
        more complete item; the loser only fills holes the winner still has.
        """
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
            a, b = by_qn[qn], dict(it)
            if ChapterRunner._item_completeness(b) > ChapterRunner._item_completeness(a):
                a, b = b, a
            # a is winner; b fills holes only
            for k, v in b.items():
                if k in ("_qn", "_continuation"):
                    continue
                if k == "options":
                    merged, _ = _norm_options(a.get("options"))
                    for letter, text in _norm_options(v)[0].items():
                        if not merged.get(letter):
                            merged[letter] = text
                    a["options"] = merged
                elif k == "source_page_range":
                    lo = (a.get("source_page_range") or [None])[0]
                    hi = (a.get("source_page_range") or [None, None])[-1]
                    for pg in (v or []):
                        lo = pg if lo is None else min(lo, pg)
                        hi = pg if hi is None else max(hi, pg)
                    a["source_page_range"] = [x for x in (lo, hi)
                                              if x is not None]
                elif not a.get(k) and v not in (None, "", [], {}):
                    a[k] = v
            if b.get("_continuation") or a.get("_continuation"):
                a["_seen_continuation"] = True
            by_qn[qn] = a
        return [by_qn[q] for q in order]

    # -- C1: deterministic split at printed solution headers -------------------
    _SOL_SPLIT_RE = re.compile(
        r"(?im)^\s*Solution\s+to\s+Question\s+(\d{1,3})\s*[:.\-–]?\s*")

    def _flag_solution_interval_mismatch(self, items):
        """Reject a body that cannot live in this q_no's header interval.

        Ch1 Q14 must not keep Call-Exner (Q22). Pages outside the solution
        strip stay flagged; we empty the body so READY cannot ship it.
        """
        ivals = {iv["n"]: iv for iv in (self._visual_intervals("Solution") or [])}
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                out.append(it)
                continue
            qn = it.get("_qn")
            text = str(it.get("solution_text") or "")
            bad = False
            if qn != 22 and re.search(r"call[\s-]*exner", text, re.I):
                bad = True
                note = ("solution_body_page_mismatch: Call-Exner text on "
                        f"q{qn} (belongs to Q22); emptied for same-crop re-ask")
            else:
                note = ""
            iv = ivals.get(qn) if qn is not None else None
            if iv:
                allowed = {st["page"] for st in (iv.get("strips") or [])}
                spr = it.get("source_page_range") or []
                got = {p for p in spr if _safe_int(p) is not None}
                if got and allowed and not (got & allowed):
                    bad = True
                    note = (note + " | " if note else "") + (
                        f"solution pages {sorted(got)} ∩ interval "
                        f"{sorted(allowed)} empty")
            if bad:
                it = dict(it)
                it["solution_text"] = ""
                it["_split_note"] = (
                    (it.get("_split_note") or "") +
                    (" | " if it.get("_split_note") else "") + note)
                self.notes.append(f"q{qn}: {note}")
            out.append(it)
        return out

    def _c1_split_solutions(self, items):
        """C1 BACKSTOP (user order: C2 first, C1 as the deterministic fallback).

        ANAT-001 live: the model folds the solution whose header sits at a
        page BOTTOM into the PREVIOUS question (q16 absorbed q17's body;
        later re-asks chained it again: q17 tail = corpus-luteum text,
        q18 = q19 text). The printed 'Solution to Question N:' marker inside
        an item's text is a HARD boundary -- split there. Deterministic, no
        extra model call, zero inventing: the text after the marker is moved
        to question N (created if absent). Every affected row is flagged
        _split_note -> REVIEW_NEEDED so it is never silently trusted.

        Order guarantee: overflow from an EARLIER item joins question M's
        text BEFORE M's own part (reading order). Duplicates (the same
        overflow re-dumped inside M's own item) are detected and NOT
        doubled -- flagged instead."""
        if not items:
            return items
        max_s = None
        if self._printed_s_hdrs:
            mx = max((max(v) for v in self._printed_s_hdrs.values()),
                     default=None)
            max_s = mx
        finals = {}      # qn -> {"parts": [str], "it": dict, "created": bool}
        order = []
        noqn = []

        def _dup(a, b):
            a, b = a.strip(), b.strip()
            if not a or not b:
                return False
            return (b.startswith(a[:80]) or a.startswith(b[:80])
                    or (len(a) > 40 and a[:60] in b[:400]))

        def _append(rec, part, src_it):
            """Append a text part with duplicate detection: the same segment
            can legitimately arrive twice (overflow + the item's own copy),
            it must not be doubled -- flagged instead."""
            if any(_dup(p, part) for p in rec["parts"]):
                rec["it"] = rec["it"] or dict(src_it)
                rec["it"]["_split_note"] = (
                    (rec["it"].get("_split_note") or "") +
                    (" | " if rec["it"].get("_split_note") else "") +
                    "C1: duplicate segment detected, kept one copy only; "
                    "verify manually")
                return
            rec["parts"].append(part)

        for it in items:
            if not isinstance(it, dict):
                noqn.append(it)
                continue
            qn = it.get("_qn")
            if qn is None:
                noqn.append(it)
                continue
            text = str(it.get("solution_text") or "")
            ms = list(self._SOL_SPLIT_RE.finditer(text))
            targets = []
            for m in ms:
                M = int(m.group(1))
                if M == qn or M < 1:
                    continue
                if max_s is not None and M > max_s:
                    continue
                targets.append((m.start(), m.end(), M))
            if not targets:
                rec = finals.setdefault(qn, {"parts": [], "it": None,
                                             "created": False})
                if rec["it"] is None:
                    rec["it"] = it
                if text.strip():
                    _append(rec, text, it)
                if qn not in order:
                    order.append(qn)
                continue
            own = text[:targets[0][0]].rstrip()
            rec = finals.setdefault(qn, {"parts": [], "it": None,
                                         "created": False})
            if rec["it"] is None:
                rec["it"] = it
            if own.strip():
                _append(rec, own, it)
            if qn not in order:
                order.append(qn)
            it["_split_note"] = (f"C1: solution split at printed 'Solution "
                                 f"to Question' header(s) inside the text"
                                 f"{' (own part kept)' if own.strip() else ''}"
                                 f"; moved segments went to their own "
                                 f"questions -- verify manually")
            if rec["it"].get("_split_note") is None and it is not rec["it"]:
                rec["it"]["_split_note"] = it["_split_note"]
            for i, (st, en, M) in enumerate(targets):
                nxt = targets[i + 1][0] if i + 1 < len(targets) else len(text)
                part = text[en:nxt].strip()
                if not part:
                    continue
                rec2 = finals.setdefault(M, {"parts": [], "it": None,
                                             "created": True})
                if rec2["it"] is None:
                    src = dict(it)
                    src["_qn"] = M
                    src["_split_note"] = (
                        f"C1: q{M} constructed from the text printed after "
                        f"the 'Solution to Question {M}:' header that was "
                        f"folded into q{qn}'s answer -- verify manually")
                    src["_c1_created"] = True
                    rec2["it"] = src
                    if M not in order:
                        order.append(M)
                else:
                    prev = rec2["it"].get("_split_note") or ""
                    rec2["it"]["_split_note"] = (
                        (prev + " | " if prev else "") +
                        f"C1: q{M} received the segment folded into q{qn}'s "
                        f"answer (printed header boundary) -- verify "
                        f"manually")
                _append(rec2, part, it)
        out = [x for x in noqn]
        for qn in order:
            rec = finals[qn]
            it = rec["it"]
            if it is None:
                continue
            joined = "\n\n".join(p for p in rec["parts"] if p.strip()).strip()
            it["solution_text"] = joined
            out.append(it)
        if any(f.get("created") or f["it"].get("_split_note")
               for f in finals.values()):
            self.notes.append(
                "C1: solution text split at printed header boundary(s) "
                "-- deterministic, rows flagged for manual review")
        return out

    # -- Verify helper (Steps 2/4/6 share this) --------------------------------
    def _recheck_unverified(self, phase_name, items, verdict):
        """RUN-35: re-test a verify verdict AFTER the re-ask has run.

        _verify_phase runs BEFORE _printed_header_reask, so its verdict goes
        stale. OPH-001 live:

            verify -> q3 'Solution text is completely empty.'  (genuine)
            re-ask -> filled q3
            commit -> missing solution: 0, 0 INCOMPLETE      (data was fine)
            gate   -> phase_unresolved ... NOT a clean export (stale verdict)

        The chapter was blocked on a problem the pipeline had already fixed,
        and because the note is chapter-scoped (q_no=None) it could not even
        be traced to a row. Re-test every flagged q_no against the items we
        are actually about to ship: whatever is now complete comes off the
        list, and if nothing is left the phase counts as resolved. A verdict
        in any other shape is returned untouched -- this never invents a pass.
        """
        if verdict is True:
            return True
        if not isinstance(verdict, tuple) or len(verdict) != 2:
            return verdict
        reason, payload = verdict
        if not isinstance(payload, dict) or not isinstance(
                payload.get("mismatches"), list):
            return verdict

        content_key = {"Solution": "solution_text",
                       "Question": "stem"}.get(phase_name)
        by_qn = {}
        for it in items or []:
            qn = _norm_q_no(it.get("_qn") if it.get("_qn") is not None
                            else it.get("q_no"))
            if qn is not None:
                by_qn[qn] = it

        def _now_complete(qn):
            it = by_qn.get(qn)
            if it is None or content_key is None:
                return False
            val = str(it.get(content_key) or "").strip()
            if content_key == "solution_text":
                val, _ = qp.sanitize_solution_text(val, own_qn=qn)
                val = val.strip()
            return bool(val)

        still = []
        recovered = []
        for m in payload.get("mismatches") or []:
            qn = _norm_q_no((m or {}).get("q_no"))
            if qn is not None and _now_complete(qn):
                recovered.append(qn)
            else:
                still.append(m)
        if recovered:
            print(f"[BPH] {self.chapter_id}: {phase_name} verify verdict was "
                  f"stale -- q{sorted(recovered)} recovered by the re-ask, "
                  f"cleared from the phase verdict")
        if not still:
            return True
        if len(still) == len(payload.get("mismatches") or []):
            return verdict
        updated = dict(payload)
        updated["mismatches"] = still
        return (reason, updated)

    def _verify_phase(self, phase_name, items, pages, dpi=110):
        if not pages:
            return items, True
        bleed_line = BLEED_LINE if phase_name == "Solution" else ""
        prompt = VERIFY_PROMPT.format(phase_name=phase_name, bleed_line=bleed_line)
        # BUG 6: verify the SAME text we will ship (normalized options /
        # sanitized solutions), never a pre-sanitize draft.
        payload_items = []
        for it in items:
            it2 = dict(it)
            if phase_name == "Question":
                opts, _ = _norm_options(it.get("options"))
                it2["options"] = opts
            elif phase_name == "Solution":
                st, _ = qp.sanitize_solution_text(
                    it.get("solution_text"), own_qn=it.get("_qn"))
                it2["solution_text"] = st
            payload_items.append(it2)
        payload = json.dumps(payload_items, ensure_ascii=False, default=str)
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
            genuine = self._filter_verify_mismatches(phase_name, items, mism)
            if not genuine:
                self._ledger(f"VERIFY_{phase_name[0]}", pages,
                             qp.PASS_STATUS_SUCCESS, len(items),
                             "no genuine mismatches (minor/phantom dropped)")
                return items, True
            items = self._targeted_fix(phase_name, items, genuine)
        # Fix 5: one widened-crop retry to the next visible header, then stop.
        items = self._widened_crop_retry(phase_name, items, genuine)
        return items, ("exceeded attempts", last)

    def _widened_crop_retry(self, phase_name, items, mismatches):
        """After MAX_FIX_ATTEMPTS: one crop from this header to the next
        visible header. No 7-page window. Failure stays INCOMPLETE.
        """
        if phase_name not in ("Question", "Solution"):
            return items
        qns = []
        for m in mismatches or []:
            qn = _norm_q_no((m or {}).get("q_no"))
            if qn is not None:
                qns.append(qn)
        if not qns:
            return items
        label = phase_name
        ivals = {iv["n"]: iv for iv in (self._visual_intervals(label) or [])}
        prompt_tmpl = (QUESTION_PROMPT if label == "Question"
                       else SOLUTION_PROMPT)
        by = {it.get("_qn"): it for it in items if it.get("_qn") is not None}
        for qn in qns:
            iv = ivals.get(qn)
            if not iv:
                continue
            wide = header_index.widen_interval_to_next_header(
                iv, self._visual_headers)
            self.notes.append(
                f"{phase_name}: exceeded attempts q{qn} -> one widened crop "
                f"p{wide.get('start_page')}-{wide.get('end_page')}")
            kept = self._gemini_crop_batch(
                [wide], prompt_tmpl, label, f"WIDE_{label[0]}", dpi=130)
            hit = next((it for it in kept if it.get("_qn") == qn), None)
            if hit and self._crop_item_ok(hit, label):
                hit["_widened_retry"] = True
                by[qn] = hit
            else:
                cur = dict(by.get(qn) or {"_qn": qn, "q_no": str(qn)})
                cur["_exceeded_attempts"] = True
                if label == "Question" and not str(cur.get("stem") or "").strip():
                    pass
                if label == "Solution" and not self._crop_item_ok(cur, label):
                    cur["solution_text"] = cur.get("solution_text") or ""
                by[qn] = cur
        order = []
        seen = set()
        out = []
        for it in items:
            qn = it.get("_qn")
            if qn in by and qn not in seen:
                out.append(by[qn])
                seen.add(qn)
            elif qn not in by:
                out.append(it)
        for qn, it in by.items():
            if qn not in seen:
                out.append(it)
        return self._merge_phase_items(out)

    def _filter_verify_mismatches(self, phase_name, items, mism):
        """BUG 6 (OBG-001 live): verify hallucinated Q1 'Lobia' (not in JSON)
        and Q25 visual 'image says B' while printed grid + extract are C.
        Drop: non-genuine severity, quotes absent from the item, visual
        letter-flips that contradict the printed key, self-contradictory
        chain-of-thought. Never rewrite content here -- only ignore phantom
        flags so the retry loop cannot lock a clean chapter forever.
        """
        by_qn = {}
        for it in items or []:
            qn = it.get("_qn")
            if qn is not None:
                by_qn[qn] = it
        kept = []
        for m in mism or []:
            if not isinstance(m, dict):
                continue
            sev = str(m.get("severity") or "genuine").strip().lower()
            issue = str(m.get("issue") or "")
            qn = _norm_q_no(m.get("q_no"))
            if sev and sev != "genuine":
                self.notes.append(
                    f"verify drop q{qn}: severity={sev} (not genuine)")
                continue
            if _issue_self_contradicts(issue):
                self.notes.append(
                    f"verify drop q{qn}: self-contradictory issue text")
                continue
            it = by_qn.get(qn) if qn is not None else None
            quoted = _quoted_snippets(issue)
            if it is not None and quoted:
                blob = json.dumps(it, ensure_ascii=False, default=str).lower()
                missing = [q for q in quoted if q.lower() not in blob
                           and len(q) >= 4]
                if missing:
                    self.notes.append(
                        f"verify drop q{qn}: quoted {missing!r} not in "
                        f"current item (stale/hallucinated)")
                    continue
            if it is not None and phase_name in ("Answer-key", "Question"):
                extracted = None
                if phase_name == "Answer-key":
                    extracted = (str(it.get("correct_option") or "")
                                 .strip().upper() or None)
                printed = (getattr(self, "_printed_key", None) or {}).get(qn)
                vis = _visual_letter_claim(issue)
                if printed and extracted and printed == extracted and vis                         and vis != printed:
                    self.notes.append(
                        f"verify drop q{qn}: visual '{vis}' loses to printed "
                        f"key '{printed}' (matches extract)")
                    continue
            kept.append(m)
        return kept

    def _printed_boundary_note(self, phase_name, qns):
        """C2 (ANAT-001 live): a targeted re-ask keeps failing because the
        model folds a solution whose header sits at a page BOTTOM into the
        PREVIOUS question (q16 item absorbed q17's body; then the fix loop
        re-flags forever). The text layer PROVES where each header is printed
        -- hand that proof to the model as an explicit page-split rule."""
        if phase_name == "Solution":
            hdr_map = getattr(self, "_printed_s_hdrs", None) or {}
            label = "Solution to Question"
        elif phase_name == "Question":
            hdr_map = getattr(self, "_printed_q_hdrs", None) or {}
            label = "Question"
        else:
            return ""
        lines = []
        for q in qns:
            qi = _safe_int(q)
            if qi is None:
                continue
            pages = sorted(p for p, s in hdr_map.items() if qi in s)
            if pages:
                lines.append(f"- q{qi}: '{label} {qi}:' header PRINTED on "
                             f"page(s) {pages}")
        if not lines:
            return ""
        return ("\n\nPAGE-BOUNDARY PROOF (printed headers -- page se liya hai, "
                "guess nahi):\n" + "\n".join(lines) +
                "\nRules:"
                "\n- Ek header kisi page ke BOTTOM me ho sakta hai aur uska "
                "poora text agle page ke TOP me -- us case me poora text usi "
                "question ko do."
                "\n- Agar kisi answer ke ANDAR agla 'Solution to Question N:' "
                "header dikhe, to wahan text KAT do -- uske baad jo text hai "
                "wo question N ka hai."
                "\n- Har answer SIRF apne header ke baad aur agle header se "
                "PEHLE wala text ho.")

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
        boundary = self._printed_boundary_note(phase_name, qns)
        for chunk in _chunk_pages(fix_pages, PAGE_CHUNK):
            if not chunk:
                continue
            re_prompt = (
                phase_hint.format(chapter_name=self.chapter_id,
                                  start=chunk[0], end=chunk[-1])
                + f"\n\nNOTE: Ab SIRF in question numbers ko dobara extract "
                  f"karo: {qns}. Baaki sab ignore karo. WAHI JSON array "
                  f"format use karo jo upar diya hai." + boundary)
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
            health = header_index.text_layer_health(txt)
            if health in ("EMPTY", "GARBLED"):
                self.notes.append(f"p{p} text-layer {health} -- not printed")
                continue                      # pdftotext is not printed
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
        # printed answer-key letters (text layer) -- verify visual re-reads
        # lose to this map (BUG 6 / OBG-001-025).
        printed_key = {}
        for p in range(ch_first, ch_last + 1):
            try:
                txt = qp.pdftotext_page(self.pdf, p) or ""
            except Exception:
                txt = ""
            if not txt.strip():
                continue
            if header_index.text_layer_health(txt) in ("EMPTY", "GARBLED"):
                continue
            for m in self._KEYROW_TXT.finditer(txt):
                printed_key[int(m.group(1))] = m.group(2).upper()
        self._printed_key = printed_key
        # FIX B phantom-q_no guard: the highest number this chapter actually
        # prints as a 'Question N:' header is the ceiling of the real
        # question set. Anything the model reports ABOVE it (RAD-002-026
        # live: the page number '26' read as a question number) cannot
        # exist in the chapter and is orphaned at intake, never shipped.
        self._printed_q_max = max((max(v) for v in q_hdrs.values()),
                                  default=None) if q_hdrs else None
        return {"q": q_pages, "s": s_pages, "keys": key_cands}

    @staticmethod
    def _solution_dup_pairs(items, thresh=0.85):
        """Deterministic label-misassignment detector (ANAT-001 live, C3
        hardening): two DIFFERENT questions can never legitimately carry
        near-identical solution text. Returns [(q_a, q_b, sim)]."""
        import difflib as _d
        norm = lambda t: re.sub(r"\s+", " ", str(t or "")).strip().lower()
        by_qn = {}
        for it in items:
            qn = it.get("_qn")
            if qn is None:
                continue
            t = norm(it.get("solution_text"))
            if len(t) < 80:
                continue
            by_qn.setdefault(qn, t)
        keys = sorted(by_qn)
        out = []
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                sim = _d.SequenceMatcher(None, by_qn[a][:400],
                                         by_qn[b][:400]).ratio()
                if sim >= thresh:
                    out.append((a, b, sim))
        return out

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
        # RUN-32: scan the pages the CROPS actually covered, not just the
        # clamped zone. _resolve_zones clamps every zone at ch_last, but
        # detect_boundaries scans ch_last+2 and header_index.intervals closes
        # the last block at file_end -- so a block whose header sits just past
        # the chapter's last page IS cropped and sent to Gemini, yet the old
        # `for p in zone_pages` loop never saw that page and never re-asked.
        # OPH-001 q23: a header-only solution ("Solution to Question 23:")
        # sanitized to empty and shipped as missing_solution with no retry.
        scan_pages = list(zone_pages or [])
        for iv in (self._visual_intervals(phase_name) or []):
            for st in (iv.get("strips") or []):
                pg = st.get("page")
                if pg is not None and pg not in scan_pages:
                    scan_pages.append(pg)
        for p in scan_pages:
            for qn in (hdrs.get(p) or set()):
                if qn not in have:
                    missing_pages.setdefault(qn, p)
        # Boilerplate (Sol 8/9) is NOT a mislabel. Do not re-ask on
        # similarity. Page-interval mismatch is flagged at commit.
        dup_force = set()
        if not missing_pages:
            return items
        qns = sorted(missing_pages)
        # RUN-32: clamp at the last page the CROPS covered, not max(zone_pages)
        # -- otherwise a re-ask for a block past the chapter end had its page
        # window clamped back inside the zone and re-read the wrong page.
        _hi = max(scan_pages) if scan_pages else max(zone_pages)
        pages = sorted({p for qn in qns
                        for p in range(missing_pages[qn],
                                       min(missing_pages[qn] + 1,
                                           _hi) + 1)})
        self.notes.append(f"{phase_name}: printed headers prove missing/"
                          f"empty block(s) q{qns} -- targeted re-ask on "
                          f"{pages}")
        self._ledger(f"REASK_{phase_name[0]}", pages, qp.PASS_STATUS_PARTIAL,
                     0, f"printed headers prove q{qns} exist; re-asking")
        # FULL prompt (JSON template included): a template-less re-ask
        # shape-drifts (OBG ch2 live: {"solutions": [...], "text": ...}).
        boundary = self._printed_boundary_note(phase_name, qns)
        for chunk in _chunk_pages(pages, PAGE_CHUNK):
            re_prompt = (
                prompt_tmpl.format(chapter_name=self.chapter_id,
                                   start=chunk[0], end=chunk[-1])
                + f"\n\nNOTE: Ab SIRF in question numbers ko extract karo: "
                  f"{qns}. Baaki sab ignore karo. WAHI JSON array format use "
                  f"karo jo upar diya hai. Ye blocks page par PRINTED hain -- "
                  f"dhundh kar nikaalo, skip mat karo." + boundary)
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
                if existing is not None and _content_ok(existing) \
                        and qn0 not in dup_force:
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
        # Visual header index (RENDER + OCR). pdftotext is never authoritative
        # when this index sees Question/Solution/Answer-Key bands.
        try:
            if not self._visual_headers:
                self._visual_headers = header_index.scan_chapter(
                    self.pdf, ch_first, ch_last)
            self._visual_headers = header_index.heal_visual_headers(
                self._visual_headers)
            vis = header_index.index_sets(self._visual_headers)
            if vis["q_ns"]:
                self._printed_q_hdrs = vis["q_hdrs"] or self._printed_q_hdrs
                self._printed_s_hdrs = vis["s_hdrs"] or self._printed_s_hdrs
                self._printed_q_max = max(vis["q_ns"])
                if printed is None:
                    printed = {"q": set(), "s": set(), "keys": []}
                printed["q"] = vis["q_pages"] or printed.get("q") or set()
                printed["s"] = vis["s_pages"] or printed.get("s") or set()
                kreg = header_index.key_region_pages(self._visual_headers)
                if kreg:
                    printed["keys"] = kreg
                elif vis["key_pages"]:
                    printed["keys"] = sorted(vis["key_pages"])
                print(f"[BPH] {self.chapter_id}: VISUAL header index "
                      f"Q{sorted(vis['q_ns'])} S{sorted(vis['s_ns'])} "
                      f"key_pages={sorted(vis['key_pages'])} "
                      f"n={len(self._visual_headers)}")
        except Exception as e:
            self.notes.append(f"visual header index failed ({e})")
        if not printed:
            q_pages, a_pages, s_pages = self._clamp_zone_order(
                m_q, m_a, m_s, ch_first, ch_last)
            self._log_zone_audit(bounds, m_q, m_a, m_s, None,
                                 q_pages, a_pages, s_pages, ch_first, ch_last)
            return q_pages, a_pages, s_pages
        q_pages = m_q
        if printed["q"]:
            # Inclusive [min, max] of printed Question headers only.
            # OBG-001 live: max Q header is p12 (Q25-26 + key start);
            # the old `max+1` then another `+1` pulled p13 (solutions) into Q.
            # RUN-31: headers past ch_last belong to the NEXT chapter and must
            # not stretch this one (OPH-001: a p24 header made Q 4-11 -> 4-22).
            spanned = _zone_pages_from_headers(printed["q"], ch_last)
            if spanned:
                q_pages = spanned
        s_pages = m_s
        if printed["s"]:
            spanned = _zone_pages_from_headers(printed["s"], ch_last)
            if spanned:
                s_pages = spanned
        a_pages = m_a
        if printed["keys"]:
            a_pages = sorted(set(printed["keys"]))
        q_pages, a_pages, s_pages = self._clamp_zone_order(
            q_pages, a_pages, s_pages, ch_first, ch_last)
        if q_pages != m_q or s_pages != m_s or a_pages != m_a:
            self.notes.append(
                f"zones corrected by PRINTED headers (model said "
                f"Q{m_q[0] if m_q else '?'}-{m_q[-1] if m_q else '?'}/A{m_a}/"
                f"S{m_s[0] if m_s else '?'}-{m_s[-1] if m_s else '?'}; printed says "
                f"Q{q_pages[0] if q_pages else '?'}-{q_pages[-1] if q_pages else '?'}/"
                f"A{a_pages}/S{s_pages[0] if s_pages else '?'}-"
                f"{s_pages[-1] if s_pages else '?'})")
        self._log_zone_audit(bounds, m_q, m_a, m_s, printed,
                             q_pages, a_pages, s_pages, ch_first, ch_last)
        return q_pages, a_pages, s_pages

    def _clamp_zone_order(self, q_pages, a_pages, s_pages, ch_first, ch_last):
        """Solution cannot start before the question block.

        Printed-header overrides can pick a false-positive on the chapter
        TITLE page (OBG-002 live: S used 31-46 while Q started at 32).
        Drop S pages before Q start; prefer S pages at/after the answer-key
        (or last Q page). Numbered 'Solution to Question N' headers win
        over a bare 'Detailed Explanations' hit on an earlier page.
        """
        q_pages = list(q_pages or [])
        a_pages = list(a_pages or [])
        s_pages = list(s_pages or [])
        if not q_pages or not s_pages:
            return q_pages, a_pages, s_pages
        q_lo = min(q_pages)
        floor = q_lo
        if a_pages:
            floor = max(floor, min(a_pages))
        elif q_pages:
            floor = max(floor, max(q_pages))
        numbered = []
        for pg, qs in (getattr(self, "_printed_s_hdrs", None) or {}).items():
            if qs and pg >= q_lo:
                numbered.append(pg)
        if numbered:
            valid = [pg for pg in s_pages if pg >= min(numbered)]
        else:
            valid = [pg for pg in s_pages if pg >= q_lo]
            after_floor = [pg for pg in valid if pg >= floor]
            if after_floor:
                valid = after_floor
        if valid:
            s_pages = _inclusive_pages(valid, ch_last)
        return q_pages, a_pages, s_pages

    def _log_zone_audit(self, bounds, m_q, m_a, m_s, printed,
                        q_pages, a_pages, s_pages, ch_first, ch_last):
        """Always print RAW model JSON vs model-derived vs printed vs USED
        so a silent mismatch cannot hide (BUG 1)."""
        raw = json.dumps(bounds, ensure_ascii=False)
        print(f"[BPH] {self.chapter_id}: RAW boundary JSON: {raw}")
        print(f"[BPH] {self.chapter_id}: model-derived zones "
              f"Q {self._fmt_span(m_q)} | A {m_a or '-'} | S {self._fmt_span(m_s)}")
        if printed:
            print(f"[BPH] {self.chapter_id}: printed-header probe "
                  f"Q pages {sorted(printed.get('q') or [])} | "
                  f"keys {printed.get('keys') or []} | "
                  f"S hits {sorted(printed.get('s') or [])}")
        else:
            print(f"[BPH] {self.chapter_id}: printed-header probe: none "
                  f"(scanned / empty text layer) -- model zones stand")
        print(f"[BPH] {self.chapter_id}: USED zones "
              f"Q {self._fmt_span(q_pages)} | A {a_pages or '-'} | "
              f"S {self._fmt_span(s_pages)} "
              f"(chapter file pages {ch_first}-{ch_last})")
        if s_pages and q_pages and min(s_pages) < min(q_pages):
            print(f"[BPH] {self.chapter_id}: IMPOSSIBLE zone leftover: "
                  f"S starts at {min(s_pages)} before Q {min(q_pages)}")

    @staticmethod
    def _fmt_span(pages):
        pages = list(pages or [])
        if not pages:
            return "-"
        return f"{pages[0]}-{pages[-1]}"


    def _key_table_png_parts(self, a_pages, dpi=None):
        """300dpi washed table strips -> Gemini image parts. Not cached."""
        dpi = int(dpi or KEY_TABLE_DPI)
        strips = header_index.key_region_strips(self._visual_headers, a_pages)
        if not strips:
            strips = [{"page": p, "y_hi": 9999.0, "y_lo": 0.0} for p in a_pages]
        parts = []
        for st in strips:
            raw = _crop_strip_png(self.pdf, st["page"], st["y_hi"], st["y_lo"],
                                  dpi=dpi)
            if not raw:
                continue
            try:
                from PIL import Image
                import io
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                im = header_index.wash_key_crop(im)
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                raw = buf.getvalue()
            except Exception:
                pass
            parts.append({"mime_type": "image/png",
                          "data": base64.b64encode(raw).decode()})
        return parts

    def _parse_key_letter_map(self, raw):
        items = _unwrap_items(_parse_json(raw))
        out = {}
        if not isinstance(items, list):
            return out
        for it in items:
            if not isinstance(it, dict):
                continue
            _normalize_phase_item("Answer-key", it)
            qn = _item_qn(it)
            let = str(it.get("correct_option") or "").strip().upper()
            if qn is None or not re.fullmatch(r"[A-E]", let or ""):
                continue
            out[qn] = let
        return out

    def _gemini_key_table_once(self, a_pages, parts, tag):
        """One independent full-table transcription. Never reuse a prior call."""
        lo, hi = (min(a_pages), max(a_pages)) if a_pages else (0, 0)
        prompt = ANSWER_KEY_PROMPT.format(
            chapter_name=self.chapter_id, start=lo, end=hi)
        files = [
            f"KEY TABLE CROP ONLY (file pages {list(a_pages)}, {KEY_TABLE_DPI} "
            f"dpi, watermark/header/footer trimmed). Independent read {tag}. "
            f"Do not reuse memory of a previous answer.",
        ]
        files.extend(parts)
        files.append(prompt)
        try:
            raw = self._gen(files)
        except ModelBlocked:
            self._ledger(f"A_DUAL_{tag}", a_pages, qp.PASS_STATUS_UNRESOLVED,
                         0, "model blocked on key table")
            return {}
        mp = self._parse_key_letter_map(raw)
        self._ledger(f"A_DUAL_{tag}", a_pages,
                     qp.PASS_STATUS_SUCCESS if mp else qp.PASS_STATUS_PARTIAL,
                     len(mp), f"independent key read {tag}")
        return mp

    def _gemini_key_row_third(self, a_pages, parts, qns):
        """Narrower re-ask for mismatched rows only. Does not pick a winner."""
        if not qns or not parts:
            return {}
        lo, hi = (min(a_pages), max(a_pages)) if a_pages else (0, 0)
        prompt = ANSWER_KEY_PROMPT.format(
            chapter_name=self.chapter_id, start=lo, end=hi)
        prompt += (f"\n\nNOTE: SIRF in q_no rows ko padho: {sorted(qns)}. "
                   f"Baaki ignore. Guess mat karo.")
        files = [f"KEY TABLE ROW TIE-CHECK q{sorted(qns)} pages {list(a_pages)}"]
        files.extend(parts)
        files.append(prompt)
        try:
            raw = self._gen(files)
        except ModelBlocked:
            return {}
        return {n: let for n, let in self._parse_key_letter_map(raw).items()
                if n in set(qns)}

    def _extract_key_dual(self, a_pages):
        """Two independent Gemini reads of the SAME 300dpi table crop.

        Agree -> letter. Disagree -> one row-narrow third read (audit only);
        still no guess: letter stays empty + key_conflict.
        """
        parts = self._key_table_png_parts(a_pages, dpi=KEY_TABLE_DPI)
        if not parts:
            self.notes.append("key dual: no table crop rendered")
            return []
        m1 = self._gemini_key_table_once(a_pages, parts, "1")
        m2 = self._gemini_key_table_once(a_pages, parts, "2")
        disagree = sorted({n for n in set(m1) | set(m2)
                           if m1.get(n) != m2.get(n)})
        third = {}
        if disagree:
            third = self._gemini_key_row_third(a_pages, parts, disagree)
            self.notes.append(f"key dual mismatch rows {disagree} -- third "
                              f"read recorded, no guess")
        merged = header_index.merge_dual_key_reads(m1, m2, third)
        items = []
        for n, rec in sorted(merged.items()):
            ev = dict(rec)
            ev["pages"] = list(a_pages)
            self._key_evidence[n] = ev
            items.append({"_qn": n, "q_no": str(n),
                          "correct_option": ev.get("letter"),
                          "low_confidence": not ev.get("agree"),
                          "_key_evidence": ev})
        n_ok = sum(1 for v in merged.values() if v.get("agree"))
        print(f"[BPH] {self.chapter_id}: key dual Gemini "
              f"agree={n_ok}/{len(merged)} mismatch={disagree or '-'}")
        return items

    def _attach_key_evidence(self, a_pages):
        """Answers are READY only with key-TABLE evidence (pixels/OCR of the
        table pages). pdftotext _printed_key is a hint, never enough alone
        on a garbled page. Gemini table letters must cite this map."""
        ev = dict(self._key_evidence or {})
        if a_pages:
            try:
                ocr_map = header_index.ocr_key_table(
                    self.pdf, a_pages, dpi=KEY_TABLE_DPI,
                    recs=self._visual_headers)
            except Exception:
                ocr_map = {}
            for n, let in ocr_map.items():
                prev = ev.get(n) or {}
                if prev.get("agree") and prev.get("letter") == let:
                    prev = dict(prev)
                    prev["ocr"] = let
                    ev[n] = prev
                elif prev.get("agree") and prev.get("letter") and prev["letter"] != let:
                    prev = dict(prev)
                    prev["method"] = "key_conflict"
                    prev["conflict"] = let
                    prev["agree"] = False
                    ev[n] = prev
                    self.notes.append(
                        f"key dual vs OCR conflict q{n}: "
                        f"dual={prev.get('letter')} ocr={let}")
                elif not prev.get("letter"):
                    ev[n] = {"letter": let, "method": "key_table_ocr",
                             "pages": list(a_pages)}
        # Dual independent read: CLEAN text-layer on KEY PAGES only.
        # Disagree with OCR → flag, do not pick a winner (flag-don't-fix).
        layer = {}
        for p in (a_pages or []):
            try:
                txt = qp.pdftotext_page(self.pdf, p) or ""
            except Exception:
                txt = ""
            if header_index.text_layer_health(txt) != "CLEAN":
                continue
            layer.update(header_index.parse_key_rows_from_ocr_text(txt))
        for n, let in layer.items():
            if n in ev and ev[n].get("letter") and ev[n]["letter"] != let:
                ev[n]["conflict"] = let
                ev[n]["method"] = "key_conflict"
                self.notes.append(
                    f"key dual-read conflict q{n}: "
                    f"ocr={ev[n].get('letter')} layer={let}")
            elif n not in ev:
                ev[n] = {"letter": let, "method": "text_layer_hint",
                         "pages": list(a_pages or [])}
        self._key_evidence = ev
        self._key_evidence_required = bool(a_pages) or any(
            v.get("method") in ("key_table_ocr", "key_dual_gemini")
            for v in ev.values())
        if ev:
            print(f"[BPH] {self.chapter_id}: key evidence for "
                  f"{len(ev)} row(s) ({','.join(sorted({v['method'] for v in ev.values()}))})")
        return ev

    def _write_audit_artifacts(self, ch_first, ch_last, q_items, a_items, s_items):
        """Sidecar audit files. Never changes questions.jsonl / zip schema."""
        root = qp.DATA_DIR / "audit" / self.chapter_id
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / "header_index.json").write_text(
                json.dumps(self._visual_headers or [], indent=2,
                           ensure_ascii=False), encoding="utf-8")
            health = {}
            for p in range(int(ch_first), int(ch_last) + 1):
                try:
                    t = qp.pdftotext_page(self.pdf, p) or ""
                except Exception:
                    t = ""
                health[str(p)] = header_index.text_layer_health(t)
            (root / "health_map.json").write_text(
                json.dumps(health, indent=2), encoding="utf-8")
            zones = {k: list(v) if not isinstance(v, list) else v
                     for k, v in (self._zones or {}).items()}
            (root / "zone_regions.json").write_text(
                json.dumps(zones, indent=2), encoding="utf-8")
            (root / "answer_key.json").write_text(
                json.dumps(self._key_evidence or {}, indent=2),
                encoding="utf-8")
            (root / "ledger.json").write_text(
                json.dumps({
                    "q": sorted({i.get("_qn") for i in q_items
                                 if i.get("_qn") is not None}),
                    "a": sorted({i.get("_qn") for i in a_items
                                 if i.get("_qn") is not None}),
                    "s": sorted({i.get("_qn") for i in s_items
                                 if i.get("_qn") is not None}),
                    "visual_q": sorted({r["n"] for r in (self._visual_headers or [])
                                        if r.get("type") == header_index.T_QUESTION
                                        and r.get("n")}),
                    "visual_s": sorted({r["n"] for r in (self._visual_headers or [])
                                        if r.get("type") == header_index.T_SOLUTION
                                        and r.get("n")}),
                }, indent=2), encoding="utf-8")
        except Exception as e:
            self.notes.append(f"audit artifacts write failed: {e}")

    def _ledger_lock(self, q_items, a_items, s_items):
        """Chapter LOCK from identity sets — never from Gemini 'LOCKED'."""
        q = {i.get("_qn") for i in q_items if i.get("_qn") is not None}
        a = {i.get("_qn") for i in a_items if i.get("_qn") is not None}
        s = {i.get("_qn") for i in s_items if i.get("_qn") is not None}
        if not q:
            return False, "no extracted questions"
        # OCR visual miss is NOT lock truth (Ch1 S14, Ch3 Q7). Join on q_no.
        key_ns = {n for n, v in (self._key_evidence or {}).items()
                  if v.get("letter") and v.get("method") != "key_conflict"}
        if key_ns and q != key_ns:
            return False, f"extracted Q {sorted(q)} != key rows {sorted(key_ns)}"
        if a and q != a:
            return False, f"Q {sorted(q)} != A {sorted(a)}"
        if s and q != s:
            return False, f"Q {sorted(q)} != S {sorted(s)}"
        if not a:
            return False, "no answer-key rows"
        if not s:
            return False, "no solutions"
        return True, "sets equal"

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
            if q.get("_exceeded_attempts") or srow.get("_exceeded_attempts"):
                reasons.append(
                    "verify exceeded attempts after one widened-crop retry")
            if srow.get("_split_note"):
                reasons.append("solution text split at printed 'Solution "
                               "to Question N:' header boundary (deterministic "
                               "C1 backstop) -- verify manually: "
                               + str(srow.get("_split_note"))[:220])
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
            # RUN-34: strip page furniture HERE, not later, so the master row
            # and the split row both get the cleaned text. OPH-001 shipped
            # "5 Sold by @itachibot" inside q7's stem and "12 Sold by
            # @itachibot" between two clauses of q1's solution.
            stem_txt, n_f = qp.strip_page_furniture(
                str(q.get("stem") or "").strip())
            sol_txt, n_fs = qp.strip_page_furniture(
                str(srow.get("solution_text") or "").strip())
            opt_txt = {}
            for _k, _v in (opts or {}).items():
                _cv, _n = qp.strip_page_furniture(str(_v or ""))
                opt_txt[_k] = _cv
                n_f += _n
            if n_f or n_fs:
                reasons.append(
                    f"stripped {n_f + n_fs} page-furniture line(s) "
                    f"(reseller stamp / publisher mark / page number)")
            records[qn] = {
                "q_no": qn,
                "question_text": stem_txt,
                "options": opt_txt,
                "correct_option": amap.get(qn),
                "solution_text": sol_txt,
                "tables": qp._dedupe_tables(srow.get("tables") or []),
                "has_figure_in_question": bool(q.get("has_figure")),
                "has_figure_in_solution": bool(srow.get("has_figure")),
                # RUN-33: where the TEXT came from. crop_parse tags its own
                # output "_method": "geometric_text"; a model read leaves it
                # absent. Downstream checks that only make sense for
                # model-written text (the [IMG] placeholder count) need to
                # tell the two apart.
                "_q_text_method": str(q.get("_method") or ""),
                "_s_text_method": str(srow.get("_method") or ""),
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
            ev = (self._key_evidence or {}).get(qn)
            if ev:
                records[qn]["_key_evidence"] = ev
                if ev.get("method") == "key_conflict":
                    records[qn]["_review_reasons"].append(
                        f"dual key read conflict "
                        f"{ev.get('letter')} vs {ev.get('conflict')}")
            elif records[qn].get("correct_option") and self._key_evidence_required:
                records[qn]["_review_reasons"].append(
                    "answer has no key-table evidence (not READY)")
            records[qn]["_key_evidence_required"] = self._key_evidence_required
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
        vis_by_page = {}
        for r in (self._visual_headers or []):
            vis_by_page.setdefault(int(r["page"]), []).append(
                header_index.block_headers_for_page([r], r["page"])[0]
                if header_index.block_headers_for_page([r], r["page"])
                else None)
        vis_clean = {}
        for p, lst in vis_by_page.items():
            vis_clean[p] = [x for x in lst if x]
        qp._VISUAL_HEADERS_BY_PAGE = vis_clean
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
                active_block = (seed[0], seed[1], prev)
                break
        for page in pages:
            imgs = qp.extract_real_images(self.pdf, page, watermark_ids,
                                          self.subject,
                                          qp.ASSETS_DIR / "questions",
                                          skip_hashes=chapter_owned_hashes)
            if not imgs:
                qp.share_reprint_obj_ids(
                    self.pdf, page, True, chapter_records,
                    image_files_by_q, self.subject, self.chapter_no,
                    visual_recs=self._visual_headers)
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
                still = []
                for rel in leftover:
                    y_img = None
                    try:
                        oid = int(Path(rel).stem.rsplit("-", 1)[-1])
                        info = (pos or {}).get(oid)
                        if info:
                            y_img = info[0]
                            h = info[4] if len(info) > 4 else 0
                            if h and y_img + h < y_img:
                                y_img = y_img + h
                    except Exception:
                        y_img = None
                    moved = qp.try_reassign_cap_hit(
                        rel, page, y_img, chapter_records, image_files_by_q,
                        self.subject, self.chapter_no,
                        visual_recs=self._visual_headers)
                    if not moved:
                        still.append(rel)
                leftover = still
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
                active_block = (last[0], last[1], page)
        qp._VISUAL_HEADERS_BY_PAGE = {}
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
        qp.flag_high_image_counts(chapter_records, image_files_by_q)
        qp.apply_img_placeholder_reconcile(chapter_records, image_files_by_q)
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
        # LABEL-MISASSIGNMENT GATE (ANAT-001 live): if two questions still
        # ship near-identical solution text (the model re-ask keeps
        # mislabelling), the chapter must NOT look clean -- BLOCKER + zip
        # shut until a human decides. Never silent.
        # Shared didactic boilerplate (Sol 8/9) is NOT a blocker.
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
            # BUG 2: a non-empty gate must NEVER lock. Cross-check LOCKED
            # while missing_solution/INCOMPLETE shipped as lock=True (OBG-001).
            if locked:
                print(f"  [GATE] {self.chapter_id}: forcing lock=False "
                      f"(export-gate violations present)")
                locked = False
        else:
            print(f"  [GATE] {self.chapter_id}: export gate CLEAN "
                  f"(stems/options/answers/solutions/images/assets all "
                  f"accounted)")

        # Master rows FIRST (run-29). The split layer used to be written
        # before chapter_rows existed, so it could only see the raw
        # chapter_records and had to invent its own structural
        # extraction_status -- leaving qa_status (READY / REVIEW_NEEDED /
        # INCOMPLETE) stranded in data/questions.jsonl, which final_export.zip
        # does not ship. Building the rows here lets the SAME verdict be
        # copied verbatim onto the split rows instead of being re-derived.
        # Nothing the split layer reads depends on the master file being
        # written after it, so the swap is safe; the master write still
        # precedes _close_previous_flags, which needs rows on disk.
        gate_by_qn = {}
        for kind, qn_v, detail in violations:
            # NOTE: chapter/page-scope violations carry qn=None or even the
            # pages LIST (unresolved_page_* rows) -- only a true int is a
            # question number that row-level gate notices can attach to.
            if isinstance(qn_v, int):
                gate_by_qn.setdefault(qn_v, []).append((kind, detail))
        chapter_rows = []
        # Per-question verdict map handed to the split layer (run-29): the
        # split rows copy these three fields verbatim, so final_export.zip is
        # distinguishable row-by-row instead of shipping REVIEW_NEEDED rows
        # that look identical to READY ones. Keyed here, where qn is known --
        # the built master row carries only "id", no q_no.
        row_status = {}
        for qn, rec in sorted(chapter_records.items(), key=lambda x: x[0]):
            _row = qp.build_final_question(
                self.subject, self.chapter_id, self.chapter_no, qn, rec,
                image_files_by_q.get(qn, {"question": [], "solution": []}),
                source_pages=qn_source_pages.get(qn),
                ownership_pages=ownership_pages,
                gate_notices=gate_by_qn.get(qn, []))
            chapter_rows.append(_row)
            row_status[int(qn)] = {
                "qa_status": _row.get("qa_status"),
                "qa_reasons": _row.get("qa_reasons") or [],
                "manual_review": bool(_row.get("manual_review")),
            }
        questions_path = qp.DATA_DIR / "questions.jsonl"
        qp.rewrite_questions_file(questions_path, self.chapter_id, chapter_rows)
        qp.write_chapter_file(self.subject, self.chapter_id, chapter_rows)

        # Attribution mix for this chapter (run-29). Recomputed from the
        # append-only ledgers, so the carry rate is a reported number instead
        # of something counted out of "[IMG] ... active-block carry" log
        # lines. Nothing here blocks the export -- it is the metric that says
        # whether figure attribution is actually improving.
        try:
            _attrib = qp.image_attribution_summary(self.chapter_id)
            _share = _attrib["carry_share"]
            print(f"  [IMG] {self.chapter_id}: attribution "
                  f"{_attrib['positional']} block-position / "
                  f"{_attrib['carry']} carry / {_attrib['model']} model / "
                  f"{_attrib['unclaimed']} unclaimed"
                  + (f" | carry share {_share:.0%} of "
                     f"{_attrib['claimed_total']} claimed"
                     if _share is not None else " | no claimed figures"))
        except Exception as _ae:
            print(f"  [IMG] {self.chapter_id}: attribution summary failed "
                  f"({_ae}) -- non-fatal, counts unavailable")

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
                ownership_pages=ownership_pages,
                row_status=row_status)
            print(f"  [SPLIT] {self.chapter_id}: "
                  f"{split_completeness.get('question_records')} questions / "
                  f"{split_completeness.get('answer_records')} answers / "
                  f"{split_completeness.get('solution_records')} solutions")
        except Exception as e:
            print(f"  [SPLIT] {self.chapter_id}: split-layer error ({e}) -- "
                  f"master output unaffected, split files NOT written")

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
        return chapter_rows, locked

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
        q_ok = self._recheck_unverified("Question", q_items, q_ok)
        if q_ok is not True:
            self.notes.append(f"question phase unresolved: {q_ok}")
        qp.save_state(self.state)
        if a_pages:
            a_items = self._extract_key_dual(a_pages)
            a_ok = True
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
        self._attach_key_evidence(a_pages if a_pages else q_pages)
        # Dual Gemini is authoritative when both reads agree. OCR at 300dpi
        # may only CONFIRM or force REVIEW — never replace a dual letter.
        for it in a_items:
            qn = it.get("_qn")
            ev = (self._key_evidence or {}).get(qn)
            if not ev:
                continue
            it["_key_evidence"] = ev
            if ev.get("agree") and ev.get("letter") and ev.get("method") != "key_conflict":
                it["correct_option"] = ev["letter"]
            elif ev.get("method") == "key_conflict":
                it["correct_option"] = ev.get("letter")
                it["low_confidence"] = True
        s_items = self._extract_phase(s_pages, SOLUTION_PROMPT, "Solution", "S")
        s_items = self._c1_split_solutions(s_items)
        s_items = self._flag_solution_interval_mismatch(s_items)
        s_items, s_ok = self._verify_phase("Solution", s_items, s_pages)
        s_items = self._printed_header_reask("Solution", s_items, s_pages,
                                             "_printed_s_hdrs", SOLUTION_PROMPT)
        s_items = self._c1_split_solutions(s_items)
        s_items = self._flag_solution_interval_mismatch(s_items)
        # After the re-ask AND the C1 split, so the re-check sees exactly the
        # items that will be committed.
        s_ok = self._recheck_unverified("Solution", s_items, s_ok)
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

        # LOCK is a LEDGER, not a Gemini token (final rule).
        # header_q_nos == extracted == key_rows == solution_headers.
        self._write_audit_artifacts(ch_first, ch_last, q_items, a_items, s_items)
        locked, lock_why = self._ledger_lock(q_items, a_items, s_items)
        if not locked:
            self.notes.append("ledger lock refused: " + lock_why)
        print(f"[BPH] {self.chapter_id}: ledger lock={locked} ({lock_why})")

        page_section = {p: "S" for p in s_pages}
        rows, locked = self._commit(ch_first, ch_last, page_section,
                            q_items, a_items, s_items, locked, pre_rows)
        return {"locked": locked, "committed": True,
                "chapter_id": self.chapter_id, "questions": len(q_items),
                "answers": len(a_items), "solutions": len(s_items),
                "rows_written": len(rows), "notes": self.notes}


def unlock_gated_chapters(state):
    """BUG 2 backfill: a chapter that shipped lock=True despite export-gate
    violations (OBG-001 missing_solution) must leave chapters_done so the
    next run re-extracts it into review instead of treating it as clean."""
    gate = qp.DATA_DIR / "export_gate.jsonl"
    if not gate.exists() or not state:
        return
    dirty = set()
    try:
        for line in gate.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            cid = row.get("chapter_id")
            kind = row.get("kind") or ""
            if cid and kind in (
                    "missing_solution", "missing_answer", "missing_stem",
                    "bad_options", "duplicate_solution"):
                dirty.add(cid)
    except Exception:
        return
    if not dirty:
        return
    progress = state.get("pdf_progress") or {}
    n = 0
    for subj, rec in progress.items():
        done = rec.get("chapters_done") or []
        keep = [c for c in done if c not in dirty]
        if len(keep) != len(done):
            rec["chapters_done"] = keep
            n += len(done) - len(keep)
    if n:
        qp.save_state(state)
        print(f"[BPH] unlocked {n} chapter(s) that had export-gate "
              f"violations -- they will re-extract on this run")


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
    unlock_gated_chapters(state)
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
