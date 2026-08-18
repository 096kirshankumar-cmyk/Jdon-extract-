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
import threading
import time
import traceback
import zipfile
from pathlib import Path

import requests
from flask import Flask, render_template_string, request, redirect, url_for, send_file, jsonify
from werkzeug.utils import secure_filename

import qbank_pipeline as pipeline
import gemini_keys
import master_review_export  # MASTER_REVIEW/ package builder (read-only)

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
    rep = qbank_validator.run_hybrid(pipeline.OUTPUT_ROOT, audit=False)
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
            rep = qbank_validator.run_hybrid(pipeline.OUTPUT_ROOT, audit=False)
            log(f"🧪 Validation: {rep['summary']['flags_total']} flag(s) across "
                f"{rep['summary']['flagged_chapters']}/{rep['summary']['chapters']} chapters → "
                f"see data/validation_report.json")
        except Exception as ve:
            log(f"⚠️ post-run validation report failed (extraction unaffected): {ve}")
        with state_lock:
            state["status"] = "completed"
        log("✅ Done (or paused at daily Gemini limit — tap Run again tomorrow to resume).")
        make_zip()
        # After a successful run, also build the MASTER_REVIEW package
        # (read-only copy of split/ + assets/ + data/) so it's ready
        # for download alongside output_results.zip. Build failures
        # are LOGGED but do NOT fail the run -- the live output is
        # the source of truth, MASTER_REVIEW is just a convenience
        # view for the human reviewer.
        try:
            master_review_export.build_master_review_zip(Path(pipeline.OUTPUT_ROOT))
            log("📒 MASTER_REVIEW package ready -> tap 'Download MASTER_REVIEW' below.")
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

  <details class="bg-white rounded-lg shadow p-4">
    <summary class="text-sm font-semibold cursor-pointer">Recovery mode — heal specific pages (missing solutions etc.)</summary>
    <form action="/recover" method="POST" class="space-y-3 mt-3">
      <div>
        <label class="block text-sm font-semibold mb-1">Recovery plan (JSON)</label>
        <textarea name="plan" rows="6" class="w-full text-xs font-mono border p-2 rounded">{
  "PSY-016": {"pages": [214, 217], "reason": "recitation batch loss"},
  "PSY-001": {"pages": [17], "reason": "missing solution for q13"}
}</textarea>
        <p class="text-xs text-gray-500">Pages = true PDF file page numbers (see orphans.jsonl / unmatched image filenames). Renders ±1 neighbour page for context. Never overwrites existing text; only fills what is missing.</p>
      </div>
      <button class="w-full bg-indigo-600 text-white font-bold py-2 rounded" {% if state.status == 'processing' %}disabled{% endif %}>
        Run recovery
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
    """Smoke-test the v2 3-pass flow on ONE chapter, output into an isolated
    <OUTPUT_ROOT>_v2test/ folder -- never touches real data. Phone-friendly
    equivalent of running test_v2_chapter.py on the server."""
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

                cfg = {"subject": subject_code, "path": str(pdf_path), "page_offset": page_offset}
                st = pipeline.load_state()
                chapters_out = []
                log(f"🧪 [V2-TEST] {subject_code} chapter {chapter_no} (3-pass chal raha hai)...")
                import google.generativeai as genai
                # Multi-key pool: configures the first usable key and lets the
                # pipeline rotate to the next project as each one is spent.
                gemini_keys.init(st, pipeline.MAX_CALLS_PER_DAY)
                model = gemini_keys.track(genai.GenerativeModel(pipeline.GEMINI_MODEL))
                q_path = pipeline.DATA_DIR / "questions.jsonl"
                # run-16: per-chapter atomic rewrite inside process_pdf --
                # pass the PATH, not an append handle (crash-safe resume).
                pipeline.process_pdf(cfg, st, model, chapters_out, q_path,
                                     only_chapter_no=chapter_no)
                import json as _json
                rows = [_json.loads(l) for l in q_path.read_text().splitlines() if l.strip()] \
                    if q_path.exists() else []
                n = len(rows)
                ma = sum(1 for r in rows if not r.get("correct_options"))
                ms = sum(1 for r in rows if not (r.get("solution") or {}).get("text"))
                log(f"📊 [V2-TEST] Result: {n} questions | missing answer: {ma} | "
                    f"missing solution: {ms} (output: _v2test/ folder, asli data safe ✅)")
                # Non-empty fields do not prove a complete solution. Surface
                # the retry ledger prominently so a test is never described
                # as clean while truncated-solution suspects remain.
                ledger = test_root / "data" / "still_incomplete_after_retry.jsonl"
                pending = sum(1 for line in ledger.read_text(encoding="utf-8").splitlines()
                              if line.strip()) if ledger.exists() else 0
                if pending:
                    log(f"⚠️ [V2-TEST] REVIEW REQUIRED: {pending} item(s) remain in "
                        "still_incomplete_after_retry.jsonl — do not start full-book run yet.")
                else:
                    log("✅ [V2-TEST] No retry-ledger gaps remain; run validator before full book.")
                safety_events = [e for e in st.get("safety_blocked", [])
                                 if e.get("chapter_id") == f"{subject_code}-{chapter_no:03d}"]
                if safety_events:
                    safety_pages = sorted({str(p) for e in safety_events for p in e.get("pages", [])})
                    log("🚫 [V2-TEST] SAFETY BLOCKED page(s): " + ", ".join(safety_pages) +
                        " — recovered output may exist, but inspect these pages manually before a full-book run.")
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

RECOVERY_PLAN_PATH = Path("./recovery_plan.json")

@app.route("/recover", methods=["POST"])
def recover():
    if VOLUME_WARN:
        return ("Volume /data pe attach nahi hai -- pehle Railway Settings me Volume lagao "
                "(Mount Path: /data), warna jo bhi likha jayega wo next redeploy pe udd jayega.", 400)
    if not try_mark_processing():          # AUDIT-FIX: atomic busy-guard
        return redirect(url_for("index"))
    plan_text = request.form.get("plan", "").strip()
    if not plan_text:
        with state_lock:
            state["status"] = "idle"
        return "No plan provided", 400
    import json as _json
    try:
        plan = _json.loads(plan_text)
        assert isinstance(plan, dict) and plan, "plan must be a non-empty object"
        for cid, spec in plan.items():
            assert isinstance(spec.get("pages"), list) and spec["pages"], \
                f"{cid}: needs a non-empty 'pages' list"
    except (ValueError, AssertionError) as e:
        with state_lock:
            state["status"] = "idle"
        return f"Invalid plan JSON: {e}", 400
    RECOVERY_PLAN_PATH.write_text(plan_text)

    def _do_recover():
        try:
            log(f"🩹 Recovery started for: {', '.join(plan)}")
            pipeline.recover_pages(str(RECOVERY_PLAN_PATH))
            with state_lock:
                state["status"] = "completed"
            log("🩹 Recovery finished. Download zip to inspect healed rows.")
            make_zip()
        except SystemExit:
            with state_lock:
                state["status"] = "paused"
            log("⏸ Recovery paused at Gemini daily limit -- run it again tomorrow.")
            make_zip()
        except Exception as e:
            with state_lock:
                state["status"] = "failed"
                state["error"] = str(e)
            log(f"❌ Recovery error: {e}")
            traceback.print_exc()

    t = threading.Thread(target=_do_recover)
    t.daemon = True
    t.start()
    return redirect(url_for("index"))

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
        manifest = json.loads(
            (out / "MASTER_REVIEW" / "MASTER_REVIEW_MANIFEST.json").read_text(
                encoding="utf-8"))
        log(f"📒 MASTER_REVIEW: {manifest.get('total_chapters', 0)} chapter(s), "
            f"{manifest.get('total_files_copied', 0)} file(s) copied, "
            f"{manifest.get('total_images_copied', 0)} image(s) copied, "
            f"{manifest.get('total_files_missing', 0)} file(s) missing, "
            f"{manifest.get('total_images_missing', 0)} image(s) missing "
            f"-> {zip_path.name}")
    except Exception:
        log(f"📒 MASTER_REVIEW zip ready -> {zip_path}")
    return send_file(str(zip_path), as_attachment=True,
                     download_name="MASTER_REVIEW.zip")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
