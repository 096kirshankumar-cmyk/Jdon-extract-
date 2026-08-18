#!/usr/bin/env python3
"""
Hybrid chapter validation pipeline -- ISOLATED MODULE.

Deliberately NOT wired into qbank_pipeline.py's production path. Run it
after the extractor, against the same OUTPUT_ROOT:

    # Stage 1 only (deterministic, zero tokens, safe anywhere):
    python3 qbank_validator.py --report-only

    # Full hybrid (deterministic -> full-chapter witness audit ->
    # neighborhood verification -> evidence-gated patches):
    python3 qbank_validator.py --audit --audit-budget 150

Architecture (agreed design):
  1. Deterministic validation: code-only checks. Source of truth.
  2. Full-chapter audit: the model is a WITNESS -- it enumerates which
     question numbers / components are visible on the pages. CODE diffs
     that enumeration against the JSONL. The model never says PASS/FAIL
     and this stage never patches.
  3. Neighborhood verification: every flag (stage 1 or 2) becomes a
     small anchored ask over the flag's own page window (+-1 page).
  4. Evidence-backed patch application: candidates must fuzzy-match the
     page text layer (token overlap >= 0.95 auto-apply, 0.80-0.95 human
     queue, < 0.80 reject; no text layer -> human queue, never auto).
     fill_only merge via qbank_pipeline machinery; deletes only for
     proven duplicate text (tombstone log); all-or-nothing per chapter.
  5. Final re-verification + status state machine with attempt caps, so
     permanently unfixable (source-gap) chapters stop burning quota.

Both modes emit data/validation_report.json; audit mode adds fix-per-call
metrics so the on/off benchmark the user wants is measurable, not vibes.
"""

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_DIR", "./qbank_output"))

# evidence-acceptance thresholds (token overlap, candidate vs page text)
AUTO_APPLY_MIN = 0.95
HUMAN_QUEUE_MIN = 0.80
# duplicate-text auto-delete threshold -- stricter: deletes are the only
# destructive op, everything else is fill-only.
DELETE_AUTO_MIN = 0.98

MAX_AUDIT_ATTEMPTS = 2           # per chapter, then status -> source_gap (stop re-burning quota)
ANSWER_KEY_ONLY_MIN_SHARE = 0.8  # >=80% "has answer, no solution" -> book prints no explanations (RC-4)
SUSPECT_DENSITY_MIN = 0.5        # missing-solution rate that is ALWAYS suspicious...
SUSPECT_DENSITY_MULT = 3.0       # ...or 3x book median, whichever triggers first

HIGH, LOW = "high", "low"

# run-4 audit mirrors of the pipeline guards (kept module-local: this file is
# deliberately isolated from qbank_pipeline's import graph).
MIN_IMAGE_BYTES = 1500        # <1.5KB webp = broken crop (PSY-003-014 shipped 414B)
MAX_QUESTION_IMAGES = 3       # >3 question-side figures = over-attribution suspect
MAX_SOLUTION_IMAGES = 2       # >2 solution-side figures = over-attribution suspect
                              # (user report: 7 figures on one solutions page
                              # collapsed into 2 solutions)
# run-21 DYNAMIC CAP MIRROR: qbank_pipeline lets a MODEL-DECLARED owner
# (figure map / full-page vision / isolated crop) exceed the deterministic
# cap up to a hard ceiling, because real questions do cite 4-6 figures and
# the flat cap was dumping legitimate figures into unmatched_images.jsonl.
# The validator must not re-flag what the pipeline deliberately allowed:
# only counts above the CEILING are over-attribution now; counts between the
# soft cap and the ceiling are reported at INFO-level severity for review.
# Keep these three constants in sync with qbank_pipeline.py.
IMAGE_CAP_CEILING_QUESTION = 8
IMAGE_CAP_CEILING_SOLUTION = 6
DANGLING_END_RE = re.compile(r"(:|\u2014|\u2013|\u2022)\s*$")
TERMINAL_PUNCT = ".!?)\"'\u201d\u00bb"
OPTION_LINE_START_RE = re.compile(r"^\s*Option\s+([A-D])\b\s*[:.)]\s*", re.IGNORECASE)
OPTION_LINE_ANY_RE = re.compile(r"Option\s+([A-D])\b\s*[:.)]", re.IGNORECASE)
SOLUTION_TO_Q_RE = re.compile(r"Solution\s+to\s+Question\s+(\d{1,3})", re.IGNORECASE)

# ---- run-7 cross-field contamination mirrors -------------------------------
# A non-empty question_text is NOT automatically a valid stem: if it opens
# with explanation language, or its tokens are substantially contained in the
# row's own solution, it is solution prose that contaminated the stem field.
CONTAMINATION_TOKEN_SHARE = 0.8
EXPLANATION_START_RE = re.compile(
    r"^\s*(?:option\s+[a-d]\s*[:.)\-]|ans(?:wer)?\s*[:.)\-]|the\s+correct\s+(?:answer|option)\b|"
    r"(?:hence|thus|therefore|so)\s*,\s*(?:the\s+)?(?:correct\s+)?option\b|"
    r"correct\s+answer\s+is\b|the\s+(?:correct\s+)?answer\s+is\b|"
    r"solution\s*[:.)\-]|explanation\s*[:.)\-]|answer\s*[:.)\-]|"
    r"solution\s+to\s+question\s+\d+|explanation\s+of\s+question\s+\d+)",
    re.IGNORECASE)
# run-17: mirror qbank_pipeline._stem_reject_reason semantics EXACTLY --
# the validator used the OLD naive token-containment rule, so it flagged
# GOOD stems that the pipeline now accepts (run-12/15 narrowing: a short
# question-shaped stem whose solution restates it, e.g. ch26 q1 "...is
# called ___") as contaminated_question -> false positives on every re-run.
_MAX_REAL_STEM_LEN = 250
_QUESTION_SHAPED_RE = re.compile(
    r"\?\s*$|which\b|what\b|who\b|whom\b|how\b|why\b|identify\b|choose\b|"
    r"select\b|best\b|most likely\b|correct\b|diagnos|drug\b|treatment\b|"
    r"following\b|regarding\b|according\b|is the\b|are the\b|of the\b",
    re.IGNORECASE)
_OCR_NOISE_LINE_RES = [
    re.compile(r"^\s*[-–—.·]?\s*\d{1,4}\s*[-–—.·]?\s*$"),          # 12 / -12- / 12.
    re.compile(r"^\s*page\s*\d{1,4}\s*(of\s*\d{1,4})?\s*$", re.I),  # Page 12 of 300
    re.compile(r"^\s*(https?://|www\.)\S+\s*$", re.I),              # urls
    re.compile(r"^\s*(©|\(c\)|copyright).*$", re.I),                # copyright
]


def _stem_contamination_reason(qtext, stext):
    """Cross-field contamination proof for a final row's question_text.
    Returns a reason string, or None when the text plausibly IS a stem.
    run-17: mirrors qbank_pipeline._stem_reject_reason -- explanation-opener
    is always contamination; token-containment only fires for DECLARATIVE/
    implausibly-long text (run-12 narrowing: solutions RESTATE question-shaped
    stems); a stem that IS its own solution verbatim (reverse containment) is
    contamination no matter how question-shaped it looks (run-15 ch7 q23/25)."""
    t = (qtext or "").strip()
    if not t:
        return None
    if EXPLANATION_START_RE.match(t):
        return "opens with explanation-style language"
    s = (stext or "").strip()
    if s and len(t) >= 60 and token_overlap(t, s) >= CONTAMINATION_TOKEN_SHARE:
        # reverse containment: the whole solution fits inside the would-be
        # stem -> the two fields are the SAME text -> always contamination
        if token_overlap(s, t) >= CONTAMINATION_TOKEN_SHARE:
            return "question_text is the row's own solution verbatim"
        if len(t) > _MAX_REAL_STEM_LEN or not _QUESTION_SHAPED_RE.search(t):
            return (f"{token_overlap(t, s):.0%} of its tokens appear in the "
                    f"row's own solution (>= {CONTAMINATION_TOKEN_SHARE:.0%})")
    return None


def _ocr_noise_lines(stext):
    """Lines of a solution that are page-level OCR noise (page numbers,
    footers, watermarks) -- run-7 hardening #5/#6. Returns the offending
    lines (conservative: whole-line matches only, prose never touched)."""
    bad = []
    for ln in (stext or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if any(r.match(s) for r in _OCR_NOISE_LINE_RES):
            bad.append(s[:60])
    return bad[:5]


def _payload_coherence(stem, row):
    """Share of stem tokens present in the row's own options+solution (which
    side of a duplicate pair owns the stem). Mirrors the pipeline resolver."""
    toks = [t for t in re.findall(r"\w+", (stem or "").lower()) if len(t) > 2]
    payload = " ".join(filter(None, [
        (row.get("solution") or {}).get("text") or "",
        " ".join(str(o.get("text") or "") for o in row.get("options") or []),
    ]))
    ptoks = set(re.findall(r"\w+", payload.lower()))
    if not toks or not ptoks:
        return 0.0
    return sum(1 for t in toks if t in ptoks) / len(toks)


# ============================================================
# small helpers
# ============================================================

def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokens(s):
    return Counter(re.findall(r"\w+", _norm(s)))


def token_overlap(candidate, page_text):
    """Share of the candidate's word tokens (count-aware) present in the
    page text. Deterministic, explainable anti-confabulation evidence."""
    cand, page = _tokens(candidate), _tokens(page_text)
    total = sum(cand.values())
    if not total or not page:
        return 0.0
    return sum((cand & page).values()) / total


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def q_no_of(row):
    return _as_int(str(row.get("id", "")).rsplit("-", 1)[-1])


def load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def append_jsonl(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def flag(chapter_id, kind, detail, q_no=None, severity=HIGH, pages=None, source="deterministic", **extra):
    d = {"chapter_id": chapter_id, "kind": kind, "q_no": q_no, "severity": severity,
         "detail": detail, "pages": pages or [], "source": source}
    d.update(extra)
    return d


# ============================================================
# STAGE 1 -- deterministic validation (zero tokens)
# ============================================================

def check_row(row, assets_questions):
    """Per-question structural checks against the final JSONL schema."""
    cid, flags = row.get("chapter_id"), []
    qn = q_no_of(row)
    qtext = (row.get("question") or {}).get("text")
    opts = row.get("options") or []
    correct = row.get("correct_options") or []
    sol = row.get("solution") or {}

    # AUDIT-FIX: the pipeline's own per-row verdict travels with the row --
    # surface it in the report so a row never LOOKS fine while its status
    # disagrees (the INCOMPLETE/REVIEW_NEEDED classes of the build step).
    qa = row.get("qa_status")
    if qa == "INCOMPLETE":
        flags.append(flag(cid, "qa_incomplete",
                          f"{row.get('id')}: pipeline-marked INCOMPLETE -- "
                          f"{'; '.join(row.get('qa_reasons') or [])[:200]}", qn, HIGH))
    elif qa == "REVIEW_NEEDED":
        flags.append(flag(cid, "qa_review_needed",
                          f"{row.get('id')}: REVIEW_NEEDED -- "
                          f"{'; '.join(row.get('qa_reasons') or [])[:200]}", qn, LOW))
    if not qtext or not qtext.strip():
        flags.append(flag(cid, "empty_question", f"{row.get('id')}: empty question text", qn))
    # run-13: pipeline-quarantined suspect stem (kept, not deleted) -- the
    # question ships WITH a stem that MAY be solution prose; flag loudly so
    # it is never silently accepted as clean content.
    if row.get("stem_suspect"):
        flags.append(flag(cid, "suspect_stem",
                          f"{row.get('id')}: stem quarantined by pipeline "
                          f"({row['stem_suspect']}) -- review before use", qn, HIGH))
    # run-22: options MAY belong to another question (harvested off a solution
    # page). The record is intentionally shipped unchanged -- neither rejected
    # nor auto-corrected -- so surface it loudly for a human instead.
    if row.get("options_suspect"):
        flags.append(flag(cid, "options_suspect",
                          f"{row.get('id')}: options flagged for MANUAL REVIEW "
                          f"({row['options_suspect']}) -- record shipped as "
                          f"extracted, verify against the source page", qn, HIGH))
    opt_ids = {str(o.get("id", "")).strip().upper() for o in opts}
    nonempty = sum(1 for o in opts if (o.get("text") or "").strip())
    if len(opts) != 4 or opt_ids != {"A", "B", "C", "D"} or nonempty != 4:
        flags.append(flag(cid, "bad_options",
                          f"{row.get('id')}: {len(opts)} options ({sorted(opt_ids)}), "
                          f"{nonempty} non-empty -- expected A-D x4", qn))
    # A blank correct answer makes the question unusable even when every
    # other structural field exists; elevate it above the generic option flag.
    blank_correct = [str(c).strip().upper() for c in correct
                     if not str(next((o.get("text") for o in opts
                                     if str(o.get("id", "")).strip().upper() == str(c).strip().upper()), "") or "").strip()]
    if blank_correct:
        flags.append(flag(cid, "blank_correct_option", f"{row.get('id')}: correct option(s) "
                          f"{blank_correct} have null/empty text", qn, HIGH))
    if not correct:
        flags.append(flag(cid, "missing_answer", f"{row.get('id')}: no correct option", qn))
    elif any(str(c).strip().upper() not in opt_ids for c in correct):
        flags.append(flag(cid, "answer_mismatch",
                          f"{row.get('id')}: correct option {correct} not among option ids {sorted(opt_ids)}", qn))
    sol_text = (sol.get("text") or "")
    sol_strip = sol_text.strip()
    # ---- run-7 cross-field contamination checks ---------------------------
    # "Field is populated" is NOT treated as "field is valid": a question_text
    # that is really solution prose, or a solution carrying page-level OCR
    # noise, is flagged for retry/manual review instead of passing silently.
    if (qtext or "").strip():
        qcontam = _stem_contamination_reason(qtext, sol_text)
        if qcontam:
            flags.append(flag(cid, "contaminated_question",
                              f"{row.get('id')}: question_text is not a stem "
                              f"({qcontam}) -- cross-field contamination "
                              f"suspect, needs retry/review", qn, HIGH))
    if sol_strip:
        noise = _ocr_noise_lines(sol_text)
        if noise:
            flags.append(flag(cid, "ocr_noise_solution",
                              f"{row.get('id')}: solution contains page-level "
                              f"OCR noise line(s): {noise}", qn))
    if not sol_strip:
        flags.append(flag(cid, "missing_solution", f"{row.get('id')}: empty solution text", qn))
    else:
        s = sol_text.rstrip()
        # REAL truncation only (run-4 audit: the old 'no terminal punctuation'
        # heuristic produced ~53 false positives against this book's
        # bullet-list endings; only 2 of 55 flags were real). Patterns kept:
        # dangling connector ends ('...criteria:', '...--'), raw trailing
        # space after a word (stream cut mid-flow: '...• During ').
        # zip-8 audit addendum: a dangling ':' header lead-in ('... are
        # listed below:') is NOT truncation when the printed table it
        # introduces lives in solution.tables -- all 6 such flags in the
        # resumed run were row-verified false positives (001-012, 006-013,
        # 006-019, 011-015, 013-003, 023-005).
        # run-24: the same exemption must cover FIGURES, not just tables.
        # The pipeline sweep already excuses a dangling ':' when a solution
        # image is attached ("lead-in explained by figure, retry skipped" ->
        # truncated_solution_suppressed_by_image), but the validator re-raised
        # it as a HIGH truncated_solution. Ch.60 q3/q21 were both flagged that
        # way with the figure sitting right there in solution.images, which
        # makes a clean chapter look broken and trains reviewers to ignore the
        # flag. One rule, both places: ':' + (table OR image) = a lead-in.
        _leadin_explained = s.endswith(":") and (sol.get("tables") or sol.get("images"))
        if DANGLING_END_RE.search(s) and not _leadin_explained:
            flags.append(flag(cid, "truncated_solution",
                              f"{row.get('id')}: solution ends on a dangling connector (...{s[-50:]!r})", qn))
        elif sol_text != s and re.search(r"[A-Za-z0-9]$", s) and s[-1] not in TERMINAL_PUNCT:
            flags.append(flag(cid, "truncated_solution",
                              f"{row.get('id')}: solution cut mid-flow (ends ...{s[-50:]!r} + trailing space)", qn))
        elif len(s) < 60 and s[-1] not in TERMINAL_PUNCT \
                and not sol.get("tables") and not sol.get("images"):
            flags.append(flag(cid, "short_bare_solution",
                              f"{row.get('id')}: very short solution, no table/figure (...{s[-40:]!r})",
                              qn, LOW))
        # print-furniture / recitation dumps (PSY-032-001/002/003)
        m_hd = SOLUTION_TO_Q_RE.search(sol_text)
        if m_hd:
            if m_hd.start() <= 2:
                flags.append(flag(cid, "solution_header_furniture",
                                  f"{row.get('id')}: solution begins with 'Solution to Question N' header",
                                  qn, LOW))
            else:
                flags.append(flag(cid, "solution_recitation_dump",
                                  f"{row.get('id')}: embedded 'Solution to Question {m_hd.group(1)}' "
                                  f"header mid-solution -- possible whole-block dump", qn))
        # foreign 'Option X:' head (PSY-009-007): the line cannot belong to
        # this row's own options.
        m_opt = OPTION_LINE_START_RE.match(sol_strip)
        opt_map = {str(o.get("id", "")).strip().upper(): str(o.get("text") or "") for o in opts}
        if m_opt:
            o_text = opt_map.get(m_opt.group(1).upper())
            if o_text is not None:
                otoks = [t for t in re.findall(r"\w+", o_text.lower()) if len(t) > 2]
                if otoks:
                    head = " ".join(re.findall(r"\w+", sol_strip.lower())[:25])
                    if sum(1 for t in otoks[:6] if t in head) == 0:
                        flags.append(flag(cid, "foreign_option_head",
                                          f"{row.get('id')}: solution starts with an 'Option "
                                          f"{m_opt.group(1).upper()}' line that does not match its own "
                                          f"option ({o_text[:60]!r}) -- wrong-owner fragment", qn))
        # option<->solution disagreement (PSY-008-007): the solution explains
        # 'Option X' with content that never matches the row's option X text
        # (row options polluted by neighboring explanations).
        disagree = []
        for m in OPTION_LINE_ANY_RE.finditer(sol_text):
            letter = m.group(1).upper()
            o_text = opt_map.get(letter)
            if not o_text:
                continue
            otoks = [t for t in re.findall(r"\w+", o_text.lower()) if len(t) > 2]
            if len(otoks) < 3:
                continue
            seg = sol_text[m.end():m.end() + 200].lower()
            if sum(1 for t in otoks[:6] if t in seg) == 0:
                disagree.append(letter)
        if disagree:
            flags.append(flag(cid, "option_solution_disagree",
                              f"{row.get('id')}: solution's 'Option {sorted(set(disagree))}' lines do not "
                              f"describe the row's own option texts -- options likely polluted", qn))
    # image refs: missing, or suspiciously tiny (broken crop shipped)
    for side in ("question", "solution"):
        for img in (row.get(side) or {}).get("images") or []:
            fpath = (img or {}).get("file")
            if not fpath:
                continue
            fobj = Path(assets_questions) / fpath
            if not fobj.exists():
                flags.append(flag(cid, "image_ref_missing",
                                  f"{row.get('id')}: {side} image not on disk: {fpath}", qn))
            elif fobj.stat().st_size < MIN_IMAGE_BYTES:
                flags.append(flag(cid, "suspicious_tiny_image",
                                  f"{row.get('id')}: {side} image only {fobj.stat().st_size}B "
                                  f"(< {MIN_IMAGE_BYTES}) -- likely a broken crop: {fpath}", qn))
    # AUDIT-FIX (A5): a DECLARED figure with no attached image is a silent
    # loss / wrong-owner indicator. Emitted LOW here (model declarations are
    # noisy); validate_deterministic upgrades to HIGH when an unresolved or
    # unmatched image exists on the row's own source pages -- that pattern is
    # a REAL printed figure we failed to own (OPH-028 q8 class).
    qimgs_now = (row.get("question") or {}).get("images") or []
    simgs_now = (row.get("solution") or {}).get("images") or []
    if row.get("declared_has_figure_in_question") and not qimgs_now:
        flags.append(flag(cid, "declared_figure_missing",
                          f"{row.get('id')}: extraction declared a QUESTION figure but no "
                          f"image is attached (lost or misattributed to a neighbour)", qn, LOW))
    if row.get("declared_has_figure_in_solution") and not simgs_now:
        flags.append(flag(cid, "declared_figure_missing",
                          f"{row.get('id')}: extraction declared a SOLUTION figure but no "
                          f"image is attached (lost or misattributed to a neighbour)", qn, LOW))
    n_qimg = len((row.get("question") or {}).get("images") or [])
    if n_qimg > IMAGE_CAP_CEILING_QUESTION:
        flags.append(flag(cid, "over_attributed_images",
                          f"{row.get('id')}: {n_qimg} question-side images "
                          f"(> ceiling {IMAGE_CAP_CEILING_QUESTION}) -- over-attribution", qn, LOW))
    n_simg = len((row.get("solution") or {}).get("images") or [])
    if n_simg > IMAGE_CAP_CEILING_SOLUTION:
        flags.append(flag(cid, "over_attributed_solution_images",
                          f"{row.get('id')}: {n_simg} solution-side images "
                          f"(> ceiling {IMAGE_CAP_CEILING_SOLUTION}) -- over-attribution "
                          f"(solutions-page figures mapped by block position)", qn, LOW))
    # duplicate tables inside one solution (PSY-012-008/009-005)
    tbls = sol.get("tables") or []
    seen_tbl, dup_tbl = set(), 0
    for t in tbls:
        key = re.sub(r"\s+", "", str(t.get("markdown") or "").lower())
        if key and key in seen_tbl:
            dup_tbl += 1
        if key:
            seen_tbl.add(key)
    if dup_tbl:
        flags.append(flag(cid, "duplicate_table",
                          f"{row.get('id')}: {dup_tbl} duplicate table(s) inside one solution", qn, LOW))
    # stray printed answer-key table parked in a random solution (info only;
    # harmless but useful ground truth for key cross-checks)
    for t in tbls:
        md = str(t.get("markdown") or "")
        if "answer" in str(t.get("type", "")).lower() and "Correct Option" in md:
            flags.append(flag(cid, "stray_answer_key_table",
                              f"{row.get('id')}: solution embeds the printed Answer Key table "
                              f"(informational; used for key cross-checks)", qn, LOW))
            break
    return flags


def check_chapter(chapter_id, rows):
    """Chapter-level structural checks: duplicates and numbering coverage."""
    flags = []
    ids = [r.get("id") for r in rows]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        flags.append(flag(chapter_id, "duplicate_id", f"id appears {ids.count(dup)}x: {dup}"))

    # near-duplicate question stems (same question surviving merge twice).
    # Guardrails against boilerplate-stem false positives: text-only verdicts
    # need enough material -- short/generic stems and length-mismatched pairs
    # are skipped here and left to the model stages to disambiguate.
    norm_texts = [(r.get("id"), _norm((r.get("question") or {}).get("text"))) for r in rows]
    norm_texts = [(i, t) for i, t in norm_texts if t]
    for a in range(len(norm_texts)):
        for b in range(a + 1, len(norm_texts)):
            id_a, t_a = norm_texts[a]
            id_b, t_b = norm_texts[b]
            if min(len(t_a), len(t_b)) < 80:
                continue  # too short to decide on text alone
            if max(len(t_a), len(t_b)) > 1.2 * min(len(t_a), len(t_b)):
                continue  # real duplicates are near-equal length
            sim = SequenceMatcher(None, t_a[:400], t_b[:400]).ratio()
            if sim >= 0.95:
                row_a = next((r for r in rows if r.get("id") == id_a), {})
                row_b = next((r for r in rows if r.get("id") == id_b), {})
                ca, cb = _payload_coherence(t_a, row_a), _payload_coherence(t_b, row_b)
                # the wrong-owner copy is the side whose OTHER payload
                # (solution/options/answer) never mentions the shared stem
                # (run-4: PSY-012-001 carried 012-013's stem with a mania
                # solution -- coherence 0 vs the true owner).
                suspect = id_a if ca < cb else id_b
                flags.append(flag(chapter_id, "duplicate_text",
                                  f"{id_a} ~ {id_b} (similarity {sim:.3f}); "
                                  f"payload coherence {ca:.2f} vs {cb:.2f} -- {suspect} is the "
                                  f"wrong-owner suspect (its solution describes a different stem)",
                                  _as_int(suspect.rsplit("-", 1)[-1]), similarity=round(sim, 3),
                                  suspect_id=suspect))

    qns = sorted({q_no_of(r) for r in rows if q_no_of(r) is not None})
    if qns:
        s = set(qns)

    # foreign-solution-segment: a solution TAIL that lives VERBATIM inside a
    # sibling row's solution belongs to the other question (zip-8: PSY-009-017
    # carried PSY-009-012's Alzheimer's paragraphs after its own correct
    # solution). Shingle-8 overlap over normalized text, segment >= 250 chars.
    sols = [(r.get("id"), (r.get("solution") or {}).get("text") or "") for r in rows]
    sols = [(i, t) for i, t in sols if len(t) > 400]
    def _shingles(t, n=8):
        w = re.findall(r"\w+", t.lower())
        return {" ".join(w[i:i + n]) for i in range(0, max(0, len(w) - n + 1))}
    sh = {i: _shingles(t) for i, t in sols}
    for a in range(len(sols)):
        for b in range(a + 1, len(sols)):
            id_a, id_b = sols[a][0], sols[b][0]
            common = sh[id_a] & sh[id_b]
            if len(common) < 25:  # << 200 char of verbatim overlap
                continue
            # Counting every shared shingle grossly overstates one contiguous
            # overlap because adjacent shingles share seven of eight words.
            # Report the largest contiguous matching token block instead.
            wa = re.findall(r"\w+", sols[a][1].lower())
            wb = re.findall(r"\w+", sols[b][1].lower())
            blocks = SequenceMatcher(None, wa, wb).get_matching_blocks()
            best = max(blocks, key=lambda b: b.size)
            seg_chars = len(" ".join(wa[best.a:best.a + best.size]))
            if seg_chars < 250:
                continue
            # the parasitic copy is usually the LONGER solution (real + foreign tail)
            la, lb = len(sols[a][1]), len(sols[b][1])
            suspect, other = (id_a, id_b) if la > lb else (id_b, id_a)
            flags.append(flag(chapter_id, "foreign_solution_segment",
                              f"{suspect}: ~{seg_chars} chars of its solution ALSO appear verbatim in "
                              f"{other} -- foreign tail suspected; scan & trim",
                              _as_int(suspect.rsplit("-", 1)[-1]), other_id=other))

    # suspect_truncated_table: solution ends on a header lead-in ('... shown
    # below:') AND its table has the SAME header as a sibling's but far fewer
    # rows (zip-8: PSY-001-011 Erikson table 5 rows vs 001-012's full 8).
    hdr_rows = {}
    for r in rows:
        st = (r.get("solution") or {}).get("text") or ""
        for t in (r.get("solution") or {}).get("tables") or []:
            md = (t.get("markdown") or "").strip()
            if not md:
                continue
            hd = re.sub(r"\s+", "", md.splitlines()[0].lower())
            body = sum(1 for ln in md.splitlines() if ln.strip().startswith("|") and "---" not in ln) - 1
            hdr_rows.setdefault(hd, []).append((r.get("id"), body, st.rstrip(), md.splitlines()[0][:60]))
    for hd, items in hdr_rows.items():
        if len(items) < 2:
            continue
        biggest = max(x[1] for x in items)
        for rid, body, st, hdr_txt in items:
            if body > 0 and biggest - body >= 3 and st.endswith(":"):
                flags.append(flag(chapter_id, "suspect_truncated_table",
                                  f"{rid}: table '{hdr_txt}' has {body} row(s) vs sibling's "
                                  f"{biggest} with the same header AND the solution ends on a "
                                  f"header lead-in -- table likely cut",
                                  _as_int(rid.rsplit("-", 1)[-1])))
        for missing in [n for n in range(min(qns), max(qns) + 1) if n not in s]:
            flags.append(flag(chapter_id, "numbering_gap",
                              f"question {missing} absent (series runs {min(qns)}..{max(qns)})", missing))
        if min(qns) != 1:
            flags.append(flag(chapter_id, "numbering_start",
                              f"series starts at {min(qns)}, not 1 -- earlier questions may be lost",
                              min(qns), LOW))
    return flags


def is_answer_key_only(rows, min_share=ANSWER_KEY_ONLY_MIN_SHARE):
    """RC-4 [PATTERN]: some book sections print an answer key with NO
    explanations. Missing solutions there are source truth, not defects."""
    with_answer = [r for r in rows if (r.get("correct_options") or [])]
    if len(rows) < 5 or not with_answer:
        return False
    no_sol = sum(1 for r in with_answer if not ((r.get("solution") or {}).get("text") or "").strip())
    return no_sol / len(with_answer) >= min_share


def validate_deterministic(output_root=OUTPUT_ROOT, explicit_source_gap=()):
    """Stage 1 over the whole output. Returns (flags_by_chapter, summary)."""
    output_root = Path(output_root)
    data_dir, assets_q = output_root / "data", output_root / "assets" / "questions"
    rows = load_jsonl(data_dir / "questions.jsonl")
    by_chapter = {}
    for r in rows:
        by_chapter.setdefault(r.get("chapter_id"), []).append(r)

    explicit = set(explicit_source_gap)
    flags_by, suppressions, sol_rates = {}, {}, {}
    for cid, crows in sorted(by_chapter.items()):
        row_flags = [f for r in crows for f in check_row(r, assets_q)]
        ch_flags = check_chapter(cid, crows)
        if cid in explicit or is_answer_key_only(crows):
            before = len(row_flags)
            row_flags = [f for f in row_flags if f["kind"] != "missing_solution"]
            suppressions[cid] = before - len(row_flags)
        else:
            n = len(crows) or 1
            sol_rates[cid] = sum(1 for r in crows
                                 if not ((r.get("solution") or {}).get("text") or "").strip()) / n
        flags_by[cid] = row_flags + ch_flags

    # suspect density vs book median (answer-key-only chapters excluded so
    # they don't poison the baseline). max() = the STRICTER of the two rules:
    # an absolute alarm (>=50% of a chapter unexplained) on a healthy book,
    # a relative alarm (>=3x median) on a sloppy run. Never fires below 50%.
    if sol_rates:
        rates = sorted(sol_rates.values())
        median = rates[len(rates) // 2]
        threshold = max(SUSPECT_DENSITY_MIN, SUSPECT_DENSITY_MULT * median)
        for cid, rate in sol_rates.items():
            if rate >= threshold:
                flags_by[cid].append(flag(cid, "suspect_density",
                                          f"{rate:.0%} of chapter missing solutions vs book median {median:.0%}"))

    # sidecar artifacts the extractor already wrote.
    for orph in load_jsonl(data_dir / "orphans.jsonl"):
        cid = orph.get("chapter_id")
        if cid:
            flags_by.setdefault(cid, []).append(
                flag(cid, "orphan_unresolved", "unclaimed Gemini fragment (q_no unknown)",
                     pages=orph.get("pdf_pages") or orph.get("new_pages") or []))
    for um in load_jsonl(data_dir / "unmatched_images.jsonl"):
        cid = um.get("chapter_id")
        if cid:
            flags_by.setdefault(cid, []).append(
                flag(cid, "image_unclaimed",
                     f"page {um.get('page')} figure not owned by any question: {um.get('files')}",
                     pages=[um.get("page")] if um.get("page") else []))
    # run-13: unresolved_images.jsonl entries are gate-relevant unless
    # deterministically junk (broken crop < MIN_IMAGE_BYTES). A single model
    # "decorative" verdict must NOT clear this (page-4 class: a real Q1
    # figure was called decorative and the chapter still printed CLEAN).
    for ui in load_jsonl(data_dir / "unresolved_images.jsonl"):
        cid = ui.get("chapter_id")
        if not cid or ui.get("deterministic_junk"):
            continue
        flags_by.setdefault(cid, []).append(
            flag(cid, "image_unresolved",
                 f"page {ui.get('page')} figure unresolved after all ownership "
                 f"levels: {ui.get('file')} (method={ui.get('method') or '?'})",
                 pages=[ui.get("page")] if ui.get("page") else []))

    # AUDIT-FIX (A5): surface the PIPELINE GATE's own anchor-accurate
    # wrong-owner findings (figure_page_mismatch entries in
    # data/export_gate.jsonl) instead of re-deriving page consistency from
    # row.source_pages -- those pages legitimately include answer-key windows
    # pages away from the stem page (q7's A-pass window made the naive check
    # fire on a correct claim; false positive proven on OPH ch. 28).
    rows_by_id = {r.get("id"): r for r in rows}
    for gf in load_jsonl(data_dir / "export_gate.jsonl"):
        if gf.get("kind") != "figure_page_mismatch":
            continue
        cid = gf.get("chapter_id")
        row = rows_by_id.get(f"{cid}-{int(gf.get('q_no')):03d}") \
            if gf.get("q_no") is not None else None
        flags_by.setdefault(cid, []).append(
            flag(cid, "image_owner_gate_miss",
                 f"export gate: {gf.get('detail')}",
                 gf.get("q_no"),
                 severity=HIGH,
                 source="export_gate"))
    # upgrade declared_figure_missing LOW -> HIGH when an unresolved or
    # unmatched image sits on that row's source pages (a REAL unclaimed
    # figure, not model over-declaration)
    figless = {}
    for cid, crows in flags_by.items():
        for fl in crows:
            if fl.get("kind") == "declared_figure_missing":
                figless[(cid, fl.get("q_no"))] = fl
    if figless:
        hot_pages = {}
        for uj in load_jsonl(data_dir / "unresolved_images.jsonl") + \
                  load_jsonl(data_dir / "unmatched_images.jsonl"):
            if uj.get("page"):
                hot_pages.setdefault(uj.get("chapter_id"), set()).add(
                    int(uj["page"]))
        rows_by_cq = {}
        for r in rows:
            if q_no_of(r) is not None:
                rows_by_cq.setdefault((r.get("chapter_id"), q_no_of(r)),
                                      []).append(r)
        for (cid, qn), fl_rows in rows_by_cq.items():
            fl = figless.get((cid, qn))
            if fl is None:
                continue
            ch_hot = hot_pages.get(cid, set())
            for r in fl_rows:
                sp = set(r.get("source_pages") or [])
                if sp and any(min(sp) - 1 <= hp <= max(sp) + 2
                              for hp in ch_hot):
                    fl["severity"] = HIGH
                    fl["detail"] += (" | UPGRADED: unresolved/unmatched image "
                                     "exists on this question's source pages "
                                     "-- real printed figure left ownerless")
                    break

    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chapters": len(by_chapter),
        "questions": len(rows),
        "flagged_chapters": sum(1 for v in flags_by.values() if v),
        "flags_total": sum(len(v) for v in flags_by.values()),
        "flags_by_kind": dict(Counter(f["kind"] for v in flags_by.values() for f in v)),
        "answer_key_only_suppressed": suppressions,
    }
    return flags_by, summary


# ============================================================
# STAGE 2 -- full-chapter audit: model as WITNESS, code decides
# ============================================================

WITNESS_PROMPT = """You are auditing page images of one MCQ chapter. You are a WITNESS, not a judge:
do NOT say whether extraction was correct, do NOT assess quality -- just enumerate
what is physically printed on these pages. Code compares your list against a database.

Return ONE JSON object (no commentary):
{
 "q_nos": [<every question number printed anywhere on these pages, as ints>],
 "components": {"<qn>": {"options": true/false, "answer": true/false,
                          "explanation": true/false, "table": true/false, "figure": true/false}},
 "answer_key_q_nos": [<q_nos whose ONLY answer source is an answer-key table>]
}
"answer" = a correct-option marking exists somewhere for that question (inline or key table).
If a question truly has no explanation printed, say "explanation": false -- do not assume one.
Never invent numbers. If a page shows a fragment with no visible number, ignore it here.
"""


def _model_json(resp):
    text = resp.text.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def chapter_audit(model, page_files, image_opener, gen_kwargs=None):
    """One full-chapter witness call. page_files/image_opener are injected
    (PIL.Image.open in prod; identities in tests). gen_kwargs carries the
    pipeline's safety_settings in prod (clinical content)."""
    parts = [WITNESS_PROMPT] + [image_opener(p) for p in page_files]
    resp = model.generate_content(parts, **(gen_kwargs or {}))
    if not getattr(resp, "candidates", None):
        raise RuntimeError("audit: empty/blocked response")
    return _model_json(resp)


def diff_witness(witness, rows, suppress_components=()):
    """CODE decides: diff the witness enumeration against extracted rows."""
    cid = rows[0].get("chapter_id") if rows else None
    flags = []
    json_qns = {q_no_of(r) for r in rows if q_no_of(r) is not None}
    comp = witness.get("components") or {}
    w_qns = {int(n) for n in (witness.get("q_nos") or [])}
    w_qns |= {int(k) for k in comp if _as_int(k) is not None}
    row_by_qn = {q_no_of(r): r for r in rows}
    suppress = set(suppress_components)  # e.g. {"explanation"} for RC-4 chapters

    for n in sorted(w_qns - json_qns):
        flags.append(flag(cid, "audit_missing_question",
                          f"witness saw question {n} on the pages; JSON has no record",
                          n, source="audit"))
    for n in sorted(json_qns - w_qns):
        flags.append(flag(cid, "audit_ghost_question",
                          f"JSON has question {n} but witness never saw it on the pages",
                          n, source="audit"))
    for k, c in comp.items():
        n = _as_int(k)
        row = row_by_qn.get(n)
        if row is None:
            continue
        if c.get("explanation") and "explanation" not in suppress \
                and not ((row.get("solution") or {}).get("text") or "").strip():
            flags.append(flag(cid, "audit_component_missing",
                              f"witness saw an explanation for q{n}; JSON solution empty", n, source="audit"))
        if c.get("answer") and not (row.get("correct_options") or []):
            flags.append(flag(cid, "audit_component_missing",
                              f"witness saw an answer marking for q{n}; JSON has none", n, source="audit"))
        if c.get("options") and len(row.get("options") or []) < 4:
            flags.append(flag(cid, "audit_component_missing",
                              f"witness saw 4 options for q{n}; JSON has {len(row.get('options') or [])}",
                              n, source="audit"))
        if c.get("figure") and not (row.get("question") or {}).get("images") \
                and not (row.get("solution") or {}).get("images"):
            flags.append(flag(cid, "audit_component_missing",
                              f"witness saw a figure for q{n}; JSON has no image attached", n, source="audit"))
    return flags


# ============================================================
# STAGE 3 -- neighborhood verification (small anchored asks)
# ============================================================

def verify_prompt_for(fl):
    """One binary-ish, page-anchored ask per flag. Never open recall."""
    qn, kind = fl.get("q_no"), fl["kind"]
    base = ("Look ONLY at the attached page images (the neighborhood of one flagged "
            "defect). Answer with ONE JSON object, no commentary. If the requested "
            "content is genuinely not printed, return {\"found\": false}. Never guess; "
            "verbatim text only.\n\n")
    if kind in ("missing_solution", "audit_component_missing", "truncated_solution"):
        return base + (f'Is explanation/solution text for question {qn} printed on these pages? '
                       f'If yes: {{"found": true, "q_no": {qn}, "solution_text": "<FULL verbatim text>", '
                       f'"anchor": "<exact first 10 words>"}}')
    if kind in ("missing_answer", "answer_mismatch"):
        return base + (f'Is a correct-answer marking for question {qn} printed anywhere '
                       f'(inline or in an answer-key table)? If yes: {{"found": true, "q_no": {qn}, '
                       f'"correct_option": "A"|"B"|"C"|"D", "anchor": "<the key row or marking>"}}')
    if kind == "bad_options":
        return base + (f'Return ALL options printed for question {qn}: {{"found": true, "q_no": {qn}, '
                       f'"options": {{"A": "...","B": "...","C": "...","D": "..."}}}}')
    if kind in ("numbering_gap", "audit_missing_question"):
        return base + (f'Is question number {qn} printed on these pages at all? If yes, return it FULLY: '
                       f'{{"found": true, "q_no": {qn}, "question_text": "...", '
                       f'"options": {{"A":"...","B":"...","C":"...","D":"..."}}, '
                       f'"correct_option": "A"|"B"|"C"|"D"|null, "solution_text": "..."|null}}')
    if kind == "orphan_unresolved":
        return base + ('A text fragment was extracted here with no question number (shown below). '
                       'Which PRINTED question number does it belong to, and what role does it play? '
                       '{"owner_q_no": <int>|null, "role": "solution"|"options"|"question"|"table"|null}\n'
                       f'FRAGMENT: {fl["detail"][:600]}')
    if kind == "image_unclaimed":
        return base + ('These pages contain a figure/illustration. Which PRINTED question number does '
                       'it belong to, and is it part of the question stem or the explanation? '
                       '{"q_no": <int>|null, "role": "question"|"solution"|null}')
    return None  # duplicate/status kinds need no model


def pages_from_window(window_files, fallback):
    """True PDF page numbers come from pdftoppm output filenames
    (page-012.jpg = true page 12 -- established pipeline convention).
    Falls back to the flag's own pages when filenames aren't parseable."""
    pages = []
    for p in window_files or []:
        try:
            pages.append(int(Path(p).stem.split("-")[-1]))
        except (TypeError, ValueError, AttributeError):
            pass
    return sorted(set(pages)) or list(fallback or [])


def verify_flag(model, fl, window_files, image_opener, gen_kwargs=None):
    """One model call over the flag's own page window -> candidate or None.
    Candidate pages come from the WINDOW ACTUALLY SHOWN (not the flag), so
    the evidence stage can locate the exact page text even when the
    original flag carried no page provenance."""
    prompt = verify_prompt_for(fl)
    if prompt is None or not window_files:
        return None
    parts = [prompt] + [image_opener(p) for p in window_files]
    try:
        resp = model.generate_content(parts, **(gen_kwargs or {}))
        if not getattr(resp, "candidates", None):
            return None
        data = _model_json(resp)
    except Exception as e:
        print(f"  [AUDIT] verify call failed ({fl['kind']} q{fl.get('q_no')}): {e}")
        return None
    return {"flag": fl, "data": data, "pages": pages_from_window(window_files, fl.get("pages"))}


# ============================================================
# STAGE 4 -- evidence-gated patch adjudication + application
# ============================================================

def candidate_value(candidate):
    """The payload text that must be backed by page evidence."""
    d = candidate["data"] or {}
    for k in ("solution_text", "question_text"):
        if d.get(k):
            return d[k]
    if d.get("options"):
        return " ".join(str(v) for v in d["options"].values() if v)
    if d.get("correct_option"):
        return str(d.get("anchor") or d["correct_option"])
    return ""


def adjudicate(candidate, page_text_lookup):
    """candidate -> (verdict, score). auto_apply only with hard textual
    evidence; no usable text layer -> human queue, never auto."""
    if not candidate or not isinstance(candidate.get("data"), dict):
        return "reject", 0.0
    d, kind = candidate["data"], candidate["flag"]["kind"]
    if kind == "duplicate_text":
        sim = candidate["flag"].get("similarity", 0.0)
        return ("auto_apply", sim) if sim >= DELETE_AUTO_MIN else ("human_queue", sim)
    if kind == "truncated_solution":
        # truncated != empty: filling needs append/replace semantics, which
        # fill_only merge deliberately lacks. Human decides the splice.
        return "human_queue", 0.0
    if d.get("found") is False:
        return "reject", 0.0
    value = candidate_value(candidate)
    if not value:
        return "reject", 0.0
    pages = candidate.get("pages") or []
    text = " ".join(page_text_lookup(p) for p in pages) if pages else ""
    if not text.strip():
        return "human_queue", 0.0
    score = token_overlap(value, text)
    if score >= AUTO_APPLY_MIN:
        return "auto_apply", score
    if score >= HUMAN_QUEUE_MIN:
        return "human_queue", score
    return "reject", score


def candidate_to_merge_item(candidate):
    """Approved candidate -> Gemini-item-shaped dict for fill_only merge."""
    kind, d = candidate["flag"]["kind"], candidate["data"]
    qn = _as_int(d.get("q_no")) or _as_int(d.get("owner_q_no")) or candidate["flag"].get("q_no")
    if qn is None:
        return None
    # "truncated_solution" is deliberately absent: it is human-queue-only
    # (append/replace semantics), never an auto merge item.
    if kind in ("missing_solution", "audit_component_missing") and d.get("solution_text"):
        return {"q_no": qn, "solution_text": d["solution_text"]}
    if kind in ("missing_answer", "answer_mismatch") and d.get("correct_option"):
        return {"q_no": qn, "correct_option": d["correct_option"]}
    if kind == "bad_options" and d.get("options"):
        return {"q_no": qn, "options": d["options"]}
    if kind in ("numbering_gap", "audit_missing_question") and d.get("question_text"):
        return {"q_no": qn, "question_text": d["question_text"], "options": d.get("options"),
                "correct_option": d.get("correct_option"), "solution_text": d.get("solution_text")}
    if kind == "orphan_unresolved" and d.get("owner_q_no"):
        key = {"solution": "solution_text", "question": "question_text"}.get(d.get("role"))
        if key:
            frag = candidate["flag"]["detail"][:2000]
            return {"q_no": qn, key: frag}
    return None


def check_preconditions(rows, items, deletes, inserts):
    """All-or-nothing per chapter: if ANY patch violates its precondition,
    nothing is written. Fills must target empty fields; inserts must be
    new q_nos; deletes must name an existing id."""
    by_qn = {q_no_of(r): r for r in rows}
    ids = [r.get("id") for r in rows]
    for qn in inserts:
        if qn in by_qn:
            return f"insert q{qn}: q_no already present"
    for d in deletes:
        if d not in ids:
            return f"delete {d}: id not present in chapter"
    for it in items:
        qn = _as_int(it.get("q_no"))
        row = by_qn.get(qn)
        if row is None:
            continue  # insert-shaped item handled above
        if it.get("solution_text") and ((row.get("solution") or {}).get("text") or "").strip():
            return f"q{qn}: solution already filled (fill-only violation)"
        if it.get("correct_option") and (row.get("correct_options") or []):
            return f"q{qn}: answer already present (fill-only violation)"
    return None


def apply_chapter_patches(chapter_id, rows, approved, qp, dry_run=True):
    """approved: [(candidate, score)].
    Returns (result_dict, new_rows | None) -- None means preconditions
    failed and the chapter was left UNTOUCHED (transactional).
    Production machinery is injected as qp (= qbank_pipeline module)."""
    items, deletes, inserts, applied = [], [], [], []
    existing_qns = {q_no_of(r) for r in rows}

    for cand, score in approved:
        kind = cand["flag"]["kind"]
        if kind == "duplicate_text":
            parts = cand["flag"]["detail"].split(" ~ ")
            later_id = parts[1].split(" ")[0].strip() if len(parts) > 1 else None
            if not later_id:
                return {"chapter_id": chapter_id, "applied": [],
                        "aborted": f"duplicate_text flag unparsable: {cand['flag']['detail']}"}, None
            deletes.append(later_id)
            applied.append({"op": "delete", "id": later_id, "score": score})
            continue
        item = candidate_to_merge_item(cand)
        if item is None:
            continue  # unactionable candidate (should have been queued earlier)
        if _as_int(item["q_no"]) not in existing_qns:
            inserts.append(_as_int(item["q_no"]))
        items.append(item)
        applied.append({"op": "merge_item", "q_no": item["q_no"],
                        "fields": sorted(k for k in item if k != "q_no"),
                        "score": score, "pages": cand.get("pages")})

    abort = check_preconditions(rows, items, deletes, inserts)
    if abort:
        return {"chapter_id": chapter_id, "applied": [], "aborted": abort}, None

    records, owned = {}, {}
    subject = chapter_id.split("-", 1)[0]
    chapter_no = int(chapter_id.split("-", 1)[1])
    for r in rows:
        rec, own = qp.final_q_to_record(r)
        records[rec["q_no"]], owned[rec["q_no"]] = rec, own
    stats = {}
    if items:
        records, _skipped = qp.merge_question_records(records, items, stats, fill_only=True)

    kept_qns = set()
    new_rows = []
    delete_set = set(deletes)
    deleted_qns = {q_no_of(r) for r in rows if r.get("id") in delete_set}
    for r in rows:
        if r.get("id") in delete_set:
            continue
        qn = q_no_of(r)
        kept_qns.add(qn)
        new_rows.append(qp.build_final_question(subject, chapter_id, chapter_no, qn,
                                                records[qn], owned.get(qn, {"question": [], "solution": []})))
    # only true INSERTS are appended as brand-new rows -- never resurrect a
    # record whose row was just deleted.
    for qn in sorted(set(inserts) - kept_qns - deleted_qns):
        new_rows.append(qp.build_final_question(subject, chapter_id, chapter_no, qn,
                                                records[qn], {"question": [], "solution": []}))
    return {"chapter_id": chapter_id, "applied": applied, "aborted": None,
            "dry_run": dry_run, "stats": stats}, new_rows


def rewrite_questions(path, all_rows):
    """Unique-id rewrite (same guarantee as recovery mode) + atomic replace."""
    path = Path(path)
    seen, out = set(), []
    for r in all_rows:
        rid = r.get("id")
        if rid and rid not in seen:
            seen.add(rid)
            out.append(r)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return len(out)


# ============================================================
# STAGE 5 -- status state machine + orchestration
# ============================================================

def load_audit_state(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else {}


def save_audit_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def run_hybrid(output_root=OUTPUT_ROOT, audit=False, model=None, image_opener=None,
               page_text_provider=None, window_provider=None, chapter_pages_provider=None,
               max_attempts=MAX_AUDIT_ATTEMPTS, audit_budget=150, dry_run=False,
               explicit_source_gap=(), gen_kwargs=None):
    """Full hybrid pipeline. All model/page access is injectable:
      image_opener(p)              -> image object (tests: identity)
      page_text_provider(cid)      -> fn(true_pdf_page) -> text layer (pdftotext in prod)
      window_provider(flag)        -> rasterized page files for one flag's neighborhood
      chapter_pages_provider(cid)  -> full chapter page files for the witness call
    """
    output_root = Path(output_root)
    data_dir = output_root / "data"
    report_path = data_dir / "validation_report.json"
    audit_state_path = data_dir / "audit_state.json"
    applied_log = data_dir / "patches_applied.jsonl"
    human_log = data_dir / "human_review_queue.jsonl"
    assets_q = output_root / "assets" / "questions"

    flags_by, summary = validate_deterministic(output_root, explicit_source_gap)
    report = {"mode": "audit" if audit else "report-only", "summary": summary,
              "chapters": {cid: flags for cid, flags in flags_by.items() if flags},
              "metrics": {}}
    data_dir.mkdir(parents=True, exist_ok=True)
    if not audit:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        return report

    rows = load_jsonl(data_dir / "questions.jsonl")
    by_chapter = {}
    for r in rows:
        by_chapter.setdefault(r.get("chapter_id"), []).append(r)
    astate = load_audit_state(audit_state_path)
    spent = applied_n = human_n = rejected_n = 0

    for cid, flags in sorted(flags_by.items()):
        if not flags:
            continue
        entry = astate.get(cid, {})
        if entry.get("status") in ("verified_clean", "source_gap"):
            continue
        if entry.get("verify_attempts", 0) >= max_attempts:
            entry["status"] = "source_gap"
            astate[cid] = entry
            print(f"  [AUDIT] {cid}: attempt cap ({max_attempts}) reached -> source_gap, suppressed")
            continue
        if spent >= audit_budget:
            print(f"  [AUDIT] audit budget ({audit_budget}) reached -- remaining chapters stay flagged")
            break

        spent_before = spent
        chapter_flags = list(flags)
        crows = by_chapter.get(cid, [])
        rc4 = is_answer_key_only(crows) or cid in set(explicit_source_gap)

        # stage 2: full-chapter witness audit (the stage the user wants kept)
        if chapter_pages_provider and model is not None:
            page_files = chapter_pages_provider(cid)
            if page_files:
                try:
                    witness = chapter_audit(model, page_files, image_opener, gen_kwargs)
                    spent += 1
                    chapter_flags += diff_witness(witness, crows,
                                                  suppress_components={"explanation"} if rc4 else set())
                except Exception as e:
                    print(f"  [AUDIT] witness call failed for {cid}: {e}")

        # stage 3: neighborhood verification per flag
        candidates = []
        if window_provider and model is not None:
            for fl in chapter_flags:
                if spent >= audit_budget:
                    break
                cand = verify_flag(model, fl, window_provider(fl), image_opener, gen_kwargs)
                if cand:
                    spent += 1
                    candidates.append(cand)

        # stage 4: adjudicate (per-chapter text layer), then transactional apply
        ptl = page_text_provider(cid) if page_text_provider else (lambda p: "")
        approved, queued = [], []
        for cand in candidates:
            verdict, score = adjudicate(cand, ptl)
            if verdict == "auto_apply":
                approved.append((cand, score))
            elif verdict == "human_queue":
                queued.append({"flag": cand["flag"], "candidate": cand["data"], "score": score})
            else:
                rejected_n += 1
        auto_dups = [({"flag": f, "data": {}, "pages": []}, f.get("similarity", 0.0))
                     for f in chapter_flags
                     if f["kind"] == "duplicate_text" and f.get("similarity", 0.0) >= DELETE_AUTO_MIN]
        applied_this = []
        if approved or auto_dups:
            import qbank_pipeline as qp  # lazy: report-only mode needs no genai/pypdf
            result, new_rows = apply_chapter_patches(cid, crows, approved + auto_dups, qp, dry_run=dry_run)
            if result.get("aborted"):
                print(f"  [AUDIT] {cid}: patch set aborted ({result['aborted']}) -- chapter left untouched")
            else:
                by_chapter[cid] = new_rows
                applied_this = result["applied"]
                applied_n += len(applied_this)
        human_n += len(queued)
        if not dry_run:
            for hq in queued:
                append_jsonl(human_log, hq)
            for ap in applied_this:
                append_jsonl(applied_log, {"chapter_id": cid, **ap})

        # stage 5: final re-verification on patched rows + status transition
        remaining = [f for r in by_chapter.get(cid, []) for f in check_row(r, assets_q)]
        remaining += check_chapter(cid, by_chapter.get(cid, []))
        entry["verify_attempts"] = entry.get("verify_attempts", 0) + (1 if spent > spent_before else 0)
        entry["status"] = "verified_clean" if not remaining else "flagged"
        if queued:
            entry["status"] = "human_queue"
        entry["last_flags"] = len(remaining)
        entry["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        astate[cid] = entry
        print(f"  [AUDIT] {cid}: flags={len(chapter_flags)} applied={len(applied_this)} "
              f"human={len(queued)} rejected_remaining={len(remaining)} status={entry['status']}")

    if not dry_run:
        all_rows = [r for cid in sorted(by_chapter) for r in by_chapter[cid]]
        rewrite_questions(data_dir / "questions.jsonl", all_rows)
        save_audit_state(audit_state_path, astate)
    report["metrics"] = {"calls_spent": spent, "patches_applied": applied_n,
                         "human_queued": human_n, "rejected": rejected_n,
                         "fix_per_call": round(applied_n / spent, 3) if spent else None}
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


# ============================================================
# prod glue: pdftotext / pdftoppm providers (only used with --audit)
# ============================================================

def make_page_text_lookup(pdf_path):
    def lookup(true_page):
        if not pdf_path:
            return ""
        out = subprocess.run(["pdftotext", "-f", str(true_page), "-l", str(true_page),
                              "-layout", str(pdf_path), "-"], capture_output=True, text=True)
        return out.stdout or ""
    return lookup


def make_window_provider(pdf_path, dpi=150):
    def provider(fl):
        pages = fl.get("pages") or []
        if not pdf_path or not pages:
            return []
        lo, hi = max(1, min(pages) - 1), max(pages) + 1
        out_dir = Path(f"/tmp/audit_{abs(hash((str(pdf_path), lo, hi))) % 10**8}")
        out_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["pdftoppm", "-jpeg", "-r", str(dpi),
                        "-f", str(lo), "-l", str(hi), str(pdf_path), str(out_dir / "page")])
        return sorted(out_dir.glob("page-*.jpg"))
    return provider


def main():
    args = sys.argv[1:]
    output_root = Path(os.environ.get("OUTPUT_DIR", "./qbank_output"))
    if "--output-root" in args:
        output_root = Path(args[args.index("--output-root") + 1])

    if "--audit" not in args:  # default = report-only (zero tokens)
        report = run_hybrid(output_root, audit=False)
        print(json.dumps(report["summary"], indent=2))
        print(f"\nreport -> {output_root / 'data' / 'validation_report.json'}")
        return

    # --audit: build real providers + a real model.
    import google.generativeai as genai
    from PIL import Image as PILImage
    import qbank_pipeline as qp

    import gemini_keys
    if not gemini_keys.discover_keys():
        print("No Gemini API key set (GEMINI_API_KEYS / GEMINI_API_KEY_1..N / "
              "GEMINI_API_KEY) -- cannot run model stages")
        sys.exit(1)
    _audit_state = qp.load_state()
    gemini_keys.init(_audit_state, qp.MAX_CALLS_PER_DAY)
    model = gemini_keys.track(genai.GenerativeModel(qp.GEMINI_MODEL))
    budget = int(args[args.index("--audit-budget") + 1]) if "--audit-budget" in args else 150
    dry = "--dry-run" in args

    def chapter_pdf(cid):
        cfg = next((c for c in qp.PDFS if c["subject"] == cid.split("-", 1)[0]), None)
        return cfg["path"] if cfg else None

    def chapter_pages_provider(cid):
        pdf = chapter_pdf(cid)
        if not pdf:
            return []
        cfg = next(c for c in qp.PDFS if c["subject"] == cid.split("-", 1)[0])
        toc = qp.extract_toc_chapters(pdf)
        total = len(qp.PdfReader(pdf).pages)
        ranges = qp.compute_page_ranges(toc, cfg["page_offset"], total)
        rng = next((c for c in ranges if c["chapter_no"] == int(cid.split("-", 1)[1])), None)
        if not rng:
            return []
        out_dir = Path(f"/tmp/audit_full_{cid}")
        out_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["pdftoppm", "-jpeg", "-r", "150", "-f", str(rng["file_start"]),
                        "-l", str(rng["file_end"]), pdf, str(out_dir / "page")])
        return sorted(out_dir.glob("page-*.jpg"))

    def window_provider(fl):
        return make_window_provider(chapter_pdf(fl["chapter_id"]))(fl)

    def page_text_provider(cid):
        return make_page_text_lookup(chapter_pdf(cid))

    report = run_hybrid(output_root, audit=True, model=model, image_opener=PILImage.open,
                        page_text_provider=page_text_provider, window_provider=window_provider,
                        chapter_pages_provider=chapter_pages_provider,
                        audit_budget=budget, dry_run=dry,
                        gen_kwargs={"safety_settings": qp.SAFETY_SETTINGS,
                                    "request_options": {"retry": None}})
    print(json.dumps(report["metrics"], indent=2))
    print(f"\nreport -> {output_root / 'data' / 'validation_report.json'}")


if __name__ == "__main__":
    main()
