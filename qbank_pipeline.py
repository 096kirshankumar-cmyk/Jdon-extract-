#!/usr/bin/env python3
"""
QBank PDF -> JSON extraction pipeline.
Run this on your own machine / Railway (needs: poppler-utils, pypdf, Pillow,
google-generativeai, requests). Designed to survive the daily Gemini
free-tier limit by checkpointing progress and resuming across multiple runs
(e.g. via a daily cron job / Railway scheduled task).

EXTRACTION METHOD (single, non-negotiable): the boundary-phased engine in
boundary_phased.py (Steps 0-8: zone the chapter -> phase Q/A/key/S with
verify loops -> whole-chapter cross-check -> LOCK -> write). The old
multi-pass architecture (process_pdf, section windows, carry-forward
merging, targeted-retry/rescue/critique passes, --recover healers) was
REMOVED on 2026-08-22; this module now carries only the shared
infrastructure both the engine and the review/validation layer need:
state/quota, TOC page ranges, PDF text/OCR anchors, watermark gates,
deterministic image claiming + ownership ledger, export gate, final-row and
split writers. main() delegates to the engine; app.py calls it for the
one-chapter smoke test. See EXTRACTION_V2.md.

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
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import datetime
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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
TOC_SCAN_LAST_PAGE = 8          # run-22: how many leading pages to scan for
                                 # the contents table. Was effectively 3, which
                                 # truncated the MARROW Anatomy book at chapter
                                 # 42 of 63 -- a third of the book silently
                                 # never processed. Over-scanning is safe:
                                 # _longest_toc_run() discards anything that
                                 # is not a contiguous chapter sequence.
                                 # NOTE: this is only the FIRST window. The
                                 # scan now GROWS automatically -- see
                                 # TOC_SCAN_MAX_PAGE and extract_toc_chapters.
TOC_SCAN_MAX_PAGE = 40          # run-23: hard ceiling for the growing TOC
                                 # scan. A fixed 8-page window is a book-
                                 # specific constant in disguise: a 110-chapter
                                 # book needs ~9-11 contents pages, and a book
                                 # with a long front matter (title, copyright,
                                 # foreword, contributors) can push the table
                                 # well past page 8. Both cases truncate the
                                 # chapter list SILENTLY, which is the exact
                                 # class of bug run-22 fixed for 63 chapters.
                                 # 40 pages of pdftotext is ~0.2s and costs no
                                 # API calls, so the ceiling is generous.
TOC_SCAN_GROW_STEP = 8          # widen by this many pages per attempt.
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
                                 # Without pacing, back-to-back phase calls bust
                                 # the RPM window instantly -> 429 bursts. 5s
                                 # spacing caps a run at 12 RPM: bursts disappear
                                 # and the 65s backoff ladder stops firing.
_last_call_ts = 0.0



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

# AUDIT-FIX: subject length was hard-coded to 3 here while the dashboard
# accepts any uppercase code -- a 4-letter subject passed the pipeline and
# build_final_question then dropped EVERY image ref with only a print().
IMG_PATH_RE = re.compile(r"^[A-Z]{2,5}/[A-Z]{2,5}-\d{3}-\d{3}_[A-Z]+(_[A-Z])?_\d{2}\.webp$")

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

# ---- Daily-quota day boundary (run-24 fix) ----------------------------
# Google resets Gemini RPD at MIDNIGHT US/PACIFIC (08:00 UTC in winter,
# 07:00 UTC in summer -- it follows US DST), NOT at UTC midnight and
# certainly not at the container's local midnight.
#
# `time.strftime("%Y-%m-%d")` used the container clock, so on a UTC host the
# pipeline rolled its per-key counters over ~8 hours EARLY. In that window it
# believed every key was fresh while Google still counted them against the
# previous day: the very first call 429'd, the key was parked `exhausted`,
# and the pool burned through all 6 keys for nothing.
#
# We stamp the day in US/Pacific so our rollover lines up with Google's.
# QUOTA_RESET_TZ can override it if Google ever moves the boundary.
QUOTA_RESET_TZ = os.environ.get("QUOTA_RESET_TZ", "America/Los_Angeles").strip()

def _quota_tz():
    """Return the tzinfo Google resets RPD on, or None if unavailable."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(QUOTA_RESET_TZ)
    except Exception:
        # No tzdata in the image (slim containers often ship without it).
        # Fall back to a fixed UTC-8 offset: worst case during US summer we
        # roll over one hour late, which is safe (late = we under-count, so
        # we never think a spent key is fresh).
        try:
            return datetime.timezone(datetime.timedelta(hours=-8))
        except Exception:
            return None

def today_stamp():
    """Calendar date in Google's quota-reset timezone (US/Pacific)."""
    tz = _quota_tz()
    if tz is None:
        return time.strftime("%Y-%m-%d")          # last-resort legacy path
    return datetime.datetime.now(tz).strftime("%Y-%m-%d")

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



# ============================================================
# STEP 1: parse the TOC to auto-discover chapters + page ranges
# (TOC pages have clean, non-garbled text -- confirmed reliable to
# pdftotext even on PDFs where body-page text is broken/garbled)
# ============================================================

def _scan_toc_candidates(pdf_path, first_page, last_page):
    """pdftotext one window and return every "<n> <title> <page>" line in it."""
    text = subprocess.run(
        ["pdftotext", "-f", str(first_page), "-l", str(last_page),
         "-layout", pdf_path, "-"],
        capture_output=True, text=True, timeout=120   # AUDIT-FIX: never hang the single worker
    ).stdout

    candidates = []
    # Matches lines like: "12   Bipolar and Related Disorders   160"
    for line in text.splitlines():
        m = re.match(r"^\s*(\d{1,3})\s+(.*?)\s+(\d{1,4})\s*$", line)
        if m:
            no, title, page = m.groups()
            title = title.strip()
            if len(title) < 3:
                continue
            candidates.append({
                "chapter_no": int(no),
                "chapter_title": title,
                "start_printed_page": int(page),
            })
    return candidates


def extract_toc_chapters(pdf_path, toc_page_range=None):
    """
    Returns [{"chapter_no": int, "chapter_title": str, "start_printed_page": int}, ...]

    RUN-23 GROWING SCAN: pass toc_page_range to pin the window (tests do this).
    Left as None, the scan starts at TOC_SCAN_LAST_PAGE and widens by
    TOC_SCAN_GROW_STEP until the detected chapter count STOPS growing, or
    TOC_SCAN_MAX_PAGE is reached. A fixed window is a book-specific constant
    in disguise -- 8 pages happens to fit a 63-chapter book with short front
    matter, but a 110-chapter book needs ~9-11 contents pages and would be
    truncated just as silently as run-22's 42-of-63. Widening is free
    (pdftotext, no API calls) and safe (_longest_toc_run rejects non-contiguous
    body-text noise), so the scan keeps growing while it is still finding
    chapters and stops as soon as it is not.

    RUN-22 SILENT CHAPTER TRUNCATION: the default range was (1, 3), which on
    the MARROW ED8 Anatomy book found only chapters 1-42 -- its contents table
    spans FIVE pages, so chapters 43-63 (printed pages 800-1217, a third of
    the book) were never detected. Nothing failed: the old process_pdf simply
    never saw those chapters, so a full-book run silently produced no
    questions for them and no gate flag could fire, because the gate only
    reports on chapters it knows about.

    Scanning wider is not free -- past the real contents table, ordinary body
    pages contain lines that match the same "<n> <title> <page>" shape and
    would inject phantom chapters. So the scan is widened AND the result is
    validated: keep only the longest run that starts at chapter 1 and has
    strictly increasing chapter numbers with non-decreasing start pages. A
    real contents table satisfies this; scattered body-text matches do not.
    """
    if toc_page_range is not None:
        # Explicit window -- honour it exactly (used by tests and callers that
        # already know where the contents table is).
        return _longest_toc_run(
            _scan_toc_candidates(pdf_path, toc_page_range[0], toc_page_range[1]))

    last = TOC_SCAN_LAST_PAGE
    best = _longest_toc_run(_scan_toc_candidates(pdf_path, 1, last))

    while last < TOC_SCAN_MAX_PAGE:
        wider = min(last + TOC_SCAN_GROW_STEP, TOC_SCAN_MAX_PAGE)
        found = _longest_toc_run(_scan_toc_candidates(pdf_path, 1, wider))
        if len(found) <= len(best):
            # Widening stopped paying. The contents table has ended; anything
            # further is body text, which _longest_toc_run already rejects.
            break
        print(f"  [TOC] scan widened to page {wider}: "
              f"{len(best)} -> {len(found)} chapters")
        best, last = found, wider

    if best and len(best) != best[-1]["chapter_no"]:
        # Defensive: _longest_toc_run guarantees a 1..N run, so this should be
        # unreachable. Loud rather than silent if that ever changes.
        print(f"  [TOC] WARNING: kept {len(best)} chapters but the last is "
              f"numbered {best[-1]['chapter_no']} -- non-contiguous run")
    if best and last >= TOC_SCAN_MAX_PAGE:
        print(f"  [TOC] WARNING: scan hit the {TOC_SCAN_MAX_PAGE}-page ceiling "
              f"with {len(best)} chapters; raise TOC_SCAN_MAX_PAGE if this "
              f"book's contents table is longer")
    return best


def _longest_toc_run(candidates):
    """Keep the longest chapter run starting at 1 with strictly increasing
    chapter numbers and non-decreasing start pages (see extract_toc_chapters).

    Duplicates are tolerated: a contents table repeated in a per-section
    listing yields the same chapter twice, and the FIRST occurrence wins.
    Anything that breaks the sequence -- an out-of-order number, a page that
    jumps backwards -- ends the run, which is what keeps body-text noise from
    being accepted as chapters.
    """
    run = []
    for c in candidates:
        if not run:
            if c["chapter_no"] == 1:
                run.append(c)
            continue
        prev = run[-1]
        if c["chapter_no"] == prev["chapter_no"]:
            continue                      # duplicate listing -- first wins
        if (c["chapter_no"] == prev["chapter_no"] + 1
                and c["start_printed_page"] >= prev["start_printed_page"]):
            run.append(c)
    return run

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

# A book may contain MORE THAN ONE XObject id for the same repeating
# watermark.  MIC is the production example: object 1707 is used on pages
# 7-118, while object 2197 (the same "Sold by @Itachibot" full-page overlay)
# is used on 503 other pages.  The old first-30-page vote returned only 1707,
# so 2197 was extracted and then deterministically attached to many questions.
# Cache by immutable file identity because recovery/smoke-test modes may ask
# for the filter repeatedly for the same large PDF.
_WATERMARK_IDS_CACHE = {}


def _sparse_light_page_image(im):
    """Conservative visual proof for a watermark/background candidate.

    A candidate must already be repeated and drawn across almost the entire
    PDF page.  This extra test protects genuine repeated diagrams/icons: the
    image must also be overwhelmingly white with only sparse light-grey ink,
    like the MIC seller watermark.  A normal scan/photo/diagram is retained.
    """
    try:
        gray = im.convert("L")
        gray.thumbnail((128, 128))
        hist = gray.histogram()
        total = max(sum(hist), 1)
        mean = sum(i * n for i, n in enumerate(hist)) / total
        below_245 = sum(hist[:245]) / total
        below_180 = sum(hist[:180]) / total
        return mean >= 245.0 and below_245 <= 0.15 and below_180 <= 0.03
    except Exception:
        return False


def _object_is_full_page(pdf_path, reader, obj_id, pages):
    """True only when obj_id is drawn over >=80% of page width AND height.

    Frequency alone is unsafe: a publisher logo or a repeated clinical figure
    can legitimately occur many times.  Drawn geometry makes the filter apply
    only to page backgrounds/overlays, never ordinary question figures.
    """
    for page_no in list(pages)[:3]:
        try:
            pos = (_image_positions_raw(pdf_path, page_no) or {}).get(obj_id)
            if pos is None:
                continue
            left, bottom, right, top = _rect_from_position(pos)
            page = reader.pages[page_no - 1]
            page_w = float(page.mediabox.width)
            page_h = float(page.mediabox.height)
            if (abs(right - left) >= 0.80 * page_w
                    and abs(top - bottom) >= 0.80 * page_h):
                return True
        except Exception:
            continue
    return False


def find_watermark_object_ids(pdf_path):
    """Return every deterministically-proven watermark/background object id.

    Detection has THREE independent gates so real image extraction is not
    affected:
      1. the same XObject id occurs on many pages across the WHOLE book;
      2. it is drawn over at least 80% of both page dimensions;
      3. its pixels are a sparse, overwhelmingly white/light-grey overlay.

    Scanning the whole object table is zero-token and cheap (about a second on
    the supplied 615-page MIC PDF).  It is necessary because a watermark id
    can change after the first section, outside the old 30-page sample.
    """
    path = Path(pdf_path)
    try:
        st = path.stat()
        cache_key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    except OSError:
        cache_key = (str(path), None, None)
    cached = _WATERMARK_IDS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)
    counts = {}
    pages_by_id = {}
    first_use = {}
    for page_no, page in enumerate(reader.pages, 1):
        seen_on_page = set()
        for name, ref in _page_xobjects(page).items():
            obj = _resolve(ref)
            if obj.get("/Subtype") != "/Image":
                continue
            obj_id = getattr(ref, "idnum", None)
            if obj_id is None or obj_id in seen_on_page:
                continue
            seen_on_page.add(obj_id)
            counts[obj_id] = counts.get(obj_id, 0) + 1
            pages_by_id.setdefault(obj_id, []).append(page_no)
            first_use.setdefault(obj_id, (page_no, name, obj))

    # 5% of a short/medium book; capped at 20 pages so section-specific
    # watermarks in a very large book are still detected.  Geometry + visual
    # gates below prevent a repeated real figure from being removed.
    min_repeat = max(2, min(20, (total_pages + 19) // 20))
    watermark_ids = set()
    for obj_id, count in counts.items():
        if count < min_repeat:
            continue
        if not _object_is_full_page(pdf_path, reader, obj_id,
                                    pages_by_id.get(obj_id, [])):
            continue
        page_no, name, obj = first_use[obj_id]
        try:
            im = reader.pages[page_no - 1].images[name].image
        except Exception:
            im = _decode_image_fallback(obj)
        if im is not None and _sparse_light_page_image(im):
            watermark_ids.add(obj_id)

    result = frozenset(watermark_ids)
    _WATERMARK_IDS_CACHE[cache_key] = result
    return result


def _normalise_watermark_ids(value):
    """Accept the new set filter and the legacy single integer argument."""
    if value is None:
        return frozenset()
    if isinstance(value, int):
        return frozenset({value})
    try:
        return frozenset(int(v) for v in value)
    except (TypeError, ValueError):
        return frozenset()

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

# A single printed figure is very often stored as several abutting JPEG
# XObjects (the typesetter's image slicer cuts one photo into horizontal or
# vertical strips). Two drawn rects whose edges are within this many points
# of each other -- and which overlap on the perpendicular axis -- are treated
# as slices of ONE figure and stitched back together before saving.
IMAGE_SLICE_GAP_PT = 3.0


def _rect_from_position(pos):
    """(y, x, draw_idx, w, h) from image_positions_on_page -> normalised
    (left, bottom, right, top) in PDF user space. The cm scale can be
    negative (flipped placement), so fold that into the corners."""
    y, x, _draw_idx, w, h = pos
    left, right = (x, x + w) if w >= 0 else (x + w, x)
    bottom, top = (y, y + h) if h >= 0 else (y + h, y)
    return (float(left), float(bottom), float(right), float(top))


def _rect_contains(outer, inner, pad=1.0):
    return (outer[0] <= inner[0] + pad and outer[1] <= inner[1] + pad
            and outer[2] >= inner[2] - pad and outer[3] >= inner[3] - pad)


def _rect_area(r):
    return max(r[2] - r[0], 0.0) * max(r[3] - r[1], 0.0)


def _rects_are_slices(r1, r2, gap=IMAGE_SLICE_GAP_PT):
    """True if r1 and r2 look like two slices of one figure: they touch (or
    nearly touch) along one axis while substantially overlapping on the
    other. Pure side-by-side figures with a real gutter between them stay
    separate because the gutter is far wider than `gap`."""
    # A backdrop that swallows the other rect (page watermark, coloured panel,
    # figure frame) is NOT a slice of it -- without this, one full-page image
    # unions every figure on the page into a single bogus group.
    a1, a2 = _rect_area(r1), _rect_area(r2)
    if a1 > 0 and a2 > 0:
        big, small = (r1, r2) if a1 >= a2 else (r2, r1)
        if _rect_contains(big, small) and max(a1, a2) >= 2.5 * min(a1, a2):
            return False
    l1, b1, r1x, t1 = r1
    l2, b2, r2x, t2 = r2
    # overlap (positive) or gap (negative) on each axis
    x_ov = min(r1x, r2x) - max(l1, l2)
    y_ov = min(t1, t2) - max(b1, b2)
    if x_ov < -gap or y_ov < -gap:
        return False  # separated on both/either axis by more than the tolerance
    w_min = min(r1x - l1, r2x - l2)
    h_min = min(t1 - b1, t2 - b2)
    # stacked vertically: edges meet in y, and they share most of their width
    if y_ov <= gap and x_ov >= 0.6 * max(w_min, 1e-6):
        return True
    # side by side horizontally: edges meet in x, and they share most of their height
    if x_ov <= gap and y_ov >= 0.6 * max(h_min, 1e-6):
        return True
    # genuinely overlapping tiles (slicers sometimes overlap by a pixel)
    if x_ov > gap and y_ov > gap:
        return True
    return False


def _group_slice_rects(rects):
    """Union-find over {key -> rect}; returns a list of key-lists, each list
    being one printed figure. Keys with no rect are returned as singletons."""
    keys = list(rects.keys())
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            if _rects_are_slices(rects[keys[i]], rects[keys[j]]):
                union(keys[i], keys[j])
    groups = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    return list(groups.values())


def _slice_reading_key(rect):
    """Reading order for slices of one figure: top edge first, then left.
    The top edge is QUANTIZED to whole points because slicers emit strips
    whose tops differ by ~0.1 pt of rounding -- an exact sort would then pick
    the right-hand strip as the group's lead, and the saved filename (and so
    every geometry lookup keyed off it) would flip between runs."""
    return (-round(rect[3]), rect[0])


def _group_lead(group, rects):
    """The slice whose object id names the stitched file."""
    return sorted(group, key=lambda k: _slice_reading_key(rects[k]))[0]


def _stitch_slices(members, rects):
    """Paste every slice onto one canvas at its page position.
    `members` = [(key, PIL.Image), ...] all belonging to one figure.
    Returns the stitched PIL image (RGB)."""
    if len(members) == 1:
        return members[0][1]
    lefts = [rects[k][0] for k, _ in members]
    bottoms = [rects[k][1] for k, _ in members]
    rights = [rects[k][2] for k, _ in members]
    tops = [rects[k][3] for k, _ in members]
    u_l, u_b, u_r, u_t = min(lefts), min(bottoms), max(rights), max(tops)
    u_w, u_h = max(u_r - u_l, 1e-6), max(u_t - u_b, 1e-6)
    # keep the sharpest slice's resolution: points -> pixels scale
    ppp_x = max((im.width / max(rects[k][2] - rects[k][0], 1e-6))
                for k, im in members)
    ppp_y = max((im.height / max(rects[k][3] - rects[k][1], 1e-6))
                for k, im in members)
    cw, ch = max(int(round(u_w * ppp_x)), 1), max(int(round(u_h * ppp_y)), 1)
    canvas = Image.new("RGB", (cw, ch), (255, 255, 255))
    for k, im in members:
        l, b, r, t = rects[k]
        tw = max(int(round((r - l) * ppp_x)), 1)
        th = max(int(round((t - b) * ppp_y)), 1)
        tile = im.convert("RGB")
        if (tile.width, tile.height) != (tw, th):
            tile = tile.resize((tw, th), Image.LANCZOS)
        px = int(round((l - u_l) * ppp_x))
        py = int(round((u_t - t) * ppp_y))  # PDF y grows up, image y grows down
        canvas.paste(tile, (max(px, 0), max(py, 0)))
    return canvas


def hash_owned_image_files(dir_path):
    """Content hashes (pixel-level sha1) of the ALREADY-OWNED images in a
    subject's asset dir. AUDIT-FIX (recovery/re-run duplicates): recovery
    and interrupted-chapter re-runs re-extract the same pages; with no
    byte-level identity anywhere, the same figure attached twice
    (QID_Q_01 + QID_Q_02). Temp-named leftovers are deliberately EXCLUDED --
    they were never claimed, so re-extraction must still return them for
    claiming. Only final (claimed) names block a duplicate."""
    out = set()
    d = Path(dir_path)
    if not d.exists():
        return out
    for f in d.glob("*.webp"):
        if _TEMP_IMG_NAME_RE.match(f.name):
            continue                       # unclaimed temp file: stays claimable
        try:
            out.add(hashlib.sha1(f.read_bytes()).hexdigest())
        except OSError:
            continue
    return out


def _iter_page_image_xobjects(page):
    """Yield (name, ref, obj) for EVERY image XObject drawable on the page,
    INCLUDING images nested inside Form XObjects (cycle-guarded by object
    id). AUDIT-FIX: extract_real_images previously read only page-level
    /XObject entries, so Form-wrapped figures were silently never extracted
    -- a whole class of missing question_images with zero warnings.
    """
    seen_forms = set()

    def _walk_res(xobjs):
        for name, ref in xobjs.items():
            obj = _resolve(ref)
            sub = obj.get("/Subtype")
            if sub == "/Image":
                yield name, ref
            elif sub == "/Form":
                fid = getattr(ref, "idnum", None)
                if fid is not None and fid in seen_forms:
                    continue
                if fid is not None:
                    seen_forms.add(fid)
                fres = _resolve(obj.get("/Resources"))
                if fres:
                    fx = _resolve(fres.get("/XObject"))
                    if fx:
                        yield from _walk_res(fx)

    yield from _walk_res(_page_xobjects(page))


def extract_real_images(pdf_path, file_page, watermark_ids, subject, out_dir,
                        skip_hashes=None):
    """
    Extracts every printed FIGURE on file_page EXCEPT deterministically-proven
    watermark objects. `watermark_ids` accepts the new id set or a legacy
    single integer. Returns saved relative paths ("SUBJECT/filename.webp") --
    exactly one entry per figure (no duplicates, no watermarks).

    NOTE (run-25): one printed figure is frequently stored as several abutting
    image XObjects. Saving one file per XObject produced N fragments of the
    same photo, which the attribution pass then scattered across N different
    questions (observed: DER p11's infant photo split into 3 files, all three
    attributed to q15 while its real owner q22 got none). Slices are now
    grouped by their drawn geometry and stitched back into a single image
    before saving, so downstream sees one figure = one file.
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
    watermark_ids = _normalise_watermark_ids(watermark_ids)
    (out_dir / subject).mkdir(parents=True, exist_ok=True)
    seen_ids = set()  # some PDFs alias the SAME image object under two XObject
                       # names on one page -> would return the same path twice,
                       # and the second rename in process_pdf crashes with
                       # FileNotFoundError (observed in prod on PSY p264)
    seen_name_objs = {}  # name -> id(obj): a no-idnum name reused for a
                         # DIFFERENT object (two Form XObjects each drawing
                         # their own /Im1) must not drop the second figure
    decoded = {}   # dedupe_key -> PIL image
    order = {}     # dedupe_key -> position in the XObject dict (fallback order)
    for name, ref in _iter_page_image_xobjects(page):
        obj = _resolve(ref)
        if obj.get("/Subtype") != "/Image":
            continue
        obj_id = getattr(ref, "idnum", None)
        dedupe_key = obj_id if obj_id is not None else str(name)
        if dedupe_key in seen_ids:
            continue  # alias of an image already saved from this page
        if obj_id is None:
            prev_obj = seen_name_objs.get(str(name))
            if prev_obj is not None and prev_obj is not id(obj):
                # same name, different object -> real second figure, not an
                # alias. Keep it under a uniquified key so it is not silently
                # dropped (it will need the model/positional passes; it has no
                # content-stream position under this dup name).
                dedupe_key = f"{name}#dup"
                n = 2
                while dedupe_key in seen_ids:
                    dedupe_key = f"{name}#dup{n}"
                    n += 1
            seen_name_objs[str(name)] = id(obj)
        seen_ids.add(dedupe_key)
        if obj_id in watermark_ids:
            continue  # proven watermark/background -- never save as a figure
        # Decode exactly THIS image object. NOTE: don't shell out to
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
        decoded[dedupe_key] = im
        order[dedupe_key] = len(order)
    if not decoded:
        return []

    # --- group slices of the same printed figure by drawn geometry ---------
    try:
        raw_pos = _image_positions_raw(pdf_path, file_page) or {}
    except Exception as e:
        print(f"  [WARN] geometry unavailable on page {file_page} ({e}) -- "
              f"saving one file per image object")
        raw_pos = {}
    rects = {k: _rect_from_position(p) for k, p in raw_pos.items() if k in decoded}
    placed = [k for k in decoded if k in rects]
    unplaced = [k for k in decoded if k not in rects]  # no geometry -> standalone
    groups = _group_slice_rects({k: rects[k] for k in placed}) if placed else []
    groups += [[k] for k in unplaced]

    def _group_sort_key(g):
        # reading order: highest top edge first, then leftmost
        gr = [rects[k] for k in g if k in rects]
        if gr:
            return (-max(r[3] for r in gr), min(r[0] for r in gr), 0)
        return (float("inf"), 0.0, min(order[k] for k in g))

    groups.sort(key=_group_sort_key)

    for g in groups:
        g_sorted = sorted(
            g, key=lambda k: _slice_reading_key(rects[k]) if k in rects
            else (float("inf"), order[k]))
        members = [(k, decoded[k]) for k in g_sorted]
        if len(members) > 1:
            try:
                im = _stitch_slices(members, rects)
            except Exception as e:
                print(f"  [WARN] slice stitch failed on page {file_page} "
                      f"({e}) -- falling back to the largest slice")
                im = max((m[1] for m in members), key=lambda i: i.width * i.height)
            print(f"  [slices] page {file_page}: merged {len(members)} image "
                  f"objects into 1 figure ({[str(k) for k in g_sorted]})")
        else:
            im = members[0][1]
        if im.size[0] * im.size[1] < 5000:
            continue  # skip tiny noise images
        stem_key = g_sorted[0]
        stem = stem_key if isinstance(stem_key, int) else str(stem_key).strip("/")
        fname = f"{subject}-p{file_page}-{stem}.webp"
        rel_path = f"{subject}/{fname}"
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "WEBP", quality=95)
        blob = buf.getvalue()
        if skip_hashes and hashlib.sha1(blob).hexdigest() in skip_hashes:
            # AUDIT-FIX: identical figure bytes are already owned under a
            # final name (earlier run / recovery / resume) -- writing a fresh
            # temp copy would let the claimer attach the SAME figure twice.
            print(f"  [IMG] page {file_page}: figure bytes already owned on "
                  f"disk -- skipping duplicate extraction of {fname}")
            continue
        (out_dir / subject / fname).write_bytes(blob)
        saved.append(rel_path)
    return saved

# ============================================================
# STEP 3: Gemini call — page images in, structured JSON out
# ============================================================


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












                                  # solution = the "stem" is really solution
                                  # prose (cross-field contamination class)


















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
















def pdftotext_page(pdf_path, true_page):
    try:
        out = subprocess.run(["pdftotext", "-f", str(true_page), "-l", str(true_page),
                              "-layout", str(pdf_path), "-"], capture_output=True, text=True,
                             timeout=120)   # AUDIT-FIX: bounded
    except subprocess.TimeoutExpired:
        return ""
    return out.stdout or ""






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
        m = QSTEM_HEADING_RE.match(line)
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
        # AUDIT-FIX B1: "below the first solution header" means the TOPMOST
        # solution header (max y); the old min() used the LOWEST, letting
        # numbered list items inside solution prose survive as question
        # anchors (they then stole figures onto the wrong slot).
        first_sol_y = max(y for _k, _q, y in ss)
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
    kind, qn = owner[0], owner[1]
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
    for m in QSTEM_HEADING_MULTILINE_RE.finditer(text):
        qn = int(m.group(1))
        if qn in chapter_records:
            found.add(qn)
    return sorted(found)










def _transient_gemini_err(err_text):
    t = err_text.lower()
    return ("500" in t or "503" in t or "internal error" in t
            or "high demand" in t or "unavailable" in t
            or "deadline exceeded" in t or "deadline_exceeded" in t)







# ============================================================
# FEATURE 2 — carry-forward context (Gemini's API is stateless:
# continuity must be injected manually into every new request)
# ============================================================




SOLUTION_TO_Q_RE = re.compile(r"Solution to Question\s+(\d{1,3})", re.IGNORECASE)

# --- run-22 (D1): ONE definition of the printed question-stem heading -------
# The terminator after the number used to be [.:\-–)] and was copy-pasted into
# SIX places. Ch. 38 p670 prints "Question 13:" but tesseract read it as
#     Question 13, ~\
# -- the colon came back as a COMMA. No branch matched, so q13's four options
# and its answer were never bound to the record, while its stem and solution
# arrived from other passes. The result LOOKS like a complete question but is
# silently missing options+answer, which is worse than a clean miss: the
# export gate only caught it as "bad_options", and the [CRITIQUE] pass even
# concluded "question 13 is not present anywhere in the text" (it is).
#
# Widened terminator class, one shared source of truth:
#   , ;  -> OCR mangles ':' into these constantly
#   ·    -> middot, another frequent ':' misread
#   ]    -> "13]" seen in some scans
# Kept anchored at line start and still requiring SOME terminator, so prose
# like "in 13 patients" cannot match. A stray extra match here is cheap (one
# redundant Q-pass on an already-covered page); a MISSED one loses real data.
QSTEM_TERMINATORS = r".:;,·\-\u2013)\]"
QSTEM_HEADING_RE = re.compile(
    r"^\s*(?:Q(?:uestion)?\s*[.:]?\s*)?(\d{1,3})\s*[" + QSTEM_TERMINATORS + r"]")
QSTEM_HEADING_MULTILINE_RE = re.compile(
    r"(?m)^\s*(?:Q(?:uestion)?\s*[.:]?\s*)?(\d{1,3})\s*[" + QSTEM_TERMINATORS + r"]")





# --- run-4 audit RCA guards (2026-07-26 full-output audit; see
# ROOT_CAUSE_ANALYSIS.md "Run-4 audit" section). Deterministic, zero-token:
# --- run-21 DYNAMIC IMAGE CAP (2026-08-11) -------------------------------
# The flat cap below is the right default for DETERMINISTIC claims (block
# position / OCR geometry): those stack a neighbour's figures onto whichever
# question happens to sit above them, so 3 is a real over-attribution signal.
# It is the WRONG rule for a claim the MODEL declared while looking at the
# printed page: ANA-009-012 legitimately cites 4 figures, and the flat cap
# refused the 4th ("over-attribution guard: ... already has 3 question images")
# even though the full-page vision pass had named q12 with a printed anchor.
# The refused file then landed in unmatched_images.jsonl and tripped the
# export gate -- content loss dressed up as a safety guard.
#
# Rule: a model-declared owner may exceed the deterministic cap up to a hard
# ceiling; positional claims stay at the strict cap. The per-question
# allowance actually granted is remembered so the late over-attribution sweep
# trims to the SAME number instead of undoing the fix.
IMAGE_CAP_CEILING_QUESTION = 8   # hard ceiling even for model-declared owners
IMAGE_CAP_CEILING_SOLUTION = 6
# How many pages back to look for the block that is still open when a window's
# leading pages were already imaged by the previous window (run-21 §2.1(b)).
CARRY_SEED_LOOKBACK_PAGES = 3
# A cross-page CARRY claim is deterministic page evidence (the block provably
# opened on an earlier page and no new heading has been printed since), so it
# is not the "stacking neighbours' figures" failure the flat cap defends
# against. ch9 q11's solution genuinely spans p158 x2 + p159; the flat cap of 2
# refused the third and handed it to vision, which then misfiled it under q12.
# Carry claims may reach the model ceiling; plain same-page positional claims
# still stop at the strict cap.
CARRY_CLAIM_SOURCE = "positional_carry"
# RUN-39: the figure lies inside a CROP INTERVAL -- the same block extent the
# text was cut from. This is the strongest deterministic claim there is: the
# block boundary is the one the reader sees, and it spans pages, so a figure
# above its question's stem (header on the previous page) is proven without
# any carry. See boundary_phased.ChapterRunner._claim_images_by_interval.
INTERVAL_CLAIM_SOURCE = "interval"
# BUG 7B: a carry that crossed more than this many pages without a new
# printed heading is too weak to auto-attach (image-heavy books).
MAX_CARRY_PAGES = 1
MODEL_CLAIM_SOURCES = frozenset((
    "figure_map",            # model's window-level figure map (exact-count guarded)
    "full_page_vision",      # L3: red-boxed page render, printed-anchor evidence
    "isolated_crop_vision",  # L4: last-resort single-image verdict
))
_declared_image_allowance = {}   # (subject, chapter_no, qn, kind) -> granted cap


def _allowance_key(subject, chapter_no, qn, kind):
    return (subject, int(chapter_no), int(qn), kind)


def _dump_declared_allowances(state):
    """Persist the dynamic per-question image caps so a daily-quota resume
    (new process) does not revert them to the flat default and let the
    chapter-end sweep silently trim a legitimately-earned 4th figure."""
    state["declared_image_allowance"] = {
        "|".join(map(str, k)): int(v) for k, v in _declared_image_allowance.items()}


def _load_declared_allowances(state):
    for k, v in (state.get("declared_image_allowance") or {}).items():
        try:
            s, c, q, kind = k.split("|")
            _declared_image_allowance[_allowance_key(s, int(c), int(q), kind)] = int(v)
        except Exception:
            continue


def image_cap_for(subject, chapter_no, qn, kind):
    """The cap in force for one (question, side) -- the deterministic default
    unless a model-declared claim already earned a higher allowance."""
    base = MAX_QUESTION_IMAGES if kind == "question" else MAX_SOLUTION_IMAGES
    return max(base, _declared_image_allowance.get(
        _allowance_key(subject, chapter_no, qn, kind), 0))


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
# RUN-34 (OPH-001 live): q3's solution shipped as the two characters ". X" and
# the gate called it complete, because missing_solution only tested for an
# EMPTY string. The book's shortest real explanation is 63 characters ("The
# canal of Schlemm appears by the 4th month after conception."), so 20 is a
# very wide margin -- it catches residue without ever flagging a genuine
# one-line answer.
MIN_SOLUTION_CHARS = 20
                              # must differ by at least this to decide automatically;
                              # below it both variants are logged for review (no silent picks).
DANGLING_END_RE = re.compile(r"(:|\u2014|\u2013|\u2022)\s*$")   # ends ':' / em/en-dash / bullet








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













# ============================================================
# STEP 4: merge partial results (a question's text might be on one
# page and its answer/solution on a later page) into final records
# ============================================================


_PAGE_FURNITURE_RES = (
    # reseller stamp, with or without the page number in front of it
    re.compile(r"(?i)^\s*\d{1,4}\s+sold\s+by\s+@\S+\s*$"),
    re.compile(r"(?i)^\s*sold\s+by\s+@\S+\s*$"),
    # publisher mark, with or without the copyright glyph
    re.compile(r"(?i)^\s*(?:\u00a9|\(c\))?\s*marrow\s*$"),
)
_BARE_PAGE_NO_RE = re.compile(r"^\s*\d{1,4}\s*$")
# RUN-55 (external-audit idea, made safe): the reseller stamp also appears
# INLINE inside a content line ("Middle cerebral artery 6 Sold by @itachibot
# PRunebdinn IN"), which the whole-line rules never see. The stamp is never
# real content, so strip it inline. Bone-"marrow" IS content, so the publisher
# mark stays whole-line-only.
# [ \t] (not \s) so it never spans a newline -- a stamp on its OWN line is the
# whole-line rule's job; this only cleans a stamp welded into a content line.
_INLINE_STAMP_RE = re.compile(r"(?i)(?:[ \t]\d{1,4})?[ \t]+sold\s+by\s+@\S+")


def strip_page_furniture(text):
    """Drop page furniture that extraction sweeps into body copy.

    OPH-001 live, straight out of questions.jsonl:
        q1 solution  "...leading to\\n12 Sold by @itachibot\\n\\nhypermetropia."
        q7 stem      "...appearsby_\\n5 Sold by @itachibot"
        q9 option D  "Middle cerebral artery 6 Sold by @itachibot PRunebdinn IN"
        q9 solution  "...superior hypophyseal artery - ventral...\\n\u00a9MARROW"

    Only whole lines that are PURELY furniture are removed. A line carrying
    anything else is left untouched -- eating a clause to remove a stamp would
    be a worse defect than the stamp. A bare page-number line is dropped only
    when the next non-empty line is a reseller stamp, which is the shape this
    footer actually takes ("11\\nSold by @itachibot").

    The garbled watermark variants ("Cmlistklianm Fm Pir tr rebkiana") are
    deliberately NOT matched: they are OCR noise with no stable spelling, and
    pattern-guessing at them would risk eating real text. Those are reported
    by _ocr_noise_note instead of silently rewritten.

    Returns (cleaned_text, dropped_line_count)."""
    src = text or ""
    if not src.strip():
        return src, 0
    # RUN-55: strip INLINE reseller stamps first ("... 6 Sold by @itachibot
    # ..."). They sit inside a content line so the whole-line rules never see
    # them, and the stamp is never real content.
    lines = src.split("\n")
    keep, dropped = [], 0
    # RUN-41: drop LEADING bare page-number line(s) -- crop-top residue from
    # a footer sitting under a bottom-of-page header. Only lines before any
    # real content qualify; a lone digit line anywhere later is ambiguous
    # (a numbered list item may legitimately be a bare digit line).
    while lines:
        head = lines[0].strip()
        if not head:
            lines = lines[1:]          # leading blank: harmless, drop
            continue
        if _BARE_PAGE_NO_RE.match(head) or any(r.match(head)
                                               for r in _PAGE_FURNITURE_RES):
            dropped += 1
            lines = lines[1:]
        else:
            break
    for i, ln in enumerate(lines):
        if any(r.match(ln) for r in _PAGE_FURNITURE_RES):
            dropped += 1
            continue
        if _BARE_PAGE_NO_RE.match(ln):
            nxt = next((x for x in lines[i + 1:] if x.strip()), "")
            if any(r.match(nxt) for r in _PAGE_FURNITURE_RES[:2]):
                dropped += 1
                continue
        # RUN-55: an inline reseller stamp welded into a CONTENT line (the
        # line is not pure furniture, so the whole-line rule kept it). Strip
        # just the stamp substring; the real text is preserved.
        if _INLINE_STAMP_RE.search(ln):
            ln2 = re.sub(r"[ \t]{2,}", " ", _INLINE_STAMP_RE.sub(" ", ln)).strip()
            dropped += 1
            if ln2:
                keep.append(ln2)
            continue
        keep.append(ln)
    if not dropped:
        return src, 0
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
    return cleaned, dropped


# Characters that indicate OCR or font damage rather than real typography.
# Medical textbooks legitimately use ° µ – — ‘ ’ “ ” • → × ≤ ≥ ± and Greek
# letters, so the original "ord(c) > 127" test produced false positives on
# OPH-001: q9's three-bullet list scored 3/137 = 0.022 non-ASCII and was
# flagged as damage, pushing the chapter from 6 REVIEW_NEEDED to 11 for text
# that was perfectly correct. Only glyphs that never appear in correct print
# count now; the private-use area is where garbled font encodings land.
_OCR_DAMAGE_CHARS = frozenset("\ufffd\u25a0\u25a1\u25aa\u25ab")


def _ocr_damage_share(s):
    """Share of characters that are OCR/font damage, not typography."""
    n = max(len(s), 1)
    return sum(1 for c in s
               if c in _OCR_DAMAGE_CHARS or "\ue000" <= c <= "\uf8ff") / n


def _ocr_noise_note(text, field):
    """A review reason when `text` looks like OCR damage rather than prose.

    Nothing here rewrites anything -- the record ships as extracted and a
    human decides. Applied per FIELD, because a whole-page health check says
    nothing about one option label and would call a legitimate "2.4 cm" empty.

    OPH-001 shipped both of these as READY:
        q2 stem  "oe SS \u00ab\\ni i ity?\\nAt what age would a child attain full a \u00ab"
        q3 stem  "A ascarid presen GR the abnormality as shown below"
        q1 opt D "Astigmatic x\u201c es _ ne 0 n\\* \\ \u00bb\\* XW e Niactinn = a ws +."
    """
    s = (text or "").strip()
    if not s:
        return None
    n = len(s)
    letters = sum(1 for c in s if c.isalpha())
    damage = _ocr_damage_share(s)
    if n >= 24 and damage > 0.02:
        return (f"{field}: damaged glyphs {damage * n:.0f}/{n} chars -- looks "
                f"like OCR damage, not print")
    if n >= 40 and letters / n < 0.55:
        return (f"{field}: only {letters}/{n} alphabetic -- looks like OCR "
                f"damage, not prose")
    return None


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
    # RUN-34: page furniture goes first. OPH-001 shipped q1's solution with
    # "12 Sold by @itachibot" sitting between two clauses, and several others
    # ended in "\u00a9MARROW".
    s, _dropped = strip_page_furniture(s)
    if _dropped:
        notes.append(f"stripped {_dropped} page-furniture line(s) "
                     f"(reseller stamp / publisher mark / page number)")
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
        # RUN-36 (OPH-001 q4 live): the crop for this block bled over the page
        # break and picked up the PREVIOUS question's explanation, so the
        # record read
        #     "Uveal melanomas arise from uveal melanocytes...   <- q3's
        #
        #      Solution to Question 4:
        #
        #      The macula is fully developed by 4-6 months..."
        # When the embedded header names THIS question, everything before it
        # cannot be this question's solution -- the book prints that header as
        # the start of the explanation. Drop the head. This is deterministic,
        # not a guess, and it shipped as qa_status=READY before: the old code
        # only stripped a LEADING header, so a foreign head survived silently.
        try:
            hdr_qn = int(m.group(1))
        except (TypeError, ValueError):
            hdr_qn = None
        tail_first = tail.split("\n", 1)[0][:150]
        if head and own_qn is not None and hdr_qn == int(own_qn):
            s = tail
            notes.append(
                f"dropped {len(head)} chars of the PREVIOUS question's "
                f"solution that bled into this crop (embedded 'Solution to "
                f"Question {hdr_qn}' header names this question)")
        elif head and tail_first and _frag_mostly_present(tail_first, head, 0.8):
            # The precise dump proof: the chunk IMMEDIATELY after the header
            # restates THIS solution's own earlier content (the model
            # re-recited this question before dumping its neighbours).
            # Neighbour content further down the tail is never judged -- only
            # the first line.
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




def build_final_question(subject, chapter_id, chapter_no, q_no, rec, image_files,
                         source_pages=None, ownership_pages=None,
                         gate_notices=None, carry_corroborated=None):
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
            out.append({"type": "figure", "file": f,
                        # AUDIT-FIX: the PDF page the figure was extracted
                        # from travels WITH the row, so downstream review /
                        # validation can re-prove ownership instead of
                        # trusting the filename's QID.
                        "source_page": (ownership_pages or {}).get(f)})
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
    # RUN-55: options previously went out as-is, so an inline reseller stamp
    # ("... 6 Sold by @itachibot ...") shipped inside option text and read as
    # garbage / bad_options. Clean each option with the same furniture stripper.
    option_rows = [{"id": str(k).strip().upper(),
                    "text": strip_page_furniture(str(v or ""))[0],
                    "images": valid_images(opt_imgs.get(str(k).strip().upper(), []), "option")}
                   for k, v in (rec["options"] or {}).items()]
    # A missing option must remain missing.  The previous release copied the
    # solution's opening sentence into a blank correct option, which fabricated
    # content and could make an incomplete row look structurally complete.
    # Extraction is provenance-only: targeted retries may recover the printed
    # option, otherwise the export gate/qa layer flags it for a human.
    correct_id = str(rec.get("correct_option") or "").strip().upper()
    _build_flags = []
    sol_text, _unbacked = strip_unbacked_img_markers(
        sol_text, len(sol_images))
    if _unbacked:
        _build_flags.append(
            f"removed {_unbacked} unbacked [IMG] marker(s) from solution; "
            "no solution figure exists in the owned interval")
        print(f"  [IMG_MARKER_CLEANUP] {qid}: removed {_unbacked} "
              "unbacked solution marker(s); image ownership remains empty")
    for opt in option_rows:
        if opt["id"] == correct_id and not str(opt.get("text") or "").strip():
            _build_flags.append(
                f"option {correct_id} text missing; not reconstructed from solution")
            print(f"  [OPTION_MISSING] {qid}: correct option {correct_id} "
                  "is blank; left unchanged for review")
    # Correct clearly mislabelled "Option X:" explanation lines only when the
    # description overlaps another option at least twice as strongly.
    opt_text = {o["id"]: str(o.get("text") or "") for o in option_rows}
    def relabel(m):
        label, desc = m.group(1).upper(), m.group(2)
        words = {w for w in re.findall(r"\w+", desc.lower()) if len(w) > 2}
        scores = {k: len(words & set(re.findall(r"\w+", v.lower()))) for k, v in opt_text.items() if v}
        best = max(scores, key=scores.get) if scores else label
        if best != label and scores.get(best, 0) >= 2 * max(1, scores.get(label, 0)):
            # Do not rewrite a printed/model label based on token overlap.
            # Overlap can identify a suspect mismatch, but it cannot prove
            # which label the source intended. Preserve the extracted text and
            # surface the evidence for human review instead of silently
            # changing content.
            print(f"  [LABEL_SUSPECT] {qid}: Option {label} may point to "
                  f"Option {best}'s content -- preserved unchanged")
            _build_flags.append(f"printed 'Option {label}:' explanation line "
                                f"may point at option {best}'s content; "
                                "preserved unchanged for review")
            return m.group(0)
        return m.group(0)
    sol_text = re.sub(r"(?m)Option\s+([A-D])\s*:\s*([^\n]+)", relabel, sol_text)

    # ---- qa_status: NEVER silent -----------------------------------------
    # READY         = extracted content + every image backed by deterministic
    #                 or high-confidence evidence; no review reason.
    # REVIEW_NEEDED = content complete but something needs human eyes
    #                 (model-claim-only figures, gate mismatch, sweep
    #                 findings, backfilled option, answer/solution letter
    #                 disagreement, quarantined stem/options).
    # INCOMPLETE    = structural fields missing (stem/4 options/answer/
    #                 solution) -- aap manually review karenge.
    # Nothing here corrects data; it only flags (aapke rule ke hisaab se --
    # no brute-force fix).
    status_reasons = list(rec.get("_review_reasons") or [])
    structural_missing = []
    if not (rec.get("question_text") or "").strip():
        structural_missing.append("question_text")
    opts = rec.get("options") or {}
    if len(opts) < 4 or any(not str(v or "").strip() for v in opts.values()):
        structural_missing.append("options")
    if not rec.get("correct_option"):
        structural_missing.append("correct_option")
    # the SHIPPED text is the sanitized one: a header-only model answer
    # ("Solution to Question 21:") strips to "" -- that must be INCOMPLETE,
    # never READY (OBG-010-021 live: shipped READY with empty solution).
    if not sol_text.strip():
        structural_missing.append("solution_text")
    if structural_missing:
        status_reasons.append(f"missing structural fields: {structural_missing}")

    # RUN-34: OCR damage that survived the page-furniture strip. Reported,
    # never rewritten -- the record ships exactly as extracted and a human
    # decides. OPH-001 shipped every one of these as READY, which is how a
    # stem reading "A ascarid presen GR the abnormality" reached the export.
    _noise = _ocr_noise_note(rec.get("question_text"), "question stem")
    if _noise:
        status_reasons.append(_noise)
    for _letter in sorted(rec.get("options") or {}):
        _n_opt = _ocr_noise_note((rec.get("options") or {})[_letter],
                                 f"option {_letter}")
        if _n_opt:
            status_reasons.append(_n_opt)
    _n_sol = _ocr_noise_note(sol_text, "solution")
    if _n_sol:
        status_reasons.append(_n_sol)

    # image-evidence review: any attached image whose ownership proof is
    # WEAKER than same-page printed geometry. Two distinct classes, reported
    # separately so a reviewer can tell them apart:
    #   * model-only -- isolated_crop_vision, or full_page_vision at
    #     non-high confidence (the original rule);
    #   * positional_carry -- deterministic, but the owner is the block
    #     CARRIED across the page edge (active_block), not a heading printed
    #     above the figure on its own page. RUN-29 (OPH-001 live): carry
    #     claims were graded "medium" and matched neither branch above, so
    #     the row shipped READY. Every mis-attribution the carry produced was
    #     invisible to qa_status and therefore to /review. A carry is real
    #     evidence, so it does not make the row INCOMPLETE -- it makes it
    #     REVIEW_NEEDED, which is exactly what the review layer is for.
    ownership_methods = _ownership_method_map(chapter_id)

    def _img_evidence_class(meth, conf):
        """None | "model" | "carry" -- the weakness class of one claim."""
        if meth == "isolated_crop_vision":
            return "model"
        if meth == "full_page_vision" and conf != "high":
            return "model"
        if meth == CARRY_CLAIM_SOURCE:
            return "carry"
        return None

    weak_img_files = []
    carry_img_files = []
    corroborated_files = []
    _corroborated = carry_corroborated or set()

    def _record_image_flag(fname, cls, meth, conf):
        """RUN-37: a carry whose figure lies inside its owner's own block
        interval is corroborated by geometry and does not need a human. Any
        other carry still flags -- clearing the class wholesale would just
        hide the attributions that really are guesses."""
        if cls == "model":
            weak_img_files.append(f"{fname} ({meth}/{conf or '?'})")
        elif cls == "carry":
            if fname in _corroborated:
                corroborated_files.append(fname)
            else:
                carry_img_files.append(f"{fname} ({meth}/{conf or '?'})")

    for side, imgs in (("question", q_images), ("solution", sol_images)):
        for im in imgs:
            ev = ownership_methods.get(im["file"], {})
            meth, conf = ev.get("method"), ev.get("confidence")
            _record_image_flag(im["file"], _img_evidence_class(meth, conf),
                               meth, conf)
    opt_blob = (image_files.get("option") or {})
    for letter, files in opt_blob.items():
        for fn, meta in [] if not ownership_methods else [
                (f, ownership_methods.get(f, {})) for f in files]:
            meth, conf = meta.get("method"), meta.get("confidence")
            _record_image_flag(fn, _img_evidence_class(meth, conf), meth, conf)
    if weak_img_files:
        status_reasons.append("image(s) attached by model-only evidence: "
                              + "; ".join(weak_img_files))
    if carry_img_files:
        status_reasons.append(
            "image(s) owned by cross-page carry (no heading printed above "
            "the figure on its own page -- owner inferred from the block "
            "still open on the previous page): " + "; ".join(carry_img_files))

    # gate notices for this question (wrong-owner suspect, strong missing
    # figure, etc.)
    for knd, detail in (gate_notices or []):
        status_reasons.append(f"export-gate {knd}: {detail}")

    for mark, label in ((rec.get("_stem_suspect_reason"), "stem_suspect"),
                        (rec.get("_options_suspect_reason"), "options_suspect")):
        if mark and not any(r.startswith(label) for r in status_reasons):
            status_reasons.append(f"{label}: {mark}")

    am = _answer_option_mismatch(rec, correct_id, option_rows)
    if am:
        status_reasons.append("answer_suspect: " + am)

    status_reasons.extend(_build_flags)

    # READY answers must cite a key-table cell (pixels/OCR). A letter
    # without evidence is REVIEW_NEEDED even if structurally complete.
    ev = rec.get("_key_evidence")
    if rec.get("_key_evidence_required") and rec.get("correct_option") and not ev:
        status_reasons.append("answer missing key-table evidence")
    if structural_missing:
        qa_status = "INCOMPLETE"
    elif status_reasons:
        qa_status = "REVIEW_NEEDED"
    else:
        qa_status = "READY"

    return {
        "id": qid,
        "subject": subject,
        "chapter_id": chapter_id,
        "question": {"text": rec["question_text"], "images": q_images},
        "options": option_rows,
        "correct_options": [rec["correct_option"]] if rec["correct_option"] else [],
        "solution": {"text": sol_text, "images": sol_images, "tables": tables},
        "tags": [],
        # AUDIT-FIX: persist the MODEL-DECLARED figure flags. Before this,
        # the only figure signal in the export was derived from the attached
        # image lists, so a wrong-owner assignment even rewrote the declared
        # intent to match the mistake (and 'declared-but-missing' questions
        # like Q7/Q8 in ch. 28 were invisible to every gate).
        "declared_has_figure_in_question": bool(rec.get("has_figure_in_question")),
        "declared_has_figure_in_solution": bool(rec.get("has_figure_in_solution")),
        # run-19: which PDF page(s) this question was extracted from -- lets
        # a downstream reviewer (human or the critique pass below) jump
        # straight to source without re-deriving it from the page ledger.
        "source_pages": sorted(source_pages) if source_pages else [],
        # run-13: quarantined suspect stem marker -- ships in questions.jsonl
        # so the post-run validator flags it too (not only the export gate).
        "stem_suspect": rec.get("_stem_suspect_reason"),
        # run-22: options MAY have been harvested off another question's
        # solution page (ch. 38 q13). Ships as-extracted; this is the review
        # marker, not a correction. None on healthy records.
        "options_suspect": rec.get("_options_suspect_reason"),
        # run-26: unrepaired integrity-sweep findings (iflag matched=False).
        # These are the "I found something wrong and could not fix it safely"
        # cases -- they used to live only in integrity_flags.jsonl, leaving
        # the exported row looking clean. None on healthy records.
        "review_reasons": list(rec.get("_review_reasons") or []) or None,
        "manual_review": bool((rec.get("_stem_suspect_reason")
                               or rec.get("_options_suspect_reason")
                               or rec.get("_review_reasons")
                               or _build_flags)
                              or status_reasons),
        # AUDIT-FIX (user ask): the export now carries an explicit per-row
        # status with its machine-readable reasons. A question that could not
        # be proven is NEVER silently COMPLETE -- INCOMPLETE rows and
        # REVIEW_NEEDED rows are distinguishable and reviewable manually.
        "qa_status": qa_status,
        "qa_reasons": status_reasons or [],
        "key_evidence": rec.get("_key_evidence"),
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


def _image_positions_raw(pdf_path, file_page):
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
        state = {"draw_idx": 0, "visited_forms": set(), "draw_counts": {}}

        def _decompress(data):
            try:
                return zlib.decompress(data)
            except Exception:
                return data

        def _walk(data, names, ctm=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0)):
            # AUDIT-FIX: the recursion into Form XObjects restarted with an
            # identity CTM, so a Form-wrapped figure reported FORM-LOCAL
            # coordinates (the outer cm translation was lost) and every
            # "closest heading above" comparison for it ran against the wrong
            # y. The CTM is now threaded: a Form's own /Matrix composes onto
            # the inherited CTM exactly as the PDF spec requires.
            tokens = re.findall(rb"/[^\s\[\]()<>{}/%]+|\([^)]*\)|\[[^\]]*\]|"
                                rb"[-+]?\d*\.?\d+|[A-Za-z'\"]+", _decompress(data))
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
                            # AUDIT-FIX (user report, OBGYN p54/p63 class):
                            # the SAME image object can be drawn MULTIPLE times
                            # on one page -- e.g. a figure printed a second
                            # time inside the next solution. The old
                            # positions[key] = ... line simply OVERWROTE the
                            # entry, so only the last draw survived: one copy
                            # existed, one position existed, and the figure
                            # could belong to only one block; the usage whose
                            # heading sat elsewhere silently vanished.
                            # Audit note: the FIRST draw keeps the plain key;
                            # every subsequent draw of the same object on the
                            # page lands at "<key>@d<N>" so the claimer can
                            # see each printed usage and attach the same
                            # content to every PROVEN owner.
                            if key in positions:
                                n_draws = state["draw_counts"].get(key, 1)
                                state["draw_counts"][key] = n_draws + 1
                                positions[f"{key}@d{n_draws}"] = (
                                    ctm[5], ctm[4], state["draw_idx"],
                                    ctm[0], ctm[3])
                            else:
                                state["draw_counts"][key] = 1
                                positions[key] = (ctm[5], ctm[4],
                                                  state["draw_idx"],
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
                                    # compose the Form's own /Matrix (default
                                    # identity) onto the inherited CTM, then
                                    # walk the form body in that space.
                                    _fm = _resolve(obj.get("/Matrix"))
                                    _ctm_form = ctm
                                    try:
                                        if _fm and len(_fm) == 6:
                                            _ctm_form = _mat_mult(
                                                tuple(float(v) for v in _fm), ctm)
                                    except (TypeError, ValueError):
                                        _ctm_form = ctm
                                    _walk(fdata, fnames, _ctm_form)
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


def _extra_draw_keys(pos, oid):
    """The '<oid>@d<N>' alias keys of an image object, in draw order.
    Emitted by _image_positions_raw when the same object is drawn multiple
    times on one page (second printings of the same figure inside a later
    block, e.g. OBGYN p54/p63)."""
    base = str(oid)
    out = [k for k in pos
           if isinstance(k, str) and k.startswith(base + "@d")]
    return sorted(out, key=lambda k: pos[k][2])


def image_positions_on_page(pdf_path, file_page):
    """Same contract as _image_positions_raw -- {key -> (y, x, draw_index, w, h)}
    with (x, y) the BOTTOM-LEFT corner in PDF user space -- but SLICE-AWARE.

    run-25: extract_real_images now stitches abutting slices of one printed
    figure into a single file named after the group's FIRST slice. Every
    consumer here (claim_block_images, _order_imgs_by_position, the bbox
    overlay, ...) looks a file's geometry up by parsing that object id out of
    the filename, so the id must resolve to the geometry of the WHOLE figure,
    not of its top strip. We therefore report, for each group's lead id, the
    union rect of all its slices, and drop the swallowed slice ids (no file
    references them any more).
    """
    raw = _image_positions_raw(pdf_path, file_page)
    if len(raw) < 2:
        return raw
    rects = {k: _rect_from_position(p) for k, p in raw.items()}
    merged = {}
    for g in _group_slice_rects(rects):
        if len(g) == 1:
            merged[g[0]] = raw[g[0]]
            continue
        lead = _group_lead(g, rects)
        u_l = min(rects[k][0] for k in g)
        u_b = min(rects[k][1] for k in g)
        u_r = max(rects[k][2] for k in g)
        u_t = max(rects[k][3] for k in g)
        merged[lead] = (u_b, u_l, min(raw[k][2] for k in g),
                        u_r - u_l, u_t - u_b)
    return merged


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


_TEMP_IMG_NAME_RE = re.compile(r"^[A-Za-z0-9]+-(?:p|page)(\d+)-(.+)\.webp$",
                               re.IGNORECASE)


def _temp_name_provenance(rel):
    """(page, obj_id) parsed from the extraction temp name
    SUBJ-p{page}-{obj}.webp. (None, None) for already-final names -- the temp
    name IS the only place page + object id survive extraction, so every
    ownership ledger row is written HERE, before the rename destroys it."""
    m = _TEMP_IMG_NAME_RE.match(Path(str(rel)).name)
    if not m:
        return None, None
    page = int(m.group(1))
    tok = m.group(2)
    try:
        oid = int(tok)
    except ValueError:
        oid = tok                  # name-keyed images (objects w/o idnum)
    return page, oid


def _rename_for_slot(rel, qn, kind, subject, chapter_no, image_files_by_q,
                     option_letter=None, claim_source="positional",
                     evidence="", confidence=None):
    """Rename one extracted temp image into the locked convention for the
    given (q_no, "question"|"solution"|"option") slot. kind letters: Q, SOL,
    or OPT_{L} (option_letter A-D). Returns the new rel path or None.

    claim_source (run-21): "positional" (default -- deterministic block/OCR
    geometry) keeps the strict over-attribution cap. A source in
    MODEL_CLAIM_SOURCES means the model NAMED this owner while looking at the
    printed page, so the cap lifts to the hard ceiling and the granted
    allowance is remembered for the late sweep.

    AUDIT-FIX: this is the single choke point EVERY claiming path flows
    through, so the ownership ledger row (temp name, source page, object id,
    method, evidence, outcome=claimed/refused_*) is written HERE. The
    chapter-end gate and validator can then re-prove that an exported
    question_images entry is backed by page/geometry/anchor evidence instead
    of merely existing on disk."""
    chapter_id = f"{subject}-{chapter_no:03d}"
    qid = f"{subject}-{chapter_no:03d}-{qn:03d}"
    src_page, src_oid = _temp_name_provenance(rel)
    old_path = ASSETS_DIR / "questions" / rel
    if not old_path.exists():
        print(f"  [WARN] {rel} missing at rename time -- skipping (alias/dup ref)")
        _record_image_ownership(subject, chapter_id, src_page, rel, qid, kind,
                                claim_source, evidence or "rename-time file missing",
                                confidence="high", outcome="stale_ref",
                                obj_id=src_oid)
        return None
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
        _record_image_ownership(subject, chapter_id, src_page, rel, qid, kind,
                                claim_source,
                                evidence or f"tiny file ({size}B < {MIN_IMAGE_BYTES})",
                                confidence="high", outcome="refused_tiny",
                                obj_id=src_oid)
        return None
    # No hard image-count cap (2026-08-24). Geometry owns the file: if this
    # caller proved an interval, every figure in that interval ships. A 6-
    # figure solution is real data; refusing the 3rd+ was data loss. Outliers
    # get a soft review_suggested flag later — never refused_cap.
    if kind in ("question", "solution"):
        n_have = len(entry.get(kind) or [])
        if n_have >= 8:
            print(f"  [IMG] {qid} {kind} already has {n_have} images; "
                  f"still claiming {rel} (no cap, will flag high_image_count)")
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
    if confidence not in ("high", "medium", "low"):
        # deterministic same-page geometry is the strongest claim; a carry
        # spans a page edge; a model verdict is medium unless it declared
        # its own confidence (full_page_vision passes it via `confidence`).
        # An interval claim is the strongest of all: the figure sits inside
        # the very block extent the text was cut from.
        confidence = {"positional": "high",
                      INTERVAL_CLAIM_SOURCE: "high",
                      CARRY_CLAIM_SOURCE: "medium"}.get(claim_source, "medium")
    _record_image_ownership(subject, chapter_id, src_page, rel, qid, kind,
                            claim_source,
                            evidence or "renamed into claimed slot",
                            confidence=confidence,
                            outcome="claimed", obj_id=src_oid,
                            final_file=new_rel)
    return new_rel


def claim_page_images_one_to_one(imgs, pdf_path, file_page, subject, chapter_no,
                                 chapter_records, image_files_by_q, section=None):
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
        # AUDIT-FIX (ch. 28 class): "one printed q_no + one needy slot" never
        # proved WHICH figures belong to it -- the old greedy loop attached
        # EVERY extracted image on the page, so a figure belonging to a
        # different question (printed above the stem, or whose own heading
        # the text layer lost) was absorbed by the lone slot. Require block
        # evidence: the candidate's heading must be locatable on this page
        # and the image must sit INSIDE its block extent (below the heading,
        # above the next detected anchor). Anything else stays for the
        # explicit-attribution / manual levels.
        headers = union_block_headers_on_page(pdf_path, file_page, chapter_records,
                                          section=section)
        pos = image_positions_on_page(pdf_path, file_page)
        idx = next((i for i, (k, q, _y) in enumerate(headers) if q == qn), None)
        if idx is None or not pos:
            print(f"  [IMG] page {file_page}: q{qn} is the sole printed owner but "
                  f"its heading/block extent is not locatable -- not auto-attaching")
            return list(imgs)
        y_head = headers[idx][2]
        y_bottom = headers[idx + 1][2] if idx + 1 < len(headers) else 0.0
        entry = image_files_by_q.setdefault(qn, {"question": [], "solution": []})
        leftover = []
        for rel in imgs:
            # append IMMEDIATELY after each rename: _rename_for_slot derives
            # the _01/_02/... suffix from len(entry[kind]), so deferring the
            # append would hand the same filename to every image on this
            # page and silently overwrite them (caught by tests).
            try:
                oid = int(Path(rel).stem.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                leftover.append(rel)
                continue
            info = pos.get(oid)
            if info is None:
                leftover.append(rel)
                continue
            y_img, _x_img, _didx, _w, _h = info
            if _h and y_img + _h < y_img:
                y_img = y_img + _h          # bottom edge (flip-normalized)
            if not (y_bottom < y_img < y_head):
                print(f"  [IMG] one-to-one: {rel} outside q{qn}'s block extent "
                      f"on page {file_page} -- left for explicit attribution")
                leftover.append(rel)
                continue
            new_rel = _rename_for_slot(rel, qn, kind, subject, chapter_no,
                                       image_files_by_q,
                                       evidence=(f"sole printed owner q{qn} on "
                                                 f"page {file_page}; image inside "
                                                 "its block extent"))
            if new_rel:
                entry[kind].append(new_rel)
            else:
                leftover.append(rel)
        return leftover


def claim_block_images(imgs, pdf_path, file_page, subject, chapter_no,
                       chapter_records, image_files_by_q, active_block=None,
                       section=None):
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
    # AUDIT-FIX (ch. 28 root cause): headers are the TEXT+OCR UNION and do
    # not depend on which questions have records yet. The old text-only,
    # record-filtered header list made under-detected headings geometrically
    # invisible, and the "closest heading above" rule then attached their
    # figures to the PREVIOUS question (OPH-028-006 collected Q5/Q7's
    # figures while Q7/Q8 shipped with none). OCR anchors are cached
    # (_ocr_anchors_for_page), so the union costs no extra renders.
    headers = union_block_headers_on_page(pdf_path, file_page, chapter_records,
                                          section=section)
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
        # normalize against a possibly-flipped cm (negative h): y_img must be
        # the image's BOTTOM edge for the "heading above" comparison.
        if y_img + _h < y_img:
            y_img = y_img + _h
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
        if owner is active_block and active_block is not None \
                and len(active_block) >= 3:
            gap = int(file_page) - int(active_block[2])
            if gap > MAX_CARRY_PAGES:
                print(f"  [IMG] page {file_page}: refusing long positional_carry "
                      f"({gap} pages from heading on p{active_block[2]}) "
                      f"-- left for review")
                leftover.append(rel)
                continue
        kind, qn = owner[0], owner[1]
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
                                   image_files_by_q, option_letter=opt_letter,
                                   claim_source=(CARRY_CLAIM_SOURCE
                                                 if owner is active_block
                                                 else "positional"),
                                   evidence=(
                                       f"carry: block ({active_block[0]} "
                                       f"q{active_block[1]}) still open from "
                                       "a previous page; no heading above "
                                       f"image on page {file_page}"
                                       if owner is active_block else
                                       f"closest printed ({kind} q{qn}) "
                                       "heading above image on page "
                                       f"{file_page} (text+OCR union)"))
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

            # AUDIT-FIX (user report, OBGYN ed8 ch.2 p54 / ch.3 p63 class):
            # the SAME image object can be drawn twice on one page (the upper
            # copy belongs to the block still open from the previous page;
            # the lower sits under the next printed heading). Extraction
            # rightly saves ONE file -- but ownership must see BOTH usages.
            # _image_positions_raw keeps each draw as "<oid>@d<N>" aliases;
            # for every ALIAS draw whose own printed anchor proves a
            # different block, we attach the SAME final file to that owner
            # too (no second copy, no second Gemini call). Without this the
            # upper copy silently vanished and only the lower one existed.
            for _exo_key in _extra_draw_keys(pos, oid):
                _ey, _ex, _di, _ew, _eh = pos[_exo_key]
                _ey = min(_ey, _ey + _eh) if _eh else _ey
                _exo = next(((k2, q2) for k2, q2, y_hdr in reversed(headers)
                             if y_hdr > _ey), None)
                if _exo is None:
                    _exo = active_block   # same carry contract as primary
                if _exo is None:
                    continue
                _ekind, _eqn = _exo
                if (_ekind, _eqn) == (kind, qn) or _eqn not in chapter_records:
                    continue
                _e_entry = image_files_by_q.setdefault(
                    _eqn, {"question": [], "solution": []})
                _e_entry.setdefault("option", {})
                if new_rel in _e_entry.get(_ekind, []):
                    continue
                _cap = image_cap_for(subject, chapter_no, _eqn, _ekind)
                if len(_e_entry.get(_ekind, [])) >= _cap:
                    print(f"  [IMG] multi-draw share: {rel} also drawn in "
                          f"({_ekind} q{_eqn})'s block on page {file_page} but "
                          f"that side is at cap {_cap} -- noted for review")
                    continue
                _e_entry[_ekind].append(new_rel)
                print(f"  [IMG] multi-draw share: {new_rel} also belongs to "
                      f"({_ekind} q{_eqn}) -- same image object drawn twice on "
                      f"page {file_page}")
                _record_image_ownership(
                    subject, f"{subject}-{chapter_no:03d}", file_page, rel,
                    f"{subject}-{chapter_no:03d}-{_eqn:03d}", _ekind,
                    "multi_draw_geometry",
                    f"same object also drawn under ({_ekind} q{_eqn}) on "
                    f"page {file_page} (alias {_exo_key}); shared reference",
                    confidence="high", outcome="shared",
                    obj_id=oid, final_file=new_rel)
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
    a 300+ page book. Also drops the OCR anchor cache (same lifecycle)."""
    _RENDER_CACHE.clear()
    _OCR_ANCHOR_CACHE.clear()
    _UNION_HEADER_CACHE.clear()
    _UNION_DROP_LOGGED.clear()


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
        # AUDIT-FIX: the negative-cache (failure) path added the entry but
        # never evicted -- a book whose pages keep failing to render grew the
        # cache unboundedly (the run-16 OOM class, re-entering through a side
        # door). Evict on both paths.
        _RENDER_CACHE[key] = (None, 0, 0)
        while len(_RENDER_CACHE) > _RENDER_CACHE_MAX:
            _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)))
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
        try:
            lid = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        except (KeyError, IndexError):
            lid = None
        words.append((left, top + hgt / 2.0, t, lid))   # x_px, y_center_px
    if not words:
        return []
    # --- run-21 CRITICAL FIX: group by tesseract's OWN line ids ------------
    # The old grouper rebuilt lines from y centers with a running average
    # (cur_y = cur_y*0.5 + yc*0.5). That average DRIFTS across a wide line, so
    # a heading like "Solution to Question 10:" was split into fragments and
    # the tail fragment "10:" then matched the QUESTION regex below. Result:
    # a solution header was recorded as a question anchor at the same y --
    # every image under it was attributed to the "question" slot of q10, and
    # on this book's solution pages that is simply the wrong side. tesseract
    # already returns block/par/line numbers that group the line correctly;
    # use them, and keep the y-proximity grouper only as a fallback for
    # output dicts that lack those keys.
    lines = []
    if all(w[3] is not None for w in words):
        by_line = {}
        for x, yc, t, lid in words:
            by_line.setdefault(lid, []).append((x, yc, t))
        for lid in sorted(by_line, key=lambda k: min(w[1] for w in by_line[k])):
            grp = by_line[lid]
            y_mid = sum(w[1] for w in grp) / len(grp)
            lines.append((y_mid, sorted((x, t) for x, _y, t in grp)))
    else:
        tol = max(6.0, scale * 6.0)   # ~half a line height in px at this dpi
        words.sort(key=lambda w: (w[1], w[0]))
        cur, cur_y = [], None
        for x, yc, t, _lid in words:
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
        # run-21: SOLUTION headers are tested FIRST. "Solution to Question 10:"
        # also satisfies the question regex once any prefix is lost to OCR, and
        # whichever branch runs first wins -- so the more specific pattern must
        # go first, and a line carrying it never falls through to "question".
        # keyword marker for the S-section bare-list guard: kw = explicit
        # "Question N"/"Q N" prefix (see union_block_headers_on_page).
        n_words = len(wl)
        sol_hits = list(re.finditer(r"Solution\s+to\s+Question\s+(\d{1,3})", line, re.I))
        if sol_hits:
            for sm in sol_hits:
                anchors.append(("solution", int(sm.group(1)),
                                (img_h - _yc) / scale, wl[0][0] / scale, True))
            continue
        m = QSTEM_HEADING_RE.match(line)
        if m:
            has_kw = bool(re.match(r"^\s*Q(?:uestion)?\s*[.:]?\s*\d", line, re.I))
            # 5th field: 2 = explicit keyword (Question N:/Q N.), 1 = bare but
            # multiword line, 0 = bare single token ('2.')
            strength = 2 if has_kw else (1 if n_words >= 2 else 0)
            anchors.append(("question", int(m.group(1)),
                            (img_h - _yc) / scale, wl[0][0] / scale, strength))
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
    return [a[:3] for a in ocr_page_anchors_xy(png, scale, page_h_pt)]


def ocr_page_anchors_xy(png, scale, page_h_pt):
    """[(kind, q_no, y_pdf_pt, x_pdf_pt, strong)] -- like ocr_page_anchors but
    the anchor also carries the x of the line's first word (for the in-figure
    phantom filter) and a `strong` flag (False = bare single-token line, the
    shape of figure-texture phantoms -- union harvest corroborates those
    against text-layer occupancy)."""
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












def _record_image_ownership(subject, chapter_id, page, rel, qid, slot,
                            method, evidence, confidence="high",
                            outcome="claimed", obj_id=None, final_file=None):
    """Provenance ledger for EVERY automatic image assignment: owner, slot,
    method (deterministic_geometry / deterministic_ocr_geometry /
    model_figure_map / deterministic_one_to_one / full_page_vision ...),
    evidence, confidence. Append-only; shipped in the export zip.

    AUDIT-FIX: before this update the ledger only recorded the L2 (OCR) and
    L3 (vision) claims -- the dominant L1 positional path, the one-to-one
    matcher and the figure-map wrote NOTHING, and _rename_for_slot destroyed
    the only embodied provenance (the temp filename carried the page + object
    id). The ledger now fires in _rename_for_slot itself, so EVERY claimed or
    guard-refused image is provable after export: temp name, page, object id,
    final file, method, evidence and outcome.
    """
    entry = {"subject": subject, "chapter_id": chapter_id, "page": page,
             "file": rel, "owner": qid, "slot": slot, "method": method,
             "evidence": str(evidence or "")[:240], "confidence": confidence,
             "outcome": outcome,
             "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if obj_id is not None:
        entry["obj_id"] = obj_id
    if final_file and final_file != rel:
        entry["final_file"] = final_file
    _append_jsonl(DATA_DIR / "image_ownership.jsonl", entry)


def _ownership_method_map(chapter_id):
    """final_file -> {method, confidence, page} from the ownership ledger
    (claimed rows only). Drives the per-row qa_status evidence: any image
    claimed only by a model fallback (isolated/full-page vision at non-high
    confidence) makes its owner row REVIEW_NEEDED, never silent."""
    out = {}
    path = DATA_DIR / "image_ownership.jsonl"
    if not path.exists():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("chapter_id") != chapter_id or row.get("outcome") != "claimed":
            continue
        for key in ("final_file", "file"):
            v = row.get(key)
            if v:
                out[v] = {"method": row.get("method"),
                          "confidence": row.get("confidence"),
                          "page": row.get("page")}
    return out


def carry_claims(chapter_id):
    """[{file, final_file, owner, slot, page}] for this chapter's carry claims.

    RUN-37. A carry claim is the weakest deterministic ownership: no heading
    was found above the figure on its own page, so it went to the block still
    open from the previous page. The caller decides whether independent
    geometry corroborates it -- see
    boundary_phased.ChapterRunner._corroborated_carry_files."""
    out = []
    path = DATA_DIR / "image_ownership.jsonl"
    if not path.exists():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("chapter_id") != chapter_id:
            continue
        if row.get("outcome") != "claimed":
            continue
        if row.get("method") != CARRY_CLAIM_SOURCE:
            continue
        out.append({"file": row.get("file"),
                    "final_file": row.get("final_file") or row.get("file"),
                    "owner": row.get("owner"),
                    "slot": row.get("slot"),
                    "page": row.get("page")})
    return out


def image_attribution_summary(chapter_id):
    """How this chapter's figures actually got their owner, from the ledgers.

    RUN-29 (OPH-001): the carry rate was only readable by counting "[IMG]
    page N: active-block carry" lines out of a run log, so it was not a
    metric anyone could watch regress. This recomputes the same split from
    the append-only ownership ledger -- claimed rows by `method` -- plus the
    unclaimed count from unmatched_images.jsonl, so every chapter reports
    its own attribution mix and a rising carry share is visible without
    reading logs.

    Returns {"claimed_by_method": {method: n}, "carry": n, "positional": n,
             "model": n, "claimed_total": n, "unclaimed": n,
             "carry_share": float|None}. carry_share is the share of CLAIMED
    figures owned by cross-page carry (the OPH-001 number)."""
    path = DATA_DIR / "image_ownership.jsonl"
    # One file can appear on several claimed rows (multi-draw share,
    # resume-relink), so key by the file and keep its LATEST claim -- then
    # count each file exactly once, the same de-duplication
    # _ownership_method_map applies when it drives qa_status.
    latest_method: dict = {}
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("chapter_id") != chapter_id:
                continue
            if row.get("outcome") != "claimed":
                continue
            key = row.get("final_file") or row.get("file")
            if not key:
                continue
            latest_method[key] = row.get("method") or "unknown"

    by_method: dict = {}
    for m in latest_method.values():
        by_method[m] = by_method.get(m, 0) + 1

    unclaimed = 0
    um_path = DATA_DIR / "unmatched_images.jsonl"
    if um_path.exists():
        try:
            for line in um_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    u = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if u.get("chapter_id") == chapter_id:
                    unclaimed += 1
        except OSError:
            pass

    carry = by_method.get(CARRY_CLAIM_SOURCE, 0)
    interval = by_method.get(INTERVAL_CLAIM_SOURCE, 0)
    positional = by_method.get("positional", 0) + \
        by_method.get("deterministic_geometry", 0) + \
        by_method.get("deterministic_ocr_geometry", 0)
    model = by_method.get("isolated_crop_vision", 0) + \
        by_method.get("full_page_vision", 0)
    claimed_total = sum(by_method.values())
    return {"claimed_by_method": by_method,
            "carry": carry,
            "interval": interval,
            "positional": positional,
            "model": model,
            "claimed_total": claimed_total,
            "unclaimed": unclaimed,
            "carry_share": (carry / claimed_total) if claimed_total else None}


def _answer_option_mismatch(rec, correct_id, option_rows):
    """Deterministic 'answer letter disagrees with its own solution' detector.
    Returns a reason string or None. NEVER corrects the data -- only flags it.
    (Live case proven on OPH-009-002: extracted B while the printed key and
    its own solution both point at C.)"""
    if not correct_id:
        return None
    sol = (rec.get("solution_text") or "").strip()
    if not sol:
        return None
    WORD = r"[0-9A-Za-z_]+"
    opt_text = {o["id"]: str(o.get("text") or "") for o in option_rows}
    opt_toks = {k: {w for w in re.findall(WORD, v.lower()) if len(w) > 3}
                for k, v in opt_text.items() if v}

    # ordered tokens for verbatim containment (the strongest signal: the
    # winning option's text appears word-for-word inside the sentence)
    opt_words = {k: re.findall(WORD, v.lower()) for k, v in opt_text.items() if v}

    def _overlap_letter(frag):
        norm = " ".join(re.findall(WORD, frag.lower()))
        if opt_words:
            verbatim = [k for k, ws in opt_words.items()
                        if ws and " ".join(ws) in norm]
            # exactly ONE option's text is verbatim-contained and it is NOT
            # the extracted answer -> the flip is provable on its own
            if len(verbatim) == 1:
                return verbatim[0]
            # more than one contained: beside-the-point sentence, skip
            if len(verbatim) > 1:
                return None
        f = {w for w in re.findall(WORD, frag.lower()) if len(w) > 3}
        if len(f) < 3:
            return None
        # RUN-54 (OPH-020 live CRASH): a row whose options are all blank
        # (bad_options) leaves opt_toks empty, and max() over an empty dict
        # raised ValueError out of build_final_question -> the WHOLE chapter
        # was lost to the per-chapter crash handler. A detector must never
        # take a chapter down; with no option text there is nothing to
        # compare, so there is no mismatch to report.
        if not opt_toks:
            return None
        scores = {k: len(f & s) for k, s in opt_toks.items()}
        best = max(scores, key=scores.get)
        if scores[best] >= max(3, scores.get(correct_id, 0) + 2):
            return best
        return None

    m = re.search("(?:correct" + r"\s+" + "answer|answer)" + r"\s*" + "(?:is|:)" + r"\s*" +
                  "(?:option" + r"\s+" + ")?" + "(?:the" + r"\s+" + ")?" + "([A-Da-d]|the" + r"\s+" + "[^.]+)", sol, re.I)
    if m:
        letter = m.group(1).strip()
        if len(letter) == 1 and letter.upper() in opt_text:
            if letter.upper() != correct_id:
                return (f"printed phrase 'answer is {letter}' disagrees with "
                        f"extracted correct_option {correct_id}")
        else:
            hit = _overlap_letter(letter)
            if hit and hit != correct_id:
                return (f"solution says the answer describes option {hit}'s "
                        f"content but correct_option is {correct_id}")
    _split_pat = re.compile(r"(?<=[.!?])\s+")
    first = _split_pat.split(sol, maxsplit=1)[0].strip()
    hit = _overlap_letter(first)
    if hit and hit != correct_id:
        return (f"solution opens on option-{hit} content but correct_option is "
                f"{correct_id} (key-letter flip suspect)")
    return None

def _ownership_page_map(chapter_id):
    """{final_rel_or_temp_rel: pdf_page} for one chapter, from the ownership
    ledger. Last write wins (a file re-claimed after a guard refusal updates
    cleanly). Zero-token; used by the export gate, the row builder and the
    split layer to keep every exported image tied to its source page."""
    out = {}
    path = DATA_DIR / "image_ownership.jsonl"
    if not path.exists():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("chapter_id") != chapter_id or row.get("outcome") != "claimed":
            continue
        for key in ("final_file", "file"):
            v = row.get(key)
            if v:
                out[v] = row.get("page")
    return out


# Locked final-name convention written by _rename_for_slot:
#   {SUBJ}/{SUBJ}-{chap:03d}-{qn:03d}_{Q|SOL|OPT_[A-D]}_{slot:02d}.webp
_FINAL_IMG_NAME_RE = re.compile(
    r"^(?P<subj>[^/]+)/(?P=subj)-(?P<chap>\d{3})-(?P<qn>\d{3})_"
    r"(?P<kind>Q|SOL|OPT_[A-D])_(?P<slotn>\d{2})\.webp$")


def _relink_resume_owned_images(chapter_id, subject, chapter_no,
                                chapter_records, image_files_by_q,
                                ownership_pages=None):
    """AUDIT-FIX (OBG-003, 2026-08-19): the RESUME path used to lose images.

    When a chapter is re-run after a pause/crash, its figures are already
    claimed and renamed under the final convention, so extract_real_images
    prints "figure bytes already owned on disk -- skipping duplicate
    extraction" and hands NOTHING to the claimers. The fresh chapter_records
    then export with EMPTY image lists while the correctly-named files sit on
    disk -- the chapter's live exports "forget" every figure an earlier run
    owned (proven live: OBG ch3 q3-q11 all exported empty solution_images
    after the quota-paused run was resumed).

    The ownership ledger is append-only and survives the restart, so the
    previous run's claim rows are still on disk with full provenance. This
    re-attaches them -- EVIDENCE ONLY, no position/filename guessing:
      1. ledger row for THIS chapter with outcome claimed|shared,
      2. final_file matches the locked naming convention for THIS chapter,
      3. the file truly exists under ASSETS_DIR/questions/,
      4. the owner question still exists in this run's chapter_records
         (a claim whose record was later reconciled away is skipped and
         LOUDLY noted -- it is a stale claim, not attachable evidence).
    Owner and slot come from the ledger row (never re-derived), so a
    multi-draw "shared" row re-attaches to BOTH owners exactly as claimed.
    On a non-resume run every row here is already present in
    image_files_by_q (added by _rename_for_slot in memory), so this is a
    no-op. Mutates image_files_by_q (and ownership_pages) in place; returns
    the list of relink notes for logging."""
    notes = []
    path = DATA_DIR / "image_ownership.jsonl"
    if not path.exists():
        return notes
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return notes
    seen = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("chapter_id") != chapter_id:
            continue
        if row.get("outcome") not in ("claimed", "shared"):
            continue
        final_rel = row.get("final_file") or ""
        m = _FINAL_IMG_NAME_RE.match(final_rel)
        if not m or m.group("subj") != subject:
            continue
        if int(m.group("chap")) != int(chapter_no):
            continue
        owner = row.get("owner") or ""
        try:
            qn = int(owner.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if chapter_records is not None and qn not in chapter_records:
            print(f"  [IMG] resume-relink skipped: ledger row {final_rel} -> "
                  f"{owner}, but that question is not in this run's records "
                  f"(stale claim; left for review, never re-attached blind)")
            continue
        if not (ASSETS_DIR / "questions" / final_rel).exists():
            continue
        entry = image_files_by_q.setdefault(
            qn, {"question": [], "solution": []})
        kind = m.group("kind")
        if kind == "SOL":
            slot_list = entry.setdefault("solution", [])
        elif kind == "Q":
            slot_list = entry.setdefault("question", [])
        else:
            slot_list = entry.setdefault("option", {}).setdefault(kind[-1], [])
        key = (qn, final_rel)
        if key in seen or final_rel in slot_list:
            continue
        seen.add(key)
        slot_list.append(final_rel)
        slot_list.sort()  # _01, _02, ... slot order is deterministic
        if ownership_pages is not None and row.get("page") is not None:
            ownership_pages.setdefault(final_rel, row.get("page"))
        notes.append({"q_no": qn, "kind": kind, "file": final_rel,
                      "method": row.get("method"), "page": row.get("page"),
                      "outcome": row.get("outcome"),
                      "owner_seen": owner})
    for n in notes:
        print(f"  [IMG] resume-relink: {n['file']} -> q{n['q_no']} "
              f"({n['kind']}) restored from prior run's ledger claim "
              f"({n['outcome']} via {n['method']}, page {n['page']})")
    if notes:
        print(f"  [IMG] resume-relink: {len(notes)} prior-run claim(s) "
              f"restored for {chapter_id} (evidence: ownership ledger + "
              f"locked file names, zero guessing)")
    return notes


def claim_block_images_ocr(imgs, pdf_path, file_page, subject, chapter_no,
                           chapter_records, image_files_by_q, chapter_id=None,
                           active_block=None, dpi=150, section=None):
    """run-13 LEVEL 2 (deterministic OCR-anchored geometry): the SAME
    closest-heading-above rule as claim_block_images, but the block headings
    come from OCR of the RENDERED page instead of the (garbled/absent) PDF
    text layer. Deterministic, zero Gemini calls. Runs only on leftovers from
    L1. Returns the files STILL unclaimed (they flow to L3 vision)."""
    if not imgs:
        return []
    # run-21 §2.1: a CONTINUATION page (this book's p158: a solution that runs
    # over from p157, with no printed header of its own) OCRs to ZERO anchors.
    # The old code returned every image untouched, so a perfectly determinable
    # figure fell all the way to the vision passes and then to
    # "all_levels_failed". When a block is still open from the previous page
    # that block IS the deterministic owner -- keep going instead of bailing.
    # AUDIT-FIX: use the SAME cached TEXT+OCR union anchors as L1, so both
    # deterministic levels resolve ownership against an identical header set
    # (previously L1=text-only/record-filtered, L2=OCR-only -- two different
    # views of the same page could disagree).
    headers = union_block_headers_on_page(pdf_path, file_page, chapter_records,
                                          dpi=dpi, section=section)
    if not headers and active_block is None:
        return imgs
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
        if owner is active_block and active_block is not None \
                and len(active_block) >= 3:
            gap = int(file_page) - int(active_block[2])
            if gap > MAX_CARRY_PAGES:
                print(f"  [IMG] page {file_page}: refusing long positional_carry "
                      f"({gap} pages from heading on p{active_block[2]}) "
                      f"-- left for review")
                leftover.append(rel)
                continue
        kind, qn = owner[0], owner[1]
        if qn not in chapter_records:
            leftover.append(rel)
            continue
        new_rel = _rename_for_slot(rel, qn, kind, subject, chapter_no,
                                   image_files_by_q,
                                   claim_source=(CARRY_CLAIM_SOURCE
                                                 if owner is active_block
                                                 else "positional"),
                                   evidence=("L2 fallback after L1 leftover: "
                                             "closest union (text+OCR) "
                                             "question/solution heading above "
                                             f"image on page {file_page}"))
        if new_rel:
            entry = image_files_by_q.setdefault(qn, {"question": [], "solution": []})
            entry[kind].append(new_rel)
            qid = f"{subject}-{chapter_no:03d}-{qn:03d}"
            print(f"  [IMG] page {file_page}: OCR block position -> {rel} -> {qid} ({kind})")
        else:
            leftover.append(rel)
    return leftover








def claim_page_images(imgs, pdf_path, file_page, subject, chapter_no,
                      chapter_records, image_files_by_q, active_block=None,
                      section=None):
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
                                  active_block=active_block, section=section)
    if leftover:
        leftover = claim_page_images_one_to_one(leftover, pdf_path, file_page,
                                                subject, chapter_no, chapter_records,
                                                image_files_by_q,
                                                section=section)
    return leftover


# ======================================================================
# AUDIT-FIX (ch. 28 wrong-QID root cause): UNION anchor harvest
# ----------------------------------------------------------------------
# The old L1 claimer saw ONLY the text layer AND only headings whose q_no
# already existed in chapter_records (question_headers_on_page's
# `qn in chapter_records` filter). Two compounding results:
#   1. a heading the (garbled) text layer missed was GEOMETRICALLY
#      invisible, so its figures attached to the previous question by
#      "closest heading above" (OPH-028-006 collected Q5/Q7's figures);
#   2. a heading printed for a question whose RECORD was not extracted yet
#      was filtered out, with the same wrong-owner result.
# Heading detection must depend on what is PRINTED, not on which questions
# the model happened to extract before the image pass ran. The functions
# below harvest block anchors from BOTH layers (text + rendered-page OCR),
# merge them line-wise, and apply record membership only at CLAIM-COMMIT
# time (a figure whose owner has no record yet stays unclaimed for the
# chapter-end second pass instead of being glued to a neighbour).
# ======================================================================

_OCR_ANCHOR_CACHE = {}          # (pdf, page, dpi) -> [(kind, qn, y_pt)]
_OCR_ANCHOR_XY = {}             # same key -> [(kind, qn, y, x, strong)]
_OCR_ANCHOR_CACHE_MAX = 64      # tiny tuples; covers several chapters
_UNION_HEADER_CACHE = {}        # (pdf, page, dpi, section, rec_sig) -> headers
_UNION_DROP_LOGGED = set()      # (pdf, page, kind, qn, y) already printed
# ChapterRunner injects visual (render+OCR) headers: page -> [(kind, n, y)]
_VISUAL_HEADERS_BY_PAGE = {}


def _ocr_anchors_for_page(pdf_path, file_page, dpi=150):
    """[(kind, q_no, y_pdf_pt)] OCR anchors for one page, cached. Tesseract
    costs ~1-2s/page and the SAME page anchors are consulted by the union
    harvest, both claim levels, last_block_on_page and the chapter-end gate
    -- cache by (pdf, page, dpi). Page content never changes mid-run.

    PHANTOM-ANCHOR FILTER (audit, ch. 28 p617 class): tesseract reads digit
    runs off FIGURE TEXTURE (a knurled instrument read "1." -> a phantom
    question anchor inside the figure's bbox -> the figure attached to a
    question that does not even exist on this page). An OCR anchor whose
    (x, y) lands INSIDE a drawn image rect that is not page-background is
    therefore rejected. Text-layer anchors are immune (they are real glyph
    runs)."""
    key = (str(pdf_path), int(file_page), int(dpi))
    hit = _OCR_ANCHOR_CACHE.get(key)
    if hit is not None:
        return hit
    rendered = render_page_png(pdf_path, file_page, dpi=dpi)
    if not rendered[0]:
        anchors, anchors_xy = [], []
    else:
        img, scale, page_h_pt = rendered
        try:
            _raw = ocr_page_anchors_xy(img, scale, page_h_pt)
        except Exception:
            _raw = []
        # tolerate legacy 3-tuple stubs (tests patch ocr_* with 3-tuples):
        # x=None -> the phantom filter simply can't fire that call; stubs are
        # treated as strong (test harnesses assert claim behavior, not OCR)
        anchors_xy = []
        for a in _raw:
            if len(a) == 3:
                anchors_xy.append((a[0], a[1], a[2], None, True))
            elif len(a) == 4:
                anchors_xy.append((a[0], a[1], a[2], a[3], True))
            else:
                anchors_xy.append(a)
        anchors = [(k, q, y) for k, q, y, _x, _s in anchors_xy]
        if anchors_xy:
            try:
                pos = image_positions_on_page(pdf_path, file_page) or {}
                page_w_pt = (rendered[0].width / rendered[1])
                rects = []
                for p in pos.values():
                    r = _rect_from_position(p)
                    if (r[2] - r[0] >= 0.80 * page_w_pt
                            and r[3] - r[1] >= 0.80 * page_h_pt):
                        continue          # page background/overlay, keep anchors
                    rects.append(r)
                if rects:
                    kept = []
                    kept_xy = []
                    for k, q, y, x, strong in anchors_xy:
                        if x is None:
                            kept.append((k, q, y))   # legacy stub: no geometry
                            kept_xy.append((k, q, y, x, strong))
                            continue
                        if any(_rect_contains(r, (x - 0.5, y - 0.5,
                                                  x + 0.5, y + 0.5), pad=6.0)
                               for r in rects):
                            print(f"  [IMG] page {file_page}: dropping phantom OCR "
                                  f"anchor ({k} q{q}) read from inside a figure "
                                  f"(x={x:.0f}, y={y:.0f}) -- figure texture, "
                                  f"not a real heading")
                            continue
                        kept.append((k, q, y))
                        kept_xy.append((k, q, y, x, strong))
                    anchors = kept
                    anchors_xy = kept_xy   # AUDIT-FIX: the xy cache must carry
                                           # the FILTERED anchors -- the union
                                           # reads it first (phantoms were
                                           # leaking back in through the raw
                                           # side-channel)
            except Exception:
                pass                     # filter is best-effort, never blocks
    _OCR_ANCHOR_CACHE[key] = anchors
    _OCR_ANCHOR_XY[key] = anchors_xy
    while len(_OCR_ANCHOR_CACHE) > _OCR_ANCHOR_CACHE_MAX:
        _OCR_ANCHOR_CACHE.pop(next(iter(_OCR_ANCHOR_CACHE)))
        if len(_OCR_ANCHOR_XY) > _OCR_ANCHOR_CACHE_MAX:
            _OCR_ANCHOR_XY.pop(next(iter(_OCR_ANCHOR_XY)))
    return anchors


def _raw_text_headers_on_page(pdf_path, file_page):
    """[(kind, q_no, y_baseline, has_kw)] -- every line-start question/solution
    heading the text layer prints, WITHOUT the chapter_records membership
    filter. (The filtered variants question_headers_on_page /
    solution_headers_on_page keep their existing contract for other
    callers; membership is a CLAIM-time decision, not a detection-time
    one.) has_kw = the line carried an explicit 'Question'/'Q' prefix --
    used to keep bare numbered LIST items ('2. Vitreomacular traction...')
    out of anchor duty inside the solutions section."""
    headers, seen = [], set()
    for y, wl in _page_word_lines(pdf_path, file_page):
        line = " ".join(t for _, t in wl)
        sol = list(re.finditer(r"Solution\s+to\s+Question\s+(\d{1,3})",
                               line, re.IGNORECASE))
        if sol:
            for m in sol:
                qn = int(m.group(1))
                if ("solution", qn) not in seen:
                    seen.add(("solution", qn))
                    headers.append(("solution", qn, y, True))
            continue
        m = QSTEM_HEADING_RE.match(line)
        if m and "solution" not in line.lower():
            qn = int(m.group(1))
            if ("question", qn) not in seen:
                seen.add(("question", qn))
                has_kw = bool(re.match(r"^\s*Q(?:uestion)?\s*[.:]?\s*\d",
                                       line, re.I))
                headers.append(("question", qn, y, has_kw))
    return headers


def _plausible_qn_for_chapter(qn, chapter_records):
    """Anchor q_no plausibility (replaces the old strict membership test):
    a MEMBER of chapter_records is always plausible; a non-member is
    plausible only inside the records' [min, max+2] span -- this covers a
    real question whose record has not been extracted yet (the audit's A2
    case) while still rejecting far-out foreign numbers. With NO records
    yet (first window) every printed anchor is accepted: detection must not
    depend on extraction progress."""
    recs = [q for q in chapter_records if isinstance(q, int)]
    if not recs:
        return True
    if qn in chapter_records:
        return True
    return min(recs) <= qn <= max(recs) + 2


def union_block_headers_on_page(pdf_path, file_page, chapter_records, dpi=150,
                                section=None):
    recs = [q for q in (chapter_records or {}) if isinstance(q, int)]
    rec_sig = (min(recs) if recs else None, max(recs) if recs else None,
               len(recs))
    ukey = (str(pdf_path), int(file_page), int(dpi), section, rec_sig)
    hit = _UNION_HEADER_CACHE.get(ukey)
    if hit is not None:
        return hit
    headers = _union_block_headers_on_page_uncached(
        pdf_path, file_page, chapter_records, dpi=dpi, section=section)
    _UNION_HEADER_CACHE[ukey] = headers
    if len(_UNION_HEADER_CACHE) > 256:
        _UNION_HEADER_CACHE.pop(next(iter(_UNION_HEADER_CACHE)))
    return headers


def _union_block_headers_on_page_uncached(pdf_path, file_page, chapter_records,
                                          dpi=150, section=None):
    """[(kind, q_no, y)] block anchors for one page = text layer UNION OCR,
    sorted top-first (largest y first) -- the SAME consumer contract as
    block_headers_on_page.

    Merge rules (conservative):
      * both layers: same visual line (|dy| <= 8pt, same kind) -> keep the
        TEXT-layer anchor (digit decode is exact when the line decodes at
        all; garbling kills whole lines, it does not misread digits);
        OCR-only anchors fill the lines the text layer lost;
      * plausibility: _plausible_qn_for_chapter on every anchor;
      * question anchors below the FIRST (topmost) solution header are
        dropped (list items inside solution prose are not stems) -- this is
        the docstring block_headers_on_page always had; its code used min()
        (LOWEST solution header) instead of max() (audit B1);
      * section='S' (solutions-section page): QUESTION anchors without an
        explicit 'Question N:' / 'Q N.' keyword prefix are dropped -- a bare
        '2.' line there is a numbered list item / figure label inside
        solution prose, not a question stem (ch. 25 p571-573: the OCT figure
        list stole real figures onto the question side of q2/q4/q5).
    """
    text_full = [(k, q, y, kw) for k, q, y, kw in
                 _raw_text_headers_on_page(pdf_path, file_page)
                 if _plausible_qn_for_chapter(q, chapter_records)]
    for k, q, y in (_VISUAL_HEADERS_BY_PAGE or {}).get(int(file_page), []):
        if _plausible_qn_for_chapter(q, chapter_records):
            if not any(k == tk and abs(ty - y) <= 10.0
                       for tk, _q, ty, _ in text_full):
                text_full.append((k, q, y, True))
    ocr_full = [(k, q, y, st) for k, q, y, st in
                [(a[0], a[1], a[2], a[4] if len(a) > 4 else 2)
                 for a in _OCR_ANCHOR_XY.get(
                     (str(pdf_path), int(file_page), int(dpi)),
                     [(k, q, y, None, 2) for k, q, y in _ocr_anchors_for_page(
                         pdf_path, file_page, dpi=dpi)])]
                if _plausible_qn_for_chapter(q, chapter_records)]
    if section == "S":
        # drop bare-number question anchors entirely: text keeps only
        # explicit-keyword lines (kw=True); OCR keeps only strength==2
        # (an explicit 'Question N:'/'Q N.' prefix). Bare '2.' list items
        # must not anchor anything in the solutions section.
        text_full = [t for t in text_full if t[0] != "question" or bool(t[3])]
        ocr_full = [t for t in ocr_full if t[0] != "question" or t[3] == 2]
    text_h = [(k, q, y) for k, q, y, _kw in text_full]
    ocr_h = [(k, q, y) for k, q, y, _st in ocr_full]
    merged = list(text_h)
    ocr_only = []
    for k, q, y in ocr_h:
        if any(k == tk and abs(ty - y) <= 8.0 for tk, _tq, ty in text_h):
            continue                      # same visual line: text layer wins
        merged.append((k, q, y))
        ocr_only.append((k, q, y))
    if ocr_only:
        # AUDIT-FIX: phantom corroboration, applied ONLY to WEAK OCR-only
        # anchors (bare single-token lines like '2.' -- the tesseract shape
        # of figure-texture / watermark-ink reads; ch. 28 p617/p627 class).
        # '_page_word_lines' returns (y, words) even when the glyphs are
        # garbled, so a real heading still occupies a text line; a phantom
        # does not. Keyword/multi-word OCR anchors ('Question 6:', 'Solution
        # to Question N:') are strong enough to stand alone.
        # Truly scanned pages (no text layer at all): nothing to corroborate
        # against -> all anchors stand.
        try:
            text_ys = [y for y, wl in _page_word_lines(pdf_path, file_page)]
        except Exception:
            text_ys = []
        if text_ys:
            xy = {(a[0], a[1], a[2]): (a[3], a[4] if len(a) > 4 else True)
                  for a in _OCR_ANCHOR_XY.get(
                      (str(pdf_path), int(file_page), int(dpi)), [])}
            kept, dropped = [], []
            for a in merged:
                if a in ocr_only:
                    _x, strong = xy.get(a, (None, True))
                    if not strong and not any(abs(ty - a[2]) <= 7.0
                                              for ty in text_ys):
                        dropped.append(a)
                        continue
                kept.append(a)
            for k, q, y in dropped:
                dkey = (str(pdf_path), int(file_page), k, q, int(round(y)))
                if dkey in _UNION_DROP_LOGGED:
                    continue
                _UNION_DROP_LOGGED.add(dkey)
                print(f"  [IMG] page {file_page}: dropping uncorroborated weak "
                      f"OCR anchor ({k} q{q} @y{y:.0f}) -- bare token with no "
                      f"text-layer line at that height (figure texture / "
                      f"watermark phantom)")
            merged = kept
    qs = [t for t in merged if t[0] == "question"]
    ss = [t for t in merged if t[0] == "solution"]
    if ss:
        first_sol_y = max(y for _k, _q, y in ss)   # topmost = first printed
        qs = [t for t in qs if t[2] > first_sol_y]
    return sorted(qs + ss, key=lambda t: -t[2])


def chapter_anchor_pages(pdf_path, page_numbers, chapter_records, dpi=150,
                         page_sections=None):
    """{q_no: {"pages": set, "question": set, "solution": set}} -- printed
    anchor index for one chapter, zero-token (text layer + cached OCR per
    page). `pages` = union of both kinds (used by figure_page_mismatch);
    the per-kind sets let the declared-figure check compare the image side
    against anchors of the SAME side. Written once per chapter for the export
    gate and reusable by critique/review tooling."""
    idx = {}
    for p in page_numbers:
        try:
            for kind, qn, _y in union_block_headers_on_page(
                    pdf_path, p, chapter_records, dpi=dpi,
                    section=(page_sections or {}).get(p)):
                rec = idx.setdefault(qn, {"pages": set(), "question": set(),
                                          "solution": set()})
                rec["pages"].add(int(p))
                rec[kind].add(int(p))
        except Exception:
            continue
    return idx


def share_reprint_obj_ids(pdf_path, page, imgs_empty, chapter_records,
                          image_files_by_q, subject, chapter_no,
                          visual_recs=None):
    """Same XObject id drawn again (OBG obj 2222 p29+p30) is ONE figure.

    extract_real_images skips byte-identical files, so the second page has
    no leftover to claim. Attach the already-owned final file to the
    interval owner of THIS draw. Never Gemini.
    """
    if not imgs_empty:
        return 0
    try:
        pos = image_positions_on_page(pdf_path, page) or {}
    except Exception:
        return 0
    owned = _owned_files_by_obj(subject)
    if not owned:
        return 0
    n = 0
    for oid, info in pos.items():
        if isinstance(oid, str) and "@d" in oid:
            try:
                oid_n = int(str(oid).split("@")[0])
            except ValueError:
                continue
        else:
            try:
                oid_n = int(oid)
            except (TypeError, ValueError):
                continue
        final = owned.get(oid_n)
        if not final:
            continue
        y_img = info[0]
        h = info[4] if len(info) > 4 else 0
        if h and y_img + h < y_img:
            y_img = y_img + h
        owner = None
        if visual_recs:
            import header_index as hi
            owner = hi.owner_of_point(visual_recs, page, y_img)
        if owner is None:
            headers = union_block_headers_on_page(
                pdf_path, page, chapter_records)
            owner = next(((k, q) for k, q, yh in reversed(headers)
                          if yh > y_img), None)
        if owner is None:
            continue
        kind, qn = owner
        if qn not in chapter_records:
            continue
        entry = image_files_by_q.setdefault(qn, {"question": [], "solution": []})
        if final in (entry.get(kind) or []):
            continue
        # Reprint stays with the ORIGINAL owner. Do not clone Sol1 vulva
        # onto Q2 just because it is redrawn on the next page.
        orig_qn = None
        m = _FINAL_IMG_NAME_RE.match(final) if final else None
        if m:
            try:
                orig_qn = int(m.group("qn"))
            except (TypeError, ValueError):
                orig_qn = None
        if orig_qn is not None and orig_qn != qn:
            print(f"  [IMG] reprint keep: obj {oid_n} on page {page} "
                  f"already owned by q{orig_qn}; not cloning to q{qn}")
            continue
        entry.setdefault(kind, []).append(final)
        print(f"  [IMG] reprint share: obj {oid_n} on page {page} -> "
              f"q{qn} {kind} (same figure, not a new extract)")
        _record_image_ownership(
            subject, f"{subject}-{chapter_no:03d}", page, final,
            f"{subject}-{chapter_no:03d}-{qn:03d}", kind,
            "reprint_obj_id",
            f"same XObject {oid_n} redrawn on page {page}; shared file",
            confidence="high", outcome="shared", obj_id=oid_n,
            final_file=final)
        n += 1
    return n


def _owned_files_by_obj(subject):
    """obj_id -> final_file from this subject's claimed ledger rows."""
    out = {}
    path = DATA_DIR / "image_ownership.jsonl"
    if not path.exists():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("subject") != subject:
            continue
        if row.get("outcome") not in ("claimed", "shared"):
            continue
        oid = row.get("obj_id")
        fn = row.get("final_file") or row.get("file")
        if oid is not None and fn:
            out[int(oid) if str(oid).isdigit() else oid] = fn
    return out


def last_block_on_page(pdf_path, file_page, dpi=150, chapter_records=None,
                       section=None):
    """(kind, q_no) of the LAST (lowest) printed block heading on a page, or
    None when the page prints no heading.

    run-21: the window loop used to compute active_block ONCE per window
    and reuse that stale value for every page in it, so a figure at the top of
    page N (owned by the block that started on the bottom of page N-1) was
    attributed to whatever block was open at the START of the window -- pages
    away. Advancing the carry page by page makes the cross-page rule actually
    cross ONE page, which is what "continuation" means.

    AUDIT-FIX: the carry chain must see the SAME anchor set as the claims.
    The old OCR-only view both missed text-layer solution headers (p627's
    sol-11 header was invisible to the carry, so the next page's top figure
    was carried into a phantom OCR "q2" instead of solution-11) and accepted
    phantom anchors the union rejects. One anchor source end-to-end.
    """
    headers = union_block_headers_on_page(pdf_path, file_page,
                                          chapter_records or {}, dpi=dpi,
                                          section=section)
    if not headers:
        return None
    kind, qn, _y = headers[-1]        # sorted top-first: last == lowest
    return (kind, qn)




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
PASS_STATUS_PARTIAL = "PARTIAL"
PASS_STATUS_UNRESOLVED = "UNRESOLVED"







def _ledger_pass(chapter_id, subject, chapter_no, pass_name, window_pages,
                 status, n_items, note=""):
    row = {"chapter_id": chapter_id, "subject": subject,
           "chapter_no": chapter_no, "pass": pass_name,
           "pages": sorted(window_pages), "status": status,
           "items": n_items, "note": note,
           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    _append_jsonl(DATA_DIR / "page_ledger.jsonl", row)
    return row










def _export_gate_violations(chapter_records, image_files_by_q, unresolved_ledger,
                            chapter_id, unresolved_images=(), unresolved_orphans=(),
                            anchor_pages=None, ownership_pages=None):
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

    # AUDIT-FIX (A5): "claimed" is not "proven". Two new deterministic
    # violations make wrong-QID ownership (or silent figure loss) IMPOSSIBLE
    # to hide behind a clean gate:
    #   missing_declared_figure -- the extraction model declared a figure for
    #       this question's stem/solution, but no image is attached on that
    #       side (ch. 28: Q7/Q8 shipped empty-handed while their figures sat
    #       on OPH-028-006);
    #   figure_page_mismatch -- an attached image's extraction page (from the
    #       ownership ledger, now written for EVERY claim) lies outside the
    #       question's printed anchor pages +/-1 (a continuation figure one
    #       page away is legitimate; a figure harvested several pages away is
    #       a wrong-owner claim).
    # Both checks are skipped (conservatively) when their evidence maps are
    # unavailable -- an unprovable check must never block an export.
    for qn, rec in sorted(chapter_records.items()):
        entry = image_files_by_q.get(qn) or {}
        for side, flag_key in (("question", "has_figure_in_question"),
                               ("solution", "has_figure_in_solution")):
            if rec.get(flag_key) and not (entry.get(side) or []):
                detail = (f"model declared {flag_key} but no {side} "
                          "image is attached -- figure lost or "
                          "misattributed to a neighbour")
                # DECLARED-but-missing has two very different meanings, and
                # the run-28 proof on the OPH book separated them:
                #   * STRONG evidence: an image on (or next to) this
                #     question's anchor pages exhausted all ownership levels
                #     (unresolved/unmatched) -- the book PRINTS a figure for
                #     it, we failed to own it -> gate violation (OPH-028 q8;
                #     its perimeter-test figure sat unclaimed on p611).
                #   * weak: nothing on its pages ever surfaced as an
                #     unclaimed figure -> the model over-declared
                #     (OPH-028 q15/q22 carry no printed figure) -> advisory
                #     only, never blocks a clean export (model noise must not
                #     cry wolf next to real ownership gaps).
                anch_map = (anchor_pages or {}).get(qn) or {}
                anch = anch_map.get(side, set()) if isinstance(anch_map, dict) \
                    else set(anch_map)
                nearby_pages = set()
                if anch:
                    nearby_pages = {p + d for p in anch for d in (-1, 0, 1, 2)}
                strong = False
                truth_pages = set()
                for u in (unresolved_images or ()):
                    if u.get("page"):
                        truth_pages.add(int(u["page"]))
                try:
                    _um_path = DATA_DIR / "unmatched_images.jsonl"
                    if _um_path.exists():
                        for _l in _um_path.read_text(encoding="utf-8").splitlines():
                            if not _l.strip():
                                continue
                            try:
                                _u = json.loads(_l)
                            except json.JSONDecodeError:
                                continue
                            if _u.get("chapter_id") == chapter_id and _u.get("page"):
                                truth_pages.add(int(_u["page"]))
                except OSError:
                    pass
                if anchor_pages is not None and anch:
                    strong = bool(nearby_pages & truth_pages)
                else:
                    # no anchor map available: keep the row visible but weak
                    strong = False
                if strong:
                    violations.append((f"missing_declared_figure_{side}", qn,
                                       detail + " (printed figure exists on "
                                       f"{sorted(truth_pages & nearby_pages)} "
                                       "but exhausted all ownership levels)"))
                else:
                    _append_jsonl(DATA_DIR / "export_gate_advisory.jsonl",
                                  {"chapter_id": chapter_id,
                                   "kind": f"declared_figure_missing_{side}",
                                   "q_no": qn, "detail": detail,
                                   "strength": "model-declared only (no "
                                               "unclaimed figure on its "
                                               "pages) -- advisory",
                                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    if anchor_pages and ownership_pages:
        for qn, entry in (image_files_by_q or {}).items():
            _a = anchor_pages.get(qn)
            anchors = (_a.get("pages") if isinstance(_a, dict) else _a) or set()
            if not anchors:
                continue
            lo, hi = min(anchors) - 1, max(anchors) + 2
            for kind in ("question", "solution"):
                for rel in (entry.get(kind) or []):
                    pg = ownership_pages.get(rel)
                    if pg is None:
                        continue          # no provenance recorded: unprovable
                    if not (lo <= pg <= hi):
                        violations.append(("figure_page_mismatch", qn,
                                           f"{kind} image {rel} extracted from "
                                           f"page {pg} but q{qn}'s printed "
                                           f"anchors are on {sorted(anchors)} "
                                           "-- wrong-owner suspect"))
        for qn, entry in (image_files_by_q or {}).items():
            for letter, rels in (entry.get("option") or {}).items():
                _a = anchor_pages.get(qn)
                anchors = (_a.get("pages") if isinstance(_a, dict) else _a) or set()
                if not anchors:
                    continue
                lo, hi = min(anchors) - 1, max(anchors) + 2
                for rel in rels:
                    pg = ownership_pages.get(rel)
                    if pg is None:
                        continue
                    if not (lo <= pg <= hi):
                        violations.append(("figure_page_mismatch", qn,
                                           f"option {letter} image {rel} from "
                                           f"page {pg}, anchors on "
                                           f"{sorted(anchors)} -- wrong-owner "
                                           "suspect"))
    for qn, rec in sorted(chapter_records.items()):
        if not (rec.get("question_text") or "").strip():
            violations.append(("missing_stem", qn,
                               "no question_text after batch+retry+rescue"))
        opts = rec.get("options") or {}
        if len(opts) < 4 or any(not str(v or "").strip() for v in opts.values()):
            violations.append(("bad_options", qn, f"options={sorted(opts)}"))
        if not rec.get("correct_option"):
            violations.append(("missing_answer", qn, "no correct_option"))
        # shipped text = sanitized text (OBG-010-021 live: model returned only
        # 'Solution to Question 21:' -> sanitize strips it to '' but the raw
        # rec check passed, silently shipping a READY row with no solution).
        sol_check, _ = sanitize_solution_text(rec.get("solution_text"),
                                              own_qn=qn)
        if not sol_check.strip():
            violations.append(("missing_solution", qn,
                               "no solution_text (header-only model answer "
                               "stripped by sanitize)"))
        elif len(sol_check.strip()) < MIN_SOLUTION_CHARS:
            # RUN-34: OPH-001 q3 shipped ". X" as its whole explanation and
            # passed, because the test above only caught an empty string.
            violations.append(("solution_too_short", qn,
                               f"solution_text is only "
                               f"{len(sol_check.strip())} chars "
                               f"(< {MIN_SOLUTION_CHARS}): "
                               f"{sol_check.strip()[:60]!r}"))
        if rec.get("_stem_suspect_reason"):
            # run-13: quarantined suspect stem (kept for review, not deleted)
            # is still a violation -- the chapter must not look clean while a
            # stem MAY be solution prose.
            violations.append(("suspect_stem", qn,
                               f"stem quarantined (kept for review): "
                               f"{rec['_stem_suspect_reason']}"))
        if rec.get("_options_suspect_reason"):
            # run-22: options MAY belong to another question. Deliberately
            # NOT auto-corrected and NOT rejected -- but the chapter must not
            # report clean while an option set is unverified.
            violations.append(("options_suspect", qn,
                               f"options flagged for manual review (record "
                               f"unchanged): {rec['_options_suspect_reason']}"))
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


def try_reassign_cap_hit(rel, page, y_img, chapter_records, image_files_by_q,
                         subject, chapter_no, visual_recs=None):
    """Fix 4: before discard, if a neighbour declared a figure and has 0
    images, and this leftover's Y sits in that neighbour's header interval,
    claim it there. Unresolved only if reassign fails. Never Gemini.
    """
    if y_img is None or not chapter_records:
        return None
    try:
        import header_index as hi
    except Exception:
        return None
    q_ivals = {iv["n"]: iv for iv in hi.intervals(visual_recs or [], hi.T_QUESTION)}
    s_ivals = {iv["n"]: iv for iv in hi.intervals(visual_recs or [], hi.T_SOLUTION)}
    guess = hi.boundary_tie_owner(
        visual_recs or [], page, y_img, chapter_records, image_files_by_q)
    if guess is None:
        guess = hi.owner_of_point(visual_recs or [], page, y_img)
    seeds = []
    if guess:
        seeds.append(int(guess[1]))
    for qn in list(chapter_records):
        if isinstance(qn, int):
            seeds.append(qn)
    seen = set()
    for qn in seeds:
        for nb in (qn - 1, qn + 1, qn):
            if nb in seen or nb not in chapter_records:
                continue
            seen.add(nb)
            rec = chapter_records[nb]
            entry = image_files_by_q.setdefault(nb, {"question": [], "solution": []})
            for kind, flag, ivals in (
                    ("solution", "has_figure_in_solution", s_ivals),
                    ("question", "has_figure_in_question", q_ivals)):
                if not rec.get(flag):
                    continue
                if entry.get(kind):
                    continue
                iv = ivals.get(nb)
                if iv and not hi.interval_contains_point(iv, page, y_img):
                    continue
                if iv is None and guess and int(guess[1]) != nb:
                    continue
                new_rel = _rename_for_slot(
                    rel, nb, kind, subject, chapter_no, image_files_by_q,
                    claim_source="positional",
                    evidence=(f"Fix4 cap-hit reassign: leftover y={y_img:.0f} "
                              f"on p{page} matches neighbour q{nb} {kind} "
                              f"(declared figure, empty slot)"))
                if new_rel:
                    entry.setdefault(kind, []).append(new_rel)
                    print(f"  [IMG] cap-hit reassign: {rel} -> q{nb} {kind} "
                          f"(neighbour interval)")
                    return new_rel
    return None


_IMG_PLACEHOLDER_RE = re.compile(r"\[IMG\]", re.I)


def count_img_placeholders(text):
    return len(_IMG_PLACEHOLDER_RE.findall(text or ""))


def strip_unbacked_img_markers(text, n_files):
    """Remove model-only image markers when geometry proves that the block
    owns no image files.

    A marker is layout metadata, not medical content.  On OPH-008 q9 the
    isolated crop visibly contains text and a table but no figure; Gemini
    nevertheless emitted ``[IMG]``.  Keeping that invented marker makes the
    exported text claim a nonexistent asset.  We remove only this
    unambiguous zero-file case; when one or more files exist, marker/file
    ordering remains unresolved and the strict mismatch check is retained.
    Returns (clean_text, removed_count).
    """
    n_files = int(n_files or 0)
    n = count_img_placeholders(text)
    if n_files != 0 or n == 0:
        return text or "", 0
    return _IMG_PLACEHOLDER_RE.sub("", text or ""), n


def reconcile_img_placeholders(text, n_files, side=None):
    """Match [IMG] tokens to geometrically owned files by COUNT only.

    Equal -> ok (files already reading-order). Unequal -> conflict note.
    Never insert/delete tokens to force a match.

    RUN-33: `side` is optional and only labels the note. The persisted
    qa_reason used to omit it, so a reviewer reading the flag could not tell
    whether the question or the solution side disagreed -- only the stdout
    line knew.
    """
    n_tok = count_img_placeholders(text)
    n_files = int(n_files or 0)
    if n_tok == n_files:
        return True, ""
    where = f" ({side})" if side else ""
    return False, (f"img_placeholder_count_mismatch{where}: text has {n_tok} "
                   f"[IMG] token(s), interval owns {n_files} file(s)")


def flag_high_image_counts(chapter_records, image_files_by_q):
    """Soft review_suggested: high_image_count. Never drop files."""
    counts = []
    for qn, rec in (chapter_records or {}).items():
        entry = (image_files_by_q or {}).get(qn) or {}
        nq = len(entry.get("question") or [])
        ns = len(entry.get("solution") or [])
        counts.append(nq + ns)
    if not counts:
        return
    avg = sum(counts) / max(len(counts), 1)
    # Far above chapter average, or a historically-absurd single-side pile.
    thresh = max(avg * 3.0, 6.0)
    for qn, rec in (chapter_records or {}).items():
        entry = (image_files_by_q or {}).get(qn) or {}
        nq = len(entry.get("question") or [])
        ns = len(entry.get("solution") or [])
        total = nq + ns
        if total >= thresh or ns >= 6 or nq >= 8:
            reasons = list(rec.get("_review_reasons") or [])
            note = f"review_suggested: high_image_count (q={nq} s={ns} ch_avg={avg:.1f})"
            if note not in reasons:
                reasons.append(note)
            rec["_review_reasons"] = reasons
            rec["_review_suggested"] = "high_image_count"
            print(f"  [IMG] {note} on q{qn} — data kept, not dropped")


def apply_img_placeholder_reconcile(chapter_records, image_files_by_q):
    """Flag [IMG] tokens that disagree with the owned file count.

    RUN-33: skip blocks whose text was parsed DETERMINISTICALLY (crop_parse,
    off the PDF text layer or OCR of the crop). Such a parser never emits
    [IMG] tokens -- it has no notion of where a figure sits -- so comparing
    its 0 tokens against the owned files flagged every figured item in the
    chapter. OPH-001 live: once the OCR fallback started working, 22/23
    questions and 23/23 solutions became deterministic and this produced 17
    mismatches across 13 of 23 questions, pushing the chapter to 15
    REVIEW_NEEDED. The count check exists to catch a MODEL dropping or
    inventing [IMG] markers, which is a genuine failure mode; it carries no
    information about text a model never wrote. The figures still ship in
    question.images / solution.images exactly as before."""
    for qn, rec in (chapter_records or {}).items():
        entry = (image_files_by_q or {}).get(qn) or {}
        reasons = list(rec.get("_review_reasons") or [])
        for side, field, mkey in (
                ("question", "question_text", "_q_text_method"),
                ("solution", "solution_text", "_s_text_method")):
            if str(rec.get(mkey) or "") == "geometric_text":
                continue
            ok, note = reconcile_img_placeholders(
                rec.get(field), len(entry.get(side) or []), side=side)
            if not ok and side == "solution":
                # RUN-44 (OPH-001 q23 live): a solution that explains a
                # figure-based question naturally refers to THAT figure, and
                # the figure is owned by the QUESTION side -- it is drawn in
                # the stem, not in the explanation. q23's solution walks the
                # marked diagram ("A- Superior oblique, E- Inferior rectus")
                # and owns no file of its own, which read as a missing figure
                # and flagged the row.
                #
                # Only when the solution claims MORE figures than it owns.
                n_tok = count_img_placeholders(rec.get(field))
                n_own = len(entry.get("solution") or [])
                n_q = len(entry.get("question") or [])
                if n_q and n_tok > n_own and n_tok <= n_own + n_q:
                    ok = True
                    print(f"  [IMG] q{qn} solution: {n_tok} [IMG] token(s) "
                          f"explained by the question's {n_q} figure(s) -- "
                          f"cross-reference, not a missing figure")
            if not ok:
                # RUN-45 (OPH-001 q11 live) + RUN-46 (owner's refinement):
                # text with no [IMG] token is only harmless when the block
                # owns EXACTLY ONE figure -- there is a single place it can go,
                # so the converter can attach it from images[] without being
                # told where. With two or more figures the token is what
                # establishes which image sits where in the reading order, so
                # a missing marker there IS a defect and must flag.
                #
                # The other direction always flags: text that references a
                # figure nobody owns is a dangling marker (q15 -- the model
                # invented [IMG] for a question the book prints no figure for).
                n_tok = count_img_placeholders(rec.get(field))
                n_files = len(entry.get(side) or [])
                if n_tok == 0 and n_files == 1:
                    print(f"  [IMG] q{qn} {side}: 1 figure attached with no "
                          f"[IMG] token -- advisory only, a single figure "
                          f"needs no inline position")
                else:
                    if note not in reasons:
                        reasons.append(note)
                    print(f"  [IMG] q{qn} {side}: {note}")
        rec["_review_reasons"] = reasons


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




def main():
    """THE extraction entry point. There is exactly ONE extraction method in
    this repo now: the boundary-phased engine (boundary_phased.py). The old
    multi-pass architecture (process_pdf and its recovery passes) is REMOVED;
    this shim keeps every existing caller (dashboard Run button, CLI) working
    with zero changes on their side."""
    import boundary_phased
    boundary_phased.run_all(PDFS)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--recover", "--auto-recover"):
        # The recover modes belonged to the removed multi-pass engine. The
        # supported heal path now is: delete the chapter's id from
        # state.json -> chapters_done (or leave it; un-committed chapters are
        # retried automatically) and press Run -- the boundary engine
        # re-extracts that chapter cleanly. Content-level fixes go through
        # the /review queue, never through silent rewrites.
        print("[RETIRED] --recover/--auto-recover belonged to the old "
              "multi-pass engine, which has been removed. Fix flow now: "
              "re-run the chapter (it is retried while absent from "
              "chapters_done) and resolve flags in the /review queue.")
        sys.exit(2)
    main()
