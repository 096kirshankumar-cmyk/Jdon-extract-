#!/usr/bin/env python3
"""flag_verifier.py -- AI-verified false-flag reduction pass (user spec).

HARD CONTRACT (do not relax):
  0. ZERO impact on extraction: separate key pool (GEMINI_VERIFY_API_KEY_1..N),
     separate model (GEMINI_VERIFY_MODEL), separate daily budget shown on the
     dashboard ("verification (3.1) calls used today: X/3000"). The 3.5 pool
     is never touched by this module.
  1. CONTENT LOCK: this module NEVER writes stem/options/correct_option/
     solution_text/attached_images. It can only READ content and WRITE flag
     decisions (open/closed). No exception -- look at the imports.
  2. DOUBLE GATE: a flag is auto-resolved ONLY when the answer parses AND
     confidence == "high" AND flag_genuine is False. Parse failure, low/medium
     confidence, or "genuine" => row stays open (a note can be attached to it).
  3. Chapter-level / fragment-level flags that have no concrete extracted
     item are NOT verifiable here -- they are skipped (kept for humans).
  4. Reversible + traceable: closes write a review_decisions.jsonl row with
     action "ai_auto_resolved" AND a data/ai_auto_resolved.jsonl audit row;
     the dashboard tab lets any of them be re-opened (action "reopened").
  5. Self-audit sampling: a deterministic slice (flag_key hash % 15 == 0,
     ~6.7%) never auto-resolves even on high-confidence false; it is noted in
     the audit log as sampled_back so AI bias shows up to the human.
  6. If the verify pool is not configured, EVERYTHING is skipped politely.
"""
import base64
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

VERIFY_MODEL = os.environ.get("GEMINI_VERIFY_MODEL", "gemini-3.1-flash-lite")
VERIFY_DAILY_CAP_PER_KEY = 480
AUDIT_LOG = "ai_auto_resolved.jsonl"
COUNTER_FILE = "verify_counter.json"


# ---------------------------------------------------------------------------
# The exact prompt from the spec. DO NOT paraphrase.
# ---------------------------------------------------------------------------
VERIFY_PROMPT = """System context: Tum ek medical exam QBank ki extraction quality verify kar rahe ho.
Ek AI pipeline ne is textbook page se ek question/solution extract kiya tha, aur ek
automated validator ne is extraction pe ek flag raise kiya. Tumhara kaam sirf itna hai:
page image dekho aur batao ye flag GENUINE hai ya FALSE.

PAGE IMAGE: [attached -- rendered PDF page]

EXTRACTED OUTPUT (jo pipeline ne nikala):
Question: {stem}
Options: A. {opt_a} | B. {opt_b} | C. {opt_c} | D. {opt_d}
Correct Answer: {correct_option}
Solution: {solution_text}
Attached image(s): {attached_images}

FLAG RAISED: {flag_kind}
Flag detail: {flag_detail}

Task: Page image ko dhyan se dekho. Extracted output se compare karo.
Sirf is specific flag ke baare me faisla karo -- kya flag genuine hai (matlab
page pe waaki koi problem hai jo output me reflect ho rahi hai), ya false hai
(output actually sahi hai, flag galat trigger hua).

Rules:
- Agar page pe figure/diagram hai lekin output me attach nahi hai -> flag genuine.
- Agar page pe koi figure nahi hai (sirf text) -> flag false (decorative/no-figure case).
- Agar solution text page ke content se match karta hai (chahe wording thoda alag ho) -> flag false.
- Agar options ya answer page se mismatch karte hain -> flag genuine.
- Agar tumhe confidently pata nahi chal raha (page unclear, ambiguous, ya partial visible) -> confidence "low" do, flag_genuine ka koi bhi guess mat do jo overconfident lage.

ONLY respond in this exact JSON format, nothing else:
{{
  "flag_genuine": true or false,
  "reason": "<one line, specific -- quote what you saw on the page or what's missing>",
  "confidence": "high" or "medium" or "low"
}}"""


class VerifyPoolError(RuntimeError):
    pass


def _discover_verify_keys(env=None):
    env = os.environ if env is None else env
    keys = []
    raw = env.get("GEMINI_VERIFY_API_KEYS", "")
    for c in raw.replace("\n", ",").split(","):
        c = c.strip().strip('"')
        if c:
            keys.append(c)
    for i in range(1, 9):
        v = env.get(f"GEMINI_VERIFY_API_KEY_{i}", "")
        if v.strip():
            keys.append(v.strip())
    seen, uniq = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _daily_counter(out_root):
    p = Path(out_root) / "data" / COUNTER_FILE
    today = time.strftime("%Y-%m-%d")
    d = {"day": today, "calls": 0}
    if p.exists():
        try:
            d = json.loads(p.read_text())
            if d.get("day") != today:
                d = {"day": today, "calls": 0}
        except Exception:
            pass
    return p, d


def _quota_left(out_root):
    keys = _discover_verify_keys()
    _p, d = _daily_counter(out_root)
    cap = VERIFY_DAILY_CAP_PER_KEY * max(1, len(keys))
    return max(0, cap - int(d.get("calls", 0))), cap, len(keys)


def render_page_png(pdf_path, page, dpi=110):
    """Single-page PNG via the pipeline's poppler install (zero tokens).
    Returns bytes or None."""
    import tempfile
    out_prefix = Path(tempfile.mkdtemp(prefix="fv_page_")) / "pg"
    try:
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r",
                        str(dpi), "-png", "-singlefile",
                        str(pdf_path), str(out_prefix)],
                       capture_output=True, timeout=45, check=True)
    except Exception:
        return None
    png = out_prefix.with_suffix(".png")
    return png.read_bytes() if png.exists() else None


def pick_pages(output_root, flag, master_row=None):
    """The single most-likely source page for this flag (max 2 to keep the
    call narrow and cheap). Flag detail page-lists win, then the row's
    source_pages."""
    detail = str(flag.get("detail") or "")
    m = re_search_pages(detail)
    if m:
        return m[:2]
    pages = [p for p in ((master_row or {}).get("source_pages") or [])
             if isinstance(p, int)]
    return pages[:2] or pages[-2:]


def re_search_pages(text):
    import re
    m = re.search(r"(?:pdf_pages|new_pages|pages|on)\s*\[([0-9,\s]+)\]", text)
    if m:
        return [int(x) for x in m.group(1).split(",") if x.strip().isdigit()]
    return []


def flag_to_payload(output_root, flag, master_row=None):
    """Build the narrow payload. None => this flag is NOT verifiable here
    (chapter-level/orphan fragments stay human, by contract 3)."""
    q_id = flag.get("q_id")
    if not q_id or master_row is None:
        return None
    q = master_row.get("question") or {}
    s = master_row.get("solution") or {}
    opts = master_row.get("options") or []
    texts = {o.get("id"): str(o.get("text") or "") for o in opts
             if isinstance(o, dict)}
    imgs = []
    for holder in (q, s):
        for i in (holder.get("images") or []):
            if isinstance(i, dict) and i.get("file"):
                imgs.append(i["file"])
    for o in opts:
        for i in (o.get("images") or []):
            if isinstance(i, dict) and i.get("file"):
                imgs.append(i["file"])
    return {
        "stem": str(q.get("text") or "")[:700],
        "opt_a": texts.get("A", "")[:200], "opt_b": texts.get("B", "")[:200],
        "opt_c": texts.get("C", "")[:200], "opt_d": texts.get("D", "")[:200],
        "correct_option": (master_row.get("correct_options") or [None])[0],
        "solution_text": str(s.get("text") or "")[:1200],
        "attached_images": ", ".join(imgs) or "none",
        "flag_kind": flag.get("kind") or "",
        "flag_detail": str(flag.get("detail") or "")[:400],
        "q_id": q_id,
    }


def build_prompt(payload):
    return VERIFY_PROMPT.format(**payload)


def classify_response(text):
    """Strict JSON-only parse. Returns dict or None (None == inconclusive,
    the caller keeps the flag open; contract 2)."""
    if not text:
        return None
    import re
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict) or "flag_genuine" not in d or "confidence" not in d:
        return None
    if d.get("confidence") not in ("high", "medium", "low"):
        return None
    return {"flag_genuine": bool(d.get("flag_genuine")),
            "reason": str(d.get("reason") or "")[:300],
            "confidence": d["confidence"]}


def _call_verify_model(png_bytes, prompt, key):
    """One content call on the VERIFY pool (separate model env), JSON out."""
    import google.generativeai as genai
    genai.configure(api_key=key)
    model = genai.GenerativeModel(VERIFY_MODEL)
    b64 = base64.b64encode(png_bytes).decode()
    resp = model.generate_content([
        {"mime_type": "image/png", "data": b64},
        prompt,
    ], generation_config={"temperature": 0.1})
    return getattr(resp, "text", None) or None


def run_verification(output_root, *, books_dir="/data/input_pdfs",
                     pages_provider=None, call_model=None,
                     max_calls_today=None, progress=None):
    """Walk every OPEN queue row; per spec the pass closes only high-conf
    FALSE flags. Everything else stays open. Returns a summary dict."""
    import review_queue as rq
    out_root = Path(output_root)
    keys = _discover_verify_keys()
    if not keys:
        return {"ok": False, "skipped": True,
                "error": "verify pool not configured (set GEMINI_VERIFY_API_KEY_1..N)"}
    left, cap, nkeys = _quota_left(out_root)
    if left <= 0:
        return {"ok": False, "skipped": True,
                "error": f"verify daily budget spent ({cap}/day over {nkeys} key(s))"}
    if call_model is None:
        call_model = _call_verify_model
    q = rq.collect_review_queue(out_root)
    open_rows = q["rows"]
    checked = resolved = kept = sampled = failed_parse = 0
    ki = 0
    audit_rows = []
    for r in open_rows:
        if left <= 0:
            break
        qid = r.get("q_id")
        mrow = rq._find_master_row(out_root, qid) if qid else None
        if mrow is None:
            continue                          # chapter-level: kept for humans
        payload = flag_to_payload(out_root, r, mrow)
        if payload is None:
            continue
        pages = pick_pages(out_root, r, mrow)
        if not pages:
            continue
        pdf = Path(books_dir) / f"{mrow.get('subject')}.pdf"
        if not pdf.exists():
            continue
        png = render_page_png(pdf, pages[0])
        if not png:
            continue
        # deterministic self-audit: ~1/15 always stays for a human spot-check
        sample_back = (int(r["flag_key"], 16) % 15) == 0
        try:
            raw = call_model(png, build_prompt(payload), keys[ki % len(keys)])
        except Exception as e:
            kept += 1
            continue
        left -= 1
        checked += 1
        _p, d = _daily_counter(out_root)
        d["calls"] = int(d.get("calls", 0)) + 1
        _p.write_text(json.dumps(d))
        v = classify_response(raw)
        if v is None:
            failed_parse += 1
            continue
        if sample_back:
            sampled += 1
            audit_rows.append({"q_id": qid, "flag_key": r["flag_key"],
                               "flag_kind": r["kind"], "ai_reason": v["reason"],
                               "confidence": v["confidence"], "pages": pages,
                               "sampled_back": True, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
            continue
        if v["confidence"] == "high" and v["flag_genuine"] is False:
            rq.record_decision(out_root, r["flag_key"], "ai_auto_resolved",
                               reason=f"{v['reason']} (ai, {v['confidence']} conf)",
                               q_id=qid)
            resolved += 1
            audit_rows.append({"q_id": qid, "flag_key": r["flag_key"],
                               "flag_kind": r["kind"], "ai_reason": v["reason"],
                               "confidence": v["confidence"], "pages": pages,
                               "sampled_back": False, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        else:
            kept += 1
        if progress:
            progress(checked, resolved)
    audit_path = out_root / "data" / AUDIT_LOG
    for a in audit_rows:
        with open(audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")
    return {"ok": True, "checked": checked, "resolved": resolved,
            "kept": kept, "sampled_back": sampled, "parse_failed": failed_parse,
            "calls_used_after": int((_daily_counter(out_root)[1] or {}).get("calls", 0)),
            "budget_cap": cap}


def list_ai_resolved(output_root):
    """The '✅ Auto-resolved by AI (N)' tab feed: pure read of the audit log."""
    import review_queue as rq
    return rq._read_jsonl(Path(output_root) / "data" / AUDIT_LOG)
