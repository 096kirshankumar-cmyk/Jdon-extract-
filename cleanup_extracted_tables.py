#!/usr/bin/env python3
"""One-time repair for tables leaked into solution.text and overlap duplicates.

Dry run: python3 cleanup_extracted_tables.py
Apply:   python3 cleanup_extracted_tables.py --apply

Scans output JSONL question files, makes timestamped .bak copies before writing,
and is idempotent.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("OUTPUT_DIR", "./qbank_output"))
PIPE = re.compile(r"^\s*\|.*\|.*\|\s*$")


def rows(table):
    out = []
    for line in str(table.get("markdown") or "").splitlines():
        line = re.sub(r"\s+", "", line).lower()
        if line and not re.fullmatch(r"\|?-{3,}(?:\|-{3,})+\|?", line):
            out.append(line)
    return out


def dedupe(tables):
    tables = sorted((t for t in tables if isinstance(t, dict)),
                    key=lambda t: (len(rows(t)), len(str(t.get("markdown") or ""))), reverse=True)
    kept = []
    for table in tables:
        key = re.sub(r"\s+", "", str(table.get("markdown") or "").lower())
        r = rows(table)
        if any((key and key == re.sub(r"\s+", "", str(k.get("markdown") or "").lower()))
               or (r and len(r) < len(rows(k)) and rows(k)[:len(r)] == r) for k in kept):
            continue
        kept.append(table)
    return kept


def move_inline(text):
    prose, tables, lines, i = [], [], (text or "").splitlines(), 0
    while i < len(lines):
        if not PIPE.match(lines[i]):
            prose.append(lines[i]); i += 1; continue
        j = i
        while j < len(lines) and PIPE.match(lines[j]): j += 1
        if j - i >= 3:
            tables.append({"type": "recovered inline table", "markdown": "\n".join(lines[i:j])})
        else:
            prose.extend(lines[i:j])
        i = j
    return "\n".join(prose).strip(), tables


def question_files():
    for p in ROOT.rglob("*.jsonl"):
        # Only process JSONL files whose rows use the final question schema.
        try:
            first = next((x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()), "")
            if first and "\"solution\"" in first and "\"id\"" in first:
                yield p
        except OSError:
            pass


def repair(path, apply):
    src = path.read_text(encoding="utf-8").splitlines()
    changed = moved = dropped = 0; out = []
    for line in src:
        row = json.loads(line)
        sol = row.get("solution") or {}
        text, recovered = move_inline(sol.get("text") or "")
        old_tables = sol.get("tables") or []
        tables = dedupe(old_tables + recovered)
        if text != (sol.get("text") or "") or tables != old_tables:
            sol["text"], sol["tables"] = text, tables
            row["solution"] = sol; changed += 1; moved += len(recovered)
            dropped += max(0, len(old_tables) + len(recovered) - len(tables))
        out.append(json.dumps(row, ensure_ascii=False))
    print(f"{path}: {changed} rows changed; {moved} inline table block(s) moved; {dropped} duplicate/partial table(s) removed")
    if changed and apply:
        backup = path.with_name(path.name + f".bak-table-cleanup-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_text("\n".join(src) + "\n", encoding="utf-8")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"  applied; backup: {backup}")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    print("APPLY mode" if apply else "DRY RUN — pass --apply to write changes")
    for f in question_files(): repair(f, apply)
