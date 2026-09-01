#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_pdf.py
==========
Pre-process a garbled medical MCQ PDF (MARROW ED8 series) before the extraction
pipeline runs.

One problem is fixed by default (OCR is an optional extra):

  STEP 1  Remove ANY repeated watermark from a PDF with PyMuPDF (generic -
          works for any book, not just the itachibot series):
            * full-page image XObject families (several pixel variants of the
              same watermark - e.g. 82% + 18% - are one family);
            * same-position images repeated on >=90% of pages covering >=25%
              of the page area (banners);
            * full-page Form XObjects repeated on >=90% of pages (vector text
              / painted-band watermarks);
            * inline images (BI...ID...EI) repeated on pages;
            * known watermark text needles ("Sold by", "itachibot",
              "you purchased", "not for distribution", ...) and any text span
              repeated on >=90% of pages that is rotated (diagonal), large or
              known - removed from content streams and redacted visually.
          Legitimate content is protected: small corner logos, one-off
          figures, page numbers and header/footer furniture stay untouched.
          Intermediate output: step1_no_watermark.pdf

  STEP 2  OPTIONAL - rebuild a searchable text layer with OCRmyPDF
          (--force-ocr).  OFF by default: OCR re-renders every page at high
          DPI, so the output becomes many times larger than the upload, and
          on low-DPI scans (e.g. iLovePDF-compressed files at ~55 DPI) the
          recognized words are often wrong.  If OCR is disabled, the final
          PDF is exactly the watermark-free original (visuals + size like
          the upload).  Enable with --ocr (or the dashboard checkbox).

  STEP 3  Verify the output: pdftotext sample pages + watermark strings scan;
          in OCR mode readability is checked too.  A summary is printed.

Usage
-----
    python3 fix_pdf.py /data/input_pdfs/OPH.pdf          # watermark removal only
    python3 fix_pdf.py OPH.pdf --ocr                     # add OCR text layer
    python3 fix_pdf.py OPH.pdf --ocr --language eng      # Hindi pack missing
    python3 fix_pdf.py OPH.pdf --skip-ocr                # same as default

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
import hashlib
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
# Strings that are watermarks in this series (and can appear on any book).
# Matched case-insensitively against every text payload (content streams AND
# the extracted text layer), so old "Sold by@itachibot" and new
# "you purchased ..." watermarks are both caught.
DEFAULT_WATERMARK_NEEDLES = [
    "itachibot",
    "sold by",
    "you purchased",
    "you may not",
    "purchased this",
    "not for distribution",
    "not for sale",
    "do not share",
    "do not distribute",
    "do not copy",
    "may not be copied",
    "may not be shared",
    "may not be reproduced",
    "illegal copy",
]
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

# --- generic watermark detection thresholds ------------------------------
# (a) image family: all groups with median min(w,h) coverage >= this whose
#     union covers >= this share of all pages are ONE watermark family
WATERMARK_COVER_MIN = 0.80
WATERMARK_FREQ_MIN = 0.90
# groups seen on fewer pages than this are one-off full-page figures
# (never part of the watermark family - protects atlas/plate pages)
MIN_FULLPAGE_PAGES = 3
# (b) same-position rule: an image repeated on >=90% of pages at the SAME
#     rect is a watermark when it covers >= this share of the page area
#     (small corner logos stay untouched)
SAME_POS_MIN_PAGES = 0.90
SAME_POS_MIN_AREA = 0.25
SAME_POS_TOL = 0.02          # normalized rect tolerance
# (c) text overlays: a text span repeated on >=90% of pages is a watermark
#     when it is rotated (diagonal), covers >= this share of the page, or is a
#     known watermark string.  Header/footer line-items (top/bottom margins)
#     are excluded unless they are known watermark strings.
TEXT_MIN_PAGES = 0.90
TEXT_MIN_AREA = 0.02
TEXT_MARGIN = 0.08           # top/bottom 8% is "page furniture" zone

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


def tokenize_spans(data: bytes) -> tuple[list[tuple], list[tuple]]:
    """
    Like tokenize() but also returns inline-image spans.

    Returns (tokens, inline_spans) where each inline span is
    (start, end, payload_hash, width, height).  The content bytes
    data[start:end] hold 'BI ... ID <payload> EI'; end points just after
    'EI', so splicing data[:start] + data[end:] removes the image cleanly.
    """
    tokens, spans = [], []
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
            _, i = _parse_literal_string(data, i)
            tokens.append(("str", b"", False))
            continue
        if c == 0x3C:  # '<'
            if i + 1 < n and data[i + 1] == 0x3C:
                tokens.append(("delim", b"<<"))
                i += 2
                continue
            _, i = _parse_hex_string(data, i)
            tokens.append(("str", b"", True))
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
            bi_start = i
            tokens.append(("word", b"BI"))
            i = j
            continue
        if word == b"ID" and inline:
            # payload runs until whitespace-preceded 'EI'
            m = _INLINE_EI_RE.search(data, j)
            if m is None:
                m = _INLINE_EI_RE2.search(data, j)
            if m is None:
                raise ContentParseError("inline image without closing EI")
            dict_raw = data[bi_start:j]
            mw = re.search(rb"/W\s+(\d+)", dict_raw)
            mh = re.search(rb"/H\s+(\d+)", dict_raw)
            width = int(mw.group(1)) if mw else 0
            height = int(mh.group(1)) if mh else 0
            payload = data[j + 2 : m.start()]
            spans.append((bi_start, m.start() + 2, payload, width, height))
            i = m.end()
            inline = False
            tokens.append(("word", b"EI"))
            continue
        tokens.append(("num", word) if _NUM_RE.match(word) else ("word", word))
        i = j
    return tokens, spans


def scan_inline_images(data: bytes) -> list[tuple]:
    """Return inline-image spans: (start, end, sha256, width, height)."""
    import hashlib
    _, spans = tokenize_spans(bytes(data))
    out = []
    for start, end, payload, w, h in spans:
        out.append((start, end,
                    hashlib.sha256(payload).hexdigest(), w, h))
    return out


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


def _norm_text(s: str) -> str:
    """Normalize extracted text for needle matching."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _needles_hit(text: str, needles: set[str]) -> bool:
    t = _norm_text(text)
    return any(n in t for n in needles)


def _string_matches_watermark(raw: bytes, needles: set[str]) -> bool:
    """True if a content-stream string looks like watermark text."""
    low = raw.lower()
    if any(n.encode("latin-1", "ignore") in low for n in needles):
        return True
    # Unicode payloads (UTF-16BE etc.) get one extra chance
    for enc in ("utf-16-be", "utf-8", "utf-16"):
        try:
            s = raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
        if _needles_hit(s, needles):
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
        self.dropped_forms = 0
        self.inline_images_removed = 0
        self.forms_patched = 0
        self.neutralized_images = 0
        self.redaction_fallbacks = 0
        self.redacted_spans = 0
        self.unparsed_streams = 0

    def summary(self) -> str:
        return (
            f"removed {self.dropped_dos} watermark draws, "
            f"{self.dropped_forms} watermark form calls, "
            f"{self.inline_images_removed} inline image(s), "
            f"{self.dropped_text_ops} watermark text ops, "
            f"neutralized {self.neutralized_images} image object(s), "
            f"{self.redacted_spans} redacted span(s), "
            f"{self.unparsed_streams} stream(s) skipped"
        )


def strip_inline_images(data: bytes, inline_digests: set[str],
                        stats: PatchStats) -> bytes:
    """Splice candidate watermark inline images (BI...ID...EI) out of a
    content stream.  Returns the new stream."""
    if not data or not inline_digests:
        return data
    try:
        spans = scan_inline_images(data)
    except ContentParseError:
        stats.unparsed_streams += 1
        return data
    remove = [s for s in spans if s[2] in inline_digests]
    if not remove:
        return data
    out = bytearray()
    prev = 0
    for start, end, _, _, _ in sorted(remove):
        out += data[prev:start]
        prev = end
    out += data[prev:]
    stats.inline_images_removed += len(remove)
    return bytes(out)


def patch_data(
    doc,
    data: bytes,
    xo_map: dict[bytes, int],
    watermark_xrefs: set[int],
    watermark_forms: set[int],
    text_needles: set[str],
    stats: PatchStats,
    forms_done: set[int],
) -> bytes:
    """
    Remove operations from a content stream:
      * 'Do' ops invoking a watermark image XObject;
      * 'Do' ops invoking a watermark Form XObject; other forms are recursed
        into (they may draw the watermark image);
      * text ops ('Tj', 'TJ', quote, doublequote) whose payload contains a
        watermark needle.

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
                if xref in watermark_xrefs or xref in watermark_forms:
                    remove_ops.add(idx)
                    stats.dropped_dos += 1
                    if xref in watermark_forms:
                        stats.dropped_forms += 1
                elif xobject_subtype(doc, xref) == FORM_SUBTYPE:
                    forms_to_patch.add(xref)
        elif opname in (b"Tj", b"'"):
            if operands and operands[-1][0] == "str":
                if _string_matches_watermark(operands[-1][1], text_needles):
                    remove_ops.add(idx)
                    stats.dropped_text_ops += 1
        elif opname == b"TJ":
            for tok in reversed(operands):
                if tok[0] == "group":
                    for sub in tok[1]:
                        if sub[0] == "str" and _string_matches_watermark(
                                sub[1], text_needles):
                            remove_ops.add(idx)
                            stats.dropped_text_ops += 1
                    break
        elif opname == b'"':
            if operands and operands[-1][0] == "str":
                if _string_matches_watermark(operands[-1][1], text_needles):
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
        new, ok = patch_data(doc, old, fx_map, watermark_xrefs,
                             watermark_forms, text_needles, stats, forms_done)
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


def patch_page(doc, page, watermark_xrefs: set[int], watermark_forms: set[int],
               inline_digests: set[str], text_needles: set[str],
               stats: PatchStats, forms_done: set[int]) -> bool:
    """
    Patch one page's /Contents (and any forms it uses).

    Returns True when the page was parsed completely (i.e. we KNOW no
    watermark draw remains), False when a stream could not be parsed and the
    page may still draw the watermark (caller must then neutralize the object).
    """
    data = page.read_contents()
    if not data:
        return True
    data = strip_inline_images(data, inline_digests, stats)
    xo_map = get_xobject_map(doc, page.xref)
    new, ok = patch_data(doc, data, xo_map, watermark_xrefs, watermark_forms,
                         text_needles, stats, forms_done)
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


def redact_watermark_text(doc, page, stats: PatchStats,
                          needles: set[str]) -> int:
    """
    Fallback for encodings the byte-level scan cannot decode (and for streams
    that MuPDF can still extract text from but rawdict cannot): find the
    watermark strings (known needles + spans detected on >=90% of pages) with
    MuPDF's own text search and redact exactly those rectangles (text only -
    images and line art are preserved).
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
                    span_text = "".join(
                        ch.get("c", "") for ch in span.get("chars", []))
                    if _needles_hit(span_text, needles):
                        rects.append(fitz.Rect(span["bbox"]))
    except Exception:
        rects = []
    # 2) MuPDF text search - works even when rawdict spans are empty.
    # Longest needle first, so "Sold by@itachibot" wins over its substrings
    # and the "@" between them is covered (no visual leftovers).
    if not rects:
        try:
            for needle in sorted(needles, key=len, reverse=True):
                if len(needle) < 3:
                    continue
                try:
                    for r in page.search_for(needle):
                        r = fitz.Rect(r)
                        if not any(r.intersects(x) for x in rects):
                            rects.append(r)
                except Exception:
                    continue
        except Exception:
            pass
    # 3) last resort: word boxes containing a needle
    if not rects:
        try:
            for w in page.get_text("words"):
                if _needles_hit(w[4], needles):
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
            stats.redacted_spans += len(rects)
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
        self.areas: list[float] = []       # bbox area / page area
        self.rects: list[tuple] = []       # normalized bbox per page
        self.width = width
        self.height = height

    @property
    def frequency(self) -> float:
        return len(self.pages)

    def area_of(self) -> float:
        return statistics.median(self.areas) if self.areas else 0.0

    def same_rect(self) -> bool:
        """True when the image sits at the same rect on every page."""
        if len(self.rects) < 2:
            return True
        base = self.rects[0]
        return all(
            abs(r[k] - base[k]) <= SAME_POS_TOL for r in self.rects
            for k in range(4)
        )


class TextGroup:
    """A text span repeated across pages (candidate watermark overlay)."""

    def __init__(self, needle: str, rect: tuple, rotated: bool,
                 area: float, page_no: int) -> None:
        self.needle = needle               # normalized string
        self.rect = rect                   # normalized (x,y,w,h)
        self.rotated = rotated
        self.area = area
        self.pages: set[int] = {page_no}

    @property
    def frequency(self) -> float:
        return len(self.pages)


class WatermarkAnalysis:
    def __init__(self) -> None:
        self.groups: list[WatermarkGroup] = []       # image digest groups
        self.inline_groups: list[WatermarkGroup] = []  # inline-image groups
        self.form_groups: list[WatermarkGroup] = []    # Form-XObject groups
        self.text_groups: list[TextGroup] = []
        self.total_pages = 0
        self.candidates: list[int] = []              # image xrefs to remove
        self.candidate_groups: list[WatermarkGroup] = []
        self.inline_candidates: set[str] = set()     # inline payload digests
        self.inline_candidate_groups: list[WatermarkGroup] = []
        self.form_candidates: set[int] = set()       # Form xrefs to remove
        self.form_candidate_groups: list[WatermarkGroup] = []
        self.text_needles: set[str] = set()          # strings to remove/redact
        self.text_detected: set[str] = set()         # strings actually found
        self.text_candidate_groups: list[TextGroup] = []


def _group_add(groups: dict, key, group, page_no: int, ratio: float,
               area: float, rect: tuple) -> None:
    g = groups.get(key)
    if g is None:
        groups[key] = group
    else:
        g.xrefs |= group.xrefs
    groups[key].pages.add(page_no)
    groups[key].cover.append(ratio)
    groups[key].areas.append(area)
    groups[key].rects.append(rect)


def analyze_watermarks(doc, pages) -> WatermarkAnalysis:
    """
    Generic watermark detection - works for ANY repeated watermark, not just
    the itachibot series:

    1. IMAGE FAMILY   - XObject images are grouped by pixel digest.  All
       groups with median min(w,h) coverage >= 80% whose UNION covers >=90%
       of pages are one watermark family (the same watermark is often stored
       as several pixel variants: 82% + 18% etc.).  One-off full-page figures
       (a few pages) are protected.
    2. SAME POSITION  - an image repeated on >=90% of pages at the SAME rect
       covering >=25% of the page area is a watermark banner (small corner
       logos stay untouched).
    3. FORM OBJECTS   - Form XObjects with a full-page BBox repeated on >=90%
       of pages are watermarks (their content stream digest groups variants).
    4. INLINE IMAGES  - BI...ID...EI images in the content stream, grouped by
       payload digest, follow the same full-page family rule.
    5. TEXT OVERLAY   - a text span (or known watermark string) repeated on
       >=90% of pages, when rotated (diagonal), large, or a known watermark
       string, is a watermark overlay.  Page-number/header/footer furniture in
       the top/bottom margins is excluded.
    """
    an = WatermarkAnalysis()
    an.total_pages = len(pages)
    groups_by_digest: dict[bytes, WatermarkGroup] = {}
    inline_by_digest: dict[bytes, WatermarkGroup] = {}
    form_by_digest: dict[bytes, WatermarkGroup] = {}
    text_by_key: dict[tuple, TextGroup] = {}

    for pno, page in enumerate(pages):
        prect = page.rect
        if prect.is_empty or prect.is_infinite:
            continue
        pw = max(prect.width, 1e-9)
        ph = max(prect.height, 1e-9)

        # ---- XObject images --------------------------------------------
        try:
            infos = page.get_image_info(xrefs=True, hashes=True)
        except Exception:
            infos = []
        if not infos:
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
            wr = min(1.0, rect.width / pw)
            hr = min(1.0, rect.height / ph)
            ratio = min(wr, hr)
            key = digest or f"xr:{xref}".encode()
            g = WatermarkGroup(key, xref, it.get("width", 0) or 0,
                               it.get("height", 0) or 0)
            _group_add(groups_by_digest, key, g, pno, ratio,
                       (rect.width * rect.height) / (pw * ph),
                       (rect.x0 / pw, rect.y0 / ph, rect.width / pw,
                        rect.height / ph))

        # ---- Form XObjects (digest of the form stream) ------------------
        xo_map = get_xobject_map(doc, page.xref)
        for name, fx in xo_map.items():
            try:
                if xobject_subtype(doc, fx) != FORM_SUBTYPE:
                    continue
                stream = doc.xref_stream(fx)
                fdigest = __import__("hashlib").sha256(stream).digest()
                fkey = b"form:" + fdigest
                bbox_v = doc.xref_get_key(fx, "BBox")[1]
                bbox = _parse_bbox(bbox_v)
                if bbox is None or bbox.is_empty or bbox.is_infinite:
                    continue
                wr = min(1.0, bbox.width / pw)
                hr = min(1.0, bbox.height / ph)
                ratio = min(wr, hr)
                fg = WatermarkGroup(fkey, fx, int(bbox.width),
                                    int(bbox.height))
                _group_add(form_by_digest, fkey, fg, pno, ratio,
                           (bbox.width * bbox.height) / (pw * ph),
                           (bbox.x0 / pw, bbox.y0 / ph, bbox.width / pw,
                            bbox.height / ph))
            except Exception:
                continue

        # ---- inline images ----------------------------------------------
        try:
            data = page.read_contents()
        except Exception:
            data = b""
        if data:
            try:
                for start, end, digest, w, h in scan_inline_images(data):
                    key = digest.encode()
                    ig = WatermarkGroup(key, -1, w, h)
                    # bbox from CTM is not tracked here; use unit square
                    # (conservative: family rule needs >=80% cover anyway)
                    _group_add(inline_by_digest, key, ig, pno, 1.0, 1.0,
                               (0.0, 0.0, 1.0, 1.0))
            except ContentParseError:
                pass

        # ---- text spans --------------------------------------------------
        try:
            raw = page.get_text("rawdict")
        except Exception:
            raw = {}
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                d = line.get("dir") or (1.0, 0.0)
                try:
                    rotated = abs(float(d[1])) > 0.05
                except (TypeError, ValueError, IndexError):
                    rotated = False
                for span in line.get("spans", []):
                    # NOTE: rawdict spans store text as chars[]["c"], not
                    # span["text"] (dict-mode has span["text"], rawdict does not)
                    span_text = "".join(
                        ch.get("c", "") for ch in span.get("chars", []))
                    txt = _norm_text(span_text)
                    if not txt:
                        continue
                    bbox = fitz.Rect(span.get("bbox", (0, 0, 0, 0)))
                    area = (bbox.width * bbox.height) / (pw * ph)
                    q = (round(bbox.x0 / pw * 100),
                         round(bbox.y0 / ph * 100),
                         round(bbox.width / pw * 100),
                         round(bbox.height / ph * 100))
                    key = (txt, bool(rotated), q)
                    tg = text_by_key.get(key)
                    if tg is None:
                        text_by_key[key] = TextGroup(
                            txt, (bbox.x0 / pw, bbox.y0 / ph,
                                  bbox.width / pw, bbox.height / ph),
                            bool(rotated), area, pno)
                    else:
                        tg.pages.add(pno)
                        tg.area = max(tg.area, area)
                        tg.rotated = tg.rotated or bool(rotated)
        if (pno + 1) % PROGRESS_EVERY == 0:
            print(f"[step1] analyzed page {pno + 1}/{len(pages)} "
                  f"(images, forms, inline images, text overlays)")

    def cover_of(g: WatermarkGroup) -> float:
        return statistics.median(g.cover) if g.cover else 0.0

    # ---- image family (multi-variant, full-page) ------------------------
    an.groups = list(groups_by_digest.values())
    an.groups.sort(key=lambda g: -len(g.pages))
    full_page = [g for g in an.groups
                 if cover_of(g) >= WATERMARK_COVER_MIN
                 and len(g.pages) >= MIN_FULLPAGE_PAGES]
    union_pages: set[int] = set()
    for g in full_page:
        union_pages |= g.pages
    union_freq = len(union_pages) / max(1, an.total_pages)
    if union_freq >= WATERMARK_FREQ_MIN:
        an.candidate_groups = full_page
        if len(full_page) > 1:
            print(f"[step1] watermark FAMILY: {len(full_page)} full-page image "
                  f"variant(s) together cover {len(union_pages)}/"
                  f"{an.total_pages} pages ({union_freq:.0%}) - treating all "
                  f"as one watermark")
    else:
        an.candidate_groups = [
            g for g in an.groups
            if len(g.pages) / max(1, an.total_pages) >= WATERMARK_FREQ_MIN
            and cover_of(g) >= WATERMARK_COVER_MIN
        ]
    # same-position banners (e.g. a dim grey band at the page centre)
    for g in an.groups:
        if g in an.candidate_groups:
            continue
        if (len(g.pages) / max(1, an.total_pages) >= SAME_POS_MIN_PAGES
                and g.area_of() >= SAME_POS_MIN_AREA and g.same_rect()):
            an.candidate_groups.append(g)
            print(f"[step1] watermark same-position image: "
                  f"{g.digest.hex()[:10] if g.digest else 'xref ' + str(min(g.xrefs))} "
                  f"on {len(g.pages)}/{an.total_pages} pages, "
                  f"{g.area_of():.0%} page area at one fixed rect")
    for g in an.candidate_groups:
        an.candidates.extend(sorted(g.xrefs))
    an.candidates = sorted(set(an.candidates))

    # ---- forms -----------------------------------------------------------
    an.form_groups = list(form_by_digest.values())
    an.form_groups.sort(key=lambda g: -len(g.pages))
    an.form_candidate_groups = [
        g for g in an.form_groups
        if cover_of(g) >= WATERMARK_COVER_MIN
        and len(g.pages) / max(1, an.total_pages) >= WATERMARK_FREQ_MIN
    ]
    for g in an.form_candidate_groups:
        an.form_candidates |= g.xrefs

    # ---- inline images ---------------------------------------------------
    an.inline_groups = list(inline_by_digest.values())
    an.inline_groups.sort(key=lambda g: -len(g.pages))
    an.inline_candidate_groups = [
        g for g in an.inline_groups
        if cover_of(g) >= WATERMARK_COVER_MIN
        and (len(g.pages) / max(1, an.total_pages) >= WATERMARK_FREQ_MIN
             or len(g.pages) >= MIN_FULLPAGE_PAGES)
        and len(g.pages) >= MIN_FULLPAGE_PAGES
    ]
    if an.inline_candidate_groups:
        an.inline_candidates = {g.digest.decode() for g in an.inline_candidate_groups}

    # ---- text overlays ---------------------------------------------------
    an.text_groups = list(text_by_key.values())
    an.text_groups.sort(key=lambda g: -len(g.pages))
    known = set(DEFAULT_WATERMARK_NEEDLES)
    for tg in an.text_groups:
        frac = len(tg.pages) / max(1, an.total_pages)
        known_hit = any(n in tg.needle for n in known)
        if known_hit:
            # a known watermark string found anywhere (any page) is treated
            # as a watermark overlay - most of this series' PDFs draw the
            # same string on (nearly) every page
            an.text_detected |= {n for n in known if n in tg.needle}
        if frac < TEXT_MIN_PAGES and not known_hit:
            continue
        # 8% top/bottom margin = page furniture (page numbers, headers)
        margin_hit = ((tg.rect[1] + tg.rect[3]) <= TEXT_MARGIN
                      or tg.rect[1] >= 1 - TEXT_MARGIN) and tg.area < TEXT_MIN_AREA
        if margin_hit and not known_hit:
            continue
        if known_hit or tg.rotated or tg.area >= TEXT_MIN_AREA:
            an.text_candidate_groups.append(tg)
            an.text_needles.add(tg.needle)
            an.text_detected.add(tg.needle)
    if an.text_candidate_groups:
        print(f"[step1] watermark TEXT overlay(s): {len(an.text_candidate_groups)} "
              f"repeated span(s) on {an.total_pages}-page book, e.g. "
              f"{an.text_candidate_groups[0].needle!r}")
    # active needle set: detected strings + known defaults (bytes-level scan)
    an.text_needles |= {n for n in known if n and len(n) >= 3}

    # union of page coverage across ALL detection channels (report only)
    cov = set(union_pages)
    for g in an.form_candidate_groups:
        cov |= g.pages
    for g in an.inline_candidate_groups:
        cov |= g.pages
    for tg in an.text_candidate_groups:
        cov |= tg.pages
    if cov:
        print(f"[step1] watermark coverage (all channels): {len(cov)}/"
              f"{an.total_pages} pages ({len(cov) / max(1, an.total_pages):.0%})")
    return an


def _parse_bbox(value: str):
    """Parse a PDF /BBox value ('[0 0 612 792]' or an indirect ref)."""
    try:
        import ast
        vals = re.findall(r"-?[\d.]+", value or "")
        if len(vals) == 4:
            return fitz.Rect(float(vals[0]), float(vals[1]),
                             float(vals[2]), float(vals[3]))
    except Exception:
        return None
    return None


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
    if an.form_candidate_groups:
        print(f"[step1] watermark FORM XObject(s): "
              f"{sorted(an.form_candidates)} on "
              f"{len(an.form_candidate_groups[0].pages)} pages")
    if an.inline_candidate_groups:
        print(f"[step1] watermark INLINE image(s): "
              f"{len(an.inline_candidate_groups)} digest group(s)")

    nothing_found = not an.candidates and not an.form_candidates \
        and not an.inline_candidates and not an.text_detected
    if nothing_found:
        doc.close()
        raise RuntimeError(
            "no candidate watermark found "
            "(looked for: full-page image families, same-position repeated "
            "images, full-page Form XObjects, repeated inline images and "
            "repeated/known watermark text overlays). "
            "Inspect the report above; refusing to proceed so no figure is "
            "damaged."
        )
    if an.candidates:
        print(f"[step1] watermark XObject(s) detected: {an.candidates} "
              f"(content digest groups: {len(an.candidate_groups)})")
    if an.form_candidates:
        print(f"[step1] watermark Form(s) to remove: {sorted(an.form_candidates)}")
    if an.inline_candidates:
        print(f"[step1] watermark inline image digest(s): "
              f"{len(an.inline_candidates)}")
    if an.text_needles:
        shown = sorted(an.text_needles, key=len, reverse=True)[:6]
        print(f"[step1] watermark text needle(s): {shown}")

    stats = PatchStats()
    watermark_set = set(an.candidates)
    form_set = set(an.form_candidates)
    forms_done: set[int] = set()
    t0 = time.time()
    # "dirty" = we could not prove the watermark draw is gone (parse failure
    # on the page or on a Form it uses).  NOTE: we never trust get_image_rects
    # here - PyMuPDF caches per-page image info, so it keeps answering with
    # stale rects after an in-place content swap.
    dirty: list[int] = []
    for pno, page in enumerate(pages):
        try:
            ok = patch_page(doc, page, watermark_set, form_set,
                            an.inline_candidates, an.text_needles, stats,
                            forms_done)
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
        if text and _needles_hit(text, an.text_needles):
            hits = redact_watermark_text(doc, page, stats, an.text_needles)
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
    return stats, list(an.candidates), an


def verify_step1(doc, watermark_xrefs: list[int], label: str,
                 needles: set[str]) -> bool:
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
        text_hits += len(re.findall(
            "(" + "|".join(re.escape(x) for x in sorted(
                needles, key=len, reverse=True)) + ")",
            t, flags=re.IGNORECASE))
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


def verify_output(pdf_path: Path, sample_pages, language: str, tag: str,
                  check_quality: bool = True,
                  needles: set[str] | None = None) -> dict:
    """
    Verify an output PDF.

    check_quality=True  (OCR mode): sample pages must contain readable text.
    check_quality=False (no-OCR mode): the ORIGINAL text layer is kept, which
      may be garbled (broken ToUnicode CMaps) - do NOT judge readability, only
      confirm the watermark strings are gone.
    """
    needles = needles or set(DEFAULT_WATERMARK_NEEDLES)
    pat = re.compile("(" + "|".join(re.escape(x) for x in sorted(
        needles, key=len, reverse=True)) + ")", re.IGNORECASE)
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
        wm = len(pat.findall(text))
        watermark_hits += wm
        bad = (check_quality and (garb > 0.05 or read < 0.55 or words < 5))
        if bad:
            bad_samples += 1
        tag_flags = []
        if check_quality and bad:
            tag_flags.append("BAD")
        if wm:
            tag_flags.append("WATERMARK")
        print(f"  page {pno:>4}: readable {read:.0%}, garbage {garb:.2%}, "
              f"words {words}, watermark hits {wm}  "
              f"[{', '.join(tag_flags) if tag_flags else 'ok'}]")
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
    full_hits = len(pat.findall(full or ""))

    ok = (seen_any and watermark_hits == 0 and full_hits == 0
          and (bad_samples == 0 or not check_quality))
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
    p.add_argument("--ocr", action="store_true", default=False,
                   help="rebuild the text layer with OCR (DEFAULT is OFF: "
                        "watermark removal only, so the output keeps the "
                        "original page content, looks identical to the upload "
                        "and stays near the original size.  OCR is only useful "
                        "for high-resolution scans; on low-DPI files it "
                        "produces inaccurate text and a much larger file)")
    p.add_argument("--skip-ocr", action="store_true",
                   help="stop after step 1 (same as leaving --ocr off)")
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
    print("FIX_PDF  -  watermark removal (OCR text layer optional, off by default)")
    print(f"input   : {inp}")
    print(f"output  : {out}")
    print("=" * 78)

    size_before = inp.stat().st_size

    # --------------------------------------------------------------- step 1
    try:
        stats, wm_xrefs, an = remove_watermark(inp, step1, args)
    except Exception as exc:
        print(f"error in step 1: {exc}", file=sys.stderr)
        return 3

    ok1 = True
    with fitz.open(step1) as chk:
        ok1 = verify_step1(chk, wm_xrefs, "step1", an.text_needles)
    if not ok1:
        print("[warn] step1 verification found residue; see log", file=sys.stderr)

    sample_pages = []
    for s in args.samples.split(","):
        s = s.strip()
        if s.isdigit():
            sample_pages.append(int(s))

    run_ocr = args.ocr and not args.skip_ocr

    if not run_ocr:
        # DEFAULT: watermark removal only.  The original pages (images,
        # figures, layout) are kept exactly as in the upload - no re-render,
        # no OCR - so the output is visually identical and similar in size.
        print("[step2] OCR skipped (default). The output keeps the original "
              "page content; only the watermark was removed.")
        shutil.copyfile(step1, out)
        size_after = out.stat().st_size
        res = verify_output(out, sample_pages, "none (no OCR)", "final",
                            check_quality=False, needles=an.text_needles)
        elapsed = time.time() - t_start
        print("=" * 78)
        print("SUMMARY")
        print(f"  total pages             : {res['pages']}")
        print(f"  OCR                     : skipped (watermark removal only)")
        print(f"  file size before/after  : {size_before / 1e6:.1f} MB -> "
              f"{size_after / 1e6:.1f} MB  (upload-like size)")
        print(f"  watermark strings found : {res['full_watermark_hits']}  "
              f"(target 0)")
        print(f"  elapsed                 : {elapsed:.0f}s")
        print(f"  output                  : {out}")
        print(f"  intermediate            : {step1}")
        verdict = "PASS" if res["ok"] and res["full_watermark_hits"] == 0 else "FAIL"
        print(f"  VERDICT                 : {verdict}")
        print("=" * 78)
        print("Note: no searchable text layer was added (the original broken "
              "text layer is untouched - copy/search may still show garbage). "
              "Use --ocr if you really need OCR text; OCR re-renders pages at "
              "high DPI (much larger file) and is inaccurate on low-DPI scans.")
        return 0 if verdict == "PASS" else 1

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
    res = verify_output(out, sample_pages, language, "final",
                        check_quality=True, needles=an.text_needles)

    if not args.keep_intermediate:
        step1.unlink(missing_ok=True)

    elapsed = time.time() - t_start
    print("=" * 78)
    print("SUMMARY")
    print(f"  total pages             : {res['pages']}")
    print(f"  OCR engine / language   : {ocr_engine} / {res['language']}")
    print(f"  file size before/after  : {size_before / 1e6:.1f} MB -> "
          f"{size_after / 1e6:.1f} MB  (OCR re-renders pages, hence larger)")
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
