#!/usr/bin/env python3
"""Deterministic stem/option parse from a question crop's TEXT.

Gemini is last. CLEAN geometric/text parse wins. Never invents letters
from medical meaning. Output is a phase-item dict the engine already uses
— does not change questions.jsonl / zip schema.
"""
from __future__ import annotations

import re

_Q_HEAD = re.compile(
    r"(?im)^\s*(?:question\s+)?(\d{1,3})\s*[:.)\-–]\s*")
_NEXT_FURN = re.compile(
    r"(?im)^\s*(?:question\s+\d{1,3}\s*[:.)]|answer[\s-]*key|"
    r"detailed\s+explanations|solution\s+to\s+question)")
_OPT = re.compile(
    r"(?im)(?:^|\s)[\(（\[]?\s*([A-Da-d])\s*[\)\]\.、:：\-–>]\s+")


def clip_question_body(text, q_no):
    """Text after 'Question N:' until next printed header. Empty if missing."""
    t = text or ""
    hits = list(_Q_HEAD.finditer(t))
    start = None
    for m in hits:
        if int(m.group(1)) == int(q_no):
            start = m.end()
            break
    if start is None:
        return (t or "").strip()
    rest = t[start:]
    nxt = _NEXT_FURN.search(rest)
    if nxt:
        rest = rest[:nxt.start()]
    return rest.strip()


def parse_options(body):
    """-> ({A: text, ...}, leftover_stem, issue).

    Only lettered tokens (a)/A./(b) count. Unlettered tail stays in stem.
    """
    body = body or ""
    marks = list(_OPT.finditer(body))
    if not marks:
        return {}, body.strip(), "no option letters"
    stem = body[:marks[0].start()].strip()
    opts, issues = {}, []
    for i, m in enumerate(marks):
        letter = m.group(1).upper()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        chunk = body[m.end():end].strip()
        chunk = re.sub(r"\s+", " ", chunk).strip()
        if letter in opts and opts[letter] and chunk and opts[letter] != chunk:
            issues.append(f"duplicate option {letter}")
        if chunk:
            if letter not in opts or len(chunk) > len(opts[letter]):
                opts[letter] = chunk
    if len(opts) < 4:
        issues.append(f"only {len(opts)} lettered options")
    return opts, stem, "; ".join(issues)


def parse_question_text(text, q_no):
    """Phase-item or None. q_no is forced from the header index."""
    body = clip_question_body(text, q_no)
    if not body:
        return None
    opts, stem, issue = parse_options(body)
    if not stem and not opts:
        return None
    it = {
        "q_no": int(q_no),
        "_qn": int(q_no),
        "_header_n": int(q_no),
        "stem": stem,
        "options": opts,
        "has_figure": False,
        "figure_location": None,
        "text_confidence": "high" if len(opts) >= 4 and stem else "medium",
        "_method": "geometric_text",
    }
    if issue:
        it["_opt_issue"] = issue
        if len(opts) < 4:
            it["text_confidence"] = "low"
    return it


def parse_solution_text(text, q_no):
    """Body after Solution to Question N: until next such header."""
    t = text or ""
    pat = re.compile(
        rf"(?is)solution\s+to\s+question\s+{int(q_no)}\s*[:.\-–]?\s*")
    m = pat.search(t)
    if not m:
        body = t.strip()
    else:
        body = t[m.end():]
        nxt = re.search(r"(?i)solution\s+to\s+question\s+\d+", body)
        if nxt:
            body = body[:nxt.start()]
    body = body.strip()
    if not body:
        return None
    return {
        "q_no": int(q_no),
        "_qn": int(q_no),
        "_header_n": int(q_no),
        "solution_text": body,
        "has_figure": False,
        "text_confidence": "high" if len(body) > 40 else "medium",
        "_method": "geometric_text",
    }
