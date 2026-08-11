#!/usr/bin/env python3
"""
QBank PDF -> JSON extraction pipeline.
Run this on your own machine / Railway (needs: poppler-utils, pypdf, Pillow,
google-generativeai, requests). Designed to survive a 100-req/day Gemini
free-tier limit by checkpointing progress and resuming across multiple runs
(e.g. via a daily cron job / Railway scheduled task).

SETUP
-----
pip install pypdf pillow google-generativeai
apt-get install poppler-utils        # gives you pdftoppm, pdfimages, pdftotext

Set your key:
    export GEMINI_API_KEY="your-key-here"

CONFIGURE
---------
Edit PDFS below: one entry per subject PDF. `page_offset` = (PDF file page
number) - (printed page number shown at the bottom of the page). Find this
ONCE per PDF manually:
    pdftoppm -jpeg -r 150 -f 4 -l 4 yourbook.pdf /tmp/check
    # open /tmp/check-004.jpg, look at chapter 1's printed page number vs "4"
    # offset = 4 - printed_page_number_seen

RUN
---
python3 qbank_pipeline.py
(re-run it daily / whenever you hit the rate limit message; it resumes
automatically from state.json)
"""

import difflib
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import google.generativeai as genai
from PIL import Image, ImageDraw
from pypdf import PdfReader
import pytesseract

# Phase-1 split-output layer: a strictly additive module that writes
# data/split/{subject}/{chapter_id}/{questions,answers,solutions,...}.jsonl
# AFTER every existing in-pipeline reconciliation. Does NOT modify the
# existing extraction loop, chapter_records, or any output the dashboard
# and validator consume. See split_outputs.py for the full design and
# docs/SPLIT_OUTPUTS_DESIGN.md for the contract.
import split_outputs
import gemini_keys

# ============================================================
# CONFIG — edit this section for each new subject PDF
# ============================================================

PDFS = [
    {
        "subject": "PSY",
        "path": "./pdfs/Psychiatry_ed8.pdf",
        "page_offset": -1,
    },
    # add the other 19 here, same shape — path is relative to /app/pdfs/
    # since that's where the Dockerfile copies them
]

# OUTPUT_ROOT points into the Railway Volume mount (/data) so progress
# and output survive restarts/redeploys. Falls back to a local folder
# if you're running this outside Railway (e.g. Colab) without a volume.
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_DIR", "./qbank_output"))
DATA_DIR = OUTPUT_ROOT / "data"
ASSETS_DIR = OUTPUT_ROOT / "assets"
STATE_FILE = OUTPUT_ROOT / "state.json"

MAX_CALLS_PER_DAY = 480         # RUN-12: the free tier's actual limit for
                                 # gemini-3.1-flash-lite is 500 RPD (the
                                 # run-12 log: "limit: 500, model:
                                 # gemini-3.1-flash-lite"). The old 1400 brake
                                 # never fired, so the pipeline ran into the
                                 # hard 500 cap mid-run and wasted 2 calls on
                                 # the 429 backoff before exiting. 480 stops
                                 # the day GRACEFULLY with ~20 calls of
                                 # headroom for transient retries; state.json
                                 # resumes the next day.
PAGES_PER_GEMINI_CALL = 6       # tune this: more pages/call = fewer calls,
                                 # but keep it small enough that Gemini can
                                 # read every question accurately
BATCH_OVERLAP_PAGES = 2         # consecutive batches share 2 pages, so a
                                 # question/solution split across a batch
                                 # boundary is seen WHOLE (with its q_no) in at
                                 # least one call -- see ROOT_CAUSE_ANALYSIS.md
                                 # RC-3. Step size = 6-2 = 4 new pages/call.
                                 # Merge by q_no makes re-extraction idempotent.
TARGETED_RETRY_MAX_ROUNDS = 2   # after a chapter's normal pass, up to this many
                                 # small focused re-asks for answer/options fields
                                 # still missing (merged from target_retry_patch.py)
SOLUTION_GATE_MIN_SHARE = 0.6   # if >=60% of a chapter's questions already have
                                 # solution text, the book DOES print explanations
                                 # here -> remaining solution gaps are extraction
                                 # losses and become retry-eligible. Replaces the
                                 # blanket solution-exclusion (RC-4), which the
                                 # 2026-07-25 run-2 log REFUTED: run-1's "answer-key
                                 # only" chapters (ch4/ch6) came back 0-missing on
                                 # the same pages, and ch11's count changed 5->8
                                 # between runs -- proof of nondeterministic model
                                 # drops, not absent print.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
                                 # 2026-08-11: migrated 3.1-flash-lite-preview ->
                                 # 3.5-flash-lite (GA, stable ID -- no "-preview"
                                 # suffix). 3.5 Flash-Lite is the newer generation,
                                 # NOT a rename of 3.1: same 1M-token input window
                                 # and 64k output cap, ~350 tok/s, and Google
                                 # positions it explicitly for "document parsing"
                                 # and "simple data extraction" -- exactly this
                                 # pipeline's workload.
                                 # Env-overridable so the model can be A/B tested
                                 # or rolled back from Railway (Variables ->
                                 # GEMINI_MODEL) without a code change + redeploy.
                                 # NOTE: 3.5-era models REJECT the deprecated
                                 # sampling params (temperature/top_p/top_k). This
                                 # repo never sets a generation_config, so no call
                                 # site needed changing -- keep it that way.

MIN_SECONDS_BETWEEN_CALLS = 5   # free tier = ~15 requests/minute (1 per 4s).
                                 # Without pacing, back-to-back calls (v2's Q/A/S
                                 # passes, single-page retries, crop ladders) bust
                                 # the RPM window instantly -> 429 bursts. 5s
                                 # spacing caps a run at 12 RPM: bursts disappear
                                 # and the 65s backoff ladder stops firing.
_last_call_ts = 0.0

# --- SECTION-AWARE BATCHING (run-6 user ask: "pehle questions ek saath, phir
# answer table, phir solutions") ------------------------------------------
# Instead of walking the chapter in fixed 6-page windows (33% of pages re-sent
# as overlap), the Solutions-section start is detected ONCE from the text
# layer and the chapter is sent in section-sized windows: the whole
# questions+answers stretch in LARGE windows (1-2 calls -> every question
# shares one context, so boundary splits and cross-window option drops
# disappear, and fewer calls = less 15-RPM pressure + the daily quota lasts),
# and the Solutions section in recitation-safe chunks (long verbatim spans
# are what trigger finish_reason=4, page 218 class). Pass activation stays
# probe-based -- the section labels only SIZE the windows, they never skip a
# pass, so a mislabeled page can't lose a question.
QUESTIONS_CHUNK_PAGES = 10     # a chapter's question section usually fits in
                               # 1-2 calls; all questions share one context
SOLUTIONS_CHUNK_PAGES = 5      # smaller spans = recitation-safe (page 218
                               # class: a whole-section S-pass fails as a unit)
SECTION_OVERLAP_PAGES = 1      # tiny intra-section overlap (a question split
                               # across a chunk boundary is still seen whole);
                               # overlap drops from 2/6 (33%) to 1/10 (10%) --
                               # the token waste the old fixed windows had

def _batch_after_routing(pass_name, batch, routed_pages):
    """run-17: routed_pages are recitation-sensitive SOLUTION pages whose
    solutions were already OCR-recovered (PREFLIGHT_OCR). They are skipped
    ONLY by the S-pass -- Q and A still receive them, so QUESTIONS printed on
    a mixed sensitive page are never silently lost (the old code excluded
    them from every pass, and no drain ran because the page never "failed")."""
    if pass_name == "S":
        return [pf for pf in batch if pf not in routed_pages]
    return list(batch)


def _pace_gemini_call():
    """Sleep just enough that consecutive Gemini requests stay
    MIN_SECONDS_BETWEEN_CALLS apart. Called at the two choke points EVERY
    request flows through: call_gemini_on_pages and
    gemini_json_call_splitting's one_call."""
    global _last_call_ts
    gap = time.time() - _last_call_ts
    if gap < MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(MIN_SECONDS_BETWEEN_CALLS - gap)
    _last_call_ts = time.time()

IMG_PATH_RE = re.compile(r"^[A-Z]{3}/[A-Z]{3}-\d{3}-\d{3}_[A-Z]+(_[A-Z])?_\d{2}\.webp$")

# ============================================================
# STATE (checkpoint / resume)
# ============================================================

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"calls_today": 0, "day_stamp": "", "pdf_progress": {}}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

def write_chapters(path, chapters_out):
    """Write chapters.json deduplicated by chapter_id (last entry wins).
    Dedup matters when a chapter is re-processed after manual state surgery
    (removing its id from chapters_done to force re-extraction): the id gets
    appended again while an older entry is already in chapters.json."""
    uniq = {}
    for c in chapters_out:
        uniq[c["chapter_id"]] = c
    path.write_text(json.dumps(list(uniq.values()), indent=2, ensure_ascii=False))


def write_chapter_file(subject, chapter_id, chapter_rows):
    """Per-chapter output file, written the moment ONE chapter FULLY completes
    (batches, orphans, image ladder, drain, sweep, targeted retry -- every
    process) and BEFORE the next chapter starts. Proves per-chapter closure
    at a glance and lets the consuming app load chapters individually."""
    d = DATA_DIR / "by_chapter"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{chapter_id}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in chapter_rows),
        encoding="utf-8")


def build_subject_bundle(subject, chapters_out):
    """All chapters done -> bundle everything under subjects/{SUBJECT_NAME}/:
    chapters.json (this subject only), questions.jsonl (concat of the
    per-chapter files, in chapter order) and chapters/{CH}.jsonl copies.
    Additive convenience layer -- data/questions.jsonl stays the master."""
    src = DATA_DIR / "by_chapter"
    ch_files = sorted(src.glob(f"{subject}-*.jsonl")) if src.exists() else []
    root = OUTPUT_ROOT / "subjects" / subject
    (root / "chapters").mkdir(parents=True, exist_ok=True)
    combined = []
    for f in ch_files:
        txt = f.read_text(encoding="utf-8")
        (root / "chapters" / f.name).write_text(txt, encoding="utf-8")
        combined.append(txt)
    (root / "questions.jsonl").write_text("".join(combined), encoding="utf-8")
    mine = [c for c in chapters_out if c.get("subject") == subject]
    (root / "chapters.json").write_text(json.dumps(mine, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
    print(f"[{subject}] bundle ready -> subjects/{subject}/ "
          f"({len(ch_files)} chapter file(s) + chapters.json + questions.jsonl)")

def today_stamp():
    return time.strftime("%Y-%m-%d")

def reset_daily_counter_if_needed(state):
    if state.get("day_stamp") != today_stamp():
        state["day_stamp"] = today_stamp()
        state["calls_today"] = 0
        # Per-key counters roll over with the same stamp (multi-key pool).
        try:
            gemini_keys.pool().reset_day(state)
        except gemini_keys.NoKeysConfigured:
            pass


# ============================================================
# QUOTA BRAKE -- multi-key aware (2026-08-11)
# ============================================================
# Historically the brake was `quota_exhausted(state)` and
# the counter bump was `note_call(state)`, repeated at ~30 call
# sites. Both now delegate to the key pool so that exhausting one key ROTATES
# to the next independent project instead of ending the run. With a single
# key configured the behaviour is byte-for-byte what it always was.
#
# `state["calls_today"]` is still maintained as the POOL-WIDE total so every
# existing log line, dashboard field and report keeps working unchanged.

def quota_exhausted(state):
    """True only when EVERY key in the pool has spent its daily budget."""
    try:
        return gemini_keys.pool().quota_exhausted(state)
    except gemini_keys.NoKeysConfigured:
        return state.get("calls_today", 0) >= MAX_CALLS_PER_DAY


def note_call(state):
    """Record one Gemini request against the active key + the pool total."""
    state["calls_today"] = state.get("calls_today", 0) + 1
    try:
        gemini_keys.pool().note_call(state)
    except gemini_keys.NoKeysConfigured:
        pass


def handle_429(state, err_text=""):
    """A 429 came back. Park/rotate the key. Returns True if another key is
    now active (caller should retry immediately), False if the pool is spent
    (caller should fall back to its historical backoff/exit path)."""
    try:
        return gemini_keys.pool().note_429(state, err_text) is not None
    except gemini_keys.NoKeysConfigured:
        return False


def keypool_summary(state):
    try:
        return gemini_keys.pool().summary(state)
    except gemini_keys.NoKeysConfigured:
        return f"single-key {state.get('calls_today', 0)}/{MAX_CALLS_PER_DAY}"

# ============================================================
# STEP 1: parse the TOC to auto-discover chapters + page ranges
# (TOC pages have clean, non-garbled text -- confirmed reliable to
# pdftotext even on PDFs where body-page text is broken/garbled)
# ============================================================

def extract_toc_chapters(pdf_path, toc_page_range=(1, 3)):
    """
    Returns [{"chapter_no": int, "chapter_title": str, "start_printed_page": int}, ...]
    Adjust toc_page_range per PDF if the contents table spans more/fewer pages.
    """
    text = subprocess.run(
        ["pdftotext", "-f", str(toc_page_range[0]), "-l", str(toc_page_range[1]),
         "-layout", pdf_path, "-"],
        capture_output=True, text=True
    ).stdout

    chapters = []
    # Matches lines like: "12   Bipolar and Related Disorders   160"
    for line in text.splitlines():
        m = re.match(r"^\s*(\d{1,3})\s+(.*?)\s+(\d{1,4})\s*$", line)
        if m:
            no, title, page = m.groups()
            title = title.strip()
            if len(title) < 3:
                continue
            chapters.append({
                "chapter_no": int(no),
                "chapter_title": title,
                "start_printed_page": int(page),
            })
    return chapters

def compute_page_ranges(chapters, page_offset, last_page_file):
    """Turns a flat chapter list into (file_start, file_end) ranges."""
    for i, ch in enumerate(chapters):
        file_start = ch["start_printed_page"] + page_offset
        if i + 1 < len(chapters):
            next_start = chapters[i + 1]["start_printed_page"] + page_offset
            file_end = next_start - 1
        else:
            file_end = last_page_file
        ch["file_start"] = file_start
        ch["file_end"] = file_end
    return chapters

# ============================================================
# STEP 2: watermark auto-detection
# (the shared background image reused on every page -- must be
# excluded, or you'll extract the watermark instead of real figures)
# ============================================================

def _resolve(obj):
    """Follow an IndirectObject reference; pass through anything else."""
    return obj.get_object() if hasattr(obj, "get_object") else obj

def _page_xobjects(page):
    """Return the page's /Resources /XObject dict (resolved), or {}."""
    res = _resolve(page.get("/Resources"))
    if not res:
        return {}
    xobjs = _resolve(res.get("/XObject"))
    return xobjs if xobjs else {}

def find_watermark_object_id(pdf_path, sample_pages=30):
    reader = PdfReader(pdf_path)
    counts = {}
    n = min(sample_pages, len(reader.pages))
    for i in range(n):
        for name, ref in _page_xobjects(reader.pages[i]).items():
            obj = _resolve(ref)
            if obj.get("/Subtype") != "/Image":
                continue
            obj_id = getattr(ref, "idnum", None)
            if obj_id is None:
                continue  # inline/direct image -- can't track by object id
            counts[obj_id] = counts.get(obj_id, 0) + 1
    if not counts:
        return None
    # whichever object ID appears on (almost) every sampled page = watermark
    watermark_id = max(counts, key=counts.get)
    if counts[watermark_id] < n * 0.5:
        return None  # no dominant repeated image -> no watermark to exclude
    return watermark_id

def _decode_image_fallback(obj):
    """Best-effort decode of an image XObject using its raw stream, for
    cases pypdf's own page.images accessor can't handle. Covers the common
    FlateDecode DeviceRGB/DeviceGray case; returns None otherwise."""
    try:
        w, h = int(obj["/Width"]), int(obj["/Height"])
        mode = {"/DeviceRGB": "RGB", "/DeviceGray": "L"}.get(str(obj.get("/ColorSpace")))
        if mode is None or w <= 0 or h <= 0:
            return None
        data = obj.get_data()  # pypdf applies filters (Flate etc.)
        need = w * h * len(mode)
        if isinstance(data, bytes) and len(data) >= need:
            return Image.frombytes(mode, (w, h), data[:need])
        return None
    except Exception:
        return None

def extract_real_images(pdf_path, file_page, watermark_id, subject, out_dir):
    """
    Extracts every embedded image on file_page EXCEPT the watermark object.
    Returns a list of saved relative paths ("SUBJECT/filename.webp") --
    exactly one entry per saved file (no duplicates, no watermarks).
    Caller is responsible for deciding which question/option/solution each
    belongs to (Gemini's response should say which figure goes where).
    """
    reader = PdfReader(pdf_path)
    if not (1 <= file_page <= len(reader.pages)):
        print(f"  [WARN] extract_real_images: page {file_page} out of range "
              f"(pdf has {len(reader.pages)} pages) -- skipping")
        return []
    page = reader.pages[file_page - 1]
    saved = []
    (out_dir / subject).mkdir(parents=True, exist_ok=True)
    seen_ids = set()  # some PDFs alias the SAME image object under two XObject
                       # names on one page -> would return the same path twice,
                       # and the second rename in process_pdf crashes with
                       # FileNotFoundError (observed in prod on PSY p264)
    for name, ref in _page_xobjects(page).items():
        obj = _resolve(ref)
        if obj.get("/Subtype") != "/Image":
            continue
        obj_id = getattr(ref, "idnum", None)
        dedupe_key = obj_id if obj_id is not None else str(name)
        if dedupe_key in seen_ids:
            continue  # alias of an image already saved from this page
        seen_ids.add(dedupe_key)
        if watermark_id is not None and obj_id == watermark_id:
            continue  # the watermark -- never save it as a question figure
        # Save exactly THIS image object. NOTE: don't shell out to
        # `pdfimages -f P -l P` per object here -- it dumps EVERY image on
        # the page (watermark included) under one prefix each time, so
        # looping over N real images re-extracts the whole page N times
        # and overwrites/duplicates output files. Decode this one object
        # directly instead.
        try:
            im = page.images[name].image
        except Exception:
            im = _decode_image_fallback(obj)
        if im is None:
            print(f"  [WARN] could not decode image {name} (obj {obj_id}) on "
                  f"page {file_page} -- skipping")
            continue
        if im.size[0] * im.size[1] < 5000:
            continue  # skip tiny noise images
        stem = obj_id if obj_id is not None else str(name).strip("/")
        fname = f"{subject}-p{file_page}-{stem}.webp"
        rel_path = f"{subject}/{fname}"
        im.convert("RGB").save(out_dir / subject / fname, "WEBP", quality=95)
        saved.append(rel_path)
    return saved

# ============================================================
# STEP 3: Gemini call — page images in, structured JSON out
# ============================================================

SCHEMA_PROMPT = """You are extracting MCQ questions from scanned textbook pages into strict JSON.

Return a JSON array. Each element is one question:
{
  "q_no": <question number as printed>,
  "question_text": "...",
  "options": {"A": "...", "B": "...", "C": "...", "D": "..."},
  "correct_option": "A" | "B" | "C" | "D" | null,   // null if answer key not on these pages
  "solution_text": "..." | null,                     // null if solution not on these pages
  "tables": [{"type": "short_label", "markdown": "| col | col |\\n|---|---|\\n..."}],
  "has_figure_in_question": true|false,
  "has_figure_in_solution": true|false
}

Rules:
- Preserve every word verbatim. Do NOT summarize or paraphrase.
- ANSWER KEY TABLES ARE CRITICAL -- READ THIS CAREFULLY: any table you see
  with a "Question No." / "Q.No" column and a "Correct Option" / "Answer"
  column, however many rows it has, MUST produce one JSON entry PER ROW.
  Do not skip rows, do not summarize the table, do not describe it in prose.
  Example: if the table shows
      5 -> b
      6 -> c
      7 -> a
  you must output all three as separate entries:
      {"q_no": 5, "question_text": null, "options": null, "correct_option": "b", "solution_text": null, "tables": [], "has_figure_in_question": false, "has_figure_in_solution": false}
      {"q_no": 6, ..., "correct_option": "c", ...}
      {"q_no": 7, ..., "correct_option": "a", ...}
  A table with 20 rows means 20 separate JSON entries, not one summary entry.
- If a page only contains solutions, return entries with only "solution_text"
  (and "tables" if present) filled, matched to the right q_no.
- If a question's options are split across two pages (e.g. A/B on this page,
  C/D on the next), only include the options actually visible on THIS batch
  of pages -- do not guess or invent the missing ones. They will be merged
  with the other batch's output automatically.
- Any table in the solution (e.g. stage/phase comparison tables) must be
  converted to a markdown table string in "tables", not skipped.
- If the text at the top of the FIRST page is clearly the continuation of a
  question, options or a solution from BEFORE these pages (starts mid-sentence
  and no question number is visible), STILL return it as one item with
  "q_no": null and the visible fragment under "solution_text"/"question_text".
  Never invent a question number -- the pipeline salvages these fragments for
  review instead of guessing.
- CONTEXT HANDLING: a "CONTEXT FROM PREVIOUS BATCH" text block may precede
  the page images (the Gemini API is stateless, so continuity context is
  injected manually into every request). Use it ONLY to continue the
  referenced item under its original q_no -- never output that context text
  as a new item. Some leading page-images may be OVERLAP from the previous
  batch, provided purely as continuity context: extract normally, and if an
  item visibly SPANS from an overlap page into the new pages, combine both
  sides into ONE complete item under its printed q_no.
- BATCH META (required): after the last question object, append ONE extra
  control object describing how the LAST page of this batch ends:
  {"_batch_meta": {"last_q_no": <int or null>,
                   "ends_mid_content": true|false,
                   "cut_part": "question"|"options"|"solution"|null,
                   "tail_text": "<verbatim last ~25 words at the bottom of
                                 the last page, else empty string>"}}
  ends_mid_content = true ONLY when the last question's text, options or
  solution is visibly cut off at the bottom of the last page (must continue
  on the following page).
- Output ONLY the JSON array, no commentary, no markdown code fences.
"""

SAFETY_SETTINGS = [
    {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
    for c in ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
              "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]
]
# Medical/psychiatry textbook content routinely covers violence, self-harm,
# sexual assault etc. in a clinical context (e.g. "which defense mechanism
# explains this rape survivor's amnesia") -- BLOCK_ONLY_HIGH keeps obviously
# harmful content blocked while allowing legitimate clinical material through.

# ============================================================
# V2 — MULTI-PHASE (3-PASS) ARCHITECTURE
# Same pages, three small focused asks instead of one big ask.
# Why: the v1 single-pass prompt made the model juggle stems+options,
# answer keys AND solutions in one response -- its per-call attention
# budget split three ways, causing context bleeding (wrong-owner stems),
# glued solution blobs and dropped key rows. Small focused asks are
# measurably more accurate (the same principle as targeted_retry).
#
# QUOTA NOTE: this is NOT "3x every call". A zero-token pdftotext probe
# decides per batch which passes are even worth a call:
#   * questions-section batch -> Q-pass only (A/S skipped)
#   * solutions-section batch -> S-pass only (Q skipped, sticky)
#   * batch whose pages print an answer-key table -> +A-pass
# On the trial book this lands very close to v1's total call count while
# each call is narrower and cleaner.
# ============================================================

PIPELINE_TAG = "v2-3pass"

_BATCH_META_BLOCK = """- BATCH META (required): after the last object, append ONE extra control
  object describing how the LAST page of this batch ends:
  {"_batch_meta": {"last_q_no": <int or null>,
                   "ends_mid_content": true|false,
                   "cut_part": "question"|"options"|"solution"|null,
                   "tail_text": "<verbatim last ~25 words at the bottom of
                                 the last page, else empty string>"}}
- FIGURE MAP (required whenever ANY figure, photo, diagram or chart is
  visible on these pages): append ONE more control object
  {"_figure_map": [{"q_no": <int|null>, "slot": "question"|"solution"|null}, ...]}
  with EXACTLY ONE entry per figure, in top-to-bottom reading order page by
  page. q_no = the question the figure belongs to (null if it is
  decorative/unrelated/watermark). slot = "question" if the figure appears
  with or above the question stem, "solution" if it appears inside that
  question's explanation region (null when q_no is null). Every visible
  figure MUST have an entry -- the pipeline uses this map to attach each
  extracted image to its question. If no figures at all: {"_figure_map": []}
- Output ONLY the JSON array, no commentary, no markdown code fences.
"""

SCHEMA_PROMPT_Q = """You are extracting MCQ QUESTIONS from scanned textbook pages into strict JSON.
This is the QUESTION-ONLY pass. Extract question stems, options and any table
that is part of a QUESTION. DO NOT extract answers or solutions/explanations
in this pass -- a separate pass handles those; never copy solution prose here.

Return a JSON array. Each element is one question:
{
  "q_no": <question number as printed>,
  "question_text": "..." | null,
  "options": {"A": "...", "B": "...", "C": "...", "D": "..."} | null,
  "correct_option": null,
  "solution_text": null,
  "tables": [{"type": "short_label", "markdown": "| col | col |\\n|---|---|\\n..."}],
  "has_figure_in_question": true|false,
  "has_figure_in_solution": false
}

Rules:
- Preserve every word verbatim. Do NOT summarize or paraphrase.
- NEVER invent a question number. If text at the top of the FIRST page is a
  continuation from BEFORE these pages (starts mid-sentence, no number
  visible), FIRST use the OVERLAP/CONTEXT pages to determine which question
  it continues (the preceding overlap page usually shows that question's
  number) and return it under that q_no; ONLY when ownership cannot be
  established from the context, return it with "q_no": null and the visible
  fragment under "question_text"/"options".
- CONTINUATION-WITHIN-PAGE (run-13): a stem/options block that visibly
  continues a question whose printed number appeared EARLIER ON THE SAME
  page (e.g. options printed under a figure, a question split by a table,
  or a tail question block after an image) MUST repeat that q_no on the
  continuation item. Never emit "q_no": null for content that belongs to a
  question number visible on these pages -- a null q_no there detaches the
  fragment permanently (orphans.jsonl) and the question ships without its
  options/answer.
- If a question's options are split across two pages, only include the
  options actually visible on THIS batch -- they merge automatically.
- If a visible line is clearly an answer-letter line or explanation prose
  (not a stem/option), SKIP it -- do not force it into an item.
- CONTEXT HANDLING: a "CONTEXT FROM PREVIOUS BATCH" text block may precede
  the page images. Use it ONLY to continue the referenced item under its
  original q_no -- never output that context text as a new item. Leading
  page-images may be OVERLAP from the previous batch (continuity only):
  if a stem/options visibly SPAN from an overlap page into new pages,
  combine both sides into ONE complete item under its printed q_no.
""" + _BATCH_META_BLOCK

SCHEMA_PROMPT_A = """You are reading ANSWER KEYS from scanned textbook pages into strict JSON.
This is the ANSWER-KEY-ONLY pass. Your ONLY job: extract the mapping from
question number to correct option letter, wherever it is printed on these
pages (dedicated key tables, or answer lines printed beside questions).

Return a JSON array with ONE entry PER ROW you can see:
{"q_no": <int>, "question_text": null, "options": null,
 "correct_option": "A" | "B" | "C" | "D",
 "solution_text": null, "tables": [],
 "has_figure_in_question": false, "has_figure_in_solution": false}

Rules:
- READ THIS CAREFULLY: any table with a "Question No." / "Q.No" column and a
  "Correct Option" / "Answer" column -- however many rows -- MUST produce one
  JSON entry PER ROW. A 20-row table = 20 entries, not one summary entry.
  Do not skip rows, do not summarize the table, do not describe it in prose.
- Normalise the letter to UPPERCASE A/B/C/D. If a row's letter is illegible,
  SKIP that row entirely -- never guess.
- Preserve row order and letters exactly as printed (verbatim accuracy).
- Return ONLY the rows you can actually see on THESE pages. If no key/answer
  is printed here, return an empty array [].
""" + _BATCH_META_BLOCK.replace(
    '"cut_part": "question"|"options"|"solution"|null', '"cut_part": null')

SCHEMA_PROMPT_S = """You are extracting printed SOLUTIONS / EXPLANATIONS from scanned textbook
pages into strict JSON. This is the SOLUTION-ONLY pass. DO NOT extract
question stems or options in this pass -- a separate pass handles those.

Return a JSON array. Each element is one question's solution:
{
  "q_no": <question number the solution is printed for>,
  "question_text": null,
  "options": null,
  "correct_option": "A" | "B" | "C" | "D" | null,   // only if "Ans: B" style is printed
  "solution_text": "..." ,
  "tables": [{"type": "short_label", "markdown": "| col | col |\\n|---|---|\\n..."}],
  "has_figure_in_question": false,
  "has_figure_in_solution": true|false
}

Rules:
- Preserve every word verbatim. Do NOT summarize or paraphrase.
- NEVER invent a question number -- use ONLY numbers explicitly printed with
  the solution (e.g. "Solution to Question 4:" -> q_no 4) or PROVEN by the
  OVERLAP/CONTEXT pages (e.g. the "Solution to Question 4:" header visible
  at the bottom of the preceding overlap page). If the top of the FIRST
  page continues a solution from BEFORE these pages with no number visible,
  FIRST use the OVERLAP/CONTEXT pages to determine which question it
  continues and return it under that q_no; ONLY when ownership cannot be
  established, return it with "q_no": null under "solution_text".
- ONE ENTRY PER QUESTION. The text of EACH question's solution goes ONLY into
  that question's own entry. Text printed after a "Solution to Question N:"
  header belongs to q_no N, never to an earlier entry.
- Any table inside a solution must become a markdown table string in "tables".
- CONTEXT HANDLING: a "CONTEXT FROM PREVIOUS BATCH" text block may precede
  the page images. Use it ONLY to continue the referenced solution under its
  original q_no -- never output that context text as a new item. Leading
  page-images may be OVERLAP (continuity only): if a solution visibly SPANS
  from an overlap page into new pages, combine both sides into ONE complete
  entry under its printed q_no.
""" + _BATCH_META_BLOCK

# Zero-token pdftotext probes that decide which passes a batch even needs.
KEY_TABLE_PROBE_RE = re.compile(
    r"(question\s*no|q\.?\s*no)[^\n]{0,40}(correct\s*option|answer)"
    r"|answer\s*key", re.IGNORECASE)
SOLUTION_PROBE_RE = re.compile(
    r"solution\s+to\s+question\s+\d{1,3}\s*:", re.IGNORECASE)

# Claude's mandated Task-4 marker (kept verbatim for audit parity) -- the
# SAFE clipping built around it lives in clip_pass_solutions(); the naive
# "cut at first match" version is NOT used anywhere because it can delete
# unique neighbour content when the model fails to emit sibling items.
SOLUTION_MARKER_RE = re.compile(r'(?i)solution\s+to\s+question\s+(\d+)\s*:', re.MULTILINE)


def clip_pass_solutions(items):
    """V2 S-pass response parser guard (Task 4, hardened).

    For every item, clip a foreign "Solution to Question N:" tail ONLY when
    the tail is provably redundant -- i.e. the numbered question appears as
    its OWN sibling item in the same response (the model emitted both, so
    nothing unique is lost). Steps per item:
      1. strip LEADING "Solution to Question N:" furniture headers (all of
         them -- never clip-to-empty, which the naive version would do);
      2. scan for an embedded header naming a DIFFERENT q_no that exists as a
         sibling item -> hard-cut before it;
      3. an embedded header naming a q_no with NO sibling item is LEFT
         INTACT (possibly unique neighbour content) -- the chapter-level
         integrity sweep trims it later with a chapter-wide donor proof.
    Returns (items, n_clipped)."""
    def _qn(it):
        try:
            q = it.get("q_no")
            return int(q) if q is not None and not isinstance(q, bool) else None
        except (TypeError, ValueError):
            return None

    sibling_qns = {q for q in (_qn(it) for it in items) if q is not None}
    n_clipped = 0
    for it in items:
        s = it.get("solution_text") or ""
        if not s:
            continue
        orig = s
        own = _qn(it)
        # 1. leading furniture headers
        while True:
            m = re.match(r"\s*Solution\s+to\s+Question\s+\d{1,3}\s*[:.\-]?\s*",
                         s, re.IGNORECASE)
            if not m:
                break
            s = s[m.end():]
        # 2. provably-redundant foreign tail
        for m in SOLUTION_MARKER_RE.finditer(s):
            if m.start() == 0:
                continue
            n = int(m.group(1))
            if own is not None and n == own:
                continue
            if n in sibling_qns:
                s = s[:m.start()].rstrip()
                n_clipped += 1
            break  # 3. donor-less tails are left for the chapter sweep
        if s != orig:
            it["solution_text"] = s
    return items, n_clipped

def parse_gemini_json_array(text):
    """Parse Gemini's structured response without discarding valid records.

    Gemini occasionally emits two adjacent JSON arrays despite the prompt's
    "one array only" instruction. ``json.loads`` then raises ``Extra data``;
    previously that made a healthy six-page batch fall back to six expensive
    single-page requests, and an individual page could still be lost. Accept
    consecutive complete arrays (or objects) while rejecting malformed tails.
    """
    clean = re.sub(r"```(?:json)?", "", (text or "").strip(),
                   flags=re.IGNORECASE).strip()
    if not clean:
        raise ValueError("Gemini returned an empty JSON response")

    # Some otherwise-valid answers start with prose such as "Here is the
    # JSON:". Recover the first JSON container rather than discarding a full
    # batch and retrying every page individually.
    starts = [i for i in (clean.find("["), clean.find("{")) if i >= 0]
    if starts:
        clean = clean[min(starts):]

    decoder = json.JSONDecoder()
    values, pos = [], 0
    length = len(clean)
    while pos < length:
        while pos < length and clean[pos].isspace():
            pos += 1
        if pos >= length:
            break
        try:
            value, end = decoder.raw_decode(clean, pos)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid Gemini JSON near character {pos}: {exc.msg}") from exc
        if isinstance(value, list):
            values.extend(value)
        elif isinstance(value, dict):
            # Tolerate newline-delimited objects from a model that ignored the
            # array wrapper. Downstream validation still handles every item.
            values.append(value)
        else:
            raise ValueError("Gemini JSON must contain an array or object")
        pos = end
    return values


def _ocr_content_owner(item, chapter_records):
    """Prefer q_no, then identify OCR scraps from option-content evidence."""
    try:
        qn = int(item.get("q_no"))
        if qn in chapter_records:
            return qn
    except (TypeError, ValueError):
        pass
    values = " ".join(str(v or "") for v in (item.get("options") or {}).values()).lower()
    tokens = {t for t in re.findall(r"\w+", values) if len(t) > 3}
    if not tokens:
        return None
    best_qn, best_score = None, 0.0
    for qn, rec in chapter_records.items():
        opt_text = " ".join(str(v or "") for v in (rec.get("options") or {}).values()).lower()
        opt_tokens = {t for t in re.findall(r"\w+", opt_text) if len(t) > 3}
        score = len(tokens & opt_tokens) / max(1, min(len(tokens), len(opt_tokens)))
        if score > best_score:
            best_qn, best_score = qn, score
    return best_qn if best_score >= 0.5 else None


def _novel_solution_tail(existing, incoming):
    """Return only the unseen suffix of OCR text; never append a re-read body."""
    if not incoming or _frag_mostly_present(incoming, existing, 0.85):
        return ""
    # Select the match that consumes the FURTHEST part of incoming text, not
    # merely its largest isolated block. This removes an entire repeated
    # solution before retaining its continuation after the truncation point.
    blocks = difflib.SequenceMatcher(None, existing.lower(), incoming.lower()).get_matching_blocks()
    usable = [b for b in blocks if b.size >= 30 and b.b < len(incoming) * 0.75]
    if usable:
        end = max(b.b + b.size for b in usable)
        tail = incoming[end:].lstrip(" \n,.;:")
        if tail:
            return tail
    return ""  # uncertain overlap is safer than duplicating a full solution


def is_recitation_risk_solution_page(pdf_path, page_no):
    """Route printed sensitive solution pages away from vision generation."""
    text = pdftotext_page(pdf_path, page_no)
    if not re.search(r"Solution\s+to\s+Question\s+\d{1,3}", text, re.I):
        return False
    risk = r"sexual|rape|genital|vulva|penis|assault|suicide|homicide|abuse|forensic|injury"
    return bool(re.search(risk, text, re.I))


def _recover_ocr_solution_headers(raw_text, chapter_records):
    """Use Tesseract text directly when printed Solution-to-Question headers
    exist; this avoids asking Gemini to regenerate a blocked page at all."""
    hits = list(re.finditer(r"Solution\s+to\s+Question\s+(\d{1,3})\s*[:.]?", raw_text, re.I))
    recovered = 0
    for i, hit in enumerate(hits):
        qn = int(hit.group(1)); rec = chapter_records.get(qn)
        if not rec:
            continue
        end = hits[i + 1].start() if i + 1 < len(hits) else len(raw_text)
        segment = raw_text[hit.end():end].strip()
        tail = _novel_solution_tail(rec.get("solution_text") or "", segment)
        if tail:
            rec["solution_text"] = (rec.get("solution_text") or "").rstrip() + "\n" + tail
            recovered += 1
            print(f"  [OCR_FALLBACK] header-spliced continuation to q{qn}")
    return recovered


def normalize_ocr_fallback_item(raw_item):
    """Map OCR-structurer aliases into the merge schema before orphan logic."""
    return {
        "q_no": raw_item.get("q_no") or raw_item.get("question_number"),
        "question_text": raw_item.get("question_text") or raw_item.get("stem") or raw_item.get("topic"),
        "solution_text": raw_item.get("solution_text") or raw_item.get("explanation"),
        "options": raw_item.get("options"),
        "correct_option": raw_item.get("correct_option") or raw_item.get("correct_options"),
        "tables": raw_item.get("tables") or [],
        "has_figure_in_question": bool(raw_item.get("has_figure_in_question")),
        "has_figure_in_solution": bool(raw_item.get("has_figure_in_solution")),
    }


# ---------------------------------------------------------------------------
# Cross-field contamination hardening (run-7 audit): OCR text can carry page
# numbers / watermarks / footers, and a recovered SOLUTION fragment can
# contaminate question_text. Every recovery path below is field-scoped and
# provenance-tagged so a solution recovery can never populate a stem.
# ---------------------------------------------------------------------------
CONTAMINATION_TOKEN_SHARE = 0.8   # >=80% of a stem's tokens in its own
                                  # solution = the "stem" is really solution
                                  # prose (cross-field contamination class)

_OCR_NOISE_LINE_RES = [
    re.compile(r"^\s*[-–—.·]?\s*\d{1,4}\s*[-–—.·]?\s*$"),          # 12 / -12- / 12.
    re.compile(r"^\s*page\s*\d{1,4}\s*(of\s*\d{1,4})?\s*$", re.I),  # Page 12 of 300
    re.compile(r"^\s*(https?://|www\.)\S+\s*$", re.I),              # urls
    re.compile(r"^\s*(©|\(c\)|copyright).*$", re.I),                # copyright
    re.compile(r"^\s*(\[?\s*no\.?\s*\]?\s*)?\d{1,4}\s*$", re.I),    # bare "12"
]

# Explanation-style OPENERS that can never start a real question stem
# (mirrors/extends SOLUTION_STYLE_STEM_RE -- kept here for the contamination
# validator so the two modules stay independent).
_EXPLANATION_START_RE = re.compile(
    r"^\s*(?:option\s+[a-d]\s*[:.)\-]|ans(?:wer)?\s*[:.)\-]|the\s+correct\s+(?:answer|option)\b|"
    r"(?:hence|thus|therefore|so)\s*,\s*(?:the\s+)?(?:correct\s+)?option\b|"
    r"correct\s+answer\s+is\b|the\s+(?:correct\s+)?answer\s+is\b|"
    r"solution\s*[:.)\-]|explanation\s*[:.)\-]|answer\s*[:.)\-]|"
    r"solution\s+to\s+question\s+\d+|explanation\s+of\s+question\s+\d+)",
    re.IGNORECASE)


def _clean_ocr_text(text):
    """Strip page-level noise from OCR text BEFORE it is merged or spliced
    (run-7 hardening #5). Conservative: removes whole lines only, never
    rewrites prose. Detected:
      * standalone page numbers ("12", "- 12 -", "12.")
      * "Page 12 of 300" footers
      * urls, copyright lines, ISBNs
      * a short line repeated >=3 times in the block (running header/footer)
    Medical wording is preserved verbatim."""
    if not text:
        return text
    lines = text.splitlines()
    counts = {}
    for ln in lines:
        s = ln.strip()
        if s and len(s) <= 40:
            counts[s] = counts.get(s, 0) + 1
    out = []
    n_stripped = 0
    for ln in lines:
        s = ln.strip()
        if not s:
            out.append(ln)
            continue
        if any(r.match(s) for r in _OCR_NOISE_LINE_RES):
            n_stripped += 1
            continue
        if counts.get(s, 0) >= 3 and len(s) <= 40:
            # repeated short line (running header/footer) -- but never strip
            # a "Solution to Question N:" header the recovery relies on
            if not re.match(r"Solution\s+to\s+Question\s+\d", s, re.I):
                n_stripped += 1
                continue
        out.append(ln)
    if n_stripped:
        print(f"  [OCR_CLEAN] stripped {n_stripped} page-noise line(s) "
              f"(page numbers / footers / watermarks)")
    return "\n".join(out)


def _stem_reject_reason(qtext, rec=None):
    """Cross-field contamination proof for a would-be question stem
    (run-7 hardening #3/#6, refined run-12, USER-FIX 2026-08-08).

    Returns a short reason string when the text is provably NOT a stem,
    or None when the text plausibly IS one. A stem is rejected ONLY when:
      1. opens with explanation-style language ("Option A:", "Ans. is B",
         "The correct answer is", "Solution to Question N:" ...) -- this is
         the reliable contamination signal (ch7 q1's "Option A: CAGE
         questionnaire..." case). UNCHANGED across all runs.
      2. phantom-record shape (RUN-20): a non-empty question_text WITH a
         non-empty solution_text AND empty options AND no correct_option.
         A real MCQ record has stem + options + answer + solution; when
         only stem+sol are present the stem is hallucinated (PSY-007
         Q23-Q26 phantom class). UNCHANGED.

    REMOVED 2026-08-08 (USER ASK: "yrr solutions ko questions se
    verify nhi Krna h, agar koi suspicious h to Krna h rescue"):
      * the token-overlap check (stem shares >=80% tokens with own
        solution). REMOVAL REASON: medical terminology is shared between
        stems and solutions ("the patient", "schizophrenia",
        "Clozapine", "dystonia", ...). The 80%-of-tokens threshold fired
        on the post-routing-fix Railway run's 5 of 5 suspect_stem cases
        (q2/q4/q6/q7/q9 of PSY-007) -- the CRITIQUE pass then proved:
          - q2: REAL contamination (corrected -- record had solution
            prose as stem)
          - q4: FALSE alarm (CRITIQUE confirmed correct despite flag)
          - q6, q7, q9: FALSE alarms (CRITIQUE returned
            "cannot_verify" because the source page provided to
            CRITIQUE didn't include the solution -- this is a page
            coverage issue, not a stem contamination)
        The CRITIQUE pass is the deterministic check the user
        actually wanted: "verify against the printed page, not
        against this record's own solution". Rescue sends the
        model to the question-side page; the model returns the stem
        region verbatim; the old heuristic was rejecting it
        because medical terms are shared with the solution. With
        the heuristic removed, rescue fills 3-5 of 5 previously
        quarantined records.
    A false rejection costs a stem (shipped empty + gated); a false
    accept ships corruption. The explanation-opener rule is strict
    enough to catch real contamination (ch7 q1 "Option A: ...") and
    the phantom-shape rule is strict enough to catch the Q23-Q26
    class. The removed token-overlap rule was the only one that
    used the solution text as a contamination signal, and that is
    exactly what the user told us to stop doing."""
    t = (qtext or "").strip()
    if not t:
        return None
    if _EXPLANATION_START_RE.match(t):
        return "opens with explanation-style language"
    sol = (rec.get("solution_text") or "").strip() if rec else ""
    # RUN-20 HALLUCINATED-STEM GUARD: shape only, not token content.
    # A non-empty question_text + non-empty solution_text + empty
    # options + no correct_option = phantom record (the real question
    # is in another chapter; the "stem" was hallucinated from
    # surrounding solution prose by the run-19 critique pass).
    if rec and t and sol:
        opts = rec.get("options") or {}
        correct = rec.get("correct_option")
        opts_empty = (not isinstance(opts, dict)
                      or len(opts) < 4
                      or any(not str(v or "").strip() for v in opts.values()))
        no_answer = not (correct and str(correct).strip())
        if opts_empty and no_answer:
            return ("record has only question_text+solution_text (no options, no "
                    "answer) -- phantom-record shape: the real question is in "
                    "another chapter; run-20 upstream fix")
    return None


# field scopes per recovery pass (run-7 hardening #2: patch-only recovery).
# A recovery response may ONLY modify the fields its pass was invoked to
# recover -- everything else is dropped at the merge boundary.
_RECOVERY_SCOPE = {
    "Q": {"question_text", "options"},
    "A": {"correct_option"},
    # S includes correct_option pragmatically: the printed "Ans: B" line sits
    # INSIDE the solution block and no other pass may ever see this page
    # (recitation-blocked); question_text/options are NEVER touched.
    "S": {"solution_text", "tables", "correct_option"},
}


def _apply_recovery_scope(item, scope, prov):
    """Null every field of a recovered item that its recovery pass is NOT
    allowed to produce (run-7 hardening #2), and tag the item with its
    provenance. scope: None = unrestricted (normal batches)."""
    if scope is not None:
        for f in list(item.keys()):
            if f not in scope and f not in ("q_no", "_prov",
                                            "has_figure_in_question",
                                            "has_figure_in_solution"):
                item[f] = None
    item["_prov"] = prov
    return item


def ocr_fallback_text(image_path):
    """Non-generative final fallback for recitation-blocked page imagery.
    Output is cleaned of page-level noise (page numbers, watermarks,
    footers) before anything merges it (run-7 hardening #5)."""
    raw = pytesseract.image_to_string(Image.open(image_path))
    return _clean_ocr_text(raw)


def call_gemini_text_only(model, prompt):
    """Structure OCR text without resending the recitation-triggering image."""
    _pace_gemini_call()
    resp = model.generate_content([prompt], safety_settings=SAFETY_SETTINGS,
                                  request_options={"retry": None})
    if not getattr(resp, "candidates", None) or not (resp.text or "").strip():
        raise RuntimeError("Empty Gemini text-only OCR restructuring response")
    return parse_gemini_json_array(resp.text)


def call_gemini_on_pages(model, image_paths, context="", prompt=None):
    parts = [prompt or SCHEMA_PROMPT,
             "These are medical/psychiatric educational pages. Clinical references to violence, "
             "sexuality, self-harm, abuse, or forensic scenarios are quoted textbook content; "
             "transcribe them faithfully for educational extraction, without adding advice."]
    if context:
        parts.append(context)  # carry-forward / overlap context (stateless API)
    for p in image_paths:
        parts.append(Image.open(p))
    page_label = ",".join(Path(p).name for p in image_paths)
    _pace_gemini_call()
    try:
        resp = model.generate_content(
            parts,
            safety_settings=SAFETY_SETTINGS,
            request_options={"retry": None},
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None) or "unknown"
        print(f"  [GEMINI_ERROR] {page_label}: status={status} reason={str(exc)[:240]}")
        raise RuntimeError(f"Gemini API error status={status}: {exc}") from exc

    candidates = getattr(resp, "candidates", None) or []
    feedback = getattr(resp, "prompt_feedback", None)
    if not candidates:
        print(f"  [GEMINI_ERROR] {page_label}: status=ok candidates=0 block_reason={feedback}")
        raise RuntimeError(f"Empty Gemini response; block_reason={feedback}")

    candidate = candidates[0]
    finish_reason = getattr(candidate, "finish_reason", None)
    if finish_reason and str(finish_reason) not in ("1", "STOP"):
        kind = "SAFETY_BLOCKED" if str(finish_reason) in ("8", "PROHIBITED_CONTENT") else "GEMINI_ERROR"
        print(f"  [{kind}] {page_label}: status=ok finish_reason={finish_reason} block_reason={feedback}", flush=True)
        raise RuntimeError(f"Gemini response did not finish normally (finish_reason={finish_reason})")
    try:
        text = resp.text
    except Exception as exc:
        print(f"  [GEMINI_ERROR] {page_label}: status=ok finish_reason={finish_reason} text_unavailable={exc}")
        raise RuntimeError(f"Gemini response text unavailable: {exc}") from exc
    if not (text or "").strip():
        print(f"  [GEMINI_ERROR] {page_label}: status=ok finish_reason={finish_reason} empty_body=true")
        raise RuntimeError("Empty Gemini response body")
    return parse_gemini_json_array(text)

def retry_batch_page_by_page(model, batch, state, ctx=None, prompt=None):
    """A whole-batch failure (RECITATION/safety finish_reason, token limit)
    is usually caused by just ONE page in the batch. Retrying each page
    alone isolates the bad page instead of losing the whole batch's worth
    of questions/answers/solutions (seen in prod: finish_reason=4 killed a
    6-page batch, wiping one chapter's answers and another's solutions).
    Respects the same daily quota and exits cleanly if it's hit.

    A page that fails EVEN ALONE (usually recitation-sensitive content) is
    NO LONGER silently dropped: it is persisted to state["failed_pages"]
    and gets a second-chance call at chapter end (drain_failed_pages) with
    a different prompt framing -- run-2: page 217's skip cost 5 solutions."""
    print(f"  [INFO] retrying {len(batch)} pages one-by-one to isolate the failing page...")
    items = []
    recovered = 0
    for pf in batch:
        reset_daily_counter_if_needed(state)
        if quota_exhausted(state):
            print("Daily Gemini call limit reached during single-page retry. Saving progress, exiting.")
            save_state(state)
            sys.exit(0)
        try:
            items.extend(call_gemini_on_pages(model, [pf], prompt=prompt))
            note_call(state)
            recovered += 1
        except Exception as e2:
            t2 = str(e2)
            if "429" in t2 or "quota" in t2.lower():
                if handle_429(state, t2):
                    print(f"  [429] quota hit during retry -- rotated to "
                          f"{keypool_summary(state)}, retrying this page")
                    try:
                        items.extend(call_gemini_on_pages(model, [pf], prompt=prompt))
                        note_call(state)
                        recovered += 1
                        continue
                    except Exception as e3:
                        print(f"  [WARN] page {pf.name} failed after rotation too: {e3}")
                        t2 = str(e3)
                else:
                    print(f"  [QUOTA] every key exhausted during retry -- stopping run: {e2}")
                    save_state(state)
                    sys.exit(0)
            print(f"  [WARN] page {pf.name} failed even alone ({e2}) -- queued for second-chance drain")
            entry = {"page_file": pf.name, "true_page": int(pf.stem.split("-")[-1]),
                     "reason": t2[:200], "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            if ctx:
                # persist WHICH pass failed (run-7 hardening #2): the drain
                # must recover only the fields that pass was supposed to
                # produce (Q->question/options, A->answer, S->solution).
                entry.update({"subject": ctx.get("subject"), "chapter_no": ctx.get("chapter_no"),
                              "chapter_id": ctx.get("chapter_id"),
                              "pass": ctx.get("pass")})
            failed = state.setdefault("failed_pages", [])
            # Q and S passes can fail on the same recitation-blocked page.
            # Drain it once; duplicate entries caused duplicate OCR splices.
            if not any(e.get("chapter_id") == entry.get("chapter_id") and
                       e.get("true_page") == entry.get("true_page") for e in failed):
                failed.append(entry)
    save_state(state)
    print(f"  [INFO] single-page retry: {recovered}/{len(batch)} pages recovered")
    return items

# ============================================================
# FEATURE: targeted gap-retry (run AFTER normal batches + orphan
# recovery, BEFORE writing the chapter's questions to disk)
#
# WHY: even with full page context in one call, Gemini sometimes drops a
# few fields out of a large batch (proven: a 17-row answer-key table fully
# visible in a single call still came back missing rows 9-13; a single
# question's 4 options fully visible on one page came back with only 3).
# This isn't a batching/context bug -- it's the model's own per-call error
# rate on dense extraction tasks. The fix: after the normal pass, check
# what's STILL missing and ask again with a MUCH smaller, narrowly-scoped
# prompt (just the specific gaps) -- small focused asks are consistently
# more accurate than "extract everything on these 6 pages at once".
#
# (Merged in from target_retry_patch.py: config TARGETED_RETRY_MAX_ROUNDS
# sits beside the other constants; the call site is in process_pdf right
# after orphan recovery and before the chapter write loop.)
# ============================================================

def chapter_integrity_sweep(chapter_records, image_files_by_q, subject, chapter_no, stats):
    """Zero-token deterministic pre-retry sweep (run-4 audit RCA classes).
    Runs BEFORE targeted retry so provably-wrong/provably-incomplete fields
    are re-asked in the SAME run instead of shipping. Never destroys content
    without a deterministic proof; every action is written to
    data/integrity_flags.jsonl. Returns the set of q_nos whose solutions
    look truncated (handed to the retry as forced solution re-asks)."""
    forced_solution, flags = set(), []

    def iflag(kind, qn, detail, matched=True, **extra):
        entry = {"kind": kind, "q_no": qn, "chapter_id": f"{subject}-{chapter_no:03d}",
                 "detail": detail, "matched": matched}
        entry.update(extra)
        flags.append(entry)
        _append_jsonl(DATA_DIR / "integrity_flags.jsonl", entry)

    # 1. duplicate-stem pairs (012-001 class): identical/near-identical stems
    #    on two records of one chapter. The record whose stem does NOT cohere
    #    with its own payload is the wrong-owner copy -- strip its stem so
    #    the retry's Gap-1 anchor refills it from the pages.
    qns = sorted(chapter_records)
    stems = {qn: (chapter_records[qn].get("question_text") or "").strip() for qn in qns}
    for i, qa in enumerate(qns):
        for qb in qns[i + 1:]:
            ta, tb = stems.get(qa) or "", stems.get(qb) or ""
            if not ta or not tb or min(len(ta), len(tb)) < 80:
                continue
            sim = difflib.SequenceMatcher(None, ta[:400], tb[:400]).ratio()
            if sim < 0.95:
                continue
            ca = _stem_payload_coherence(ta, chapter_records[qa])
            cb = _stem_payload_coherence(tb, chapter_records[qb])
            if abs(ca - cb) >= STEM_COHERENCE_MARGIN:
                loser, winner = (qa, qb) if ca < cb else (qb, qa)
                chapter_records[loser]["question_text"] = None
                stems[loser] = ""
                stats["dup_stems_stripped"] = stats.get("dup_stems_stripped", 0) + 1
                iflag("duplicate_stem_stripped", loser,
                      f"stem duplicated q{winner} (sim {sim:.2f}) but coherence "
                      f"{min(ca, cb):.2f} vs winner {max(ca, cb):.2f} -- stripped, retry refills",
                      winner=winner, similarity=round(sim, 3))
                print(f"  [SWEEP] q{loser}: stem duplicated q{winner} with worse "
                      f"payload coherence -- stripped for same-run retry")
            else:
                iflag("duplicate_stem_review", qb,
                      f"stem near-duplicates q{qa} (sim {sim:.2f}); coherence tie "
                      f"({ca:.2f} vs {cb:.2f}) -- needs review", matched=False)
                print(f"  [WARN] [SWEEP] q{qa}~q{qb}: near-duplicate stems, coherence "
                      f"undecidable -- logged for review, no data touched")

    # 2. foreign 'Option X:' line glued at a solution's head (009-007 class):
    #    strip it ONLY when the same line already exists verbatim on another
    #    record of this chapter (proves it is a stray duplicate, not content
    #    this question alone owns). Otherwise flag, keep text, retry nothing.
    for qn in qns:
        sol = (chapter_records[qn].get("solution_text") or "").strip()
        if not _foreign_option_line(sol, chapter_records[qn]):
            continue
        head_line = sol.splitlines()[0].strip()
        dup_elsewhere = any(other != qn and head_line
                            and head_line in (chapter_records[other].get("solution_text") or "")
                            for other in qns)
        if dup_elsewhere:
            chapter_records[qn]["solution_text"] = sol[len(sol.splitlines()[0]):].lstrip("\n ")
            stats["foreign_heads_stripped"] = stats.get("foreign_heads_stripped", 0) + 1
            iflag("foreign_option_head_stripped", qn,
                  f"solution began with a foreign 'Option' line that exists verbatim "
                  f"on another record -- stripped: {head_line[:120]!r}")
            print(f"  [SWEEP] q{qn}: stripped foreign 'Option' head (verbatim dup elsewhere)")
        else:
            iflag("foreign_option_head_review", qn,
                  f"solution begins with an 'Option' line its own options cannot own "
                  f"-- kept (unique), needs review: {head_line[:120]!r}", matched=False)
            print(f"  [WARN] [SWEEP] q{qn}: foreign 'Option' head but no verbatim donor "
                  f"-- kept, logged for review")

    # 2b. foreign "Solution to Question N:" dump TAIL (external-audit class,
    #     2026-07-27: on a dense solutions page the model sometimes returns
    #     the FIRST record's item with its own correct solution PLUS the
    #     verbatim solutions of every later question on that page concatenated
    #     after 'Solution to Question 2:' headers; e.g. ch11 q1 carried
    #     q1+q2+...+q8 in one 5689-char blob while q2..q8 ALSO owned their
    #     own correct copies). sanitize_solution_text only trims such a tail
    #     when it duplicates THIS record's own text; a tail holding the
    #     neighbour's UNIQUE solution is kept there by caution. Here the
    #     cross-record proof exists: if the header names a record of THIS
    #     chapter that already owns a non-empty solution, the tail is provably
    #     redundant -> trim at the FIRST such header. Donor-less headers are
    #     left intact (never delete possibly-unique content) and flagged.
    for qn in qns:
        sol = chapter_records[qn].get("solution_text") or ""
        if not sol:
            continue
        for m in SOLUTION_DUMP_HDR_RE.finditer(sol):
            if m.start() <= 2:
                continue  # leading header: sanitize_solution_text strips it at build
            n = int(m.group(1))
            if n == qn:
                continue
            donor_sol = chapter_records.get(n, {}).get("solution_text") or ""
            if donor_sol.strip():
                trimmed = sol[:m.start()].rstrip()
                chapter_records[qn]["solution_text"] = trimmed
                stats["solution_dumps_trimmed"] = stats.get("solution_dumps_trimmed", 0) + 1
                iflag("foreign_solution_dump_trimmed", qn,
                      f"embedded 'Solution to Question {n}:' header at char {m.start()} -- "
                      f"tail trimmed ({len(sol) - len(trimmed)} chars); donor q{n} already owns "
                      f"its solution ({len(donor_sol)} chars) -- redundancy proven")
                print(f"  [SWEEP] q{qn}: trimmed foreign 'Solution to Question {n}:' dump tail "
                      f"({len(trimmed)} chars kept; donor q{n} owns its own solution)")
                if looks_truncated_solution(trimmed, has_tables=bool(chapter_records[qn].get("tables"))):
                    forced_solution.add(qn)
                break
            iflag("foreign_solution_dump_review", qn,
                  f"embedded 'Solution to Question {n}:' header but donor q{n} owns NO "
                  f"solution -- tail kept (may be unique content), needs review",
                  matched=False)
            break

    # 3. truncated-solution suspects (023-007/006-009 class): deterministic
    #    dangling-end / mid-flow-cut patterns -- re-ask the FULL solution.
    for qn in qns:
        rec = chapter_records[qn]
        sol = (rec.get("solution_text") or "")
        if not sol.strip():
            continue
        entry = image_files_by_q.get(qn, {"question": [], "solution": []})
        sol_imgs = entry.get("solution") or []
        if looks_truncated_solution(sol, has_tables=bool(rec.get("tables")),
                                    has_images=bool(sol_imgs)):
            forced_solution.add(qn)
            iflag("truncated_solution_retry", qn,
                  f"solution looks truncated (...{sol.rstrip()[-50:]!r}) -- forced re-ask")
            print(f"  [SWEEP] q{qn}: solution looks truncated -- targeted retry will re-ask it")
        elif sol_imgs and DANGLING_END_RE.search(sol.rstrip()):
            # AUDITABLE SUPPRESSION (2026-08-11): the dangling lead-in is
            # explained by an attributed solution figure, so no re-ask is
            # spent. Logged + flagged (not silent) so a genuine truncation
            # hiding behind a figure is still reviewable in the export.
            iflag("truncated_solution_suppressed_by_image", qn,
                  f"solution ends {sol.rstrip()[-40:]!r} but {len(sol_imgs)} solution "
                  f"image(s) attached -- lead-in explained by figure, retry skipped")
            print(f"  [SWEEP] q{qn}: dangling end but {len(sol_imgs)} solution image(s) "
                  f"attached -- not treating as truncated (retry saved)")

    # 4. over-attributed question images (022-003 class, rows healed by the
    #    recovery path where the rename-time cap never ran).
    for qn in qns:
        entry = image_files_by_q.get(qn)
        if not entry or len(entry.get("question") or []) <= MAX_QUESTION_IMAGES:
            continue
        extras = entry["question"][MAX_QUESTION_IMAGES:]
        del entry["question"][MAX_QUESTION_IMAGES:]
        _append_jsonl(DATA_DIR / "unmatched_images.jsonl",
                      {"subject": subject, "chapter_id": f"{subject}-{chapter_no:03d}",
                       "page": None, "files": extras,
                       "reason": f"over-attribution sweep (> {MAX_QUESTION_IMAGES} question "
                                 f"images on one question) -- de-referenced for review"})
        iflag("question_images_trimmed", qn,
              f"had {len(extras) + MAX_QUESTION_IMAGES} question images; kept first "
              f"{MAX_QUESTION_IMAGES}, de-referenced {len(extras)}")
        print(f"  [SWEEP] q{qn}: de-referenced {len(extras)} over-attributed question "
              f"image(s) -- logged to unmatched_images.jsonl")

    # 4b. over-attributed SOLUTION images (same class on the solution side:
    #     user report -- 7 figures on one solutions page collapsed into 2
    #     solutions; the sweep also heals rows from runs before the cap).
    for qn in qns:
        entry = image_files_by_q.get(qn)
        if not entry or len(entry.get("solution") or []) <= MAX_SOLUTION_IMAGES:
            continue
        extras = entry["solution"][MAX_SOLUTION_IMAGES:]
        del entry["solution"][MAX_SOLUTION_IMAGES:]
        _append_jsonl(DATA_DIR / "unmatched_images.jsonl",
                      {"subject": subject, "chapter_id": f"{subject}-{chapter_no:03d}",
                       "page": None, "files": extras,
                       "reason": f"over-attribution sweep (> {MAX_SOLUTION_IMAGES} solution "
                                 f"images on one question) -- de-referenced for review"})
        iflag("solution_images_trimmed", qn,
              f"had {len(extras) + MAX_SOLUTION_IMAGES} solution images; kept first "
              f"{MAX_SOLUTION_IMAGES}, de-referenced {len(extras)}")
        print(f"  [SWEEP] q{qn}: de-referenced {len(extras)} over-attributed solution "
              f"image(s) -- logged to unmatched_images.jsonl")

    # 5. contaminated stems (run-7 cross-field contamination class): a
    #    question_text that OPENS with explanation language or is
    #    substantially contained in its own solution is solution prose, not a
    #    stem (the audit's pattern: "question_text contains a paragraph from
    #    that question's or a neighbor's solution"). Strip it so the targeted
    #    retry REFILLS the stem from the pages (Gap-1 anchor: the solution
    #    names its question) instead of shipping a populated-but-wrong field.
    #    "Field is populated" is NOT treated as "field is valid".
    for qn in qns:
        rec = chapter_records[qn]
        qt = (rec.get("question_text") or "").strip()
        if not qt:
            continue
        reason = _stem_reject_reason(qt, rec)
        if not reason:
            continue
        # RUN-13 STEM QUARANTINE (replaces strip-to-None): the sweep's
        # containment heuristic fired on REAL stems that the solution
        # restates (ch26 q1, ch7 q24/q26 in this run), the retry could not
        # refill them ("blocked contaminated stem ... next round ... still
        # missing"), and the record shipped with NO stem -- permanent data
        # loss (missing_stem gate flag). Quarantine instead: keep the text
        # (flagged suspect), let the retry REPLACE it with a passing
        # candidate, and if nothing passes the record ships the suspect with
        # a suspect_stem gate flag -- preserved for review, never silently
        # deleted, never silently accepted.
        rec["_stem_suspect_reason"] = reason
        stats["contaminated_stems_stripped"] = stats.get("contaminated_stems_stripped", 0) + 1
        iflag("contaminated_stem_suspect", qn,
              f"question_text MAY BE solution prose ({reason}; prov="
              f"{rec.get('_prov', {}).get('question_text')}) -- quarantined "
              f"(kept for review), retry may replace with a passing candidate")
        print(f"  [SWEEP] q{qn}: quarantined suspect stem ({reason}) -- kept "
              f"for review, retry may replace it")

    if flags:
        stats["integrity_flags"] = stats.get("integrity_flags", 0) + len(flags)
    return forced_solution


def find_incomplete_records(chapter_records, force_solution_qns=(), printed_solution_qns=()):
    """
    Returns [(q_no, missing_fields), ...] for records worth retrying.

    force_solution_qns: q_nos whose non-empty solutions the integrity sweep
    judged truncated -- re-asked like a missing solution regardless of the
    60% gate (the book provably printed SOMETHING here; we hold a fragment).

    printed_solution_qns: q_nos whose "Solution to Question N:" header was
    found in the chapter's text layer (chapter_printed_solution_qns). A
    header PROVES the book prints an explanation for that q_no, so a missing
    solution for it is an extraction loss -- retry-eligible even when the
    chapter as a whole sits below the 60% gate (ch25 class: 7/12 = 58%,
    gate suppressed 5 real solutions).

    "answer" and "options" gaps are always retry-worthy: every real MCQ has
    4 options and one marked answer somewhere in the book.

    "solution" gaps are retry-worthy only when chapter-internal evidence
    says the book PRINTS explanations here (>=60% of questions already have
    solution text -- SOLUTION_GATE_MIN_SHARE, or a printed header for that
    specific q_no). Chapters where the book genuinely prints no explanations
    (answer-key-only sections, RC-4) show ~0% coverage and stay protected:
    no quota is wasted chasing content that was never printed.
    """
    incomplete = []
    for qn, rec in chapter_records.items():
        # SEMANTIC COMPLETENESS (run-7 hardening #6): a non-empty
        # question_text that is really solution prose is NOT a valid stem --
        # treat it as missing so the retry replaces it instead of the record
        # shipping a populated-but-wrong field. The sweep strips these before
        # retry; this check is the net for records the sweep never saw.
        qt = (rec.get("question_text") or "").strip()
        if qt and _stem_reject_reason(qt, rec):
            missing = ["question"]
            if not rec.get("correct_option"):
                missing.append("answer")
            options = rec.get("options") or {}
            if len(options) < 4 or any(not str(v or "").strip() for v in options.values()):
                missing.append("options")
            if (rec.get("solution_text") or "").strip() or rec.get("correct_option") or options:
                incomplete.append((qn, missing))
            continue
        if not qt:
            # Stem-less records USED to be skipped here ("nothing to anchor a
            # retry to") -- wrong: a present solution_text/correct_option IS
            # the anchor. The stem and its own solution never share lexical
            # overlap, but the solution names the question it explains, and
            # Gemini can walk back from it (Gap-1: PSY-001-003).
            if (rec.get("solution_text") or "").strip() or rec.get("correct_option"):
                incomplete.append((qn, ["question"]))
            continue  # truly anchorless scraps stay ineligible
        missing = []
        if not rec.get("correct_option"):
            missing.append("answer")
        options = rec.get("options") or {}
        if len(options) < 4 or any(not str(v or "").strip() for v in options.values()):
            missing.append("options")
        if missing:
            incomplete.append((qn, missing))

    n = len(chapter_records)
    n_with_sol = sum(1 for r in chapter_records.values() if (r.get("solution_text") or "").strip())
    book_prints_solutions = n > 0 and n_with_sol / n >= SOLUTION_GATE_MIN_SHARE
    printed = set(printed_solution_qns or ())
    forced = set(force_solution_qns or ())
    if book_prints_solutions or forced or printed:
        by_qn = {qn: missing for qn, missing in incomplete}
        for qn, rec in chapter_records.items():
            truncated = qn in forced and (rec.get("solution_text") or "").strip()
            printed_here = qn in printed
            if rec.get("question_text") and (
                    (not (rec.get("solution_text") or "").strip()
                     and (book_prints_solutions or printed_here))
                    or truncated):
                if qn in by_qn:
                    if "solution" not in by_qn[qn]:
                        by_qn[qn].append("solution")
                else:
                    incomplete.append((qn, ["solution"]))
    return incomplete


def build_targeted_retry_prompt(incomplete_items, chapter_records,
                                stem_only_qns=None):
    """Focused retry schema.  Tables must never be returned inside prose.
    stem_only_qns (run-12, hardened 2026-08-08):
      q_nos whose stem was contamination-blocked in a previous round
      (their record carries `_stem_suspect_reason`) -- for these, ask
      for the STEM REGION ONLY (the text between the printed question
      number and the first option label), never the options/solution,
      and CRITICALLY do NOT echo the (possibly contaminated) existing
      text. The legacy prompt echoed the existing stem in a "stem
      begins: '...'" prefix, which BIASED the rescue model toward
      re-paraphrasing the same solution prose -- the very thing the
      contamination heuristic had just rejected. Suppressing the echo
      in stem-only mode forces the model to look at the printed page
      and return the actual stem region (or null when absent).
      Other missing classes (answer, options, solution) keep the
      existing text echo so the model has context for the missing
      piece."""
    stem_only = set(stem_only_qns or ())
    lines = [
        "You already extracted most of this chapter from these SAME pages. Find ONLY the requested missing pieces.",
        "Return ONLY a valid JSON array, beginning with [ and ending with ]. No prose or markdown fences.",
        "Each element uses exactly this schema:",
        '{"q_no": <int>, "question_text": "..."|null, "correct_option": "A"|"B"|"C"|"D"|null, "options": {"A":"...","B":"...","C":"...","D":"..."}|null, "solution_text":"plain prose only"|null, "tables":[{"type":"short label","markdown":"| col | col |\\n|---|---|\\n..."}]}',
        "Every table MUST be in tables[] as markdown. NEVER put pipes, headers, or table rows in solution_text.",
        "Only fill requested fields; use null when not visible. Return [] if none are visible.",
        "MISSING PIECES TO FIND:",
    ]
    for qn, missing in incomplete_items:
        rec = chapter_records[qn]
        # STEM-ONLY MODE: do NOT echo the existing (contaminated) text.
        # The legacy behavior echoed "stem begins: '...'" which biased the
        # model toward re-paraphrasing the same solution prose. For
        # _stem_suspect_reason records the existing text is provably
        # contaminated, so echo is the worst possible input. A real stem
        # on the page is the ONLY acceptable return value.
        in_stem_only = qn in stem_only
        if not in_stem_only:
            qtext = (rec.get("question_text") or "")[:120]
            lines.append(f"Question {qn} (stem begins: {qtext!r}):")
        else:
            # Use a clear marker so the model can see the per-record
            # mode and the model-side context (no echo) is unambiguous.
            lines.append(f"Question {qn} (STEM-ONLY ASK -- do NOT echo the "
                         f"prior suspect text; return ONLY the verbatim text "
                         f"printed between the question number and option A):")
        if "question" in missing:
            if in_stem_only:
                # RUN-12: stem-region-only ask. The earlier broad ask kept
                # returning the solution text; a tight region instruction
                # cannot be satisfied by explanation prose. Hardened 2026-08-08:
                # the prompt also explicitly forbids the model from
                # paraphrasing the surrounding solution, and instructs null
                # when the region is empty (no hallucinated stems).
                lines.append(
                    f"- Return ONLY q{qn}'s QUESTION STEM: the exact sentence(s) "
                    f"printed directly under the question number and ABOVE the "
                    f"option labels (A./B./C./D.). The stem is the question the "
                    f"options answer. Do NOT include any option text, answer "
                    f"letter, explanation, or 'Solution to Question' text. "
                    f"Do NOT paraphrase the surrounding solution prose. If "
                    f"the page shows no stem region for q{qn}, return null.")
            else:
                lines.append(f"- Return full verbatim question stem and all four options A-D for q{qn}.")
        if "answer" in missing:
            lines.append(f"- Return the correct option letter for q{qn} from the printed answer key.")
        if "options" in missing:
            lines.append(f"- Return all four options A-D for q{qn}; captured letters: {sorted((rec.get('options') or {}).keys())}.")
        if "solution" in missing:
            original = (rec.get("solution_text") or "")[:500]
            lines.append(
                f"- Return only the missing/continuing part of q{qn}'s verbatim solution; put any table only in tables[]. "
                f"Preserve the source's line breaks and bullet-list structure. Existing text (do not repeat): {original!r}")
    return "\n".join(lines)


def _log_blocked_retry_fragment(chapter_id, qn, reason, fragment):
    """Ledger entry for a targeted-retry response that was provably another
    question's solution (wrong-owner guard). The fragment is never merged
    into the record; it stays visible here for review instead of silently
    blending two solutions (PSY-016/017-017 class)."""
    _append_jsonl(DATA_DIR / "integrity_flags.jsonl",
                  {"kind": "retry_foreign_fragment_blocked", "q_no": qn,
                   "chapter_id": chapter_id, "detail": reason,
                   "fragment": (fragment or "")[:600]})


def targeted_retry(model, page_files, chapter_records, state, max_rounds=2,
                   force_solution_qns=None, chapter_id=None, printed_solution_qns=None,
                   stats=None):
    """
    Up to `max_rounds` small, focused re-asks for whatever answer/option
    fields are still missing after normal processing. Sends the chapter's
    full page set again each round (simple and robust -- we don't track
    per-field page provenance) but with a MUCH smaller ask, which is what
    actually improves accuracy, not the page count. Stops early if a round
    makes no progress (no point burning quota repeating the same miss).
    force_solution_qns: integrity-sweep verdicts -- those records' non-empty
    solutions are REPLACED by a longer verbatim re-ask (truncated heal).
    printed_solution_qns: q_nos whose printed 'Solution to Question N:'
    header exists in the chapter -- bypasses the 60% solution-gate for them.
    stats: optional counter dict (chapter stats; used for the contaminated-
    stem block counter). Defaults to a throwaway dict when not passed.
    Returns the total number of fields filled.
    """
    if stats is None:
        stats = {}
    total_fixed = 0
    first_check = True
    forced = set(force_solution_qns or ())
    stem_blocked = set()   # run-12: q_nos whose stem retry was contamination-blocked
    for round_no in range(1, max_rounds + 1):
        incomplete = find_incomplete_records(chapter_records, force_solution_qns=forced,
                                             printed_solution_qns=printed_solution_qns)
        if not incomplete:
            if first_check:
                # never exit silently again (run-2 learning: "retry skipped"
                # was really "nothing eligible") -- say WHY.
                n_sol_gaps = sum(1 for r in chapter_records.values()
                                 if r.get("question_text") and not (r.get("solution_text") or "").strip())
                if n_sol_gaps:
                    print(f"  [RETRY] nothing eligible: {n_sol_gaps} solution gap(s) suppressed by the "
                          f"60% source-evidence gate (treated as book-printed answer-key-only)")
            break
        first_check = False

        reset_daily_counter_if_needed(state)
        if quota_exhausted(state):
            print("  [RETRY] daily call limit reached -- stopping retries for now")
            break

        # Include the actual eligibility reason; a non-empty solution alone
        # does not say whether q13 is missing an answer, option, stem, or was
        # explicitly marked as a truncation suspect.
        preview = ", ".join(f"q{qn}[{','.join(missing)}]" for qn, missing in incomplete[:10])
        if len(incomplete) > 10:
            preview += ", ..."
        print(f"  [RETRY] round {round_no}: {len(incomplete)} question(s) still "
              f"incomplete ({preview}) -- sending targeted re-ask")

        prompt = build_targeted_retry_prompt(incomplete, chapter_records,
                                          stem_only_qns=stem_blocked)
        # Resilient execution (run-4 lesson, ch9/ch16): never ONE heavy
        # whole-chapter call that fails as a unit -- back off on transient
        # 5xx, split halves->singles on any failure. A recitation-prone
        # single page now only costs that page, not the whole chapter's
        # retry.
        fix_arrays = gemini_json_call_splitting(
            model, prompt, page_files, state,
            label=f" (targeted retry round {round_no})", direct_page_fallback=True)
        if not fix_arrays:
            print("  [RETRY] every sub-call failed even after splitting -- skipping this round")
            continue
        fixes = []
        for arr in fix_arrays:
            if isinstance(arr, list):
                fixes.extend(arr)
        # A targeted response is a PATCH, never permission to replace fields
        # that were already complete. Ignore any extra model fields.
        requested_by_qn = {qn: set(missing) for qn, missing in incomplete}

        fixed_this_round = 0
        for fix in fixes:
            try:
                qn = int(fix.get("q_no"))
            except (TypeError, ValueError):
                continue
            rec = chapter_records.get(qn)
            requested = requested_by_qn.get(qn, set())
            if rec is None or not requested:
                continue
            # PATCH-ONLY + PROVENANCE (run-7 hardening #2/#4): a retry
            # response may only touch the fields that were requested, and
            # every patched field records its provenance (Q_RETRY / A_RETRY /
            # S_RETRY). A Q-retry's returned stem is additionally checked
            # for solution-prose contamination before it is accepted.
            req_prov = ("Q_RETRY" if "question" in requested
                        else ("A_RETRY" if "answer" in requested else "S_RETRY"))
            if "question" in requested and fix.get("question_text") and not (rec.get("question_text") or "").strip():
                incoming_q = str(fix["question_text"]).strip()
                stem_reason = _stem_reject_reason(incoming_q, rec)
                if stem_reason:
                    stats.setdefault("contaminated_stems_blocked", 0)
                    stats["contaminated_stems_blocked"] += 1
                    stem_blocked.add(qn)   # switch to stem-region-only ask next round
                    print(f"  [RETRY] blocked contaminated stem for q{qn} "
                          f"({stem_reason}) -- kept for review, still stem-missing; "
                          f"next round will ask for the stem region ONLY")
                    _log_blocked_retry_fragment(chapter_id, qn, f"contaminated stem: {stem_reason}",
                                                incoming_q)
                else:
                    rec["question_text"] = incoming_q
                    rec["_prov"]["question_text"] = req_prov
                    fixed_this_round += 1
            if "answer" in requested and fix.get("correct_option") and not rec.get("correct_option"):
                rec["correct_option"] = str(fix["correct_option"]).strip().upper()
                rec["_prov"]["correct_option"] = req_prov
                fixed_this_round += 1
            sol_existing = (rec.get("solution_text") or "").strip()
            incoming_text, incoming_tables = _normalize_solution_payload(
                str(fix.get("solution_text") or ""), fix.get("tables") or [], qn)
            if "solution" in requested and incoming_text:
                if not sol_existing:
                    # A foreign fragment must not FILL an empty solution either
                    # (same audit class as the append below: the re-ask for
                    # q16 can come back carrying q17's block).
                    foreign = _solution_fragment_foreign(incoming_text, qn, rec, chapter_records)
                    if foreign:
                        _log_blocked_retry_fragment(chapter_id, qn, foreign, incoming_text)
                        print(f"  [RETRY] blocked foreign solution fragment for q{qn} "
                              f"(empty solution): {foreign}")
                    else:
                        rec["solution_text"] = incoming_text
                        rec["_prov"]["solution_text"] = req_prov
                        fixed_this_round += 1
                elif qn in forced:
                    # Targeted prompt asks for the missing continuation, so
                    # preserve the established prose/bullets instead of
                    # replacing it with a regenerated full solution.
                    if incoming_text.startswith(sol_existing) and len(incoming_text) > len(sol_existing):
                        rec["solution_text"] = incoming_text
                        rec["_prov"]["solution_text"] = req_prov
                        fixed_this_round += 1
                    elif not _frag_mostly_present(incoming_text, sol_existing, 0.9):
                        # Wrong-owner guard (external-audit 2026-08-02:
                        # q16's truncated re-ask returned q17's solution and
                        # the old code APPENDED it -- 'not mostly present'
                        # was misread as new continuation). Only append when
                        # the fragment passes the deterministic foreign
                        # proofs; a genuine continuation is kept.
                        foreign = _solution_fragment_foreign(incoming_text, qn, rec, chapter_records)
                        if foreign:
                            _log_blocked_retry_fragment(chapter_id, qn, foreign, incoming_text)
                            print(f"  [RETRY] blocked foreign solution fragment for q{qn}: {foreign} "
                                  f"(existing solution untouched; fragment logged)")
                        else:
                            rec["solution_text"] = sol_existing.rstrip() + "\n" + incoming_text
                            rec["_prov"]["solution_text"] = req_prov
                            fixed_this_round += 1
            if "solution" in requested and incoming_tables:
                before_tables = rec.get("tables") or []
                merged_tables = _dedupe_tables(list(before_tables) + incoming_tables)
                if merged_tables != before_tables:
                    rec["tables"] = merged_tables
                    # Only count a table patch if it adds information, not if
                    # normalization merely changes ordering of an identical set.
                    if len(merged_tables) > len(before_tables):
                        fixed_this_round += 1
            if "options" in requested and fix.get("options"):
                rec["options"] = rec.get("options") or {}
                before = len(rec["options"])
                for k, v in fix["options"].items():
                    if v:  # don't let a null/empty value overwrite nothing-useful
                        rec["options"].setdefault(str(k).strip().upper(), v)
                if len(rec["options"]) > before:
                    fixed_this_round += 1

        print(f"  [RETRY] round {round_no}: filled {fixed_this_round} field(s)")
        total_fixed += fixed_this_round
        if fixed_this_round == 0:
            print("  [RETRY] no progress this round -- stopping (remaining gaps "
                  "will be logged, not re-tried, to avoid wasting quota)")
            break

    # whatever's STILL missing after all rounds -- log it, don't hide it.
    # Forced (truncated) items are re-judged LIVE here: a healed solution
    # must not stay logged as missing just because the sweep's verdict came
    # before this round's fix landed.
    live_forced = {qn for qn in forced
                   if qn in chapter_records
                   and looks_truncated_solution(
                       (chapter_records[qn].get("solution_text") or ""),
                       has_tables=bool(chapter_records[qn].get("tables")))}
    still_incomplete = find_incomplete_records(chapter_records, force_solution_qns=live_forced,
                                               printed_solution_qns=printed_solution_qns)
    # ALWAYS rewrite this chapter's ledger entries to the outcome of THIS run
    # (possibly zero -- full heal must also clear stale rows), never blindly append.
    _prune_still_incomplete(chapter_id)
    if still_incomplete:
        path = DATA_DIR / "still_incomplete_after_retry.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)  # same guard as save_state/_append_jsonl (fresh volume)
        with open(path, "a", encoding="utf-8") as f:
            for qn, missing in still_incomplete:
                f.write(json.dumps({"q_no": qn, "missing": missing, "chapter_id": chapter_id},
                                   ensure_ascii=False) + "\n")
        print(f"  [RETRY] {len(still_incomplete)} question(s) still incomplete after "
              f"{max_rounds} round(s) -- logged to still_incomplete_after_retry.jsonl")

    return total_fixed

def _prune_still_incomplete(chapter_id):
    """Drop this chapter's OLD entries from still_incomplete_after_retry.jsonl.
    The ledger was append-only: rows healed later (next retry round, recovery,
    or the healer) left stale 'missing solution' entries behind (confirmed in
    the 2026-07-27 external audit: 12 entries whose questions.jsonl rows were
    actually complete). Rewriting per chapter keeps the ledger truthful."""
    path = DATA_DIR / "still_incomplete_after_retry.jsonl"
    if not chapter_id or not path.exists():
        return
    kept = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            if json.loads(ln).get("chapter_id") == chapter_id:
                continue
        except json.JSONDecodeError:
            pass
        kept.append(ln)
    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")


RESCUE_MAX_CALLS_PER_CHAPTER = 12   # page-focused last-ditch re-ask budget


def rescue_incomplete_records(model, page_files, pdf_path, chapter_records, state,
                              stats, chapter_id, printed_solution_qns=None,
                              max_calls=RESCUE_MAX_CALLS_PER_CHAPTER):
    """Chapter-end rescue pass -- the page-focused LAST DITCH for records the
    targeted retry could not fill (run-5 audit: 9 records still missing
    answer/options after 2 whole-chapter retry rounds, e.g. ch2 q25/26,
    ch18 q13, ch19 q11/12, ch24 q12/13, ch27 q11, ch33 q9).

    Why this is different from targeted_retry: the retry re-sends the ENTIRE
    chapter every round -- a big, diffuse ask that stalls ("filled 0
    field(s)"). The rescue locates the EXACT pages where each missing q_no
    is printed (question stem and/or 'Solution to Question N:' header, via
    the zero-token text layer) and re-asks ONE focused call PER PAGE with
    just that page's image -- the same small-focused-ask principle that made
    per-page retries consistently recover where batch asks failed.

    Merges fill-only (never overwrites existing content); respects the daily
    quota; rewrites this chapter's still_incomplete_after_retry.jsonl to the
    post-rescue truth. Returns the number of fields filled."""
    incomplete = find_incomplete_records(chapter_records, force_solution_qns=(),
                                         printed_solution_qns=printed_solution_qns)
    if not incomplete:
        return 0
    qn_missing = {qn: set(missing) for qn, missing in incomplete}
    located = locate_missing_record_pages(pdf_path, page_files, qn_missing, chapter_records)
    unlocated = [qn for qn in qn_missing if qn not in located]
    for qn in unlocated:
        print(f"  [RESCUE] q{qn}: no printed question/solution page locatable in the "
              f"text layer -- left for --auto-recover / manual review")
        _append_jsonl(DATA_DIR / "integrity_flags.jsonl",
                      {"kind": "rescue_no_page_located", "q_no": qn,
                       "chapter_id": chapter_id,
                       "missing": sorted(qn_missing[qn])})

    def _count_fields(rec, missing):
        n = 0
        if "question" in missing and (rec.get("question_text") or "").strip():
            n += 1
        if "answer" in missing and rec.get("correct_option"):
            n += 1
        if "options" in missing and len(rec.get("options") or {}) >= 4:
            n += 1
        if "solution" in missing and (rec.get("solution_text") or "").strip():
            n += 1
        return n

    filled, calls = 0, 0
    # Build a per-page map: page_no -> {"qns": set, "purpose": str}
    # The "purpose" is the gap type this page is being used for on this
    # q_no ("question"/"options"/"answer"/"solution"). Multiple q_nos may
    # share a page; we run ONE call per page, asking for ALL needed
    # fields of ALL the q_nos on that page (the build_targeted_retry_prompt
    # already handles per-q_no gap lists).
    #
    # 2026-08-08 routing fix: each q_no's needed pages are taken ONLY
    # from the category that matches the gap (question-side for stems/
    # options, answer-side for answers, solution-side for solutions).
    # The old code took the union of all categories and sent stem rescues
    # to solution-side pages where the model found no stem region.
    pages_of = {}
    for qn, cats in located.items():
        need = qn_missing.get(qn, set())
        # Per-category page lists for this q_no (empty if category
        # wasn't located in the text layer).
        q_pages = cats.get("question") or []   # q-side pages
        a_pages = cats.get("answer") or []      # key-table pages
        s_pages = cats.get("solution") or []   # solution-side pages
        # Build the set of pages to use FOR THIS Q_NO based on its gaps.
        # (We don't store the purpose per (qn, page) -- the per-page
        # call asks for whichever fields are missing for all q_nos on
        # the page, and the prompt handles each q_no's gaps. The
        # IMPORTANT thing is that the page set is the UNION of only the
        # category-correct pages, not the union of all categories.)
        relevant_pages = set()
        if "question" in need or "options" in need:
            relevant_pages.update(q_pages)
        if "answer" in need:
            relevant_pages.update(a_pages)
        if "solution" in need:
            relevant_pages.update(s_pages)
        for p in relevant_pages:
            slot = pages_of.setdefault(p, {"qns": set(), "purposes": set()})
            slot["qns"].add(qn)
            if "question" in need or "options" in need:
                if p in q_pages:
                    slot["purposes"].add("question/options")
            if "answer" in need and p in a_pages:
                slot["purposes"].add("answer")
            if "solution" in need and p in s_pages:
                slot["purposes"].add("solution")
    for page_no in sorted(pages_of):
        if calls >= max_calls:
            print(f"  [RESCUE] call budget ({max_calls}) exhausted -- remaining gaps go "
                  f"to --auto-recover / manual review")
            break
        qns_here = pages_of[page_no]["qns"]
        purposes_here = pages_of[page_no]["purposes"]
        pf = next((p for p in page_files
                   if int(p.stem.split("-")[-1]) == page_no), None)
        if pf is None:
            continue
        reset_daily_counter_if_needed(state)
        if quota_exhausted(state):
            print("  [RESCUE] daily Gemini call limit reached -- saving, exiting")
            save_state(state)
            sys.exit(0)
        # FIELD-SPECIFIC PROMPT (run-11 RC-5): an answer-only gap gets an
        # answer-only ask (the broad rescue prompt returned text the scope/
        # contamination filters rejected -> '0 field(s) filled' every time).
        # We check the per-page PURPOSES (which gap-types are being
        # recovered for the q_nos on this page) to decide prompt shape.
        need = sorted({f for qn in qns_here for f in qn_missing[qn]})
        # 2026-08-08: if the page is ONLY being used for answer-recovery
        # (no question/options gap on it), AND only one q_no is on it,
        # use the focused answer_rescue_prompt. This avoids the
        # "scope/contamination filter rejected" failure when the broad
        # prompt returns other-field text for an answer-only gap.
        if (need == ["answer"] and len(qns_here) == 1
                and purposes_here == {"answer"}):
            qn0 = qns_here[0]
            prompt = answer_rescue_prompt(qn0, chapter_records[qn0], chapter_records)
        else:
            # CONTAMINATION STEM ROUTING (2026-08-08): for any q_no whose
            # record carries `_stem_suspect_reason`, the rescue pass must
            # use the STEM-ONLY ask template (suppresses the echo of the
            # existing contaminated text, forces verbatim stem-region
            # extraction). Without this routing, the rescue pass always
            # re-paraphrases the same solution prose the contamination
            # heuristic just rejected, and the chapter ships with a
            # suspect_stem violation forever. This is the partner fix
            # to the build_targeted_retry_prompt stem-only mode hardened
            # in 2026-08-08: the prompt template is right, but the rescue
            # caller never told it which q_nos need the stem-only mode.
            #
            # CRITICAL 2026-08-08: the stem-only ask is meaningful ONLY
            # when the page being sent is a question-side page (where
            # the printed "N." stem region actually exists). With the
            # per-page-purpose tracking above, we know whether THIS
            # page is a question-side page. If a page being used for
            # stem recovery is NOT a question-side page, we skip the
            # call entirely (no point sending a stem-only ask to a
            # solutions page -- the user observed this exact bug:
            # "yrr solutions to us page pr h hi nhi").
            if not purposes_here.intersection({"question/options"}):
                # This page is only used for answer/solution recovery,
                # not for stem recovery. Skip the stem-only path; the
                # regular build_targeted_retry_prompt below handles it.
                stem_only = set()
            else:
                stem_only = {qn for qn in qns_here
                             if chapter_records.get(qn, {}).get("_stem_suspect_reason")}
            if stem_only:
                print(f"  [RESCUE] page {page_no}: stem-only mode for "
                      f"q{sorted(stem_only)} (suspect stems; no prior text echo; "
                      f"purpose={sorted(purposes_here)})")
            prompt = build_targeted_retry_prompt(
                [(qn, sorted(qn_missing[qn])) for qn in qns_here],
                chapter_records,
                stem_only_qns=stem_only)
        before_n = sum(_count_fields(chapter_records[qn], qn_missing[qn]) for qn in qns_here)
        try:
            raw = call_gemini_on_pages(model, [pf], context=RECOVERY_CONTEXT, prompt=prompt)
            note_call(state)
            save_state(state)
            calls += 1
        except Exception as e:
            print(f"  [RESCUE] page {page_no} call failed ({e}) -- skipping page")
            continue
        items, _meta = extract_batch_meta(raw)
        for it in items:
            if isinstance(it, dict):
                it["_prov"] = "RESCUE"   # page-focused rescue provenance
        chapter_records, skipped = merge_question_records(chapter_records, items, stats,
                                                          fill_only=True)
        for it in skipped:
            _append_jsonl(DATA_DIR / "integrity_flags.jsonl",
                          {"kind": "rescue_unmatched_fragment", "chapter_id": chapter_id,
                           "page": page_no, "item": str(it)[:300]})
        after_n = sum(_count_fields(chapter_records[qn], qn_missing[qn]) for qn in qns_here)
        gained = after_n - before_n
        filled += gained
        print(f"  [RESCUE] page {page_no}: q{','.join(map(str, qns_here))} "
              f"-> {gained} field(s) filled")
    stats["rescue_calls"] = stats.get("rescue_calls", 0) + calls
    stats["rescue_filled"] = stats.get("rescue_filled", 0) + filled

    # truth the ledger post-rescue (targeted_retry wrote it before this pass)
    still = find_incomplete_records(chapter_records, force_solution_qns=(),
                                    printed_solution_qns=printed_solution_qns)
    _prune_still_incomplete(chapter_id)
    if still:
        path = DATA_DIR / "still_incomplete_after_retry.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for qn, missing in still:
                f.write(json.dumps({"q_no": qn, "missing": missing, "chapter_id": chapter_id},
                                   ensure_ascii=False) + "\n")
    return filled


def pdftotext_page(pdf_path, true_page):
    out = subprocess.run(["pdftotext", "-f", str(true_page), "-l", str(true_page),
                          "-layout", str(pdf_path), "-"], capture_output=True, text=True)
    return out.stdout or ""


def probe_batch_pages(pdf_path, window_pages):
    """V2 zero-token pass-activation probe. Reads the pdftotext layer of the
    batch's pages ONCE (one subprocess for the whole window) and decides which
    of the 3 passes are worth a Gemini call:
      key_table=True  -> pages print an answer-key table  -> run A-pass
      solutions=True  -> pages print 'Solution to Question N:' -> run S-pass
    Text layer is used ONLY for activation decisions, never as content. On any
    probe failure (scanned-only PDF, pdftotext missing) fall back to running
    ALL passes -- accuracy must never depend on the text layer existing."""
    try:
        lo, hi = min(window_pages), max(window_pages)
        out = subprocess.run(["pdftotext", "-f", str(lo), "-l", str(hi),
                              "-layout", str(pdf_path), "-"],
                             capture_output=True, text=True)
        text = out.stdout or ""
    except Exception:
        return {"key_table": True, "solutions": True, "probe_failed": True}
    if not text.strip():
        return {"key_table": True, "solutions": True, "probe_failed": True}
    # >=2 solution headers = a real solutions page, not a stray cross-reference
    return {"key_table": bool(KEY_TABLE_PROBE_RE.search(text)),
            "solutions": len(SOLUTION_PROBE_RE.findall(text)) >= 2,
            "probe_failed": False}


def build_section_windows(page_files, pdf_path):
    """Section-aware window planner (run-6 user ask).

    Reads the chapter's text layer ONCE (zero-token) to find where the
    Solutions section starts (first page with >=2 'Solution to Question N:'
    headers), then builds windows so:
      * the whole questions+answers stretch is sent in LARGE
        QUESTIONS_CHUNK_PAGES windows with 1-page overlap (a chapter's
        question section usually fits in 1-2 calls -- every question shares
        one context, so boundary splits and cross-window option drops
        disappear, and fewer calls = less 15-RPM pressure + less token
        waste: overlap drops from 2/6 (33%) to 1/10 (10%));
      * the Solutions section is sent in SMALLER SOLUTIONS_CHUNK_PAGES
        windows with 1-page overlap (long verbatim spans are what trigger
        finish_reason=4 recitation -- page 218 class).

    Pass ACTIVATION is NOT changed: each window still runs the probe-based
    Q/A/S decision (and the sticky extraction boundary), so a question page
    that the text layer mislabels can never be skipped -- section labels
    here only SIZE the windows and mark the carry hard-reset at the
    Solutions boundary. Returns a list of (page_numbers, section_label)
    tuples in reading order, or [] when the text layer cannot be read / no
    solutions section is detected -- the caller then falls back to the
    fixed 6-page window loop (unchanged safe path)."""
    pages = []
    for p in page_files:
        try:
            pages.append(int(p.stem.split("-")[-1]))
        except (ValueError, IndexError):
            pass
    if not pages:
        return []
    try:
        text_by_page = {p: (pdftotext_page(pdf_path, p) or "") for p in pages}
    except Exception:
        return []
    if not any(t.strip() for t in text_by_page.values()):
        return []
    solutions_start = None
    for p in sorted(pages):
        t = text_by_page[p]
        if t.strip() and len(SOLUTION_PROBE_RE.findall(t)) >= 2:
            solutions_start = p
            break
    if solutions_start is None:
        return []  # no solutions section detectable -> fixed-window fallback

    def chunks(pagenos, size, overlap):
        wins, i, step = [], 0, max(1, size - overlap)
        while i < len(pagenos):
            wins.append(pagenos[i:i + size])
            i += step
        return wins

    ordered = sorted(pages)
    windows = []
    q_pages = [p for p in ordered if p < solutions_start]
    if q_pages:
        for w in chunks(q_pages, QUESTIONS_CHUNK_PAGES, SECTION_OVERLAP_PAGES):
            windows.append((w, "Q"))
    s_pages = [p for p in ordered if p >= solutions_start]
    if s_pages:
        # RUN-12 CROSS-SECTION OVERLAP: include the LAST question-section
        # page (solutions_start - 1) as the first page of the FIRST S window.
        # A question that spans the boundary (stem on the question side,
        # options/solution tail on the first solution page) is then seen by
        # the Q-pass as an OVERLAP page of that S window -- without this, the
        # boundary split silently dropped the tail (a class of the run-12
        # missing-options / missing-stem records at every Q/S boundary).
        first_s = [p for p in s_pages[:SOLUTIONS_CHUNK_PAGES]]
        if q_pages and first_s and (solutions_start - 1) in q_pages \
                and (solutions_start - 1) not in first_s:
            first_s = [solutions_start - 1] + first_s[:SOLUTIONS_CHUNK_PAGES - 1]
        windows.append((first_s, "S"))
        covered = set(first_s) & set(s_pages)
        rest = [p for p in s_pages if p not in covered]
        for w in chunks(rest, SOLUTIONS_CHUNK_PAGES, SECTION_OVERLAP_PAGES):
            windows.append((w, "S"))
    return windows


def _page_word_lines(pdf_path, file_page):
    """[(y, [(x, text), ...])] for a page, top-first (larger y first), from
    pypdf's text visitor. (x, y) in PDF user space (origin bottom-left) --
    the SAME coordinate space as image_positions_on_page. Shared by
    question-header, solution-header and option-anchor detection (run-10);
    the body-page pdftotext CLI is GARBLED on this book, the visitor is not."""
    try:
        page = PdfReader(pdf_path).pages[file_page - 1]
    except Exception:
        return []
    words = []

    def _visitor(text, _cm, tm, _font_dict, _font_size):
        t = (text or "").strip()
        if t:
            # tm[4]=x, tm[5]=baseline y from the PDF bottom-left origin
            words.append((round(float(tm[5]), 1), round(float(tm[4]), 1), t))

    try:
        page.extract_text(visitor_text=_visitor)
    except Exception:
        return []
    if not words:
        return []
    lines = {}
    for y, x, t in words:
        lines.setdefault(y, []).append((x, t))
    return [(y, sorted(lines[y])) for y in sorted(lines, reverse=True)]


def question_headers_on_page(pdf_path, file_page, chapter_records):
    """Locate every printed question-stem heading ("1.", "1)", "Q1." etc.) on
    a page WITH its vertical position. Returns [(q_no, y_baseline)] in
    reading order (top of page first), same bottom-left coordinate space as
    image_positions_on_page.

    The run-9 fix for the page-4 class: the OLD question-side path
    (qns_printed_on_page) used the pdftotext CLI, whose body-page text is
    GARBLED on this book -- so it returned nothing and the figure fell to the
    unreliable 4th-pass Gemini "decorative" verdict. This uses pypdf's text
    visitor (the SAME tool the solution-header mapper uses successfully on
    page 33), so question-side figures get the same deterministic geometry
    treatment as solution-side ones."""
    headers, seen = [], set()
    for y, wl in _page_word_lines(pdf_path, file_page):
        line = " ".join(t for _, t in wl)
        # run-18: accept "Question N:" and "N -" too, not just "N." / "N)"
        # -- some books print colon or dash after the number, and the old
        # class silently matched ZERO headers on every page of those books.
        m = re.match(r"^\s*(?:Q(?:uestion)?\s*[.:]?\s*)?(\d{1,3})\s*[.:\-–)]", line)
        if not m:
            continue
        qn = int(m.group(1))
        if qn in chapter_records and qn not in seen \
                and not re.match(r"^\s*Solution\s+to\s+Question", line, re.I):
            seen.add(qn)
            headers.append((qn, y))
    return headers


def block_headers_on_page(pdf_path, file_page, chapter_records):
    """Every block-start heading on a page: [(kind, q_no, y_baseline)] in
    reading order (top first), kind in {"question", "solution"}. Question
    headings that sit BELOW the first solution header on the page are
    dropped: once the solutions section begins, a "1." line is a list item
    inside solution prose, not a question stem."""
    qs = [("question", qn, y)
          for qn, y in question_headers_on_page(pdf_path, file_page, chapter_records)]
    ss = [("solution", qn, y)
          for qn, y in solution_headers_on_page(pdf_path, file_page, chapter_records)]
    if ss:
        first_sol_y = min(y for _k, _q, y in ss)   # lowest solution header
        qs = [t for t in qs if t[2] > first_sol_y]
    return sorted(qs + ss, key=lambda t: -t[2])


# --- run-10 OPTION-LEVEL image ownership -----------------------------------
# An "A." / "B." label line that begins a line inside a QUESTION block is an
# option anchor. Anchors are computed ONLY inside question blocks (bounded by
# the question heading above and the next block heading below), so "Option A:"
# prose inside SOLUTION text, table cells or bullet lists can never become
# anchors (the user's hard requirement).
OPTION_LABEL_RE = re.compile(r"^\s*([A-D])\s*[.)]\s*(.*)$", re.IGNORECASE)
_OPTION_ROW_TOL = 3.0      # labels on the same visual line share a baseline
_OPTION_X_MARGIN = 20.0    # x-distance tie margin (ambiguous if too close)

# --- run-12 stem-contamination discriminator -------------------------------
# Medical solutions routinely RESTATE the question stem ("The correct answer
# is B. The patient presents with ... as described above"), so a short,
# QUESTION-SHAPED stem can legitimately share >=80% of its tokens with its own
# solution. Flagging that as contamination destroyed GOOD stems (ch1 q3/q4/q10,
# ch2 q25, ch7 q1/q23-26, ch11 q1/q17, ch16 q2/q10 in run-12) and sent the
# retry into a dead-end ("blocked contaminated stem ... still stem-missing"
# for every round). A genuinely contaminated stem is DECLARATIVE explanation
# prose or implausibly long; a real stem is question-shaped and short.
_QUESTION_SHAPED_RE = re.compile(
    r"\?\s*$|which\b|what\b|who\b|whom\b|how\b|why\b|identify\b|choose\b|"
    r"select\b|best\b|most likely\b|correct\b|diagnos|drug\b|treatment\b|"
    r"following\b|regarding\b|according\b|is the\b|are the\b|of the\b",
    re.IGNORECASE)
_MAX_REAL_STEM_LEN = 250   # a printed MCQ stem is never longer than this


def option_anchors_in_block(pdf_path, file_page, y_head, y_bottom):
    """[(letter, x, y)] option-label anchors ("A." etc.) inside ONE vertical
    band [y_bottom, y_head] -- a question block's extent. Top-first (larger y
    first). Empty when the page's text layer can't be read or the block has
    no option labels.

    Detection (all via pypdf's text visitor, no pdftotext):
      1. a LINE-START label ("A. text") -> first anchor of that line;
      2. if the line starts with a label, EMBEDDED word-start labels
         ("A. text B. text" in a horizontal / 2x2 option row) -> second,
         third, fourth anchors (each word carries its own x);
      3. standalone label WORDS ("A." / "B)") on lines without a line-start
         label.
    Anchors are only collected inside a question block, so "Option A:" prose
    in solutions, table cells or bullet lists can never become anchors."""
    anchors = []
    for y, wl in _page_word_lines(pdf_path, file_page):
        if not (y_bottom < y <= y_head):
            continue
        line = " ".join(t for _, t in wl)
        first = OPTION_LABEL_RE.match(line)
        if first:
            anchors.append((first.group(1).upper(), wl[0][0], y))
            if len(wl) >= 2:                       # horizontal option row
                for x, t in wl[1:]:
                    m = re.match(r"^([A-D])\s*[.)]", t)
                    if m:
                        anchors.append((m.group(1).upper(), x, y))
        else:
            for x, t in wl:                        # standalone label words
                if re.fullmatch(r"[A-D][.)]", t):
                    anchors.append((t[0].upper(), x, y))
    return anchors


def _assign_option(anchors, x_img, y_img):
    """Conservative deterministic option assignment for an image inside a
    QUESTION block. Returns an option letter, or None (keep the image as a
    question-level figure -- never guess, never drop).

    Layout detection: anchors are grouped into rows by baseline (same-line
    labels share y). For an image:
      * rows strictly ABOVE the image's bottom edge are candidates; the
        closest row above is the block the image sits in;
      * a single-anchor row (vertical layout, e.g. "A. text" then [IMG])
        -> that option;
      * a multi-anchor row (horizontal / 2x2: "A [IMG] B [IMG]") -> nearest
        anchor by x, but only when unambiguous (margin _OPTION_X_MARGIN);
        an image left of the whole row or equidistant (a figure shared by
        several options) stays question-level;
      * no row above -> image is a stem figure (between the heading and
        option A) -> question-level.
    This never runs on SOLUTION blocks -- option anchors only exist inside
    question blocks, so "Option A:" prose in a solution can never steal a
    figure."""
    if not anchors:
        return None
    rows = []
    for a in sorted(anchors, key=lambda a: -a[2]):   # y desc, top first
        if rows and abs(rows[-1][-1][2] - a[2]) <= _OPTION_ROW_TOL:
            rows[-1].append(a)
        else:
            rows.append([a])
    above = [r for r in rows if r[0][2] > y_img]
    if not above:
        return None                      # stem figure above all option labels
    row = min(above, key=lambda r: r[0][2] - y_img)   # closest row above
    if len(row) == 1:
        return row[0][0]                 # vertical layout -> that option
    # horizontal / 2x2 row: nearest anchor by x, unambiguous only
    xs = sorted((a[1], a[0]) for a in row)
    if x_img < xs[0][0] - _OPTION_X_MARGIN:
        return None                      # image left of the whole row -> ambiguous
    dists = sorted((abs(a_x - x_img), letter) for a_x, letter in xs)
    if len(dists) >= 2 and abs(dists[0][0] - dists[1][0]) < _OPTION_X_MARGIN:
        return None                      # equidistant (shared figure) -> ambiguous
    return dists[0][1]


def _option_for_image_in_block(pdf_path, file_page, headers, owner, x_img, y_img):
    """Option-letter for an image already owned by a QUESTION block, or None.
    Runs ONLY for question-kind owners; finds the owner's block extent from
    the page's block headers (heading above, next heading below), collects
    the option-label anchors inside that block, and applies the conservative
    geometry rule. Solution blocks and cross-page carried blocks (no header
    on this page) return None -> the image stays a question-level figure."""
    kind, qn = owner
    if kind != "question":
        return None
    idx = next((i for i, (k, q, _y) in enumerate(headers)
                if k == kind and q == qn), None)
    if idx is None:
        return None                       # carried block: no header here
    y_head = headers[idx][2]
    y_bottom = headers[idx + 1][2] if idx + 1 < len(headers) else 0.0
    anchors = option_anchors_in_block(pdf_path, file_page, y_head, y_bottom)
    return _assign_option(anchors, x_img, y_img)


def solution_headers_on_page(pdf_path, file_page, chapter_records):
    """Locate every printed "Solution to Question N:" header on a page WITH
    its vertical position. Returns [(q_no, y_baseline)] in reading order
    (top of page first), where y is the header's baseline in PDF user space
    (origin at the page BOTTOM-left -- the SAME space image_positions_on_page
    reports, so no coordinate conversion is needed).

    Implementation: pypdf's text visitor (no extra subprocess) -- words are
    grouped into lines by baseline and matched against the header pattern;
    headers whose q_no is not in chapter_records are ignored (foreign chapter
    references). Returns [] when the text layer is missing/garbled or no
    header survives the chapter filter.

    Why positions matter: the old owner lookup returned q_nos WITHOUT
    positions, so a page whose text layer decoded only ONE of seven headers
    let the caller dump ALL seven figures onto that one solution. With
    positions, each figure can be matched to the header actually drawn above
    it -- and when a header cannot be located, the caller claims NOTHING for
    that figure instead of guessing."""
    headers, seen = [], set()
    for y, wl in _page_word_lines(pdf_path, file_page):
        line = " ".join(t for _, t in wl)
        for m in re.finditer(r"Solution\s+to\s+Question\s+(\d{1,3})", line, re.IGNORECASE):
            qn = int(m.group(1))
            if qn in chapter_records and qn not in seen:
                seen.add(qn)
                headers.append((qn, y))
    return headers


def qns_printed_on_page(pdf_path, true_page, chapter_records):
    """Which of this chapter's q_nos are printed on this page, read from the
    text layer (0 tokens). Conservative: a hit counts only if the number
    appears at a question-stem position AND the q_no exists in the chapter.
    Returns sorted unique list -- caller auto-attaches ONLY on exactly one."""
    text = pdftotext_page(pdf_path, true_page)
    if not text.strip():
        return []
    found = set()
    # run-18: colon/dash-terminated headings ("Question 1:") were silently
    # invisible to this scanner -- see question_headers_on_page.
    for m in re.finditer(r"(?m)^\s*(?:Q(?:uestion)?\s*[.:]?\s*)?(\d{1,3})\s*[.:\-–)]", text):
        qn = int(m.group(1))
        if qn in chapter_records:
            found.add(qn)
    return sorted(found)


def chapter_printed_solution_qns(pdf_path, page_files, chapter_records):
    """Which of this chapter's q_nos have a printed 'Solution to Question N:'
    header somewhere in its pages (zero-token text layer). Per-question
    proof that the book prints an explanation for q_no -- used to bypass the
    60% solution-gate for exactly those q_nos (ch25 class: 7/12 solutions =
    58%, gate suppressed 5 REAL solutions whose headers were printed).

    One pdftotext subprocess per page (only run when a gate bypass might
    matter -- see process_pdf). Returns a set of q_nos."""
    found = set()
    for pf in page_files:
        try:
            page_no = int(pf.stem.split("-")[-1])
        except (ValueError, IndexError):
            continue
        text = pdftotext_page(pdf_path, page_no)
        if not text.strip():
            continue
        for m in re.finditer(r"Solution\s+to\s+Question\s+(\d{1,3})", text, re.IGNORECASE):
            qn = int(m.group(1))
            if qn in chapter_records:
                found.add(qn)
    return found


def chapter_printed_question_qns(pdf_path, page_files):
    """Which q_nos have a printed question-stem heading somewhere on the
    chapter's pages (zero-token text layer). The deterministic, no-Gemini
    complement to the Q-pass extraction: where the text layer is readable
    (the MARROW/PSY chapter Q-stem pages), this gives the chapter's actual
    question range. Where the text layer is garbled (scanned pages, certain
    books), this returns an empty set -- the caller must fall back to
    the Q-pass anchor set.

    Reuses the same regex as qns_printed_on_page, but does NOT filter on
    `if qn in chapter_records` -- we want the chapter's full set of
    question-stem-printed q_nos, not just those already in chapter_records,
    because the upstream bug we're fixing is exactly: S-pass accepts
    q_nos for which the question stem was never printed in this chapter.

    Returns a set of q_nos. Zero pdftotext subprocesses per page -- the
    text layer was already read by the rest of the pipeline, but we read
    it again here for clarity (this runs ONCE per chapter, not per window)."""
    found = set()
    # run-18: accept "Question N:" / "N." / "N)" / "N -" / "Q N." headings
    stem_re = re.compile(
        r"(?m)^\s*(?:Q(?:uestion)?\s*[.:]?\s*)?(\d{1,3})\s*[.:\-\u2013)]"
    )
    for pf in page_files:
        try:
            page_no = int(pf.stem.split("-")[-1])
        except (ValueError, IndexError):
            continue
        text = pdftotext_page(pdf_path, page_no)
        if not text.strip():
            continue
        for m in stem_re.finditer(text):
            qn = int(m.group(1))
            found.add(qn)
    return found


def locate_missing_record_pages(pdf_path, page_files, qn_missing, chapter_records):
    """One zero-token text-layer pass over the chapter's pages; returns
    {qn: {"question": [pages], "answer": [pages], "solution": [pages]}}
    for every incomplete qn whose number is printed as a question stem,
    a 'Solution to Question N:' header, OR an ANSWER-KEY table row.

    WHY THREE CATEGORIES (2026-08-08): the rescue pass was sending every
    gap type to the same page list, including solution-side pages
    (105-107) for STEM rescues. The model found no stem region there
    and returned null -- 0 fields filled for all 5 contaminated
    stems. User observed: "yrr solutions to us page pr h hi nhi,
    verify kiu Krna h bs jesa h de de q_id honi chahiye". The
    category split lets the rescue pass route each gap type to
    the correct page set:
      * "question"/"options" -> question-side pages ("N." is printed)
      * "answer"             -> any answer-key row page
      * "solution"           -> solution-side pages ("Solution to Question N:")
    Powers the chapter-end rescue pass: instead of re-sending the whole
    chapter (which targeted retry already did and stalled on), the rescue
    re-asks ONLY the pages where the missing record is actually printed."""
    qns = set(qn_missing)
    pages = {qn: {"question": set(), "answer": set(), "solution": set()}
             for qn in qns}
    if not qns:
        return {}
    header_re = re.compile(r"Solution\s+to\s+Question\s+(\d{1,3})", re.IGNORECASE)
    stem_re_cache = {}
    # RUN-12 ANSWER-KEY ROW MATCHERS: an answer-missing record's rescue must
    # target the page where its answer is printed. The key table can appear in
    # several formats -- a markdown pipe row ("| 13 | B |"), a column list
    # ("13. B"), or a compact "13-B" line -- and its page may not carry the
    # KEY_TABLE_PROBE header ("Answer Key"), so we match rows on EVERY page
    # and only use the header to give key-looking pages a stronger vote.
    key_row_re = re.compile(
        r"(?m)^\s*\|\s*(\d{1,3})\s*\|\s*([A-Da-d])\s*\|"          # | 13 | B |
        r"|^\s*(\d{1,3})\s*[.)]\s*([A-Da-d])\s*$"                # 13. B / 13) B
        r"|^\s*(\d{1,3})\s*[-–]\s*([A-Da-d])\s*$")               # 13 - B
    for pf in page_files:
        try:
            page_no = int(pf.stem.split("-")[-1])
        except (ValueError, IndexError):
            continue
        text = pdftotext_page(pdf_path, page_no)
        if not text.strip():
            continue
        is_key_page = bool(KEY_TABLE_PROBE_RE.search(text))
        # QUESTION-SIDE: the printed question-stem heading ("N." / "N)" / "N-")
        # is on this page -> add to this q_no's "question" page list. The
        # stem itself is the text between this heading and the next option
        # label (A./B./C./D.), so this is the right page for STEM and
        # OPTIONS recovery.
        for qn in qns:
            if qn not in stem_re_cache:
                stem_re_cache[qn] = re.compile(
                    r"(?m)^\s*(?:Q(?:uestion)?\s*[.:]?\s*)?%d\s*[.:\-–)]" % qn)
            if stem_re_cache[qn].search(text):
                pages[qn]["question"].add(page_no)
        # SOLUTION-SIDE: a "Solution to Question N:" header on this page
        # -> this is the page where the explanation for q_no is printed.
        # Use it for SOLUTION recovery, NOT for stem recovery.
        for m in header_re.finditer(text):
            qn = int(m.group(1))
            if qn in pages:
                pages[qn]["solution"].add(page_no)
        # ANSWER-SIDE: a key-table row on this page (markdown pipe row,
        # column list, or "13-B" line) -> this page has the answer for
        # q_no. Use it for ANSWER recovery. Note: a page that is BOTH
        # a key-page AND a solution page (rare, but seen on some MCQ
        # books) gets both categories, so the rescue can use whichever
        # page it lands on first.
        for m in key_row_re.finditer(text):
            qn = int(m.group(1) or m.group(3) or m.group(5))
            if qn in pages:
                pages[qn]["answer"].add(page_no)
        if is_key_page:
            # even a headerless table still puts every row's q on this page
            for m in re.finditer(r"(?m)^\s*\|\s*(\d{1,3})\s*\|", text):
                qn = int(m.group(1))
                if qn in pages:
                    pages[qn]["answer"].add(page_no)
    return {qn: {k: sorted(v) for k, v in cats.items() if v}
            for qn, cats in pages.items()
            if any(cats.values())}


def answer_rescue_prompt(qn, rec, chapter_records):
    """ANSWER-ONLY focused prompt (run-11 RC-5): for a record whose answer is
    missing, ask for ONLY the answer letter of qN -- no stems, no options, no
    solutions. The broad rescue prompt returned other-field text that the
    contamination/scope filters rejected, so answer rescues stalled at
    '0 field(s) filled'."""
    return (
        "You already extracted most of this chapter. Find ONLY the printed "
        f"answer for Question {qn} (the correct option letter A/B/C/D) in the "
        "answer key or the answer line beside the question.\n"
        f"Question {qn} stem begins: {(rec.get('question_text') or '')[:120]!r}\n"
        "Return ONLY a valid JSON array: "
        '[{"q_no": ' + str(qn) + ', "correct_option": "A"|"B"|"C"|"D"|null}] '
        "- one element, nothing else. null if the answer is genuinely not "
        "printed here. No prose, no markdown fences.")


def _transient_gemini_err(err_text):
    t = err_text.lower()
    return ("500" in t or "503" in t or "internal error" in t
            or "high demand" in t or "unavailable" in t)


def gemini_json_call_splitting(model, prompt, page_files, state, label="", direct_page_fallback=False):
    """Execute ONE logical ask (prompt + page images) so that a single bad
    or heavy call can never sink it. Run-4 PROOF of why this exists: a
    whole-chapter targeted retry went out as ONE 14-page call and failed
    as a unit -- 500 on ch9's set, recitation (finish_reason=4) on ch16's
    set (page 217 inside it poisoned the whole call) -- and both rounds
    then just SKIPPED, permanently losing 4+5 solutions and q11's options.
    Ladder per failure:
      1. transient (500/503/high-demand): 20s backoff, identical re-try once
      2. 429/quota burst: 65s backoff, re-try once; still limited -> the
         usual clean save+exit (same as the main batch loop)
      3. any remaining failure: split the page set in HALVES, re-ask each;
         a failing half descends to SINGLE pages
    Returns a list of parsed JSON arrays from all successful sub-calls
    ([] = everything failed; deterministic per-page failures like
    recitation-on-one-page simply cost that page, not the ask)."""
    def one_call(files):
        reset_daily_counter_if_needed(state)
        if quota_exhausted(state):
            print("Daily Gemini call limit reached. Saving progress, exiting.")
            save_state(state)
            sys.exit(0)
        result = call_gemini_on_pages(model, files, prompt=prompt)
        note_call(state)
        save_state(state)
        return result

    malformed_json = object()

    def attempt(files):
        try:
            return one_call(files)
        except Exception as e:
            t = str(e)
            if "429" in t or "quota" in t.lower():
                # Multi-key: rotate to the next project FIRST. A different
                # project has its own RPM window and its own daily budget, so
                # sleeping 65s is pointless when a fresh key is available.
                if handle_429(state, t):
                    print(f"  [429] rate limited{label} -- rotated to "
                          f"{keypool_summary(state)}, retrying now")
                    try:
                        return one_call(files)
                    except Exception as e2:
                        t2 = str(e2)
                        if not ("429" in t2 or "quota" in t2.lower()):
                            print(f"  [WARN] post-rotation call failed differently{label}: {e2}")
                            return None
                        # Rotated key was limited too -- fall through to the
                        # historical backoff below.
                        t = t2
                print(f"  [429] rate limited{label} -- backing off 65s once "
                      f"({keypool_summary(state)})")
                time.sleep(65)
                try:
                    return one_call(files)
                except Exception as e2:
                    t2 = str(e2)
                    if "429" in t2 or "quota" in t2.lower():
                        if handle_429(state, t2):
                            print(f"  [429] post-backoff limit -- rotated to "
                                  f"{keypool_summary(state)}, retrying once")
                            try:
                                return one_call(files)
                            except Exception as e3:
                                print(f"  [WARN] retry after rotation failed{label}: {e3}")
                                return None
                        print(f"  [QUOTA] every key exhausted -- saving, exiting: {e2}")
                        save_state(state)
                        sys.exit(0)
                    print(f"  [WARN] post-backoff call failed differently{label}: {e2}")
                    return None
            if "Invalid Gemini JSON" in t or "empty JSON response" in t:
                # Splitting a malformed structured response into every single
                # page does not repair the model's output format; it merely
                # burns quota (the 2026-07-28 V2 smoke test made 17 such
                # calls). Keep the chapter data, log the retry failure, and
                # leave the targeted fields in the review ledger.
                print(f"  [WARN] malformed Gemini JSON{label}; not splitting into per-page retries")
                return malformed_json
            if _transient_gemini_err(t) or "Empty Gemini response" in t or "Gemini API error" in t:
                print(f"  [WARN] transient/empty Gemini response{label} ({t[:120]}) -- one 20s-backoff retry")
                time.sleep(20)
                try:
                    return one_call(files)
                except Exception as e2:
                    print(f"  [WARN] backoff retry failed{label}: {str(e2)[:160]}")
                    return None
            print(f"  [WARN] call failed{label}: {t[:200]}")
            return None

    if not page_files:
        return []
    whole = attempt(page_files)
    if whole is malformed_json:
        return []
    if whole is not None:
        return [whole]
    # A retry request already combines every remaining q_no into one focused
    # call. If that genuine combined call fails, targeted retry falls back
    # directly to single pages (not another cascade of arbitrary halves).
    if direct_page_fallback:
        halves = [[page] for page in page_files]
    else:
        mid = (len(page_files) + 1) // 2
        halves = [page_files[:mid], page_files[mid:]]
    results = []
    for half in halves:
        if not half:
            continue
        r = attempt(half)
        if r is malformed_json:
            continue
        if r is not None:
            results.append(r)
            continue
        if len(half) == 1:
            print(f"  [WARN] page {Path(half[0]).name} failed even alone{label} -- excluded from this ask")
            continue
        for single in half:
            r2 = attempt([single])
            if r2 is malformed_json:
                continue
            if r2 is not None:
                results.append(r2)
            else:
                print(f"  [WARN] page {Path(single).name} failed even alone{label} "
                      f"-- excluded from this ask (chapter-end recovery paths remain)")
    return results


def _page_crops(pf, parts, overlap_frac=0.12):
    """Split a page image into `parts` horizontal bands with a small overlap
    (a solution clipped at the cut line appears WHOLE in >=1 crop; overlap
    re-extraction is merge-safe because every consumer is fill-only/deduped).
    Returns [(label, crop_path)]; crops live next to the source page."""
    im = Image.open(pf)
    try:
        w, h = im.size
        labels_map = {2: ("TOP half", "BOTTOM half"),
                      4: ("quarter 1 (top)", "quarter 2", "quarter 3", "quarter 4 (bottom)")}
        labels = labels_map.get(parts) or [f"band {i + 1}/{parts}" for i in range(parts)]
        step = h / parts
        ov = step * overlap_frac
        crops = []
        for i in range(parts):
            top = max(0, int(i * step - ov))
            bot = min(h, int((i + 1) * step + ov))
            out = Path(str(pf).rsplit(".", 1)[0] + f"_crop{parts}x{i + 1}.jpg")
            im.crop((0, top, w, bot)).save(out, "JPEG", quality=90)
            crops.append((labels[i], out))
        return crops
    finally:
        im.close()   # run-17: close the PIL handle (GC alone is not immediate)


def drain_failed_pages(model, entries, page_dir, chapter_records, state, stats, pdf_path=None):
    """Second-chance pass for pages that failed even alone (recitation-prone
    content, run-2: PSY page 217 cost ch16 5 solutions). Called at chapter
    end with the recovery framing -- a differently-phrased, focused
    single-page ask often clears a recitation filter that fired on the
    bulk prompt. Fill-only merge: never overwrites first-pass content.
    Returns (chapter_records, new_orphans, healed_entries)."""
    new_orphans = []
    healed = []
    for entry in entries:
        reset_daily_counter_if_needed(state)
        if quota_exhausted(state):
            print("Daily Gemini call limit reached during failed-page drain. Saving, exiting.")
            save_state(state)
            sys.exit(0)
        # PATCH-ONLY RECOVERY (run-7 hardening #2): this page failed WHICH
        # pass? Recover only the fields that pass is allowed to produce.
        # Unknown pass -> unrestricted (None scope) but still provenance-tagged.
        scope = _RECOVERY_SCOPE.get(entry.get("pass"))
        drain_prov = f"DRAIN_{entry.get('pass') or 'S'}"
        pf = page_dir / entry["page_file"]
        if not pf.exists() and pdf_path is not None:
            # cross-day run: /tmp may be wiped -- re-render just this page.
            subprocess.run(["pdftoppm", "-jpeg", "-r", "150",
                            "-f", str(entry["true_page"]), "-l", str(entry["true_page"]),
                            str(pdf_path), str(page_dir / "page")])
        if not pf.exists():
            print(f"  [DRAIN] {entry['page_file']} could not be re-rendered -- dropping from queue")
            healed.append(entry)  # nothing more we can do; don't loop forever
            continue
        try:
            recitation_safe = "finish_reason=4" in str(entry.get("reason", ""))
            if recitation_safe:
                print(f"  [RECITATION_RECOVERY] {entry['page_file']}: paraphrase-mode recovery")
            raw = call_gemini_on_pages(
                model, [pf], context=RECOVERY_CONTEXT,
                prompt=(SCHEMA_PROMPT + RECITATION_RECOVERY_CONTEXT) if recitation_safe else None)
            note_call(state)
            save_state(state)
        except Exception as e:
            # CROP LADDER (run-4 PROOF: PSY ch16 page 217 failed with
            # finish_reason=4 recitation at batch, alone, AND here, and its
            # 5 solutions were lost; ch9's whole-chapter retry died as one
            # 500-prone call). Recitation/safety filters fire on long
            # verbatim spans, and heavy calls hit 500s -- smaller crops mean
            # a smaller span and a lighter call per ask. Halves -> quarters,
            # fill-only merge per successful crop; the page is healed only
            # when EVERY crop at some level came back as a valid call.
            print(f"  [DRAIN] {entry['page_file']} failed on second chance ({e}) -- trying crop ladder")
            ladder_healed = False
            for parts_n in (2, 4):
                all_ok, any_items = True, False
                for crop_label, crop_pf in _page_crops(pf, parts_n):
                    try:
                        crop_ctx = (f"RECOVERY NOTE: you are seeing one crop ({crop_label}) of a "
                                    f"page that must be extracted in pieces. {RECOVERY_CONTEXT}")
                        raw = call_gemini_on_pages(model, [crop_pf], context=crop_ctx)
                        note_call(state)
                        save_state(state)
                    except Exception as e2:
                        print(f"  [DRAIN] {entry['page_file']} {crop_label} failed too ({e2})")
                        all_ok = False
                        continue
                    items2, _ = extract_batch_meta(raw)
                    items2 = [_apply_recovery_scope(dict(it), scope, drain_prov)
                              for it in items2 if isinstance(it, dict)]
                    if items2:
                        any_items = True
                    chapter_records, skipped2 = merge_question_records(
                        chapter_records, items2, stats, fill_only=True)
                    for it in skipped2:
                        new_orphans.append({"chapter_id": entry.get("chapter_id"), "batch_start": -1, "pass": entry.get("pass"),
                                            "pdf_pages": [int(entry["true_page"])], "new_pages": [],
                                            "carry_q_no": None, "item": it})
                    print(f"  [DRAIN] {entry['page_file']} {crop_label}: {len(items2)} item(s)")
                if all_ok:
                    print(f"  [DRAIN] {entry['page_file']} recovered via {parts_n}x crop ladder "
                          f"({any_items and 'items found' or 'page genuinely had no items'})")
                    healed.append(entry)
                    ladder_healed = True
                    break
            if not ladder_healed and recitation_safe:
                print(f"  [OCR_FALLBACK] {entry['page_file']}: attempting OCR+restructure", flush=True)
                try:
                    raw_text = ocr_fallback_text(pf)
                    if raw_text.strip():
                        header_recovered = _recover_ocr_solution_headers(raw_text, chapter_records)
                        # Printed solution headers are the reliable path for this
                        # book. Only ask Gemini to structure OCR when no header
                        # gave us a deterministic owner.
                        if header_recovered:
                            healed.append(entry)
                            ladder_healed = True
                            print(f"  [OCR_FALLBACK] {entry['page_file']}: header recovery completed")
                            continue
                        raw = call_gemini_text_only(model, RECITATION_RECOVERY_CONTEXT +
                            "\nRaw OCR text follows. Structure it into the normal JSON array; "
                            "correct obvious OCR errors but do not invent content.\n\nOCR TEXT:\n" + raw_text)
                        note_call(state); save_state(state)
                        items2, _ = extract_batch_meta(raw)
                        items2 = [normalize_ocr_fallback_item(item) for item in items2
                                  if isinstance(item, dict)]
                        # OCR recovery is field-scoped by the failed pass
                        # (run-7 hardening #2/#4): an OCR_S fragment carries
                        # solution-only content; its stray question/option
                        # text is dropped BEFORE anything can merge.
                        items2 = [_apply_recovery_scope(
                            it, scope, f"OCR_{entry.get('pass') or 'S'}")
                            for it in items2]
                        for item in items2:
                            owner_qn = _ocr_content_owner(item, chapter_records)
                            owner = chapter_records.get(owner_qn) if owner_qn is not None else None
                            continuation = (item.get("solution_text") or "").strip()
                            if owner and continuation and (owner.get("solution_text") or "").strip():
                                tail = _novel_solution_tail(owner["solution_text"], continuation)
                                if tail:
                                    owner["solution_text"] = owner["solution_text"].rstrip() + "\n" + tail
                                    print(f"  [OCR_FALLBACK] spliced novel continuation to q{owner_qn}")
                                else:
                                    print(f"  [OCR_FALLBACK] duplicate solution content ignored for q{owner_qn}")
                                item["solution_text"] = None
                                # A null q_no fragment becomes mergeable once its
                                # option-content evidence identifies an owner.
                                item["q_no"] = owner_qn
                        print(f"  [OCR_FALLBACK] {entry['page_file']}: normalized {len(items2)} OCR item(s)")
                        chapter_records, skipped2 = merge_question_records(chapter_records, items2, stats, fill_only=True)
                        new_orphans.extend({"chapter_id": entry.get("chapter_id"), "pass": entry.get("pass"), "item": it,
                                            "pdf_pages": [entry["true_page"]]} for it in skipped2)
                        healed.append(entry)
                        ladder_healed = True
                        print(f"  [OCR_FALLBACK] {entry['page_file']}: recovered {len(items2)} item(s)")
                except Exception as ocr_err:
                    print(f"  [OCR_FALLBACK] {entry['page_file']} failed: {ocr_err}")
            if not ladder_healed:
                print(f"  [DRAIN] {entry['page_file']} resisted crop+OCR recovery -- kept in failed_pages queue")
            continue
        print(f"  [DRAIN] {entry['page_file']} recovered on second chance")
        items, _meta = extract_batch_meta(raw)
        items = [_apply_recovery_scope(dict(it), scope, drain_prov)
                 for it in items if isinstance(it, dict)]
        chapter_records, skipped = merge_question_records(chapter_records, items, stats, fill_only=True)
        for it in skipped:
            new_orphans.append({"chapter_id": entry.get("chapter_id"), "batch_start": -1, "pass": entry.get("pass"),
                                "pdf_pages": [int(entry["true_page"])], "new_pages": [],
                                "carry_q_no": None, "item": it})
        healed.append(entry)
    return chapter_records, new_orphans, healed

# ============================================================
# FEATURE 2 — carry-forward context (Gemini's API is stateless:
# continuity must be injected manually into every new request)
# ============================================================

def extract_batch_meta(items):
    """Peel the {"_batch_meta": {...}} and {"_figure_map": [...]} control
    objects out of Gemini's array. Returns (question_items, meta_dict).
    Meta of a failed/absent call = {}. The figure map (q_no+slot per figure
    in reading order, run-6 user ask) rides along under meta["figure_map"]."""
    questions, meta = [], {}
    for it in items:
        if isinstance(it, dict) and "_batch_meta" in it:
            m = it.get("_batch_meta")
            if isinstance(m, dict):
                # keep a figure_map that arrived BEFORE the batch-meta object
                # (response order is not guaranteed)
                if meta.get("figure_map"):
                    m = {**m, "figure_map": meta["figure_map"]}
                meta = m          # last one wins (single-page retries)
            continue
        if isinstance(it, dict) and "_figure_map" in it:
            fm = it.get("_figure_map")
            if isinstance(fm, list) and not meta.get("figure_map"):
                meta["figure_map"] = fm   # first non-empty map wins
            continue
        questions.append(it)
    return questions, meta

def compute_carry(batch_meta, items, chapter_records, ending_page):
    """Decide whether a batch ended mid-question and build the payload carried
    into the NEXT request. Primary signal: Gemini's own _batch_meta (it can
    see the page bottom). Fallback when NO usable meta: detect the pass shape
    from the items themselves --
      * S-pass items carry solution_text (never question_text): a non-empty
        solution on the window's highest q_no that LOOKS TRUNCATED proves the
        page ended mid-solution -> carry that q_no as a "solution" cut. This
        was the run-8 root cause: the old fallback required question_text,
        which S-pass records never have, so carry-in was ALWAYS "-" and the
        unnumbered continuation on the next page came back q_no=null.
      * Q-pass items carry question_text: keep the battle-tested fallback
        (highest q_no with a stem but no solution yet -> carry as "solution").
    Stores: last_open_question, last_question_text, partial_solution,
    partial_options, ending_page."""
    have_meta = bool(batch_meta)
    meta_says_open = bool(batch_meta.get("ends_mid_content")) if have_meta else False
    last_qn = None
    if have_meta:
        try:
            last_qn = int(batch_meta.get("last_q_no"))
        except (TypeError, ValueError):
            last_qn = None
    cut_part = batch_meta.get("cut_part") or "unknown" if have_meta else "solution"

    if meta_says_open and last_qn is None:
        # model knows it's cut but can't see the number -- still carry the tail
        return {"last_open_question": None, "last_question_text": None,
                "partial_solution": batch_meta.get("tail_text") or None,
                "partial_options": None, "ending_page": ending_page,
                "cut_part": cut_part}
    if not meta_says_open:
        if have_meta:
            return None              # model says the page ended cleanly
        batch_qns = []
        s_shaped = False             # items look like S-pass output
        for it in items:
            try:
                batch_qns.append(int(it.get("q_no")))
            except (TypeError, ValueError):
                pass
            if (it.get("solution_text") or "").strip() \
                    and not (it.get("question_text") or "").strip():
                s_shaped = True
        if not batch_qns:
            return None
        candidate = max(batch_qns)
        rec = chapter_records.get(candidate, {})
        if s_shaped:
            # S-pass fallback (run-8): a truncated solution proves the page
            # ended mid-solution -> carry it so the next window's unnumbered
            # continuation resolves to this q_no instead of q_no=null.
            sol = (rec.get("solution_text") or "").strip()
            if sol and looks_truncated_solution(
                    sol, has_tables=bool(rec.get("tables"))):
                last_qn, cut_part = candidate, "solution"
            else:
                return None
        else:
            # Q-pass fallback (battle-tested): highest q_no with a stem but
            # no solution yet -> carry as "solution" (if it spans the next
            # window, the model continues it under the same q_no).
            if rec.get("question_text") and not rec.get("solution_text"):
                last_qn, cut_part = candidate, "solution"
            else:
                return None

    rec = chapter_records.get(last_qn, {})
    return {"last_open_question": last_qn,
            "last_question_text": rec.get("question_text"),
            "partial_solution": rec.get("solution_text") or batch_meta.get("tail_text") or None,
            "partial_options": rec.get("options"),
            "ending_page": ending_page,
            "cut_part": cut_part}

def build_carry_context(carry, overlap_pages, new_pages=None):
    """The actual text prepended to the next request.

    carry: the previous window's open item (its q_no + partial content).
    overlap_pages: PDF pages re-sent from the previous window (continuity).
    new_pages: the genuinely NEW pages of this window (run-8: made explicit
    so Gemini can resolve an unnumbered continuation's owner from the
    preceding overlap page instead of defaulting to q_no=null -- the orphan
    source the audit found)."""
    lines = []
    if carry:
        qn = carry["last_open_question"]
        lines += [
            "CONTEXT FROM PREVIOUS BATCH (continuity context only -- do NOT",
            "output any of this text as a new item):",
            "Previous batch ended with an incomplete question.",
            f"Question Number: {qn if qn is not None else 'unknown'}",
            f"Question: {(carry.get('last_question_text') or '')[:600]}",
            f"Options seen so far: {json.dumps(carry.get('partial_options'), ensure_ascii=False)[:400]}",
            f"Partial Solution: {(carry.get('partial_solution') or '')[:600]}",
            f"(cut part: {carry.get('cut_part')}; ended at PDF page {carry.get('ending_page')})",
            "If the first content in this batch belongs to this question,",
            "CONTINUE it under the SAME q_no instead of creating a new question.",
        ]
    if overlap_pages:
        lines.append(
            "OVERLAP / CONTEXT PAGES (supplied ONLY to establish continuity "
            "and ownership; do NOT re-output their content as new items): "
            f"PDF page(s) {', '.join(map(str, overlap_pages))}."
        )
        if new_pages:
            lines.append(
                "NEW PAGES TO EXTRACT (the pages whose content this pass must "
                f"return): PDF page(s) {', '.join(map(str, new_pages))}."
            )
        lines.append(
            "OWNERSHIP RULES for unnumbered continuations:\n"
            "- If a new page begins with an unnumbered continuation and the "
            "preceding OVERLAP page proves it belongs to Question N (e.g. the "
            "'Solution to Question N:' header or question stem N is visible "
            "at the bottom of the overlap page), return that continuation "
            "with q_no=N.\n"
            "- Keep assigning it to N until an explicit new question/solution "
            "heading establishes another owner.\n"
            "- Do NOT return q_no=null merely because the number is not "
            "repeated on the new page when ownership is clearly established "
            "by the overlap page.\n"
            "- NEVER invent a q_no when ownership is uncertain. If ownership "
            "genuinely cannot be established, return q_no=null as an explicit "
            "unassigned fragment for later recovery -- never attach it to a "
            "different question."
        )
    return "\n".join(lines)

ANSWER_KEY_ROW_RE = re.compile(r"\|\s*(\d{1,3})\s*\|\s*([A-Da-d])\s*\|")
SOLUTION_TO_Q_RE = re.compile(r"Solution to Question\s+(\d{1,3})", re.IGNORECASE)
# Dump-tail detector (stricter): title-case header WITH colon, i.e. the real
# printed "Solution to Question 2:" section header, not prose mentions.
SOLUTION_DUMP_HDR_RE = re.compile(r"Solution to Question\s+(\d{1,3})\s*:")

# --- stale carry-context guards (clarified RCA: the header-alone-at-page-end
# split is NORMAL in this book for questions AND solutions, and the overlap
# window resolves it -- do NOT touch that path. The actual bug is a carry
# context whose OWN split never resolves staying alive long enough to meet
# the same number again in the Solutions section ("Solution to Question 4:")
# and cross-merge question prose with solution prose).
CARRY_EXPIRY_BATCHES = 3      # unresolved carry dies after this many batches

SECTION_HEADING_RE = re.compile(
    r"^\s*(?:chapter\s+\d{1,3}\s*[:.\-–]?\s*)?"
    r"(detailed\s+explanations?|answer\s*keys?|answers?\s+(?:and|&)\s+explanations?|"
    r"explanations?|answers?)\s*[.:\-–]?\s*$", re.IGNORECASE)

# --- run-4 audit RCA guards (2026-07-26 full-output audit; see
# ROOT_CAUSE_ANALYSIS.md "Run-4 audit" section). Deterministic, zero-token:
MAX_QUESTION_IMAGES = 3       # >3 question-side figures on ONE question is almost
                              # certainly wrong-owner attribution (PSY-022-003
                              # collected SEVEN via repeated model-confirmed passes).
MAX_SOLUTION_IMAGES = 2       # a solution block cites at most a figure or two;
                              # >2 on ONE solution from the deterministic path means
                              # under-detected headers dumped neighbours' figures onto
                              # it (user report: a 7-figure solutions page collapsed
                              # into just 2 solutions -- the old single-owner
                              # shortcut attached EVERY page image to the one header
                              # the text layer happened to decode).
MIN_IMAGE_BYTES = 1500        # <1.5 KB webp is virtually always an empty/broken crop
                              # (PSY-003-014_Q_01 was 414 bytes of nothing and shipped).
STEM_COHERENCE_MARGIN = 0.15  # stem-conflict resolver: stem<->payload coherence scores
                              # must differ by at least this to decide automatically;
                              # below it both variants are logged for review (no silent picks).
DANGLING_END_RE = re.compile(r"(:|\u2014|\u2013|\u2022)\s*$")   # ends ':' / em/en-dash / bullet
OPTION_LINE_START_RE = re.compile(r"^\s*Option\s+([A-D])\b\s*[:.)]\s*", re.IGNORECASE)

TERMINAL_PUNCT = ".!?)\"'\u201d\u00bb"


PIPE_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|.*\|\s*$")


def _table_body_rows(table):
    """Normalized non-header body rows for completeness/prefix comparison."""
    lines = []
    for line in str((table or {}).get("markdown") or "").splitlines():
        norm = re.sub(r"\s+", "", line).lower()
        if not norm or re.fullmatch(r"\|?-{3,}(?:\|-{3,})+\|?", norm):
            continue
        lines.append(norm)
    return lines


def _dedupe_tables(tables):
    """Keep one best table per overlap capture.

    Exact whitespace-insensitive matches are duplicates.  A shorter table
    whose normalized rows are a strict prefix of another is the page-break
    capture of the longer table, so retain the longer version.
    """
    candidates = [t for t in (tables or []) if isinstance(t, dict)]
    # Evaluate full captures first so a partial capture can never win by order.
    candidates.sort(key=lambda t: (-len(_table_body_rows(t)), -len(str(t.get("markdown") or "")),
                                  re.sub(r"\s+", "", str(t.get("markdown") or "").lower())))
    kept = []
    for table in candidates:
        key = re.sub(r"\s+", "", str(table.get("markdown") or "").lower())
        rows = _table_body_rows(table)
        duplicate = False
        for winner in kept:
            winner_key = re.sub(r"\s+", "", str(winner.get("markdown") or "").lower())
            winner_rows = _table_body_rows(winner)
            same = bool(key) and key == winner_key
            strict_prefix = bool(rows) and len(rows) < len(winner_rows) and winner_rows[:len(rows)] == rows
            if same or strict_prefix:
                duplicate = True
                break
        if not duplicate:
            kept.append(table)
    return kept


def _extract_inline_pipe_tables(text):
    """Remove 3+ consecutive markdown pipe-table lines from prose and return
    them as structured tables.  This is a schema firewall for retry output."""
    lines = (text or "").splitlines()
    prose, extracted, i = [], [], 0
    while i < len(lines):
        if not PIPE_TABLE_LINE_RE.match(lines[i]):
            prose.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and PIPE_TABLE_LINE_RE.match(lines[j]):
            j += 1
        block = lines[i:j]
        if len(block) >= 3:
            extracted.append({"type": "recovered inline table", "markdown": "\n".join(block)})
        else:
            prose.extend(block)
        i = j
    return "\n".join(prose).strip(), extracted


def _normalize_solution_payload(text, tables, qn=None):
    clean, recovered = _extract_inline_pipe_tables(text)
    all_tables = _dedupe_tables(list(tables or []) + recovered)
    if recovered:
        print(f"  [SCHEMA_VIOLATION] q{qn if qn is not None else '?'}: moved "
              f"{len(recovered)} inline pipe-table block(s) from solution_text to tables")
    # This warning is deliberately after remediation: any surviving 3-line
    # pipe table is a future parser case, not silently shipped prose.
    if any(len([ln for ln in clean.splitlines()[i:i + 3] if PIPE_TABLE_LINE_RE.match(ln)]) == 3
           for i in range(max(0, len(clean.splitlines()) - 2))):
        print(f"  [SCHEMA_VIOLATION] q{qn if qn is not None else '?'}: pipe-table syntax remains in solution_text")
    return clean, all_tables


def looks_truncated_solution(text, has_tables=False, has_images=False):
    """REAL truncation patterns only (replaces the weak 'no terminal punct'
    heuristic that produced ~53 false positives against this book's
    bullet-list endings). Detects:
      - dangling connector endings  ('...criteria:', '...given below --')
      - raw trailing space after a word (stream cut mid-flow: '...• During ')
      - suspiciously short AND bare (no table/figure carrying the rest)
    """
    t = (text or "")
    s = t.rstrip()
    if not s:
        return False
    # This sweep is specifically a prose-continuation detector. A populated
    # table is the continuation for a lead-in ("stages are:"), so never send
    # that record to a truncation retry based on text ending alone.
    if has_tables:
        return False
    # Same logic for figures (2026-08-11): "...as shown in the image below:"
    # is a lead-in whose continuation IS the attributed figure, not missing
    # prose. This parameter was accepted but never read, so every such
    # solution was re-asked at a real quota cost (38 forced retries in the
    # 2026-08-11 ANA run) and came back identical -- the source text genuinely
    # ends there. Only suppress when a figure is actually attached to the
    # SOLUTION side; a question-side figure does not explain a solution's
    # dangling lead-in.
    if has_images:
        return False
    # Do not infer truncation from absent terminal punctuation, a trailing
    # OCR space, or a short explanation. Source pages frequently omit a final
    # period, and those heuristics created false retries (including q13).
    # Only an explicit dangling lead-in is deterministic enough to re-ask.
    return bool(DANGLING_END_RE.search(s))


def _stem_payload_coherence(stem, rec):
    """Share of stem word-tokens present in the record's OWN options+solution.
    A stem is explained by its own solution, so the right stem for a record
    coheres with the record's payload (run-4: PSY-012-001 kept PSY-012-013's
    chart stem while its solution described a mania vignette -- coherence
    0 vs the real stem)."""
    toks = [t for t in re.findall(r"\w+", (stem or "").lower()) if len(t) > 2]
    payload = " ".join(filter(None, [
        rec.get("solution_text") or "",
        " ".join(str(v) for v in (rec.get("options") or {}).values()),
    ]))
    ptoks = set(re.findall(r"\w+", payload.lower()))
    if not toks or not ptoks:
        return 0.0
    return sum(1 for t in toks if t in ptoks) / len(toks)


def _foreign_option_line(frag, rec):
    """Wrong-owner guard for solution fragments that BEGIN with an
    'Option X:' explanation (run-4: PSY-009-007 got PSY-009-006's
    'Option C: Catharsis...' line glued on top). A legitimate
    'Option X:' continuation names the OWNER's option X content; a
    foreign one does not."""
    m = OPTION_LINE_START_RE.match(frag or "")
    if not m:
        return False
    letter = m.group(1).upper()
    opt_text = (rec.get("options") or {}).get(letter)
    if opt_text is None:
        return True   # owner has no such option -> provably foreign
    otoks = [t for t in re.findall(r"\w+", str(opt_text).lower()) if len(t) > 2]
    if not otoks:
        return False
    head = " ".join(re.findall(r"\w+", (frag or "").lower())[:25])
    return sum(1 for t in otoks[:6] if t in head) == 0


def _solution_fragment_foreign(frag, qn, rec, chapter_records):
    """Deterministic 'this retry fragment does NOT belong to q{qn}' proofs
    for solution text returned by targeted_retry. External-audit class
    (2026-08-02): a truncated-solution re-ask for q16 came back carrying
    q17's text and the old code APPENDED it because 'not mostly present'
    was treated as new continuation -- blending two questions' solutions.
    Returns a short reason string, or None when no proof fires.

    Proofs (all zero-token, cross-record where possible):
      1. the fragment begins with an 'Option X:' explanation of an option
         this record does not own (reuses the orphan wrong-owner guard);
      2. the fragment carries an embedded 'Solution to Question N:' header
         naming a DIFFERENT question (self-labeled foreign block);
      3. the fragment's first content line exists verbatim in another
         record of this chapter (sibling-donor proof -- the same evidence
         the integrity sweep uses before it trims a head).

    A fragment that passes all three is kept: a genuine continuation after
    a cut point shares no tokens with the existing text by construction,
    so low overlap alone is deliberately NOT foreign evidence."""
    s = (frag or "").strip()
    if not s:
        return None
    if _foreign_option_line(s, rec):
        return "fragment begins with an 'Option' line the owner cannot own"
    for m in re.finditer(r"Solution\s+to\s+Question\s+(\d{1,3})", s, re.IGNORECASE):
        n = int(m.group(1))
        if n != qn:
            return f"fragment carries 'Solution to Question {n}:' (not q{qn})"
    first_line = s.splitlines()[0].strip()
    if first_line and len(first_line) >= 20:
        for other_qn, other in chapter_records.items():
            if other_qn == qn:
                continue
            if first_line in (other.get("solution_text") or ""):
                return f"first line exists verbatim in q{other_qn}'s solution"
    return None


def detect_section_boundary(items):
    """Return a short label on the FIRST batch whose extracted content shows
    the questions -> answers/solutions section boundary, else None. Signals,
    all from Gemini's own extraction (body-page pdftotext is garbled for this
    book, so deterministic page-text scanning is NOT an option):
      - a standalone heading line like "Detailed Explanations" / "Answer Key"
      - a self-labeled "Solution to Question N:" solution fragment
      - an Answer Key table (type says answer + 'Correct Option' markdown)"""
    for it in items:
        for t in it.get("tables") or []:
            md = t.get("markdown") or ""
            if "answer" in str(t.get("type", "")).lower() and "Correct Option" in md:
                return "Answer Key table"
        sol = it.get("solution_text") or ""
        m = SOLUTION_TO_Q_RE.search(sol)
        if m:
            return f"'Solution to Question {m.group(1)}' fragment"
        for field in (it.get("question_text"), sol):
            for line in str(field or "").splitlines():
                line = line.strip()
                if line and len(line) <= 60 and SECTION_HEADING_RE.match(line):
                    return f"'{line}' heading"
    return None


def _carry_resolved(rec, cut_part):
    """Has the piece this carry was waiting for actually arrived?"""
    cut = (cut_part or "solution").lower()
    if cut == "options":
        return len(rec.get("options") or {}) >= 4
    if cut == "question":
        return bool((rec.get("question_text") or "").strip()) and \
            len(rec.get("options") or {}) >= 4
    return bool((rec.get("solution_text") or "").strip())   # "solution"/"unknown"


def enforce_carry_expiry(carry, batch_seq, tracker, banned, chapter_records, chapter_id):
    """Kill stale carry contexts before they can cross-merge into the
    Solutions section. Rules, in order:
      - q_no already banned this chapter -> drop (the no-meta fallback in
        compute_carry would otherwise RESPAWN the same stale carry every
        batch that number stays the max text-no-solution candidate).
      - carried piece now filled -> resolved, drop quietly, un-track.
      - same q_no unresolved for CARRY_EXPIRY_BATCHES batches -> drop,
        mark the question still-incomplete IMMEDIATELY (turant), and ban
        the number for the rest of the chapter.
    Numberless tails (last_open_question=None) live one batch by
    construction and pass through untouched."""
    if carry is None:
        return None
    qn = carry.get("last_open_question")
    if qn is None:
        return carry
    if qn in banned:
        return None
    rec = chapter_records.get(qn) or {}
    if _carry_resolved(rec, carry.get("cut_part")):
        tracker.pop(qn, None)
        return None
    opened = tracker.setdefault(qn, batch_seq)
    if batch_seq - opened >= CARRY_EXPIRY_BATCHES:
        tracker.pop(qn, None)
        banned.add(qn)
        cut = carry.get("cut_part") or "solution"
        print(f"  [CARRY] q{qn} carry EXPIRED unresolved after "
              f"{CARRY_EXPIRY_BATCHES} batches (cut part: {cut}) -- dropped + "
              f"number banned this chapter + marked still-incomplete (a stale "
              f"context must never meet its number again in the Solutions "
              f"section)")
        _append_jsonl(DATA_DIR / "still_incomplete_after_retry.jsonl",
                      {"q_no": qn, "missing": [cut], "chapter_id": chapter_id,
                       "reason": "carry-context expired unresolved"})
        return None
    return carry


def _frag_mostly_present(frag, existing, threshold=0.85):
    """Token-overlap duplicate guard: substring checks miss near-dupes when
    punctuation differs ('...target.' vs '...target for...'), which caused a
    double-append in testing. True when >=threshold of frag's tokens already
    appear in existing's token set."""
    f = re.findall(r"\w+", (frag or "").lower())
    e = set(re.findall(r"\w+", (existing or "").lower()))
    if not f or not e:
        return False
    return sum(1 for t in f if t in e) / len(f) >= threshold


def recover_orphans(orphans, chapter_records, subject, chapter_no, stats):
    """FEATURE 3 -- second-pass owner matching for q_no=null fragments.
    Confidence rules, in order:
      0. ANSWER-KEY TABLE orphan (run-2 finding: pages 182/194/235/241 all
         produced {'q_no': None, 'tables':[Answer Key]} orphans and the old
         rules had NO handler for them -- a latent whole-chapter answer-loss
         bug). Parse the markdown rows deterministically and fill missing
         correct_options (fill-only, never overwrite).
      1. owner printed INSIDE the fragment text ("Solution to Question 3:")
         -- run-2: pages 85/159/273 all carried self-labeling fragments that
         the old matcher never parsed (0/13 orphans recovered that run).
      2. the carry-forward owner captured when the fragment arrived.
      3. the highest-numbered question from the SAME batch window is missing
         exactly the field the orphan provides (solution/options/question).
         NEW: also attaches continuation fragments to PARTIAL owners
         (half-solutions) via append, when the fragment leads NEW text.
    Recovered content is APPENDED / fill-only (existing text is never
    overwritten). Whatever remains unmatched is returned for orphans.jsonl
    -- never silently discarded."""
    remaining = []
    for orph in orphans:
        item = orph["item"]
        page = (orph.get("new_pages") or orph.get("pdf_pages") or ["?"])[0]

        # ---- rule 0: answer-key table -> deterministic correct_option fills
        # Upgraded (run-4 audit): a key whose rows ALL match existing answers
        # is CONSUMED as "verified" instead of lingering in orphans.jsonl as
        # noise (5 such orphans in the PSY run), and any DISAGREEING row is
        # written to data/integrity_flags.jsonl -- a free wrong-answer alarm.
        key_rows = []
        for t in item.get("tables") or []:
            if "answer" not in str(t.get("type", "")).lower() and "Correct Option" not in (t.get("markdown") or ""):
                continue
            for qn_s, letter in ANSWER_KEY_ROW_RE.findall(t.get("markdown") or ""):
                key_rows.append((int(qn_s), letter.upper()))
        if key_rows:
            filled_by_key, disagreed, unknown_qn = 0, [], []
            for kqn, letter in key_rows:
                rec = chapter_records.get(kqn)
                if rec is None:
                    unknown_qn.append(kqn)
                elif rec.get("correct_option"):
                    if str(rec["correct_option"]).strip().upper() != letter:
                        disagreed.append({"q_no": kqn, "record": rec["correct_option"], "key": letter})
                else:
                    rec["correct_option"] = letter
                    filled_by_key += 1
            if disagreed:
                _append_jsonl(DATA_DIR / "integrity_flags.jsonl",
                              {"kind": "answer_key_disagrees", "page": page,
                               "chapter_id": stats.get("chapter_id"), "rows": disagreed})
                print(f"  [WARN] [ORPHAN] answer-key table DISAGREES with extracted answers on "
                      f"{len(disagreed)} row(s) -- logged to integrity_flags.jsonl")
            if filled_by_key or not unknown_qn:
                stats["orphans_recovered"] += 1
                print(f"  [ORPHAN] Recovered orphan: page={page} answer-key table -> "
                      f"{filled_by_key} answer(s) filled, {len(key_rows) - filled_by_key - len(unknown_qn)} "
                      f"row(s) verified against existing answers -- consumed")
                continue
            # ENTIRELY foreign key (every row references another chapter's
            # q_nos): STOP here. Falling through to rules 1-4 would let this
            # key's table glue onto a local record via carry/last-qn merge.
            print(f"  [ORPHAN] answer-key table references q_nos outside this chapter "
                  f"({unknown_qn}) -- kept for review, NOT merged anywhere")
            remaining.append({**orph, "blocked_reason": "foreign answer key (all rows "
                              "reference q_nos not in this chapter)"})
            continue

        # ---- rule 0b: duplicate scrap consume (run-4 audit: both content
        # orphans in PSY-006 were re-extractions of records that ALREADY
        # exist complete -- a stem identical to some record's stem, or bare
        # options identical to that record's options). Consume them instead
        # of re-merging (idempotent) or persisting as noise.
        if not item.get("tables") and not item.get("solution_text"):
            itxt = (item.get("question_text") or "").strip()
            if itxt:
                dup = any((r2.get("question_text") or "").strip()
                          and _frag_mostly_present(itxt, r2["question_text"], 0.9)
                          and _frag_mostly_present(r2["question_text"], itxt, 0.9)
                          for r2 in chapter_records.values())
                if dup:
                    stats["orphans_recovered"] += 1
                    print(f"  [ORPHAN] Consumed orphan: page={page} stem already present "
                          f"verbatim in this chapter (duplicate re-extraction scrap)")
                    continue
            elif item.get("options"):
                cand = chapter_records.get(orph.get("last_qn_in_batch"))
                c_opts = {str(k).strip().upper(): str(v) for k, v in (cand or {}).get("options", {}).items()}
                if cand and all(str(k).strip().upper() in c_opts
                                and _frag_mostly_present(str(v), c_opts[str(k).strip().upper()], 0.9)
                                for k, v in item["options"].items()):
                    stats["orphans_recovered"] += 1
                    print(f"  [ORPHAN] Consumed orphan: page={page} options fragment already "
                          f"present on q{orph.get('last_qn_in_batch')} (duplicate scrap)")
                    continue

        owner, reason = None, None
        # ---- rule 1: owner self-labeled inside the fragment text
        m = SOLUTION_TO_Q_RE.search(item.get("solution_text") or "")
        if m:
            hint_qn = int(m.group(1))
            if hint_qn in chapter_records:
                owner, reason = hint_qn, "self-labeled 'Solution to Question N' fragment"
        # ---- rule 2: carry-forward owner
        carry_qn = orph.get("carry_q_no")
        if owner is None and carry_qn is not None and carry_qn in chapter_records:
            owner = carry_qn
            reason = f"{orph.get('cut_part') or 'content'} continuation (carry-forward)"
        # ---- rule 3: highest q_no of the same batch window missing that field
        if owner is None:
            last_qn = orph.get("last_qn_in_batch")
            rec = chapter_records.get(last_qn) if last_qn is not None else None
            if rec:
                frag = (item.get("solution_text") or "").strip()
                existing = (rec.get("solution_text") or "").strip()
                if item.get("solution_text") and not existing:
                    owner, reason = last_qn, "solution continuation"
                elif (item.get("solution_text") and existing and frag
                      and looks_truncated_solution(existing,
                                                   has_tables=bool(rec.get("tables")))
                      and not _frag_mostly_present(frag, existing)):
                    # PARTIAL owner append (run-8 tightening): only append a
                    # continuation to an owner whose existing solution PROVABLY
                    # ends mid-flow (truncated). Appending to a complete
                    # solution would glue a neighbour's or new question's text
                    # onto it -- a wrong-owner guess. The reliable signal
                    # matches the compute_carry S-pass fallback, so the two
                    # paths agree.
                    owner, reason = last_qn, "solution continuation (PARTIAL owner append)"
                elif item.get("options") and not rec.get("options") \
                        and orph.get("pass") in ("Q", None):
                    owner, reason = last_qn, "options continuation"
                elif item.get("question_text") and not rec.get("question_text") \
                        and orph.get("pass") in ("Q", None):
                    owner, reason = last_qn, "question continuation"
        # ---- rule 4: positional certainty (Gap-1). An orphan carrying the
        # STEM (+options) can only belong to a record that is MISSING its
        # stem. Text-similarity between a stem and its own solution is
        # always ~0 (they never overlap lexically), so similarity-based
        # matching provably fails here (prod: PSY-001-003 stayed stemless
        # with answer+solution intact). When the chapter has EXACTLY ONE
        # stem-less record, position alone is the proof. Gated to Q-pass
        # fragments: a solution/OCR fragment must never claim the stem slot.
        if owner is None and item.get("question_text") and item.get("options") \
                and orph.get("pass") in ("Q", None):
            stemless = [qn for qn, r in chapter_records.items()
                        if not (r.get("question_text") or "").strip()]
            if len(stemless) == 1:
                owner, reason = stemless[0], "question+options fallback (chapter's sole stem-less record)"
        # ---- rule 5 (run-14): VERIFIED DUPLICATE. A q_no-less fragment whose
        # text is already present in a record (option tail re-sent by a later
        # batch's carry -- PAY-033 p356 "(d) the person has recently shown..."
        # duplicating q8's option D; or a DRAIN scrap that repeats an option
        # block) is a duplicate, NOT new content. Consume it deterministically
        # with a match to the record's EXISTING text -- no ownership guess, no
        # merge (the content is already there).
        if owner is None:
            frag_text = (item.get("question_text") or "").strip()
            frag_opts = item.get("options") or {}
            dup_qn = None
            for qn, r in chapter_records.items():
                r_opts = {str(k).strip().upper(): str(v or "").strip()
                          for k, v in (r.get("options") or {}).items() if v}
                matched = False
                if frag_text:
                    for opt_text in r_opts.values():
                        if opt_text and _frag_mostly_present(frag_text, opt_text, 0.9):
                            matched = True
                            break
                if not matched and frag_opts:
                    f_vals = {str(v or "").strip().lower() for v in frag_opts.values() if v}
                    r_vals = {v.lower() for v in r_opts.values()}
                    if f_vals and f_vals <= r_vals:
                        matched = True
                if matched:
                    dup_qn = qn
                    break
            if dup_qn is not None:
                stats["orphans_recovered"] = stats.get("orphans_recovered", 0) + 1
                print(f"  [ORPHAN] Consumed orphan: page={page} fragment already "
                      f"present on q{dup_qn} (verified duplicate -- not data loss)")
                continue
        if owner is None:
            # RUN-20 (2026-08-08): a FOREIGN-chapter q_no drop is EXPECTED,
            # not data loss. The merge correctly rejected the item (its
            # q_no was not in the chapter's question range and not in
            # carry), the split layer will route it to
            # unresolved_qids.jsonl with reason=
            # "missing_question_for_solution", and the export gate
            # already counts it there. Logging "Could not determine
            # owner" + bumping stats["orphans_remaining"] would
            # double-count: the operator would see N in unresolved
            # orphans AND a count of N in unresolved_qids.jsonl, and
            # have no way to tell they're the same set. Print a clear
            # note and DO NOT count it as unresolved.
            if orph.get("drop_reason") == "foreign_chapter_qno":
                item_qn = (orph.get("item") or {}).get("q_no")
                print(f"  [ORPHAN] q{item_qn}: foreign-chapter q_no drop "
                      f"kept for review in orphans.jsonl (NOT a data loss; "
                      f"split layer will route to unresolved_qids.jsonl "
                      f"with reason='missing_question_for_solution')")
                # Keep the item in remaining so the orphans.jsonl record
                # is still persisted for the human reviewer -- but DO NOT
                # count it as unresolved here (the split layer's count
                # is the authoritative one for this class).
                remaining.append(orph)
                continue
            print(f"  [ORPHAN] Could not determine owner: page={page} kept in orphans.jsonl")
            remaining.append(orph)
            continue
        rec = chapter_records[owner]
        # Wrong-owner guard (run-4: PSY-009-007): an orphan solution fragment
        # that BEGINS with an 'Option X:' explanation of an option the owner
        # does not have belongs to a DIFFERENT question -- never glue it on.
        # Other fields still merge; the blocked fragment stays visible.
        blocked_sol = bool(item.get("solution_text")
                           and _foreign_option_line(item["solution_text"].strip(), rec))
        if blocked_sol:
            stats["foreign_fragments_blocked"] = stats.get("foreign_fragments_blocked", 0) + 1
            print(f"  [WARN] [ORPHAN] blocked foreign solution fragment for q{owner} "
                  f"(starts with 'Option' line the owner cannot own) -- fragment kept in "
                  f"orphans.jsonl, other fields still merge")
            remaining.append({**orph, "blocked_reason":
                              "foreign Option-line head (wrong-owner guard); "
                              f"suspected owner differs from q{owner}"})
        sol_blocked = False
        if item.get("solution_text") and not blocked_sol:
            frag = item["solution_text"].strip()
            if frag and not _frag_mostly_present(frag, rec.get("solution_text") or ""):
                # Wrong-owner guard (same audit class as the retry append):
                # rule 3's "PARTIAL owner append" must not glue a NEIGHBOUR's
                # solution onto this record just because the overlap is low
                # (the audit's foreign-tail candidates: 006-014, 011-017,
                # 011-026, 012-002, 014-015, 022-008).
                foreign = _solution_fragment_foreign(frag, owner, rec, chapter_records)
                if foreign:
                    stats["foreign_fragments_blocked"] = stats.get("foreign_fragments_blocked", 0) + 1
                    print(f"  [WARN] [ORPHAN] blocked foreign solution fragment for q{owner} "
                          f"({foreign}) -- fragment kept in orphans.jsonl for review")
                    remaining.append({**orph, "blocked_reason": f"foreign solution fragment: {foreign}"})
                    sol_blocked = True
                else:
                    rec["solution_text"] = ((rec.get("solution_text") or "") + " " + frag).strip()
        orph_prov = f"ORPHAN_{str(orph.get('pass') or '?')}"
        # patch-only recovery (run-7 hardening #2): a fragment's question/
        # option content may ONLY merge when the fragment came from a Q-pass.
        # An S-pass/OCR solution fragment carrying stray question/option text
        # is blocked (cross-field contamination class), never merged.
        can_fill_question = orph.get("pass") in ("Q", None)
        if item.get("options") and can_fill_question:
            rec["options"] = rec["options"] or {}
            for k, v in item["options"].items():
                rec["options"].setdefault(str(k).strip().upper(), v)
            rec["_prov"]["options"] = orph_prov
        if item.get("question_text") and not rec.get("question_text"):
            if not can_fill_question:
                stats.setdefault("contaminated_stems_blocked", 0)
                stats["contaminated_stems_blocked"] += 1
                print(f"  [WARN] [ORPHAN] blocked {orph_prov} fragment from "
                      f"filling q{owner}'s stem (patch-only recovery) -- kept "
                      f"for review")
                remaining.append({**orph, "blocked_reason":
                                  f"{orph_prov} fragment carried question_text "
                                  f"(cross-field contamination) -- blocked"})
            else:
                stem_reason = _stem_reject_reason(item["question_text"], rec)
                if stem_reason:
                    stats.setdefault("contaminated_stems_blocked", 0)
                    stats["contaminated_stems_blocked"] += 1
                    print(f"  [WARN] [ORPHAN] blocked contaminated stem for q{owner} "
                          f"({stem_reason}) -- kept for review")
                    remaining.append({**orph, "blocked_reason":
                                      f"contaminated stem: {stem_reason}"})
                else:
                    rec["question_text"] = item["question_text"]
                    rec["_prov"]["question_text"] = orph_prov
        if item.get("correct_option") and not rec.get("correct_option"):
            rec["correct_option"] = str(item["correct_option"]).strip().upper()
            rec["_prov"]["correct_option"] = orph_prov
        if item.get("tables"):
            have = {t.get("markdown") for t in rec["tables"]}
            for t in item["tables"]:
                if t.get("markdown") not in have:
                    rec["tables"].append(t)
                    have.add(t.get("markdown"))
        qid = f"{subject}-{chapter_no:03d}-{owner:03d}"
        merged_something = bool(
            item.get("options") or item.get("question_text") or item.get("correct_option")
            or item.get("tables") or (item.get("solution_text") and not blocked_sol and not sol_blocked))
        if merged_something:
            note = (" (+ a foreign solution fragment was blocked, kept aside)"
                    if (blocked_sol or sol_blocked) else "")
            print(f"  [ORPHAN] Recovered orphan: page={page} assigned_to={qid} reason={reason}{note}")
            stats["orphans_recovered"] += 1
            if "carry-forward" in reason:
                stats["carry_merges"] = stats.get("carry_merges", 0) + 1
        elif blocked_sol:
            print(f"  [ORPHAN] owner q{owner} identified but the fragment added nothing new "
                  f"(foreign head) -- review the kept orphan entry")
    return remaining

IMAGE_ATTRIBUTION_PROMPT = """This image was extracted from one page of a medical MCQ chapter.

The chapter's questions are listed below (q_no: first words of stem):
{Q_LIST}

Look at the image and decide: does it BELONG to one of these questions
(a figure, diagram, chart, table, or clinical image that the question or
its solution refers to)?

Return ONE JSON object only:
{"q_no": <int>|null, "slot": "question"|"solution"|null, "decorative": true|false}
- q_no: the question this image belongs to. null if it belongs to none.
- slot: "question" if the figure appears with/above the stem, "solution" if
  it appears in the explanation region. null if q_no is null.
- decorative: true ONLY if you are confident this is decoration/unrelated to
  any question (portrait, logo, ornament, watermark, cover art, chapter icon).
Never guess a number. When unsure between decorative and a weak match,
prefer {"q_no": null, "decorative": true}.
"""


def attribute_orphan_image(model, rel_path, chapter_records, state):
    """FINAL safety net (Gap-2): one image, one call, one verdict. Never
    grouped -- a single image per call removes cross-image confusion.
    Returns (verdict_dict | None). Quota-brake-safe: returns
    {"decorative": "brake"} when the daily limit is hit so the caller can
    stop and persist instead of guessing."""
    reset_daily_counter_if_needed(state)
    if quota_exhausted(state):
        print("  [IMG] daily call limit reached during image attribution -- leftovers stay queued")
        return {"decorative": "brake"}
    q_list = "\n".join(
        f"q{qn}: {(chapter_records[qn].get('question_text') or '')[:80]}"
        for qn in sorted(chapter_records)
    ) or "(no question text available)"
    prompt = IMAGE_ATTRIBUTION_PROMPT.replace("{Q_LIST}", q_list)
    img_file = ASSETS_DIR / "questions" / rel_path
    if not img_file.exists():
        # STALE-PATH GUARD (run-11 RC-1): the image was already renamed to a
        # final slot by an earlier claim (figure-map / positional) but a stale
        # temp reference reached the 4th pass. Treat it as already-owned --
        # NOT as an unmatched image. The caller logs this skip.
        print(f"  [IMG] attribution skipped for {rel_path}: file already "
              f"relocated (claimed by an earlier pass)")
        return {"decorative": "already_claimed"}
    # PACE THIS CALL (run-5 evidence): attribute_orphan_image calls
    # generate_content DIRECTLY, bypassing the 5s pacing every other path
    # enforces -- a page with 3 leftover images fired 3 calls in ~1.5s, and
    # a multi-page chapter fired several in the SAME microsecond (log:
    # 14:08:48.0293 x3). That burst is what pushes the free tier past its
    # 15 RPM window and triggers the 429s. Every Gemini call must be paced.
    _pace_gemini_call()
    try:
        resp = model.generate_content(
            [prompt, Image.open(img_file)],
            safety_settings=SAFETY_SETTINGS,
            request_options={"retry": None},
        )
        note_call(state)
        save_state(state)
        if not resp.candidates:
            return None
        text = resp.text.strip()
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
        return json.loads(text)
    except Exception as e:
        print(f"  [IMG] attribution call failed for {rel_path}: {e}")
        return None


# ============================================================
# STEP 4: merge partial results (a question's text might be on one
# page and its answer/solution on a later page) into final records
# ============================================================

def merge_question_records(existing, new_items, stats=None, fill_only=False,
                             known_chapter_qns=None, carry_q_nos=()):
    """existing: dict keyed by q_no -> record (in progress for current chapter).
    stats: optional dict updated with "duplicates_merged"/"conflicts" counters.
    fill_only: recovery mode -- never overwrite a field that already has
    content; only fill what's missing (heals old rows without risking
    re-extraction noise replacing good data).

    known_chapter_qns (run-20 upstream fix, 2026-08-08): the set of
    q_nos the chapter's text layer prints question-stem headers for
    (chapter_printed_question_qns), UNION the set the Q-pass already
    returned in this chapter (chapter_records.keys()). When this is
    non-empty, an incoming item whose q_no is NEITHER in this set NOR
    in carry_q_nos is treated as a foreign-chapter q_no and DROPPED to
    the skipped list (the caller appends it to the chapter's orphans
    with reason="foreign_chapter_qno"). This is the upstream fix for
    the PSY-007 Q23-Q26 phantom-record bug: the S-pass returns
    "Solution to Question 23:" fragments from the previous chapter's
    solution tail that lives inside PSY-007's page range, but Q23 is
    not in PSY-007's question set, so accepting it would create a
    phantom record whose real question lives in a different chapter.

    carry_q_nos (run-20): the q_nos currently open in carry-forward
    state from a previous window. An incoming item whose q_no is in
    carry_q_nos is allowed even if it is not in known_chapter_qns
    (legitimate cross-page continuation of a real question the
    previous window was mid-solution on).

    Overlap-merge rules (sliding window re-extracts shared pages by design):
    - same q_no + question text similarity >= 95%  -> genuine re-extraction:
      merge fields (solutions/tables/options/images), count as duplicate.
    - same q_no but VERY different text AND a different answer key -> almost
      certainly a numbering collision: keep the first record, drop the item.
    Returns (existing, skipped): items with a missing/invalid q_no are NOT
    merged (never invent a number -- handoff rule #3) but ARE returned to the
    caller for orphan recovery (see ROOT_CAUSE_ANALYSIS.md RC-2)."""
    if stats is None:
        stats = {"duplicates_merged": 0, "conflicts": 0,
                 "foreign_chapter_qno_dropped": 0}
    skipped = []
    carry_set = set(carry_q_nos or ())
    # Build the allowed set: existing chapter_records (everything already
    # merged in earlier windows) + the known_chapter_qns from the text layer.
    # If known_chapter_qns is empty (scanned-only PDF, text layer garbled)
    # we fall back to existing.keys() -- which is the pre-fix behavior, so
    # no regression for books the text layer can't read.
    if known_chapter_qns is None:
        allowed_qns = set(existing.keys())
    else:
        allowed_qns = set(existing.keys()) | set(known_chapter_qns)
    for item in new_items:
        raw_qn = item.get("q_no")
        if raw_qn is None:
            print(f"  [WARN] Gemini returned an item with no q_no, skipping: {str(item)[:200]}")
            skipped.append(item)
            continue
        try:
            qn = int(raw_qn)  # Gemini's JSON sometimes returns q_no as a
                               # string ("7") and sometimes as a number (7);
                               # force a consistent type so later sorting
                               # never compares int against str.
        except (TypeError, ValueError):
            print(f"  [WARN] Gemini returned a non-numeric q_no ({raw_qn!r}), skipping")
            skipped.append(item)
            continue
        # ---- FOREIGN-CHAPTER Q_NO GUARD (run-20 upstream fix, 2026-08-08):
        # the S-pass on PSY-007 pages 105-107 returned 9 items including
        # q_no=23..26 from cross-chapter solution headers; the merge
        # accepted them and created phantom chapter_records[23..26] whose
        # real questions are in a different chapter. This guard drops such
        # items AT the merge step (not at the output step) so the master
        # chapter_records dict the build_final_question loop consumes
        # never carries them. Bypassed for legitimate carry-forward
        # continuations (carry_q_nos) and for chapters where the text
        # layer is unreadable (known_chapter_qns is None: we trust
        # chapter_records.keys() instead, which is the pre-fix path).
        if known_chapter_qns is not None and qn not in allowed_qns and qn not in carry_set:
            print(f"  [FOREIGN] q{qn}: not in known chapter q_nos "
                  f"(text layer / Q-pass anchors) and not in carry "
                  f"-- dropped at merge step (phantom record prevented); "
                  f"caller will route to orphans with reason='foreign_chapter_qno'")
            stats["foreign_chapter_qno_dropped"] = stats.get(
                "foreign_chapter_qno_dropped", 0) + 1
            # Tag the dropped item with a _drop_reason so the caller's
            # RC-2 salvage buffer (and downstream export gate) can
            # distinguish a FOREIGN drop -- expected, not data loss,
            # already correctly routed to unresolved_qids.jsonl by the
            # split layer -- from a real orphan that genuinely lost its
            # owner (data loss, gate violation). Without this marker
            # the gate's `orphan_unresolved` check fired on every
            # foreign-dropped item as if it were missing content (the
            # 4 [ORPHAN] page=100 lines in the post-fix Railway run).
            # The dict() copies the item so the original is not mutated.
            skipped.append({**item, "_drop_reason": "foreign_chapter_qno"})
            continue
        # ---- PROVENANCE + PATCH-ONLY RECOVERY (run-7 hardening #1/#2/#4):
        # every item carries _prov (set by the pass that produced it, e.g.
        # "Q_PASS", "S_PASS", "A_RETRY", "OCR_S", "RECOVER"). A SOLUTION or
        # ANSWER recovery may ONLY patch solution/answer fields -- its
        # question_text/options are dropped here so a recovered solution
        # fragment can NEVER populate a stem (the cross-field contamination
        # class the audit found).
        prov = str(item.get("_prov") or "GEMINI")
        if prov.startswith("S") or prov.startswith("A"):
            if item.get("question_text") or item.get("options"):
                print(f"  [PROV] q{qn}: {prov} item carried question/option "
                      f"content -- dropped (patch-only recovery; a {prov} "
                      f"fragment must never fill a stem)")
                item = {**item, "question_text": None, "options": None}

        # Enforce the solution schema before any overlap merge.  A retry or
        # normal pass may put markdown tables in prose; route them to tables
        # so the final record never carries the same table twice.
        if item.get("solution_text"):
            clean_sol, item_tables = _normalize_solution_payload(
                item.get("solution_text"), item.get("tables") or [], qn)
            item = {**item, "solution_text": clean_sol, "tables": item_tables}

        rec = existing.setdefault(qn, {
            "q_no": qn, "question_text": None, "options": None,
            "correct_option": None, "solution_text": None, "tables": [],
            "has_figure_in_question": False, "has_figure_in_solution": False,
            "_prov": {},   # per-field provenance (run-7 hardening #4)
        })
        if "_prov" not in rec:
            rec["_prov"] = {}

        # ---- semantic stem guard (run-7 hardening #3/#6): a would-be stem
        # that OPENS with explanation language, or whose text is substantially
        # contained in this record's OWN solution, is not a stem -- reject the
        # field so the record stays stem-missing and becomes retry-eligible
        # (Gap-1 anchor: the solution names its question). Valid stems are
        # never touched.
        stem_reason = _stem_reject_reason(item.get("question_text"), rec)
        if stem_reason:
            stats.setdefault("contaminated_stems_rejected", 0)
            stats["contaminated_stems_rejected"] += 1
            _append_jsonl(DATA_DIR / "integrity_flags.jsonl",
                          {"kind": "contaminated_stem_rejected", "q_no": qn,
                           "chapter_id": stats.get("chapter_id"),
                           "detail": stem_reason,
                           "prov": prov,
                           "text": str(item["question_text"])[:300]})
            print(f"  [WARN] q{qn}: rejected contaminated stem ({stem_reason}; "
                  f"prov={prov}) -- field kept empty for retry")
            item = {**item, "question_text": None}
        # ---- duplicate / conflict classification for overlap pages ----
        old_q, new_q = rec.get("question_text"), item.get("question_text")
        if old_q and new_q:
            sim = difflib.SequenceMatcher(None, old_q, new_q).ratio()
            if sim >= 0.95:
                stats["duplicates_merged"] += 1   # expected overlap re-read
            else:
                a1, a2 = rec.get("correct_option"), item.get("correct_option")
                # normalize case BEFORE comparing: 'D' vs 'd' is the same
                # answer, but the raw string compare called it a conflict and
                # dropped the item (run-2 log 17:36:00, ch15 q1).
                a1 = str(a1).strip().upper() if a1 else None
                a2 = str(a2).strip().upper() if a2 else None
                if a1 and a2 and a1 != a2:
                    stats["conflicts"] += 1   # count only ACTUAL drops (matches "conflicts dropped" log label)
                    print(f"  [WARN] conflicting re-extraction for q{qn} "
                          f"(similarity {sim:.2f}, answers {a1} vs {a2}) -- keeping first, dropping item")
                    continue
                # STEM CONFLICT (run-4: PSY-012-001 silently got PSY-012-013's
                # stem, similarity 0.25 in the log, wrong stem won by write
                # order). A stem coheres with its OWN options+solution; pick
                # the variant that matches the record's payload. When the
                # scores can't decide, keep the first and log BOTH variants to
                # data/stem_conflicts.jsonl -- never silently guess again.
                # fill_only (recovery) mode never overwrites an existing stem:
                # the existing row keeps its text, only the ledger note is written.
                if fill_only:
                    # RUN-13: recovery mode never overwrites an existing stem
                    # -- EXCEPT a quarantined suspect stem: a passing retry
                    # candidate replaces it and clears the quarantine (ch26
                    # q1 class: sweep-quarantined real stem, retry candidate
                    # arrived, fill-only kept the suspect -> shipped empty).
                    if rec.get("_stem_suspect_reason"):
                        rec["question_text"] = new_q
                        rec["_stem_suspect_reason"] = None
                        rec["_prov"]["question_text"] = prov
                        print(f"  [STEM] q{qn}: quarantined suspect stem "
                              f"replaced by retry candidate (fill_only)")
                        item = {**item, "question_text": None}
                    else:
                        stats["stem_conflicts"] = stats.get("stem_conflicts", 0) + 1
                        _append_jsonl(DATA_DIR / "stem_conflicts.jsonl", {
                            "q_no": qn, "chapter_id": stats.get("chapter_id"),
                            "similarity": round(sim, 3), "verdict": "fill-only kept-existing",
                            "old_stem": old_q[:600], "new_stem": new_q[:600]})
                else:
                    # RUN-12 STEM-CONTAMINATION PROTECTION: the coherence
                    # resolver must NEVER let solution-prose win. A
                    # contaminated "stem" IS the record's own solution text,
                    # so its payload coherence is artificially HIGH -- the
                    # resolver would pick "kept-new" and replace a GOOD stem
                    # with the solution, after which the sweep strips it and
                    # retry dead-ends (the run-12 recurring class). Check
                    # both variants for contamination FIRST; a contaminated
                    # variant can never be chosen over a clean one.
                    new_contam = _stem_reject_reason(new_q, rec)
                    old_contam = _stem_reject_reason(old_q, rec)
                    if new_contam and not old_contam:
                        keep, verdict = old_q, "kept-old (new variant contaminated)"
                    elif old_contam and not new_contam:
                        keep, verdict = new_q, "kept-new (old variant contaminated)"
                    else:
                        co, cn = _stem_payload_coherence(old_q, rec), _stem_payload_coherence(new_q, rec)
                        old_interrog = bool(re.search(r"\?\s*$", old_q or ""))
                        new_interrog = bool(re.search(r"\?\s*$", new_q or ""))
                        if old_interrog != new_interrog and min(co, cn) > 0:
                            # run-18: a genuinely interrogative stem beats a
                            # declarative one even when raw coherence favors
                            # the declarative side (PSY-006 q1 class -- see
                            # rationale above the call site).
                            keep, verdict = (old_q, "kept-old (new not interrogative)") \
                                if old_interrog else (new_q, "kept-new (old not interrogative)")
                        elif abs(co - cn) >= STEM_COHERENCE_MARGIN and max(co, cn) > 0:
                            keep, verdict = (old_q, "kept-old") if co > cn else (new_q, "kept-new")
                        else:
                            keep, verdict = old_q, "kept-old (undecidable -- review logged)"
                    stats["stem_conflicts"] = stats.get("stem_conflicts", 0) + 1
                    _append_jsonl(DATA_DIR / "stem_conflicts.jsonl", {
                        "q_no": qn, "chapter_id": stats.get("chapter_id"),
                        "similarity": round(sim, 3),
                        "coherence_old": round(co, 3), "coherence_new": round(cn, 3),
                        "verdict": verdict, "old_stem": old_q[:600], "new_stem": new_q[:600]})
                    print(f"  [WARN] stem conflict for q{qn} (similarity {sim:.2f}, "
                          f"coherence {co:.2f} vs {cn:.2f}) -- {verdict}; "
                          f"both variants logged to stem_conflicts.jsonl")
                    rec["question_text"] = keep
                item = {**item, "question_text": None}  # block the generic loop below
        for k in ["question_text", "solution_text"]:
            if not item.get(k):
                continue
            if k == "question_text":
                # RUN-12: never fill or overwrite a stem with contaminated
                # solution prose (the run-12 dead-end source). A contaminated
                # incoming stem is dropped regardless of fill_only; an
                # existing valid stem is never replaced by one.
                if _stem_reject_reason(item[k], rec):
                    stats.setdefault("contaminated_stems_blocked", 0)
                    stats["contaminated_stems_blocked"] += 1
                    print(f"  [WARN] q{qn}: dropped contaminated stem at merge "
                          f"({_stem_reject_reason(item[k], rec)}) -- existing "
                          f"stem kept; field stays retry-eligible")
                    item = {**item, "question_text": None}
                    continue
                if fill_only and rec.get(k) and not rec.get("_stem_suspect_reason"):
                    continue  # recovery: never overwrite existing content
                             # (EXCEPT a quarantined suspect stem -- run-13:
                             # a passing candidate replaces it and clears the
                             # quarantine, so a wrongly-swept real stem is
                             # healed instead of shipping empty)
                rec[k] = item[k]
                rec["_stem_suspect_reason"] = None   # a passing stem clears quarantine
                rec["_prov"][k] = prov   # provenance of every patched field
            else:
                if fill_only and rec.get(k):
                    continue  # recovery: never overwrite existing content
                rec[k] = item[k]
                rec["_prov"][k] = prov   # provenance of every patched field

        # Options can arrive across TWO different batches when a question
        # straddles a page break (e.g. options A/B on one page, C/D on the
        # next). Merge by option letter instead of overwriting the whole
        # dict, or the earlier batch's options get silently discarded.
        # Also normalize every option letter to uppercase here, since Gemini
        # (and the source PDF itself) mixes "a)" and "A." lettering -- if we
        # don't normalize once, centrally, correct_options ("D") will fail
        # to match options[].id ("d") later and the answer will look wrong
        # in the app even though the data is technically all there.
        if item.get("options"):
            if rec["options"] is None:
                rec["options"] = {}
            for opt_id, opt_text in item["options"].items():
                key = str(opt_id).strip().upper()
                if fill_only:
                    rec["options"].setdefault(key, opt_text)
                else:
                    rec["options"][key] = opt_text
            rec["_prov"]["options"] = prov

        if item.get("correct_option"):
            if not (fill_only and rec.get("correct_option")):
                rec["correct_option"] = str(item["correct_option"]).strip().upper()
                rec["_prov"]["correct_option"] = prov

        if item.get("tables"):
            # Overlap captures can be byte-identical OR a shorter prefix when
            # the first batch ends mid-table. Keep the fullest table, not the
            # first table merely because it arrived first.
            rec["tables"] = _dedupe_tables(list(rec.get("tables") or []) +
                                            list(item.get("tables") or []))
        rec["has_figure_in_question"] = rec["has_figure_in_question"] or item.get("has_figure_in_question", False)
        rec["has_figure_in_solution"] = rec["has_figure_in_solution"] or item.get("has_figure_in_solution", False)
    return existing, skipped

def sanitize_solution_text(text, own_qn=None):
    """Strip print furniture that leaks into solution text (run-4 audit):
      1. leading verbatim book headers  ("Solution to Question 2:") that the
         model carried over (PSY-032-001/002 shipped with them on).
      2. an EMBEDDED later "Solution to Question N:" header whose tail is a
         duplicate of what already precedes it -- the model dumped the whole
         recitation block into one question (PSY-032-003 carried its own
         solution twice plus Q4/Q5 inline). Non-duplicate tails (possibly the
         neighbor's real content) are LEFT intact and reported, never cut.
    Returns (cleaned_text, notes)."""
    notes = []
    s = text or ""
    if not s.strip():
        return s, notes
    while True:
        m = re.match(r"\s*Solution\s+to\s+Question\s+\d{1,3}\s*[:.\-]?\s*", s, re.IGNORECASE)
        if not m:
            break
        s = s[m.end():]
        notes.append("stripped leading 'Solution to Question N' header")
    m = SOLUTION_TO_Q_RE.search(s)
    if m and m.start() > 0:
        head = s[:m.start()].rstrip()
        tail = s[m.end():].lstrip(" :\n")
        # The precise dump proof: the chunk IMMEDIATELY after the header
        # restates THIS solution's own earlier content (the model re-recited
        # this question before dumping its neighbours). Neighbour content
        # further down the tail is never judged -- only the first line.
        tail_first = tail.split("\n", 1)[0][:150]
        if head and tail_first and _frag_mostly_present(tail_first, head, 0.8):
            s = head
            notes.append(f"truncated duplicated 'Solution to Question {m.group(1)}' dump")
        else:
            notes.append(f"embedded 'Solution to Question {m.group(1)}' header kept "
                         f"(tail not a duplicate -- needs model/review)")
    return s, notes


def _is_printed_answer_key(t):
    """The book's printed Answer Key grid is never solution content --
    when the model inline-reads the answers page it rides into solutions
    (zip-8: 39 stray-key flags across 18 chapters). Strip at the source
    so future books never carry them; fix_output.py P10 heals old files."""
    ty = str(t.get("type") or "").strip().lower().replace("_", " ")
    md = (t.get("markdown") or "")
    head = md.lstrip().splitlines()[0] if md.strip() else ""
    return ty == "answer key" or ("Question No." in head and "Correct Option" in head)


def _anchorless_record(rec):
    """True when a record carries NO usable content: no stem, no options and
    no solution (ch24 q12/13 class -- phantom rows born from an answer-key
    table spanning chapters, or fully-lost fragments). Such rows are dropped
    at build time with a ledger entry; the chapter's question count reflects
    only real records."""
    return not ((rec.get("question_text") or "").strip()
                or (rec.get("options") or {})
                or (rec.get("solution_text") or "").strip())


def build_final_question(subject, chapter_id, chapter_no, q_no, rec, image_files,
                         source_pages=None):
    qid = f"{subject}-{chapter_no:03d}-{q_no:03d}"

    def valid_images(imgs, kind):
        out = []
        for f in imgs:
            if not IMG_PATH_RE.match(f):
                print(f"  [WARN] Dropping malformed {kind} image path for {qid}: {f}")
                continue
            p = ASSETS_DIR / "questions" / f
            if not p.exists():
                print(f"  [WARN] Dropping missing {kind} image ref for {qid}: {f}")
                continue
            size = p.stat().st_size
            if size < MIN_IMAGE_BYTES:
                print(f"  [WARN] Dropping suspicious-tiny ({size}B) {kind} image ref for "
                      f"{qid}: {f} -- broken-crop guard (never ship a broken figure)")
                continue
            out.append({"type": "figure", "file": f})
        return out

    q_images = valid_images(image_files.get("question", []), "question")
    sol_images = valid_images(image_files.get("solution", []), "solution")
    sol_text, sanitize_notes = sanitize_solution_text(rec.get("solution_text"), own_qn=q_no)
    for note in sanitize_notes:
        print(f"  [SANITIZE] {qid}: {note}")
    tables = [{"type": t.get("type", "table"), "markdown": t["markdown"], "file": None}
              for t in _dedupe_tables(rec.get("tables", []))
              if not _is_printed_answer_key(t)]

    # OPTION-LEVEL images (run-10): image_files["option"] = {letter: [rel,...]}
    # populated by the deterministic option geometry; the per-option "images"
    # array already existed in the schema (hardcoded [] before) -- now filled.
    opt_imgs = image_files.get("option") or {}
    option_rows = [{"id": str(k).strip().upper(), "text": v,
                    "images": valid_images(opt_imgs.get(str(k).strip().upper(), []), "option")}
                   for k, v in (rec["options"] or {}).items()]
    # Last-resort release backfill: targeted retry above requests all options
    # when one is blank. If OCR/model extraction still leaves the *correct*
    # option blank, preserve usability with the solution's opening sentence
    # and make the repair conspicuous for validator/manual review.
    correct_id = str(rec.get("correct_option") or "").strip().upper()
    for opt in option_rows:
        if opt["id"] == correct_id and not str(opt.get("text") or "").strip():
            first = re.split(r"(?<=[.!?])\s+", sol_text.strip(), maxsplit=1)[0].strip()
            if first:
                opt["text"] = first
                print(f"  [OPTION_BACKFILLED] {qid}: correct option {correct_id} reconstructed from solution opening")
    # Correct clearly mislabelled "Option X:" explanation lines only when the
    # description overlaps another option at least twice as strongly.
    opt_text = {o["id"]: str(o.get("text") or "") for o in option_rows}
    def relabel(m):
        label, desc = m.group(1).upper(), m.group(2)
        words = {w for w in re.findall(r"\w+", desc.lower()) if len(w) > 2}
        scores = {k: len(words & set(re.findall(r"\w+", v.lower()))) for k, v in opt_text.items() if v}
        best = max(scores, key=scores.get) if scores else label
        if best != label and scores.get(best, 0) >= 2 * max(1, scores.get(label, 0)):
            print(f"  [LABEL_CORRECTED] {qid}: Option {label} -> Option {best}")
            return f"Option {best}: {desc}"
        return m.group(0)
    sol_text = re.sub(r"(?m)Option\s+([A-D])\s*:\s*([^\n]+)", relabel, sol_text)

    return {
        "id": qid,
        "subject": subject,
        "chapter_id": chapter_id,
        "question": {"text": rec["question_text"], "images": q_images},
        "options": option_rows,
        "correct_options": [rec["correct_option"]] if rec["correct_option"] else [],
        "solution": {"text": sol_text, "images": sol_images, "tables": tables},
        "tags": [],
        # run-19: which PDF page(s) this question was extracted from -- lets
        # a downstream reviewer (human or the critique pass below) jump
        # straight to source without re-deriving it from the page ledger.
        "source_pages": sorted(source_pages) if source_pages else [],
        # run-13: quarantined suspect stem marker -- ships in questions.jsonl
        # so the post-run validator flags it too (not only the export gate).
        "stem_suspect": rec.get("_stem_suspect_reason"),
    }


# ============================================================
# MAIN DRIVER
# ============================================================

def _mat_mult(m1, m2):
    """2D affine composition CTM' = M1 x M2 (PDF row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def image_positions_on_page(pdf_path, file_page):
    """Best-effort map {image object idnum -> (y, x, draw_index, w, h)} for
    every image XObject drawn on a page, by walking the content stream and
    tracking the cm matrix before each `Do` -- RECURSING INTO Form XObjects
    (run-13: figures are often drawn inside a Form / clip / mask wrapper, and
    the old flat walk silently skipped those images, leaving them with no
    position and therefore no geometry owner). (x, y) = the image's
    BOTTOM-LEFT corner in PDF user space (origin at the page bottom-left),
    so LARGER y == HIGHER on the page and LARGER x == further RIGHT; (w, h)
    = drawn size in user-space points (from the cm scale). x/y feed the
    block and option-image geometry; w/h feed the bbox overlay of the
    full-page-vision pass (run-13). Returns {} on any parse hiccup -- callers
    then fall back to plain reading order / the render-based passes."""
    positions = {}
    try:
        page = PdfReader(pdf_path).pages[file_page - 1]
        root_names = {str(name): ref for name, ref in _page_xobjects(page).items()}
        contents = page.get_contents()
        if contents is None:
            return {}
        streams = []
        if isinstance(contents, (list, tuple)):
            for c in contents:
                d = c.get_data() if hasattr(c, "get_data") else None
                if d:
                    streams.append(d)
        else:
            d = contents.get_data() if hasattr(contents, "get_data") else None
            if d:
                streams.append(d)
        if not streams:
            return {}
        import zlib
        state = {"draw_idx": 0, "visited_forms": set()}

        def _decompress(data):
            try:
                return zlib.decompress(data)
            except Exception:
                return data

        def _walk(data, names):
            tokens = re.findall(rb"/[^\s\[\]()<>{}/%]+|\([^)]*\)|\[[^\]]*\]|"
                                rb"[-+]?\d*\.?\d+|[A-Za-z'\"]+", _decompress(data))
            ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            stack = []
            num_buf = []
            i = 0
            while i < len(tokens):
                t = tokens[i]
                if t == b"q":
                    stack.append(ctm)
                elif t == b"Q":
                    ctm = stack.pop() if stack else ctm
                elif t == b"cm" and len(num_buf) >= 6:
                    m = tuple(float(x) for x in num_buf[-6:])
                    ctm = _mat_mult(m, ctm)
                    num_buf = []
                elif t == b"Do" and num_buf:
                    name = num_buf[-1].decode("latin-1")
                    if name in names:
                        obj = _resolve(names[name])
                        sub = obj.get("/Subtype")
                        if sub == "/Image":
                            oid = getattr(names[name], "idnum", None)
                            key = oid if oid is not None else name
                            positions[key] = (ctm[5], ctm[4], state["draw_idx"],
                                              ctm[0], ctm[3])
                            state["draw_idx"] += 1
                        elif sub == "/Form":
                            # recurse with the Form's OWN resources; the form
                            # content runs at the current CTM, so pass it down
                            fid = getattr(names[name], "idnum", None)
                            if fid in state["visited_forms"]:
                                pass  # cycle guard: don't re-walk
                            else:
                                if fid is not None:
                                    state["visited_forms"].add(fid)
                                fdata = obj.get_data() if hasattr(obj, "get_data") else None
                                if fdata:
                                    fres = _resolve(obj.get("/Resources"))
                                    fnames = {}
                                    if fres:
                                        fxobjs = _resolve(fres.get("/XObject"))
                                        if fxobjs:
                                            fnames = {str(n): r for n, r in fxobjs.items()}
                                    _walk(fdata, fnames)
                    num_buf = []
                i += 1
                if t not in (b"q", b"Q", b"cm", b"Do"):
                    if t.startswith(b"/") or re.fullmatch(rb"[-+]?\d*\.?\d+", t):
                        num_buf.append(t)
                    else:
                        num_buf = []
        for data in streams:
            _walk(data, root_names)
        return positions
    except Exception:
        return {}


def pending_image_slots(chapter_records, image_files_by_q):
    """Chapter's needy image slots in APPEARANCE order: q_no ascending, and
    within one question its question-side figure precedes its solution-side
    figure (that's the physical reading order in MCQ books)."""
    slots = []
    for qn in sorted(chapter_records):
        rec = chapter_records[qn]
        entry = image_files_by_q.setdefault(qn, {"question": [], "solution": []})
        if rec.get("has_figure_in_question") and not entry["question"]:
            slots.append((qn, "question"))
        if rec.get("has_figure_in_solution") and not entry["solution"]:
            slots.append((qn, "solution"))
    return slots


def _rename_for_slot(rel, qn, kind, subject, chapter_no, image_files_by_q,
                     option_letter=None):
    """Rename one extracted temp image into the locked convention for the
    given (q_no, "question"|"solution"|"option") slot. kind letters: Q, SOL,
    or OPT_{L} (option_letter A-D). Returns the new rel path or None."""
    old_path = ASSETS_DIR / "questions" / rel
    if not old_path.exists():
        print(f"  [WARN] {rel} missing at rename time -- skipping (alias/dup ref)")
        return None
    qid = f"{subject}-{chapter_no:03d}-{qn:03d}"
    entry = image_files_by_q.setdefault(qn, {"question": [], "solution": []})
    entry.setdefault("option", {})
    # Broken-crop guard (run-4: PSY-003-014_Q_01 was 414 bytes): a sub-1.5KB
    # webp cannot hold a real MCQ figure. Do NOT auto-claim it -- the caller's
    # leftover path hands it to the model fourth-pass / manual review, which
    # decides on ACTUAL content instead of position.
    size = old_path.stat().st_size
    if size < MIN_IMAGE_BYTES:
        print(f"  [WARN] {rel} is only {size}B (< {MIN_IMAGE_BYTES}) -- refusing auto-claim "
              f"(broken-crop guard); left for model/manual review")
        return None
    # Over-attribution guard (run-4: PSY-022-003 collected 7 question-side
    # images through repeated model-confirmed passes -- every pass was
    # individually reasonable, the SUM was nonsense). One question in this
    # book never legitimately cites >3 figures.
    if kind == "question" and len(entry["question"]) >= MAX_QUESTION_IMAGES:
        print(f"  [WARN] over-attribution guard: {qid} already has {MAX_QUESTION_IMAGES} "
              f"question images -- refusing {rel}; left for model/manual review")
        return None
    # Same guard on the solution side (user report: a 7-figure solutions page
    # collapsed into 2 solutions because a single decoded header swallowed
    # every image under it). A solution block legitimately cites a figure or
    # two; beyond that the deterministic matcher is stacking neighbours'
    # figures -- refuse and let the model/manual pass decide on content.
    if kind == "solution" and len(entry["solution"]) >= MAX_SOLUTION_IMAGES:
        print(f"  [WARN] over-attribution guard: {qid} already has {MAX_SOLUTION_IMAGES} "
              f"solution images -- refusing {rel}; left for model/manual review")
        return None
    if kind == "option":
        opt = str(option_letter or "A").strip().upper()
        bucket = entry["option"].setdefault(opt, [])
        idx = len(bucket) + 1
        new_name = f"{qid}_OPT_{opt}_{idx:02d}.webp"
    else:
        letter = "Q" if kind == "question" else "SOL"
        idx = len(entry[kind]) + 1
        new_name = f"{qid}_{letter}_{idx:02d}.webp"
    new_rel = f"{subject}/{new_name}"
    old_path.rename(ASSETS_DIR / "questions" / subject / new_name)
    return new_rel


def claim_page_images_one_to_one(imgs, pdf_path, file_page, subject, chapter_no,
                                 chapter_records, image_files_by_q):
    """Gap-2 core matcher: distribute one page's N extracted images across
    the chapter's needy slots ONE-TO-ONE, in reading order: images sorted
    top->bottom by their drawn y-position (positions parsed from the PDF
    content stream; falls back to resource order), slots in appearance order
    (pending_image_slots). Returns the list of files STILL unclaimed.
    With 0 or 1 needy slot, degenerates to the old greedy behavior (all
    page images go to that one slot) -- which is correct for a page whose
    images all belong to a single question."""
    # Never distribute page images across chapter-wide "pending" slots by
    # reading order alone. That heuristic silently mapped diagrams to the
    # wrong questions whenever a page had several nearby questions. Auto-claim
    # only with deterministic page evidence: exactly one printed q_no on this
    # page and exactly one matching needy slot. Everything else is retained
    # for the later explicit attribution/manual-review path.
    slots = pending_image_slots(chapter_records, image_files_by_q)
    if not slots:
        return list(imgs)
    try:
        printed = qns_printed_on_page(pdf_path, file_page, chapter_records)
    except Exception:
        printed = []
    if len(printed) != 1:
        print(f"  [IMG] page {file_page}: ambiguous printed owners {printed or '-'}; "
              "not auto-attaching image(s)")
        return list(imgs)
    candidates = [(qn, kind) for qn, kind in slots if qn == printed[0]]
    if len(candidates) != 1:
        print(f"  [IMG] page {file_page}: q{printed[0]} has {len(candidates)} eligible image slots; "
              "not auto-attaching image(s)")
        return list(imgs)
    if len(candidates) == 1:
        qn, kind = candidates[0]
        entry = image_files_by_q.setdefault(qn, {"question": [], "solution": []})
        leftover = []
        for rel in imgs:
            # append IMMEDIATELY after each rename: _rename_for_slot derives
            # the _01/_02/... suffix from len(entry[kind]), so deferring the
            # append would hand the same filename to every image on this
            # page and silently overwrite them (caught by tests).
            new_rel = _rename_for_slot(rel, qn, kind, subject, chapter_no, image_files_by_q)
            if new_rel:
                entry[kind].append(new_rel)
            else:
                leftover.append(rel)
        return leftover
    # N images, M>=2 slots: position-ordered one-to-one
    pos = image_positions_on_page(pdf_path, file_page)
    ordered_imgs = _order_imgs_by_position(imgs, pos)
    leftover = []
    for i, rel in enumerate(ordered_imgs):
        if i >= len(slots):
            leftover.append(rel)
            continue
        qn, kind = slots[i]
        new_rel = _rename_for_slot(rel, qn, kind, subject, chapter_no, image_files_by_q)
        if new_rel:
            image_files_by_q.setdefault(qn, {"question": [], "solution": []})[kind].append(new_rel)
            qid = f"{subject}-{chapter_no:03d}-{qn:03d}"
            print(f"  [IMG] one-to-one: {rel} -> {qid} ({kind} slot #{i + 1})")
        else:
            leftover.append(rel)
    return leftover


def claim_block_images(imgs, pdf_path, file_page, subject, chapter_no,
                       chapter_records, image_files_by_q, active_block=None):
    """GEOMETRY-FIRST deterministic owner for figures inside question OR
    solution blocks (run-9, generalized from the solution-only mapper).

    Every image belongs to the block it is DRAWN UNDER: the closest
    "question-stem heading" OR "Solution to Question N:" header whose
    baseline sits above the image's bottom edge. Images and headers are
    matched by real PDF y positions (same coordinate space as
    image_positions_on_page), never by count or a single text-layer hit.

    This is the page-4 class fix: the old QUESTION-side path needed the
    (garbled) pdftotext CLI + Gemini's has_figure flag, so page 4's figure
    fell to a single Gemini "decorative" verdict and was discarded. Here the
    SAME deterministic positional system that maps solution figures (page 33
    -> PSY-002-014) is extended to question-side figures.

    active_block: (q_no, kind) for the block still open from the PREVIOUS
    window (cross-page carry, run-9 priority C). An image with NO heading
    above it on this page (the block started on the previous page) is
    assigned to the carried block instead of being left unclaimed.

    Priority implemented (run-9 #5): A/B. strong same-page block ownership
    (closest heading above) -> C. cross-page carry (active_block) -> else
    unclaimed (never guessed). Gemini never overrides this: it runs only on
    the leftovers via claim_figure_map_images / the 4th pass.

    Safety (each returns the image unclaimed rather than guessing):
      * no locatable header AND no active_block -> claim NOTHING;
      * image position unparsable -> claim NOTHING;
      * MAX_QUESTION_IMAGES / MAX_SOLUTION_IMAGES per owner (enforced in
        _rename_for_slot): extras flow to the model/manual passes.
    Returns the files STILL unclaimed."""
    headers = block_headers_on_page(pdf_path, file_page, chapter_records)
    pos = image_positions_on_page(pdf_path, file_page)
    if (not headers and active_block is None) or not pos:
        # No block evidence (or unparsable positions) -> claim NOTHING by
        # geometry; the leftovers flow to the one-to-one matcher and the
        # model/manual passes (the same safe path as before header binding).
        return list(imgs)
    leftover = []
    for rel in imgs:
        try:
            oid = int(Path(rel).stem.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            leftover.append(rel)
            continue
        info = pos.get(oid)
        if info is None:
            leftover.append(rel)
            continue
        y_img, x_img, _didx, _w, _h = info
        # Closest header drawn ABOVE the image: iterate bottom-first, take
        # the first hit (the topmost header above would hand every figure on
        # the page to the first block).
        owner = next(((kind, qn) for kind, qn, y_hdr in reversed(headers)
                      if y_hdr > y_img), None)
        if owner is None:
            if active_block is not None:
                owner = active_block          # cross-page carry (priority C)
            else:
                leftover.append(rel)          # no block starts above -> unclaimed
                continue
        kind, qn = owner
        if qn not in chapter_records:
            leftover.append(rel)
            continue
        # OPTION-LEVEL ownership (run-10): inside a QUESTION block, an image
        # that geometrically belongs to an option label row is assigned to
        # THAT option (deterministic; Gemini never overrides). Only computed
        # for question-kind owners; solution blocks are never option-scanned.
        slot, opt_letter = kind, None
        if kind == "question":
            opt_letter = _option_for_image_in_block(
                pdf_path, file_page, headers, owner, x_img, y_img)
            if opt_letter is not None:
                slot = "option"
        new_rel = _rename_for_slot(rel, qn, slot, subject, chapter_no,
                                   image_files_by_q, option_letter=opt_letter)
        if new_rel:
            entry = image_files_by_q.setdefault(qn, {"question": [], "solution": []})
            entry.setdefault("option", {})
            if slot == "option":
                entry["option"].setdefault(opt_letter, []).append(new_rel)
            else:
                entry[slot].append(new_rel)
            qid = f"{subject}-{chapter_no:03d}-{qn:03d}"
            src = "active-block carry" if (owner is active_block) else "block position"
            if slot == "option":
                print(f"  [IMG] page {file_page}: {src} -> {rel} -> {qid} option {opt_letter}")
            else:
                print(f"  [IMG] page {file_page}: {src} -> {rel} -> {qid} ({kind})")
        else:
            leftover.append(rel)
    return leftover


def _order_imgs_by_position(imgs, pos):
    """Sort extracted image rel paths top->bottom by their drawn y-position
    (PDF content stream), falling back to resource order when positions are
    unparsable. Both the figure-map pass and the one-to-one matcher rely on
    this ordering matching Gemini's top-to-bottom reading order."""
    def order_key(rel):
        try:
            oid = int(Path(rel).stem.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            oid = None
        y, _x, didx, _w, _h = pos.get(oid, (None, None, 10**6, 0, 0))
        return (-(y if y is not None else float("-inf")), didx)
    return sorted(imgs, key=order_key)


# ======================================================================
# run-13 UNIFIED IMAGE-OWNERSHIP ARCHITECTURE (full-page context levels)
# ======================================================================
# The page-4 class: a REAL question-side figure (PSY-p4-7 -> Q1) was not
# attached. The run-9 "geometry-first" system reads block headings from the
# PDF TEXT LAYER; on this book's QUESTION pages the body-font text layer is
# garbled/absent (broken ToUnicode), so question_headers_on_page finds no
# headings, one_to_one's pdftotext probe also finds none ("ambiguous printed
# owners -"), and the figure falls to an ISOLATED-crop Gemini call that
# cannot see any printed anchor -- it guessed "decorative" for a real figure.
# The synthetic tests passed because their PDFs use clean Helvetica text.
#
# The ownership ladder below is the unified design. Each level sees ONLY the
# leftovers of the previous level, and every level records provenance to
# data/image_ownership.jsonl:
#   L1 deterministic text-layer geometry (run-9, closest heading above)
#   L2 deterministic OCR-anchored geometry (NEW -- tesseract on the RENDERED
#      page, immune to text-layer garble, zero Gemini calls; tesseract ships
#      in the prod Docker image)
#   L3 full-page vision (NEW -- rendered page with each leftover's drawn bbox
#      highlighted + labeled; Gemini decides ONLY from printed layout
#      anchors, one call per page with all leftovers batched; adjacent pages
#      attached when a figure touches a page edge)
#   L4 unresolved_images.jsonl (conservative -- never discard on a single
#      model verdict; the export gate now flags these per chapter)
# Never let a lower level override a higher one: each level only receives
# what the previous one left unclaimed.

# run-16 BOUNDED-MEMORY: a full-page RGB render at 150 dpi (letter) is
# ~6.3 MB. The old cache was a plain dict that NEVER evicted, so every page
# rendered by Q-activation OCR, L2 OCR geometry, L3 full-page vision and its
# context pages accumulated forever -- ~150 renders by chapter 11 of a 33-
# chapter book (~950 MB) blew the Railway container's memory and the kernel
# sent the worker SIGKILL ("Perhaps out of memory?" -- it WAS OOM). The
# cache is now a bounded LRU; callers also clear it at chapter end.
_RENDER_CACHE_MAX = 10          # ~65 MB worst case at 150 dpi
_RENDER_CACHE = {}


def render_cache_size():
    return len(_RENDER_CACHE)


def clear_render_cache():
    """Drop every cached page render. Called at chapter end (pages of the
    previous chapter are never re-needed) so peak memory stays small even on
    a 300+ page book."""
    _RENDER_CACHE.clear()


def render_page_png(pdf_path, file_page, dpi=150):
    """(PIL.Image, scale_px_per_pt, page_height_pt) render of the page, or
    (None, 0, 0) when no renderer is available. Tries pdftoppm (poppler-utils,
    installed in the prod image) first, then PyMuPDF (self-contained).
    Bounded LRU cache per (pdf, page, dpi) -- never unbounded (run-16)."""
    key = (str(pdf_path), file_page, dpi)
    hit = _RENDER_CACHE.pop(key, None)      # LRU touch
    if hit is not None:
        _RENDER_CACHE[key] = hit
        return hit
    out = None
    tmpdir = None
    if shutil.which("pdftoppm"):
        try:
            tmpdir = tempfile.mkdtemp(prefix="qbank_render_")
            prefix = os.path.join(tmpdir, "page")
            subprocess.run(["pdftoppm", "-f", str(file_page), "-l", str(file_page),
                            "-r", str(dpi), "-png", "-singlefile",
                            str(pdf_path), prefix],
                           check=True, capture_output=True, timeout=180)
            png_path = prefix + ".png"
            if os.path.exists(png_path):
                out = Image.open(png_path).convert("RGB")
        except Exception as e:
            print(f"  [WARN] pdftoppm render failed for page {file_page}: {e}")
        finally:
            # run-16: the render PNG (~6 MB) was only needed to load the PIL
            # image -- remove the temp dir instead of leaking it per page.
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
    if out is None:
        try:
            import fitz  # PyMuPDF -- self-contained, no system deps
            doc = fitz.open(str(pdf_path))
            try:
                page = doc[file_page - 1]
                pix = page.get_pixmap(dpi=dpi)
                out = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            finally:
                # run-16: close the document immediately (native memory is
                # freed here, not whenever the refcount happens to drop)
                doc.close()
        except Exception as e:
            print(f"  [WARN] PyMuPDF render failed for page {file_page}: {e}")
    if out is None:
        _RENDER_CACHE[key] = (None, 0, 0)
        return _RENDER_CACHE[key]
    scale = dpi / 72.0
    page_h_pt = out.height / scale
    _RENDER_CACHE[key] = (out, scale, page_h_pt)
    while len(_RENDER_CACHE) > _RENDER_CACHE_MAX:
        _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)))   # evict oldest (LRU)
    return _RENDER_CACHE[key]


def _ocr_anchors_from_data(data, scale, img_h):
    """Shared word->line->anchor extraction for one tesseract image_to_data
    dict. Digits (question numbers) keep a relaxed confidence floor (30 vs
    40 for words): tesseract scores small standalone numbers lower, and a
    missed number = a lost block anchor on a page whose text layer is
    already garbled."""
    words = []
    for i, txt in enumerate(data.get("text", []) or []):
        t = (txt or "").strip()
        if not t:
            continue
        try:
            left = int(data["left"][i]); top = int(data["top"][i])
            hgt = int(data["height"][i])
            conf = float(data["conf"][i])
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        floor = 30 if re.fullmatch(r"\d{1,3}", t) else 40
        if conf < floor:
            continue
        words.append((left, top + hgt / 2.0, t))   # x_px, y_center_px
    if not words:
        return []
    # group words into visual lines by vertical center
    tol = max(6.0, scale * 6.0)   # ~half a line height in px at this dpi
    words.sort(key=lambda w: (w[1], w[0]))
    lines = []
    cur = []
    cur_y = None
    for x, yc, t in words:
        if cur and abs(yc - cur_y) > tol:
            lines.append((cur_y, sorted(cur)))
            cur = []
        cur.append((x, t))
        cur_y = yc if cur_y is None else (cur_y * 0.5 + yc * 0.5)
    if cur:
        lines.append((cur_y, sorted(cur)))
    anchors = []
    for _yc, wl in lines:
        line = " ".join(t for _x, t in wl)
        # run-18: accept "Question N:" / "N -" too, not just "N." / "N)" --
        # some books print colon or dash after the number, and the old
        # class silently matched ZERO headers on every page of those books,
        # which starved the run-13/run-14 Q-pass activation safety nets
        # below (they rely entirely on this function to prove question
        # content). A stray false-positive here just costs one redundant
        # Q-pass call on an already-covered page -- same safe-default
        # philosophy as probe_batch_pages ("never let a window-sizer
        # disable a pass"); a missed one silently drops real questions.
        m = re.match(r"^\s*(?:Q(?:uestion)?\s*[.:]?\s*)?(\d{1,3})\s*[.:\-–)]", line)
        if m:
            anchors.append(("question", int(m.group(1)),
                            (img_h - _yc) / scale))
            continue
        for sm in re.finditer(r"Solution\s+to\s+Question\s+(\d{1,3})", line, re.I):
            anchors.append(("solution", int(sm.group(1)),
                            (img_h - _yc) / scale))
    return sorted(set(anchors), key=lambda a: -a[2])


def ocr_page_anchors(png, scale, page_h_pt):
    """[(kind, q_no, y_pdf_pt)] block headings read by OCR from the RENDERED
    page pixels (tesseract). kind in {"question","solution"}; y_pdf_pt is the
    line's center converted back to PDF user space (origin bottom-left,
    LARGER y == HIGHER), the same space claim_block_images uses.
    Returns [] when the tesseract binary is unavailable or OCR yields nothing
    usable -- the caller then falls through to the vision level.

    run-13: tries several tesseract segmentation modes in order (psm 6
    uniform block -> psm 4 single column -> psm 11 sparse text) because
    scanned page layouts differ; a mode that yields no anchors is retried
    with the next. This is what the L2 OCR-geometry claim and the run-13
    Q-pass activation both rely on."""
    if not shutil.which("tesseract"):
        return []
    for cfg in ("--psm 6", "--psm 4", "--psm 11"):
        try:
            data = pytesseract.image_to_data(png, config=cfg,
                                             output_type=pytesseract.Output.DICT)
        except Exception:
            continue
        anchors = _ocr_anchors_from_data(data, scale, png.height)
        if anchors:
            return anchors
    return []


def page_has_question_content(pdf_path, page, chapter_records, dpi=150):
    """Per-page variant: does THIS rendered page print a question-stem heading
    above its first solution header (or no solution header at all)?
    Rendered-page OCR -- immune to the garbled body-font text layer. Returns
    False when OCR/render is unavailable (the caller decides the safe
    default).

    run-18: this used to also require the OCR-read q_no to already be a KEY
    in chapter_records. That is backwards for what this function exists to
    do -- it is the Q-pass ACTIVATION safety net, called precisely to prove
    a page has question content BEFORE any question on it has been
    extracted. chapter_records is empty on a chapter's first batch by
    definition, so the old check silently returned False on exactly the
    highest-risk window (ch2 pages 22-26 / ch7 pages 100-104 class: the
    section planner mislabels the chapter's very first pages "S", and this
    net -- the only thing that can override that -- could never fire there).
    A question-shaped OCR heading positioned above the page's own first
    solution header is sufficient deterministic evidence on its own; no
    prior extraction is required to trust it."""
    rendered = render_page_png(pdf_path, page, dpi=dpi)
    if not rendered[0]:
        return False
    img, scale, _page_h = rendered
    anchors = ocr_page_anchors(img, scale, _page_h)
    if not anchors:
        return False
    sol_y = min((y for k, _q, y in anchors if k == "solution"), default=None)
    for k, qn, y in anchors:
        if k != "question":
            continue
        if sol_y is None or y > sol_y:
            return True
    return False


def window_has_question_content(pdf_path, pages, chapter_records, dpi=150):
    """Deterministic check used by Q-pass ACTIVATION (run-13 root cause: the
    section planner labels a whole chapter "S" when the text layer shows
    solution headers on its FIRST pages -- often the previous chapter's
    solution tail -- and _should_run_q_pass then skips the Q-pass for every
    S window, so question stems/options on those pages are only ever
    recovered by the fragile targeted retry, and tail questions ship
    missing. A window "has question content" when OCR of its RENDERED pages
    finds a printed question-stem heading (a q_no in chapter_records) ABOVE
    the page's first solution header (or on a page with no solution header
    at all) -- the same filter block_headers_on_page applies. Rendered-page
    OCR is immune to the garbled body-font text layer. Zero Gemini calls.
    Returns True when ANY of the pages shows question content."""
    for p in pages:
        if page_has_question_content(pdf_path, p, chapter_records, dpi=dpi):
            return True
    return False





def _record_image_ownership(subject, chapter_id, page, rel, qid, slot,
                            method, evidence, confidence="high"):
    """Provenance ledger for EVERY automatic image assignment: owner, slot,
    method (deterministic_geometry / deterministic_ocr_geometry /
    model_figure_map / deterministic_one_to_one / full_page_vision ...),
    evidence, confidence. Append-only; shipped in the export zip."""
    entry = {"subject": subject, "chapter_id": chapter_id, "page": page,
             "file": rel, "owner": qid, "slot": slot, "method": method,
             "evidence": str(evidence or "")[:240], "confidence": confidence,
             "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _append_jsonl(DATA_DIR / "image_ownership.jsonl", entry)


def claim_block_images_ocr(imgs, pdf_path, file_page, subject, chapter_no,
                           chapter_records, image_files_by_q, chapter_id=None,
                           active_block=None, dpi=150):
    """run-13 LEVEL 2 (deterministic OCR-anchored geometry): the SAME
    closest-heading-above rule as claim_block_images, but the block headings
    come from OCR of the RENDERED page instead of the (garbled/absent) PDF
    text layer. Deterministic, zero Gemini calls. Runs only on leftovers from
    L1. Returns the files STILL unclaimed (they flow to L3 vision)."""
    if not imgs:
        return []
    rendered = render_page_png(pdf_path, file_page, dpi=dpi)
    if not rendered[0]:
        return imgs
    img, scale, page_h_pt = rendered
    anchors = ocr_page_anchors(img, scale, page_h_pt)
    if not anchors:
        return imgs
    # identical filtering to block_headers_on_page: question headings below
    # the first solution header are solution-prose list items, not stems
    qs = [("question", qn, y) for k, qn, y in anchors if k == "question"]
    ss = [("solution", qn, y) for k, qn, y in anchors if k == "solution"]
    if ss:
        first_sol_y = min(y for _k, _q, y in ss)
        qs = [t for t in qs if t[2] > first_sol_y]
    headers = sorted(qs + ss, key=lambda t: -t[2])
    pos = image_positions_on_page(pdf_path, file_page)
    if not pos:
        return imgs
    leftover = []
    for rel in imgs:
        try:
            oid = int(Path(rel).stem.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            leftover.append(rel)
            continue
        info = pos.get(oid)
        if info is None:
            leftover.append(rel)
            continue
        y_img, x_img, _didx, _w, _h = info
        owner = next(((k, qn) for k, qn, y_hdr in reversed(headers)
                      if y_hdr > y_img), None)
        if owner is None:
            if active_block is not None:
                owner = active_block          # cross-page carry (priority C)
            else:
                leftover.append(rel)
                continue
        kind, qn = owner
        if qn not in chapter_records:
            leftover.append(rel)
            continue
        new_rel = _rename_for_slot(rel, qn, kind, subject, chapter_no,
                                   image_files_by_q)
        if new_rel:
            entry = image_files_by_q.setdefault(qn, {"question": [], "solution": []})
            entry[kind].append(new_rel)
            qid = f"{subject}-{chapter_no:03d}-{qn:03d}"
            print(f"  [IMG] page {file_page}: OCR block position -> {rel} -> {qid} ({kind})")
            _record_image_ownership(subject, chapter_id, file_page, rel, qid,
                                    kind, "deterministic_ocr_geometry",
                                    "closest OCR question/solution heading above image")
        else:
            leftover.append(rel)
    return leftover


FULL_PAGE_VISION_PROMPT = """A rendered page of an MCQ book is attached with some figures
highlighted and labeled (red box + label like IMG-1). Decide the OWNER of each labeled
figure using ONLY the printed page layout you can SEE in the render:
- the printed question number ("1." / "1)" / "Q1.") drawn directly above the figure,
- option letters (A. B. C. D.) drawn next to the figure,
- "Solution to Question N:" headers,
- the figure's position relative to those anchors.
Do NOT infer ownership from the medical content of the figure itself.
Valid question numbers: {Q_RANGE}.
Return ONE JSON object mapping each label to:
{{"q_no": <int|null>, "slot": "question"|"solution"|"option"|null,
  "option": "A"|"B"|"C"|"D"|null, "confidence": "high"|"medium"|"low"|null,
  "evidence": "<one short sentence quoting the printed anchor used>"}}
- q_no null = the figure has no printed owner on the shown page(s).
- slot "option" only when the figure sits ON a printed option line (A./B./C./D.).
- "low" confidence or missing evidence = UNRESOLVED (return null rather than guess)."""


def full_page_vision_ownership(model, pdf_path, file_page, rels, positions,
                               subject, chapter_no, chapter_records,
                               chapter_id, state, image_files_by_q,
                               dpi=150, edge_tol_pt=36.0):
    """run-13 LEVEL 3 (full-page vision): ONE Gemini call per page; every
    leftover image on the page is highlighted on the RENDERED page with a
    labeled bbox, and the model answers ONLY from printed layout anchors --
    never from the figure's medical content (the failure mode of the old
    isolated-crop 4th pass). Adjacent pages are rendered and attached as
    context when a figure touches the page top/bottom edge (cross-page
    blocks). Returns (claimed, still_unclaimed, verdicts)."""
    claimed, still, verdicts = [], [], {}
    if not rels:
        return claimed, still, verdicts
    render = render_page_png(pdf_path, file_page, dpi=dpi)
    if not render[0]:
        # run-13: never silently skip -- the isolated fallback that runs next
        # mislabels real figures "decorative" without page context (the
        # page-4 class). Log loudly so the audit trail shows WHY.
        print(f"  [IMG] full-page vision SKIPPED for page {file_page}: page "
              f"render unavailable ({len(rels)} leftover(s)) -- isolated fallback runs")
        return claimed, list(rels), verdicts
    page_img, scale, page_h_pt = render
    # run-16: NEVER mutate the cached render (the red highlight boxes used to
    # be drawn onto the cached PIL object -- a later render of the same page
    # returned an already-highlighted image, and the mutated copy stayed in
    # memory). Draw on a private copy; the cache keeps the clean page.
    page_img = page_img.copy()
    labels = {}
    missing_pos = []
    for i, rel in enumerate(rels):
        try:
            oid = int(Path(rel).stem.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            oid = None
        info = positions.get(oid) if oid is not None else None
        if info is None:
            missing_pos.append(rel)
            still.append(rel)
            continue
        labels[f"IMG-{i + 1}"] = (rel, oid, info)
    if not labels:
        # run-13: no parsed drawn bbox for ANY leftover (Form-wrapped /
        # unusual content stream, e.g. the p104 class) -- say so explicitly
        # instead of failing silently into the isolated-crop fallback.
        print(f"  [IMG] full-page vision SKIPPED for page {file_page}: NO parsed "
              f"image positions for {len(rels)} leftover(s) ({rels}) -- isolated "
              f"fallback runs")
        return claimed, still, verdicts
    if missing_pos:
        print(f"  [IMG] full-page vision page {file_page}: positions missing for "
              f"{missing_pos} -- those fall to the isolated pass")
    draw = ImageDraw.Draw(page_img)
    font = None
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(10, int(14 * scale / 2)))
    except Exception:
        pass
    for label, (_rel, _oid, info) in labels.items():
        y_pt, x_pt, _didx, w_pt, h_pt = info
        x0 = int(x_pt * scale); y0 = int((page_h_pt - (y_pt + h_pt)) * scale)
        x1 = int((x_pt + w_pt) * scale); y1 = int((page_h_pt - y_pt) * scale)
        lw = max(3, int(scale / 8))
        draw.rectangle([x0, y0, x1, y1], outline="red", width=lw)
        if font:
            tw = max(10, int(len(label) * 9 * scale / 2))
            draw.rectangle([x0, max(0, y0 - int(22 * scale / 2)),
                            x0 + tw, y0], fill="red")
            draw.text((x0 + 4, max(0, y0 - int(18 * scale / 2))),
                      label, fill="white", font=font)
    # cross-page context (LEVEL 3b): a figure touching the top/bottom edge
    # may belong to a block that started/continues on the adjacent page
    min_y = min(info[0] for _r, _o, info in labels.values())
    max_top = max(info[0] + info[3] for _r, _o, info in labels.values())
    context_imgs = []
    if min_y < edge_tol_pt and file_page > 1:
        ctx = render_page_png(pdf_path, file_page - 1, dpi=dpi)
        if ctx[0]:
            context_imgs.append((file_page - 1, ctx[0]))
    if max_top > page_h_pt - edge_tol_pt:
        ctx = render_page_png(pdf_path, file_page + 1, dpi=dpi)
        if ctx[0]:
            context_imgs.append((file_page + 1, ctx[0]))
    q_min, q_max = (min(chapter_records), max(chapter_records)) if chapter_records else (0, 0)
    prompt = FULL_PAGE_VISION_PROMPT.replace(
        "{Q_RANGE}", f"{q_min}-{q_max} (chapter {subject}-{chapter_no:03d})")
    parts = [prompt, page_img]
    if context_imgs:
        parts.append("Adjacent context page(s) attached (NOT highlighted) -- use them "
                     "only to locate the block a highlighted figure continues from.")
        for _pn, cimg in context_imgs:
            parts.append(cimg)
    _pace_gemini_call()
    try:
        resp = model.generate_content(parts, safety_settings=SAFETY_SETTINGS,
                                      request_options={"retry": None})
        note_call(state)
        save_state(state)
        if not getattr(resp, "candidates", None):
            return claimed, [r for r, _o, _i in labels.values()] + still, verdicts
        text = (resp.text or "").strip()
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
        parsed = json.loads(text) if text else {}
    except Exception as e:
        print(f"  [IMG] full-page vision call failed for page {file_page}: {e}")
        return claimed, [r for r, _o, _i in labels.values()] + still, verdicts
    for label, (rel, _oid, _info) in labels.items():
        v = parsed.get(label) if isinstance(parsed, dict) else None
        if not isinstance(v, dict):
            still.append(rel)
            continue
        verdicts[rel] = v
        try:
            qn = int(v.get("q_no"))
        except (TypeError, ValueError):
            still.append(rel)
            continue
        if qn not in chapter_records:
            still.append(rel)
            continue
        slot = v.get("slot")
        conf = str(v.get("confidence") or "").lower()
        if slot not in ("question", "solution", "option") or conf not in ("high", "medium"):
            still.append(rel)
            continue
        opt = None
        if slot == "option":
            opt = str(v.get("option") or "").strip().upper()
            if opt not in ("A", "B", "C", "D"):
                still.append(rel)
                continue
        new_rel = _rename_for_slot(rel, qn, slot, subject, chapter_no,
                                   image_files_by_q, option_letter=opt)
        if not new_rel:
            still.append(rel)
            continue
        entry = image_files_by_q.setdefault(qn, {"question": [], "solution": []})
        entry.setdefault("option", {})
        if slot == "option":
            entry["option"].setdefault(opt, []).append(new_rel)
        else:
            entry[slot].append(new_rel)
        qid = f"{subject}-{chapter_no:03d}-{qn:03d}"
        evidence = str(v.get("evidence") or "")[:200]
        print(f"  [IMG] full-page vision: page {file_page} {rel} -> {qid} ({slot}) [{conf}]")
        _record_image_ownership(subject, chapter_id, file_page, rel, qid, slot,
                                "full_page_vision", evidence, conf)
        claimed.append((rel, qid, slot))
    return claimed, still, verdicts


def claim_figure_map_images(fig_map, window_rows, subject, chapter_no,
                            chapter_records, image_files_by_q):
    """Run-6 user ask ("bta ye image kis question ki h"): claim every image
    of a window using Gemini's OWN _figure_map (one {q_no, slot} entry per
    figure, in top-to-bottom reading order page by page, returned by the
    extraction prompt).

    window_rows: [(file_page_num, [rel paths in top-to-bottom order]), ...]
    with pages in window order.

    EXACT-COUNT GUARD: the map only fires when len(fig_map) == the total
    number of images extracted for the window -- then the alignment is exact
    because both lists are top-to-bottom, page by page. ANY mismatch
    (watermark skipped, tiny crop dropped by the guard, model double-counted
    or missed a figure) skips the whole pass safely; those images stay for
    the deterministic positional passes and the 4th-pass model attribution.
    Returns {page_no: [rels still unclaimed]}."""
    if not fig_map:
        return {p: rels for p, rels in window_rows}
    total_imgs = sum(len(rels) for _, rels in window_rows)
    if total_imgs != len(fig_map):
        print(f"  [IMG] figure-map count mismatch ({len(fig_map)} declared vs "
              f"{total_imgs} extracted) -- skipping model figure-map claim; "
              f"left for positional/model passes")
        return {p: rels for p, rels in window_rows}
    remaining = {}
    it = iter(fig_map)
    for page_no, rels in window_rows:
        still = []
        for rel in rels:
            entry = next(it, None)
            if not entry:
                still.append(rel)
                continue
            try:
                qn = int(entry.get("q_no"))
            except (TypeError, ValueError):
                still.append(rel)
                continue
            if qn not in chapter_records:
                still.append(rel)
                continue
            slot = entry.get("slot")
            if slot not in ("question", "solution"):
                still.append(rel)
                continue
            new_rel = _rename_for_slot(rel, qn, slot, subject, chapter_no,
                                       image_files_by_q)
            if new_rel:
                image_files_by_q.setdefault(qn, {"question": [], "solution": []})[slot].append(new_rel)
                qid = f"{subject}-{chapter_no:03d}-{qn:03d}"
                print(f"  [IMG] figure-map: {rel} -> {qid} ({slot} side, model-declared)")
            else:
                still.append(rel)   # tiny-crop / over-attribution guard refused
        if still:
            remaining[page_no] = still
    return remaining


def claim_page_images(imgs, pdf_path, file_page, subject, chapter_no,
                      chapter_records, image_files_by_q, active_block=None):
    """Deterministic claimer for one page's images (run-9 geometry-first):

      1. block-header mapping -- every image drawn under a question-stem
         heading OR a "Solution to Question N:" header goes to THAT block's
         owner (closest header above the image; cross-page carry via
         active_block). This is the SAME deterministic positional system that
         maps solution figures, now extended to question-side figures (the
         page-4 class fix).
      2. leftovers fall to the one-to-one matcher (exactly one printed q_no +
         exactly one needy slot, else nothing is claimed).

    Gemini never overrides a deterministic assignment: it runs only on the
    leftovers (claim_figure_map_images in the window loop, and the 4th pass
    at chapter end -- which is now conservative, see _record_unresolved_image).

    Returns the files STILL unclaimed (they reach the second pass / model
    attribution / manual review)."""
    leftover = claim_block_images(imgs, pdf_path, file_page, subject,
                                  chapter_no, chapter_records, image_files_by_q,
                                  active_block=active_block)
    if leftover:
        leftover = claim_page_images_one_to_one(leftover, pdf_path, file_page,
                                                subject, chapter_no, chapter_records,
                                                image_files_by_q)
    return leftover


def _active_block_from_carries(carry_by_pass):
    """(kind, q_no) of the block still OPEN from the previous window, or None.
    Kind derives from the pass + cut_part: an S-pass carry (or a Q-pass carry
    whose cut was "solution"/"unknown") means the active block is a SOLUTION
    block; a Q-pass "question"/"options" cut means the active block is a
    QUESTION block. The (kind, q_no) tuple order matches block_headers_on_page
    header tuples so claim_block_images can treat it identically. Used as the
    cross-page carry owner (run-9 priority C) -- an image at the top of a new
    page with no heading above it belongs to the block that started on the
    previous page."""
    for p in ("Q", "S"):
        c = carry_by_pass.get(p) or {}
        qn = c.get("last_open_question")
        if qn is None:
            continue
        if p == "S" or c.get("cut_part") in ("solution", "unknown"):
            return ("solution", qn)
        return ("question", qn)   # Q-pass cut at question/options
    return None


def _append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---- run-11 PAGE/PASS LEDGER + STRUCTURED STATUS (user points A/B/K) -----
# A page/pass must never be "done" merely because a later attempt returned
# some records. Each window-pass attempt is classified into one of:
#   SUCCESS            -- call returned items for its pass's section
#   EXPECTED_EMPTY     -- call OK, 0 items, and the window's section is not
#                         this pass's section (legitimately nothing to do)
#   PARTIAL            -- call OK but the window's section is this pass's
#                         section and 0 items came back (possible FAILED_ZERO)
#   RETRYABLE_FAILURE  -- API error; page-by-page retry recovered some/all
#   UNRESOLVED         -- every retry ladder failed; the pass obligation for
#                         these pages is NOT met -> chapter-end loud failure
PASS_STATUS_SUCCESS = "SUCCESS"
PASS_STATUS_EXPECTED_EMPTY = "EXPECTED_EMPTY"
PASS_STATUS_PARTIAL = "PARTIAL"
PASS_STATUS_RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
PASS_STATUS_UNRESOLVED = "UNRESOLVED"

# which sections each pass legitimately produces items in
_PASS_SECTION_OK = {"Q": "Q", "A": "A", "S": "S"}


def _should_run_q_pass(section, has_question_overlap, q_carry, solutions_section_seen):
    """RUN-12: whether the Q-pass should run on this window. On a window the
    section planner labeled "S" (all pages >= solutions_start), Q-pass runs
    ONLY when the window carries question-section overlap pages (the boundary
    tail) or a live Q carry (a question genuinely continuing into the
    solutions). Running Q-pass over the whole solution section was the
    upstream source of the run-12 contaminated stems (Q read solution prose
    as question_text) and the resulting retry dead-end."""
    if section == "S" and not has_question_overlap and not q_carry:
        return False
    return not solutions_section_seen


def _classify_pass_status(pass_name, section, n_items, had_error, recovered_all):
    """Structured extraction status (run-11 point B): a zero-item pass is
    EXPECTED_EMPTY when the window's section is NOT the pass's section;
    it is PARTIAL (possible FAILED_ZERO) when the section matches and 0 items
    returned. An error that the retry ladder fully recovered is
    RETRYABLE_FAILURE; anything still failing after the ladder is UNRESOLVED."""
    if had_error:
        return PASS_STATUS_RETRYABLE_FAILURE if recovered_all else PASS_STATUS_UNRESOLVED
    if n_items == 0 and section is not None and section != _PASS_SECTION_OK.get(pass_name):
        return PASS_STATUS_EXPECTED_EMPTY
    if n_items == 0 and section is not None and section == _PASS_SECTION_OK.get(pass_name):
        return PASS_STATUS_PARTIAL
    return PASS_STATUS_SUCCESS


def _ledger_pass(chapter_id, subject, chapter_no, pass_name, window_pages,
                 status, n_items, note=""):
    row = {"chapter_id": chapter_id, "subject": subject,
           "chapter_no": chapter_no, "pass": pass_name,
           "pages": sorted(window_pages), "status": status,
           "items": n_items, "note": note,
           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    _append_jsonl(DATA_DIR / "page_ledger.jsonl", row)
    return row


# ============================================================
# run-19: CRITIQUE-AND-REPAIR PASS (Sachin's idea, isolated on purpose)
# ============================================================
# Toggle: flip to False to remove this entire step with zero effect on the
# rest of the pipeline -- it only reads export-gate violations and, at most,
# patches specific flagged fields on specific records after they're already
# fully merged. Nothing upstream depends on it running.
ENABLE_CRITIQUE_PASS = True

# how many *distinct questions* this pass will spend a Gemini call on, per
# chapter -- a bound, not a target, so one badly-flagged chapter can't blow
# the day's quota on repeated re-checks.
CRITIQUE_MAX_QUESTIONS_PER_CHAPTER = 15

CRITIQUE_PROMPT = """You are reviewing ONE previously-extracted MCQ against its
source textbook page(s). An earlier extraction pass flagged possible
problems with it. Look ONLY at the page image(s) provided.

CURRENT EXTRACTED RECORD (question {q_no}):
{current_json}

FLAGGED PROBLEM(S): {problems}

Compare the current record against what is actually printed on the page(s).
Return ONE JSON object, nothing else:
{{
  "verdict": "confirmed" | "corrected" | "cannot_verify",
  "question_text": "..." | null,
  "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}} | null,
  "correct_option": "A"|"B"|"C"|"D" | null,
  "solution_text": "..." | null,
  "note": "one short sentence explaining the verdict"
}}

Rules:
- "confirmed": the current record is actually correct despite the flag (the
  flag was a false alarm) -- return the SAME values back, unchanged.
- "corrected": you can see the true content on the page and the current
  record is genuinely wrong or incomplete -- return the CORRECTED values.
  Only fields you actually verified against the page should differ from the
  current record; leave any field you did not need to touch exactly as it
  was in the current record.
- "cannot_verify": the page(s) provided don't show enough of this question
  to confirm or correct it (e.g. it continues on a page not shown, or the
  relevant text is illegible/obscured) -- return the current record's values
  unchanged and say so in "note". Do NOT guess.
- Preserve wording verbatim from the page -- do not paraphrase.
- Never invent an option or answer that is not actually printed."""


def _critique_prompt_for(q_no, rec, problems):
    current = {
        "question_text": rec.get("question_text"),
        "options": rec.get("options"),
        "correct_option": rec.get("correct_option"),
        "solution_text": (rec.get("solution_text") or "")[:1500],
    }
    return CRITIQUE_PROMPT.format(
        q_no=q_no,
        current_json=json.dumps(current, ensure_ascii=False, indent=2),
        problems="; ".join(problems),
    )


def critique_and_repair_chapter(chapter_id, chapter_records, violations,
                                qn_source_pages, pdf_path, genai_model,
                                dpi=150):
    """run-19: for each question the deterministic export gate flagged,
    show Gemini ONLY that question's source page(s) + its current extracted
    JSON, and ask it to confirm or correct -- patch-only (a field the model
    didn't need to touch is never overwritten, and a field it returns
    unchanged is a no-op). Returns (n_confirmed, n_corrected, n_unverifiable,
    n_skipped_no_page). Never raises -- a single question's critique failing
    (API error, bad JSON back, etc.) is logged and skipped, the rest of the
    chapter proceeds normally, and the export gate result already on disk
    stands as the honest record of what happened.

    Deliberately narrow: only touches question_text/options/correct_option/
    solution_text, and only for q_nos that actually appear in `violations`.
    It cannot invent a fix for something it can't see (cannot_verify), and
    it never adds a question that wasn't already a merged record."""
    if not ENABLE_CRITIQUE_PASS or not violations or genai_model is None:
        return 0, 0, 0, 0
    flagged = {}
    for kind, qn, detail in violations:
        if qn is None or qn not in chapter_records:
            continue
        flagged.setdefault(qn, []).append(f"{kind}: {detail}")
    if not flagged:
        return 0, 0, 0, 0
    qns = sorted(flagged)[:CRITIQUE_MAX_QUESTIONS_PER_CHAPTER]
    if len(flagged) > len(qns):
        print(f"  [CRITIQUE] {chapter_id}: {len(flagged)} flagged questions, "
              f"reviewing first {len(qns)} (CRITIQUE_MAX_QUESTIONS_PER_CHAPTER "
              f"bound) -- rest stand as export-gate violations, unchanged")
    n_confirmed = n_corrected = n_unverifiable = n_skipped = 0
    for qn in qns:
        pages = sorted(qn_source_pages.get(qn) or [])
        if not pages:
            print(f"  [CRITIQUE] q{qn}: no known source page on record -- "
                  f"skipping (can't show Gemini a page)")
            n_skipped += 1
            continue
        pages = pages[:3]   # a question never legitimately spans more than this
        tmpdir = tempfile.mkdtemp(prefix="qbank_critique_")
        try:
            img_paths = []
            for p in pages:
                img, _scale, _ph = render_page_png(pdf_path, p, dpi=dpi)
                if img is None:
                    continue
                fp = os.path.join(tmpdir, f"critique-{p}.png")
                img.save(fp)
                img_paths.append(fp)
            if not img_paths:
                print(f"  [CRITIQUE] q{qn}: could not render source page(s) "
                      f"{pages} -- skipping")
                n_skipped += 1
                continue
            rec = chapter_records[qn]
            prompt = _critique_prompt_for(qn, rec, flagged[qn])
            try:
                result = call_gemini_on_pages(genai_model, img_paths, prompt=prompt)
            except Exception as exc:
                print(f"  [CRITIQUE] q{qn}: Gemini call failed ({exc}) -- "
                      f"leaving record as-is")
                n_skipped += 1
                continue
            # call_gemini_on_pages always returns a list (array-parsed) --
            # a single-object critique response may come back as a 1-item
            # list or a bare dict depending on the parser; handle both.
            if isinstance(result, list):
                result = result[0] if result else None
            if not isinstance(result, dict):
                print(f"  [CRITIQUE] q{qn}: unparseable response -- leaving "
                      f"record as-is")
                n_skipped += 1
                continue
            verdict = str(result.get("verdict") or "").strip().lower()
            if verdict == "confirmed":
                print(f"  [CRITIQUE] q{qn}: confirmed correct despite flag "
                      f"-- {result.get('note', '')}")
                n_confirmed += 1
                continue
            if verdict == "cannot_verify":
                print(f"  [CRITIQUE] q{qn}: cannot verify from available "
                      f"page(s) -- {result.get('note', '')}")
                n_unverifiable += 1
                continue
            if verdict != "corrected":
                print(f"  [CRITIQUE] q{qn}: unrecognized verdict "
                      f"{verdict!r} -- leaving record as-is")
                n_skipped += 1
                continue
            # patch-only: only overwrite a field if the model returned a
            # non-empty value that actually differs from the current one --
            # a flagged field the model left null/unchanged stays exactly
            # as the earlier passes left it (still gate-visible, not hidden).
            changed = []
            for field in ("question_text", "solution_text"):
                new_v = result.get(field)
                if new_v and str(new_v).strip() and new_v != rec.get(field):
                    rec[field] = new_v
                    changed.append(field)
            new_opts = result.get("options")
            if isinstance(new_opts, dict) and any(str(v or "").strip() for v in new_opts.values()):
                if new_opts != rec.get("options"):
                    rec["options"] = new_opts
                    changed.append("options")
            new_ans = result.get("correct_option")
            if new_ans and str(new_ans).strip().upper() in ("A", "B", "C", "D") \
                    and new_ans != rec.get("correct_option"):
                rec["correct_option"] = str(new_ans).strip().upper()
                changed.append("correct_option")
            if changed:
                print(f"  [CRITIQUE] q{qn}: corrected {changed} -- "
                      f"{result.get('note', '')}")
                n_corrected += 1
            else:
                print(f"  [CRITIQUE] q{qn}: verdict=corrected but nothing "
                      f"differed from the current record -- treating as "
                      f"confirmed")
                n_confirmed += 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return n_confirmed, n_corrected, n_unverifiable, n_skipped


def _export_gate_violations(chapter_records, image_files_by_q, unresolved_ledger,
                            chapter_id, unresolved_images=(), unresolved_orphans=()):
    """run-11 EXPORT GATE: returns a list of (kind, q_no, detail) violations
    that must be ZERO before a chapter export counts as clean. Deterministic
    checks only -- no Gemini. This is what makes 'missing answer = 0 / missing
    solution = 0' insufficient: stems, options, orphans, images, asset refs
    and unresolved pages are all gated too.

    run-13: an unresolved IMAGE is now a gate violation too. A chapter whose
    extracted figure never got a deterministic/vision owner is NOT clean --
    the old gate only inspected CLAIMED images (broken_asset_ref), so
    [GATE] CLEAN printed even while a source-verified Q1 figure sat in
    unresolved_images.jsonl. Exceptions (deterministically NOT a relevant
    figure, no human review needed): broken crops below MIN_IMAGE_BYTES and
    images excluded at extraction (watermark object id). A single Gemini
    "decorative" verdict is NOT an exception -- that verdict mislabeled a real
    figure in production and must not clear the gate."""
    violations = []
    for qn, rec in sorted(chapter_records.items()):
        if not (rec.get("question_text") or "").strip():
            violations.append(("missing_stem", qn,
                               "no question_text after batch+retry+rescue"))
        opts = rec.get("options") or {}
        if len(opts) < 4 or any(not str(v or "").strip() for v in opts.values()):
            violations.append(("bad_options", qn, f"options={sorted(opts)}"))
        if not rec.get("correct_option"):
            violations.append(("missing_answer", qn, "no correct_option"))
        if not (rec.get("solution_text") or "").strip():
            violations.append(("missing_solution", qn, "no solution_text"))
        if rec.get("_stem_suspect_reason"):
            # run-13: quarantined suspect stem (kept for review, not deleted)
            # is still a violation -- the chapter must not look clean while a
            # stem MAY be solution prose.
            violations.append(("suspect_stem", qn,
                               f"stem quarantined (kept for review): "
                               f"{rec['_stem_suspect_reason']}"))
    # every referenced asset file must exist on disk
    for qn, entry in (image_files_by_q or {}).items():
        for kind, paths in ({"question": entry.get("question", []),
                             "solution": entry.get("solution", [])}).items():
            for rel in paths:
                if not (ASSETS_DIR / "questions" / rel).exists():
                    violations.append(("broken_asset_ref", qn,
                                       f"{kind} image missing on disk: {rel}"))
        for letter, paths in (entry.get("option") or {}).items():
            for rel in paths:
                if not (ASSETS_DIR / "questions" / rel).exists():
                    violations.append(("broken_asset_ref", qn,
                                       f"option {letter} image missing: {rel}"))
    for l in unresolved_ledger:
        violations.append((f"unresolved_page_{l['pass']}", l["pages"],
                           l["status"]))
    for u in unresolved_images or ():
        if u.get("deterministic_junk"):
            continue   # broken crop (<MIN_IMAGE_BYTES) -- not a real figure
        violations.append(("unresolved_image", u.get("page"),
                           f"{u.get('file')} (method={u.get('method') or '?'} "
                           f"confidence={u.get('confidence') or '?'})"))
    # run-13 orphan accounting: a chapter with a MEANINGFUL unclaimed
    # q_no-less fragment must NOT print GATE CLEAN (ch11/17/33 printed
    # "orphans: N unresolved" next to "[GATE] ... CLEAN" -- the four systems
    # orphan summary / export gate / validator / ZIP disagreed). Empty or
    # junk fragments (everything None) are not data loss and stay silent.
    for o in unresolved_orphans or ():
        # RUN-20 (2026-08-08): skip FOREIGN-chapter q_no drops. These are
        # EXPECTED (cross-chapter "Solution to Question N:" headers that
        # fell into this chapter's page range, e.g. PSY-006's tail into
        # PSY-007's pages 100-107 returning q23..26). The upstream merge
        # correctly rejected them as foreign, the caller routes them
        # to orphans.jsonl for human review, and the split layer's
        # reconcile_qids writes them to unresolved_qids.jsonl with
        # reason="missing_question_for_solution". They are NOT data loss:
        # the export gate's `missing_question_for_solution` upstream
        # check (counted by the split layer) is the authoritative report
        # for this class. Including them again as `orphan_unresolved`
        # would double-count and inflate gate violation counts.
        # Other orphan classes (q_no is None, stem-rejected to empty,
        # solution fragment with no owner) remain flagged -- those are
        # genuine data loss.
        if o.get("drop_reason") == "foreign_chapter_qno":
            continue
        item = o.get("item") or {}
        present = [k for k in ("question_text", "options", "correct_option",
                               "solution_text", "tables")
                   if item.get(k)]
        if not present:
            continue
        pages = o.get("pdf_pages") or o.get("new_pages") or []
        violations.append(("orphan_unresolved", None,
                           f"meaningful q_no-less fragment on pages {pages} "
                           f"unclaimed (fields: {present})"))
    return violations


def drop_phantom_solution_only_records(chapter_records, chapter_id, stats,
                                       prior_rows=()):
    """RUN-14 PHANTOM SOLUTION-ONLY RECORDS (ch2 q25/26 class): a record whose
    ONLY content is solution_text -- stem/options/answer were never set by ANY
    pass (proven by per-field provenance) AND whose solution DUPLICATES a
    record already shipped in an earlier chapter (same q_no, >=50% solution
    similarity) is a cross-chapter solution spill: the PREVIOUS chapter's
    "Solution to Question N:" header landed inside this chapter's page range
    and the S-pass created a phantom question record whose real q_no already
    shipped in the previous chapter (ch1 q25/26: "Hysteria...", "Big five
    personality traits..."). Shipping it pollutes the app with an empty
    question and the gate triple-flags it forever. Returns the list of
    dropped q_nos; the full record is preserved in
    data/dropped_phantom_records.jsonl (shipped in the zip), so the solution
    text is never lost, just not emitted as a question here.

    Conservative by design:
      * only records whose solution came from S/DRAIN/OCR passes AND whose
        stem/options/answer have NO provenance are considered;
      * the cross-chapter duplicate check is REQUIRED -- a solution-only
        record with no prior duplicate (e.g. a real question whose stem was
        lost but whose solution was extracted) is KEPT and stays flagged by
        the gate (missing_stem etc.), never silently dropped."""
    dropped = []
    for qn, rec in sorted(chapter_records.items(), key=lambda x: x[0]):
        prov = rec.get("_prov") or {}
        has_question = (rec.get("question_text") or "").strip()
        has_options = bool(rec.get("options"))
        has_answer = bool(rec.get("correct_option"))
        has_solution = (rec.get("solution_text") or "").strip()
        sol_prov = str(prov.get("solution_text") or "")
        if has_question or has_options or has_answer or not has_solution:
            continue
        if not (sol_prov.startswith("S") or sol_prov.startswith("DRAIN")
                or sol_prov.startswith("OCR")):
            continue
        # cross-chapter duplicate proof: same q_no in an ALREADY-WRITTEN
        # chapter with a highly similar solution
        dup = False
        for row in prior_rows:
            try:
                rq = int(row.get("q_no"))
            except (TypeError, ValueError):
                continue
            if rq != qn:
                continue
            r_sol = (row.get("solution_text") or "").strip()
            if r_sol and _frag_mostly_present(has_solution, r_sol, 0.5):
                dup = True
                break
        if not dup:
            continue
        dropped.append(qn)
        _append_jsonl(DATA_DIR / "dropped_phantom_records.jsonl",
                      {"chapter_id": chapter_id, "q_no": qn,
                       "reason": "solution-only record duplicating an earlier "
                                 "chapter's q_no (cross-chapter solution spill); "
                                 "no stem/options/answer ever extracted",
                       "solution_prov": sol_prov,
                       "solution_text": str(has_solution)[:1200]})
    if dropped:
        stats["phantom_solution_dropped"] = stats.get("phantom_solution_dropped", 0) + len(dropped)
    return dropped


def _verdict_confidence(verdicts, rel):
    """The model's OWN confidence for one file, pulled off the verdict dict
    the vision passes already return (2026-08-11).

    _record_unresolved_image has always accepted confidence= but NO call site
    passed it, so every unresolved_images.jsonl row serialized
    "confidence": null and the export gate printed 'confidence=?'. The value
    was being computed and discarded. Returns None when the pass produced no
    verdict for this file (render unavailable / call failed / no position),
    which is a genuine unknown rather than a low score."""
    if not isinstance(verdicts, dict):
        return None
    v = verdicts.get(rel)
    if not isinstance(v, dict):
        return None
    conf = str(v.get("confidence") or "").strip().lower()
    return conf if conf in ("high", "medium", "low") else None


def _record_unresolved_image(subject, chapter_id, page, rel, reason,
                             model_verdict=None, method="unresolved",
                             confidence=None):
    """Run-9 CONSERVATIVE rule (run-13 provenance + junk marking): an
    extracted image with no deterministic owner must NOT be permanently
    discarded on a single Gemini "decorative" verdict (PSY-p4-7 was called
    decorative in one run yet belongs to Q1). It is RECORDED to
    data/unresolved_images.jsonl -- kept on disk under its temp name with the
    model verdict attached -- for human review. Only STRONG deterministic
    evidence (watermark object id, already excluded at extraction; a broken
    crop below MIN_IMAGE_BYTES, marked deterministic_junk) may permanently
    classify non-figure. The export gate flags every entry that is NOT
    deterministic_junk."""
    size = 0
    p = ASSETS_DIR / "questions" / rel
    try:
        size = p.stat().st_size if p.exists() else 0
    except OSError:
        size = 0
    entry = {"subject": subject, "chapter_id": chapter_id, "page": page,
             "file": rel, "reason": reason, "method": method,
             "confidence": confidence,
             "deterministic_junk": bool(size and size < MIN_IMAGE_BYTES),
             "size_bytes": size}
    if model_verdict is not None:
        entry["model_verdict"] = model_verdict
    _append_jsonl(DATA_DIR / "unresolved_images.jsonl", entry)
    return entry

def process_pdf(pdf_cfg, state, genai_model, chapters_out, questions_path,
                only_chapter_no=None):
    """only_chapter_no (v2 test hook): when set, every other chapter is
    skipped -- lets test_v2_chapter.py run the full 3-pass machinery on ONE
    chapter without touching the rest of the book."""
    subject = pdf_cfg["subject"]
    pdf_path = pdf_cfg["path"]
    progress = state["pdf_progress"].setdefault(subject, {"chapters_done": [], "current": None})

    watermark_id = find_watermark_object_id(pdf_path)
    print(f"[{subject}] watermark object id: {watermark_id}")

    total_pages = len(PdfReader(pdf_path).pages)
    toc = extract_toc_chapters(pdf_path)
    chapters = compute_page_ranges(toc, pdf_cfg["page_offset"], total_pages)

    for ch in chapters:
        chapter_id = f"{subject}-{ch['chapter_no']:03d}"
        if only_chapter_no is not None and ch["chapter_no"] != only_chapter_no:
            continue
        if chapter_id in progress["chapters_done"]:
            continue

        chapters_out.append({
            "chapter_id": chapter_id, "subject": subject,
            "chapter_no": ch["chapter_no"], "chapter_title": ch["chapter_title"],
        })

        page_dir = Path(f"/tmp/{subject}_ch{ch['chapter_no']:03d}")
        page_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "pdftoppm", "-jpeg", "-r", "150",
            "-f", str(ch["file_start"]), "-l", str(ch["file_end"]),
            pdf_path, str(page_dir / "page")
        ])
        page_files = sorted(page_dir.glob("page-*.jpg"))

        chapter_records = {}
        image_files_by_q = {}
        pages_imaged = set()       # overlap pages must not be image-extracted twice
        unmatched_images = []      # no claimant yet -- retried at chapter end
        orphans = []               # Gemini items with null/invalid q_no (RC-2)
        qn_source_pages = {}       # run-19: q_no -> sorted list of PDF pages
                                    # any pass claimed to have extracted it
                                    # from -- exported per-question, and used
                                    # by the post-chapter critique pass below
                                    # to know which page to re-show Gemini.
        # run-20 (PSY-007 Q23-Q26 phantom-record fix, 2026-08-08): the
        # set of q_nos the text layer prints question-stem headers for
        # in this chapter (chapter_printed_question_qns), UNION the
        # q_nos the Q-pass already produced in this chapter
        # (chapter_records.keys() as the windows progress). Threaded
        # into merge_question_records so the S-pass cannot create a
        # phantom record for a q_no whose real question is in another
        # chapter. Computed ONCE per chapter (one pdftotext pass over
        # the chapter's pages); the Q-pass-anchor portion is updated
        # as chapter_records grows. None when the text layer is empty
        # (scanned-only PDF) -- the merge falls back to the pre-fix
        # behavior in that case (no regression for books the text
        # layer cannot read).
        known_chapter_qns = chapter_printed_question_qns(pdf_path, page_files)
        stats = {"batches": 0, "duplicates_merged": 0, "conflicts": 0,
                 "carry_used": 0, "carry_merges": 0,
                 "orphans_recovered": 0, "orphans_buffered": 0, "orphans_remaining": 0,
                 "chapter_id": chapter_id}
        # V2: per-pass carry-forward state (Q-pass and S-pass track their own
        # open items; A-pass items are one-shot key rows, no carry needed).
        # PHASE-2 ANCHOR OBSERVATIONS (read-only hooks -- no behavior change):
        # The split-output layer's reconcile_qids grader needs the
        # run-18 GUARD's connected-run analysis (batch_qnos / runs /
        # trusted_qnos) and the per-pass compute_carry() output to
        # promote single-anchor records from PROVISIONAL to RESOLVED
        # (design doc §2 / §3.1). Both live inside transient local
        # variables in this loop, so we capture them here for the
        # chapter-end reconcile step. Every entry is a plain dict so
        # the split layer can read it without depending on this module's
        # internal types. This is the "option (a)" from the Phase-2 plan
        # (~6 lines of read-only observation per anchor; no Gemini call,
        # no carry state mutation, no behavior change to the loop).
        chapter_anchor_observations = {
            "neighbor_runs": [],      # one per Q-pass window
            "carry_forwards": [],     # one per (Q/S) carry-compute
        }
        # PHASE-2 PLAN: feed these into split_outputs.reconcile_qids()
        # at chapter end. Until then they sit in memory and add zero
        # observable behavior to the loop.
        carry_by_pass = {"Q": None, "S": None}     # FEATURE 2 payloads, per pass
        carry_trackers = {"Q": {}, "S": {}}        # q_no -> batch-seq of UNRESOLVED carry
        carry_banned = {"Q": set(), "S": set()}    # expired q_nos: never respawn
        ledger_rows = []                          # run-11 page/pass ledger (in-memory for chapter-end gate)
        solutions_section_seen = False             # sticky once the Solutions section begins
        solutions_section_announced = False        # run-11: text-layer S boundary
                                                   # announced/reset ONCE per
                                                   # chapter (was re-logged on
                                                   # EVERY S window because the
                                                   # extraction boundary owns
                                                   # solutions_section_seen)
        q_covered_pages = set()                    # run-14: pages the Q-pass has
                                                   # actually run on (per chapter)
        prev_window_last_page = None

        # SECTION-AWARE WINDOWS (run-6): detect questions/answers/solutions
        # boundaries from the text layer ONCE and send each section in its own
        # larger windows (whole question section in 1-2 calls -> no boundary
        # splits, no overlap waste; answers in one call; solutions in
        # recitation-safe chunks). Falls back to the fixed 6-page window loop
        # when the text layer can't be read (scanned-only pages).
        section_windows = build_section_windows(page_files, pdf_path)
        if section_windows:
            page_by_no = {int(p.stem.split("-")[-1]): p for p in page_files}
            window_specs = []
            for page_nos, sec in section_windows:
                batch = [page_by_no[n] for n in page_nos if n in page_by_no]
                if batch:
                    window_specs.append((batch, sec))
            if not window_specs:
                section_windows = []  # degenerate -> fall back below
        if not section_windows:
            overlap = max(0, min(BATCH_OVERLAP_PAGES, PAGES_PER_GEMINI_CALL - 1))
            batch_step = PAGES_PER_GEMINI_CALL - overlap
            window_specs = []
            for batch_start in range(0, len(page_files), batch_step):
                if batch_start and batch_start + overlap >= len(page_files):
                    break  # trailing window would contain ONLY overlap pages
                           # (nothing new) -- don't spend a quota call on it
                window_specs.append(
                    (page_files[batch_start:batch_start + PAGES_PER_GEMINI_CALL], None))
        prev_section = None
        for batch, section in window_specs:
            window_pages = [int(p.stem.split("-")[-1]) for p in batch]
            # provenance anchor used by the log lines / orphan records below
            # (the fixed-window loop used its index; the section loop uses the
            # window's first PDF page -- equally unique per window)
            batch_start = window_pages[0] if window_pages else 0
            if section is None:
                # fixed-window fallback: keep the original overlap semantics
                overlap_pages = [pn for pn in window_pages
                                 if prev_window_last_page is not None
                                 and pn <= prev_window_last_page]
            elif section == prev_section:
                # intra-section overlap only -- cross-section windows share
                # NOTHING (that was the token waste: fixed windows re-sent the
                # previous section's tail pages in every new window)
                overlap_pages = [pn for pn in window_pages
                                 if prev_window_last_page is not None
                                 and pn <= prev_window_last_page]
            else:
                overlap_pages = []  # first window of a section: no overlap
            new_pages = [pn for pn in window_pages if pn not in overlap_pages]
            if not new_pages:
                continue  # trailing window = pure overlap; nothing new
            stats["batches"] += 1

            if section == "S" and not solutions_section_announced:
                # text-layer section boundary: hard-reset ALL carry context
                # before the Solutions section, exactly like the extraction-
                # based boundary guard, so question/solution prose can never
                # cross-merge (the stale-carry class). ANNOUNCED ONCE (run-11
                # RC-8: the old guard fired on EVERY S window). NOTE: we
                # deliberately do NOT flip solutions_section_seen here --
                # Q-pass stays active until the EXTRACTION-based boundary
                # fires (probe below), exactly like the old fixed windows, so
                # the handful of questions that tail into the first solution
                # pages (ch1 class: 3 questions on pages 11-16) are never
                # skipped by a text-layer guess.
                solutions_section_announced = True
                had_pending = any(v is not None for v in carry_by_pass.values()) \
                    or any(carry_trackers.values())
                carry_by_pass = {"Q": None, "S": None}
                carry_trackers = {"Q": {}, "S": {}}
                print(f"  [SECTION] solutions section begins at page "
                      f"{window_pages[0]} (text-layer detected) -- ALL carry "
                      f"context HARD-RESET"
                      f"{' (dropped pending context)' if had_pending else ''}; "
                      f"pass activation unchanged (extraction boundary decides)")
            # V2 pass activation (zero-token pdftotext probe + sticky section
            # state) -- IDENTICAL for section and fallback windows: questions-
            # section batch -> Q-pass only; solutions section -> S-pass only;
            # key tables / solution headers on THESE pages -> +A-pass / S-pass.
            # A probe failure (scanned-only PDF) returns all-True -> all passes
            # run (safe). Never let a window-sizer disable a pass -- the text
            # layer of scanned books mislabels pages, and a skipped Q-pass
            # would silently drop those questions.
            probe = probe_batch_pages(pdf_path, window_pages)
            do_s = solutions_section_seen or probe["solutions"]
            do_a = probe["key_table"]
            # RUN-12 Q-PASS ACTIVATION FIX: on a window the section planner
            # labeled "S" (all pages >= solutions_start), the Q-pass must NOT
            # run over the whole solution section -- that is the upstream
            # source of the contaminated stems (Q read solution prose as
            # question_text on pages 13-17, 17-21, 122-126, ... and the
            # merge/guard then dead-ended). Q-pass on an S window runs ONLY
            # when the window carries question-section overlap pages (the
            # boundary tail) or a live Q carry (a question genuinely
            # continuing into the solutions).
            do_q = _should_run_q_pass(section, bool(overlap_pages),
                                       carry_by_pass.get("Q"), solutions_section_seen)
            # RUN-13 Q-PASS ACTIVATION SAFETY NET: _should_run_q_pass returns
            # False for every S-labeled window (section planner saw solution
            # headers on the chapter's first pages -- frequently the previous
            # chapter's solution tail -- and labeled the WHOLE chapter "S").
            # That silently skipped the Q-pass on real question pages and
            # produced the run's mass stem/option losses (ch2/7/11/16/18/19/
            # 24/25/28/30/32: 9-27 records each needing [question]+[options]
            # targeted retry, tails like ch7 q23-26 / ch2 q25-26 lost). If
            # the RENDERED pages still print question-stem headings, the
            # Q-pass MUST run -- deterministic OCR evidence overrides the
            # text-layer section label.
            if not do_q and section == "S" and new_pages:
                if window_has_question_content(pdf_path, new_pages, chapter_records):
                    print(f"  [ACTIVATE] OCR question anchors on S-window pages "
                          f"{new_pages} -- Q-pass ON (deterministic question content)")
                    do_q = True
            # RUN-14 Q-COVERAGE SAFETY NET: a page the Q-pass NEVER ran on is
            # a question-loss risk no matter what the section planner guessed
            # (ch2 q25/26, ch7 q23-26, ch18 q13, ch19 q11/12, ch24 q12/13 --
            # the whole chapter was labeled "S" from the previous chapter's
            # solution tail, so Q never saw the question pages and only the
            # fragile targeted retry recovered part of the loss). Run Q on
            # any window with never-Q-covered NEW pages UNLESS OCR proves the
            # pages contain no question content (pure solutions). OCR/render
            # unavailable -> RUN (safe default: accuracy must never depend on
            # the text layer -- same philosophy as probe_batch_pages).
            if not do_q:
                uncovered = [p for p in new_pages if p not in q_covered_pages]
                if uncovered:
                    try:
                        has_q = window_has_question_content(pdf_path, uncovered,
                                                            chapter_records)
                    except Exception:
                        has_q = True
                    if has_q:
                        print(f"  [ACTIVATE] pages {uncovered} never Q-passed "
                              f"and show question content -- Q-pass ON "
                              f"(run-14 Q-coverage safety net)")
                        do_q = True
            if not (do_q or do_s or do_a):
                do_q = True  # eerily silent page (figures only?) -- default to Q-pass

            # Proactive route: do not first trigger Gemini recitation on a
            # printed, clinically sensitive solutions page. OCR/header merge
            # owns it before any vision pass is built.
            routed_pages = set()
            for pf in batch:
                page_no = int(pf.stem.split("-")[-1])
                if is_recitation_risk_solution_page(pdf_path, page_no):
                    raw_ocr = ocr_fallback_text(pf)
                    n = _recover_ocr_solution_headers(raw_ocr, chapter_records)
                    if n:
                        routed_pages.add(pf)
                        print(f"  [PREFLIGHT_OCR] {pf.name}: header-routed {n} solution(s); Gemini skipped")

            fig_map_by_pass = {}   # this window's _figure_map control objects
            # Capture the carry state BEFORE this window's passes update it:
            # the active block for the window's NEW pages is the block that
            # was open at the END of the PREVIOUS window (cross-page carry).
            carries_at_window_start = dict(carry_by_pass)
            for pass_name, prompt, active in (
                    ("Q", SCHEMA_PROMPT_Q, do_q),
                    ("A", SCHEMA_PROMPT_A, do_a),
                    ("S", SCHEMA_PROMPT_S, do_s)):
                if not active:
                    continue
                reset_daily_counter_if_needed(state)
                if quota_exhausted(state):
                    print("Daily Gemini call limit reached. Saving progress, exiting.")
                    save_state(state)
                    sys.exit(0)
                pass_batch = _batch_after_routing(pass_name, batch, routed_pages)
                if not pass_batch:
                    continue
                if pass_name == "Q":
                    # run-14: the Q-pass ATTEMPTED this window's pages -- mark
                    # them covered so the safety net doesn't re-ask the same
                    # pages in every later S window (retry/rescue own failures).
                    q_covered_pages.update(window_pages)
                carry_in = carry_by_pass.get(pass_name) if pass_name in ("Q", "S") else None
                context_str = build_carry_context(carry_in, overlap_pages, new_pages)
                if carry_in:
                    stats["carry_used"] += 1
                pass_had_error = False      # run-11 page ledger
                pass_recovered = True
                try:
                    raw_items = call_gemini_on_pages(genai_model, pass_batch,
                                                     context=context_str, prompt=prompt)
                    note_call(state)
                    save_state(state)
                except Exception as e:
                    err_text = str(e)
                    pass_had_error = True
                    pass_recovered = False
                    if "finish_reason=8" in err_text or "PROHIBITED_CONTENT" in err_text:
                        event = {"subject": subject, "chapter_id": chapter_id, "chapter_no": ch["chapter_no"],
                                 "pass": pass_name, "pages": window_pages, "reason": err_text[:240]}
                        state.setdefault("safety_blocked", []).append(event)
                        save_state(state)
                        print(f"  [SAFETY_BLOCKED] {subject} {chapter_id} {pass_name}-pass pages {window_pages} "
                              "-- queued for recovery and manual review", flush=True)
                    if "429" in err_text or "quota" in err_text.lower():
                        # Free tier = ~1500 req/day PER DAY but also ~15 RPM per
                        # minute. A burst 429 is NOT the daily cap -- back off
                        # once before declaring the whole day over.
                        print(f"  [429] rate limited on {subject} ch{ch['chapter_no']} "
                              f"batch {batch_start} {pass_name}-pass -- backing off 65s "
                              f"(could be the per-minute cap, not the daily one)")
                        time.sleep(65)
                        try:
                            raw_items = call_gemini_on_pages(genai_model, batch,
                                                             context=context_str, prompt=prompt)
                            note_call(state)
                            save_state(state)
                        except Exception as e2:
                            t2 = str(e2)
                            if "429" in t2 or "quota" in t2.lower():
                                if handle_429(state, t2):
                                    print(f"  [429] still limited after backoff -- rotated to "
                                          f"{keypool_summary(state)}, retrying batch")
                                    try:
                                        raw_items = call_gemini_on_pages(
                                            genai_model, batch,
                                            context=context_str, prompt=prompt)
                                        note_call(state)
                                        save_state(state)
                                    except Exception as e3:
                                        print(f"  [WARN] batch failed after rotation: {e3}")
                                        raw_items = retry_batch_page_by_page(
                                            genai_model, pass_batch, state,
                                            ctx={"subject": subject, "chapter_no": ch["chapter_no"],
                                                 "chapter_id": chapter_id, "pass": pass_name},
                                            prompt=prompt)
                                else:
                                    print(f"  [QUOTA] every key exhausted -- daily cap "
                                          f"it is. Saving progress, exiting: {e2}")
                                    save_state(state)
                                    sys.exit(0)
                            print(f"  [WARN] post-backoff call failed differently "
                                  f"({pass_name}-pass): {e2}")
                            raw_items = retry_batch_page_by_page(
                                genai_model, pass_batch, state,
                                ctx={"subject": subject, "chapter_no": ch["chapter_no"],
                                     "chapter_id": chapter_id, "pass": pass_name},
                                prompt=prompt)
                            pass_recovered = bool(raw_items)
                            if not raw_items:
                                ledger_rows.append(_ledger_pass(
                                    chapter_id, subject, ch["chapter_no"], pass_name,
                                    window_pages, PASS_STATUS_UNRESOLVED, 0,
                                    "batch+backoff+page retries all failed"))
                                continue
                    elif "Invalid Gemini JSON" in err_text or "empty JSON response" in err_text:
                        # A malformed structured response is a model output
                        # glitch, not a page problem (run-5: 2 A-pass batches
                        # burned 6 single-page calls each before recovering).
                        # Re-ask the SAME batch once (1 call); only if that
                        # also fails, descend to page-by-page salvage.
                        print(f"  [WARN] malformed JSON on {pass_name}-pass pages "
                              f"{window_pages} -- one same-batch re-ask before "
                              f"page-by-page salvage")
                        try:
                            raw_items = call_gemini_on_pages(genai_model, pass_batch,
                                                             context=context_str,
                                                             prompt=prompt)
                            # RUN-12 LEDGER FIX: a SUCCESSFUL same-batch re-ask
                            # is a full recovery -- without this flag the pass
                            # was classified UNRESOLVED (ch13 gate flagged
                            # unresolved_page_A [175-179] even though the
                            # re-ask returned 14 items).
                            pass_recovered = True
                            note_call(state)
                            save_state(state)
                        except Exception as e2:
                            print(f"  [WARN] same-batch re-ask failed ({e2}) "
                                  f"-- page-by-page salvage")
                            raw_items = retry_batch_page_by_page(
                                genai_model, pass_batch, state,
                                ctx={"subject": subject, "chapter_no": ch["chapter_no"],
                                     "chapter_id": chapter_id, "pass": pass_name},
                                prompt=prompt)
                            pass_recovered = bool(raw_items)
                            if not raw_items:
                                ledger_rows.append(_ledger_pass(
                                    chapter_id, subject, ch["chapter_no"], pass_name,
                                    window_pages, PASS_STATUS_UNRESOLVED, 0,
                                    "malformed JSON + same-batch re-ask + page salvage failed"))
                                continue
                    else:
                        print(f"  [WARN] Gemini {pass_name}-pass failed on {subject} "
                              f"ch{ch['chapter_no']} batch {batch_start}: {e}")
                        # don't lose the whole batch over one bad page
                        raw_items = retry_batch_page_by_page(
                            genai_model, pass_batch, state,
                            ctx={"subject": subject, "chapter_no": ch["chapter_no"],
                                 "chapter_id": chapter_id, "pass": pass_name},
                            prompt=prompt)
                        pass_recovered = bool(raw_items)
                        if not raw_items:
                            ledger_rows.append(_ledger_pass(
                                chapter_id, subject, ch["chapter_no"], pass_name,
                                window_pages, PASS_STATUS_UNRESOLVED, 0,
                                "batch + page-by-page retries failed"))
                            continue

                if pass_name == "S":
                    raw_items, n_clip = clip_pass_solutions(raw_items)
                    if n_clip:
                        print(f"  [S-CLIP] {n_clip} foreign 'Solution to Question N:' "
                              f"tail(s) clipped in S-pass output (sibling item "
                              f"present -- provably zero-loss)")
                items, batch_meta = extract_batch_meta(raw_items)
                # run-11 page ledger: classify this window-pass attempt
                ledger_rows.append(_ledger_pass(
                    chapter_id, subject, ch["chapter_no"], pass_name,
                    window_pages,
                    _classify_pass_status(pass_name, section, len(items),
                                          pass_had_error, pass_recovered),
                    len(items)))
                # provenance of every normal-pass item (run-7 hardening #4):
                # used by merge to enforce patch-only recovery and to reject
                # contamination (an S/A item's stray stem is never merged).
                for it in items:
                    if isinstance(it, dict):
                        it["_prov"] = f"{pass_name}_PASS"
                if batch_meta.get("figure_map"):
                    # Q-pass sees question-side figures, S-pass solution-side;
                    # keep the first non-empty map per pass for this window.
                    fig_map_by_pass[pass_name] = batch_meta["figure_map"]
                # run-18: Gemini's Q-pass can occasionally confabulate a
                # full, plausible-looking question (stem+options+answer)
                # that has no basis on the actual page -- seen for real on
                # PSY-002 q25/q26 (pages 31-33 render as an unrelated
                # catatonic-signs glossary; the "hysteria"/"Big Five"
                # questions it returned exist nowhere on those pages). A
                # hallucinated item is indistinguishable from a real one by
                # shape alone (well-formed stem, 4 options, matching
                # solution) -- the only independent signal is whether the
                # page actually PRINTS that question number anywhere. Only
                # gate items claiming a q_no this chapter has never seen
                # before (a genuine re-ask/continuation of an already-known
                # q_no is not at risk of this failure mode); route ungated
                # ones to the existing orphans review pathway rather than
                # dropping them outright, since OCR itself can miss a
                # genuine heading (that's a false negative here, not a false
                # positive, and orphans already get a chapter-end recovery
                # pass).
                if pass_name == "Q" and items:
                    def _as_int_qno(v):
                        try:
                            return int(v)
                        except (TypeError, ValueError):
                            return None
                    known_qnos = [qn for qn in chapter_records if isinstance(qn, int)]
                    known_max = max(known_qnos) if known_qnos else 0
                    # run-19 fix: Gemini returns q_no as int OR string ("1" vs
                    # 1) depending on the call -- confirmed in production,
                    # where a string-only batch made every isinstance(int)
                    # check below silently fail, emptied trusted_qnos, and
                    # sent an entire genuine chapter-1 batch (q1-26+) to
                    # orphans. Normalize once, up front, exactly like
                    # merge_question_records already does for the same
                    # reason.
                    batch_qnos = sorted(set(
                        qn for qn in (_as_int_qno(it.get("q_no")) for it in items
                                      if isinstance(it, dict))
                        if qn is not None))
                    # connected runs (gap <= 2) within this batch's numbers --
                    # a run is trusted (no OCR needed) if it's substantial
                    # (>=5 items: a real fresh chapter start looks like this)
                    # or it touches the chapter's already-established range.
                    # A small run floating far above known_max validates
                    # nothing about itself just by being internally
                    # consecutive -- that was the exact gap that let q25+q26
                    # wrongly confirm each other below.
                    runs, cur_run = [], []
                    for qn in batch_qnos:
                        if cur_run and qn - cur_run[-1] > 2:
                            runs.append(cur_run); cur_run = []
                        cur_run.append(qn)
                    if cur_run:
                        runs.append(cur_run)
                    trusted_qnos = set()
                    for run in runs:
                        if len(run) >= 5 or (known_max and min(run) - known_max <= 3):
                            trusted_qnos.update(run)
                    # PHASE-2 ANCHOR OBSERVATION: capture the GUARD's connected-run
                    # analysis unchanged for the chapter-end reconcile step.
                    # Read-only: nothing here mutates loop state. The grader
                    # uses this to promote single-anchor records from
                    # PROVISIONAL to RESOLVED when q_no is in a trusted run
                    # (design doc §2 / §3.1). One entry per Q-pass window
                    # that actually ran the GUARD.
                    chapter_anchor_observations["neighbor_runs"].append({
                        "window_pages": list(window_pages),
                        "batch_qnos": list(batch_qnos),
                        "runs": [list(r) for r in runs],
                        "trusted_qnos": sorted(int(q) for q in trusted_qnos),
                        "known_max": int(known_max),
                    })
                    verified_items, unverified = [], []
                    ocr_conf_cache = {}
                    for it in items:
                        qn = _as_int_qno(it.get("q_no")) if isinstance(it, dict) else None
                        if qn is None or qn in chapter_records or qn in trusted_qnos:
                            verified_items.append(it)
                            continue
                        seen_here = ocr_conf_cache.get(id(window_pages))
                        if seen_here is None:
                            seen_here = set()
                            for _p in window_pages:
                                try:
                                    _img, _scale, _ph = render_page_png(pdf_path, _p, dpi=150)
                                    if _img:
                                        for _k, _q, _y in ocr_page_anchors(_img, _scale, _ph):
                                            if _k == "question":
                                                seen_here.add(_q)
                                except Exception:
                                    pass
                            ocr_conf_cache[id(window_pages)] = seen_here
                        if qn in seen_here:
                            verified_items.append(it)
                        else:
                            print(f"  [GUARD] q{qn}: unconfirmed by OCR AND a large jump "
                                  f"from chapter max q{known_max} (pages "
                                  f"{window_pages[0]}-{window_pages[-1]}) -- routing to "
                                  f"orphans for review instead of auto-accepting "
                                  f"(possible confabulation, run-18)")
                            unverified.append(it)
                    items = verified_items
                    for it in unverified:
                        orphans.append({
                            "chapter_id": chapter_id, "batch_start": batch_start,
                            "pdf_pages": window_pages, "new_pages": new_pages,
                            "reason": "unconfirmed_discontinuous_qno",
                            "pass": pass_name, "item": it,
                        })
                    stats["orphans_buffered"] = stats.get("orphans_buffered", 0) + len(unverified)
                # run-20: thread the chapter's known q_no set (text-layer
                # printed headers + Q-pass anchors accumulated so far) and
                # the carry-in's open q_no into merge_question_records.
                # Foreign-chapter q_nos are dropped at the merge step
                # (before the master build_final_question loop), not at
                # the split-output step. Carry-only is allowed (legitimate
                # cross-page continuation of a real question).
                carry_qns = []
                if carry_in and carry_in.get("last_open_question") is not None:
                    carry_qns.append(carry_in["last_open_question"])
                chapter_records, skipped = merge_question_records(
                    chapter_records, items, stats,
                    known_chapter_qns=known_chapter_qns,
                    carry_q_nos=carry_qns)
                # Refresh known_chapter_qns with the Q-pass anchors that
                # merged in this window (the text-layer portion is fixed;
                # this grows the allowed set as new real questions appear).
                known_chapter_qns |= {qn for qn in chapter_records if isinstance(qn, int)}
                for it in items:
                    _qn = it.get("q_no") if isinstance(it, dict) else None
                    if isinstance(_qn, (int, str)):
                        try:
                            _qn = int(_qn)
                        except (TypeError, ValueError):
                            _qn = None
                    if _qn is not None and _qn in chapter_records:
                        qn_source_pages.setdefault(_qn, set()).update(window_pages)
                try:
                    last_qn_in_batch = max(int(it.get("q_no")) for it in items
                                           if it.get("q_no") is not None)
                except (ValueError, TypeError):
                    last_qn_in_batch = None
                for it in skipped:
                    # RC-2 salvage buffer: fragments (usually batch-boundary
                    # continuations) carry real content -- keep with provenance
                    # (+ which pass produced them) for chapter-end recovery.
                    orphans.append({
                        "chapter_id": chapter_id, "batch_start": batch_start,
                        "pdf_pages": window_pages, "new_pages": new_pages,
                        "carry_q_no": carry_in["last_open_question"] if carry_in else None,
                        "cut_part": carry_in.get("cut_part") if carry_in else None,
                        "last_qn_in_batch": last_qn_in_batch,
                        "pass": pass_name,
                        "item": it,
                        # RUN-20 (2026-08-08): propagate the merge's
                        # _drop_reason (set when the FOREIGN guard drops
                        # the item) so recover_orphans and the export
                        # gate's orphan_unresolved check can recognize
                        # foreign-chapter drops as EXPECTED (not data
                        # loss). Items dropped for any other reason
                        # (e.g. q_no is None, stem-rejected to empty)
                        # leave drop_reason unset -- the export gate
                        # still flags THOSE as data loss.
                        "drop_reason": it.get("_drop_reason"),
                    })
                stats["orphans_buffered"] += len(skipped)
                if pass_name in ("Q", "S"):
                    # per-pass carry-forward (Feature 2), then stale-carry guard #1
                    carry_by_pass[pass_name] = compute_carry(
                        batch_meta, items, chapter_records, max(window_pages))
                    # PHASE-2 ANCHOR OBSERVATION: capture the compute_carry()
                    # output unchanged for the chapter-end reconcile step.
                    # Read-only: nothing here mutates the carry state below.
                    # The grader uses this to promote a single-anchor record
                    # from PROVISIONAL to RESOLVED when it has a valid
                    # carry-forward origin (design doc §2 / §3.1). One entry
                    # per (Q/S) carry-compute. The carry dict is JSON-safe
                    # via str() of last_open_question so the split layer
                    # never has to deal with live dict references.
                    _co = carry_by_pass[pass_name] or {}
                    chapter_anchor_observations["carry_forwards"].append({
                        "pass": str(pass_name),
                        "window_pages": list(window_pages),
                        "last_open_question": _co.get("last_open_question"),
                        "cut_part": _co.get("cut_part"),
                        "ends_mid_content": _co.get("ends_mid_content"),
                    })
                    carry_by_pass[pass_name] = enforce_carry_expiry(
                        carry_by_pass[pass_name], stats["batches"],
                        carry_trackers[pass_name], carry_banned[pass_name],
                        chapter_records, chapter_id)
                # stale-carry guard #2: questions->solutions SECTION boundary.
                # The first batch that shows the solutions section hard-resets
                # ALL carry context -- resolved or not -- so it can NEVER bleed
                # into the Solutions section and cross-merge there. Q-pass also
                # turns OFF from the next batch (probe may still enable S/A).
                if pass_name == "Q" and not solutions_section_seen:
                    boundary = detect_section_boundary(items)
                    if boundary:
                        solutions_section_seen = True
                        had_pending = any(v is not None for v in carry_by_pass.values()) \
                            or any(carry_trackers.values())
                        carry_by_pass = {"Q": None, "S": None}
                        carry_trackers = {"Q": {}, "S": {}}
                        print(f"  [SECTION] {boundary} first seen at pages "
                              f"{window_pages[0]}-{window_pages[-1]} -- solutions "
                              f"section begins; ALL carry context HARD-RESET"
                              f"{' (dropped pending context)' if had_pending else ''}; "
                              f"Q-pass disabled from the next batch")
                carry_obj = carry_by_pass.get(pass_name)
                last_open = (f"q{carry_obj['last_open_question']}"
                             if carry_obj and carry_obj["last_open_question"] is not None
                             else ("open (no number)" if carry_obj else "-"))
                print(f"  [GEMINI:{pass_name}] pages {window_pages[0]}-{window_pages[-1]}"
                      f" | overlap: {overlap_pages if overlap_pages else '-'}"
                      f" | carry-in: {('q' + str(carry_in['last_open_question'])) if carry_in and carry_in['last_open_question'] is not None else '-'}"
                      f" | last-open: {last_open}"
                      f" | items: {len(items)} | orphans buffered: {len(skipped)}")

            prev_window_last_page = max(window_pages)
            prev_section = section

            # extract real (non-watermark) images from this batch's pages.
            # pdftoppm names output files using the ACTUAL pdf page number
            # (e.g. page-005.jpg for real page 5) -- read it directly from
            # the filename, don't recompute it relative to ch["file_start"].
            # First collect every page's images (top-to-bottom order) for the
            # window, THEN claim -- GEOMETRY-FIRST (run-9): deterministic
            # block ownership (question + solution headings, closest header
            # above each image) + cross-page carry (active_block from the
            # previous window) runs FIRST on every page; the Gemini figure-map
            # and the 4th-pass model attribution only run on the LEFTOVERS and
            # can never override a deterministic assignment.
            window_rows = []
            for pf in batch:
                file_page_num = int(pf.stem.split("-")[-1])
                if file_page_num in pages_imaged:
                    continue  # overlap page -- images already extracted once
                pages_imaged.add(file_page_num)
                imgs = extract_real_images(pdf_path, file_page_num, watermark_id, subject, ASSETS_DIR / "questions")
                if not imgs:
                    continue
                pos = image_positions_on_page(pdf_path, file_page_num)
                ordered = _order_imgs_by_position(imgs, pos)
                window_rows.append((file_page_num, ordered))

            # GEOMETRY-FIRST (run-9 priority A/B/C): every image goes to the
            # closest question/solution heading ABOVE it (or the carried
            # active block for cross-page continuations). This is the SAME
            # deterministic system that maps solution figures (page 33 ->
            # PSY-002-014) now extended to question-side figures -- the
            # page-4 class fix. Gemini never overrides this.
            active_block = _active_block_from_carries(carries_at_window_start)
            leftover_by_page = {}
            for file_page_num, rels in window_rows:
                leftover = claim_page_images(rels, pdf_path, file_page_num, subject,
                                             ch["chapter_no"], chapter_records,
                                             image_files_by_q, active_block=active_block)
                # run-13 LEVEL 2 (deterministic OCR-anchored geometry): the
                # text layer on this book's QUESTION pages is garbled, so
                # L1 finds no headings there and question-side figures would
                # fall straight to the model. OCR the RENDERED page and
                # re-apply the closest-heading-above rule -- zero Gemini
                # calls, immune to text-layer garble.
                leftover = claim_block_images_ocr(
                    leftover, pdf_path, file_page_num, subject, ch["chapter_no"],
                    chapter_records, image_files_by_q, chapter_id=chapter_id,
                    active_block=active_block)
                leftover_by_page[file_page_num] = leftover

            # FIGURE-MAP pass (run-6 user ask, run-9 priority D): Gemini's
            # own _figure_map declares q_no+slot per figure in reading order.
            # Now runs ONLY on what geometry left unclaimed (exact-count guard
            # inside: a mismatch skips safely) -- it is a fallback, never an
            # override of deterministic block ownership.
            window_fig_map = fig_map_by_pass.get("Q") or fig_map_by_pass.get("S") or None
            if window_fig_map:
                remaining_rows = [(p, leftover_by_page[p]) for p, _rels in window_rows
                                  if leftover_by_page.get(p)]
                if remaining_rows:
                    fig_leftover = claim_figure_map_images(
                        window_fig_map, remaining_rows, subject, ch["chapter_no"],
                        chapter_records, image_files_by_q)
                    # STALE-PATH FIX (run-11 RC-1): claim_figure_map_images
                    # RENAMES (moves) each claimed temp file to its final slot
                    # name. A page whose images were ALL claimed is absent from
                    # fig_leftover -- leaving leftover_by_page[page] with the
                    # OLD TEMP NAMES made those stale paths flow to
                    # unmatched_images, and the 4th pass then threw
                    # FileNotFoundError ("attribution call failed ... No such
                    # file or directory") for images that were ALREADY owned.
                    # Fix: every page we fed to the map gets its post-map
                    # leftover ([] when fully claimed).
                    for page_no, _rels in remaining_rows:
                        leftover_by_page[page_no] = fig_leftover.get(page_no) or []

            for file_page_num, _rels in window_rows:
                leftover = leftover_by_page.get(file_page_num) or []
                if leftover:
                    unmatched_images.append({"page": file_page_num, "files": leftover})
                    print(f"  [INFO] Page {file_page_num}: image(s) {leftover} unclaimed for now "
                          f"-- will retry after all batches (owner may be in a later batch)")

        # FEATURE 3 -- orphan recovery runs BEFORE image claiming and JSON
        # writing: recovered fragments can complete solutions/options, and
        # only genuinely ownerless orphans are persisted (after the drain's
        # second recovery -- persisting early wrote "unresolved" entries for
        # fragments the drain later healed).
        orphans = recover_orphans(orphans, chapter_records, subject, ch["chapter_no"], stats)
        stats["orphans_remaining"] = len(orphans)

        # SECOND PASS image claiming: a figure can be extracted BEFORE the
        # batch that introduces its owning question (plate printed just before
        # the question text, or owner arrived via an overlap window). Chapter
        # records are complete now -- retry every leftover once.
        n_unmatched = 0
        for um in unmatched_images:
            # Full two-stage claimer again: chapter records are now complete,
            # so a solution header whose q_no was missing at first-pass time
            # becomes usable for position mapping; leftovers still fall to the
            # one-to-one matcher.
            leftover2 = claim_page_images(um["files"], pdf_path, um["page"],
                                          subject, ch["chapter_no"],
                                          chapter_records, image_files_by_q)
            um["files"] = leftover2
            if not leftover2:
                print(f"  [INFO] second pass: page {um['page']} image(s) matched to a question")
                um["matched"] = True
                continue
            # THIRD pass (0 tokens): Gemini never set has_figure flags, so
            # the flag-only matcher can never fire (run-2: p127/128/188/295/
            # 318). Read the page's printed question numbers via pdftotext --
            # if EXACTLY ONE of this chapter's questions lives on the image's
            # page, that question is the owner with high confidence. Zero or
            # multiple candidates stay unmatched (evidence insufficient).
            try:
                qns = qns_printed_on_page(pdf_path, um["page"], chapter_records)
            except Exception as e:
                print(f"  [WARN] third-pass pdftotext failed for page {um['page']}: {e}")
                qns = []
            if len(qns) == 1:
                qn = qns[0]
                rec = chapter_records[qn]
                entry = image_files_by_q.setdefault(qn, {"question": [], "solution": []})
                qt, st = (rec.get("question_text") or "").lower(), (rec.get("solution_text") or "").lower()
                side = "question" if ("fig" in qt or "diagram" in qt or not st) else "solution"
                if not entry[side]:
                    qid = f"{subject}-{ch['chapter_no']:03d}-{qn:03d}"
                    # _rename_for_slot: collision-proof suffixing (two pages can
                    # print the same q_no across a page break), the
                    # MAX_QUESTION_IMAGES cap, and the tiny-crop guard -- the old
                    # hand-rolled rename here bypassed all three (overwrite risk).
                    consumed = []  # (old_rel, new_rel)
                    for old_rel in list(um["files"]):
                        new_rel = _rename_for_slot(old_rel, qn, side, subject,
                                                   ch["chapter_no"], image_files_by_q)
                        if new_rel:
                            consumed.append((old_rel, new_rel))
                    if consumed:
                        entry[side].extend(nr for _, nr in consumed)
                        done = {o for o, _ in consumed}
                        um["files"] = [f for f in um["files"] if f not in done]
                        um["matched"] = not um["files"]
                        if um["matched"]:
                            print(f"  [INFO] third pass: page {um['page']} image(s) attached to {qid} "
                                  f"(sole printed question on that page, {side} side)")
                        else:
                            print(f"  [INFO] third pass: page {um['page']}: {len(consumed)} image(s) "
                                  f"attached to {qid}; rest refused by guards -- left for review")
                    else:
                        print(f"  [WARN] third pass: rename failed for page {um['page']} "
                              f"-- left unmatched (file already moved earlier?)")
            elif qns:
                print(f"  [INFO] third pass: page {um['page']} has {len(qns)} printed questions "
                      f"{qns} -- ambiguous owner, left for manual review")
        # FOURTH pass (run-13 LEVEL 3 first, isolated-crop fallback after):
        # L3 renders the page, highlights every leftover's drawn bbox and asks
        # Gemini for ownership from the PRINTED LAYOUT (question numbers /
        # option letters / solution headers) -- the page-4 class fix: the old
        # isolated-crop call could not see any printed anchor and guessed
        # "decorative" for a real Q1 figure. Only files the vision pass could
        # not place (render unavailable / call failed / null verdict) fall
        # through to the legacy one-image-per-call attribution, which remains
        # conservative (a "decorative" verdict is never a discard).
        chapter_unresolved_images = []
        for um in unmatched_images:
            if um.get("matched"):
                continue
            try:
                vis_pos = image_positions_on_page(pdf_path, um["page"])
            except Exception:
                vis_pos = {}
            um["vision_positions"] = bool(vis_pos)   # audit tag for unresolved
            _vis_claimed, vis_still, vis_verdicts = full_page_vision_ownership(
                genai_model, pdf_path, um["page"], um["files"], vis_pos,
                subject, ch["chapter_no"], chapter_records, chapter_id, state,
                image_files_by_q)
            um["files"] = vis_still
            um["model_verdicts"] = vis_verdicts
        for um in unmatched_images:
            if um.get("matched"):
                continue
            still, brake_hit, verdicts = [], False, {}
            for rel in um["files"]:
                if brake_hit:
                    still.append(rel)
                    continue
                verdict = attribute_orphan_image(genai_model, rel, chapter_records, state)
                if verdict and verdict.get("decorative") == "brake":
                    brake_hit = True
                    still.append(rel)
                    continue
                if verdict and verdict.get("decorative") == "already_claimed":
                    # STALE-PATH FIX (run-11 RC-1): the file was already
                    # renamed by an earlier claim -- drop the stale temp
                    # reference entirely (it is NOT an unmatched image).
                    print(f"  [IMG] fourth pass: page {um['page']} {rel} already "
                          f"claimed (relocated) -- removed from unmatched set")
                    continue
                if not verdict:
                    still.append(rel)   # undecided / call failed -> manual review
                    continue
                if verdict.get("decorative") is True:
                    # CONSERVATIVE (run-9): a single Gemini "decorative"
                    # verdict is NOT strong enough to discard a real extracted
                    # image (PSY-p4-7 was called decorative in one run yet
                    # belongs to Q1). Record it to unresolved_images.jsonl
                    # (kept on disk for review) instead of permanently logging
                    # it as decorative. Only STRONG deterministic evidence --
                    # the watermark object id, already excluded at extraction
                    # -- may permanently classify decorative.
                    print(f"  [IMG] fourth pass: page {um['page']} {rel} model says "
                          f"decorative -- CONSERVATIVE: recorded to "
                          f"unresolved_images.jsonl, NOT discarded")
                    rec = _record_unresolved_image(
                        subject, chapter_id, um["page"], rel,
                        "model-declared decorative (single "
                        "verdict -- conservative, kept for review)",
                        model_verdict={"decorative": True}, method="isolated_crop_vision",
                        confidence=_verdict_confidence(um.get("model_verdicts"), rel))
                    if rec:
                        chapter_unresolved_images.append(rec)
                    continue
                qn_attr = verdict.get("q_no")
                if isinstance(qn_attr, bool) or not isinstance(qn_attr, int) \
                        or qn_attr not in chapter_records:
                    still.append(rel)   # weak/no match the model wouldn't stand behind
                    verdicts[rel] = verdict
                    continue
                slot = verdict.get("slot")
                if slot not in ("question", "solution"):
                    slot = "question"
                new_rel = _rename_for_slot(rel, qn_attr, slot, subject, ch["chapter_no"],
                                           image_files_by_q)
                if new_rel:
                    image_files_by_q.setdefault(qn_attr, {"question": [], "solution": []})[slot].append(new_rel)
                    qid = f"{subject}-{ch['chapter_no']:03d}-{qn_attr:03d}"
                    print(f"  [IMG] fourth pass: page {um['page']} {rel} -> {qid} "
                          f"({slot} side, model-attributed)")
                else:
                    # model DECLARED the owner but a guard (tiny-crop /
                    # over-attribution cap) refused the rename -- keep the
                    # verdict visible so nothing is silently unclaimed.
                    verdicts[rel] = verdict
                    print(f"  [IMG] fourth pass: page {um['page']} {rel} -> q{qn_attr} "
                          f"({slot}) DECLARED by model but guard refused rename "
                          f"-- verdict recorded, left for review")
                    still.append(rel)
            um["files"] = still
            um["model_verdicts"] = verdicts
            if not still:
                um["matched"] = True
            elif brake_hit:
                um["brake_hit"] = True
                print(f"  [WARN] image attribution stopped early (daily quota) -- "
                      f"{len(still)} file(s) from page {um['page']} stay queued")
        for um in unmatched_images:
            if not um.get("matched"):
                n_unmatched += 1
                print(f"  [WARN] Page {um['page']}: extracted image(s) {um['files']} but no "
                      f"question/solution in this chapter claimed one -- left under its temp "
                      f"filename for manual review (see data/unmatched_images.jsonl).")
                entry = {"subject": subject, "chapter_id": chapter_id,
                         "page": um["page"], "files": um["files"]}
                if um.get("model_verdicts"):
                    entry["model_verdicts"] = um["model_verdicts"]  # model's q_no/slot answers
                _append_jsonl(DATA_DIR / "unmatched_images.jsonl", entry)
                # run-13 L4: every file that exhausted ALL ownership levels is
                # ALSO recorded to unresolved_images.jsonl so the export gate
                # can never print CLEAN while a real figure lacks an owner
                # (the page-4 false-clean class).
                for rel in um["files"]:
                    if um.get("brake_hit"):
                        reason = "left queued by daily-quota brake (resume tomorrow)"
                        method = "quota_brake"
                    elif not um.get("vision_positions"):
                        reason = ("no owner after L1 geometry + L2 OCR geometry; "
                                  "L3 full-page vision SKIPPED (no parsed image "
                                  "positions on the page) -- isolated fallback "
                                  "also could not prove ownership")
                        method = "vision_skipped_no_position"
                    else:
                        reason = ("no owner after L1 geometry + L2 OCR geometry + "
                                  "L3 full-page vision + isolated fallback")
                        method = "all_levels_failed"
                    rec = _record_unresolved_image(
                        subject, chapter_id, um["page"], rel, reason,
                        method=method,
                        confidence=_verdict_confidence(um.get("model_verdicts"), rel))
                    if rec:
                        chapter_unresolved_images.append(rec)

        # FAILED-PAGE DRAIN: second chance for recitation-skipped pages
        # BEFORE orphan recovery (drained fragments may join the orphan
        # pool) and BEFORE targeted retry (so drained solutions count when
        # the 60% book-prints-solutions gate is evaluated).
        pending_failed = [e for e in state.get("failed_pages", [])
                          if e.get("chapter_id") == chapter_id]
        if pending_failed:
            chapter_records, drain_orphans, healed = drain_failed_pages(
                genai_model, pending_failed, page_dir, chapter_records, state, stats,
                pdf_path=pdf_path)
            orphans.extend(drain_orphans)
            orphans = recover_orphans(orphans, chapter_records, subject, ch["chapter_no"], stats)
            stats["orphans_remaining"] = len(orphans)
            if healed:
                healed_ids = {(e["subject"], e["chapter_no"], e["true_page"]) for e in healed}
                state["failed_pages"] = [e for e in state.get("failed_pages", [])
                                         if (e.get("subject"), e.get("chapter_no"), e.get("true_page"))
                                         not in healed_ids]
                save_state(state)
            print(f"  [DRAIN] second chance: {len(healed)}/{len(pending_failed)} previously-failed page(s) recovered")

        # persist only the FINAL unresolved orphans (after the drain's second
        # recovery pass) -- never ledger entries the drain later healed.
        for orph in orphans:
            _append_jsonl(DATA_DIR / "orphans.jsonl", orph)

        # INTEGRITY SWEEP: zero-token deterministic proofs (run-4 audit RCA:
        # duplicated wrong-owner stems, foreign 'Option' heads, truncated
        # solutions, over-attributed images) BEFORE targeted retry, so
        # stripped/provably-incomplete fields are re-asked in the SAME run.
        forced_solution_qns = chapter_integrity_sweep(
            chapter_records, image_files_by_q, subject, ch["chapter_no"], stats)

        # PRINTED-SOLUTION EVIDENCE (ch25 class): a q_no whose "Solution to
        # Question N:" header exists in the chapter's text layer PROVES the
        # book prints an explanation for it. When the chapter sits below the
        # 60% solution-gate, scan the pages ONCE (zero-token) and bypass the
        # gate for exactly those q_nos -- the run-5 audit showed the gate
        # suppressing 5 REAL solutions in ch25 (7/12 = 58%).
        n_with_sol = sum(1 for r in chapter_records.values()
                         if (r.get("solution_text") or "").strip())
        gate_marginal = (
            chapter_records
            and n_with_sol / len(chapter_records) < SOLUTION_GATE_MIN_SHARE
            and any(r.get("question_text") and not (r.get("solution_text") or "").strip()
                    for r in chapter_records.values())
        )
        printed_sol_qns = chapter_printed_solution_qns(
            pdf_path, page_files, chapter_records) if gate_marginal else set()
        if printed_sol_qns:
            print(f"  [GATE] chapter {ch['chapter_no']} below the {SOLUTION_GATE_MIN_SHARE:.0%} "
                  f"solution gate but {len(printed_sol_qns)} printed 'Solution to Question N:' "
                  f"header(s) found ({sorted(printed_sol_qns)}) -- retry eligible for those")

        # FEATURE: targeted gap-retry -- AFTER normal batches + orphan
        # recovery, BEFORE writing the chapter's questions to disk.
        n_fixed = targeted_retry(genai_model, page_files, chapter_records,
                                 state, max_rounds=TARGETED_RETRY_MAX_ROUNDS,
                                 force_solution_qns=forced_solution_qns,
                                 chapter_id=chapter_id,
                                 printed_solution_qns=printed_sol_qns,
                                 stats=stats)
        if n_fixed:
            print(f"  [RETRY] closed {n_fixed} field(s) via targeted retry")

        # RESCUE PASS (run-5 audit): records still incomplete after the
        # whole-chapter retry get one page-focused last ditch -- re-ask ONLY
        # the pages where their q_no is printed, instead of the whole chapter
        # again. This is what the 9 persistent gaps (ch2 q25/26, ch18 q13,
        # ch19 q11/12, ch24 q12/13, ch27 q11, ch33 q9) needed.
        n_rescued = rescue_incomplete_records(
            genai_model, page_files, pdf_path, chapter_records, state, stats,
            chapter_id, printed_solution_qns=printed_sol_qns)
        if n_rescued:
            print(f"  [RESCUE] closed {n_rescued} field(s) via page-focused rescue")

        # ANCHORLESS RECORDS (ch24 q12/13 class): rows with NO stem, NO
        # options and NO solution after batch + 2 retry rounds + rescue are
        # phantom answer-key rows (a printed key table spanning chapters) or
        # fully-lost fragments -- shipping them pollutes the app with empty
        # questions. Drop with a ledger entry; nothing is silently lost.
        dropped_anchorless = []
        kept_records = {}
        for qn, rec in sorted(chapter_records.items(), key=lambda x: x[0]):
            if not ((rec.get("question_text") or "").strip()
                    or (rec.get("options") or {})
                    or (rec.get("solution_text") or "").strip()):
                dropped_anchorless.append(qn)
                _append_jsonl(DATA_DIR / "dropped_anchorless.jsonl",
                              {"chapter_id": chapter_id, "q_no": qn,
                               "correct_option": rec.get("correct_option"),
                               "reason": "no stem/options/solution after "
                                         "batch+retry+rescue (phantom key row "
                                         "or fully-lost fragment)"})
                continue
            kept_records[qn] = rec
        if dropped_anchorless:
            print(f"  [DROP] {len(dropped_anchorless)} anchorless record(s) removed "
                  f"(q{sorted(dropped_anchorless)}) -- logged to "
                  f"data/dropped_anchorless.jsonl")
            chapter_records = kept_records
            stats["anchorless_dropped"] = stats.get("anchorless_dropped", 0) + len(dropped_anchorless)

        # cross-chapter duplicate proof for the phantom check: read the
        # already-written questions.jsonl rows (excluding this chapter) so a
        # solution-only record is only dropped when an EARLIER chapter already
        # shipped the same q_no + solution (ch2 q25/26 <- ch1 q25/26 spill).
        try:
            prior_rows = [json.loads(l) for l in
                          (DATA_DIR / "questions.jsonl").read_text(
                              encoding="utf-8").splitlines() if l.strip()]
            prior_rows = [r for r in prior_rows
                          if r.get("chapter_id") != chapter_id
                          and (r.get("solution") or {}).get("text")]
        except Exception:
            prior_rows = []
        dropped_phantom = drop_phantom_solution_only_records(
            chapter_records, chapter_id, stats, prior_rows=prior_rows)
        if dropped_phantom:
            print(f"  [DROP] {len(dropped_phantom)} solution-only phantom record(s) "
                  f"removed (q{dropped_phantom}) -- preserved in "
                  f"data/dropped_phantom_records.jsonl")
            chapter_records = {qn: r for qn, r in chapter_records.items()
                               if qn not in set(dropped_phantom)}

        # EXPORT GATE (run-11): deterministic pre-export check. A chapter may
        # not be exported as "complete" while these violations stand -- each
        # is logged loudly and persisted to data/export_gate.jsonl. The
        # pipeline still writes rows (resumability), but the gate makes the
        # incompleteness EXPLICIT instead of hiding behind
        # "0 missing answer / 0 missing solution".
        unresolved_ledger = [l for l in ledger_rows if l["status"] == PASS_STATUS_UNRESOLVED]
        violations = _export_gate_violations(chapter_records, image_files_by_q,
                                             unresolved_ledger, chapter_id,
                                             chapter_unresolved_images, orphans)
        if violations:
            stats["export_gate_violations"] = stats.get("export_gate_violations", 0) + len(violations)
            for kind, qn, detail in violations:
                _append_jsonl(DATA_DIR / "export_gate.jsonl",
                              {"chapter_id": chapter_id, "kind": kind,
                               "q_no": qn, "detail": detail,
                               "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
            print(f"  [GATE] chapter {ch['chapter_no']}: {len(violations)} export-gate "
                  f"violation(s) -- NOT a clean export:")
            for kind, qn, detail in violations[:25]:
                print(f"    - {kind} {qn}: {detail}")
        else:
            print(f"  [GATE] chapter {ch['chapter_no']}: export gate CLEAN "
                  f"(stems/options/answers/solutions/orphans/images/assets all "
                  f"accounted)")

        # run-19: CRITIQUE-AND-REPAIR (Sachin's idea) -- only runs if there
        # were violations, only touches the specific flagged fields on the
        # specific flagged questions, using each question's own source
        # page(s). See ENABLE_CRITIQUE_PASS to disable entirely.
        if violations and ENABLE_CRITIQUE_PASS:
            n_conf, n_corr, n_unver, n_skip = critique_and_repair_chapter(
                chapter_id, chapter_records, violations, qn_source_pages,
                pdf_path, genai_model)
            if n_conf or n_corr or n_unver or n_skip:
                print(f"  [CRITIQUE] {chapter_id}: {n_conf} confirmed (false "
                      f"alarm) | {n_corr} corrected | {n_unver} cannot-verify "
                      f"| {n_skip} skipped")
                stats["critique_confirmed"] = n_conf
                stats["critique_corrected"] = n_corr
                stats["critique_unverifiable"] = n_unver
                stats["critique_skipped"] = n_skip

        # PHASE-1 SPLIT-OUTPUT LAYER (strictly additive). Runs AFTER every
        # existing in-pipeline step (batches, orphans, drain, sweep, retry,
        # rescue, anchorless drop, phantom drop, critique-and-repair) and
        # BEFORE the existing build_final_question loop that writes the
        # master data/questions.jsonl. The split is built from the same
        # chapter_records dict the master file is built from, so the two
        # are guaranteed consistent for this chapter.
        #
        # Phase-1 (observation-only): reconcile_qids grades every record
        # using ONLY printed anchors (no Gemini calls, no behavior change
        # to the existing loop). The Phase-2 plan for the
        # neighbor_run / carry_forward_origin anchors is documented in
        # split_outputs.py and the chapter_completeness.json it emits.
        try:
            reconciled = split_outputs.reconcile_qids(
                chapter_records, qn_source_pages, pdf_path, page_files,
                subject, ch["chapter_no"],
                chapter_anchor_observations=chapter_anchor_observations)
            split_completeness = split_outputs.write_split_outputs(
                chapter_id=chapter_id, subject=subject,
                chapter_no=ch["chapter_no"],
                chapter_records=chapter_records,
                image_files_by_q=image_files_by_q,
                qn_source_pages=qn_source_pages,
                orphans=orphans,
                chapter_unresolved_images=chapter_unresolved_images,
                pdf_path=pdf_path, page_files=page_files,
                reconciled=reconciled,
                output_root=OUTPUT_ROOT)
            n_kept = len(reconciled.get("kept") or {})
            n_unres = len(reconciled.get("unresolved") or {})
            print(f"  [SPLIT] {chapter_id}: {n_kept} graded record(s) "
                  f"({n_unres} unresolved -> unresolved_qids.jsonl) -> "
                  f"data/split/{subject}/{chapter_id}/  "
                  f"({split_completeness['question_records']} questions / "
                  f"{split_completeness['answer_records']} answers / "
                  f"{split_completeness['solution_records']} solutions / "
                  f"{split_completeness['image_manifest_records']} image-manifest rows)")
        except Exception as e:
            # A failure in the split layer MUST NOT affect the master
            # pipeline output. The split is a sidecar; the master
            # data/questions.jsonl rewrite below proceeds unaffected.
            print(f"  [SPLIT] {chapter_id}: split-layer error ({e}) -- "
                  f"master pipeline output unaffected, split files NOT written")

        chapter_rows = []
        for qn, rec in sorted(chapter_records.items(), key=lambda x: x[0]):
            final_q = build_final_question(
                subject, chapter_id, ch["chapter_no"], qn, rec,
                image_files_by_q.get(qn, {"question": [], "solution": []}),
                source_pages=qn_source_pages.get(qn)
            )
            chapter_rows.append(final_q)
        # run-16 CRASH-SAFE COMMIT: the master questions.jsonl is rewritten
        # atomically per chapter (never appended) -- a worker SIGKILL at ANY
        # point leaves the file = the last committed chapter, and a resume
        # can never duplicate rows. Old append-mode deduped only at the end
        # of a full book, so mid-book deaths left duplicate rows behind.
        rewrite_questions_file(questions_path, chapter_id, chapter_rows)
        # per-chapter file: written only NOW, when this chapter has FULLY
        # finished every process -- the batch loop of the NEXT chapter has
        # not started yet.
        write_chapter_file(subject, chapter_id, chapter_rows)

        progress["chapters_done"].append(chapter_id)
        save_state(state)
        # Persist chapters.json incrementally too. main() also writes it at the
        # end, but if we exit early (daily Gemini limit -> sys.exit, crash,
        # redeploy) that final write never happens -- and since completed
        # chapters are in chapters_done, the next run would skip them and
        # they'd be permanently missing from chapters.json.
        chapters_path = DATA_DIR / "chapters.json"
        write_chapters(chapters_path, chapters_out)
        n_no_answer = sum(1 for r in chapter_records.values() if not r.get("correct_option"))
        n_no_solution = sum(1 for r in chapter_records.values() if not r.get("solution_text"))
        n_no_stem = sum(1 for r in chapter_records.values() if not (r.get("question_text") or "").strip())
        n_bad_opts = sum(1 for r in chapter_records.values()
                         if len(r.get("options") or {}) < 4
                         or any(not str(v or "").strip() for v in (r.get("options") or {}).values()))
        # MISSING-STEM VISIBILITY (run-11 RC-3): "0 missing answer / 0 missing
        # solution" previously MASKED stem-less records (ch1 q4/q10, ch2
        # q25/q26 shipped stem-less with those counters at 0).
        print(f"[{subject}] chapter {ch['chapter_no']} ({ch['chapter_title']}) done -> "
              f"{len(chapter_records)} questions ({n_no_answer} missing answer, "
              f"{n_no_solution} missing solution, {n_no_stem} missing stem, "
              f"{n_bad_opts} bad options)")
        stats["missing_stems"] = stats.get("missing_stems", 0) + n_no_stem
        stats["bad_options"] = stats.get("bad_options", 0) + n_bad_opts
        if n_no_solution and chapter_records:
            coverage = 1 - n_no_solution / len(chapter_records)
            if coverage >= SOLUTION_GATE_MIN_SHARE:
                print(f"  [WARN] {n_no_solution} solution(s) still missing although this chapter "
                      f"prints explanations ({coverage:.0%} coverage) -- extraction loss, "
                      f"see data/still_incomplete_after_retry.jsonl; re-run or --recover these pages")
        print(f"[{subject}]   batches: {stats['batches']} | duplicates merged: {stats['duplicates_merged']}"
              f" | conflicts dropped: {stats['conflicts']} | carry-forward used: {stats['carry_used']}"
              f" | carry merges: {stats['carry_merges']}"
              f" | orphans: {stats['orphans_recovered']} recovered, {stats['orphans_remaining']} unresolved"
              f" | foreign-chapter q_nos dropped: {stats.get('foreign_chapter_qno_dropped', 0)}"
              f" | unmatched images: {n_unmatched}"
              f" | rescue: {stats.get('rescue_filled', 0)} filled / {stats.get('rescue_calls', 0)} calls"
              f" | anchorless dropped: {stats.get('anchorless_dropped', 0)}")

        # run-16 BOUNDED MEMORY: drop every cached page render of this
        # chapter (pages are never re-needed), force a GC pass, and report
        # peak RSS so the Railway log shows memory WITHOUT waiting for a
        # kernel SIGKILL to guess. This is the chapter-11 OOM fix: the old
        # unbounded render cache held ~150 full-page PIL renders (~950 MB)
        # by that point.
        clear_render_cache()
        gc.collect()
        try:
            import resource as _resource
            _rss_kb = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
            print(f"  [MEM] chapter {ch['chapter_no']} committed: peak RSS "
                  f"{_rss_kb / 1024.0:.0f} MB, render cache cleared "
                  f"(bounded at {_RENDER_CACHE_MAX})")
        except Exception:
            pass

    # ALL chapters of this subject are complete now -> bundle everything into
    # a subject-named folder (per-chapter files were written as each chapter
    # closed; earlier run's files persist on the volume, so a resumed run
    # still produces the full bundle here).
    build_subject_bundle(subject, chapters_out)

# ============================================================
# TARGETED RECOVERY MODE (--recover plan.json)
# Heals already-written questions.jsonl rows WITHOUT reprocessing whole
# chapters and WITHOUT touching state.json / chapters_done.
# plan.json shape:
#   {"PSY-016": {"pages": [214, 217], "reason": "recitation batch loss"},
#    "PSY-001": {"pages": [17],      "reason": "missing solution for q13"}}
# Pages are TRUE PDF file page numbers (same numbering used by
# orphans.jsonl / unmatched_images.jsonl / temp image filenames).
# ============================================================

def final_q_to_record(q):
    """Reverse build_final_question: folded an existing JSONL row back into a
    merge-ready record (+ its already-owned images for re-emission)."""
    options = {o["id"]: o["text"] for o in (q.get("options") or [])} or None
    correct = q.get("correct_options") or []
    rec = {
        "q_no": int(q["id"].rsplit("-", 1)[-1]),
        "question_text": q["question"]["text"],
        "options": options,
        "correct_option": correct[0] if correct else None,
        "solution_text": q["solution"]["text"],
        "tables": [{"type": t.get("type", "table"), "markdown": t["markdown"]}
                   for t in q["solution"].get("tables", [])],
        "has_figure_in_question": bool(q["question"]["images"]),
        "has_figure_in_solution": bool(q["solution"]["images"]),
        "_prov": {},   # provenance resets on re-import; new merges re-tag
    }
    owned = {"question": [i["file"] for i in q["question"]["images"]],
             "solution": [i["file"] for i in q["solution"]["images"]],
             "option": {}}
    for o in q.get("options") or []:
        oid = str(o.get("id") or "").strip().upper()
        oimgs = [i["file"] for i in o.get("images") or []]
        if oid and oimgs:
            owned["option"][oid] = oimgs
    return rec, owned

RECITATION_RECOVERY_CONTEXT = (
    "RECITATION-SAFE RECOVERY: describe the visible educational content in your own words. "
    "Do not quote or transcribe long passages verbatim; preserve question numbers, answer letters, "
    "and the meaning of explanations. Return the normal JSON schema. "
)

RECOVERY_CONTEXT = (
    "RECOVERY NOTE: these are SELECTED pages from a single chapter, sent to "
    "fill specific extraction gaps. Pages may be non-adjacent and each page "
    "may begin or end mid-flow. Extract everything visible exactly as usual; "
    "if a fragment at a page edge has no visible question number, return it "
    'with "q_no": null as usual. Never invent numbers.'
)

def recover_pages(plan_path):
    plan = json.loads(Path(plan_path).read_text())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()   # real state: quota tracking + failed_pages queue
    # Key pool must be initialised from the loaded state (it stores per-key
    # counters there) BEFORE the first model is built.
    gemini_keys.init(state, MAX_CALLS_PER_DAY)
    model = gemini_keys.track(genai.GenerativeModel(GEMINI_MODEL))

    questions_path = DATA_DIR / "questions.jsonl"
    all_lines = [json.loads(l) for l in
                 questions_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    for chapter_id, spec in plan.items():
        pages = sorted(set(int(p) for p in spec["pages"]))
        subject, chap_str = chapter_id.split("-", 1)
        chapter_no = int(chap_str)
        pdf_cfg = next((c for c in PDFS if c["subject"] == subject), None)
        if not pdf_cfg:
            print(f"[RECOVER] no PDF configured for {subject} -- skipping {chapter_id}")
            continue
        pdf_path = pdf_cfg["path"]
        total_pages = len(PdfReader(pdf_path).pages)
        watermark_id = find_watermark_object_id(pdf_path)

        # rebuild existing chapter rows so recovery MERGES into them
        chapter_lines = [q for q in all_lines if q.get("chapter_id") == chapter_id]
        records, image_files_by_q = {}, {}
        for q in chapter_lines:
            rec, owned = final_q_to_record(q)
            records[rec["q_no"]] = rec
            image_files_by_q[rec["q_no"]] = owned
        print(f"[RECOVER] {chapter_id}: {len(records)} existing rows; "
              f"target pages {pages} ({spec.get('reason', 'no reason given')})")

        # render targets +/- 1 neighbour (continuation context!) at higher DPI
        neighbour = sorted({p for t in pages for p in (t - 1, t, t + 1)
                            if 1 <= p <= total_pages})
        rec_dir = Path(f"/tmp/{subject}_recover_{chapter_no:03d}")
        rec_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["pdftoppm", "-jpeg", "-r", "200",
                        "-f", str(neighbour[0]), "-l", str(neighbour[-1]),
                        pdf_path, str(rec_dir / "page")])
        page_files = sorted(rec_dir.glob("page-*.jpg"))
        pages_imaged = set()
        stats = {"duplicates_merged": 0, "conflicts": 0,
                 "carry_merges": 0, "orphans_recovered": 0,
                 "chapter_id": f"{subject}-{chapter_no:03d}"}
        orphans = []
        unmatched_images = []

        for win_start in range(0, len(page_files), PAGES_PER_GEMINI_CALL):
            batch = page_files[win_start:win_start + PAGES_PER_GEMINI_CALL]
            window_pages = [int(p.stem.split("-")[-1]) for p in batch]
            reset_daily_counter_if_needed(state)
            if quota_exhausted(state):
                print("Daily Gemini call limit reached during recovery. Saving, exiting.")
                save_state(state)
                sys.exit(0)
            try:
                raw = call_gemini_on_pages(model, batch, context=RECOVERY_CONTEXT)
                # quota accounting: recovery's direct calls used to bypass
                # calls_today (only the fallback retries counted) -- a long
                # recovery could overshoot the per-day cap blind.
                note_call(state)
                save_state(state)
            except Exception as e:
                print(f"  [WARN] recovery call failed for {chapter_id} pages "
                      f"{window_pages}: {e}")
                raw = retry_batch_page_by_page(model, batch, state,
                                               ctx={"subject": subject,
                                                    "chapter_no": chapter_no,
                                                    "chapter_id": chapter_id})
                if not raw:
                    continue
            items, _meta = extract_batch_meta(raw)
            # recovery items are provenance-tagged so merge applies the
            # semantic stem guard to anything that looks like solution prose
            # (run-7 hardening #4).
            for it in items:
                if isinstance(it, dict):
                    it["_prov"] = "RECOVER"
            records, skipped = merge_question_records(records, items, stats, fill_only=True)
            for it in skipped:
                orphans.append({"chapter_id": chapter_id, "batch_start": win_start,
                                "pdf_pages": window_pages, "new_pages": window_pages,
                                "carry_q_no": None, "cut_part": None,
                                "last_qn_in_batch": None, "item": it})
            for pf in batch:
                file_page_num = int(pf.stem.split("-")[-1])
                if file_page_num in pages_imaged:
                    continue
                pages_imaged.add(file_page_num)
                imgs = extract_real_images(pdf_path, file_page_num, watermark_id,
                                           subject, ASSETS_DIR / "questions")
                if imgs:
                    rec_leftover = claim_page_images(imgs, pdf_path, file_page_num,
                                                     subject, chapter_no,
                                                     records, image_files_by_q)
                    if rec_leftover:
                        unmatched_images.append({"page": file_page_num, "files": rec_leftover})

        # drain this chapter's queued failed pages (incl. ones recovery itself
        # just queued): crop-ladder second chance, fragments join the orphan
        # pool BEFORE recover_orphans runs -- same order as the production path.
        pending_failed = [e for e in state.get("failed_pages", [])
                          if e.get("chapter_id") == chapter_id]
        if pending_failed:
            records, rec_drain_orphans, healed = drain_failed_pages(
                model, pending_failed, rec_dir, records, state, stats,
                pdf_path=pdf_path)
            orphans.extend(rec_drain_orphans)
            if healed:
                healed_ids = {(e["subject"], e["chapter_no"], e["true_page"]) for e in healed}
                state["failed_pages"] = [e for e in state.get("failed_pages", [])
                                         if (e.get("subject"), e.get("chapter_no"), e.get("true_page"))
                                         not in healed_ids]
                save_state(state)

        orphans = recover_orphans(orphans, records, subject, chapter_no, stats)
        for orph in orphans:
            _append_jsonl(DATA_DIR / "orphans.jsonl", orph)

        # Close the healing loop: without this, a recovery could never fix a
        # TRUNCATED solution (merges are fill-only, and production's sweep-
        # forced re-ask lives only in process_pdf). Detection only -- the
        # sweep's destructive parts are skipped here because the recovery
        # page window is narrower than a full chapter pass.
        forced = {qn for qn, r in records.items()
                  if (r.get("solution_text") or "").strip()
                  and looks_truncated_solution(r["solution_text"],
                                               has_tables=bool(r.get("tables")))}
        targeted_retry(model, page_files, records, state,
                       force_solution_qns=forced, chapter_id=chapter_id,
                       stats=stats)
        for um in unmatched_images:
            rec_leftover = claim_page_images(um["files"], pdf_path, um["page"],
                                             subject, chapter_no,
                                             records, image_files_by_q)
            if rec_leftover:
                _append_jsonl(DATA_DIR / "unmatched_images.jsonl",
                              {"subject": subject, "chapter_id": chapter_id,
                               "page": um["page"], "files": rec_leftover})

        # rewrite questions.jsonl: keep other chapters' rows, replace this one
        others = [q for q in all_lines if q.get("chapter_id") != chapter_id]
        emitted = [build_final_question(subject, chapter_id, chapter_no, qn, rec,
                                        image_files_by_q.get(qn, {"question": [], "solution": []}))
                   for qn, rec in sorted(records.items())]
        out_ids = [q["id"] for q in emitted]
        assert len(out_ids) == len(set(out_ids)), "duplicate ids after recovery"
        # run-16: use the same atomic per-chapter rewrite as the main path so
        # a death during recovery can never leave a half-written file either.
        rewrite_questions_file(questions_path, chapter_id, emitted)
        all_lines = others + emitted  # next chapter's rebuild sees fresh rows

        n_no_solution = sum(1 for r in records.values() if not r.get("solution_text"))
        n_no_answer = sum(1 for r in records.values() if not r.get("correct_option"))
        print(f"[RECOVER] {chapter_id} done -> {len(records)} questions "
              f"({n_no_answer} missing answer, {n_no_solution} missing solution)"
              f" | conflicts dropped: {stats['conflicts']}"
              f" | orphans unresolved: {len(orphans)}")

    print("[RECOVER] all planned chapters processed.")

def rewrite_questions_file(path, chapter_id, chapter_rows):
    """run-16 CRASH-SAFE RESUME: atomically rewrite questions.jsonl so a
    committed chapter is EXACTLY-ONCE in the master file. Reads the existing
    rows, drops any row belonging to this chapter (a partially-appended
    attempt after a worker SIGKILL), appends the fresh rows, dedupes
    keep-LAST by id, and renames a temp file over the master. ANY death
    point (SIGKILL / redeploy / daily-quota exit) leaves the file equal to
    the last committed chapter -- a resume can never duplicate records.

    This replaces the old append-mode design, where main() deduped only at
    the very end of a full book; a mid-book death after a re-run left
    duplicate rows behind until a COMPLETE run happened to finish."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = []
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                prior.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    prior = [r for r in prior if r.get("chapter_id") != chapter_id]
    rows = prior + list(chapter_rows)
    by_id = {}
    for r in rows:
        by_id[r.get("id")] = r          # keep LAST per id (newest wins)
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for rid in by_id:
            fh.write(json.dumps(by_id[rid], ensure_ascii=False) + "\n")
    os.replace(tmp, path)               # atomic on POSIX
    return len(rows) - len(by_id)


def _dedupe_questions_by_id(path):
    """questions.jsonl is append-only, and surgically re-running a chapter
    (removing its id from chapters_done) appends its rows AGAIN. At 20-book
    scale that accumulates duplicate ids the app renders twice. Rewrite the
    file keeping the LAST row per id (the newest extraction wins). Returns
    the number of duplicate rows removed."""
    if not path.exists():
        return 0
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows = {}
    n_dups = 0
    for ln in lines:
        try:
            rid = json.loads(ln).get("id")
        except json.JSONDecodeError:
            continue
        if rid in rows:
            n_dups += 1
        rows[rid] = ln  # last wins (newest extraction)
    if n_dups:
        path.write_text("".join(rows[rid] + "\n" for rid in rows), encoding="utf-8")
    return n_dups


def build_auto_recovery_plan():
    """Assemble a recover_pages plan from the run's ledgers, so a whole
    book (or 20) can be healed with ONE command instead of hand-writing
    plan.json:
      * still_incomplete_after_retry.jsonl -- every gap's pages are located
        in the PDF text layer (question stem and/or 'Solution to Question
        N:' header);
      * orphans.jsonl -- their pdf_pages;
      * unmatched_images.jsonl -- their pages.
    Returns {chapter_id: {"pages": [...], "reason": "..."}}."""
    plan = {}
    pdf_by_subject = {c["subject"]: c for c in PDFS}

    def add(chapter_id, pages, reason):
        if not pages:
            return
        entry = plan.setdefault(chapter_id, {"pages": [], "reasons": []})
        entry["pages"] = sorted(set(entry["pages"] + [int(p) for p in pages]))
        entry["reasons"].append(reason)

    def pages_for_questions(subject, chapter_no, qns):
        cfg = pdf_by_subject.get(subject)
        if not cfg or not qns:
            return []
        total = len(PdfReader(cfg["path"]).pages)
        chs = compute_page_ranges(extract_toc_chapters(cfg["path"]),
                                  cfg["page_offset"], total)
        ch = next((c for c in chs if c["chapter_no"] == chapter_no), None)
        if not ch:
            return []
        found = []
        qns = set(qns)
        header_re = re.compile(r"Solution\s+to\s+Question\s+(\d{1,3})", re.IGNORECASE)
        for page_no in range(ch["file_start"], ch["file_end"] + 1):
            text = pdftotext_page(cfg["path"], page_no)
            if not text.strip():
                continue
            if any(re.search(r"(?m)^\s*(?:Q(?:uestion)?\s*[.:]?\s*)?%d\s*[.:\-–)]" % qn, text)
                   for qn in qns):
                found.append(page_no)
                continue
            if any(int(m.group(1)) in qns for m in header_re.finditer(text)):
                found.append(page_no)
        return found

    # 1) still-incomplete records
    inc_path = DATA_DIR / "still_incomplete_after_retry.jsonl"
    if inc_path.exists():
        by_chapter = {}
        for ln in inc_path.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            cid, qn = e.get("chapter_id"), e.get("q_no")
            if not cid or qn is None:
                continue
            by_chapter.setdefault(cid, set()).add(int(qn))
        for cid, qns in by_chapter.items():
            try:
                subject, chap_str = cid.split("-", 1)
                chapter_no = int(chap_str)
            except (ValueError, TypeError):
                continue
            pages = pages_for_questions(subject, chapter_no, qns)
            add(cid, pages, f"still incomplete q{','.join(map(str, sorted(qns)))} "
                            f"({len(pages)} page(s) located)")
    # 2) unresolved orphans -> their pdf_pages
    orph_path = DATA_DIR / "orphans.jsonl"
    if orph_path.exists():
        for ln in orph_path.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            cid = e.get("chapter_id")
            pages = e.get("pdf_pages") or e.get("new_pages") or []
            if cid and pages:
                add(cid, pages, "unresolved orphan fragment")
    # 3) unmatched images -> their pages
    um_path = DATA_DIR / "unmatched_images.jsonl"
    if um_path.exists():
        for ln in um_path.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            cid, page = e.get("chapter_id"), e.get("page")
            if cid and page:
                add(cid, [page], "unclaimed image(s)")
    return {cid: {"pages": e["pages"], "reason": "; ".join(e["reasons"])}
            for cid, e in plan.items()}


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    state = load_state()
    gemini_keys.init(state, MAX_CALLS_PER_DAY)
    model = gemini_keys.track(genai.GenerativeModel(GEMINI_MODEL))

    reset_daily_counter_if_needed(state)

    chapters_path = DATA_DIR / "chapters.json"
    chapters_out = json.loads(chapters_path.read_text()) if chapters_path.exists() else []

    questions_path = DATA_DIR / "questions.jsonl"
    # run-16: per-chapter atomic rewrite inside process_pdf -- no append
    # handle, so a SIGKILL can never leave a half-appended chapter.
    for pdf_cfg in PDFS:
        process_pdf(pdf_cfg, state, model, chapters_out, questions_path)

    write_chapters(chapters_path, chapters_out)
    save_state(state)
    # surgical re-runs append duplicate rows; keep the newest per id.
    n_dups = _dedupe_questions_by_id(questions_path)
    if n_dups:
        print(f"Deduplicated {n_dups} stale duplicate row(s) from questions.jsonl "
              f"(newest extraction kept).")
    print("All done (or paused at daily limit -- just re-run this script to resume).")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--recover":
        # targeted healing of already-written rows, e.g.:
        #   python3 qbank_pipeline.py --recover recovery_plan.json
        recover_pages(sys.argv[2] if len(sys.argv) > 2 else "recovery_plan.json")
    elif len(sys.argv) > 1 and sys.argv[1] == "--auto-recover":
        # one-command whole-book heal: build the plan from the run ledgers
        # (still-incomplete, orphans, unmatched images) and run it.
        plan = build_auto_recovery_plan()
        if not plan:
            print("[AUTO-RECOVER] no gaps found in the ledgers -- nothing to heal")
            sys.exit(0)
        plan_path = DATA_DIR / "auto_recovery_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        print(f"[AUTO-RECOVER] plan -> {plan_path} "
              f"({len(plan)} chapter(s), "
              f"{sum(len(e['pages']) for e in plan.values())} page(s))")
        recover_pages(str(plan_path))
    else:
        main()
