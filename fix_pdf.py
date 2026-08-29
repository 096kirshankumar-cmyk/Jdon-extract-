#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_pdf.py
==========
Pre-process a garbled medical MCQ PDF (MARROW ED8 series) before the extraction
pipeline runs.

Two problems are fixed, strictly in this order:

  STEP 1  Remove the full-page "Sold by @itachibot" watermark with PyMuPDF.
          The watermark is a /Image XObject that appears on >=90% of all pages
          and covers >=80% of page width/height.  Every invocation of that
          XObject is removed from the content streams (through Form XObjects
          too), any invocation that survives is neutralized as a safety net,
          and the watermark's text instances ("Sold by", "itachibot",
          "@itachibot") are removed from the content streams.
          Intermediate output: step1_no_watermark.pdf

  STEP 2  Rebuild a clean, searchable text layer with OCRmyPDF using --force-ocr
          (the broken text layer / ToUnicode CMaps are ignored, the pixels are
          re-OCRed).  If OCRmyPDF cannot run, fall back to
          pytesseract + reportlab (rendered page image + invisible text layer).

  STEP 3  Verify the output: pdftotext sample pages contain readable text, no
          watermark strings, and a summary is printed.

Usage
-----
    python3 fix_pdf.py /data/input_pdfs/OPH.pdf
    python3 fix_pdf.py OPH.pdf --language eng          # Hindi pack missing
    python3 fix_pdf.py OPH.pdf --skip-ocr              # only step 1
    python3 fix_pdf.py OPH.pdf --jobs 4 --keep-intermediate

Output
------
    <input dir>/<name>_CLEAN.pdf          final clean PDF
    <input dir>/step1_no_watermark.pdf    watermark-free intermediate (kept by default)

Environment
-----------
    pip install pymupdf ocrmypdf pytesseract reportlab
    apt-get install -y tesseract-ocr tesseract-ocr-hin ghostscript poppler-utils
    (unpaper is optional and only used by ocrmypdf --clean)

No AI/LLM calls.  100% local processing.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

PROGRESS_EVERY = 10          # print progress every N pages
DEFAULT_INPUT = "/data/input_pdfs/OPH.pdf"
WATERMARK_TEXT_RE = re.compile(r"(itachibot|sold\s*by)", re.IGNORECASE)
# Characters produced by broken/missing ToUnicode CMaps (never legitimate in a
# clean medical-MCQ text layer).
GARBAGE_CHARS = set("•€ŒŠ‹›ŠšŽžŸ˜ˆ™‰†‡¨¯°±²³´µ·¾¿×÷«»‚„…†‡")
# Chars that are legitimate in OCR'd eng+hin text (Basic Latin + punctuation,
# Devanagari, general punctuation, Latin-1).
READABLE_RE = re.compile(
    r"[\x20-\x7E\u00A0-\u00FF\u0900-\u097F\u2000-\u206F\u2010-\u2027]"
)

IMAGE_SUBTYPE = "/Image"
FORM_SUBTYPE = "/Form"

# --------------------------------------------------------------------------
# PDF content-stream tokenizer (minimal, safe)
# --------------------------------------------------------------------------

_WS = b" \t\r\n\f\x00"
_DELIM = b"()<>[]{}/%"
# NOTE: must map each hex char to its TRUE value (A/a -> 10 ... F/f -> 15).
# A naive enumeration over "0123456789abcdefABCDEF" maps 'A' to 16, which
# turns <FF> into byte 256 -> "byte must be in range(0, 256)".
_HEXVAL = {ord(c): int(c, 16) for c in "0123456789abcdefABCDEF"}
_ESCAPED = {
    ord("n"): b"\n",
    ord("r"): b"\r",
    ord("t"): b"\t",
    ord("b"): b"\b",
    ord("f"): b"\f",
}
_OPEN = {b"[": b"]", b"<<": b">>", b"{": b"}"}
_CLOSER = {b"]": b"[", b">>": b"<<", b"}": b"{"}


class ContentParseError(Exception):
    """Raised when a content stream cannot be parsed safely."""


def _is_break(c: int) -> bool:
    return c in _WS or c in _DELIM


def _parse_literal_string(data: bytes, i: int) -> tuple[bytes, int]:
    """Parse a PDF literal string starting at '('. Returns (bytes, next_i)."""
    n = len(data)
    i += 1
    out = bytearray()
    depth = 1
    while i < n:
        c = data[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= n:
                break
            e = data[i]
            if e in _ESCAPED:
                out += _ESCAPED[e]
                i += 1
            elif e in b"()\\":
                out.append(e)
                i += 1
            elif e in b"\r\n":
                # line continuation: CR, LF or CRLF is dropped
                if e == 0x0D and i + 1 < n and data[i + 1] == 0x0A:
                    i += 2
                else:
                    i += 1
            elif 0x30 <= e <= 0x37:  # octal, up to 3 digits
                val = 0
                k = 0
                while k < 3 and i < n and 0x30 <= data[i] <= 0x37:
                    val = val * 8 + (data[i] - 0x30)
                    i += 1
                    k += 1
                out.append(val & 0xFF)
            else:  # unknown escape: keep the escaped char
                out.append(e)
                i += 1
        elif c == 0x28:  # unescaped '(' - keep as content (defensive)
            depth += 1
            out.append(c)
            i += 1
        elif c == 0x29:  # ')'
            depth -= 1
            i += 1
            if depth == 0:
                break
            out.append(c)
        else:
            out.append(c)
            i += 1
    return bytes(out), i


def _parse_hex_string(data: bytes, i: int) -> tuple[bytes, int]:
    """Parse a PDF hex string starting at '<'. Returns (bytes, next_i)."""
    n = len(data)
    i += 1
    nibbles: bytearray = bytearray()
    while i < n and data[i] != 0x3E:
        c = data[i]
        i += 1
        if c in _WS:
            continue
        v = _HEXVAL.get(c)
        if v is not None:
            nibbles.append(v)
    if len(nibbles) % 2:
        nibbles.append(0)
    out = bytearray()
    for k in range(0, len(nibbles), 2):
        out.append((nibbles[k] << 4) | nibbles[k + 1])
    return bytes(out), i + 1


_NUM_RE = re.compile(rb"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_INLINE_EI_RE = re.compile(rb"[ \t\r\n\f]EI[ \t\r\n\f\x00]")
_INLINE_EI_RE2 = re.compile(rb"(?<![A-Za-z0-9])EI(?![A-Za-z0-9])")


def tokenize(data: bytes) -> list[tuple]:
    """
    Tokenize a PDF content stream.

    Token kinds:
      ('name', raw_bytes)            name without leading '/'
      ('num',  raw_bytes)            number as written
      ('str',  bytes, is_hex)        literal/hex string payload
      ('word', raw_bytes)            operator / keyword
      ('delim', raw_bytes)           one of b'[' b']' b'<<' b'>>' b'{' b'}'

    Inline images (BI ... ID ... EI) are skipped as a unit so their binary
    payload cannot corrupt the parse.  Raises ContentParseError on an unsafe
    stream (caller then leaves it untouched).
    """
    tokens: list[tuple] = []
    i, n = 0, len(data)
    inline = False
    while i < n:
        c = data[i]
        if c in _WS:
            i += 1
            continue
        if c == 0x25:  # '%' comment to end of line
            while i < n and data[i] not in b"\r\n":
                i += 1
            continue
        if c == 0x28:  # '(' literal string
            raw, i = _parse_literal_string(data, i)
            tokens.append(("str", raw, False))
            continue
        if c == 0x3C:  # '<'
            if i + 1 < n and data[i + 1] == 0x3C:
                tokens.append(("delim", b"<<"))
                i += 2
                continue
            raw, i = _parse_hex_string(data, i)
            tokens.append(("str", raw, True))
            continue
        if c == 0x3E:  # '>'
            if i + 1 < n and data[i + 1] == 0x3E:
                tokens.append(("delim", b">>"))
                i += 2
            else:
                tokens.append(("delim", b">"))
                i += 1
            continue
        if c in b"[]{}":
            tokens.append(("delim", bytes([c])))
            i += 1
            continue
        if c == 0x2F:  # '/' name
            j = i + 1
            while j < n and not _is_break(data[j]):
                j += 1
            tokens.append(("name", data[i + 1 : j]))
            i = j
            continue
        # word / number
        j = i
        while j < n and not _is_break(data[j]):
            j += 1
        word = data[i:j]
        if word == b"BI":
            inline = True
        elif word == b"ID" and inline:
            # skip binary data until whitespace-preceded 'EI'
            m = _INLINE_EI_RE.search(data, j)
            if m is None:
                m = _INLINE_EI_RE2.search(data, j)
            if m is None:
                raise ContentParseError("inline image without closing EI")
            i = m.end()
            inline = False
            continue
        if _NUM_RE.match(word):
            tokens.append(("num", word))
        else:
            tokens.append(("word", word))
        i = j
    return tokens


def _escape_literal(raw: bytes) -> bytes:
    out = bytearray()
    for b in raw:
        if b == 0x5C:
            out += b"\\\\"
        elif b == 0x28:
            out += b"\\("
        elif b == 0x29:
            out += b"\\)"
        elif b < 0x20 or b > 0x7E:
            out += ("\\%03o" % b).encode("ascii")
        else:
            out.append(b)
    return bytes(out)


def serialize_token(tok: tuple) -> bytes:
    kind = tok[0]
    if kind == "name":
        return b"/" + tok[1]
    if kind == "num":
        return tok[1]
    if kind == "str":
        raw, is_hex = tok[1], tok[2]
        if is_hex:
            return b"<" + raw.hex().upper().encode("ascii") + b">"
        return b"(" + _escape_literal(raw) + b")"
    if kind == "word":
        return tok[1]
    if kind == "delim":
        return tok[1]
    raise ContentParseError(f"unknown token kind {kind!r}")


def group_tokens(tokens: list[tuple]) -> list[tuple]:
    """
    Collapse bracketed/dict groups into single ('group', (tokens,)) operands.
    Returns the flat token list with groups intact but operators unchanged.
    """
    flat: list[tuple] = []
    stack: list[bytes] = []
    buf: list[tuple] = []
    for tok in tokens:
        if tok[0] == "delim" and tok[1] in _OPEN:
            stack.append(tok[1])
            buf.append(tok)
        elif tok[0] == "delim" and tok[1] in _CLOSER:
            buf.append(tok)
            if stack and stack[-1] == _CLOSER[tok[1]]:
                stack.pop()
                if not stack:
                    flat.append(("group", tuple(buf)))
                    buf = []
            # unmatched close stays inside the current operand buffer
        elif stack:
            buf.append(tok)
        else:
            flat.append(tok)
    if buf:  # unbalanced stream: keep everything as one group
        flat.append(("group", tuple(buf)))
    return flat


def parse_operators(tokens: list[tuple]) -> list[tuple[list[tuple], tuple]]:
    """
    Split grouped tokens into [(operand_tokens, operator_token), ...].
    """
    ops: list[tuple[list[tuple], tuple]] = []
    operands: list[tuple] = []
    for tok in group_tokens(tokens):
        if tok[0] == "word":
            ops.append((operands, tok))
            operands = []
        else:
            operands.append(tok)
    if operands:  # trailing tokens without an operator: keep intact
        ops.append((operands, ("word", b"")))
    return ops


def _string_matches_watermark(raw: bytes) -> bool:
    """True if a content-stream string looks like watermark text."""
    low = raw.lower()
    if b"itachibot" in low or b"@itachibot" in low:
        return True
    # covers "Sold by@itachibot" / "Sold by itachibot" / "Sold by" alone
    if b"sold" in low and b"by" in low:
        return True
    # Unicode payloads (UTF-16BE etc.) get one extra chance
    for enc in ("utf-16-be", "utf-8", "utf-16"):
        try:
            s = raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
        if WATERMARK_TEXT_RE.search(s):
            return True
    return False


# --------------------------------------------------------------------------
# PDF dictionary / resource helpers
# --------------------------------------------------------------------------


def parse_object_bytes(raw: bytes) -> list[tuple]:
    """
    Parse a small PDF object (e.g. '<< /XObject << /Wm 8 0 R >> >>') into a
    flat token list with groups collapsed.  Returns [] on parse failure.
    """
    try:
        return group_tokens(tokenize(raw))
    except ContentParseError:
        return []


def _ref_xref(vals: list[tuple]) -> int | None:
    """vals == [num N, num 0, word R]  ->  N, else None."""
    if (len(vals) == 3 and vals[0][0] == "num" and vals[1][0] == "num"
            and vals[2] == ("word", b"R")):
        try:
            return int(vals[0][1])
        except ValueError:
            return None
    return None


def dict_pairs(flat: list[tuple]) -> list[tuple[bytes, tuple]]:
    """
    From grouped tokens of a dictionary, return [(key_bytes, ('xref', N) |
    ('tokens', [value tokens]))].  Indirect references 'N 0 R' are resolved.
    """
    pairs: list[tuple[bytes, tuple]] = []
    containers: list[tuple] = []
    for tok in flat:
        if tok[0] == "group":
            inner = tok[1]
            if inner and inner[0] == ("delim", b"<<"):
                containers.append(tok)
    if not containers:
        containers = [("group", tuple(flat))]
    for cont in containers:
        inner = list(cont[1])
        if inner and inner[0] == ("delim", b"<<"):
            inner = inner[1:]
        if inner and inner[-1] == ("delim", b">>"):
            inner = inner[:-1]
        i = 0
        while i < len(inner):
            if inner[i][0] == "name":
                key = inner[i][1]
                i += 1
                vals: list[tuple] = []
                while i < len(inner) and inner[i][0] != "name":
                    vals.append(inner[i])
                    i += 1
                xref = _ref_xref(vals)
                if xref is not None:
                    pairs.append((key, ("xref", xref)))
                else:
                    pairs.append((key, ("tokens", vals)))
            else:
                i += 1
    return pairs


def resolve_xobject_map(raw_dict: bytes) -> dict[bytes, int]:
    """
    Parse a '/XObject << /Name N 0 R ... >>' dict string into
    {name_bytes: xref}.
    """
    flat = parse_object_bytes(raw_dict)
    result: dict[bytes, int] = {}
    for name, val in dict_pairs(flat):
        if val[0] == "xref":
            result[name] = val[1]
    return result


def get_xobject_map(doc, obj_xref: int) -> dict[bytes, int]:
    """
    Return {resource_name_bytes: xobject_xref} for the /Resources/XObject of a
    Page or Form XObject.  Handles direct and indirect Resources and XObject
    dictionaries.
    """
    try:
        t, v = doc.xref_get_key(obj_xref, "Resources/XObject")
    except Exception:
        return {}
    if t == "xref":
        # the XObject dictionary itself is an indirect object
        try:
            xo_xref = int(v.split()[0])
        except (ValueError, IndexError):
            return {}
        result: dict[bytes, int] = {}
        try:
            names = doc.xref_get_keys(xo_xref)
        except Exception:
            return result
        for name in names:
            nv = doc.xref_get_key(xo_xref, name)
            if nv[0] == "xref":
                pieces = nv[1].split()
                if len(pieces) >= 1:
                    try:
                        result[name.encode("latin-1")] = int(pieces[0])
                    except (ValueError, UnicodeEncodeError):
                        pass
        return result
    if t == "dict":
        return resolve_xobject_map(v.encode("latin-1", "replace"))
    return {}


_SUBTYPE_CACHE: dict[tuple, str] = {}


def xobject_subtype(doc, xref: int) -> str:
    key = (id(doc), xref)
    if key in _SUBTYPE_CACHE:
        return _SUBTYPE_CACHE[key]
    subtype = ""
    try:
        t, v = doc.xref_get_key(xref, "Subtype")
        if t == "name":
            subtype = v
    except Exception:
        subtype = ""
    _SUBTYPE_CACHE[key] = subtype
    return subtype


# --------------------------------------------------------------------------
# Content-stream patching
# --------------------------------------------------------------------------


class PatchStats:
    def __init__(self) -> None:
        self.dropped_dos = 0
        self.dropped_text_ops = 0
        self.forms_patched = 0
        self.neutralized_images = 0
        self.redaction_fallbacks = 0
        self.unparsed_streams = 0

    def summary(self) -> str:
        return (
            f"removed {self.dropped_dos} watermark draws, "
            f"{self.dropped_text_ops} watermark text ops, "
            f"{self.forms_patched} forms patched, "
            f"neutralized {self.neutralized_images} image object(s), "
            f"{self.unparsed_streams} stream(s) skipped"
        )


def patch_data(
    doc,
    data: bytes,
    xo_map: dict[bytes, int],
    watermark_xrefs: set[int],
    stats: PatchStats,
    forms_done: set[int],
) -> bytes:
    """
    Remove operations from a content stream:
      * 'Do' ops invoking a watermark image XObject;
      * 'Do' ops invoking a Form XObject (recurse into the form's stream);
      * text ops ('Tj', 'TJ', quote, doublequote) whose payload contains
        watermark text.

    Only operand+operator are dropped; graphics state (q/Q) is never touched,
    so the stream stays balanced.  Returns the new stream (identical bytes when
    nothing changed).  On parse failure the stream is returned unchanged.
    """
    data = bytes(data)
    try:
        tokens = tokenize(data)
    except ContentParseError as exc:
        stats.unparsed_streams += 1
        print(f"  [warn] content stream not parsed ({exc}); left untouched",
              file=sys.stderr)
        return data, False

    ops = parse_operators(tokens)
    remove_ops: set[int] = set()
    forms_to_patch: set[int] = set()

    for idx, (operands, op) in enumerate(ops):
        opname = op[1]
        if opname == b"Do":
            name = None
            for tok in reversed(operands):
                if tok[0] == "name":
                    name = tok[1]
                    break
            if name is not None and name in xo_map:
                xref = xo_map[name]
                if xref in watermark_xrefs:
                    remove_ops.add(idx)
                    stats.dropped_dos += 1
                elif xobject_subtype(doc, xref) == FORM_SUBTYPE:
                    forms_to_patch.add(xref)
        elif opname in (b"Tj", b"'"):
            if operands and operands[-1][0] == "str":
                if _string_matches_watermark(operands[-1][1]):
                    remove_ops.add(idx)
                    stats.dropped_text_ops += 1
        elif opname == b"TJ":
            for tok in reversed(operands):
                if tok[0] == "group":
                    for sub in tok[1]:
                        if sub[0] == "str" and _string_matches_watermark(sub[1]):
                            remove_ops.add(idx)
                            stats.dropped_text_ops += 1
                    break
        elif opname == b'"':
            if operands and operands[-1][0] == "str":
                if _string_matches_watermark(operands[-1][1]):
                    remove_ops.add(idx)
                    stats.dropped_text_ops += 1

    # patch referenced forms first (they may contain the watermark)
    forms_ok = True
    for fx in sorted(forms_to_patch):
        if fx in forms_done:
            continue
        forms_done.add(fx)
        try:
            old = doc.xref_stream(fx)
        except Exception:
            stats.unparsed_streams += 1
            forms_ok = False
            continue
        fx_map = get_xobject_map(doc, fx)
        new, ok = patch_data(doc, old, fx_map, watermark_xrefs, stats,
                             forms_done)
        if not ok:
            forms_ok = False
        if new != old:
            try:
                doc.update_stream(fx, new)
                stats.forms_patched += 1
            except Exception:
                forms_ok = False

    if not remove_ops:
        return data, forms_ok

    lines: list[bytes] = []
    for idx, (operands, op) in enumerate(ops):
        if idx in remove_ops:
            continue
        pieces = []
        for tok in operands:
            if tok[0] == "group":
                pieces.append(b" ".join(serialize_token(t) for t in tok[1]))
            else:
                pieces.append(serialize_token(tok))
        if op[1]:
            pieces.append(serialize_token(op))
        if pieces:
            lines.append(b" ".join(pieces))
    return (b"\n".join(lines) if lines else b""), forms_ok


def patch_page(doc, page, watermark_xrefs: set[int], stats: PatchStats,
               forms_done: set[int]) -> bool:
    """
    Patch one page's /Contents (and any forms it uses).

    Returns True when the page was parsed completely (i.e. we KNOW no
    watermark draw remains), False when a stream could not be parsed and the
    page may still draw the watermark (caller must then neutralize the object).
    """
    data = page.read_contents()
    if not data:
        return True
    xo_map = get_xobject_map(doc, page.xref)
    new, ok = patch_data(doc, data, xo_map, watermark_xrefs, stats, forms_done)
    if new != data:
        xref = doc.get_new_xref()
        doc.update_object(xref, "<< /Length 0 >>")
        doc.update_stream(xref, new)
        doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")
    return ok


def neutralize_image_object(doc, page, xref: int, stats: PatchStats) -> None:
    """
    Safety net: replace an image XObject with a 1x1 fully transparent image.
    Content streams stay valid and any CTM that stretches the replacement
    paints nothing.  Uses PyMuPDF's tested replace_image/delete_image path
    (which installs a proper SMask, unlike a hand-rolled DeviceGray swap).
    """
    try:
        if not doc.xref_is_image(xref):
            return
        page.delete_image(xref)
        stats.neutralized_images += 1
    except Exception as exc:
        print(f"  [warn] could not neutralize image xref {xref}: {exc}",
              file=sys.stderr)


def redact_watermark_text(doc, page, stats: PatchStats) -> int:
    """
    Fallback for encodings the byte-level scan cannot decode (and for streams
    that MuPDF can still extract text from but rawdict cannot): find the
    watermark strings with MuPDF's own text search and redact exactly those
    rectangles (text only - images and line art are preserved).
    """
    rects: list[fitz.Rect] = []
    # 1) rawdict spans (precise per-span rects)
    try:
        raw = page.get_text("rawdict")
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if WATERMARK_TEXT_RE.search(span.get("text", "") or ""):
                        rects.append(fitz.Rect(span["bbox"]))
    except Exception:
        rects = []
    # 2) MuPDF text search - works even when rawdict spans are empty.
    # Longest needle first, so "Sold by@itachibot" wins over its substrings
    # and the "@" between them is covered (no visual leftovers).
    if not rects:
        for needle in ("Sold by@itachibot", "itachibot", "Sold by"):
            try:
                for r in page.search_for(needle):
                    if not any(fitz.Rect(r).intersects(x) for x in rects):
                        rects.append(fitz.Rect(r))
            except Exception:
                continue
    # 3) last resort: word boxes containing the pattern
    if not rects:
        try:
            for w in page.get_text("words"):
                if WATERMARK_TEXT_RE.search(w[4]):
                    rects.append(fitz.Rect(w[:4]))
        except Exception:
            pass

    for r in rects:
        page.add_redact_annot(r)
    if rects:
        try:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
            stats.redaction_fallbacks += 1
        except Exception as exc:
            print(f"  [warn] redaction failed on a page: {exc}", file=sys.stderr)
            return 0
    return len(rects)


# --------------------------------------------------------------------------
# STEP 1: watermark detection + removal
# --------------------------------------------------------------------------


class WatermarkGroup:
    def __init__(self, digest: bytes, xref: int, width: int, height: int) -> None:
        self.digest = digest
        self.xrefs: set[int] = {xref}
        self.pages: set[int] = set()       # page numbers where drawn
        self.cover: list[float] = []       # best min(w,h) ratio per page
        self.width = width
        self.height = height

    @property
    def frequency(self) -> float:
        return len(self.pages)


class WatermarkAnalysis:
    def __init__(self) -> None:
        self.groups: list[WatermarkGroup] = []
        self.total_pages = 0
        self.candidates: list[int] = []    # watermark xrefs to remove
        self.candidate_groups: list[WatermarkGroup] = []


def analyze_watermarks(doc, pages) -> WatermarkAnalysis:
    """
    Detect the watermark.

    Images are grouped by pixel digest (not by xref): some PDF generators use
    one shared XObject, others embed a copy of the image on every page, and a
    digest group catches both.  A group is a watermark when it is drawn on
    >=90% of all pages and covers >=80% of page width/height (median of the
    min(w,h) ratio).  All xrefs in a qualifying group are removed surgically.
    """
    an = WatermarkAnalysis()
    an.total_pages = len(pages)
    groups_by_digest: dict[bytes, WatermarkGroup] = {}

    for pno, page in enumerate(pages):
        prect = page.rect
        if prect.is_empty or prect.is_infinite:
            continue
        try:
            infos = page.get_image_info(xrefs=True, hashes=True)
        except Exception:
            infos = []
        if not infos:
            # fallback for older PyMuPDF: image xrefs + drawn rects
            try:
                images = page.get_images(full=True)
            except Exception:
                images = []
            for item in images:
                xref = item[0] if not isinstance(item, dict) else item["xref"]
                try:
                    rects = page.get_image_rects(xref)
                except Exception:
                    rects = []
                for r in rects:
                    infos.append({"xref": xref, "bbox": tuple(r),
                                  "digest": b"", "width": 0, "height": 0})
        for it in infos:
            xref = it.get("xref")
            digest = it.get("digest") or b""
            bbox = it.get("bbox")
            if xref is None or not bbox:
                continue
            rect = fitz.Rect(bbox)
            wr = min(1.0, rect.width / max(prect.width, 1e-9))
            hr = min(1.0, rect.height / max(prect.height, 1e-9))
            ratio = min(wr, hr)
            key = digest or f"xr:{xref}".encode()
            g = groups_by_digest.get(key)
            if g is None:
                g = WatermarkGroup(key, xref, it.get("width", 0) or 0,
                                   it.get("height", 0) or 0)
                groups_by_digest[key] = g
            else:
                g.xrefs.add(xref)
            g.pages.add(pno)
            g.cover.append(ratio)
        if (pno + 1) % PROGRESS_EVERY == 0:
            print(f"[step1] analyzed images on page {pno + 1}/{len(pages)}")

    an.groups = list(groups_by_digest.values())
    an.groups.sort(key=lambda g: -len(g.pages))
    for g in an.groups:
        freq = len(g.pages) / max(1, an.total_pages)
        med = statistics.median(g.cover) if g.cover else 0.0
        if freq >= 0.90 and med >= 0.80:
            an.candidate_groups.append(g)
            an.candidates.extend(sorted(g.xrefs))
    an.candidates = sorted(set(an.candidates))
    return an


def remove_watermark(input_pdf: Path, output_pdf: Path, args) -> tuple[PatchStats, list[int]]:
    print("[step1] opening", input_pdf)
    doc = fitz.open(input_pdf)
    if doc.needs_pass:
        doc.close()
        raise RuntimeError("input PDF is encrypted; provide a decrypted copy")
    pages = list(doc)
    n = len(pages)
    print(f"[step1] {n} pages")

    an = analyze_watermarks(doc, pages)
    print("[step1] image groups by content digest - frequency/coverage (top 5):")
    for g in an.groups[:5]:
        med = statistics.median(g.cover) if g.cover else 0.0
        digest = g.digest.hex()[:10] if g.digest else f"xref {min(g.xrefs)}"
        print(f"  digest {digest}: on {len(g.pages)}/{n} pages "
              f"({len(g.pages) / max(1, n):.0%}), median min(w,h) coverage "
              f"{med:.0%}, xrefs {sorted(g.xrefs)}")

    if not an.candidates:
        doc.close()
        raise RuntimeError(
            "no candidate watermark XObject found "
            "(need >=90% page frequency and >=80% width/height coverage). "
            "Inspect the report above; refusing to proceed so no figure is damaged."
        )
    print(f"[step1] watermark XObject(s) detected: {an.candidates} "
          f"(content digest groups: {len(an.candidate_groups)})")

    stats = PatchStats()
    watermark_set = set(an.candidates)
    forms_done: set[int] = set()
    t0 = time.time()
    # "dirty" = we could not prove the watermark draw is gone (parse failure
    # on the page or on a Form it uses).  NOTE: we never trust get_image_rects
    # here - PyMuPDF caches per-page image info, so it keeps answering with
    # stale rects after an in-place content swap.
    dirty: list[int] = []
    for pno, page in enumerate(pages):
        try:
            ok = patch_page(doc, page, watermark_set, stats, forms_done)
            if not ok:
                dirty.append(pno + 1)
        except Exception as exc:
            dirty.append(pno + 1)
            print(f"  [warn] page {pno + 1}: content patch failed: {exc}",
                  file=sys.stderr)
        if (pno + 1) % PROGRESS_EVERY == 0:
            print(f"[step1] processed page {pno + 1}/{n}  -  {stats.summary()}")

    # GUARANTEED visual removal: if any page's stream could not be parsed, we
    # cannot be sure the watermark draw is gone, so replace the watermark
    # image OBJECT with a fully transparent image.  This is object-level (one
    # call), so EVERY page referencing it - including unparsed streams -
    # paints nothing.  Use a freshly loaded page object (never a stale one).
    if dirty:
        print(f"[step1] safeguard: replacing watermark image object(s) with "
              f"transparent (ambiguous page(s): {sorted(set(dirty))})")
        for xref in watermark_set:
            try:
                neutralize_image_object(doc, doc[0], xref, stats)
            except Exception as exc:
                print(f"  [warn] could not neutralize image xref {xref}: {exc}",
                      file=sys.stderr)
    else:
        print("[step1] all watermark draws removed surgically; no object "
              "replacement needed")

    # text-layer fallback for exotic font encodings
    print("[step1] checking text layer for residual watermark strings ...")
    for pno, page in enumerate(pages):
        try:
            text = page.get_text()
        except Exception:
            text = ""
        if text and WATERMARK_TEXT_RE.search(text):
            hits = redact_watermark_text(doc, page, stats)
            print(f"  [step1] page {pno + 1}: redacted {hits} watermark text span(s)")

    out_pdf = Path(output_pdf)
    tmp = out_pdf.with_suffix(out_pdf.suffix + ".tmp")
    doc.save(
        tmp,
        garbage=4,      # drop the now-unreferenced watermark objects/xrefs
        deflate=True,
        clean=False,
    )
    doc.close()
    os.replace(tmp, out_pdf)

    elapsed = time.time() - t0
    print(f"[step1] saved {out_pdf}  -  {stats.summary()}  in {elapsed:.1f}s")
    return stats, list(an.candidates)


def verify_step1(doc, watermark_xrefs: list[int], label: str) -> bool:
    """Confirm the watermark image is no longer drawn and no watermark text
    remains in the (broken but still extractable) text layer."""
    ok = True
    n = len(doc)
    for pno, page in enumerate(doc):
        for xref in watermark_xrefs:
            try:
                if page.get_image_rects(xref):
                    print(f"  [{label}] page {pno + 1}: watermark image still drawn")
                    ok = False
            except Exception:
                pass
        if (pno + 1) % PROGRESS_EVERY == 0:
            print(f"  [{label}] verified page {pno + 1}/{n} (image layer)")
    text_hits = 0
    for page in doc:
        try:
            t = page.get_text() or ""
        except Exception:
            t = ""
        text_hits += len(WATERMARK_TEXT_RE.findall(t))
    if text_hits:
        print(f"  [{label}] watermark text still present in {text_hits} place(s) "
              f"(broken CMap may hide it from text extraction; OCR replaces the "
              f"text layer anyway)")
        ok = False
    else:
        print(f"  [{label}] no watermark text in text layer")
    return ok


# --------------------------------------------------------------------------
# STEP 2: OCR / text layer rebuild
# --------------------------------------------------------------------------


def probe_tesseract() -> tuple[bool, list[str]]:
    """Return (installed, available_languages)."""
    exe = shutil.which("tesseract")
    if not exe:
        return False, []
    try:
        out = subprocess.run([exe, "--list-langs"], capture_output=True,
                             text=True, timeout=120)
    except Exception:
        return False, []
    blob = (out.stdout or "") + "\n" + (out.stderr or "")
    langs = []
    for line in blob.splitlines():
        line = line.strip()
        if not line or line.startswith("List of") or "tessdata" in line.lower():
            continue
        if "/" in line or " " in line or line.lower().startswith("available"):
            continue
        langs.append(line)
    return True, langs


def run_ocrmypdf(in_pdf: Path, out_pdf: Path, args) -> tuple[str, bool]:
    """Run OCRmyPDF with --force-ocr.  Returns (language_used, success)."""
    try:
        import ocrmypdf
    except ImportError:
        print("[step2] ocrmypdf not installed - using fallback", file=sys.stderr)
        return "", False

    tess_ok, langs = probe_tesseract()
    if not tess_ok:
        print("[step2] tesseract binary not found - using fallback",
              file=sys.stderr)
        return "", False

    requested = [l for l in (args.language or "eng+hin").split("+") if l]
    missing = [l for l in requested if l not in langs]
    if missing:
        print(f"[step2] tesseract language pack(s) missing: {missing}; "
              f"installed: {langs}", file=sys.stderr)
        requested = [l for l in requested if l in langs]
    if not requested:
        requested = ["eng"] if "eng" in langs else []
    if not requested:
        print("[step2] no usable tesseract language packs - using fallback",
              file=sys.stderr)
        return "", False
    language = "+".join(requested)
    print(f"[step2] OCR language: {language}")

    # --- memory safety ---------------------------------------------------
    # OCRmyPDF runs `jobs` tesseract processes concurrently; each one renders
    # a full page at ~300 DPI and Leptonica keeps several pixmap copies, so
    # 4 jobs easily exceed a 512 MB Railway container -> the kernel OOM-kills
    # the process (exit code -9).  Clamp to the available CPUs and 1 OpenMP
    # thread per tesseract process.
    cpu = max(1, os.cpu_count() or 1)
    jobs = max(1, min(args.jobs, cpu))
    if jobs != args.jobs:
        print(f"[step2] jobs clamped from {args.jobs} to {jobs} "
              f"(CPU count {cpu}, memory safety)")
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    if os.environ.get("OMP_THREAD_LIMIT") == "1":
        print("[step2] OMP_THREAD_LIMIT=1 (one thread per tesseract process)")

    gs_ok = shutil.which("gs") is not None
    unpaper_ok = shutil.which("unpaper") is not None
    if args.output_type == "pdfa" and not gs_ok:
        print("[step2] ghostscript not found; falling back to output-type pdf",
              file=sys.stderr)
    output_type = "pdfa" if (gs_ok and args.output_type == "pdfa") else "pdf"

    kwargs = dict(
        language=requested,
        force_ocr=True,
        jobs=jobs,
        deskew=args.deskew,
        output_type=output_type,
        progress_bar=True,
    )
    if unpaper_ok and args.clean:
        kwargs["clean"] = True
    elif args.clean and not unpaper_ok:
        print("[step2] unpaper not found; skipping --clean", file=sys.stderr)

    tmp_out = out_pdf.with_suffix(out_pdf.suffix + ".tmp")
    t0 = time.time()
    try:
        print(f"[step2] ocrmypdf: force-ocr, language={language}, "
              f"jobs={jobs}, deskew={args.deskew}, "
              f"clean={kwargs.get('clean', False)}, output-type={output_type}  "
              f"({in_pdf.name} -> {out_pdf.name})")
        rc = ocrmypdf.ocr(str(in_pdf), str(tmp_out),
                          keep_temporary_files=args.keep_tmp, **kwargs)
        if rc != 0:
            print(f"[step2] ocrmypdf exit code {rc}", file=sys.stderr)
            tmp_out.unlink(missing_ok=True)
            return language, False
        os.replace(tmp_out, out_pdf)
        print(f"[step2] ocrmypdf finished in {time.time() - t0:.1f}s")
        return language, True
    except Exception as exc:
        print(f"[step2] ocrmypdf failed: {exc}", file=sys.stderr)
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return language, False


def _find_unicode_font() -> tuple[str | None, str]:
    """
    Try to locate a Unicode TTF for the invisible text layer.
    Returns (path, family_name); (None, 'Helvetica') as last resort.
    Devanagari-aware fonts are preferred so Hindi OCR text stays searchable.
    """
    try:
        import ocrmypdf
        ocrmypdf_data = os.path.dirname(ocrmypdf.__file__)
        bundled_noto = os.path.join(ocrmypdf_data, "data", "NotoSans-Regular.ttf")
    except Exception:
        bundled_noto = ""
    candidates = [
        # Debian/Ubuntu (prefer Devanagari-capable faces)
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
        "/usr/share/fonts/truetype/mangal/Mangal-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        bundled_noto,  # ocrmypdf's bundled Noto Sans (covers Latin + scripts)
    ]
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.pdfbase import pdfmetrics
            name = "FixPdfUnicode"
            pdfmetrics.registerFont(TTFont(name, path))
            return path, name
        except Exception:
            continue
    return None, "Helvetica"


def run_fallback_ocr(in_pdf: Path, out_pdf: Path, args, language: str) -> bool:
    """
    Fallback requested by the task: pytesseract OCR -> reportlab PDF.
    Pages are re-rendered at args.fallback_dpi (visual content unchanged) and
    OCR word boxes are written as an invisible text layer.
    """
    try:
        import pytesseract
        from PIL import Image
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.utils import ImageReader
    except ImportError as exc:
        print(f"[step2] fallback dependencies missing: {exc} - cannot OCR",
              file=sys.stderr)
        return False

    tess_ok, langs = probe_tesseract()
    if not tess_ok:
        print(
            "[step2] FATAL: no OCR engine available. Install:\n"
            "  apt-get install -y tesseract-ocr tesseract-ocr-hin ghostscript\n"
            "  pip install pymupdf ocrmypdf pytesseract reportlab\n"
            f"The watermark-free intermediate was kept at {in_pdf}\n",
            file=sys.stderr,
        )
        return False
    requested = [l for l in language.split("+") if l in langs] or ["eng"]
    lang = "+".join(requested)
    font_path, font_name = _find_unicode_font()
    if font_path:
        print(f"[step2] fallback: pytesseract (language={lang}, dpi={args.fallback_dpi}, "
              f"font={os.path.basename(font_path)})")
    else:
        print(f"[step2] fallback: pytesseract (language={lang}, dpi={args.fallback_dpi}, "
              f"font=Helvetica; Hindi chars may not be searchable)")

    src = fitz.open(in_pdf)
    dpi = args.fallback_dpi
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    tmp_out = out_pdf.with_suffix(out_pdf.suffix + ".tmp")
    c = rl_canvas.Canvas(str(tmp_out), pageCompression=1)
    n = len(src)
    t0 = time.time()
    scale = 72.0 / dpi  # px -> pt
    total_words = 0
    for pno, page in enumerate(src):
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        tmp_img = None
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            im.save(fh, format="PNG")
            tmp_img = fh.name
        reader = ImageReader(tmp_img)
        pw, ph = page.rect.width, page.rect.height
        c.setPageSize((pw, ph))
        c.drawImage(reader, 0, 0, width=pw, height=ph)
        try:
            data = pytesseract.image_to_data(
                im, lang=lang, output_type=pytesseract.Output.DICT,
                config="--psm 3",
            )
        except Exception as exc:
            print(f"  [fallback] page {pno + 1}: pytesseract failed: {exc}",
                  file=sys.stderr)
            data = {}
        words = [str(w) for w in data.get("text", []) if str(w).strip()]
        total_words += len(words)
        # invisible text layer (render mode 3), one text object per word run
        boxes = zip(
            data.get("left", []), data.get("top", []),
            data.get("width", []), data.get("height", []),
            data.get("text", []),
        )
        for left, top, width, height, word in boxes:
            if not word or not str(word).strip():
                continue
            x = (float(left) + float(width) / 2) * scale
            y = ph - (float(top) + float(height) / 2) * scale
            # reportlab: invisible text via PDFTextObject.setTextRenderMode(3)
            t = c.beginText()
            t.setFont(font_name, 8)
            t.setTextRenderMode(3)
            t.setTextOrigin(x, y)
            try:
                t.textOut(str(word))
            except Exception:
                # font cannot encode this word (e.g. Devanagari with
                # Helvetica): skip rather than corrupt the page
                continue
            c.drawText(t)
        try:
            os.unlink(tmp_img)
        except Exception:
            pass
        c.showPage()  # commit the page (reportlab accumulates into one page otherwise)
        if (pno + 1) % PROGRESS_EVERY == 0:
            print(f"[step2] fallback OCR page {pno + 1}/{n} "
                  f"({time.time() - t0:.0f}s elapsed; {len(words)} words on this page)")
    src.close()
    c.save()
    if total_words == 0:
        print("[step2] fallback OCR produced no text on any page "
              "(OCR engine failure?) - discarding image-only output",
              file=sys.stderr)
        try:
            os.unlink(tmp_out)
        except Exception:
            pass
        return False
    os.replace(tmp_out, out_pdf)
    print(f"[step2] fallback finished in {time.time() - t0:.1f}s "
          f"({total_words} words total)")
    return True


# --------------------------------------------------------------------------
# STEP 3: verification
# --------------------------------------------------------------------------


def extract_text_page(pdf_path: Path, page_no: int) -> str:
    """Extract one page's text with pdftotext (preferred) or pypdf."""
    exe = shutil.which("pdftotext")
    if exe:
        try:
            r = subprocess.run(
                [exe, "-f", str(page_no), "-l", str(page_no), "-layout",
                 str(pdf_path), "-"],
                capture_output=True, text=True, timeout=600,
            )
            if r.returncode == 0:
                return r.stdout
        except Exception:
            pass
    try:
        from pypdf import PdfReader
        rdr = PdfReader(str(pdf_path))
        return rdr.pages[page_no - 1].extract_text() or ""
    except Exception as exc:
        print(f"  [verify] extraction failed for page {page_no}: {exc}",
              file=sys.stderr)
        return ""


def page_quality(text: str) -> tuple[float, float, int]:
    """
    Returns (readable_ratio, garbage_ratio, word_count).
    Broken CMaps leak symbols like .~EU  and PUA chars; those lower the
    readable ratio and raise the garbage ratio.
    """
    text = text or ""
    cleaned = re.sub(r"\s+", " ", text)
    total = len(cleaned)
    if total == 0:
        return 0.0, 0.0, 0
    good = sum(1 for ch in cleaned if READABLE_RE.match(ch))
    garbage = sum(1 for ch in cleaned
                  if ch in GARBAGE_CHARS or 0xE000 <= ord(ch) <= 0xF8FF)
    words = re.findall(r"[0-9A-Za-z\u0900-\u097F]+", text)
    return good / total, garbage / max(1, total), len(words)


def verify_output(pdf_path: Path, sample_pages, language: str, tag: str) -> dict:
    try:
        from pypdf import PdfReader
        total = len(PdfReader(str(pdf_path)).pages)
    except Exception:
        total = 0
    print(f"[verify] {tag}: {total} pages; sampling {sample_pages}")

    watermark_hits = 0
    bad_samples = 0
    results = []
    seen_any = False
    for pno in sample_pages:
        if pno < 1 or pno > total:
            continue
        seen_any = True
        text = extract_text_page(pdf_path, pno)
        read, garb, words = page_quality(text)
        wm = len(WATERMARK_TEXT_RE.findall(text))
        watermark_hits += wm
        bad = garb > 0.05 or read < 0.55 or words < 5
        if bad:
            bad_samples += 1
        print(f"  page {pno:>4}: readable {read:.0%}, garbage {garb:.2%}, "
              f"words {words}, watermark hits {wm}  "
              f"[{'BAD' if bad or wm else 'ok'}]")
        samp = Path(tempfile.gettempdir()) / f"fix_pdf_sample_p{pno}.txt"
        samp.write_text(text, encoding="utf-8")
        results.append({"page": pno, "readable": read, "garbage": garb,
                        "words": words, "watermark": wm})

    # full-document watermark scan via pdftotext (definitive)
    full = ""
    exe = shutil.which("pdftotext")
    if exe and total:
        try:
            r = subprocess.run([exe, str(pdf_path), "-"], capture_output=True,
                               text=True, timeout=3600)
            if r.returncode == 0:
                full = r.stdout
        except Exception:
            pass
    if not full:
        full = " ".join(extract_text_page(pdf_path, p) for p in sample_pages
                        if p <= total)
    full_hits = len(WATERMARK_TEXT_RE.findall(full or ""))

    ok = (seen_any and watermark_hits == 0 and full_hits == 0
          and bad_samples == 0)
    return {"pages": total, "language": language, "ok": ok,
            "sample": results, "full_watermark_hits": full_hits,
            "bad_samples": bad_samples,
            "full_char_count": len(full or "")}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Remove the itachibot watermark and rebuild the OCR text "
                    "layer of a garbled medical MCQ PDF.",
    )
    p.add_argument("input", nargs="?", default=DEFAULT_INPUT,
                   help=f"input PDF (default {DEFAULT_INPUT})")
    p.add_argument("--output", default=None,
                   help="explicit output path for the clean PDF "
                        "(default: <input dir>/<stem>_CLEAN.pdf)")
    p.add_argument("--intermediate", default=None,
                   help="explicit path for step1_no_watermark.pdf "
                        "(default: <input dir>/step1_no_watermark.pdf)")
    p.add_argument("--language", default="eng+hin",
                   help="OCR languages, '+' separated (default eng+hin)")
    p.add_argument("--jobs", type=int, default=1,
                   help="parallel OCR jobs (default 1 - safest for small "
                        "Railway containers; higher values need more RAM)")
    p.add_argument("--output-type", default="pdfa",
                   choices=["pdfa", "pdf"],
                   help="ocrmypdf output type (default pdfa; needs ghostscript)")
    p.add_argument("--deskew", action="store_true", default=True,
                   help="deskew pages before OCR (default on)")
    p.add_argument("--no-deskew", dest="deskew", action="store_false")
    p.add_argument("--clean", action="store_true", default=False,
                   help="remove scan artifacts with unpaper before OCR "
                        "(default off - unpaper is memory-heavy)")
    p.add_argument("--no-clean", dest="clean", action="store_false")
    p.add_argument("--fallback-dpi", type=int, default=200,
                   help="render DPI for the pytesseract fallback (default 200)")
    p.add_argument("--skip-ocr", action="store_true",
                   help="stop after step 1 (no OCR)")
    p.add_argument("--keep-intermediate", action="store_true", default=True,
                   help="keep step1_no_watermark.pdf (default)")
    p.add_argument("--remove-intermediate", dest="keep_intermediate",
                   action="store_false",
                   help="delete step1_no_watermark.pdf on success")
    p.add_argument("--keep-tmp", action="store_true", default=False,
                   help="keep ocrmypdf's temporary files")
    p.add_argument("--samples", default="1,50,100,200,300",
                   help="comma separated pages to verify "
                        "(default 1,50,100,200,300)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    t_start = time.time()

    global fitz
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz as fitz
        except ImportError:
            print("error: PyMuPDF not installed. Run: pip install pymupdf",
                  file=sys.stderr)
            return 3

    inp = Path(args.input).expanduser().resolve()
    if not inp.exists():
        print(f"error: input PDF not found: {inp}", file=sys.stderr)
        return 3
    if args.output:
        out = Path(args.output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        out = inp.with_name(inp.stem + "_CLEAN.pdf")
    if args.intermediate:
        step1 = Path(args.intermediate).expanduser().resolve()
        step1.parent.mkdir(parents=True, exist_ok=True)
    else:
        step1 = inp.parent / "step1_no_watermark.pdf"

    print("=" * 78)
    print("FIX_PDF  -  watermark removal + OCR text layer rebuild")
    print(f"input   : {inp}")
    print(f"output  : {out}")
    print("=" * 78)

    size_before = inp.stat().st_size

    # --------------------------------------------------------------- step 1
    try:
        stats, wm_xrefs = remove_watermark(inp, step1, args)
    except Exception as exc:
        print(f"error in step 1: {exc}", file=sys.stderr)
        return 3

    ok1 = True
    with fitz.open(step1) as chk:
        ok1 = verify_step1(chk, wm_xrefs, "step1")
    if not ok1:
        print("[warn] step1 verification found residue; continuing with OCR "
              "(the OCR output is checked again at the end)", file=sys.stderr)

    if args.skip_ocr:
        size_mid = step1.stat().st_size
        print("=" * 78)
        print(f"SUMMARY (skip-ocr): input {size_before / 1e6:.1f} MB -> "
              f"step1 {size_mid / 1e6:.1f} MB; OCR skipped")
        print(f"  intermediate : {step1}")
        print("=" * 78)
        return 0

    # --------------------------------------------------------------- step 2
    language = args.language
    ocr_engine = "none"
    ocr_ok = False
    try:
        language, ocr_ok = run_ocrmypdf(step1, out, args)
    except Exception as exc:
        print(f"[step2] error running ocrmypdf: {exc}", file=sys.stderr)
    if ocr_ok:
        ocr_engine = "ocrmypdf"
    else:
        print("[step2] falling back to pytesseract + reportlab ...")
        ocr_ok = run_fallback_ocr(step1, out, args, language or args.language)
        if ocr_ok:
            ocr_engine = "pytesseract+reportlab"

    if not ocr_ok or not out.exists():
        print(f"error: OCR did not produce {out}; step1 kept at {step1}",
              file=sys.stderr)
        return 2

    # --------------------------------------------------------------- step 3
    size_after = out.stat().st_size
    sample_pages = []
    for s in args.samples.split(","):
        s = s.strip()
        if s.isdigit():
            sample_pages.append(int(s))
    res = verify_output(out, sample_pages, language, "final")

    if not args.keep_intermediate:
        step1.unlink(missing_ok=True)

    elapsed = time.time() - t_start
    print("=" * 78)
    print("SUMMARY")
    print(f"  total pages             : {res['pages']}")
    print(f"  OCR engine / language   : {ocr_engine} / {res['language']}")
    print(f"  file size before/after  : {size_before / 1e6:.1f} MB -> "
          f"{size_after / 1e6:.1f} MB")
    print(f"  watermark strings found : {res['full_watermark_hits']}  "
          f"(target 0)")
    print(f"  bad sample pages        : {res['bad_samples']}  (target 0)")
    print(f"  elapsed                 : {elapsed:.0f}s")
    print(f"  output                  : {out}")
    print(f"  intermediate            : "
          f"{'removed' if not args.keep_intermediate else step1}")
    verdict = "PASS" if (res["ok"] and res["full_watermark_hits"] == 0) else "FAIL"
    print(f"  VERDICT                 : {verdict}")
    print("=" * 78)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
