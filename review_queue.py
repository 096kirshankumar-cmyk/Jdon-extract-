#!/usr/bin/env python3
"""review_queue.py — the manual-review engine for the QBank pipeline.

DESIGN CONTRACT (user-signed-off, 2026-08-19)
===========================================
1. PIPELINE FREEZES CONTENT after extraction. Only a human edits, via here.
2. The review queue is the UNION of EVERY known flag source — never just the
   digest, never just completeness. A flag written to any file MUST surface.
3. Catch-all: validator-reported flags per chapter are unioned in too, so a
   kind nobody mapped still shows up. Unknown flag *.jsonl files raise a
   watchdog warning row instead of being silently skipped.
4. Every decision (edit/approve/ignore) is persisted to disk in append-only
   ledgers IMMEDIATELY. The browser holds no state; refresh/redeploy/resume
   from any device returns the same queue. If the underlying row content
   changed since the decision (pipeline re-ran the chapter), the decision is
   marked STALE and the flag returns to the queue — safe direction only.
5. Edits update EVERY copy of the field atomically (master, subjects bundle,
   chapter file, split rows, image manifest) and then are READ BACK from disk
   and verified. The screen only says "saved" when the disk says so.
6. The final zip is GATED: it refuses to build while any queue row is open.
7. Images: never deleted by UI (detach only unlinks). Owner moves rename the
   file to the new slot and append a provenance row to image_ownership.jsonl
   with method="human_edit" so the gate chain never breaks. A multi-draw
   shared file cannot be blindly moved (would break the other owner) — the op
   refuses with a clear message.
8. This module NEVER calls Gemini. Zero tokens, offline, idempotent.
"""

import hashlib
import json
import re
import time
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. FLAG SOURCE REGISTRY — every known place a problem can be recorded.
#    If a new flag file is ever added to the pipeline and not registered here,
#    WATCHDOG (below) raises it instead of letting it slip.
# ---------------------------------------------------------------------------

FLAG_SOURCES = {
    # filename -> how to read it
    "export_gate.jsonl": "rows: chapter_id/q_no/kind/detail (gate violations)",
    "export_gate_advisory.jsonl": "rows: advisory gate notes",
    "integrity_flags.jsonl": "rows: answer-key disagreements etc.",
    "orphans.jsonl": "rows: unclaimed fragments (chapter_id, batch_start, ...)",
    "still_incomplete_after_retry.jsonl": "rows: gaps retry could not close",
    "stem_conflicts.jsonl": "rows: stem text conflicts",
    "unmatched_images.jsonl": "rows: extracted images with no owner",
    "unresolved_images.jsonl": "rows: figures unresolved after all levels",
}

# data/ jsonl files that are NOT flags (core state; expected)
CORE_DATA_FILES = {
    "questions.jsonl", "image_ownership.jsonl", "page_ledger.jsonl",
    "integrity_flags.jsonl", "chapters.json", "export_gate.jsonl",
    "export_gate_advisory.jsonl", "orphans.jsonl",
    "still_incomplete_after_retry.jsonl", "stem_conflicts.jsonl",
    "unmatched_images.jsonl", "human_edit_ledger.jsonl",
    "review_decisions.jsonl",
} | set(FLAG_SOURCES)

BLOCKER_KINDS = {
    "answer_key_disagrees",     # printed key contradicts extracted answer
    "unresolved_qid",           # record could not be anchored
    "incomplete_records",       # completeness.json says records missing
}

HUMAN_EDIT_LEDGER = "human_edit_ledger.jsonl"
REVIEW_DECISIONS = "review_decisions.jsonl"


# ---------------------------------------------------------------------------
# 5.5. Review-screen helpers -- readable content, page context, HTML tables.
# Pure functions; the screen renders what THIS returns, never raw internals.
# ---------------------------------------------------------------------------

_IMG_NAME_ANY = re.compile(
    r"([A-Za-z0-9]+/[A-Za-z0-9]+(?:-\d{3})?-p\d+[-A-Za-z0-9@.]*\.webp"  # temp/crop names
    r"|[A-Za-z0-9]+/[A-Za-z0-9]+-\d{3}-\d{3}_[A-Z0-9_]+_\d{2}\.webp)")   # final slots


def md_to_html(md):
    """Pipe-markdown -> escaped HTML table for the review screen. Returns ''
    if it doesn't look like one table. Display-only; the stored text is never
    modified."""
    import html as _html
    lines = [l.strip() for l in str(md or "").splitlines()
             if l.strip().startswith("|")]
    if len(lines) < 2:
        return ""
    def cells(l):
        return [c.strip() for c in l.strip().strip("|").split("|")]
    head = cells(lines[0])
    body = [cells(l) for l in lines[2:]]   # line 2 = separator
    out = ['<table style="border-collapse:collapse;font-size:11px;'
           'background:#fff">']
    out.append("<tr>" + "".join(
        f'<th style="border:1px solid #cbd5e1;padding:2px 6px;text-align:left">'
        f"{_html.escape(c)}</th>" for c in head) + "</tr>")
    for r in body:
        out.append("<tr>" + "".join(
            f'<td style="border:1px solid #e2e8f0;padding:2px 6px">'
            f"{_html.escape(c)}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


def orphan_readable(row):
    """Make a data/orphans.jsonl row readable: parse the embedded Gemini item
    and say WHAT it is (solution fragment / question shell / table) + pages."""
    item = row.get("item")
    if isinstance(item, str):
        try:
            item = json.loads(item.replace("'", '"'))
        except Exception:
            item = None
    parts = []
    pages = row.get("new_pages") or row.get("pdf_pages") or []
    if isinstance(item, dict):
        if item.get("solution_text"):
            parts.append("SOLUTION fragment: " + str(item["solution_text"]))
        if item.get("question_text"):
            parts.append("STEM fragment: " + str(item["question_text"]))
        if item.get("tables"):
            parts.append(f"{len(item['tables'])} table(s): "
                         + ", ".join(str(t.get("type", "?")) for t in item["tables"]))
        if item.get("options"):
            parts.append("options: " + str(item["options"]))
    why = row.get("blocked_reason") or row.get("drop_reason")
    if why:
        parts.append(f"kyun hold hua: {why}")
    if row.get("inferred_owner"):
        parts.append(f"system ka best guess: q{row['inferred_owner']} "
                     f"({str(row.get('inferred_reason'))[:80]})")
    return {"pages": pages, "text": "\n".join(parts)[:1200]}


def flag_extra(output_root, flag):
    """Everything the screen needs to UNDERSTAND one flag:
      pages   = book pages to open (source_pages / referenced pages)
      images  = files named in the flag detail that exist under assets
      expand  = for chapter-level 'incomplete_records': which exact q_ids and
                which of Q/A/S is incomplete (so the human never has to guess)
      orphan  = readable text for orphan rows (parsed, not raw JSON)
    """
    out = Path(output_root)
    extra = {"pages": [], "images": [], "expand": [], "orphan": None}
    detail = str(flag.get("detail") or "")

    # images named inside the detail text (unresolved/unmatched/wrong-owner)
    for rel in sorted(set(_IMG_NAME_ANY.findall(detail))):
        if (out / "assets" / "questions" / rel).exists():
            extra["images"].append(rel)
        pages_from_name = re.search(r"/[\w-]+-p(\d+)-", rel)
        if pages_from_name:
            extra["pages"].append(int(pages_from_name.group(1)))

    if flag.get("q_id"):
        row = _find_master_row(out, flag["q_id"])
        if row:
            extra["pages"] += [int(p) for p in (row.get("source_pages") or [])
                               if isinstance(p, int)]
        extra["pages"] = sorted(set(extra["pages"]))

    m = re.search(r"pages?\s*\[([\d,\s]+)\]", detail)
    if m:
        extra["pages"] = sorted(set(extra["pages"] + [int(x) for x in
                                m.group(1).split(",") if x.strip().isdigit()]))

    if flag.get("kind") == "incomplete_records" and flag.get("chapter_id"):
        ch = flag["chapter_id"]
        subj = ch.split("-")[0]
        chd = out / "split" / subj / ch
        for fname, side in (("questions.jsonl", "stem/options"),
                            ("answers.jsonl", "answer"),
                            ("solutions.jsonl", "solution")):
            for r in _read_jsonl(chd / fname):
                if r.get("extraction_status") == "INCOMPLETE":
                    miss = ", ".join(r.get("missing_fields") or [side])
                    extra["expand"].append(
                        {"q_id": r.get("q_id"), "missing": miss})

    if flag.get("kind") in ("orphan_unresolved", "orphans"):
        # EVERY orphan-flavoured flag (orphans.jsonl AND the gate/validator/
        # completeness views of the SAME fragment) must resolve to the same
        # physical fragment -- otherwise one real orphan shows as 4 cards.
        cands = [r for r in _read_jsonl(out / "data" / "orphans.jsonl")
                 if r.get("chapter_id") == flag.get("chapter_id")]
        # a chapter can hold several orphan rows -- match by the pages the
        # flag detail carries ("pages [53, 54]" / "pdf_pages: [53, 54]" / raw)
        dpages = re.search(r"(?:pdf_pages|new_pages|pages)[^0-9]*\[([\d,\s]+)\]",
                           detail)
        want = dpages.group(1).replace(" ", "") if dpages else None
        cands.sort(key=lambda r: 0 if (want and str(
            ",".join(map(str, r.get("pdf_pages") or [])).replace(" ", "")) == want)
                   else 1)
        for r in cands:
            extra["orphan"] = orphan_readable(r)
            extra["frag_key"] = orphan_key(r)
            oqid = orphan_owner_qid(r)
            extra["owner_qid"] = oqid
            frag_text, _ = _frac(r.get("item"))
            norm = lambda s: " ".join(s.lower().split())
            if oqid:
                trow = _find_master_row(out, oqid)
                if trow:
                    extra["owner_sol"] = str((trow.get("solution") or {})
                                             .get("text") or "")[:900]
                    extra["owner_imgs"] = [i.get("file") for i in
                                           ((trow.get("solution") or {})
                                            .get("images") or [])
                                           if isinstance(i, dict)]
                    if frag_text:
                        extra["already_inside"] = norm(frag_text)[:120] \
                            in norm(extra["owner_sol"])
            # CROSS-CHECK vs EVERY question (user case: merge-guess wrong,
            # fragment already lives inside ANOTHER row's solution):
            if frag_text:
                head = norm(frag_text)[:120]
                for mr in _read_jsonl(out / "data" / "questions.jsonl"):
                    sol = norm(str((mr.get("solution") or {}).get("text") or ""))
                    if head and head in sol:
                        extra["already_present_in"] = mr.get("id")
                        break
            break
    return extra


def unclaimed_images(output_root, subject):
    """On-disk image files nobody references -- the 'attach' candidates."""
    out = Path(output_root)
    used = set()
    for r in _read_jsonl(out / "data" / "questions.jsonl"):
        if r.get("subject") != subject:
            continue
        for side in ("question", "solution"):
            for im in ((r.get(side) or {}).get("images") or []):
                used.add(im.get("file"))
        for o in (r.get("options") or []):
            for im in (o.get("images") or []):
                used.add(im.get("file"))
    base = out / "assets" / "questions" / subject
    if not base.exists():
        return []
    return sorted(f"{subject}/{p.name}" for p in base.glob("*.webp")
                  if f"{subject}/{p.name}" not in used)


def card_guide(card):
    """One-line Hinglish action guide per card kind -- answers 'isko dekh ke
    main kya karun?' so a human never faces a bare Approve/Skip."""
    kinds = card.get("kinds") or []
    kset = set(kinds)
    if kset & {"answer_key_disagrees"}:
        return ("🔴 ANSWER KEY se alag: book page kholo → key table dekho → "
                "key sahi toh Answer edit karo (dropdown), book hi galat "
                "hai toh reason likh ke Approve.")
    if kset & {"incomplete", "incomplete_records"}:
        return ("🔴 koi field KHAALI hai (stem/options/answer/solution) -- "
                "Edit panel me poora karke Save karo; expand list dekh "
                "kaunsa field missing hai.")
    if kset & {"orphan_unresolved", "orphans"}:
        return ("📦 Ye content kisi question ko attach NAHI hua. Fragment "
                "padho → best-guess question ki CURRENT solution ke saamne "
                "tulna karo → missing piece hai toh ➕ Merge, extra copy/"
                "noise hai toh Skip.")
    if any(k in k for k in kset for k in
           ("image", "unresolved", "unmatched", "unclaimed")):
        return ("🖼️ Image kisi ko attach nahi hui: crop dekho → book page "
                "kholo → sahi question mile toh Attach (Gallery/Lookup), "
                "sach me decorative/watermark hai toh Skip. Pehle se attached "
                "ho gayi ho toh ye flag apne aap auto-resolved ho jata hai.")
    if kset & {"foreign_option_head", "foreign_solution_segment",
               "foreign_option_head_review", "retry_foreign_fragment_blocked"}:
        return ("⚠️ Iske andar DOOSRE question ka text ghusa lagta hai -- "
                "stem/options/solution padhke foreign hissa hatao "
                "(✏️ Edit se trim), phir Approve.")
    if any(k.startswith("contaminated") for k in kset):
        return ("⚠️ contamination suspect -- question ke andar solution-type "
                "text aa gaya ho sakta hai. Edit me stem/solution theek karo.")
    if any(k.startswith("declared_figure_missing") for k in kset):
        return ("🖼️ Model ne bola 'figure hai' par koi image judi nahi. Lookup/"
                "book page se figure dhundho → mili toh Attach, book me sach "
                "me nahi hai toh Approve.")
    if "figure_page_mismatch" in kset:
        return ("🖼️ Image ka page question ke pages se match nahi karta -- "
                "book page kholo; galat judi hai toh Detach/Move.")
    if kset & {"review_needed", "qa_review_needed"}:
        return ("ℹ️ Neeche diya reason padho -- zyadatar kuch bada nahi hota. "
                "Content dekho + book page compare karo, sahi toh Approve.")
    if "watchdog" in " ".join(kset):
        return ("🛡️ Nayi flag-file mili jo queue register me NAHI hai -- mujhe "
                "report karo (register kar dunga). Skip mat karo bina dekhe.")
    return ("Reason detail padho → book page links se asli page compare karo "
            "→ sahi hai toh Approve, theek karna hai toh ✏️ Edit, faltu "
            "hai toh Skip.")


def group_review_rows(output_root, rows, views):
    """One CARD per real issue. The same question can be flagged by the gate,
    the validator, and qa_status for the SAME underlying problem -- four cards
    would mean four decisions for one issue. Group by entity:
      - rows with a q_id -> one card per q_id
      - orphan/unclaimed fragments crossing files (orphans.jsonl, export_gate,
        validator, completeness) -> one card per PHYSICAL fragment (frag_key)
      - anything else -> (chapter, kind, detail) card
    Returns cards: [{q_id, chapter_id, subject, severity(worst), flag_keys,
    kinds[], sources[], details[], pages[], images[], orphan(list of view
    dicts), expand[]}]. Deciding ONE card closes ALL its flags."""
    cards = {}
    for r in rows:
        v = views.get(r["flag_key"], {})
        img_files = re.findall(
            r"[\w-]+/[\w-]+(?:-\d{3})?-p\d+-[\w@.]+\.webp",
            str(r.get("detail") or ""))
        if r.get("q_id"):
            key = ("qid", r["q_id"])
        elif r.get("kind") in ("orphan_unresolved", "orphans") \
                and v.get("frag_key"):
            key = ("frag", r.get("chapter_id"), v["frag_key"])
        elif img_files and any(t in r.get("kind", "") for t in
                               ("image", "unresolved", "unmatched",
                                "unclaimed")):
            # the SAME image flagged by unmatched_images, unresolved_images
            # and the validator is ONE issue: group by the file itself
            key = ("img", img_files[0])
        else:
            key = ("other", r.get("chapter_id"), r.get("kind"),
                   str(r.get("detail", ""))[:80])
        c = cards.get(key)
        if c is None:
            sev_rank = {"BLOCKER": 0, "REVIEW": 1}
            c = {"q_id": r.get("q_id"), "chapter_id": r.get("chapter_id"),
                 "subject": r.get("subject")
                            or (r.get("q_id") or "").split("-")[0]
                            or None,
                 "severity": r["severity"], "flag_keys": [], "kinds": [],
                 "sources": [], "details": [], "pages": [], "images": [],
                 "orphan": [], "expand": [], "stale_notes": [],
                 "_rank": sev_rank.get(r["severity"], 2)}
            cards[key] = c
        if r.get("stale_note") and r["stale_note"] not in c["stale_notes"]:
            c["stale_notes"].append(r["stale_note"])
        c["flag_keys"].append(r["flag_key"])
        if r["kind"] not in c["kinds"]:
            c["kinds"].append(r["kind"])
        if r.get("source") not in c["sources"]:
            c["sources"].append(r.get("source"))
        d = str(r.get("detail") or "")
        if d not in [x["detail"] for x in c["details"]]:
            c["details"].append({"kind": r["kind"], "source": r.get("source"),
                                 "detail": d})
        for p in v.get("pages") or []:
            if p not in c["pages"]:
                c["pages"].append(p)
        for im in v.get("images") or []:
            if im not in c["images"]:
                c["images"].append(im)
        if v.get("orphan"):
            c["orphan"].append(v)
        c["expand"].extend(v.get("expand") or [])
        if sev_rank.get(r["severity"], 2) < c["_rank"]:
            c["_rank"] = sev_rank[r["severity"]]
            c["severity"] = r["severity"]
    out = list(cards.values())
    for c in out:
        c["pages"].sort()
        c["expand"] = [dict(t) for t in {json.dumps(e, sort_keys=True): e
                                         for e in c["expand"]}.values()]
        c["flag_keys_json"] = json.dumps(c["flag_keys"])
        c["guide"] = card_guide(c)
    out.sort(key=lambda c: (c["_rank"], str(c.get("chapter_id") or ""),
                            c.get("q_id") or ""))
    return out
    """Which chapter of <subject> contains file-page <page>? chapters.json
    ranges when present, else the split rows' source_pages min/max. Used so
    the human types only the question NUMBER, never the chapter id."""
    out = Path(output_root)
    cj = out / "subjects" / subject / "chapters.json"
    if cj.exists():
        try:
            for c in json.loads(cj.read_text()):
                a, b = c.get("file_start"), c.get("file_end")
                if a and b and int(a) <= page <= int(b):
                    return c.get("chapter_id")
        except Exception:
            pass
    for chd in sorted((out / "split" / subject).glob("*")):
        pages = []
        for r in _read_jsonl(chd / "questions.jsonl"):
            pages += [p for p in (r.get("source_pages") or [])
                      if isinstance(p, int)]
        if pages and min(pages) <= page <= max(pages):
            return chd.name
    return None


def chapter_for_page(output_root, subject, page):
    """Which chapter of <subject> contains file-page <page>? chapters.json
    ranges when present, else the split rows' source_pages min/max. Used so
    the human types only the question NUMBER, never the chapter id."""
    out = Path(output_root)
    cj = out / "subjects" / subject / "chapters.json"
    if cj.exists():
        try:
            for c in json.loads(cj.read_text()):
                a, b = c.get("file_start"), c.get("file_end")
                if a and b and int(a) <= page <= int(b):
                    return c.get("chapter_id")
        except Exception:
            pass
    for chd in sorted((out / "split" / subject).glob("*")):
        pages = []
        for r in _read_jsonl(chd / "questions.jsonl"):
            pages += [p for p in (r.get("source_pages") or [])
                      if isinstance(p, int)]
        if pages and min(pages) <= page <= max(pages):
            return chd.name
    return None


def image_status(output_root, file_rel):
    """Where does this image file stand right now?
    {exists_on_disk, owners:[q_id...], page (from temp name), attached_seen}
    -- the lookup answer for 'ye image asal me kahin hui h ki nahi'."""
    out = Path(output_root)
    refs = _image_refs(out, file_rel)
    m = re.search(r"/[\w-]+-p(\d+)-", file_rel)
    return {
        "file": file_rel,
        "exists_on_disk": (out / "assets" / "questions" / file_rel).exists(),
        "owners": refs,
        "page": int(m.group(1)) if m else None,
    }


def lookup_questions(output_root, term, chapter_id=None):
    """Find master rows by q_id fragment or q_no (+optional chapter).
    Accepts: 'OBG-003-009', '003-009', '9' + chapter, 'q9' + chapter.
    Returns list of rows (never raises)."""
    out = Path(output_root)
    term = (term or "").strip()
    rows = _read_jsonl(out / "data" / "questions.jsonl")
    if not term:
        return []
    hit = [r for r in rows if term.lower() in str(r.get("id", "")).lower()]
    if hit:
        return hit
    m = re.match(r"^(?:q\s*)?(\d{1,3})$", term, re.I)
    if m and chapter_id:
        qn = int(m.group(1))
        want = f"{chapter_id}-{qn:03d}"
        return [r for r in rows if r.get("id") == want]
    m2 = re.match(r"^(\d{1,3})-(\d{1,3})$", term)   # '3-9' = chapter 3 q 9
    if m2 and not chapter_id:
        cid = None
        return hit
    return hit


_RE_FLAGS_KNOWN_FILE = re.compile(r"\.jsonl$")


# ---------------------------------------------------------------------------
# Small IO helpers (atomic, append-safe, read-back friendly)
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"_unparseable": line[:120]})
    return out


def _write_jsonl_atomic(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                   + ("\n" if rows else ""), encoding="utf-8")
    tmp.replace(path)  # os.replace semantics: never a half-written file


def _append_jsonl(path: Path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# 2. Queue construction — UNION of all sources + dedupe + decisions overlay
# ---------------------------------------------------------------------------

def _mk_flag(kind, severity, detail, source, chapter_id=None, q_id=None,
             q_no=None, subject=None, extra=None):
    flag = {
        "kind": kind, "severity": severity, "detail": str(detail)[:400],
        "source": source, "chapter_id": chapter_id, "q_id": q_id,
        "q_no": q_no, "subject": subject,
    }
    if extra:
        flag.update(extra)
    flag["flag_key"] = decision_key(flag)
    return flag


def decision_key(flag) -> str:
    """Stable identity of a flag across runs: kind + who + a pinch of detail.
    Detail digest is short on purpose — cosmetic rewording shouldn't resurrect
    a decided flag, but a different problem on the same row must."""
    raw = "|".join(str(x or "") for x in (
        flag.get("kind"), flag.get("q_id") or "", flag.get("chapter_id") or "",
        flag.get("source") or ""))
    det = hashlib.sha1(str(flag.get("detail", "")).encode()).hexdigest()[:8]
    return hashlib.sha1((raw + "|" + det).encode()).hexdigest()[:16]


def _chapter_from_qid(q_id):
    """OBG-003-016 -> OBG-003"""
    parts = (q_id or "").split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else None


def _qn_from_qid(q_id):
    try:
        return int((q_id or "").rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


_PAGE_NO_IMG_KINDS = ("declared_figure_missing", "missing_declared_figure")


_WM_ID_CACHE = {}


def _page_watermark_ids(pdf_path):
    """The pipeline's own three-gate watermark detector (cached). A page that
    carries ONLY the watermark image has NO printed figure -- without this
    exclusion every page would 'have an image' (the stamp IS an image)."""
    key = str(pdf_path)
    if key not in _WM_ID_CACHE:
        try:
            import qbank_pipeline as _qp
            _WM_ID_CACHE[key] = set(_qp.find_watermark_object_ids(pdf_path))
        except Exception:
            _WM_ID_CACHE[key] = frozenset()   # unknown -> conservative flow
    return _WM_ID_CACHE[key]


def _page_has_raster_image(pdf_path, page):
    """True iff this file page carries at least one NON-watermark raster image
    (Marrows scan books embed figures as raster objects). Zero objects on the
    page => no figure was printed there => a 'missing figure' flag on content
    from this page is provably false. Phoenix check -- zero tokens."""
    try:
        from pypdf import PdfReader
        r = PdfReader(str(pdf_path))
        pg = r.pages[page - 1]
        xo = (pg.get("/Resources") or {}).get("/XObject") or {}
        wm = _page_watermark_ids(pdf_path)
        for name, ref in xo.items():
            try:
                obj_id = getattr(ref, "idnum", None)
                if obj_id is not None and obj_id in wm:
                    continue            # the stamp itself, not content
                o = ref.get_object()
                if o.get("/Subtype") != "/Image":
                    continue
                w, h = int(o.get("/Width", 0)), int(o.get("/Height", 0))
                if w * h > 30000:        # anything figure-sized
                    return True
            except Exception:
                continue
    except Exception:
        return True      # proof impossible -> stay conservative (keep flag)
    return False


def _auto_prove_no_figure(output_root, flag, books_dir):
    """declared_figure_missing* flags where every implicated page has NO
    raster image -> return a note string (auto-resolve), else None."""
    kind = str(flag.get("kind") or "")
    if not any(s in kind for s in _PAGE_NO_IMG_KINDS):
        return None
    pages = []
    m = re.search(r"pages?\s*\[([\d,\s]+)\]", str(flag.get("detail") or ""))
    if m:
        pages = [int(x) for x in m.group(1).split(",") if x.strip().isdigit()]
    if not pages and flag.get("q_id"):
        row = _find_master_row(output_root, flag["q_id"])
        if row:
            pages = [p for p in (row.get("source_pages") or [])
                     if isinstance(p, int)]
    if not pages:
        return None
    subj = (flag.get("subject") or (flag.get("q_id") or "").split("-")[0])
    pdf = Path(books_dir) / f"{subj}.pdf"
    if not pdf.exists():
        return None
    if all(not _page_has_raster_image(pdf, p) for p in pages):
        return (f"PROVABLY false: source page(s) {pages} contain no raster "
                f"image object at all -- the model saw a TABLE and called it "
                f"a figure. Nothing exists to attach.")
    return None


def collect_review_queue(output_root, books_dir=None) -> dict:
    """Read EVERYTHING and return the normalized queue. See contract 2/3."""
    out_root = Path(output_root)
    data = out_root / "data"
    rows = []
    warnings = []

    # -- A. subject bundle rows: qa_status drives the primary row flags ------
    masters = _read_jsonl(data / "questions.jsonl")
    by_id = {}
    for r in masters:
        rid = r.get("id")
        if rid:
            by_id[rid] = r
        st = r.get("qa_status")
        if st == "INCOMPLETE":
            rows.append(_mk_flag(
                "incomplete", "BLOCKER",
                "; ".join(r.get("qa_reasons") or []) or "structural fields missing",
                "qa_status", chapter_id=r.get("chapter_id"), q_id=rid,
                q_no=_qn_from_qid(rid), subject=r.get("subject")))
        elif st == "REVIEW_NEEDED":
            rows.append(_mk_flag(
                "review_needed", "REVIEW",
                "; ".join(r.get("qa_reasons") or []) or "qa review requested",
                "qa_status", chapter_id=r.get("chapter_id"), q_id=rid,
                q_no=_qn_from_qid(rid), subject=r.get("subject")))

    # -- B. every registered flag file ---------------------------------------
    for fname, _desc in FLAG_SOURCES.items():
        path = data / fname
        for frow in _read_jsonl(path):
            # a flag row may carry multiple targets under "rows" (e.g.
            # integrity_flags.jsonl: answer_key_disagrees with rows[])
            targets = frow.get("rows") if isinstance(frow.get("rows"), list) \
                and frow["rows"] else [frow]
            for tgt in targets:
                if not isinstance(tgt, dict):
                    continue
                kind = frow.get("kind") or fname.replace(".jsonl", "")
                qn = tgt.get("q_no", frow.get("q_no"))
                cid = tgt.get("chapter_id", frow.get("chapter_id"))
                q_id = f"{cid}-{int(qn):03d}" if (cid and isinstance(qn, int)) \
                    else tgt.get("q_id") or frow.get("q_id")
                sev = "BLOCKER" if kind in BLOCKER_KINDS else "REVIEW"
                detail = tgt.get("detail") or json.dumps(
                    {k: v for k, v in tgt.items()
                     if k not in ("kind", "q_no", "chapter_id", "ts")},
                    ensure_ascii=False, default=str)
                rows.append(_mk_flag(
                    kind, sev, detail, fname, chapter_id=cid, q_id=q_id,
                    q_no=qn if isinstance(qn, int) else _qn_from_qid(q_id or ""),
                    subject=(q_id or "").split("-")[0] if q_id else None))

    # -- C. per-chapter split-layer completeness / unresolved ------------------
    split_root = out_root / "split"
    if split_root.exists():
        for comp in sorted(split_root.glob("*/*/chapter_completeness.json")):
            try:
                c = json.loads(comp.read_text())
            except Exception:
                continue
            cid = c.get("chapter_id")
            inc = sum(int(c.get(k) or 0) for k in (
                "incomplete_questions", "incomplete_answers", "incomplete_solutions"))
            if inc:
                rows.append(_mk_flag(
                    "incomplete_records", "BLOCKER",
                    f"{inc} split record(s) INCOMPLETE "
                    f"(Q{c.get('incomplete_questions')}/A{c.get('incomplete_answers')}"
                    f"/S{c.get('incomplete_solutions')})",
                    "chapter_completeness", chapter_id=cid))
            if int(c.get("unresolved_qid_count") or 0):
                rows.append(_mk_flag(
                    "unresolved_qid", "BLOCKER",
                    f"unresolved q_nos: {c.get('unresolved_qid_q_nos')}",
                    "chapter_completeness", chapter_id=cid))
            if int(c.get("orphan_count") or 0):
                rows.append(_mk_flag(
                    "orphan_unresolved", "REVIEW",
                    f"{c['orphan_count']} unclaimed fragment(s) — see orphans.jsonl",
                    "chapter_completeness", chapter_id=cid))

    # -- D. validator mega-aggregate (CATCH-ALL): any flag the validator
    #       computed is surfaced, even if no mapping above understood it.
    #       This is the "no flag may slip" guarantee, contract 2.
    vrep_path = data / "validation_report.json"
    surfaced_keys = {r["flag_key"] for r in rows}
    if vrep_path.exists():
        try:
            vrep = json.loads(vrep_path.read_text())
        except Exception:
            vrep = {}
        for chap, flags in (vrep.get("chapters") or {}).items():
            for f in (flags or []):
                kind = f.get("kind") or "validator"
                qn = f.get("q_no")
                q_id = f"{chap}-{int(qn):03d}" if isinstance(qn, int) else None
                sev = ("BLOCKER" if kind in BLOCKER_KINDS else "REVIEW")
                detail = f.get("detail") or json.dumps(
                    {k: v for k, v in f.items() if k not in ("kind", "q_no")},
                    ensure_ascii=False, default=str)
                row = _mk_flag(kind, sev, detail, "validation_report",
                               chapter_id=chap, q_id=q_id,
                               q_no=qn if isinstance(qn, int) else None,
                               subject=chap.split("-")[0] if chap else None)
                if row["flag_key"] not in surfaced_keys:
                    surfaced_keys.add(row["flag_key"])
                    rows.append(row)

    # -- E. WATCHDOG: a flag-file we don't know must never be silent ----------
    if data.exists():
        for p in sorted(data.glob("*.jsonl")):
            if p.name not in CORE_DATA_FILES:
                warnings.append(
                    f"unknown flag file '{p.name}' exists in data/ but is not "
                    f"registered in review_queue.FLAG_SOURCES — its rows are "
                    f"NOT in this queue; check it manually and register it")
                rows.append(_mk_flag(
                    "watchdog_unregistered_file", "BLOCKER", warnings[-1],
                    p.name))

    # -- F. decisions overlay: close decided flags, resurrect stale ones ------
    decisions = _load_decisions(out_root)
    latest = {}
    for d in decisions:
        latest[d["flag_key"]] = d            # append-only log; last wins
    open_rows, done = [], 0
    auto_resolved = []
    for r in rows:
        # PROVABLY-FALSE figure-missing flags (user asked: 'actually no image
        # exists on the page at all'): if every implicated source page carries
        # NO raster image object, the model merely mistook a TABLE for a
        # figure. Nothing exists to attach -> auto-resolved, note logged.
        if r["flag_key"] not in latest:
            note = _auto_prove_no_figure(out_root, r,
                                         books_dir or "/data/input_pdfs")
            if note:
                r["state"] = "auto_resolved"
                r["auto_note"] = note
                auto_resolved.append(r)
                done += 1
                continue
        # SELF-VERIFYING flags: a flag that names an image file must stand
        # DOWN when that file is no longer in the flagged state. "Image
        # unclaimed"-class flags become false the moment the human attaches
        # the file anywhere -- re-showing them would force a second decision
        # on an issue that is already fixed (user report, 2026-08-20).
        fx = re.findall(r"[\w-]+/[\w-]+-\d{3}-p\d+-[\w@.]+\.webp|"
                        r"[\w-]+/[\w-]+-p\d+-[\w@.]+\.webp", str(r.get("detail") or ""))
        if any(k in r.get("kind", "") for k in
               ("image", "unresolved", "unclaimed", "unmatched")) and fx:
            owned_now = []
            missing_now = []
            owner_names = []
            for fr in fx:
                st = image_status(out_root, fr)
                owners = st["owners"]
                if not owners:
                    # the file may have been RENAMED by a human attach/move:
                    # follow the ownership-ledger alias chain
                    for lr in _read_jsonl(out_root / "data"
                                          / "image_ownership.jsonl"):
                        if lr.get("file") == fr and lr.get("final_file"):
                            o2 = image_status(out_root, lr["final_file"])["owners"]
                            owners = owners or o2
                if owners:
                    owned_now.append(fr)
                    owner_names.append(owners[0])
                else:
                    missing_now.append(fr)
            if owned_now and not missing_now:
                r["state"] = "auto_resolved"
                r["auto_note"] = "file(s) now owned by " + ", ".join(owner_names)
                auto_resolved.append(r)
                done += 1
                continue
        d = latest.get(r["flag_key"])
        if not d:
            r["state"] = "open"
            open_rows.append(r)
            continue
        sig = _row_current_signature(out_root, r)
        if sig is not None and d.get("content_sig") and sig != d["content_sig"]:
            r["state"] = "open"
            r["stale_note"] = ("previously " + d["action"] + " but the row "
                               "content changed since — decide again")
            open_rows.append(r)
        else:
            r["state"] = d["action"]
            done += 1

    order = {"BLOCKER": 0, "REVIEW": 1}
    open_rows.sort(key=lambda r: (order.get(r["severity"], 2),
                                  str(r.get("chapter_id") or ""),
                                  r.get("q_no") or 0, r["kind"]))
    return {
        "rows": open_rows,
        "counts": {
            "blocker": sum(1 for r in open_rows if r["severity"] == "BLOCKER"),
            "review": sum(1 for r in open_rows if r["severity"] == "REVIEW"),
            "resolved": done,
            "auto_resolved": len(auto_resolved),
        },
        "auto_resolved_rows": auto_resolved,
        "warnings": warnings,
        "clear": not open_rows,
    }


# ---------------------------------------------------------------------------
# 3. Decisions & content signatures (refresh-proof, stale-aware)
# ---------------------------------------------------------------------------

def _row_current_signature(out_root: Path, flag: dict):
    q_id = flag.get("q_id")
    if not q_id:
        return None
    row = _find_master_row(out_root, q_id)
    if row is None:
        return None
    return _row_signature(row)


def _row_signature(row: dict) -> str:
    q = row.get("question") or {}
    s = row.get("solution") or {}
    blob = "|".join([
        str(q.get("text") or ""),
        "|".join(str(o.get("text") or "") for o in (row.get("options") or [])),
        "|".join(row.get("correct_options") or []),
        str(s.get("text") or ""),
        "|".join(i.get("file", "") for i in (q.get("images") or [])),
        "|".join(i.get("file", "") for i in (s.get("images") or [])),
    ])
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _load_decisions(out_root: Path):
    return _read_jsonl(out_root / "data" / REVIEW_DECISIONS)


def record_decision(output_root, flag_key: str, action: str, reason: str = "",
                    q_id: str = None) -> dict:
    """action: approved | ignored | resolved | edited. Persist FIRST; the
    caller re-reads the queue afterwards (never trust memory)."""
    if action not in ("approved", "ignored", "resolved", "edited"):
        return {"ok": False, "error": f"bad action {action!r}"}
    out_root = Path(output_root)
    sig = None
    if q_id:
        row = _find_master_row(out_root, q_id)
        if row is not None:
            sig = _row_signature(row)
    entry = {"flag_key": flag_key, "action": action, "reason": reason or "",
             "q_id": q_id, "content_sig": sig, "ts": _now()}
    _append_jsonl(out_root / "data" / REVIEW_DECISIONS, entry)
    return {"ok": True, "entry": entry}


# ---------------------------------------------------------------------------
# 4. Row location + atomic multi-copy field updates
# ---------------------------------------------------------------------------

def _copies(output_root, q_id: str):
    """Every file that carries this row's content. Master bundle, subjects
    view, per-chapter file, and the three split files."""
    out_root = Path(output_root)
    subject, chap = None, None
    parts = q_id.split("-")
    if len(parts) >= 3:
        subject = parts[0]
        chap = f"{parts[0]}-{parts[1]}"
    files = {"master": out_root / "data" / "questions.jsonl"}
    if subject:
        files["subjects"] = out_root / "subjects" / subject / "questions.jsonl"
    if chap:
        files["chapter_file"] = out_root / "subjects" / subject / "chapters" / f"{chap}.jsonl"
        ch_dir = out_root / "split" / subject / chap
        files["split_q"] = ch_dir / "questions.jsonl"
        files["split_a"] = ch_dir / "answers.jsonl"
        files["split_s"] = ch_dir / "solutions.jsonl"
        files["manifest"] = ch_dir / "image_manifest.jsonl"
    return subject, chap, files


def _find_master_row(output_root, q_id: str):
    for r in _read_jsonl(Path(output_root) / "data" / "questions.jsonl"):
        if r.get("id") == q_id:
            return r
    return None


def _json_path_get(row, path):
    """path like ('question','text') or ('options','A','text')"""
    cur = row
    for p in path:
        if isinstance(cur, list):
            cur = next((o for o in cur if str(o.get("id")) == str(p)), None)
        elif isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
        if cur is None:
            return None
    return cur


def _json_path_set(row, path, value):
    cur = row
    for p in path[:-1]:
        if isinstance(cur, list):
            cur = next((o for o in cur if str(o.get("id")) == str(p)), None)
        else:
            cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is None:
            return False
    last = path[-1]
    if isinstance(cur, list):
        tgt = next((o for o in cur if str(o.get("id")) == str(last)), None)
        if tgt is None:
            return False
        return True  # list target handled by caller w/ leaf key
    if isinstance(cur, dict):
        cur[last] = value
        return True
    return False


FIELD_PATHS = {
    # logical field -> (master bundle path, split file, split path)
    "question_text": (("question", "text"), "split_q", ("question_text",)),
    "solution_text": (("solution", "text"), "split_s", ("solution_text",)),
    "correct_option": (("correct_options",), "split_a", ("correct_option",)),
}

# table = solution-side tables, table_q = question-side tables. Appending a
# NEW table is allowed at table_index == len(tables) (slot is created as
# type "human_added" then filled); any larger gap is refused.


def _table_target(row, field):
    holder = row.setdefault("question" if field == "table_q" else "solution", {})
    return holder.setdefault("tables", [])


def _table_write(tabs, table_index, value, create):
    """Set tabs[table_index].markdown = value. create=True allows the append
    slot (index == len). Returns error string or None."""
    if table_index is None:
        return "table index missing"
    table_index = int(table_index)
    if table_index == len(tabs) and create:
        tabs.append({"type": "human_added", "markdown": ""})
    if not (0 <= table_index < len(tabs)):
        return ("table index out of range (have %d, next append slot is %d)"
                % (len(tabs), len(tabs)))
    tabs[table_index]["markdown"] = value
    return None


def apply_edit(output_root, q_id: str, field: str, value, reason: str = "",
               option_letter: str = None, table_index: int = None) -> dict:
    """Update ONE logical field in EVERY file copy, atomically, then read
    back and verify. Returns {ok, verified, changed_files} — the screen must
    show ok only after verified=True.
    field 'table_delete'/'table_q_delete' REMOVES the table at table_index
    (reviewer asked: duplicate/garbage tables must be removable)."""
    DELETE = field in ("table_delete", "table_q_delete")
    base_field = field.replace("_delete", "") if DELETE else field
    out_root = Path(output_root)
    subject, chap, files = _copies(out_root, q_id)
    if not chap:
        return {"ok": False, "error": f"cannot parse q_id {q_id!r}"}
    value = (value or "").strip() if isinstance(value, str) else value

    # ---- validation per field ----------------------------------------------
    if field == "correct_option":
        value = str(value).strip().upper()
        if value not in ("A", "B", "C", "D"):
            return {"ok": False, "error": "answer must be one of A/B/C/D"}
    if field == "option":
        option_letter = (option_letter or "").strip().upper()
        if option_letter not in ("A", "B", "C", "D"):
            return {"ok": False, "error": "option letter must be A-D"}
    if field in ("table", "table_q"):
        err = _table_md_error(str(value or ""))
        if err:
            return {"ok": False, "error": err}

    # ---- PRE-FLIGHT for table edits: never a half-written edit --------------
    # apply_to_* may raise inside the write loop; every copy must therefore
    # be checked BEFORE the first file is touched, and all copies must agree
    # on the table count (append slot == len for ALL, or index < len for ALL,
    # delete strictly index < len).
    if base_field in ("table", "table_q"):
        side_key = "question" if base_field == "table_q" else "solution"
        lens = []
        row0 = _find_master_row(out_root, q_id)
        if row0 is not None:
            lens.append(len((row0.get(side_key) or {}).get("tables") or []))
        for name in ("subjects", "chapter_file"):
            p = files.get(name)
            if p and p.exists():
                for r in _read_jsonl(p):
                    if r.get("id") == q_id:
                        lens.append(len((r.get(side_key) or {}).get("tables") or []))
        sp = files["split_s" if base_field == "table" else "split_q"]
        if sp.exists():
            for r in _read_jsonl(sp):
                if r.get("q_id") == q_id:
                    lens.append(len(r.get("tables") or []))
        if table_index is None:
            return {"ok": False, "error": "table index missing"}
        if not lens:
            return {"ok": False, "error": f"row {q_id} not found in any output file"}
        if len(set(lens)) != 1:
            return {"ok": False, "error": f"copies diverge on table count "
                    f"({lens}) -- manual check, refusing partial write"}
        have = lens[0]
        if DELETE:
            if not (0 <= int(table_index) < have):
                return {"ok": False, "error": f"table index {table_index} out of "
                        f"range (have {have})"}
        elif not (0 <= int(table_index) <= have):
            return {"ok": False, "error": f"table index out of range (have "
                    f"{have}, next append slot is {have})"}

    # ---- what to write where --------------------------------------------------
    def apply_to_master(row):
        if field == "question_text":
            row.setdefault("question", {})["text"] = value
        elif field == "solution_text":
            row.setdefault("solution", {})["text"] = value
        elif field == "correct_option":
            row["correct_options"] = [value]
        elif field == "option":
            for o in row.get("options") or []:
                if o.get("id") == option_letter:
                    o["text"] = value
        elif base_field in ("table", "table_q"):
            tabs = _table_target(row, base_field)
            if DELETE:
                del tabs[int(table_index)]
            else:
                err = _table_write(tabs, table_index, value, create=True)
                if err:
                    raise ValueError(err)
        row["qa_human_edit"] = True

    def apply_to_split(rows, split_kind):
        for r in rows:
            if r.get("q_id") != q_id:
                continue
            if split_kind == "split_q" and field == "question_text":
                r["question_text"] = value
            elif split_kind == "split_q" and field == "option":
                for o in r.get("options") or []:
                    if o.get("id") == option_letter:
                        o["text"] = value
            elif split_kind == "split_s" and field == "solution_text":
                r["solution_text"] = value
            elif split_kind == "split_s" and base_field == "table":
                tabs = r.setdefault("tables", [])
                if DELETE:
                    del tabs[int(table_index)]
                else:
                    err = _table_write(tabs, table_index, value, create=True)
                    if err:
                        raise ValueError(err)
            elif split_kind == "split_q" and base_field == "table_q":
                tabs = r.setdefault("tables", [])
                if DELETE:
                    del tabs[int(table_index)]
                else:
                    err = _table_write(tabs, table_index, value, create=True)
                    if err:
                        raise ValueError(err)
            elif split_kind == "split_a" and field == "correct_option":
                r["correct_option"] = value
        return rows

    targets = []
    if field in FIELD_PATHS or field == "option":
        split_kind = {"question_text": "split_q", "solution_text": "split_s",
                      "correct_option": "split_a"}.get(field, "split_q")
        targets.append(split_kind)
    elif base_field == "table":
        targets.append("split_s")
    elif base_field == "table_q":
        targets.append("split_q")

    # ---- load, modify, write, per file ---------------------------------------
    changed = []
    for name in ("master", "subjects", "chapter_file"):
        p = files.get(name)
        if not p or not p.exists():
            continue
        rows = _read_jsonl(p)
        hit = False
        for r in rows:
            if r.get("id") == q_id:
                apply_to_master(r)
                hit = True
        if hit:
            _write_jsonl_atomic(p, rows)
            changed.append(str(p.relative_to(out_root)))
    for name in targets:
        p = files.get(name)
        if not p or not p.exists():
            continue
        rows = apply_to_split(_read_jsonl(p), name)
        _write_jsonl_atomic(p, rows)
        changed.append(str(p.relative_to(out_root)))

    if not changed:
        return {"ok": False, "error": f"row {q_id} not found in any output file"}

    # ---- READ-BACK VERIFY (contract 5) ----------------------------------------
    verified = _verify_edit(out_root, q_id, field, value, option_letter,
                            table_index,
                            expect_len=(lens[0] - 1) if DELETE else None)
    _append_jsonl(out_root / "data" / HUMAN_EDIT_LEDGER, {
        "q_id": q_id, "field": field, "option_letter": option_letter,
        "table_index": table_index,
        "new_value": (str(value)[:300] if not isinstance(value, (list, dict))
                      else json.dumps(value)[:300]),
        "reason": reason or "", "verified": bool(verified),
        "changed_files": changed, "ts": _now()})
    return {"ok": True, "verified": bool(verified), "changed_files": changed}


def _verify_edit(out_root, q_id, field, value, option_letter=None,
                 table_index=None, expect_len=None):
    """Read-back from BOTH the master bundle AND the delivery split file —
    'saved' is only true when the copy the converter will read says so."""
    row = _find_master_row(out_root, q_id)
    if row is None:
        return False
    subject, chap, files = _copies(out_root, q_id)

    def split_row(fname):
        for r in _read_jsonl(files[fname]):
            if r.get("q_id") == q_id:
                return r
        return None

    if field == "question_text":
        m = (row.get("question") or {}).get("text") == value
        s = (split_row("split_q") or {}).get("question_text") == value
        return m and s
    if field == "solution_text":
        m = (row.get("solution") or {}).get("text") == value
        s = (split_row("split_s") or {}).get("solution_text") == value
        return m and s
    if field == "correct_option":
        m = row.get("correct_options") == [value]
        s = (split_row("split_a") or {}).get("correct_option") == value
        return m and s
    if field == "option":
        letter = (option_letter or "").upper()
        m = any(o.get("id") == letter and o.get("text") == value
                for o in (row.get("options") or []))
        srow = split_row("split_q") or {}
        s = any(o.get("id") == letter and o.get("text") == value
                for o in (srow.get("options") or []))
        return m and s
    if field == "table" or field == "table_q":
        side = "solution" if field == "table" else "question"
        tabs = (row.get(side) or {}).get("tables") or []
        m = (isinstance(table_index, int) and 0 <= table_index < len(tabs)
             and tabs[table_index].get("markdown") == value)
        stabs = (split_row("split_s" if field == "table" else "split_q")
                 or {}).get("tables") or []
        s = (isinstance(table_index, int) and 0 <= table_index < len(stabs)
             and stabs[table_index].get("markdown") == value)
        return m and s
    if field in ("table_delete", "table_q_delete"):
        side = "solution" if field == "table_delete" else "question"
        sfile = "split_s" if field == "table_delete" else "split_q"
        m = (row.get(side) or {}).get("tables") or []
        s = (split_row(sfile) or {}).get("tables") or []
        identical = hashlib.sha1(json.dumps(m, sort_keys=True).encode()) \
            .hexdigest() == hashlib.sha1(json.dumps(s, sort_keys=True)
                                         .encode()).hexdigest()
        if expect_len is not None:
            return identical and len(m) == expect_len   # prove the delete happened
        return identical
    return False


_COL_SPLIT = re.compile(r"(?<!\\)\|")


def _table_md_error(md: str):
    """Guard: a table whose rows disagree on column count will crash/ugly the
    app renderer. Block the save BEFORE any file is touched."""
    lines = [l for l in md.splitlines() if l.strip()]
    if len(lines) < 2:
        return "table too small (need header + separator)"
    counts = [len(_COL_SPLIT.split(l.strip().strip('|'))) for l in lines]
    if len(set(counts)) != 1:
        return (f"broken table: uneven column counts {sorted(set(counts))} "
                f"— fix before saving (app renderer guard)")
    if not re.match(r"^\s*\|?[\s:|-]+\|", lines[1]):
        return "second line must be the --- separator row"
    return None


# ---------------------------------------------------------------------------
# 5. IMAGE OPERATIONS (contract 7: unlink never deletes; moves keep the ledger)
# ---------------------------------------------------------------------------

_KIND_FOR = {"question": "Q", "solution": "SOL", "option": "OPT"}


def _assets_rel(output_root):
    return Path(output_root) / "assets" / "questions"


def _image_refs(output_root, file_rel: str):
    """q_ids currently referencing this file (master bundle view)."""
    users = []
    for r in _read_jsonl(Path(output_root) / "data" / "questions.jsonl"):
        for side in ("question", "solution"):
            for im in ((r.get(side) or {}).get("images") or []):
                if im.get("file") == file_rel:
                    users.append(r.get("id"))
        for o in (r.get("options") or []):
            for im in (o.get("images") or []):
                if im.get("file") == file_rel:
                    users.append(r.get("id"))
    return sorted(set(u for u in users if u))


def _ledger_page_map(output_root):
    out = {}
    for row in _read_jsonl(Path(output_root) / "data" / "image_ownership.jsonl"):
        if row.get("outcome") in ("claimed", "shared") and row.get("final_file"):
            out[row["final_file"]] = row.get("page")
    return out


def _next_slot(output_root, subject, q_id, kind_letter):
    base = _assets_rel(output_root) / subject
    prefix = f"{q_id}_{kind_letter}_"
    mx = 0
    if base.exists():
        for p in base.glob(f"{prefix}*.webp"):
            m = re.match(re.escape(prefix) + r"(\d{2})\.webp", p.name)
            if m:
                mx = max(mx, int(m.group(1)))
    return mx + 1


def _update_image_lists(output_root, q_id, side, option_letter, file_rel,
                        add=False, remove=False, rename_to=None,
                        new_owner=None, keep_manifest_from=None):
    """Rewrite image references in master+subjects+chapter+split+manifest."""
    subject, chap, files = _copies(output_root, q_id)
    changed = []

    def edit_imgs(imgs):
        nonlocal file_rel
        imgs = [dict(i) for i in (imgs or [])]
        if rename_to:
            for i in imgs:
                if i.get("file") == file_rel:
                    i["file"] = rename_to
        if remove:
            imgs = [i for i in imgs if i.get("file") != file_rel]
        if add:
            if not any(i.get("file") == file_rel for i in imgs):
                imgs.append({"file": file_rel, "source_pages": (
                    _ledger_pages.get(file_rel) and
                    [_ledger_pages[file_rel]] or [])})
        return imgs

    _ledger_pages = _ledger_page_map(output_root)

    for name in ("master", "subjects", "chapter_file"):
        p = files.get(name)
        if not p or not p.exists():
            continue
        rows, hit = _read_jsonl(p), False
        for r in rows:
            if r.get("id") != q_id:
                continue
            container = r.get(side) if side in ("question", "solution") else None
            if container is not None:
                container["images"] = edit_imgs(container.get("images"))
                hit = True
            elif side == "option":
                for o in (r.get("options") or []):
                    if o.get("id") == (option_letter or "").upper():
                        o["images"] = edit_imgs(o.get("images"))
                        hit = True
        if hit:
            _write_jsonl_atomic(p, rows)
            changed.append(str(p.relative_to(output_root)))

    # split-side
    sp = files["split_q"] if side in ("question", "option") else files["split_s"]
    if sp.exists():
        rows = _read_jsonl(sp)
        for r in rows:
            if r.get("q_id") != q_id:
                continue
            if side == "question":
                r["question_images"] = edit_imgs(r.get("question_images"))
            elif side == "option":
                for o in (r.get("options") or []):
                    if o.get("id") == (option_letter or "").upper():
                        o["images"] = edit_imgs(o.get("images"))
            else:
                r["solution_images"] = edit_imgs(r.get("solution_images"))
        _write_jsonl_atomic(sp, rows)
        changed.append(str(sp.relative_to(output_root)))

    return changed


def _rebuild_manifest(output_root, subject, chap, keep_pages):
    """Manifest is a VIEW of the split rows — rebuild it after every op."""
    ch_dir = Path(output_root) / "split" / subject / chap
    q_path, s_path = ch_dir / "questions.jsonl", ch_dir / "solutions.jsonl"
    if not q_path.exists():
        return
    rows = []
    def img_rows(q_id, typ, letter, imgs):
        for i in (imgs or []):
            f = i.get("file") or ""
            pg = keep_pages.get(f)
            rows.append({"q_id": q_id, "type": typ, "option_letter": letter,
                         "file": f,
                         "source_pages": ([pg] if pg is not None
                                          else (i.get("source_pages") or [])),
                         "extraction_page": pg})
    for r in _read_jsonl(q_path):
        img_rows(r["q_id"], "QUESTION", None, r.get("question_images"))
        for o in (r.get("options") or []):
            img_rows(r["q_id"], "OPTION", o.get("id"), o.get("images"))
    for r in _read_jsonl(s_path):
        img_rows(r["q_id"], "SOLUTION", None, r.get("solution_images"))
    _write_jsonl_atomic(ch_dir / "image_manifest.jsonl", rows)


def orphan_owner_qid(row):
    """The pipeline's best-guess owner for this orphan fragment, as a q_id."""
    qn = row.get("inferred_owner")
    cid = row.get("chapter_id")
    if qn in (None, "None") or not cid:
        return None
    try:
        return f"{cid}-{int(qn):03d}"
    except (TypeError, ValueError):
        return None


def orphan_key(row):
    """Stable identity for one orphan fragment row."""
    pages = row.get("new_pages") or row.get("pdf_pages") or []
    item = row.get("item")
    raw = json.dumps(item, sort_keys=True, default=str) if item else ""
    return hashlib.sha1((str(row.get("chapter_id")) + "|" + raw + "|"
                         + ",".join(map(str, pages))).encode()).hexdigest()[:16]


def _paragraphs_missing(frag_text, current_text):
    """Paragraph-wise split: which paragraphs of frag_text are NOT (already
    whitespace-normalized) inside current_text? Orphan fragments often PARTLY
    overlap an existing solution (head merged earlier, tail missing) -- the
    flat 'duplicate => refuse' guard then wrongly blocks adding the tail."""
    norm = lambda s: " ".join(s.lower().split())
    cur_n = norm(current_text or "")
    out = []
    for para in (frag_text or "").split("\n\n"):
        p = " ".join(para.split())
        if not p:
            continue
        if norm(p) not in cur_n:
            out.append(p)
    return out


def _frac(item):
    """Normalized (solution_text, tables) out of an orphan's Gemini item."""
    if isinstance(item, str):
        try:
            item = json.loads(item.replace("'", '"'))
        except Exception:
            item = None
    if not isinstance(item, dict):
        return "", []
    return str(item.get("solution_text") or "").strip(), (item.get("tables") or [])


def _retag_last_table(out, q_id, ttype):
    """Set the type of the most recently added solution table everywhere
    (keeps the fragment's own type instead of the generic 'human_added')."""
    _, _, files = _copies(out, q_id)
    if not files.get("master"):
        return
    for p in (files.get("master"), files.get("subjects"),
              files.get("chapter_file"), files.get("split_s")):
        if not p or not p.exists():
            continue
        rows = _read_jsonl(p)
        hit = False
        for r in rows:
            if r.get("id") != q_id and r.get("q_id") != q_id:
                continue
            tabs = ((r.get("solution") or {}).get("tables")
                    if "solution" in r else r.get("tables")) or []
            if tabs:
                tabs[-1]["type"] = ttype
                hit = True
        if hit:
            _write_jsonl_atomic(p, rows)


def apply_orphan_merge(output_root, chapter_id, frag_key, to_q_id,
                       reason="", by="human"):
    """Merge an orphan fragment INTO a question's solution (append, verified,
    ledgered). Detects the 'extra copy' case BEFORE writing: if the fragment
    text is already inside the target's solution, refuses with a clear
    message (approve/ignore never writes data; merge is the ONLY op that does)."""
    out = Path(output_root)
    target = _find_master_row(out, to_q_id)
    if target is None:
        return {"ok": False, "error": f"{to_q_id} not found in master"}
    hit = None
    for r in _read_jsonl(out / "data" / "orphans.jsonl"):
        if r.get("chapter_id") == chapter_id and orphan_key(r) == frag_key:
            hit = r
            break
    if hit is None:
        return {"ok": False, "error": "orphan fragment not found (already "
                "handled? refresh the queue)"}
    frag_text, frag_tables = _frac(hit.get("item"))
    if not frag_text and not frag_tables:
        return {"ok": False, "error": "fragment has no solution text or tables "
                "to merge -- nothing to adopt"}
    cur = str((target.get("solution") or {}).get("text") or "")
    norm_md = lambda s: " ".join(str(s or "").split()).lower()
    have_tables = {norm_md(t.get("markdown"))
                   for t in ((target.get("solution") or {}).get("tables") or [])}
    missing_tables = [t for t in frag_tables
                      if norm_md(t.get("markdown")) not in have_tables]
    missing_paras = []
    if frag_text:
        # Paragraph-wise merge: a fragment can PARTIALLY overlap the target
        # (earlier merges / recovery already added its head; the tail is new).
        # Rule: paragraphs already present are skipped, only the truly missing
        # tail is appended; if NOTHING is new (text fully inside, no tables),
        # refuse loudly instead of doubling content.
        missing_paras = _paragraphs_missing(frag_text, cur)
        total_paras = len([1 for p in frag_text.split("\n\n") if p.strip()])
        if not missing_paras and not missing_tables:
            return {"ok": False, "error": "ye text ALREADY is question ki "
                    "solution me maujood hai (extra copy) -- merge refused; "
                    "bas Ignore kar do"}
        new_text = (cur + "\n\n" + "\n\n".join(missing_paras).strip()).strip() \
            if missing_paras else cur
        if not new_text:
            new_text = cur
    else:
        new_text = cur
    _orphan_total_paras = total_paras if frag_text else 0
    res = apply_edit(out, to_q_id, "solution_text", new_text,
                     reason=reason or f"adopt orphan fragment pages "
                     f"{hit.get('new_pages') or hit.get('pdf_pages')}")
    if not res.get("ok"):
        return res
    note = None
    if frag_text and 0 < len(missing_paras) < _orphan_total_paras:
        note = (f"partial merge: {len(missing_paras)} missing "
                "paragraph(s) added, overlap skipped")
    adopted_tabs = 0
    for t in missing_tables:
        md = str(t.get("markdown") or "")
        if not md.strip():
            continue
        cur_tabs = ((_find_master_row(out, to_q_id).get("solution") or {})
                    .get("tables") or [])
        r2 = apply_edit(out, to_q_id, "table", md,
                        table_index=len(cur_tabs),
                        reason="adopted orphan table")
        if r2.get("ok"):
            _retag_last_table(out, to_q_id, t.get("type") or "human_added")
            adopted_tabs += 1
    _append_jsonl(out / "data" / HUMAN_EDIT_LEDGER, {
        "q_id": to_q_id, "field": "orphan_merge", "frag_key": frag_key,
        "old_len": len(cur), "fragment_pages": hit.get("new_pages")
        or hit.get("pdf_pages"), "tables_adopted": adopted_tabs,
        "reason": reason or "", "by": by, "ts": _now(), "note": note,
        "changed_files": res.get("changed_files", [])})
    return {"ok": True, "verified": res.get("verified"), "tables": adopted_tabs,
            "note": note,
            "changed_files": res.get("changed_files", [])}


def apply_image_op(output_root, q_id: str, op: str, file: str,
                   side: str = "solution", option_letter: str = None,
                   to_qid: str = None, reason: str = "") -> dict:
    """op: attach | detach | move. Full rules: contract 7."""
    out_root = Path(output_root)
    subject, chap, files = _copies(out_root, q_id)
    if not chap:
        return {"ok": False, "error": f"cannot parse q_id {q_id!r}"}
    side = (side or "solution").lower()
    if side not in ("question", "solution", "option"):
        return {"ok": False, "error": "side must be question/solution/option"}
    file = (file or "").strip()
    if op not in ("attach", "detach", "move"):
        return {"ok": False, "error": f"bad op {op!r}"}

    asset = _assets_rel(out_root) / file
    refs = _image_refs(out_root, file)

    if op == "detach":
        if not refs:
            return {"ok": False, "error": f"{file} is not attached anywhere"}
        changed = _update_image_lists(out_root, q_id, side, option_letter,
                                      file, remove=True)
        _rebuild_manifest(out_root, subject, chap, _ledger_page_map(out_root))
        _append_jsonl(out_root / "data" / HUMAN_EDIT_LEDGER, {
            "q_id": q_id, "field": "image", "op": "detach", "file": file,
            "side": side, "reason": reason or "", "ts": _now(),
            "note": "file kept on disk; only the link was removed",
            "changed_files": changed})
        return {"ok": True, "changed_files": changed}

    if op == "attach":
        if not asset.exists():
            return {"ok": False, "error": f"{file} not under assets/questions/"}
        if q_id in refs:
            return {"ok": False, "error": f"{file} already attached to {q_id}"}
        # an UNREFERENCED crop-style file takes the owner's slot name on
        # attach (locked naming in exports); shared files are never renamed
        eff = file
        if re.search(r"-p\d+-", file) and not refs:
            kind = _KIND_FOR[side]
            letter = f"{kind}_{option_letter.upper()}" if side == "option" else kind
            slot = _next_slot(out_root, subject, q_id, letter)
            new_name = f"{subject}/{q_id}_{letter}_{slot:02d}.webp"
            new_asset = _assets_rel(out_root) / new_name
            asset.rename(new_asset)
            pm = re.search(r"-p(\d+)-", file)
            _append_jsonl(out_root / "data" / "image_ownership.jsonl", {
                "subject": subject, "chapter_id": chap,
                "page": int(pm.group(1)) if pm else None,
                "file": file, "owner": q_id, "slot": side,
                "method": "human_edit",
                "evidence": f"human attached crop {file} (review queue)",
                "confidence": "high", "outcome": "claimed",
                "ts": _now(), "obj_id": None, "final_file": new_name})
            eff = new_name
        changed = _update_image_lists(out_root, q_id, side, option_letter,
                                      eff, add=True)
        _rebuild_manifest(out_root, subject, chap, _ledger_page_map(out_root))
        _append_jsonl(out_root / "data" / HUMAN_EDIT_LEDGER, {
            "q_id": q_id, "field": "image", "op": "attach", "file": file,
            "final_file": eff, "side": side, "reason": reason or "",
            "ts": _now(),
            "note": ("shared with " + ",".join(refs)) if refs else "fresh attach",
            "changed_files": changed})
        return {"ok": True, "changed_files": changed, "new_file": eff,
                "shared_with": [r for r in refs if r != q_id]}

    # ---- move ---------------------------------------------------------------
    if not to_qid:
        return {"ok": False, "error": "move needs to_qid"}
    if to_qid == q_id:
        return {"ok": False, "error": "to_qid is the current owner — nothing to move"}
    others = [r for r in refs if r != q_id]
    if others:
        return {"ok": False,
                "error": (f"{file} is SHARED with {others} (multi-draw). "
                          f"Blind move would break the other owner. Either "
                          f"detach here, or attach a copy to {to_qid} first.")}
    if q_id not in refs:
        return {"ok": False, "error": f"{file} is not attached to {q_id}"}
    to_subject, to_chap, _ = _copies(out_root, to_qid)
    if to_subject != subject or to_chap != chap:
        return {"ok": False, "error": "cross-chapter moves not allowed (attach "
                                      "on the target side instead)"}
    kind = _KIND_FOR[side]
    letter = f"{kind}_{option_letter.upper()}" if side == "option" else kind
    slot = _next_slot(out_root, subject, to_qid, letter)
    new_name = f"{subject}/{to_qid}_{letter}_{slot:02d}.webp"
    new_asset = _assets_rel(out_root) / new_name
    if not asset.exists():
        return {"ok": False, "error": f"{file} missing on disk — ledger says "
                                      f"attached but the bytes are gone"}
    old_pages = _ledger_page_map(out_root)
    asset.rename(new_asset)                    # 1. move bytes
    changed = _update_image_lists(out_root, q_id, side, option_letter, file,
                                  remove=True)
    changed += _update_image_lists(out_root, to_qid, side, option_letter,
                                   new_name, add=True)
    new_pages = dict(old_pages)
    new_pages[new_name] = old_pages.get(file)
    new_pages.pop(file, None)
    _rebuild_manifest(out_root, subject, chap, new_pages)
    # provenance chain continues: machine ledger learns the human's rename
    _append_jsonl(out_root / "data" / "image_ownership.jsonl", {
        "subject": subject, "chapter_id": chap,
        "page": old_pages.get(file), "file": file, "owner": to_qid,
        "slot": side, "method": "human_edit",
        "evidence": (f"human moved from {q_id}: {reason or 'review edit'}"),
        "confidence": "high", "outcome": "claimed",
        "ts": _now(), "obj_id": None, "final_file": new_name})
    _append_jsonl(out_root / "data" / HUMAN_EDIT_LEDGER, {
        "q_id": q_id, "field": "image", "op": "move", "file": file,
        "to_qid": to_qid, "new_file": new_name, "side": side,
        "reason": reason or "", "ts": _now(), "changed_files": changed})
    return {"ok": True, "new_file": new_name, "changed_files": changed}


# ---------------------------------------------------------------------------
# 6. Final zip gate + builder (contract 6)
# ---------------------------------------------------------------------------

def gate_final_zip(output_root) -> dict:
    q = collect_review_queue(output_root)
    return {
        "locked": not q["clear"],
        "open": q["counts"],
        "why": (None if q["clear"] else
                f"{q['counts']['blocker']} blocker + {q['counts']['review']} "
                f"review row(s) still open — final file locked until queue "
                f"is empty"),
    }


def build_final_zip(output_root, dest=None):
    """split/ + referenced assets + chapters.json + FORMAT.md + receipt.
    REFUSES while the queue is open (contract 6). Returns dict with path."""
    out_root = Path(output_root)
    gate = gate_final_zip(out_root)
    if gate["locked"]:
        return {"ok": False, "locked": True, "why": gate["why"]}

    dest = Path(dest or (out_root / "final_export.zip"))
    receipts = _load_decisions(out_root)
    edits = _read_jsonl(out_root / "data" / HUMAN_EDIT_LEDGER)

    referenced = set()
    manifest_files = []
    split_root = out_root / "split"
    subjects = set()
    if split_root.exists():
        for mf in sorted(split_root.glob("*/*/image_manifest.jsonl")):
            manifest_files.append(mf)
            subjects.add(mf.parts[-3])
            for row in _read_jsonl(mf):
                if row.get("file"):
                    referenced.add(row["file"])

    receipt = {
        "built_at": _now(), "output_root": out_root.name,
        "chapters": len(manifest_files), "subjects": sorted(subjects),
        "images_shipped": len(referenced),
        "review_decisions": len(receipts),
        "human_edits": len(edits),
        "gate": "queue clear — built only after every flag was resolved",
    }

    fm = Path(__file__).with_name("FORMAT.md")
    # slim whitelist (contract: delivery package, not the workshop)
    SPLIT_KEEP = {"questions.jsonl", "answers.jsonl", "solutions.jsonl",
                  "image_manifest.jsonl", "chapter_completeness.json"}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("REVIEW_RECEIPT.json",
                   json.dumps(receipt, indent=2, ensure_ascii=False))
        if fm.exists():
            z.write(fm, "FORMAT.md")
        if split_root.exists():
            for p in sorted(split_root.rglob("*")):
                if p.is_file() and p.name in SPLIT_KEEP:
                    z.write(p, str(p.relative_to(out_root)))
        for cj in sorted((out_root / "subjects").glob("*/chapters.json")) \
                if (out_root / "subjects").exists() else []:
            z.write(cj, str(cj.relative_to(out_root)))
        aroot = out_root / "assets" / "questions"
        for rel in sorted(referenced):
            p = aroot / rel
            if p.exists():
                z.write(p, str(Path("assets") / "questions" / rel))
    return {"ok": True, "path": str(dest), "receipt": receipt,
            "images_shipped": len(referenced)}
