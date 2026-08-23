#!/usr/bin/env python3
"""Visual header index — pixels first, pdftotext is never authoritative.

Each chapter page is rendered and OCR'd. We keep only lines that look like
the printed furniture:

  Question N:
  Answer Key / Answer-key
  Detailed Explanations
  Solution to Question N:

A record is {page, y, type, n, bbox, conf, snippet, method}.
y is PDF-ish: larger = higher on the page (from render + scale).

This module does NOT call Gemini. Header-band Gemini is optional later;
OCR of the rendered page IS printed pixels.
"""
from __future__ import annotations

import re
import shutil

# types
T_QUESTION = "question"
T_ANSWER_KEY = "answer_key"
T_DETAILED = "detailed_explanations"
T_SOLUTION = "solution"

_Q = re.compile(r"(?i)\bquestion\s+(\d{1,3})\s*[:.]")
_S = re.compile(r"(?i)solution\s+to\s+question\s+(\d{1,3})")
_AK = re.compile(r"(?i)\banswer[\s-]*key\b")
_DE = re.compile(r"(?i)\bdetailed\s+explanations\b")
_KEYROW = re.compile(r"(?im)^\s*(\d{1,3})\s*[.\)\:\-]?\s*\(?([A-Ea-e])\)?\s*$")
_KEYROW_LOOSE = re.compile(
    r"(?im)(?:^|\s)(\d{1,3})\s*[.\)\:\-]?\s*\(?([A-Ea-e])\)?(?:\s|$)")


def _ocr_lines(png, scale):
    """[(y_pdf, x_pdf, text, conf)] from tesseract on a rendered page."""
    if png is None or not shutil.which("tesseract"):
        return []
    try:
        import pytesseract
        data = pytesseract.image_to_data(
            png, config="--psm 6", output_type=pytesseract.Output.DICT)
    except Exception:
        return []
    words = []
    for i, txt in enumerate(data.get("text") or []):
        t = (txt or "").strip()
        if not t:
            continue
        try:
            left = int(data["left"][i])
            top = int(data["top"][i])
            hgt = int(data["height"][i])
            conf = float(data["conf"][i])
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        if conf < 25:
            continue
        try:
            lid = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        except (KeyError, IndexError):
            lid = (i,)
        yc = top + hgt / 2.0
        words.append((lid, left, yc, t, conf))
    by = {}
    for lid, x, yc, t, conf in words:
        by.setdefault(lid, []).append((x, yc, t, conf))
    out = []
    img_h = png.height
    for lid, grp in by.items():
        y_mid = sum(g[1] for g in grp) / len(grp)
        conf = sum(g[3] for g in grp) / len(grp)
        text = " ".join(g[2] for g in sorted(grp, key=lambda z: z[0]))
        x0 = min(g[0] for g in grp)
        y_pdf = (img_h - y_mid) / scale if scale else y_mid
        x_pdf = x0 / scale if scale else x0
        out.append((y_pdf, x_pdf, text, conf))
    out.sort(key=lambda r: -r[0])
    return out


def classify_line(text):
    """-> (type, n or None) or None."""
    t = text or ""
    m = _S.search(t)
    if m:
        return T_SOLUTION, int(m.group(1))
    if _AK.search(t) and "question" not in t.lower()[:20]:
        return T_ANSWER_KEY, None
    if _DE.search(t):
        return T_DETAILED, None
    m = _Q.search(t)
    if m:
        return T_QUESTION, int(m.group(1))
    m = re.search(r"(?i)^\s*question\s+(\d{1,3})\s*$", t)
    if m:
        return T_QUESTION, int(m.group(1))
    return None


def scan_page(pdf_path, page, dpi=150):
    """Visual headers on one rendered page. Empty if render/OCR unavailable."""
    try:
        import qbank_pipeline as qp
        png, scale, _ph = qp.render_page_png(pdf_path, page, dpi=dpi)
    except Exception:
        return []
    if not png:
        return []
    recs = []
    for y, x, text, conf in _ocr_lines(png, scale or 1.0):
        hit = classify_line(text)
        if not hit:
            continue
        typ, n = hit
        recs.append({
            "page": int(page), "y": float(y), "type": typ, "n": n,
            "bbox": [float(x), float(y), float(x) + 200, float(y) + 12],
            "conf": float(conf), "snippet": text[:160],
            "method": "render_ocr",
        })
    return recs


def scan_chapter(pdf_path, first, last, dpi=150):
    recs = []
    for p in range(int(first), int(last) + 1):
        recs.extend(scan_page(pdf_path, p, dpi=dpi))
    recs.sort(key=lambda r: (r["page"], -r["y"]))
    return recs


def intervals(recs, typ):
    """Reading-order intervals for one header type.

    Each interval is {n, start_page, start_y, end_page, end_y, strips}.
    strips = [{page, y_hi, y_lo}] from this header down to the next header
    (or page bottom). y is PDF-ish (larger = higher). Mid-page split is y.
    Cross-page bodies become two strips (join later).
    """
    ordered = sorted(recs or [], key=lambda r: (r["page"], -r["y"]))
    out = []
    for i, r in enumerate(ordered):
        if r.get("type") != typ or not r.get("n"):
            continue
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        strips = []
        p0, y0 = r["page"], float(r["y"])
        if nxt is None:
            strips.append({"page": p0, "y_hi": y0 + 14, "y_lo": 0.0})
            end_p, end_y = p0, 0.0
        elif nxt["page"] == p0:
            strips.append({"page": p0, "y_hi": y0 + 14, "y_lo": float(nxt["y"])})
            end_p, end_y = p0, float(nxt["y"])
        else:
            strips.append({"page": p0, "y_hi": y0 + 14, "y_lo": 0.0})
            for p in range(p0 + 1, nxt["page"]):
                strips.append({"page": p, "y_hi": 9999.0, "y_lo": 0.0})
            strips.append({"page": nxt["page"], "y_hi": 9999.0,
                           "y_lo": float(nxt["y"])})
            end_p, end_y = nxt["page"], float(nxt["y"])
        out.append({
            "n": int(r["n"]), "start_page": p0, "start_y": y0,
            "end_page": end_p, "end_y": end_y, "strips": strips,
            "header": r,
        })
    return out


def index_sets(recs):
    """Convenience views used by zones + lock ledger."""
    q_pages, s_pages, key_pages = set(), set(), set()
    q_ns, s_ns = set(), set()
    q_hdrs, s_hdrs = {}, {}
    for r in recs:
        p, typ, n = r["page"], r["type"], r.get("n")
        if typ == T_QUESTION and n:
            q_pages.add(p)
            q_ns.add(n)
            q_hdrs.setdefault(p, set()).add(n)
        elif typ == T_SOLUTION and n:
            s_pages.add(p)
            s_ns.add(n)
            s_hdrs.setdefault(p, set()).add(n)
        elif typ == T_DETAILED:
            s_pages.add(p)
        elif typ == T_ANSWER_KEY:
            key_pages.add(p)
    return {
        "q_pages": q_pages, "s_pages": s_pages, "key_pages": key_pages,
        "q_ns": q_ns, "s_ns": s_ns, "q_hdrs": q_hdrs, "s_hdrs": s_hdrs,
        "recs": recs,
    }


def owner_of_point(recs, page, y):
    """Closest heading ABOVE (y) on this page, else open interval from prev page.

    Returns (kind, n) or None. kind is 'question' or 'solution'.
    Mid-page split is y. Never Gemini.
    """
    if not recs:
        return None
    on = [r for r in recs if r["page"] == page
          and r.get("type") in (T_QUESTION, T_SOLUTION) and r.get("n")]
    above = [r for r in on if float(r["y"]) > float(y)]
    if above:
        hit = min(above, key=lambda r: float(r["y"]) - float(y))
        kind = "question" if hit["type"] == T_QUESTION else "solution"
        return kind, int(hit["n"])
    # carry: last numbered header on an earlier page
    earlier = [r for r in recs if r["page"] < page
               and r.get("type") in (T_QUESTION, T_SOLUTION) and r.get("n")]
    if not earlier:
        return None
    hit = max(earlier, key=lambda r: (r["page"], -float(r["y"])))
    if page - hit["page"] > 1:
        return None
    kind = "question" if hit["type"] == T_QUESTION else "solution"
    return kind, int(hit["n"])


def block_headers_for_page(recs, page):
    """[(kind, n, y)] visual headers on one page, top-first. For union inject."""
    out = []
    for r in recs or []:
        if r.get("page") != page or not r.get("n"):
            continue
        if r.get("type") == T_QUESTION:
            out.append(("question", int(r["n"]), float(r["y"])))
        elif r.get("type") == T_SOLUTION:
            out.append(("solution", int(r["n"]), float(r["y"])))
    out.sort(key=lambda t: -t[2])
    return out


def text_layer_health(text):
    """CLEAN | DEGRADED | GARBLED | EMPTY. Never treat garbled as printed."""
    t = (text or "").strip()
    if not t:
        return "EMPTY"
    letters = sum(1 for c in t if c.isalpha())
    weird = sum(1 for c in t if ord(c) > 127 or c in "■□�")
    n = max(len(t), 1)
    if weird / n > 0.08:
        return "GARBLED"
    if letters < 20:
        return "EMPTY" if letters < 5 else "DEGRADED"
    if letters / n < 0.35:
        return "GARBLED"
    if weird / n > 0.02 or letters / n < 0.50:
        return "DEGRADED"
    return "CLEAN"


def parse_key_rows_from_ocr_text(text):
    """{n: letter} from OCR/text of a KEY TABLE crop only."""
    out = {}
    for m in _KEYROW.finditer(text or ""):
        out[int(m.group(1))] = m.group(2).upper()
    if len(out) < 6:
        for m in _KEYROW_LOOSE.finditer(text or ""):
            n = int(m.group(1))
            if n not in out:
                out[n] = m.group(2).upper()
    return out


def key_region_pages(recs):
    """File pages from Answer Key header through Detailed Explanations / Sol 1.

    Mid-page Y split is implied: we include both the key-header page AND the
    explanations/first-solution page (Ch1 p12–13, Ch2 p37–38).
    """
    recs = list(recs or [])
    keys = [r for r in recs if r.get("type") == T_ANSWER_KEY]
    if not keys:
        return []
    start = min(keys, key=lambda r: (r["page"], -float(r["y"])))
    stops = [r for r in recs
             if r.get("type") in (T_DETAILED, T_SOLUTION)
             and (r["page"] > start["page"]
                  or (r["page"] == start["page"]
                      and float(r["y"]) < float(start["y"])))]
    if not stops:
        return [int(start["page"])]
    end = min(stops, key=lambda r: (r["page"], -float(r["y"])))
    return list(range(int(start["page"]), int(end["page"]) + 1))


def inject_gap_headers(recs, typ):
    """If headers skip n (6 then 8), inject n between them so crop extract runs."""
    recs = list(recs or [])
    owned = {r["n"] for r in recs if r.get("type") == typ and r.get("n")}
    for n in sequence_gaps(owned):
        prev = next((r for r in recs if r.get("type") == typ and r.get("n") == n - 1), None)
        nxt = next((r for r in recs if r.get("type") == typ and r.get("n") == n + 1), None)
        if not prev:
            continue
        y = max(0.0, float(prev["y"]) - 18.0)
        recs = backfill_header(recs, typ, n, prev["page"], y=y,
                               snippet=f"gap inject {n}", method="gap_inject")
    return recs


def sequence_gaps(ns):
    """Missing integers in 1..max(ns)."""
    ns = {int(n) for n in (ns or []) if n}
    if not ns:
        return []
    return [n for n in range(1, max(ns) + 1) if n not in ns]


def backfill_header(recs, typ, n, page, y=0.0, snippet="", method="gap_probe"):
    recs = list(recs or [])
    if any(r.get("type") == typ and r.get("n") == int(n) for r in recs):
        return recs
    recs.append({
        "page": int(page), "y": float(y), "type": typ, "n": int(n),
        "bbox": [0.0, float(y), 200.0, float(y) + 12],
        "conf": 40.0, "snippet": (snippet or "")[:160],
        "method": method,
    })
    recs.sort(key=lambda r: (r["page"], -r["y"]))
    return recs


def ocr_key_table(pdf_path, pages, dpi=170):
    """OCR whole key pages (narrow: only those pages). Pixels, not pdftotext."""
    blob = []
    for p in pages:
        try:
            import qbank_pipeline as qp
            png, scale, _ = qp.render_page_png(pdf_path, p, dpi=dpi)
        except Exception:
            png = None
        if not png:
            continue
        lines = _ocr_lines(png, scale or 1.0)
        blob.append("\n".join(t for _y, _x, t, _c in lines))
    return parse_key_rows_from_ocr_text("\n".join(blob))
