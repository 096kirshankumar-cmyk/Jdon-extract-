#!/usr/bin/env python3
"""
Phone-friendly dashboard for the QBank pipeline.
Upload a PDF (or paste a link), tap Run, watch progress, download results —
all from a phone browser. No terminal/PC needed once this is deployed.

This is just a thin UI wrapper. All the real extraction logic (watermark
detection, Gemini vision calls, checkpointing, rate-limit handling) lives
in qbank_pipeline.py — this file does not duplicate or replace any of that.
"""

import hashlib
import ipaddress
import json as _json
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import requests
from flask import Flask, render_template_string, request, redirect, url_for, send_file, jsonify
from werkzeug.utils import secure_filename

import qbank_pipeline as pipeline
import gemini_keys
import master_review_export  # MASTER_REVIEW/ package builder (read-only)
import review_queue          # human review engine (union of ALL flag sources; offline)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB per PDF upload

UPLOAD_DIR = Path("./pdfs")
UPLOAD_DIR.mkdir(exist_ok=True)

# Resolved ONCE at import (stable absolute path): the smoke-test zip writer
# and its download route must agree on the location no matter what cwd the
# server process has at request time.
TEST_ZIP_PATH = Path("test_results.zip").resolve()
UPLOAD_DIR.mkdir(exist_ok=True)

state_lock = threading.Lock()
state = {"status": "idle", "log": [], "error": None}


def try_mark_processing():
    """AUDIT-FIX: the busy-guard used to CHECK state['status'] outside the
    lock and SET it later inside -- two concurrent taps both started
    pipelines writing the same files (double Gemini spend, racing chapter
    rewrites). Check-and-set must be one atomic step."""
    with state_lock:
        if state["status"] == "processing":
            return False
        state["status"] = "processing"
        return True


# ---- AUDIT-FIX: SSRF-hardened PDF fetching -------------------------------
# The old /run-url fetched ANY user-supplied URL with redirects followed and
# no size/time cap: cloud metadata endpoints (169.254.169.254), loopback,
# private networks and slow-drip responses were all reachable, and the
# 200MB MAX_CONTENT_LENGTH only applied to multipart uploads, never to the
# URL download. fetch_pdf_guarded validates scheme + resolved IPs on EVERY
# redirect hop, caps total bytes and total wall time, and verifies the %PDF
# magic before the file is kept.
_BLOCKED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
    "::1/128", "fc00::/7", "fe80::/10"))

PDF_URL_MAX_BYTES = 300 * 1024 * 1024   # URL downloads (uploads stay at 200MB)
PDF_URL_MAX_SECONDS = 600


def _url_block_reason(url):
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return f"scheme {p.scheme!r} not allowed"
    host = p.hostname
    if not host:
        return "no hostname in URL"
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return f"hostname {host!r} does not resolve"
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return f"unparseable resolved address {ip!r}"
        for net in _BLOCKED_NETS:
            if addr in net:
                return (f"blocked: {host} resolves to private/loopback/"
                        f"link-local address {ip}")
    return None


def fetch_pdf_guarded(url, dest, max_bytes=PDF_URL_MAX_BYTES,
                      deadline_s=PDF_URL_MAX_SECONDS):
    """Download `url` to `dest` with SSRF + resource-exhaustion guards.
    Raises ValueError with a user-readable reason on any violation.
    NOTE (residual risk): DNS is validated immediately before connect; an
    attacker-controlled DNS could rebind afterwards. The residual window is
    tiny; fully closing it needs per-connection IP pinning (custom adapter)."""
    cur = url
    for _hop in range(4):
        blocked = _url_block_reason(cur)
        if blocked:
            raise ValueError(blocked)
        r = requests.get(cur, stream=True, timeout=60, allow_redirects=False,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            r.close()
            if not loc:
                raise ValueError("redirect without Location header")
            cur = requests.compat.urljoin(cur, loc)
            continue
        r.raise_for_status()
        # Google Drive big-file interstitial: sniff a BOUNDED prefix (the old
        # code did r.text -- buffering the whole HTML page into memory).
        if "text/html" in r.headers.get("Content-Type", ""):
            head = r.raw.read(65536, decode_content=True)
            r.close()
            m = _re.search(rb"confirm=([0-9A-Za-z_-]+)", head)
            if not m:
                raise ValueError("link returned a webpage, not a PDF")
            cur = (f"{cur}{'&' if '?' in cur else '?'}confirm="
                   f"{m.group(1).decode()}")
            continue
        t_end = time.time() + deadline_s
        total, first = 0, None
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                if first is None:
                    first = chunk
                f.write(chunk)
                total += len(chunk)
                if total > max_bytes:
                    r.close()
                    dest.unlink(missing_ok=True)
                    raise ValueError(
                        f"download exceeded the {max_bytes // (1024 * 1024)}MB cap")
                if time.time() > t_end:
                    r.close()
                    dest.unlink(missing_ok=True)
                    raise ValueError("download exceeded the time budget")
        r.close()
        if not first or not first.startswith(b"%PDF"):
            dest.unlink(missing_ok=True)
            raise ValueError("downloaded content is not a real PDF file")
        return dest
    raise ValueError("too many redirects")


# ---- AUDIT-FIX: source PDFs persist on the /data volume ------------------
# Uploads/downloads previously lived only in ./pdfs (ephemeral container
# fs). A redeploy wiped them while state.json kept chapters_done, so resume
# and --recover silently broke (or, worse, a DIFFERENT edition uploaded with
# the same subject code was silently paired with the old state). The source
# now persists under /data/input_pdfs with a sha256 identity record.
INPUT_META_DIR = Path("/data/input_pdfs")


def persist_input_pdf(pdf_path, subject_code, page_offset):
    """Copy the source PDF to the /data volume and record its identity.
    Returns the volume path (str) to use for the run, or None if no volume.
    Also REFUSES (returns a dict {"error": ...}) when a different PDF is
    being paired with an in-progress state for the same subject."""
    try:
        digest = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    except OSError as e:
        return {"error": f"could not read uploaded PDF: {e}"}
    meta_path = INPUT_META_DIR / f"{subject_code}.json"
    if INPUT_META_DIR.parent.exists() and Path("/data").is_mount():
        INPUT_META_DIR.mkdir(parents=True, exist_ok=True)
        if meta_path.exists():
            try:
                old = _json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                old = {}
            old_hash = old.get("sha256")
            if old_hash and old_hash != digest:
                st = {}
                try:
                    st = _json.loads(pipeline.STATE_FILE.read_text())
                except Exception:
                    pass
                done = (st.get("pdf_progress", {}).get(subject_code, {})
                        .get("chapters_done") or [])
                if done:
                    return {"error": (
                        f"A DIFFERENT PDF was uploaded for {subject_code} "
                        f"while {len(done)} chapter(s) are already extracted "
                        f"(hash mismatch). Rules me ye pairing bilkul galat "
                        f"hai -- pehle RESET karo ya wahi asli PDF dobara do.")}
        dest = INPUT_META_DIR / f"{subject_code}.pdf"
        shutil.copyfile(pdf_path, dest)
        try:
            from pypdf import PdfReader as _PR
            n_pages = len(_PR(str(dest)).pages)
        except Exception:
            n_pages = None
        meta_path.write_text(_json.dumps({
            "subject": subject_code, "sha256": digest, "path": str(dest),
            "size_bytes": dest.stat().st_size, "page_offset": page_offset,
            "total_pages": n_pages,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2),
            encoding="utf-8")
        return {"path": str(dest)}
    return None

def log(msg):
    print(msg, flush=True)  # so it shows in Railway's Deploy Logs too, not just the dashboard box
    with state_lock:
        state["log"].append(msg)
        if len(state["log"]) > 500:
            state["log"].pop(0)

def run_validator_and_log():
    """Zero-token deterministic validation; every flag printed to the
    dashboard log box so no terminal is needed. Returns the report dict."""
    import qbank_validator
    try:
        provider = qbank_validator.default_page_text_provider(INPUT_META_DIR)
    except Exception:
        provider = None
    rep = qbank_validator.run_hybrid(pipeline.OUTPUT_ROOT, audit=False,
                                     page_text_provider=provider)
    s = rep["summary"]
    log(f"🧪 Validator: {s['flags_total']} flag(s) across "
        f"{s['flagged_chapters']}/{s['chapters']} chapters ({s['questions']} questions)")
    for kind, n in sorted(s.get("flags_by_kind", {}).items(), key=lambda kv: -kv[1]):
        log(f"   • {kind}: {n}")
    for cid, flags in (rep.get("chapters") or {}).items():
        for f in flags:
            log(f"   [{f.get('severity', '?')}] {cid} {f.get('q_no') or '-'} "
                f"{f.get('kind')}: {str(f.get('detail', ''))[:100]}")
    log("🧪 Full report -> data/validation_report.json (inside the zip)")
    return rep

def _find_input_pdf():
    """The source PDF after persistence: prefer the Volume copy (survives
    redeploys), else the ./pdfs upload dir, else None."""
    try:
        vol = Path("/data/input_pdfs")
        if vol.exists():
            pdfs = sorted(vol.glob("*.pdf"), key=lambda p: -p.stat().st_mtime)
            if pdfs:
                return pdfs[0]
    except Exception:
        pass
    local = sorted(UPLOAD_DIR.glob("*.pdf"), key=lambda p: -p.stat().st_mtime) \
        if UPLOAD_DIR.exists() else []
    return local[0] if local else None


def _write_review_digests():
    """Generate per-subject review digests into <OUTPUT_ROOT>/review_digest/
    (they land in output_results.zip via make_zip -- the masterdata the user
    downloads). Runs after every successful run/fix/validate. Zero tokens.
    Never raises: a digest failure must not hurt extraction."""
    try:
        import review_digest
        out = Path(pipeline.OUTPUT_ROOT)
        if not (out / "data" / "questions.jsonl").exists():
            return
        pdf_path = _find_input_pdf()
        ranges = None
        if pdf_path:
            try:
                toc = pipeline.extract_toc_chapters(str(pdf_path))
                meta = {}
                try:
                    meta = _json.loads((INPUT_META_DIR / f"{pdf_path.stem.split('-')[0]}.json"
                                        ).read_text()) if INPUT_META_DIR.exists() else {}
                except Exception:
                    meta = {}
                offset = int(meta.get("page_offset", -1))
                total = len(pipeline.PdfReader(str(pdf_path)).pages)
                chs = pipeline.compute_page_ranges(toc, offset, total)
                ranges = [(c["chapter_no"], (c["file_start"], c["file_end"]))
                          for c in chs]
            except Exception as re_:
                log(f"📑 digest: TOC scan skipped ({re_}) -- key-awareness off; "
                    "blockers still detected")
        res = review_digest.build_digest(out, None,
                                         str(pdf_path) if pdf_path else None,
                                         ranges)
        for sub, body in res.items():
            import re as _r2
            n_blk = _r2.search(r"BLOCKER: (\d+)", body)
            n_rev = _r2.search(r"REVIEW: (\d+)", body)
            n_noi = _r2.search(r"NOISE: (\d+)", body)
            log(f"📑 review digest {sub}: {n_blk.group(1) if n_blk else '?'} blocker(s), "
                f"{n_rev.group(1) if n_rev else '?'} review, "
                f"{n_noi.group(1) if n_noi else '?'} noise -> "
                f"review_digest/{sub}.md (zip me included)")
    except Exception as e:
        log(f"⚠️ review digest skipped (extraction unaffected): {e}")


def run_pipeline_thread(subject_code, pdf_path, page_offset):
    if VOLUME_WARN:
        # HARD BLOCK: extraction without a mounted Volume = the whole output
        # silently evaporates on the next redeploy (burned once already).
        # Refuse to burn a single Gemini call in that state.
        with state_lock:
            state["status"] = "failed"
            state["error"] = "Volume not attached at /data"
        log("❌ RUN BLOCKED: Volume /data pe attach nahi hai -- is run ka data "
            "redeploy pe udd jayega. Railway → Service → Settings → Volumes → "
            "New Volume (Mount Path: /data) lagao, phir Run dabao.")
        return
    with state_lock:
        state["status"] = "processing"
        state["error"] = None
    try:
        if not gemini_keys.discover_keys():
            raise RuntimeError(
                "No Gemini API key configured in Railway variables "
                "(set GEMINI_API_KEYS, GEMINI_API_KEY_1..N, or GEMINI_API_KEY)")
        # AUDIT-FIX: persist the source PDF onto the /data volume BEFORE any
        # Gemini call, so quota resumes and recoveries survive redeploys.
        # Also refuse pairing a DIFFERENT book with this subject's existing
        # extraction state (hash mismatch) -- that silently misaligned every
        # page number in the old output.
        persisted = persist_input_pdf(pdf_path, subject_code, page_offset)
        if isinstance(persisted, dict) and persisted.get("error"):
            with state_lock:
                state["status"] = "failed"
                state["error"] = persisted["error"]
            log(f"❌ RUN BLOCKED: {persisted['error']}")
            return
        if isinstance(persisted, dict) and persisted.get("path"):
            pdf_path = persisted["path"]
            log(f"💾 Source PDF saved to the Volume at {pdf_path} (redeploy-safe).")
        pipeline.PDFS[:] = [{"subject": subject_code, "path": str(pdf_path), "page_offset": page_offset}]
        pipeline.main()
        # zero-token deterministic validation right after every run -- the
        # defect map (numbering gaps, RC-4-aware missing solutions, orphan /
        # unmatched-image sidecars) lands in data/validation_report.json.
        try:
            import qbank_validator
            try:
                provider = qbank_validator.default_page_text_provider(INPUT_META_DIR)
            except Exception:
                provider = None
            rep = qbank_validator.run_hybrid(pipeline.OUTPUT_ROOT, audit=False,
                                             page_text_provider=provider)
            log(f"🧪 Validation: {rep['summary']['flags_total']} flag(s) across "
                f"{rep['summary']['flagged_chapters']}/{rep['summary']['chapters']} chapters → "
                f"see data/validation_report.json")
        except Exception as ve:
            log(f"⚠️ post-run validation report failed (extraction unaffected): {ve}")
        with state_lock:
            state["status"] = "completed"
        log("✅ Done (or paused at daily Gemini limit — tap Run again tomorrow to resume).")
        _write_review_digests()   # per-book review page(s) land in the zip
        make_zip()
        # After a successful run, also build the MASTER_REVIEW package
        # (read-only copy of split/ + assets/ + data/) so it's ready
        # for download alongside output_results.zip. Build failures
        # are LOGGED but do NOT fail the run -- the live output is
        # the source of truth, MASTER_REVIEW is just a convenience
        # view for the human reviewer.
        try:
            master_review_export.build_master_review_zip(Path(pipeline.OUTPUT_ROOT))
            log("📒 MASTER_REVIEW package ready — NOTE: ye REVIEW KA PACKAGE hai "
                "(review ho CHUKA nahi hai abhi). Pehle 📋 Review queue khol ke "
                "sab rows decide karo; queue clear hone par hi 🚀 Final zip bane.ga.")
        except Exception as mre:
            log(f"⚠️ MASTER_REVIEW build skipped (live output unaffected): {mre}")
    except SystemExit:
        with state_lock:
            state["status"] = "paused"
        log("⏸ Hit daily Gemini call limit — progress saved. Come back tomorrow and tap Run again.")
        make_zip()
    except Exception as e:
        with state_lock:
            state["status"] = "failed"
            state["error"] = str(e)
        log(f"❌ Error: {e}")
        traceback.print_exc()  # full traceback with file/line -> Railway Deploy Logs

def _entries_to_archive(out_root):
    """Everything under the output root that a reset must move into the
    archive -- i.e. EVERYTHING except the archive itself. The old reset only
    moved data/assets/state.json and left subjects/ behind, so a previous
    book's per-subject chapter JSONs + questions.jsonl kept leaking into the
    next export zip after reset. Generalized: nothing from a previous run may
    survive into the fresh output."""
    if not out_root.exists():
        return []
    return [(p.name, p) for p in sorted(out_root.iterdir())
            if p.name != "_archive"]


def _zip_skip(rel):
    """True when a file (path relative to the output root) must NOT go into
    the export zip: healer backups, the reset archive, and the zip itself
    (self-inclusion guard). Everything else -- data/, assets/, subjects/,
    state.json -- is current-run content."""
    parts = rel.parts
    if not parts:
        return True
    if parts[0] == "_archive":
        return True
    if ".bak-" in Path(rel).name:
        return True
    if Path(rel).name == "output_results.zip":
        return True
    return False


def make_zip():
    # Use the pipeline's live root rather than recalculating OUTPUT_DIR.  This
    # keeps an export paired with the data the pipeline actually wrote.
    out = Path(pipeline.OUTPUT_ROOT)
    if not out.exists():
        return
    zpath = Path("output_results.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in out.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(out)
            if _zip_skip(rel):
                continue  # archive / backups / self -- never export these
            zf.write(f, f.relative_to(out.parent))

PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QBank Extractor</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 p-4">
<div class="max-w-lg mx-auto space-y-4">
  <h1 class="text-xl font-bold">QBank Extractor</h1>

  {% if vol_warn %}
  <div class="bg-red-600 text-white rounded-lg shadow p-4 text-sm font-bold">
    {{ vol_warn }}
  </div>
  {% endif %}

  <div class="bg-violet-700 text-white rounded-lg shadow p-3 text-sm font-bold">
    🧪 V2 — Multi-Phase 3-Pass pipeline (Q/A/S). Iska output ALAG root me jata hai — v1 data ko koi asar nahi.
  </div>

  <div class="bg-white rounded-lg shadow p-4">
    <p class="text-sm mb-2">Status: <span class="font-semibold">{{ state.status }}</span></p>
    {% if state.error %}<p class="text-red-600 text-sm">{{ state.error }}</p>{% endif %}
    <a href="/download" class="inline-block mt-2 bg-emerald-600 text-white text-sm px-3 py-2 rounded">Download results (.zip)</a>
    <a href="/download-master-review" class="inline-block mt-2 bg-amber-600 text-white text-sm px-3 py-2 rounded">📒 Download MASTER_REVIEW (.zip)</a>
    <a href="/review" class="inline-block mt-2 bg-rose-600 text-white text-sm px-3 py-2 rounded">🧑‍⚖️ Review queue (manual edit)</a>
    <a href="/download-final" class="inline-block mt-2 bg-indigo-600 text-white text-sm px-3 py-2 rounded">🚀 Final zip (review-gated)</a>
    {% if state.get('test_ready') %}
    <a href="/download-test" class="inline-block mt-2 bg-violet-600 text-white text-sm px-3 py-2 rounded">⬇️ Download TEST results (.zip)</a>
    {% endif %}
  </div>

  <div class="bg-white rounded-lg shadow p-4 border-2 border-amber-400 space-y-2">
    <p class="text-xs font-bold text-amber-700 uppercase">Maintenance — no terminal needed</p>
    <form action="/fix" method="POST">
      <button class="w-full bg-amber-500 text-white font-bold py-2 rounded" {% if state.status == 'processing' %}disabled{% endif %}>
        🩹 Fix data (heal known defects)
      </button>
    </form>
    <form action="/validate" method="POST">
      <button class="w-full bg-sky-600 text-white font-bold py-2 rounded" {% if state.status == 'processing' %}disabled{% endif %}>
        🔍 Check data (validator report)
      </button>
    </form>
    <a href="/data-status" class="block text-center w-full bg-slate-200 text-slate-800 font-bold py-2 rounded">📦 Data status (is my file safe?)</a>
    <form action="/restore-drive" method="POST" class="pt-2 border-t space-y-2">
      <label class="block text-xs font-semibold mb-1">Google Drive backup folder link (waapas lane ke liye)</label>
      <input type="text" name="folder" value="1ZOKiB1TTFXTeiGkTPQq6SkKcrDa9GQxp" class="w-full text-xs border p-2 rounded">
      <button class="w-full bg-violet-600 text-white font-bold py-2 rounded" {% if state.status == 'processing' %}disabled{% endif %}>
        ♻️ Restore data from Drive
      </button>
    </form>
    <details class="pt-2 border-t">
      <summary class="text-xs font-semibold cursor-pointer">Or upload output_results.zip (if saved on phone)</summary>
      <form action="/restore-zip" method="POST" enctype="multipart/form-data" class="space-y-2 mt-2">
        <input type="file" name="file" accept=".zip" class="w-full text-sm border p-2 rounded">
        <button class="w-full bg-slate-800 text-white font-bold py-2 rounded" {% if state.status == 'processing' %}disabled{% endif %}>Restore from zip</button>
      </form>
    </details>
    <p class="text-xs text-gray-500">Order: <b>Restore</b> (data waapas) → <b>🩹 Fix</b> → <b>🔍 Check</b>. Har step ke baad black log box ka screenshot bhejo.</p>
  </div>

  <div class="bg-white rounded-lg shadow p-4 border-2 border-red-500 space-y-2">
    <p class="text-xs font-bold text-red-700 uppercase">Danger zone — new book ke liye clean slate</p>
    <p class="text-xs text-gray-600">Purane output ko volume ke andar <b>_archive/</b> folder me move karke fresh start karta hai (delete NAHI hota — waapas la sakte ho). Zip me archive include nahi hota.</p>
    <form action="/reset" method="POST" class="space-y-2"
          onsubmit="return this.confirm.value === 'RESET' ? true : (alert('Box me RESET likho'), false);">
      <input type="text" name="confirm" placeholder="Yahan RESET likho" class="w-full text-sm border p-2 rounded">
      <button class="w-full bg-red-600 text-white font-bold py-2 rounded" {% if state.status == 'processing' %}disabled{% endif %}>
        🧹 Reset output (pehle sab archive hota hai)
      </button>
    </form>
  </div>

  <div class="bg-white rounded-lg shadow p-4 border-2 border-violet-500 space-y-3">
    <p class="text-xs font-bold text-violet-700 uppercase">🧪 V2 smoke test — sirf 1 chapter</p>
    <p class="text-xs text-gray-600">Full book lagane se pehle ek chapter test karo. Output <b>_v2test/</b> folder me jata hai — asli data bilkul safe.</p>
    <form action="/v2-test" method="POST" class="space-y-3">
      <div>
        <label class="block text-sm font-semibold mb-1">PDF link (Google Drive / direct URL)</label>
        <input type="url" name="pdf_url" class="w-full text-sm border p-2 rounded" placeholder="https://..." required>
      </div>
      <div class="grid grid-cols-3 gap-2">
        <div>
          <label class="block text-xs font-semibold mb-1">Subject</label>
          <input type="text" name="subject_code" maxlength="3" placeholder="PSY" class="w-full text-sm border p-2 rounded" required>
        </div>
        <div>
          <label class="block text-xs font-semibold mb-1">Chapter no</label>
          <input type="number" name="chapter_no" min="1" placeholder="11" class="w-full text-sm border p-2 rounded" required>
        </div>
        <div>
          <label class="block text-xs font-semibold mb-1">Page offset</label>
          <input type="number" name="page_offset" value="-1" class="w-full text-sm border p-2 rounded">
        </div>
      </div>
      <button class="w-full bg-violet-600 text-white font-bold py-2 rounded" {% if state.status == 'processing' %}disabled{% endif %}>
        🧪 Run V2 test (1 chapter)
      </button>
    </form>
  </div>

  <div class="bg-white rounded-lg shadow p-4 border-2 border-emerald-500 space-y-3">
    <p class="text-xs font-bold text-emerald-700 uppercase">Recommended for phone — FULL BOOK (v2)</p>
    <form action="/run-url" method="POST" class="space-y-3">
      <div>
        <label class="block text-sm font-semibold mb-1">PDF link (Google Drive / Telegram / direct download URL)</label>
        <input type="url" name="pdf_url" class="w-full text-sm border p-2 rounded" placeholder="https://..." required>
      </div>
      <div>
        <label class="block text-sm font-semibold mb-1">Subject code (3 letters)</label>
        <input type="text" name="subject_code" maxlength="3" class="w-full text-sm border p-2 rounded uppercase" placeholder="PSY" required>
      </div>
      <div>
        <label class="block text-sm font-semibold mb-1">Page offset</label>
        <input type="number" name="page_offset" value="-1" class="w-full text-sm border p-2 rounded">
      </div>
      <button class="w-full bg-emerald-600 text-white font-bold py-2 rounded" {% if state.status == 'processing' %}disabled{% endif %}>
        Run (from link)
      </button>
    </form>
  </div>

  <details class="bg-white rounded-lg shadow p-4">
    <summary class="text-sm font-semibold cursor-pointer">Or upload file directly (less reliable on mobile)</summary>
    <form action="/run" method="POST" enctype="multipart/form-data" class="space-y-3 mt-3">
      <div>
        <label class="block text-sm font-semibold mb-1">PDF file</label>
        <input type="file" name="file" accept=".pdf" class="w-full text-sm border p-2 rounded">
      </div>
      <div>
        <label class="block text-sm font-semibold mb-1">Subject code (3 letters)</label>
        <input type="text" name="subject_code" maxlength="3" class="w-full text-sm border p-2 rounded uppercase" placeholder="PSY" required>
      </div>
      <div>
        <label class="block text-sm font-semibold mb-1">Page offset</label>
        <input type="number" name="page_offset" value="-1" class="w-full text-sm border p-2 rounded">
      </div>
      <button class="w-full bg-slate-800 text-white font-bold py-2 rounded" {% if state.status == 'processing' %}disabled{% endif %}>
        Run (upload)
      </button>
    </form>
  </details>

  <div class="bg-black text-green-400 text-xs rounded-lg p-3 h-64 overflow-y-auto font-mono" id="log">
    {% for line in state.log %}{{ line }}<br>{% endfor %}
  </div>
</div>
<script>
setInterval(() => {
  fetch('/status').then(r => r.json()).then(d => {
    document.getElementById('log').innerHTML = d.log.join('<br>');
  });
}, 3000);
</script>
</body>
</html>
"""

import re as _re

OUTPUT_ROOT_ENV = os.environ.get("OUTPUT_DIR", "./qbank_output")

# If the service writes under /data but no Railway Volume is mounted there,
# EVERYTHING VANISHES on every redeploy (this already burned us once).
# Surface it as a huge red banner instead of silently losing data again.
VOLUME_WARN = None
if OUTPUT_ROOT_ENV.startswith("/data"):
    _d = Path("/data")
    if not (_d.exists() and _d.is_mount()):
        VOLUME_WARN = ("⚠️ Volume NAHI lagaa hai — /data pe Railway Volume attach nahi hai, "
                       "isliye har redeploy pe saara data DELETE ho jayega. "
                       "Fix: Railway → Service → Settings → Volumes → New Volume → "
                       "Mount Path: /data. Phir ye banner apne aap chala jayega.")

def resolve_download_url(url):
    """Convert common share-link formats (Google Drive etc.) into a direct
    download URL. Falls back to the original URL if it's not recognized."""
    m = _re.search(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        m = _re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        file_id = m.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url

@app.route("/")
def index():
    return render_template_string(PAGE, state=state, vol_warn=VOLUME_WARN)

@app.route("/status")
def status():
    return jsonify(state)

@app.route("/health")
def health():
    """Small, dependency-free readiness endpoint for Railway monitoring."""
    return jsonify({
        "ok": True,
        "output_root": str(OUTPUT_ROOT_ENV),
        "volume_ready": not bool(VOLUME_WARN),
        "gemini_key_configured": bool(gemini_keys.discover_keys()),
        "gemini_key_count": len(gemini_keys.discover_keys()),
        "status": state["status"],
    })

def parse_page_offset():
    """int() of "" or junk raises ValueError -> Flask 500 page. Be forgiving."""
    try:
        return int(request.form.get("page_offset") or -1)
    except (TypeError, ValueError):
        return -1

@app.route("/run-url", methods=["POST"])
def run_url():
    if not try_mark_processing():          # AUDIT-FIX: atomic busy-guard
        return redirect(url_for("index"))
    pdf_url = resolve_download_url(request.form.get("pdf_url", "").strip())
    subject_code = request.form.get("subject_code", "").strip().upper()
    page_offset = parse_page_offset()
    if not pdf_url:
        with state_lock:
            state["status"] = "idle"
        return "No URL provided", 400

    def download_then_run():
        try:
            log("⬇️ Downloading PDF from link...")
            # AUDIT-FIX: unique name per (URL, subject) -- two different
            # books whose share links end in the same tail name no longer
            # overwrite each other in ./pdfs.
            url_hash = hashlib.sha1(pdf_url.encode()).hexdigest()[:8]
            fname = f"{subject_code or 'PDF'}-{url_hash}.pdf"
            pdf_path = UPLOAD_DIR / fname
            fetch_pdf_guarded(pdf_url, pdf_path)   # SSRF + size/time guards
            log(f"✅ Downloaded {fname} ({pdf_path.stat().st_size // 1024} KB)")
            run_pipeline_thread(subject_code, pdf_path, page_offset)
        except Exception as e:
            with state_lock:
                state["status"] = "failed"
                state["error"] = str(e)
            log(f"❌ Download failed: {e}")
            traceback.print_exc()

    t = threading.Thread(target=download_then_run)
    t.daemon = True
    t.start()
    return redirect(url_for("index"))

@app.route("/v2-test", methods=["POST"])
def v2_test():
    """Smoke-test the boundary-phased engine on ONE chapter, output into an
    isolated <OUTPUT_ROOT>_v2test/ folder -- never touches real data."""
    pdf_url = resolve_download_url(request.form.get("pdf_url", "").strip())
    subject_code = request.form.get("subject_code", "").strip().upper() or "TST"
    try:
        chapter_no = int(request.form.get("chapter_no", ""))
    except ValueError:
        return "Chapter number do (e.g. 11)", 400
    page_offset = parse_page_offset()
    if not pdf_url:
        return "No URL provided", 400
    if not try_mark_processing():          # AUDIT-FIX: atomic busy-guard
        return redirect(url_for("index"))

    def download_then_test():
        try:
            log("⬇️ [V2-TEST] Downloading PDF...")
            url_hash = hashlib.sha1(pdf_url.encode()).hexdigest()[:8]
            fname = f"{subject_code}-{url_hash}.pdf"
            pdf_path = UPLOAD_DIR / fname
            fetch_pdf_guarded(pdf_url, pdf_path)   # SSRF + size/time guards
            log(f"✅ [V2-TEST] Downloaded {fname}")

            # A smoke test must never redirect a later full-book run into the
            # test folder.  The pipeline keeps these paths as module globals,
            # so save and restore all of them even when Gemini/PDF processing
            # raises an exception.
            test_root = Path(str(Path(OUTPUT_ROOT_ENV)) + "_v2test")
            # A smoke test is a fresh measurement, not a resume operation.
            # Reset does not touch this isolated directory, so retaining it
            # would append a second copy of the same chapter and report an
            # inflated question count (for example 26 extracted / 52 shown).
            if test_root.exists():
                shutil.rmtree(test_root)
                log("🧹 [V2-TEST] Previous test output cleared; starting a fresh test.")
            with state_lock:
                state["test_ready"] = False
            original_paths = (pipeline.OUTPUT_ROOT, pipeline.DATA_DIR,
                              pipeline.ASSETS_DIR, pipeline.STATE_FILE)
            try:
                pipeline.OUTPUT_ROOT = test_root
                pipeline.DATA_DIR = test_root / "data"
                pipeline.ASSETS_DIR = test_root / "assets"
                pipeline.STATE_FILE = test_root / "state.json"
                pipeline.DATA_DIR.mkdir(parents=True, exist_ok=True)

                st = pipeline.load_state()
                log(f"🧪 [V2-TEST] {subject_code} chapter {chapter_no} "
                    f"(boundary-phased engine chal raha hai)...")
                import google.generativeai as genai
                # Multi-key pool: configures the first usable key and lets the
                # pipeline rotate to the next project as each one is spent.
                gemini_keys.init(st, pipeline.MAX_CALLS_PER_DAY)
                model = gemini_keys.track(genai.GenerativeModel(pipeline.GEMINI_MODEL))
                pipeline.reset_daily_counter_if_needed(st)
                q_path = pipeline.DATA_DIR / "questions.jsonl"
                import boundary_phased as engine
                engine.run_chapter(str(pdf_path), subject_code, chapter_no,
                                   pipeline.OUTPUT_ROOT,
                                   page_offset=page_offset, model=model,
                                   state=st)
                import json as _json
                rows = [_json.loads(l) for l in q_path.read_text().splitlines() if l.strip()] \
                    if q_path.exists() else []
                n = len(rows)
                ma = sum(1 for r in rows if not r.get("correct_options"))
                ms = sum(1 for r in rows if not (r.get("solution") or {}).get("text"))
                log(f"📊 [V2-TEST] Result: {n} questions | missing answer: {ma} | "
                    f"missing solution: {ms} (output: _v2test/ folder, asli data safe ✅)")
                # Non-empty fields do not prove a clean chapter. Surface the
                # engine's gate rows prominently so a test is never described
                # as clean while blockers/review flags remain.
                ledger = test_root / "data" / "export_gate.jsonl"
                pending = sum(1 for line in ledger.read_text(encoding="utf-8").splitlines()
                              if line.strip()) if ledger.exists() else 0
                if pending:
                    log(f"⚠️ [V2-TEST] REVIEW REQUIRED: {pending} export-gate "
                        "row(s) written -- boundary engine ne kuch cheezein flag "
                        "ki hain. Pehle inhe dekho, full-book run baad me.")
                else:
                    log("✅ [V2-TEST] Export gate clean; run validator before full book.")
                # Test output lives outside the main output root, so /download
                # cannot include it; build a dedicated downloadable archive.
                try:
                    with zipfile.ZipFile(TEST_ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
                        for f in test_root.rglob("*"):
                            if f.is_file():
                                zf.write(f, f.relative_to(test_root))
                    with state_lock:
                        state["test_ready"] = True
                    log("📦 [V2-TEST] test_results.zip ready -- upar violet "
                        "'Download TEST results' button se download karo.")
                except Exception as ze:
                    log(f"⚠️ [V2-TEST] zip nahi ban paya: {ze}")
            finally:
                (pipeline.OUTPUT_ROOT, pipeline.DATA_DIR,
                 pipeline.ASSETS_DIR, pipeline.STATE_FILE) = original_paths
                log("🔒 [V2-TEST] Main output path restored for the next full-book run.")
            with state_lock:
                state["status"] = "completed"
            log("✅ [V2-TEST] Done! Full book sirf tab run karo jab review warning/validator flags resolve ho.")
        except SystemExit:
            with state_lock:
                state["status"] = "paused"
            log("⏸ [V2-TEST] daily Gemini limit -- kal dobara dabana.")
        except Exception as e:
            with state_lock:
                state["status"] = "failed"
                state["error"] = str(e)
            log(f"❌ [V2-TEST] error: {e}")
            traceback.print_exc()

    t = threading.Thread(target=download_then_test)
    t.daemon = True
    t.start()
    return redirect(url_for("index"))


@app.route("/run", methods=["POST"])
def run():
    if not try_mark_processing():          # AUDIT-FIX: atomic busy-guard
        return redirect(url_for("index"))
    f = request.files.get("file")
    subject_code = request.form.get("subject_code", "").strip().upper()
    page_offset = parse_page_offset()
    if f and f.filename.lower().endswith(".pdf"):
        # AUDIT-FIX: subject-scoped upload name -- a same-named upload for a
        # different subject no longer overwrites another book's cached PDF.
        base = secure_filename(f.filename) or "book.pdf"
        pdf_path = UPLOAD_DIR / f"{subject_code or 'PDF'}-{base}"
        f.save(pdf_path)
    else:
        # no new file uploaded -> reuse whatever PDF is already in ./pdfs
        existing = list(UPLOAD_DIR.glob("*.pdf"))
        if not existing:
            # AUDIT-FIX: fall back to the persisted volume copy (survives
            # redeploys) before declaring the input lost.
            vol = INPUT_META_DIR / f"{subject_code}.pdf"
            if vol.exists():
                pdf_path = vol
                log(f"ℹ️ ./pdfs empty -- reusing persisted Volume copy {vol}")
            else:
                with state_lock:
                    state["status"] = "idle"
                return "No PDF uploaded and none found in ./pdfs", 400
        else:
            pdf_path = existing[0]
    t = threading.Thread(target=run_pipeline_thread, args=(subject_code, pdf_path, page_offset))
    t.daemon = True
    t.start()
    return redirect(url_for("index"))

# The old multi-pass engine's /recover mode is RETIRED with the engine
# cutover. Healing a chapter now = re-run it (a chapter absent from
# state.json -> chapters_done is retried automatically on Run), and
# content-level fixes go through the /review queue (flag, don't fix).

@app.route("/fix", methods=["POST"])
def fix():
    """Button-only heal of known run-4 defects (fix_output.patch_all).
    Evidence-gated + idempotent, timestamped backup + archive before write."""
    if VOLUME_WARN:
        return ("Volume /data pe attach nahi hai -- pehle Railway Settings me Volume lagao "
                "(Mount Path: /data), warna jo bhi likha jayega wo next redeploy pe udd jayega.", 400)
    if not try_mark_processing():          # AUDIT-FIX: atomic busy-guard
        return redirect(url_for("index"))

    def _do_fix():
        try:
            import json as _json
            import time as _time
            import fix_output
            q = fix_output.QUESTIONS
            if not q.exists():
                log(f"❌ {q} not found on the Volume -- nothing to fix")
                with state_lock:
                    state["status"] = "failed"
                    state["error"] = "questions.jsonl not found"
                return
            rows = [_json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()]
            log(f"🩹 Fix pass on {len(rows)} questions ...")
            assets_q = fix_output.OUTPUT_ROOT / "assets" / "questions"
            rows, actions, archive = fix_output.patch_all(rows, assets_q)
            n_apply = 0
            for pid, st, detail in actions:
                if st == "APPLY":
                    n_apply += 1
                log(f"   [{st}] {pid}: {detail}")
            if n_apply:
                backup = q.with_suffix(f".bak-{_time.strftime('%Y%m%d-%H%M%S')}")
                backup.write_text(q.read_text(encoding="utf-8"), encoding="utf-8")
                tmp = q.with_suffix(".tmp")
                tmp.write_text("\n".join(_json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                               encoding="utf-8")
                os.replace(tmp, q)
                if archive:
                    with open(fix_output.ARCHIVE_LOG, "a", encoding="utf-8") as fh:
                        for a in archive:
                            fh.write(_json.dumps(a, ensure_ascii=False) + "\n")
                log(f"✅ {n_apply} heal(s) written. Backup -> {backup.name}, "
                    f"original fragments archived.")
            else:
                log("✅ Nothing to heal -- data already clean (all patches skipped).")
            run_validator_and_log()
            _write_review_digests()
            with state_lock:
                state["status"] = "completed"
            make_zip()
        except Exception as e:
            with state_lock:
                state["status"] = "failed"
                state["error"] = str(e)
            log(f"❌ Fix error: {e}")
            traceback.print_exc()

    t = threading.Thread(target=_do_fix)
    t.daemon = True
    t.start()
    return redirect(url_for("index"))

@app.route("/validate", methods=["POST"])
def validate():
    """Button-only re-check: fresh validation_report.json + flags in the log."""
    if VOLUME_WARN:
        return ("Volume /data pe attach nahi hai -- pehle Railway Settings me Volume lagao "
                "(Mount Path: /data), warna jo bhi likha jayega wo next redeploy pe udd jayega.", 400)
    if not try_mark_processing():          # AUDIT-FIX: atomic busy-guard
        return redirect(url_for("index"))

    def _do_validate():
        try:
            run_validator_and_log()
            _write_review_digests()
            with state_lock:
                state["status"] = "completed"
        except Exception as e:
            with state_lock:
                state["status"] = "failed"
                state["error"] = str(e)
            log(f"❌ Validate error: {e}")
            traceback.print_exc()

    t = threading.Thread(target=_do_validate)
    t.daemon = True
    t.start()
    return redirect(url_for("index"))

@app.route("/reset", methods=["POST"])
def reset_output():
    """Clean-slate for the NEXT book: move EVERYTHING in the output root
    (data/, assets/, subjects/, state.json, any strays) into
    _archive/<timestamp>/ INSIDE the volume (nothing is deleted), so a fresh
    run starts empty. The old reset only archived data/assets/state.json and
    left subjects/ behind -- a previous book's per-subject chapter JSONs and
    questions.jsonl kept leaking into the next export zip after reset."""
    if VOLUME_WARN:
        return ("Volume /data pe attach nahi hai -- reset blocked (archive bhi kahin "
                "survive nahi karegi). Pehle Volume lagao.", 400)
    if request.form.get("confirm", "").strip().upper() != "RESET":
        return "Confirmation ke liye box me RESET likho.", 400
    if state["status"] == "processing":
        return "Run chal raha hai -- pehle complete hone do.", 400
    out = Path(OUTPUT_ROOT_ENV)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    arch = out / "_archive" / stamp
    moved = []
    try:
        entries = _entries_to_archive(out)
        if entries:
            arch.parent.mkdir(parents=True, exist_ok=True)
        for name, src in entries:
            shutil.move(str(src), str(arch / name))
            moved.append(f"{name}/" if src.is_dir() else name)
        log(f"🧹 RESET complete -- archived: {', '.join(moved) if moved else '(already clean)'} "
            f"-> _archive/{stamp}/ (volume ke andar hi safe hai; zip me include nahi hota)")
        log("🆕 Fresh start -- ab nayi book run karo.")
    except Exception as e:
        log(f"❌ Reset failed: {e}")
        traceback.print_exc()
        return f"Reset failed: {e}", 500
    make_zip()
    return redirect(url_for("index"))

@app.route("/data-status")
def data_status():
    """Read-only proof that the extraction data on the Volume is intact.
    Shows file sizes + question/image counts -- nothing is modified."""
    out = Path(os.environ.get("OUTPUT_DIR", "./qbank_output"))
    lines = [f"Output folder: {out}"]
    d = Path("/data")
    lines.append(f"/data exists: {d.exists()}   /data is a mounted Volume: "
                 f"{('YES' if d.is_mount() else 'NO -- ephemeral, vanishes on redeploy!') if d.exists() else '?'}")
    if d.exists():
        kids = sorted(p.name + ("/" if p.is_dir() else "") for p in d.iterdir())
        lines.append(f"/data contents: {', '.join(kids[:30]) or '(empty)'}")
    if not out.exists():
        # maybe the data lives somewhere ELSE in the container -- hunt for it
        found = []
        for base in (Path("/app"), Path("."), Path("/tmp")):
            try:
                for p in base.rglob("questions.jsonl"):
                    found.append(str(p))
            except Exception:
                pass
        lines.append("X  output folder missing.")
        lines.append(("Found copies elsewhere: " + ", ".join(found)) if found
                     else "No questions.jsonl anywhere -- use Restore to bring it back from Drive.")
        return "<pre style='font-size:15px;padding:12px'>" + "\n".join(lines) + "</pre>"
    q = out / "data" / "questions.jsonl"
    if q.exists():
        n = sum(1 for l in q.read_text(encoding="utf-8").splitlines() if l.strip())
        lines.append(f"OK  questions.jsonl = {n} questions  ({q.stat().st_size // 1024} KB)")
    else:
        lines.append("X  data/questions.jsonl missing")
    d = out / "data"
    if d.exists():
        for f in sorted(d.iterdir()):
            if f.name != "questions.jsonl":
                lines.append(f"    data/{f.name}  ({f.stat().st_size // 1024} KB)")
    aq = out / "assets" / "questions"
    if aq.exists():
        for sub in sorted(aq.iterdir()):
            if sub.is_dir():
                lines.append(f"OK  assets/questions/{sub.name} = {len(list(sub.iterdir()))} images")
    else:
        lines.append("X  assets/questions folder missing")
    bak = list(out.glob("data/*.bak-*"))
    if bak:
        lines.append(f"    backups found: {len(bak)}")
    lines.append("")
    lines.append("Agar upar 'OK questions.jsonl = 434 questions' dikh raha hai,")
    lines.append("to data 100% safe hai. Screenshot bhej do.")
    return "<pre style='font-size:15px;padding:12px'>" + "\n".join(lines) + "</pre>"

DRIVE_DATA_FILES = {"questions.jsonl", "chapters.json", "orphans.jsonl",
                    "decorative_images.jsonl", "validation_report.json",
                    "unmatched_images.jsonl", "integrity_flags.jsonl",
                    "stem_conflicts.jsonl", "still_incomplete_after_retry.jsonl",
                    "fix_output_archive.jsonl", "audit_state.json"}

def _fetch_drive_file(file_id, dest):
    """Download one shared Drive file to dest (streamed). Returns bytes written."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    r = requests.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    if "text/html" in r.headers.get("Content-Type", ""):  # big-file confirm page
        m = _re.search(r"confirm=([0-9A-Za-z_-]+)", r.text)
        if m:
            r = requests.get(f"{url}&confirm={m.group(1)}", stream=True, timeout=120,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(dest, "wb") as fh:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                fh.write(chunk)
                n += len(chunk)
    return n

@app.route("/restore-drive", methods=["POST"])
def restore_drive():
    """Pull the whole working set back from the user's shared Google Drive
    backup folder: *.webp -> assets/questions/<SUBJECT>/, known data files
    -> data/, state.json -> output root. Everything else is ignored."""
    if VOLUME_WARN:
        return ("Volume /data pe attach nahi hai -- pehle Railway Settings me Volume lagao "
                "(Mount Path: /data), warna jo bhi likha jayega wo next redeploy pe udd jayega.", 400)
    if not try_mark_processing():          # AUDIT-FIX: atomic busy-guard
        return redirect(url_for("index"))
    folder = request.form.get("folder", "").strip()
    m = _re.search(r"(?:folders/|[?&]id=|^\s*)([A-Za-z0-9_-]{10,})", folder)
    if not m:
        with state_lock:
            state["status"] = "idle"
        return "Drive folder link samajh nahi aaya", 400
    folder_id = m.group(1)

    def _do_restore():
        try:
            import html as _html
            out = Path(OUTPUT_ROOT_ENV)
            log("♻️ Restore started -- reading Drive folder listing ...")
            lst = requests.get(f"https://drive.google.com/embeddedfolderview?id={folder_id}#list",
                               timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            lst.raise_for_status()
            entries = _re.findall(r'file/d/([A-Za-z0-9_-]+)/[^"#]*"[^>]*>.*?flip-entry-title">([^<]+)<',
                                  lst.text, _re.DOTALL)
            seen, files = set(), []
            for fid, name in entries:
                name = _html.unescape(name).strip()
                if fid not in seen and name:
                    seen.add(fid)
                    files.append((fid, name))
            log(f"   found {len(files)} file(s) in the folder")
            if not files:
                raise RuntimeError("Folder se file list nahi mili -- Drive folder ka sharing "
                                   "'Anyone with the link' pe set hai kya? Screenshot bhejo.")
            ok, skipped, failed = 0, 0, 0
            for i, (fid, name) in enumerate(files, 1):
                low = name.lower()
                if low.endswith((".webp", ".png", ".jpg", ".jpeg")):
                    sub = name.split("-", 1)[0].upper()  # PSY-001-001_Q_01.webp -> PSY
                    dest = out / "assets" / "questions" / sub / name
                elif low in DRIVE_DATA_FILES:
                    dest = out / "data" / name
                elif low == "state.json":
                    dest = out / "state.json"
                else:
                    skipped += 1
                    continue
                try:
                    _fetch_drive_file(fid, dest)
                    ok += 1
                except Exception as fe:
                    failed += 1
                    log(f"   X {name}: {fe}")
                if i % 25 == 0:
                    log(f"   ... {i}/{len(files)} ({ok} restored)")
            log(f"✅ Restore done: {ok} file(s) restored, {skipped} ignored (pdf/log etc.), "
                f"{failed} failed")
            q = out / "data" / "questions.jsonl"
            if q.exists():
                n = sum(1 for l in q.read_text(encoding="utf-8").splitlines() if l.strip())
                log(f"📦 questions.jsonl = {n} questions -- data wapas aa gaya!")
            else:
                log("⚠️ questions.jsonl missing after restore -- folder me woh file hai kya?")
            with state_lock:
                state["status"] = "completed"
            make_zip()
        except Exception as e:
            with state_lock:
                state["status"] = "failed"
                state["error"] = str(e)
            log(f"❌ Restore error: {e}")
            traceback.print_exc()

    t = threading.Thread(target=_do_restore)
    t.daemon = True
    t.start()
    return redirect(url_for("index"))

@app.route("/restore-zip", methods=["POST"])
def restore_zip():
    """Extract a downloaded output_results.zip back into OUTPUT_DIR."""
    if VOLUME_WARN:
        return ("Volume /data pe attach nahi hai -- pehle Railway Settings me Volume lagao "
                "(Mount Path: /data), warna jo bhi likha jayega wo next redeploy pe udd jayega.", 400)
    if not try_mark_processing():          # AUDIT-FIX: atomic busy-guard
        return redirect(url_for("index"))
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".zip"):
        with state_lock:
            state["status"] = "idle"
        return "output_results.zip file chahiye", 400
    tmp = UPLOAD_DIR / "restore_upload.zip"
    f.save(tmp)

    def _do_restore_zip():
        try:
            import shutil as _sh
            out = Path(OUTPUT_ROOT_ENV)
            out.mkdir(parents=True, exist_ok=True)
            n = 0
            with zipfile.ZipFile(tmp) as zf:
                for name in zf.namelist():
                    clean = name.lstrip("/")
                    if clean.startswith("./"):
                        clean = clean[2:]
                    if clean.startswith("qbank_output/"):
                        clean = clean.split("/", 1)[1]
                    if not clean or ".." in clean or clean.endswith("/"):
                        continue
                    dest = out / clean
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(dest, "wb") as dst:
                        _sh.copyfileobj(src, dst)
                    n += 1
            log(f"✅ Zip restored: {n} file(s) -> {out}")
            q = out / "data" / "questions.jsonl"
            if q.exists():
                cnt = sum(1 for l in q.read_text(encoding="utf-8").splitlines() if l.strip())
                log(f"📦 questions.jsonl = {cnt} questions -- data wapas aa gaya!")
            with state_lock:
                state["status"] = "completed"
            make_zip()
        except Exception as e:
            with state_lock:
                state["status"] = "failed"
                state["error"] = str(e)
            log(f"❌ Restore zip error: {e}")
            traceback.print_exc()

    t = threading.Thread(target=_do_restore_zip)
    t.daemon = True
    t.start()
    return redirect(url_for("index"))

@app.route("/download-test")
def download_test():
    """Violet button target: the LAST smoke test's output as its own zip
    (_v2test/ sits outside the main output root, so the green /download zip
    can never see it)."""
    p = TEST_ZIP_PATH
    if not p.exists():
        return "Abhi tak koi smoke test complete nahi hua -- pehle 🧪 violet card se test chalao.", 404
    return send_file(str(p), as_attachment=True, download_name="v2_test_results.zip")


@app.route("/download")
def download():
    if os.path.exists("output_results.zip"):
        return send_file("output_results.zip", as_attachment=True)
    return "No results yet", 404


@app.route("/download-master-review")
def download_master_review():
    """Build (or rebuild) the MASTER_REVIEW/ tree from the live output
    and serve a zip of it. Read-only: never modifies split/, assets/,
    data/, subjects/, or any extraction file -- only writes a fresh
    MASTER_REVIEW/ directory + zip next to the output root. Safe to
    click any time after at least one chapter has been split-written."""
    out = Path(OUTPUT_ROOT_ENV)
    if not out.exists():
        return "Output folder missing -- pehle Run karo.", 400
    split_dir = out / "split"
    if not split_dir.exists() or not any(split_dir.iterdir()):
        return ("MASTER_REVIEW ke liye split/ directory chahiye, but "
                "split/ is empty. Run complete nahi hua ya split layer "
                "ne kuch nahi likha. Pehle Run complete hone do."), 400
    try:
        zip_path = master_review_export.build_master_review_zip(out)
    except Exception as e:
        log(f"❌ MASTER_REVIEW build failed: {e}")
        traceback.print_exc()
        return f"MASTER_REVIEW build failed: {e}", 500
    # Log a short summary so the dashboard log box shows what
    # happened (matches the existing 'Download results' UX where
    # /make-zip prints a one-liner).
    try:
        manifest = _json.loads(
            (out / "MASTER_REVIEW" / "MASTER_REVIEW_MANIFEST.json").read_text(
                encoding="utf-8"))
        log(f"📒 MASTER_REVIEW: {manifest.get('total_chapters', 0)} chapter(s), "
            f"{manifest.get('total_files_copied', 0)} file(s) copied, "
            f"{manifest.get('total_images_copied', 0)} image(s) copied, "
            f"{manifest.get('total_files_missing', 0)} file(s) missing, "
            f"{manifest.get('total_images_missing', 0)} image(s) missing "
            f"-> {zip_path.name}")
    except Exception:
        log(f"📒 MASTER_REVIEW (review package) zip ready -> {zip_path} "
            f"(ye review hone ke BAAD ka proof copy hai, review ispar nahi hota)")
    return send_file(str(zip_path), as_attachment=True,
                     download_name="MASTER_REVIEW.zip")

# ---------------------------------------------------------------------------
# MANUAL REVIEW SCREEN (contract: pipeline freezes after extraction; humans
# edit here; every decision is disk-persisted; final zip gates on a clear
# queue). All logic lives in review_queue.py — this is only the wiring.
# ---------------------------------------------------------------------------
app.add_template_filter(review_queue.md_to_html, "mdtable")

REVIEW_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review Queue</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
.rtd table{border-collapse:collapse;font-size:11px;background:#fff}
.thumb{max-height:130px;border:1px solid #94a3b8;border-radius:6px;background:#fff}
details>summary{list-style:none}details>summary::-webkit-details-marker{display:none}
</style>
</head>
<body class="bg-gray-100 p-3">
<div class="max-w-3xl mx-auto space-y-3">
  <div class="flex items-center justify-between">
    <h1 class="text-lg font-bold">🧑‍⚖️ Review Queue</h1>
    <a href="/" class="text-xs text-sky-700 underline">← dashboard</a>
  </div>

  {% if msg %}
  <div class="rounded p-3 text-sm font-semibold {{ 'bg-emerald-100 text-emerald-900' if ok else 'bg-red-100 text-red-900' }}">{{ msg }}</div>
  {% endif %}

  <div class="sticky top-0 z-20 bg-gray-100 pb-2 -mx-3 px-3 pt-1 shadow-sm">
  <div class="bg-white rounded shadow p-3 text-sm flex gap-4 flex-wrap">
    <span id="cnt-issues-wrap">🏷️ <b id="cnt-issues">{{ rows|length }}</b> issues <span class="text-gray-400">(<span id="cnt-flags">{{ n_raw_flags }}</span> flags)</span></span>
    <span>🔴 <b id="cnt-blocker">{{ counts.blocker }}</b> blocker</span>
    <span>🟡 <b id="cnt-review">{{ counts.review }}</b> review</span>
    <span>✅ <b id="cnt-resolved">{{ counts.resolved }}</b> resolved</span>
    {% if counts.auto_resolved %}<span title="ye flags ab galat nahi hain (jaise image ab attached) -- self-verify hoke band hue">🤖 <b>{{ counts.auto_resolved }}</b> auto-resolved</span>{% endif %}
  </div>

  {% for w in warnings %}
  <div class="bg-red-600 text-white rounded p-3 text-xs mt-2">⚠️ {{ w }}</div>
  {% endfor %}

  {% if clear %}
  <div class="bg-emerald-600 text-white rounded shadow p-4 text-sm font-bold mt-2">
    ✅ Queue CLEAR — every flag decided. Final zip unlocked:
    <a href="/download-final" class="underline">🚀 Build &amp; download final zip</a>
  </div>
  {% else %}
  <div class="bg-rose-600 text-white rounded shadow p-3 text-xs mt-2">
    🔒 Final zip locked — {{ counts.blocker + counts.review }} row(s) still open.
    Sab decide karne ke baad final milega.
  </div>
  {% endif %}

  <form method="GET" action="/review" class="bg-white rounded shadow p-2 flex flex-wrap gap-2 text-xs items-center mt-2">
    <b>Filter:</b>
    <select name="chapter" class="border rounded px-1 py-1">
      <option value="">all chapters</option>
      {% for ch in chapters %}<option value="{{ ch }}" {{ 'selected' if sel_chapter==ch }}>{{ ch }}</option>{% endfor %}
    </select>
    <select name="kind" class="border rounded px-1 py-1">
      <option value="">all kinds</option>
      {% for k in kinds %}<option value="{{ k }}" {{ 'selected' if sel_kind==k }}>{{ k }}</option>{% endfor %}
    </select>
    <select name="sev" class="border rounded px-1 py-1">
      <option value="">blocker+review</option>
      <option value="BLOCKER" {{ 'selected' if sel_sev=='BLOCKER' }}>only blockers</option>
      <option value="REVIEW" {{ 'selected' if sel_sev=='REVIEW' }}>only review</option>
    </select>
    <button class="bg-slate-700 text-white px-3 py-1 rounded">Apply</button>
    <span class="text-gray-500">{{ rows|length }} issue(s)</span>
    <a class="bg-indigo-600 text-white px-3 py-1 rounded" href="/review/lookup">🔎 lookup</a>
    <span class="text-gray-400">(bar upar chipki rahegi)</span>
  </form>
  <div class="flex flex-wrap gap-2 items-center mt-1 text-xs">
    <form method="POST" action="/review/ai-verify" style="display:inline">
      <button class="bg-purple-700 text-white px-3 py-1 rounded">🤖 AI-verify (separate 3.1 pool)</button>
    </form>
    <a class="bg-teal-600 text-white px-3 py-1 rounded" href="/review/ai-resolved">✅ AI-resolved tab</a>
  </div>
  {% if rows %}
  <form method="POST" action="/review/decide-bulk" class="qact bg-amber-50 border border-amber-300 rounded p-2 text-xs flex flex-wrap gap-2 items-center" data-opt="hide-all"
       onsubmit="return confirm('{{ rows|length }} issue(s) ke saare flags par ye action lagega. Pakka?')">
    <input type="hidden" name="chapter" value="{{ sel_chapter }}">
    <input type="hidden" name="kind" value="{{ sel_kind }}">
    <input type="hidden" name="sev" value="{{ sel_sev }}">
    <input type="hidden" name="back" value="{{ self_qs }}">
    <input name="reason" placeholder="reason (bulk ke liye, jaise 'image actually nahi hai page pe')" class="border rounded px-2 py-1 flex-1 bg-white">
    <button name="action" value="ignored" class="bg-gray-600 text-white px-3 py-1 rounded">⏭️ Bulk Skip — jo abhi filter pe dikh raha hai sab</button>
    <button name="action" value="approved" class="bg-emerald-700 text-white px-3 py-1 rounded">✔ Bulk Approve — sab is filter par</button>
    <span class="text-gray-500">({{ rows|length }} issues affected; kind filter laga ke best use hota hai)</span>
  </form>
  {% endif %}
  </div>

  {% set last_ch = [None] %}
  {% for r in rows %}
  {% if r.chapter_id != last_ch[0] %}{% set _ = last_ch.pop() %}{% set _ = last_ch.append(r.chapter_id) %}
  <h2 class="text-sm font-bold text-slate-600 pt-3">📖 {{ r.chapter_id or '—' }}</h2>
  {% endif %}
  <div data-card data-sev="{{ r.severity }}" class="bg-white rounded shadow p-3 space-y-2 border-l-4 {{ 'border-red-500' if r.severity == 'BLOCKER' else 'border-amber-400' }}">
    <div class="flex flex-wrap items-center gap-2 text-xs">
      <span class="font-bold {{ 'text-red-700' if r.severity == 'BLOCKER' else 'text-amber-700' }}">{{ r.severity }}</span>
      <span class="font-mono font-bold">{{ r.q_id or r.chapter_id or '—' }}</span>
      {% for k in r.kinds %}<span class="bg-gray-200 rounded px-1">{{ k }}</span>{% endfor %}
      <span class="text-gray-500">src: {{ r.sources|join(', ') }}</span>
      {% if r.flag_keys|length > 1 %}<span class="bg-indigo-100 text-indigo-800 rounded px-1" title="same issue flagged by many tools — ek decision se sab band">{{ r.flag_keys|length }} flags → 1 decision</span>{% endif %}
    </div>
    <div class="text-[11px] bg-sky-50 border border-sky-200 rounded p-1">💡 {{ r.guide }}</div>
    {% for d in r.details %}{% if loop.first %}<p class="text-xs text-gray-700 whitespace-pre-wrap">{{ d.detail }}</p>{% endif %}{% endfor %}
    {% if r.details|length > 1 %}
    <div class="text-[11px] text-gray-500">↔ same issue seen by:
      {% for d in r.details[1:] %}<span class="bg-gray-200 rounded px-1">{{ d.kind }} · {{ d.source }}</span> {% endfor %}
      <span class="text-gray-400">(text same, repeat nahi dikhaya)</span>
    </div>
    {% endif %}
    {% for sn in r.stale_notes %}<p class="text-xs text-orange-600 font-semibold">♻️ {{ sn }}</p>{% endfor %}

    <div class="text-[11px]">📄 source page(s): <b class="font-mono">{{ r.pages|join(' ') }}</b>
    <span class="text-gray-400">(apni PDF me ye number kholo)</span></div>

    {% set v = views.get(r.flag_keys[0], {}) %}
    {% if r.images %}
    <div class="space-y-1">
      {% for im in r.images %}
      <div class="border rounded p-1 bg-gray-50">
        <img class="thumb" loading="lazy" src="/review/img?f={{ im }}">
        <div class="text-[10px] font-mono break-all text-gray-500">{{ im }} — <a class="text-sky-700 underline" href="/review/lookup?f={{ im }}">🔎 lookup</a></div>
        <form method="POST" action="/review/apply-image" class="qact flex flex-wrap gap-1 items-center mt-1" data-opt="stay">>
          <input type="hidden" name="op" value="attach">
          <input type="hidden" name="file" value="{{ im }}">
          <input name="q_id" placeholder="owner q_id (jaise OBG-003-009)" class="border rounded px-1 py-0.5 text-[11px] font-mono flex-1 min-w-32">
          <select name="side" class="border rounded px-1 py-0.5 text-[11px]">
            <option value="solution">solution</option><option value="question">question</option>
          </select>
          <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 text-[11px] w-20">
          <button class="bg-emerald-600 text-white px-2 py-0.5 rounded text-[11px]">Attach</button>
        </form>
      </div>
      {% endfor %}
    </div>
    {% endif %}
    {% for ov in r.orphan %}
    <div class="border border-amber-300 bg-amber-50 rounded p-2 text-[11px]">
      <b>📦 Unclaimed fragment (pipeline ne kisi ko diYA NAHI) {% if ov.orphan.pages %} — pages: <span class="font-mono">{{ ov.orphan.pages|join(' ') }}</span>{% endif %}</b>
      <div class="whitespace-pre-wrap mt-1">{{ ov.orphan.text }}</div>
      {% if ov.owner_qid %}
      <div class="mt-2 border-t border-amber-300 pt-1">
        <b>🤔 System ka best guess: {{ ov.owner_qid }}</b> — compare karo:
        {% if ov.already_present_in %}
        <div class="mt-1 bg-emerald-100 border border-emerald-400 rounded p-1 font-semibold">✅ Ye content ALREADY <span class="font-mono">{{ ov.already_present_in }}</span> ki solution me maujood hai (poore output ke against auto-check karke). Merge MAT karo — is card ko seedha <b>Skip</b> karo, reason: "already inside {{ ov.already_present_in }}".</div>
        {% endif %}
        <div class="mt-1 bg-white border rounded p-1">
          <b>Us question ki CURRENT solution (actual output):</b>
          <div class="whitespace-pre-wrap">{{ ov.owner_sol or '(abhi bilkul khaali hai — shayad yehi fragment asli solution hai!)' }}</div>
          {% for im in (ov.owner_imgs or []) %}<img class="thumb" loading="lazy" src="/review/img?f={{ im }}">{% endfor %}
        </div>
        <div class="mt-1 text-[10px] text-gray-600">guess galat lagta hai? kisi bhi question se compare karo (naya tab):
          <form method="GET" action="/review/lookup" target="_blank" class="inline">
            <input name="q" placeholder="q_id (jaise OBG-003-001)" class="border rounded px-1 py-0.5 font-mono w-40">
            <input type="hidden" name="chapter" value="{{ r.chapter_id }}">
            <button class="bg-indigo-600 text-white px-2 py-0.5 rounded">🔎 lookup</button>
          </form>
        </div>
        {% if ov.already_inside %}
        <div class="mt-1 text-red-700 font-semibold">⚠️ Ye text uski solution me ALREADY maujood lagta hai (extra copy). Merge MAT karo — bas Ignore.</div>
        {% else %}
        <form method="POST" action="/review/attach-orphan" class="qact mt-1 flex flex-wrap gap-1 items-center" data-opt="hide">>
          <input type="hidden" name="chapter_id" value="{{ r.chapter_id }}">
          <input type="hidden" name="frag_key" value="{{ ov.frag_key }}">
          <input type="hidden" name="flag_key" value="{{ r.flag_keys[0] }}">
          <input name="to_qid" value="{{ ov.owner_qid }}" class="border rounded px-1 py-0.5 font-mono w-32">
          <input name="reason" placeholder="why merge" class="border rounded px-1 py-0.5 flex-1">
          <button class="bg-emerald-700 text-white px-2 py-0.5 rounded">➕ Merge into this question (append + verify)</button>
        </form>
        {% endif %}
      </div>
      {% else %}
      <div class="mt-1 text-gray-600">(machine ka owner guess nahi mila — approve = 'dekh liya samajh gaya', ignore = 'faltu hai'. Merge manually nahi ho sakta yahan; agar content missing lagta hai toh mujhe q_id bata do.)</div>
      {% endif %}
    </div>
    {% endfor %}
    {% if r.expand %}
    <div class="text-[11px] bg-red-50 border border-red-200 rounded p-1">
      <b>Exactly ye rows INCOMPLETE:</b>
      {% for e in r.expand %}<div>• <span class="font-mono font-bold">{{ e.q_id }}</span> — missing: {{ e.missing }}</div>{% endfor %}
    </div>
    {% endif %}

    <form method="POST" action="/review-decide" class="qact flex flex-wrap gap-2 items-center" data-opt="hide">
      <input type="hidden" name="flag_keys" value='{{ r.flag_keys_json }}'>
      <input type="hidden" name="q_id" value="{{ r.q_id or '' }}">
      <input type="hidden" name="back" value="{{ self_qs }}">
      <input name="reason" placeholder="reason (optional)" class="text-xs border rounded px-2 py-1 flex-1">
      <button name="action" value="approved" class="bg-emerald-600 text-white text-xs px-2 py-1 rounded">✔ Approve{% if r.flag_keys|length > 1 %} (sab {{ r.flag_keys|length }}){% endif %}</button>
      <button name="action" value="ignored" class="bg-gray-500 text-white text-xs px-2 py-1 rounded">Skip{% if r.flag_keys|length > 1 %} (sab {{ r.flag_keys|length }}){% endif %}</button>
    </form>
    <p class="text-[10px] text-gray-500">Approve/Skip sirf ye flag band karte hain — content me koi change NAHI hota. Content badalna ho toh ✏️ Edit ya ➕ Merge use karo.</p>

    {% if r.q_id and masters.get(r.q_id) %}
    {% set m = masters[r.q_id] %}
    <div class="text-[11px] bg-slate-50 border rounded p-2 space-y-1">
      <div><b>Stem:</b> {{ m.question.text }}</div>
      <div><b>Options:</b> {% for o in m.options %}<span class="font-mono">{{ o.id }}.</span> {{ o.text }}{{ ' | ' if not loop.last }}{% endfor %}</div>
      <div><b>Answer: {{ (m.correct_options or ['?'])[0] }}</b>{% if m.tags %} · tags: {{ m.tags }}{% endif %}</div>
      {% if m.question.images %}<div>{% for im in m.question.images %}<img class="thumb" loading="lazy" src="/review/img?f={{ im.file }}">{% endfor %}</div>{% endif %}
      {% if m.solution.images %}<div>{% for im in m.solution.images %}<img class="thumb" loading="lazy" src="/review/img?f={{ im.file }}">{% endfor %}</div>{% endif %}
    </div>

    <details class="text-xs bg-gray-50 rounded p-2">
      <summary class="font-bold cursor-pointer">✏️ Edit content of {{ r.q_id }}</summary>
      <div class="space-y-2 mt-2">
        <form method="POST" action="/review/apply-text" class="qact" data-opt="stay">
          <input type="hidden" name="q_id" value="{{ r.q_id }}">
          <input type="hidden" name="field" value="question_text">
          <label class="font-semibold">Stem</label>
          <textarea name="value" class="w-full border rounded p-1" rows="2">{{ m.question.text }}</textarea>
          <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 w-full">
          <button class="bg-sky-600 text-white px-2 py-1 rounded mt-1">Save stem</button>
        </form>
        {% for o in m.options %}
        <form method="POST" action="/review/apply-text" class="qact flex gap-1 items-center" data-opt="stay">>
          <input type="hidden" name="q_id" value="{{ r.q_id }}">
          <input type="hidden" name="field" value="option">
          <input type="hidden" name="option_letter" value="{{ o.id }}">
          <b>{{ o.id }}.</b>
          <input name="value" value="{{ o.text }}" class="border rounded px-1 py-0.5 flex-1">
          <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 w-20">
          <button class="bg-sky-600 text-white px-2 py-1 rounded whitespace-nowrap">Save {{ o.id }}</button>
        </form>
        {% endfor %}
        <form method="POST" action="/review/apply-text" class="qact flex gap-2 items-center" data-opt="stay">>
          <input type="hidden" name="q_id" value="{{ r.q_id }}">
          <input type="hidden" name="field" value="correct_option">
          <label class="font-semibold">Answer</label>
          <select name="value" class="border rounded px-1 py-0.5">
            {% for L in ['A','B','C','D'] %}
            <option value="{{ L }}" {% if m.correct_options and m.correct_options[0] == L %}selected{% endif %}>{{ L }}</option>
            {% endfor %}
          </select>
          <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 flex-1">
          <button class="bg-sky-600 text-white px-2 py-0.5 rounded">Save answer</button>
        </form>

        {% for t in m.solution.tables %}
        <div class="border rounded p-1">
          <div class="rtd overflow-x-auto">{{ t.markdown | mdtable | safe }}</div>
          <details><summary class="text-gray-500 cursor-pointer">markdown source</summary>
          <form method="POST" action="/review/apply-text" class="qact" data-opt="stay">
            <input type="hidden" name="q_id" value="{{ r.q_id }}">
            <input type="hidden" name="field" value="table">
            <input type="hidden" name="table_index" value="{{ loop.index0 }}">
            <textarea name="value" class="w-full font-mono border rounded p-1" rows="4">{{ t.markdown }}</textarea>
            <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 w-full">
            <button class="bg-sky-600 text-white px-2 py-1 rounded mt-1">Save table {{ loop.index }}</button>
          </form></details>
          <form method="POST" action="/review/apply-text" class="qact" data-opt="stay" onsubmit="return confirm('Table {{ loop.index }} delete karein? (undo nahi)')">
            <input type="hidden" name="q_id" value="{{ r.q_id }}">
            <input type="hidden" name="field" value="table_delete">
            <input type="hidden" name="table_index" value="{{ loop.index0 }}">
            <input name="reason" placeholder="why delete" class="border rounded px-1 py-0.5 w-32">
            <button class="bg-rose-600 text-white px-2 py-0.5 rounded text-[11px]">🗑 delete table {{ loop.index }}</button>
          </form>
        </div>
        {% endfor %}
        {% for t in m.question.tables %}
        <div class="border rounded p-1">
          <div class="rtd overflow-x-auto">{{ t.markdown | mdtable | safe }}</div>
          <details><summary class="text-gray-500 cursor-pointer">markdown source</summary>
          <form method="POST" action="/review/apply-text" class="qact" data-opt="stay">
            <input type="hidden" name="q_id" value="{{ r.q_id }}">
            <input type="hidden" name="field" value="table_q">
            <input type="hidden" name="table_index" value="{{ loop.index0 }}">
            <textarea name="value" class="w-full font-mono border rounded p-1" rows="4">{{ t.markdown }}</textarea>
            <button class="bg-sky-600 text-white px-2 py-1 rounded mt-1">Save Q-table {{ loop.index }}</button>
          </form></details>
          <form method="POST" action="/review/apply-text" class="qact" data-opt="stay" onsubmit="return confirm('Q-table {{ loop.index }} delete karein?')">
            <input type="hidden" name="q_id" value="{{ r.q_id }}">
            <input type="hidden" name="field" value="table_q_delete">
            <input type="hidden" name="table_index" value="{{ loop.index0 }}">
            <input name="reason" placeholder="why delete" class="border rounded px-1 py-0.5 w-32">
            <button class="bg-rose-600 text-white px-2 py-0.5 rounded text-[11px]">🗑 delete Q-table {{ loop.index }}</button>
          </form>
        </div>
        {% endfor %}
        {% for side_field, side_label, side_tabs in [('table', 'solution', (m.solution.tables or [])), ('table_q', 'question', (m.question.tables or []))] %}
        <form method="POST" action="/review/apply-text" class="qact border-t pt-1" data-opt="stay">>
          <input type="hidden" name="q_id" value="{{ r.q_id }}">
          <input type="hidden" name="field" value="{{ side_field }}">
          <input type="hidden" name="table_index" value="{{ side_tabs|length }}">
          <label class="font-semibold text-[11px]">➕ Add new {{ side_label }} table (slot {{ side_tabs|length }}; uneven columns refused)</label>
          <textarea name="value" placeholder="| col | col |&#10;|---|---|&#10;| 1 | 2 |" class="w-full font-mono border rounded p-1" rows="3"></textarea>
          <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 w-full">
          <button class="bg-emerald-600 text-white px-2 py-1 rounded mt-1">Add {{ side_label }} table</button>
        </form>
        {% endfor %}

        <form method="POST" action="/review/apply-text" class="qact" data-opt="stay">
          <input type="hidden" name="q_id" value="{{ r.q_id }}">
          <input type="hidden" name="field" value="solution_text">
          <label class="font-semibold">Solution</label>
          <textarea name="value" class="w-full border rounded p-1" rows="4">{{ m.solution.text }}</textarea>
          <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 w-full">
          <button class="bg-sky-600 text-white px-2 py-1 rounded mt-1">Save solution</button>
        </form>

        <div class="border-t pt-2 space-y-1">
          <p class="font-semibold">🖼️ Images</p>
          {% for side, imgs in [('question', m.question.images), ('solution', m.solution.images)] %}
          {% for im in imgs %}
          <div class="border rounded p-1 bg-white">
            <img class="thumb" loading="lazy" src="/review/img?f={{ im.file }}">
            <div class="text-[10px] font-mono break-all text-gray-500">{{ im.file }} — <a class="text-sky-700 underline" href="/review/lookup?f={{ im.file }}">🔎 lookup</a></div>
            <form method="POST" action="/review/apply-image" class="qact flex flex-wrap gap-1 items-center text-[11px] mt-1" data-opt="stay">>
              <input type="hidden" name="q_id" value="{{ r.q_id }}">
              <input type="hidden" name="file" value="{{ im.file }}">
              <input type="hidden" name="side" value="{{ side }}">
              <input type="hidden" name="op" value="">
              <span class="font-mono break-all">[{{ side }}]</span>
              <button data-op="detach" class="bg-gray-600 text-white px-2 py-0.5 rounded">Detach</button>
              <input name="to_qid" placeholder="{{ (r.q_id).rsplit('-',1)[0] }}-007" class="border rounded px-1 py-0.5 w-32">
              <button data-op="move" class="bg-amber-600 text-white px-2 py-0.5 rounded">Move→</button>
              <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 flex-1">
            </form>
          </div>
          {% endfor %}
          {% endfor %}
          <form method="POST" action="/review/apply-image" class="qact text-[11px] space-y-1" data-opt="stay">>
            <input type="hidden" name="q_id" value="{{ r.q_id }}">
            <input type="hidden" name="op" value="attach">
            <input type="hidden" name="file" class="att-file" value="">
            <div class="font-semibold">📎 Attach unclaimed image — tap thumbnail to pick:</div>
            <div class="flex gap-2 overflow-x-auto py-1 border rounded bg-gray-50">
              {% for u in unclaimed.get(m.subject, []) %}
              <label class="att-pick shrink-0 border-2 border-transparent rounded p-1 text-center cursor-pointer hover:border-emerald-500" data-f="{{ u.f }}">
                <img src="/review/img?f={{ u.f }}" loading="lazy" style="max-height:90px" class="rounded">
                <span class="font-mono block">{{ u.f.split('/')[-1] }}</span>
                {% if u.page %}(book p{{ u.page }}){% endif %}
              </label>
              {% else %}
              <span class="text-gray-400 p-1">(is subject ka koi unclaimed image nahi)</span>
              {% endfor %}
            </div>
            <div class="flex flex-wrap gap-1 items-center">
              <span>picked: <b class="att-show font-mono text-emerald-700">—</b></span>
              <select name="side" class="border rounded px-1 py-0.5">
                <option value="solution">solution</option>
                <option value="question">question</option>
              </select>
              <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 flex-1">
              <button class="bg-emerald-600 text-white px-2 py-0.5 rounded">Attach</button>
            </div>
          </form>
          <form method="POST" action="/review/upload-image" enctype="multipart/form-data" class="qact flex flex-wrap gap-1 items-center text-[11px]" data-opt="stay">>
            <input type="hidden" name="q_id" value="{{ r.q_id }}">
            <span class="font-semibold">📤 ya manually upload karo (figure extract hi nahi hui toh):</span>
            <input type="file" name="image" accept="image/*" class="border rounded px-1 py-0.5 flex-1 min-w-32">
            <select name="side" class="border rounded px-1 py-0.5"><option value="solution">solution</option><option value="question">question</option></select>
            <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 w-20">
            <button class="bg-purple-600 text-white px-2 py-0.5 rounded">Upload</button>
          </form>
        </div>
      </div>
    </details>
    {% endif %}
  </div>
  {% endfor %}

  {% if pager.pages > 1 %}
  <div class="bg-white rounded shadow p-2 text-xs flex gap-2 items-center justify-between sticky bottom-0">
    {% set qs = '&chapter=' ~ (sel_chapter or '') ~ '&kind=' ~ (sel_kind or '') ~ '&sev=' ~ (sel_sev or '') %}
    <span>Page {{ pager.pg }}/{{ pager.pages }} — {{ pager.total }} issue(s) total</span>
    <span>
      {% if pager.pg > 1 %}<a class="bg-slate-700 text-white px-2 py-1 rounded" href="/review?pg={{ pager.pg - 1 }}{{ qs }}">← prev</a>{% endif %}
      {% if pager.pg < pager.pages %}<a class="bg-slate-700 text-white px-2 py-1 rounded" href="/review?pg={{ pager.pg + 1 }}{{ qs }}">next →</a>{% endif %}
    </span>
  </div>
  {% endif %}
  <p class="text-[10px] text-gray-500 text-center pb-6">State lives in data/review_decisions.jsonl + human_edit_ledger.jsonl — refresh/redeploy safe.</p>
</div>
<script>
document.querySelectorAll('button[data-op]').forEach(function(b){
  b.addEventListener('click', function(ev){
    ev.preventDefault();
    var form = ev.target.closest('form');
    form.querySelector('input[name="op"]').value = ev.target.dataset.op;
    if (ev.target.dataset.op === 'move' && !form.querySelector('input[name="to_qid"]').value.trim()) {
      alert('move ke liye to_qid chahiye'); return;
    }
    form.submit();
  });
});
// attach gallery: tap thumbnail -> fills the hidden file field of its form
document.querySelectorAll('.att-pick').forEach(function(l){
  l.addEventListener('click', function(ev){
    var form = ev.target.closest('form');
    form.querySelector('.att-file').value = l.dataset.f;
    form.querySelector('.att-show').textContent = l.dataset.f.split('/').pop();
    form.querySelectorAll('.att-pick').forEach(function(x){x.style.borderColor='transparent';});
    l.style.borderColor = '#059669';
    ev.preventDefault();
  });
});
// ---- optimistic AJAX for all queue-card actions (no full reload) --------
function rq_dec(id, delta){var e=document.getElementById(id);if(!e)return;var n=parseInt(e.textContent||'0',10);e.textContent=Math.max(0,n+delta);}
function rq_flash(form, txt, okc){var old=form.querySelector('.rq-fl');var f=document.createElement('span');f.className='rq-fl text-[11px] '+(okc?'text-emerald-700':'text-red-600');f.textContent=txt;if(old)old.replaceWith(f);else form.appendChild(f);setTimeout(function(){f.remove()},2400);}
// clicked submit-button tracker: new FormData(form) never includes the
// button that fired the submit, so buttons like name="action" (Approve/Skip)
// vanished from the AJAX body and the server got '' -> 'bad action'. Remember
// the last clicked named button and append it manually BEFORE posting.
var rq_lastBtn = null;
document.addEventListener('click', function(ev){
  var b = ev.target && ev.target.closest ? ev.target.closest('button[name]') : null;
  if (b) rq_lastBtn = b;
}, true);
document.addEventListener('submit', function(ev){
  var form=ev.target;
  if(!form.classList || !form.classList.contains('qact')) return;
  ev.preventDefault();
  var fd=new FormData(form);
  if (rq_lastBtn && form.contains(rq_lastBtn) && rq_lastBtn.name)
    fd.append(rq_lastBtn.name, rq_lastBtn.value);
  fd.append('ajax','1');
  var card=form.closest('[data-card]');
  var mode=form.getAttribute('data-opt')||'stay';
  if(mode==='hide' && card){card.style.opacity='0.35';card.style.pointerEvents='none';}
  rq_flash(form,'…',false);
  // form.action is SHADOWED by our own <button name="action"> (Approve/Skip)
  // -- it returns a RadioNodeList, not the URL ("[object RadioNodeList]" was
  // the 404 the user showed). getAttribute reads the real attribute value.
  var ACTION_URL=form.getAttribute('action');
  fetch(ACTION_URL,{method:'POST',body:fd}).then(function(resp){
     if(!resp.ok){ return resp.text().then(function(t){throw new Error('HTTP '+resp.status+': '+(t||'').slice(0,180));}); }
     var ct=resp.headers.get('content-type')||'';
     if(ct.indexOf('json')<0){ return resp.text().then(function(t){throw new Error('server ne non-JSON diya: '+(t||'').slice(0,180));}); }
     return resp.json();})
   .then(function(j){
     if(j.ok){
       if(mode==='hide'&&card){
         card.style.transition='opacity .25s';card.style.opacity='0.12';
         card.style.filter='grayscale(1)';
         var sev=card.getAttribute('data-sev');
         rq_dec(sev==='BLOCKER'?'cnt-blocker':'cnt-review',-1);
         rq_dec('cnt-resolved',1);rq_dec('cnt-flags',-1);
         rq_flash(form,'✅ '+ (j.msg||'saved'),true);
       }else if(mode==='hide-all'){
         document.querySelectorAll('[data-card]').forEach(function(c){c.style.opacity='0.12';c.style.filter='grayscale(1)';});
         location.href=(form.querySelector('input[name=back]')||{}).value||'/review';
         return;
       }
       rq_flash(form,'✅ '+(j.msg||'saved'),true);
     }else{
       if(card){card.style.opacity='1';card.style.pointerEvents='auto';}
       rq_flash(form,'❌ '+(j.msg||'failed'),false);
     }
   }).catch(function(e){if(card){card.style.opacity='1';card.style.pointerEvents='auto';}
     rq_flash(form,'❌ '+String((e&&e.message)||'network/server error'),false);
     if(window.console&&console.error){console.error('review-queue ajax',e);}});
});

// attach with no thumbnail picked -> block before the server round-trip
document.querySelectorAll('input.att-file').forEach(function(h){
  h.closest('form').addEventListener('submit', function(ev){
    if (!h.value) { alert('pehle thumbnail tap karke image choose karo'); ev.preventDefault(); }
  });
});
</script>
</body>
</html>
"""




def _unclaimed_panel(out, subject):
    """[{f, page}] for the attach gallery (thumb + name + book-page chip)."""
    out_l = []
    for u in review_queue.unclaimed_images(out, subject)[:60]:
        m = _re.search(r"-p(\d+)-", u.split("/")[-1])
        out_l.append({"f": u, "page": m.group(1) if m else ""})
    return out_l


def _review_context(msg=None, ok=True):
    """Assemble the page purely from disk (never from memory). Every row gets
    a 'view' enrichment: readable content, page links, inline images, and the
    exact split rows for chapter-level flags -- a human should never have to
    guess what a flag is about."""
    out = Path(pipeline.OUTPUT_ROOT)
    with review_queue.batch_cache():
        q = review_queue.collect_review_queue(out)
        masters = {}
        views = {}
        unclaimed = {}
        for r in q["rows"]:
            views[r["flag_key"]] = review_queue.flag_extra(out, r)
            qid = r.get("q_id")
            if qid and qid not in masters:
                row = review_queue._find_master_row(out, qid)
                if row is not None:
                    masters[qid] = row
                    subj = row.get("subject")
                    if subj and subj not in unclaimed:
                        unclaimed[subj] = _unclaimed_panel(out, subj)
        # expand incomplete-records per-qid expansion lists onto masters too
        for r in q["rows"]:
            for e in (views.get(r["flag_key"]) or {}).get("expand") or []:
                qid = e.get("q_id")
                if qid and qid not in masters:
                    row = review_queue._find_master_row(out, qid)
                    if row is not None:
                        masters[qid] = row
                        subj = row.get("subject")
                        if subj and subj not in unclaimed:
                            unclaimed[subj] = _unclaimed_panel(out, subj)
    return {"rows": q["rows"], "counts": q["counts"], "warnings": q["warnings"],
            "clear": q["clear"], "masters": masters, "views": views,
            "unclaimed": unclaimed, "msg": msg, "ok": ok,
            "chapters": sorted({r.get("chapter_id") for r in q["rows"]
                                if r.get("chapter_id")}),
            "kinds": sorted({r["kind"] for r in q["rows"]})}


@app.route("/review")
def review_home():
    msg = request.args.get("msg")
    ok = request.args.get("ok", "1") == "1"
    ctx = _review_context(msg, ok)
    sel_chapter = request.args.get("chapter") or ""
    sel_kind = request.args.get("kind") or ""
    sel_sev = request.args.get("sev") or ""
    ctx["self_qs"] = request.full_path if request.full_path.startswith("/review") else "/review"
    rows = ctx["rows"]
    if sel_chapter:
        rows = [r for r in rows if r.get("chapter_id") == sel_chapter]
    if sel_kind:
        rows = [r for r in rows if r.get("kind") == sel_kind]
    if sel_sev:
        rows = [r for r in rows if r.get("severity") == sel_sev]
    groups = review_queue.group_review_rows(pipeline.OUTPUT_ROOT, rows,
                                            ctx["views"])
    PAGE = 25
    try:
        pg = max(1, int(request.args.get("pg") or 1))
    except ValueError:
        pg = 1
    total_pages = max(1, (len(groups) + PAGE - 1) // PAGE)
    pg = min(pg, total_pages)
    slice_ = groups[(pg - 1) * PAGE: pg * PAGE]
    pager = {"pg": pg, "pages": total_pages, "total": len(groups), "per": PAGE}
    return render_template_string(REVIEW_PAGE, rows=slice_,
                                  pager=pager,
                                  n_raw_flags=len(rows),
                                  sel_chapter=sel_chapter, sel_kind=sel_kind,
                                  sel_sev=sel_sev, **{k: v for k, v in ctx.items()
                                                      if k != "rows"})


@app.route("/review/img")
def review_img():
    """Serve an extracted figure for eyeball-checking in the review screen."""
    rel = request.args.get("f", "")
    if ".." in rel or rel.startswith("/"):
        return "bad path", 400
    p = Path(pipeline.OUTPUT_ROOT) / "assets" / "questions" / rel
    if not p.exists() or not str(p.resolve()).startswith(
            str((Path(pipeline.OUTPUT_ROOT) / "assets" / "questions").resolve())):
        return "not found", 404
    return send_file(str(p), mimetype="image/webp")


_PAGECACHE = {}


@app.route("/review/page")
def review_page():
    """Render one page of the book PDF as PNG so the human can compare a flag
    against the actual printed page (cached in /tmp; zero tokens)."""
    subject = (request.args.get("subject") or "").strip()
    try:
        pnum = int(request.args.get("p", ""))
    except ValueError:
        return "bad page", 400
    pdf = INPUT_META_DIR / f"{subject}.pdf"
    if not pdf.exists():
        return (f"book PDF for {subject!r} not found at {pdf} -- the page "
                f"preview needs the input PDF on the volume", 404)
    key = f"{subject}-{pnum}"
    png = Path("/tmp/pagecache") / f"{key}.png"
    if key not in _PAGECACHE:
        png.parent.mkdir(parents=True, exist_ok=True)
        try:
            import subprocess
            subprocess.run(["pdftoppm", "-f", str(pnum), "-l", str(pnum),
                            "-r", "110", "-png", "-singlefile", str(pdf),
                            str(png.with_suffix(""))],
                           capture_output=True, timeout=45, check=True)
        except Exception as e:
            return f"page render failed: {e}", 500
        if png.exists():
            _PAGECACHE[key] = True
    if not png.exists():
        return "render produced nothing", 500
    return send_file(str(png), mimetype="image/png")


def _review_redirect(result, ok_word="done"):
    ok = bool(result.get("ok"))
    msg = (result.get("msg") or ok_word) if ok else \
        ("FAILED: " + str(result.get("error") or "unknown"))
    # AJAX path (optimistic UI): JSON, no page reload. Non-JS forms fall back
    # to the redirect flow unchanged -- both supported forever.
    if request.form.get("ajax") == "1" or \
            "application/json" in (request.headers.get("Accept") or ""):
        return jsonify({"ok": ok, "msg": msg,
                        "verified": bool(result.get("verified", ok)),
                        "result": {k: v for k, v in (result or {}).items()
                                   if isinstance(v, (str, int, bool, list, dict))}})
    back = request.form.get("back") or ""
    if back.startswith("/review"):
        sep = "&" if "?" in back else "?"
        return redirect(f"{back}{sep}ok={'1' if ok else '0'}&msg={msg}")
    return redirect(url_for("review_home", ok="1" if ok else "0", msg=msg))


@app.route("/review/decide-bulk", methods=["POST"])
def review_decide_bulk():
    """Decide EVERY currently-shown open flag in one click (the filter narrows
    the blast radius). Every flag gets its OWN ledger row, same as a manual
    click -- bulk is a shortcut over identical loops, not a black box."""
    action = request.form.get("action") or ""
    if action not in ("approved", "ignored"):
        return _review_redirect({"ok": False, "error": "bad action"})
    out = Path(pipeline.OUTPUT_ROOT)
    q = review_queue.collect_review_queue(out)
    rows = q["rows"]
    ch, kd, sv = (request.form.get("chapter") or "",
                  request.form.get("kind") or "", request.form.get("sev") or "")
    if ch:
        rows = [r for r in rows if r.get("chapter_id") == ch]
    if kd:
        rows = [r for r in rows if r.get("kind") == kd]
    if sv:
        rows = [r for r in rows if r.get("severity") == sv]
    n = 0
    for r in rows:
        res = review_queue.record_decision(
            out, r["flag_key"], action,
            request.form.get("reason") or f"bulk {action} via filter view",
            r.get("q_id") or None)
        n += bool(res.get("ok"))
    return _review_redirect({"ok": True}, f"bulk {action}: {n} flag(s) decided")


@app.route("/review-decide", methods=["POST"])
def review_decide():
    """One click can close a whole GROUPED card: flag_keys carries every flag
    the card covers (same question flagged by 3 sources = 1 decision)."""
    raw = request.form.get("flag_keys") or request.form.get("flag_key") or ""
    try:
        keys = _json.loads(raw)
        if isinstance(keys, str):
            keys = [keys]
    except Exception:
        keys = [raw]
    keys = [k for k in keys if k]
    if not keys:
        return _review_redirect({"ok": False, "error": "no flag key"})
    action = request.form.get("action") or ""
    reason = request.form.get("reason") or ""
    for k in keys:
        res = review_queue.record_decision(
            Path(pipeline.OUTPUT_ROOT), k, action, reason,
            request.form.get("q_id") or None)
        if not res.get("ok"):
            return _review_redirect(res)
    return _review_redirect(res, f"{len(keys)} flag(s) decided")


@app.route("/review/apply-text", methods=["POST"])
def review_apply_text():
    if state.get("status") == "processing":
        return _review_redirect(
            {"ok": False, "error": "extraction chal rahi hai — run khatam hone "
                                   "ke baad edit karo (freeze rule)"})
    ti = request.form.get("table_index")
    res = review_queue.apply_edit(
        Path(pipeline.OUTPUT_ROOT),
        request.form.get("q_id") or "",
        request.form.get("field") or "",
        request.form.get("value"),
        reason=request.form.get("reason") or "",
        option_letter=request.form.get("option_letter") or None,
        table_index=(int(ti) if ti not in (None, "") else None))
    if res.get("ok") and not res.get("verified"):
        res = {"ok": False,
               "error": "disk read-back verify FAILED — screen me 'saved' "
                        "dikhana galat hoga; files check karo"}
    return _review_redirect(res, "saved + verified on disk")


@app.route("/review/attach-orphan", methods=["POST"])
def review_attach_orphan():
    if state.get("status") == "processing":
        return _review_redirect({"ok": False, "error": "extraction chal rahi "
                                 "hai — run khatam hone ke baad merge karo"})
    res = review_queue.apply_orphan_merge(
        Path(pipeline.OUTPUT_ROOT),
        request.form.get("chapter_id") or "",
        request.form.get("frag_key") or "",
        request.form.get("to_qid") or "",
        reason=request.form.get("reason") or "")
    if res.get("ok"):
        # merged -> the orphan flag is genuinely handled now
        review_queue.record_decision(Path(pipeline.OUTPUT_ROOT),
                                     request.form.get("flag_key") or "",
                                     "resolved",
                                     reason="orphan merged into "
                                            + (request.form.get("to_qid") or ""),
                                     q_id=request.form.get("to_qid") or None)
    return _review_redirect(res,
                            (res.get("note") or "orphan merged") +
                            f" (tables: {res.get('tables', 0)}) + "
                            "verified on disk — flag resolved")


@app.route("/review/apply-image", methods=["POST"])
def review_apply_image():
    if state.get("status") == "processing":
        return _review_redirect(
            {"ok": False, "error": "extraction chal rahi hai — run khatam hone "
                                   "ke baad edit karo (freeze rule)"})
    res = review_queue.apply_image_op(
        Path(pipeline.OUTPUT_ROOT),
        request.form.get("q_id") or "",
        request.form.get("op") or "",
        request.form.get("file") or "",
        side=request.form.get("side") or "solution",
        option_letter=request.form.get("option_letter") or None,
        to_qid=request.form.get("to_qid") or None,
        reason=request.form.get("reason") or "")
    return _review_redirect(res, "image op done")


def _esc(v):
    import html as _html
    return _html.escape(str(v or ""), quote=False)


def _edit_forms_html(q_id, row, unclaimed_by_subject, out, back=""):
    """The SAME edit surface the queue card has (stem/options/answer/solution/
    tables/images), built for the lookup page so even READY/unflagged rows are
    editable. Manual escaping everywhere -- this HTML is injected with |safe."""
    q_ = (row or {}).get("question") or {}
    s_ = (row or {}).get("solution") or {}
    subj = (row or {}).get("subject")
    sel_opts = "".join(f"<option value='{L}'"
                       f"{' selected' if (row.get('correct_options') or [''])[0] == L else ''}>{L}</option>"
                       for L in "ABCD")
    b = [f'<input type="hidden" name="back" value="{_esc(back)}">']
    parts = [f"""
<div class="space-y-2 text-xs border-t border-sky-200 pt-2">
  <div class="font-bold text-slate-600">✏️ Edit (bina flag bhi chalega)</div>
  <form method="POST" action="/review/apply-text" class="qact" data-opt="stay">
    <input type="hidden" name="q_id" value="{q_id}">{b[0]}
    <input type="hidden" name="field" value="question_text">
    <label class="font-semibold">Stem</label>
    <textarea name="value" class="w-full border rounded p-1" rows="2">{_esc(q_.get("text"))}</textarea>
    <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 w-full">
    <button class="bg-sky-600 text-white px-2 py-1 rounded mt-1">Save stem</button>
  </form>"""]
    for o in row.get("options") or []:
        parts.append(f"""
  <form method="POST" action="/review/apply-text" class="qact flex gap-1 items-center" data-opt="stay">>
    <input type="hidden" name="q_id" value="{q_id}">{b[0]}
    <input type="hidden" name="field" value="option">
    <input type="hidden" name="option_letter" value="{o.get('id')}">
    <b>{o.get('id')}.</b>
    <input name="value" value="{_esc(o.get('text'))}" class="border rounded px-1 py-0.5 flex-1">
    <button class="bg-sky-600 text-white px-2 py-1 rounded">Save {o.get('id')}</button>
  </form>""")
    parts.append(f"""
  <form method="POST" action="/review/apply-text" class="qact flex gap-2 items-center" data-opt="stay">>
    <input type="hidden" name="q_id" value="{q_id}">{b[0]}
    <input type="hidden" name="field" value="correct_option">
    <label class="font-semibold">Answer</label>
    <select name="value" class="border rounded px-1 py-0.5">{sel_opts}</select>
    <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 flex-1">
    <button class="bg-sky-600 text-white px-2 py-0.5 rounded">Save answer</button>
  </form>
  <form method="POST" action="/review/apply-text" class="qact" data-opt="stay">
    <input type="hidden" name="q_id" value="{q_id}">{b[0]}
    <input type="hidden" name="field" value="solution_text">
    <label class="font-semibold">Solution</label>
    <textarea name="value" class="w-full border rounded p-1" rows="4">{_esc(s_.get("text"))}</textarea>
    <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 w-full">
    <button class="bg-sky-600 text-white px-2 py-1 rounded mt-1">Save solution</button>
  </form>""")
    for side_field, holder, side_lbl in (("table", s_, "solution"), ("table_q", q_, "question")):
        for i, t in enumerate(holder.get("tables") or []):
            parts.append(f"""
  <div class="border rounded p-1">
    <div class="rtd overflow-x-auto">{review_queue.md_to_html(t.get("markdown"))}</div>
    <details><summary class="text-gray-500 cursor-pointer">markdown</summary>
    <form method="POST" action="/review/apply-text" class="qact" data-opt="stay">
      <input type="hidden" name="q_id" value="{q_id}">{b[0]}
      <input type="hidden" name="field" value="{side_field}">
      <input type="hidden" name="table_index" value="{i}">
      <textarea name="value" class="w-full font-mono border rounded p-1" rows="3">{_esc(t.get("markdown"))}</textarea>
      <button class="bg-sky-600 text-white px-2 py-1 rounded mt-1">Save {side_lbl} table {i+1}</button>
    </form></details>
    <form method="POST" action="/review/apply-text" class="qact" data-opt="stay" onsubmit="return confirm('delete {side_lbl} table {i+1}?')">
      <input type="hidden" name="q_id" value="{q_id}">{b[0]}
      <input type="hidden" name="field" value="{side_field}_delete">
      <input type="hidden" name="table_index" value="{i}">
      <input name="reason" placeholder="why delete" class="border rounded px-1 py-0.5 w-28">
      <button class="bg-rose-600 text-white px-2 py-0.5 rounded">🗑 delete</button>
    </form>
  </div>""")
        parts.append(f"""
  <form method="POST" action="/review/apply-text" class="qact border-t border-dashed pt-1" data-opt="stay">>
    <input type="hidden" name="q_id" value="{q_id}">{b[0]}
    <input type="hidden" name="field" value="{side_field}">
    <input type="hidden" name="table_index" value="{len(holder.get('tables') or [])}">
    <label class="font-semibold">➕ Add {side_lbl} table</label>
    <textarea name="value" placeholder="| col | col |&#10;|---|---|&#10;| 1 | 2 |" class="w-full font-mono border rounded p-1" rows="3"></textarea>
    <button class="bg-emerald-600 text-white px-2 py-1 rounded mt-1">Add</button>
  </form>""")
    imgs = [(side, i) for side, holder in (("question", q_), ("solution", s_))
            for i in (holder.get("images") or [])]
    if imgs or True:
        parts.append('<div class="border-t border-dashed pt-1"><b>🖼️ Images</b></div>')
    for side, im in imgs:
        f = im.get("file") if isinstance(im, dict) else str(im)
        parts.append(f"""
  <div class="border rounded p-1 bg-white">
    <img loading="lazy" src="/review/img?f={f}" style="max-height:140px" class="rounded">
    <div class="text-[10px] font-mono break-all text-gray-500">{f}</div>
    <form method="POST" action="/review/apply-image" class="qact flex flex-wrap gap-1 items-center mt-1" data-opt="stay">>
      <input type="hidden" name="q_id" value="{q_id}">{b[0]}
      <input type="hidden" name="file" value="{f}">
      <input type="hidden" name="side" value="{side}">
      <input type="hidden" name="op" value="">
      <button data-imop="detach" class="bg-gray-600 text-white px-2 py-0.5 rounded">Detach</button>
      <input name="to_qid" placeholder="{q_id.rsplit('-',1)[0]}-007" class="border rounded px-1 py-0.5 w-28">
      <button data-imop="move" class="bg-amber-600 text-white px-2 py-0.5 rounded">Move→</button>
      <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 flex-1">
    </form>
  </div>""")
    pool = unclaimed_by_subject.get(subj, [])
    if pool:
        opts = "".join(f"<option value='{_esc(u['f'])}'>{_esc(u['f'].split('/')[-1])} (p{u['page']})</option>"
                       for u in pool)
        parts.append(f"""
  <form method="POST" action="/review/apply-image" class="qact flex flex-wrap gap-1 items-center" data-opt="stay">>
    <input type="hidden" name="q_id" value="{q_id}">{b[0]}
    <input type="hidden" name="op" value="attach">
    <span>Attach📎:</span>
    <select name="file" class="border rounded px-1 py-0.5 font-mono max-w-52">{opts}</select>
    <select name="side" class="border rounded px-1 py-0.5"><option value="solution">solution</option><option value="question">question</option></select>
    <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 w-24">
    <button class="bg-emerald-600 text-white px-2 py-0.5 rounded">Attach</button>
  </form>""")
    parts.append(f"""
  <form method="POST" action="/review/upload-image" enctype="multipart/form-data" class="qact flex flex-wrap gap-1 items-center border-t border-dashed pt-1" data-opt="stay">>
    <input type="hidden" name="q_id" value="{q_id}">{b[0]}
    <span class="font-semibold">📤 manual upload (pipeline ne nikali hi nahi ho toh):</span>
    <input type="file" name="image" accept="image/*" class="border rounded px-1 py-0.5 flex-1 min-w-40">
    <select name="side" class="border rounded px-1 py-0.5"><option value="solution">solution</option><option value="question">question</option></select>
    <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 w-24">
    <button class="bg-purple-600 text-white px-2 py-0.5 rounded">Upload + attach</button>
  </form>""")
    parts.append("</div>")
    return "".join(parts)


@app.route("/review/lookup")
def review_lookup():
    """Full-view lookup: ek q_id/number daalo -> poora question jaise final
    app me dikhega. Image file daali ho toh uska TRUE status (owner / disk /
    book page) + chapter is auto-guessed from the crop's page so you only
    ever type the question NUMBER."""
    out = Path(pipeline.OUTPUT_ROOT)
    f = (request.args.get("f") or "").strip()
    term = (request.args.get("q") or "").strip()
    chapter = (request.args.get("chapter") or "").strip() or None
    fstat = review_queue.image_status(out, f) if f else None
    unclaimed = {}

    # minimum typing: bare number without chapter -> try every chapter of the
    # subject (file name carries subject; chapter from page via chapters.json
    # ranges or split source_pages)
    auto_chapter = None
    if term and not chapter:
        if fstat and fstat.get("page"):
            subj = fstat["file"].split("/")[0]
            chapter = review_queue.chapter_for_page(out, subj,
                                                    fstat["page"]) or None
            if chapter:
                auto_chapter = chapter
    rows = review_queue.lookup_questions(out, term, chapter) if term else []
    if not rows and term and not chapter:
        # last-resort: q_no across ALL chapters
        rows = review_queue.lookup_questions(out, term)

    def full_view(r):
        return {
            "id": r.get("id"), "subject": r.get("subject"),
            "chapter_id": r.get("chapter_id"),
            "stem": str((r.get("question") or {}).get("text") or ""),
            "options": [{"id": o.get("id"),
                         "text": str(o.get("text") or ""),
                         "imgs": [i.get("file") for i in (o.get("images") or [])
                                  if isinstance(i, dict)]}
                        for o in (r.get("options") or [])],
            "ans": (r.get("correct_options") or ["?"])[0],
            "sol": str((r.get("solution") or {}).get("text") or ""),
            "q_imgs": [i.get("file") for i in ((r.get("question") or {}).get("images") or []) if isinstance(i, dict)],
            "s_imgs": [i.get("file") for i in ((r.get("solution") or {}).get("images") or []) if isinstance(i, dict)],
            "q_tables": (r.get("question") or {}).get("tables") or [],
            "s_tables": (r.get("solution") or {}).get("tables") or [],
            "qa": r.get("qa_status"), "qa_reasons": r.get("qa_reasons") or [],
            "pages": r.get("source_pages") or [],
        }
    cards = [full_view(r) for r in rows]
    back_url = ("/review/lookup?" + "&".join(
        f"{k}={v}" for k, v in (("q", term), ("chapter", chapter or ""),
                                ("f", f)) if v))
    for c, r in zip(cards, rows):
        if r.get("subject") not in unclaimed:
            unclaimed[r["subject"]] = _unclaimed_panel(out, r["subject"])
        c["edit_html"] = _edit_forms_html(c["id"], r, unclaimed, out,
                                          back=back_url)
    miss_hint = None
    if term and not cards:
        ch_m = _re.match(r"^([A-Za-z]+)-(\d{3})", term)
        ch = ch_m.group(0) if ch_m else (chapter or None)
        all_rows = review_queue.lookup_questions(out, "", None)
        pool = [r for r in all_rows if r.get("chapter_id") == ch] if ch else all_rows
        if pool:
            qnos = sorted(int(r["id"].rsplit("-", 1)[1]) for r in pool
                          if r.get("id"))
            if qnos:
                miss_hint = (f"{ch or 'is book'} me total {len(qnos)} questions hain "
                             f"(available: q{qnos[0]}..q{qnos[-1]}). "
                             f"'{term}' isme NAHI hai — number check karo.")
        elif not ch:
            miss_hint = f"'{term}' kisi bhi chapter me nahi mila."
    return render_template_string("""
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script><title>Lookup</title>
<style>.rtd table{border-collapse:collapse;font-size:12px;background:#fff}
.rtd th,.rtd td{border:1px solid #cbd5e1;padding:3px 8px;text-align:left}
img.big{max-width:100%;border:1px solid #94a3b8;border-radius:8px;background:#fff}</style></head>
<body class="bg-gray-100 p-3"><div class="max-w-3xl mx-auto space-y-3">
<h1 class="text-lg font-bold">🔎 Full-view lookup</h1>
<a href="/review" class="text-xs text-sky-700 underline">← queue wapas</a>
<form method="GET" class="bg-white rounded shadow p-3 text-sm flex flex-wrap gap-2">
  <input name="q" value="{{ term }}" placeholder="9 ya OBG-003-009 ya 003-009" class="border rounded px-2 py-1 flex-1 font-mono" autofocus>
  <input name="chapter" value="{{ chapter or '' }}" placeholder="chapter (optional — auto from image page)" class="border rounded px-2 py-1 w-56 font-mono">
  <input name="f" value="{{ f }}" placeholder="image file (optional)" class="border rounded px-2 py-1 w-64 font-mono">
  <button class="bg-slate-700 text-white px-4 py-1 rounded">Dikhao</button>
</form>
{% if auto_chapter %}<div class="text-[11px] text-emerald-700">ℹ️ chapter auto-detected from the image's page: <b>{{ auto_chapter }}</b> — tumhara sirf number kaafi tha</div>{% endif %}

{% if fstat %}
<div class="bg-white rounded shadow p-3 text-xs space-y-1 border-l-4 border-indigo-500">
  <b>🖼️ File status:</b> <span class="font-mono">{{ fstat.file }}</span>
  <div>disk pe hai: <b>{{ 'haan' if fstat.exists_on_disk else 'NAHI' }}</b>{% if fstat.page %} · book page: <b class="font-mono">{{ fstat.page }}</b>{% endif %}</div>
  <div class="text-sm">current owner(s): <b class="font-mono">{{ fstat.owners|join(', ') if fstat.owners else '❌ KISI KO NAHI (unlinked)' }}</b></div>
  <div><img class="big" src="/review/img?f={{ fstat.file }}"></div>
</div>
{% endif %}

{% for m in cards %}
<div class="bg-white rounded shadow p-4 space-y-3 border-l-4 border-sky-500 text-sm">
  <div class="flex flex-wrap gap-2 items-center">
    <span class="font-mono font-bold text-base">{{ m.id }}</span>
    <span class="text-xs bg-gray-200 rounded px-1">qa: {{ m.qa }}</span>
    <span class="text-xs">pages: <b class="font-mono">{{ m.pages|join(' ') }}</b></span>
    <a class="text-xs text-sky-700 underline" href="/review?chapter={{ m.chapter_id }}">is chapter ki queue →</a>
  </div>
  {% if m.qa_reasons %}<div class="text-[11px] text-amber-700">⚠ {{ m.qa_reasons|join('; ') }}</div>{% endif %}
  <div><b>Stem:</b><div class="mt-1 whitespace-pre-wrap">{{ m.stem }}</div></div>
  {% for im in m.q_imgs %}<div><img class="big" src="/review/img?f={{ im }}"><div class="text-[10px] font-mono text-gray-500">{{ im }}</div></div>{% endfor %}
  {% for t in m.q_tables %}<div class="rtd overflow-x-auto">{{ t.markdown | mdtable | safe }}</div>{% endfor %}
  <div class="border rounded p-2 bg-slate-50">
    <b>Options:</b>
    {% for o in m.options %}
    <div class="mt-1"><span class="font-mono font-bold">{{ o.id }}.</span> {{ o.text }}
      {% for im in o.imgs %}<img class="big" src="/review/img?f={{ im }}">{% endfor %}</div>
    {% endfor %}
  </div>
  <div class="font-bold">✅ Answer: {{ m.ans }}</div>
  <div><b>Solution (full):</b><div class="mt-1 whitespace-pre-wrap">{{ m.sol }}</div></div>
  {% for im in m.s_imgs %}<div><img class="big" src="/review/img?f={{ im }}"><div class="text-[10px] font-mono text-gray-500">{{ im }}</div></div>{% endfor %}
  {% for t in m.s_tables %}<div class="rtd overflow-x-auto">{{ t.markdown | mdtable | safe }}</div>{% endfor %}
  {% if fstat and fstat.exists_on_disk and m.id not in fstat.owners %}
  <form method="POST" action="/review/apply-image" class="qact flex flex-wrap gap-1 items-center border-t pt-2 text-xs" data-opt="stay">>
    <input type="hidden" name="q_id" value="{{ m.id }}">
    <input type="hidden" name="op" value="attach">
    <input type="hidden" name="file" value="{{ fstat.file }}">
    <span class="font-semibold">{{ fstat.file.split('/')[-1] }} ko {{ m.id }} me attach:</span>
    <select name="side" class="border rounded px-1 py-0.5"><option value="solution">solution</option><option value="question">question</option></select>
    <input name="reason" placeholder="why" class="border rounded px-1 py-0.5 flex-1">
    <button class="bg-emerald-600 text-white px-3 py-1 rounded font-bold">Attach</button>
  </form>
  {% endif %}
  <details class="text-xs"><summary class="font-bold text-sky-700 cursor-pointer">🛠 Edit karo ye question (flag nahi bhi ho toh)</summary>
  {{ m.edit_html|safe }}
  </details>
</div>
{% else %}
{% if term %}<div class="text-sm text-gray-600 bg-white rounded shadow p-3">koi row nahi mili: <b>{{ term }}</b>{% if miss_hint %}<br>💡 {{ miss_hint }}{% endif %}</div>{% endif %}
{% endfor %}
</div>
<script>
document.querySelectorAll('button[data-imop]').forEach(function(b){
  b.addEventListener('click', function(ev){
    ev.preventDefault();
    var form = ev.target.closest('form');
    form.querySelector('input[name="op"]').value = ev.target.dataset.imop;
    if (ev.target.dataset.imop === 'move' && !form.querySelector('input[name="to_qid"]').value.trim()) {
      alert('move ke liye to_qid chahiye'); return;
    }
    form.submit();
  });
});
</script>
</body></html>
""", f=f, term=term, chapter=chapter or "", auto_chapter=auto_chapter,
    fstat=fstat, cards=cards, miss_hint=miss_hint)


@app.route("/review/upload-image", methods=["POST"])
def review_upload_image():
    """Manual figure upload for pages the pipeline never extracted (chapter-end
    figures, image-only pages). The bytes are converted to webp and stored
    under the LOCKED q_id-locked slot name, then attached through the same
    verified path (all copies + manifest + ledger) as every other claim."""
    if state.get("status") == "processing":
        return _review_redirect({"ok": False, "error": "extraction chal rahi "
                                 "hai — run khatam hone ke baad upload karo"})
    q_id = (request.form.get("q_id") or "").strip()
    side = (request.form.get("side") or "solution").lower()
    reason = request.form.get("reason") or ""
    f = request.files.get("image")
    if f is None or not (f.filename or "").strip():
        return _review_redirect({"ok": False, "error": "koi image file nahi mili (upload karo)"})
    row = review_queue._find_master_row(pipeline.OUTPUT_ROOT, q_id)
    if row is None:
        return _review_redirect({"ok": False, "error": f"{q_id} exist nahi karta"})
    blob = f.read()
    if len(blob) > 8 * 1024 * 1024:
        return _review_redirect({"ok": False, "error": "8MB se bada file refused"})
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(blob)).convert("RGB")
        if im.size[0] < 80 or im.size[1] < 80:
            return _review_redirect({"ok": False, "error": "bahut chhota image "
                                    f"({im.size}) -- ye figure nahi lagta"})
        buf = _io.BytesIO()
        im.save(buf, "WEBP", quality=95)
        blob = buf.getvalue()
    except Exception as e:
        return _review_redirect({"ok": False, "error": f"image read nahi hui: {e}"})
    subject = q_id.split("-")[0]
    kind = {"question": "Q", "solution": "SOL"}.get(side)
    if not kind:
        return _review_redirect({"ok": False, "error": "side question/solution hona chahiye"})
    out_root = Path(pipeline.OUTPUT_ROOT)
    qdir = out_root / "assets" / "questions" / subject
    qdir.mkdir(parents=True, exist_ok=True)
    slot = review_queue._next_slot(out_root, subject, q_id, kind)
    rel = f"{subject}/{q_id}_{kind}_{slot:02d}.webp"
    (qdir / Path(rel).name).write_bytes(blob)     # final name from the start
    res = review_queue.apply_image_op(out_root, q_id, "attach", rel,
                                      side=side, reason=reason or "manual upload")
    if not res.get("ok"):
        try:
            (qdir / Path(rel).name).unlink()       # don't leave dust on failure
        except Exception:
            pass
        return _review_redirect(res)
    # provenance chain: the ownership ledger must know this claim too --
    # every other claim path writes here, a human upload must not skip it.
    review_queue._append_jsonl(Path(pipeline.OUTPUT_ROOT) / "data"
                               / "image_ownership.jsonl", {
        "subject": subject, "chapter_id": f"{subject}-{q_id.split('-')[1]}",
        "page": None, "file": rel, "owner": q_id, "slot": side,
        "method": "human_upload",
        "evidence": f"human uploaded '{f.filename}' via review (no extraction "
                    f"was possible for this figure)",
        "confidence": "high", "outcome": "claimed",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "obj_id": None, "final_file": rel})
    log(f"🖼️ manual upload {rel} -> {q_id} ({side}); {len(blob)} bytes")
    return _review_redirect(res, f"uploaded + attached as {rel}")


@app.errorhandler(Exception)
def _queue_ajax_errors(e):
    """AJAX actions must NEVER get an HTML error page back -- the optimistic
    reader does res.json() and an HTML body throws -> 'network/server error'
    hides the real failure (user's live report). Turn EVERY 500 into JSON.
    Also: a routing 404 says WHICH path was asked -- surface it so a stale
    deploy or a mis-wired button is diagnosable from the toast itself."""
    need_json = request.form.get("ajax") == "1" or \
        "application/json" in (request.headers.get("Accept") or "")
    is_404 = getattr(e, "code", None) == 404
    is_405 = getattr(e, "code", None) == 405
    if is_404 or is_405:
        log(f"❓ {e.code} asked: {request.method} {request.path} "
            f"(route {('missing' if is_404 else 'rejects this method')})")
        detail = (f"{e.code}: {request.method} {request.path} "
                  f"{'is not a registered route' if is_404 else 'rejects this method'}"
                  f" on this server")
    else:
        import traceback as _tb; _tb.print_exc()
        detail = f"server error: {type(e).__name__}: {e}"
    if need_json:
        return jsonify({"ok": False, "msg": detail, "path": request.path,
                        "method": request.method}), (getattr(e, "code", None) or 500)
    return (detail, getattr(e, "code", None) or 500)


@app.route("/review/ai-verify", methods=["POST"])
def review_ai_verify():
    """AI false-flag pass on a SEPARATE pool+model (flag_verifier.py).
    Background thread; only flag STATUS can change through it - content
    lock is hardcoded in the engine (never calls apply_*)."""
    if state.get("status") == "processing":
        return _review_redirect({"ok": False, "error": "pipeline busy"}, "…")

    def _job():
        import flag_verifier as fv
        log("🤖 AI verify pass started (separate 3.1 pool)...")
        def prog(checked, resolved):
            _p, d = fv._daily_counter(pipeline.OUTPUT_ROOT)
            if checked % 10 == 0:
                log(f"   … verify: {checked} checked, {resolved} auto-resolved, "
                    f"verification (3.1) calls used today: {d['calls']}")
        res = fv.run_verification(pipeline.OUTPUT_ROOT, progress=prog)
        if res.get("skipped"):
            log(f"🤖 verify skipped: {res['error']}")
            return
        log(f"🤖 AI verify done: {res['checked']} checked | "
            f"{res['resolved']} auto-resolved (high-conf false only) | "
            f"{res['kept']} kept | {res['sampled_back']} sampled-back | "
            f"{res['parse_failed']} parse-fail kept | "
            f"verification (3.1) calls used today: {res['calls_used_after']}/{res['budget_cap']}")

    threading.Thread(target=_job, daemon=True).start()
    return _review_redirect({"ok": True},
                            "AI verify pass started in background — progress "
                            "dashboard log me dikhega")


@app.route("/review/routes")
def review_routes_probe():
    """Self-diagnosis for ones chasing 'button kaam nahi karta': exactly which
    URLs this deployed process actually serves, plus route count. If
    review-decide is missing here -> the DEPLOY IS STALE/WONG service."""
    rules = sorted(r.rule for r in app.url_map.iter_rules())
    rev = [r for r in rules if "review" in r]
    return jsonify({
        "total_routes": len(rules),
        "review_routes": rev,
        "has_review_decide": "/review-decide" in rules,
        "has_bulk": "/review/decide-bulk" in rules,
        "has_ai_verify": "/review/ai-verify" in rules,
        "has_ai_resolved": "/review/ai-resolved" in rules,
        "has_unresolve": "/review/unresolve" in rules,
        "has_upload": "/review/upload-image" in rules,
        "has_lookup": "/review/lookup" in rules,
    })


@app.route("/review/ai-resolved")
def review_ai_resolved():
    """✅ Auto-resolved by AI (N) tab -- every one re-restorable."""
    out = Path(pipeline.OUTPUT_ROOT)
    import flag_verifier as fv
    rows = fv.list_ai_resolved(out)
    cards = "".join(
        f"""<div class="bg-white rounded shadow p-3 text-xs space-y-1">
          <div class="font-mono font-bold">{r.get('q_id') or '—'}</div>
          <div><span class="bg-gray-200 rounded px-1">{r.get('flag_kind')}</span>
               <span class="text-gray-500">pages: {r.get('pages')}</span>
               <span class="text-gray-500">conf: {r.get('confidence')}</span>
               {'<b class="text-orange-600">[self-audit sample — open hai abhi bhi]</b>' if r.get('sampled_back') else ''}
          </div>
          <div class="text-gray-700">🤖 "{{ r.get('ai_reason') }}"</div>
          <div class="text-gray-400">{{ r.get('ts') }}</div>
          <form method="POST" action="/review/unresolve" class="qact" data-opt="stay">
            <input type="hidden" name="flag_key" value="{r.get('flag_key')}">
            <button class="bg-slate-700 text-white px-2 py-0.5 rounded">🔁 reopen (wapas queue me)</button>
          </form>
        </div>""" for r in rows)
    return render_template_string(
        """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
        <script src="https://cdn.tailwindcss.com"></script></head><body class="bg-gray-100 p-3">
        <div class="max-w-2xl mx-auto space-y-2">
        <h1 class="text-lg font-bold">✅ Auto-resolved by AI ({{ n }})</h1>
        <a href="/review" class="text-xs text-sky-700 underline">← review queue</a>
        <p class="text-[11px] text-gray-500">Har row wapas queue me le sakte ho.
        Audit trail: data/ai_auto_resolved.jsonl (kabhi delete nahi hota).</p>
        {{ cards|safe }}
        </div></body></html>""", n=len(rows), cards=cards)


@app.route("/review/unresolve", methods=["POST"])
def review_unresolve():
    """One click restores an AI-resolved flag into the queue."""
    review_queue.record_decision(Path(pipeline.OUTPUT_ROOT),
                                 request.form.get("flag_key") or "",
                                 "reopened", reason="human reversed AI auto-resolve")
    return _review_redirect({"ok": True}, "reopened — queue me wapas aaya")


@app.route("/download-final")
def download_final():
    """Review-gated slim package: split/ + referenced assets + chapters.json +
    FORMAT.md + REVIEW_RECEIPT.json. REFUSES (423) while any queue row is
    undecided — a final file is only built after 'sab confirm ho gaya'."""
    out = Path(pipeline.OUTPUT_ROOT)
    gate = review_queue.gate_final_zip(out)
    if gate["locked"]:
        return (f"🔒 Final zip LOCKED: {gate['why']}. Pehle /review par sab "
                f"decide karo.", 423)
    try:
        res = review_queue.build_final_zip(out)
    except FileNotFoundError as e:
        q = review_queue.collect_review_queue(out)
        pending = q["counts"]["blocker"] + q["counts"]["review"]
        return (f"Final zip abhi ready nahi hai "
                f"({e.filename or e} missing). "
                f"{pending} issue(s) pending in /review.", 404)
    if not res.get("ok"):
        q = review_queue.collect_review_queue(out)
        pending = q["counts"]["blocker"] + q["counts"]["review"]
        why = res.get("why") or res
        return (f"Final zip abhi ready nahi hai: {why}. "
                f"{pending} issue(s) pending in /review.", 409)
    zpath = Path(res["path"])
    if not zpath.exists():
        return ("Final zip abhi ready nahi hai (build reported ok but "
                "final_export.zip is missing on disk).", 404)
    log(f"🚀 FINAL zip built: {res['receipt']['chapters']} chapter(s), "
        f"{res['receipt']['images_shipped']} image(s), "
        f"{res['receipt']['human_edits']} human edit(s) -> final_export.zip")
    return send_file(str(zpath), as_attachment=True,
                     download_name="final_export.zip")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
