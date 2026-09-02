#!/usr/bin/env python3
"""
PDF Fixer dashboard (Railway).

A tiny Flask app that does exactly one job: clean a garbled medical MCQ PDF
before any extraction runs.  It uploads a PDF, runs fix_pdf.py in the
background (watermark removal; OCR text-layer rebuild is OPTIONAL and off by
default), streams the live log, and serves the final CLEAN PDF plus the
watermark-free intermediate.

There is no JSON/QBank/Gemini/quota/date code here.  All PDF work lives in
fix_pdf.py, which runs as a subprocess per job so a hung OCR run can be
cancelled without touching the web worker.

Run:
    gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 0 app:app
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template_string,
                   request, send_file, url_for)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent
FIX_SCRIPT = _REPO_ROOT / "fix_pdf.py"

JOBS_DIR = Path(os.environ.get("PDF_FIX_JOBS_DIR", "./pdf_fix_jobs")).resolve()
MAX_CONCURRENT = max(1, int(os.environ.get("PDF_FIX_MAX_CONCURRENT", "1")))
MAX_JOB_KEEP = max(1, int(os.environ.get("PDF_FIX_MAX_JOB_KEEP", "20")))
MAX_UPLOAD_MB = int(os.environ.get("PDF_FIX_MAX_UPLOAD_MB", "200"))
DEFAULT_JOBS = max(1, min(8, int(os.environ.get("PDF_FIX_OCR_JOBS", "1"))))
OUT_TYPE_DEFAULT = os.environ.get("PDF_FIX_OUTPUT_TYPE", "pdfa")
if OUT_TYPE_DEFAULT not in ("pdfa", "pdf"):
    OUT_TYPE_DEFAULT = "pdfa"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
# make DEFAULT_JOBS / OUT_TYPE_DEFAULT available inside the page template
app.jinja_env.globals["DEFAULT_JOBS"] = DEFAULT_JOBS
app.jinja_env.globals["OUT_TYPE_DEFAULT"] = OUT_TYPE_DEFAULT

# --------------------------------------------------------------------------
# Job state (in-memory; single gunicorn worker)
# --------------------------------------------------------------------------

LOCK = threading.Lock()
JOBS: dict[str, dict] = {}      # job id -> state dict
ORDER: list[str] = []           # submission order
PROCS: dict[str, subprocess.Popen] = {}  # job id -> running process

LOG_LINES = 5000                # ring buffer per job
API_LOG_TAIL = 800              # lines returned per status request


def _now() -> float:
    return time.time()


def _clean_job_dir(job_dir: Path) -> None:
    """Best effort delete of a job directory."""
    import shutil
    shutil.rmtree(job_dir, ignore_errors=True)


def prune_old_jobs() -> None:
    """Drop old job states and job directories beyond MAX_JOB_KEEP."""
    with LOCK:
        while len(ORDER) > MAX_JOB_KEEP:
            old = ORDER.pop(0)
            jid = old
            job = JOBS.pop(jid, None)
            if job:
                _clean_job_dir(Path(job["job_dir"]))
    # also prune directories with no state object (crashed-runs leftovers)
    try:
        entries = sorted(
            JOBS_DIR.iterdir(),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in entries[MAX_JOB_KEEP:]:
            if p.is_dir():
                _clean_job_dir(p)
    except OSError:
        pass


def _append_log(job: dict, line: str) -> None:
    job["log"].append(line.rstrip("\n"))


def _spawn(job: dict) -> None:
    """Launch fix_pdf.py for a job (must be called with job status=running)."""
    cmd = [
        sys.executable, "-u", str(FIX_SCRIPT),
        str(job["input_path"]),
        "--output", str(job["output_path"]),
        "--intermediate", str(job["step1_path"]),
        "--language", job["language"],
        "--jobs", str(job["jobs"]),
        "--output-type", job["output_type"],
        "--samples", job["samples"],
    ]
    if job["deskew"]:
        cmd.append("--deskew")
    else:
        cmd.append("--no-deskew")
    if job["clean"]:
        cmd.append("--clean")
    else:
        cmd.append("--no-clean")
    # DEFAULT: watermark removal only (--ocr off).  OCR re-renders every page
    # and usually produces a much larger, less accurate file on low-DPI
    # uploads, so it must be an explicit user choice.
    if job["ocr"]:
        cmd.append("--ocr")
    else:
        cmd.append("--skip-ocr")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    log = job["log"]
    _append_log(job, f"$ {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(_REPO_ROOT),
        )
    except Exception as exc:  # file not found etc.
        job["status"] = "error"
        job["error"] = f"could not start fix_pdf.py: {exc}"
        job["finished"] = _now()
        _append_log(job, f"ERROR: {job['error']}")
        return

    with LOCK:
        PROCS[job["id"]] = proc

    def _reader(proc_: subprocess.Popen) -> None:
        for line in proc_.stdout or []:
            with LOCK:
                _append_log(job, line)
        rc = proc_.wait()
        with LOCK:
            PROCS.pop(job["id"], None)
            job["exit_code"] = rc
            # only transition if the user did not cancel it
            if job["status"] == "running":
                out_ok = job["output_path"].exists() and job["output_path"].stat().st_size > 0
                if rc == 0 and out_ok:
                    job["status"] = "done"
                    job["size_mb"] = round(job["output_path"].stat().st_size / 1e6, 2)
                    _append_log(job, f"DONE: {job['output_path']}"
                                     f" ({job['size_mb']} MB)")
                elif rc < 0:
                    # negative code = killed by a signal (-9 = SIGKILL, the
                    # classic out-of-memory killer)
                    job["status"] = "error"
                    job["error"] = (
                        f"fix_pdf.py was killed by signal {-rc} "
                        f"({'-9' if rc == -9 else ''} - almost always "
                        f"OUT OF MEMORY on small Railway plans). "
                        f"Retry with: Jobs = 1, 'Clean' OFF, Output type = PDF."
                    )
                    _append_log(job, f"ERROR: {job['error']}")
                else:
                    job["status"] = "error"
                    job["error"] = (f"fix_pdf.py exited with code {rc}; "
                                    f"see log for details")
                    _append_log(job, f"ERROR: {job['error']}")
            job["finished"] = _now()
        _append_log(job, f"job finished at {time.strftime('%H:%M:%S')}")

    threading.Thread(target=_reader, args=(proc,), daemon=True).start()


def _dispatcher() -> None:
    """Start queued jobs respecting MAX_CONCURRENT."""
    while True:
        to_start = None
        with LOCK:
            running = sum(1 for j in JOBS.values() if j["status"] == "running")
            if running < MAX_CONCURRENT:
                for jid in ORDER:
                    if JOBS.get(jid, {}).get("status") == "queued":
                        to_start = JOBS[jid]
                        to_start["status"] = "running"
                        to_start["started"] = _now()
                        break
        if to_start is None:
            time.sleep(0.5)
            continue
        _append_log(to_start, f"started at {time.strftime('%H:%M:%S')} "
                              f"(jobs running: {running + 1})")
        _spawn(to_start)


def _ensure_dispatcher() -> None:
    if not getattr(_ensure_dispatcher, "started", False):
        _ensure_dispatcher.started = True
        threading.Thread(target=_dispatcher, daemon=True).start()


def _new_job(name: str, options: dict) -> dict:
    jid = uuid.uuid4().hex[:16]
    job_dir = JOBS_DIR / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "id": jid,
        "name": name,
        "created": _now(),
        "started": None,
        "finished": None,
        "status": "queued",
        "exit_code": None,
        "error": None,
        "log": deque(maxlen=LOG_LINES),
        "job_dir": str(job_dir),
        "input_path": job_dir / "input.pdf",
        "output_path": job_dir / "output_clean.pdf",
        "step1_path": job_dir / "step1_no_watermark.pdf",
        "size_mb": None,
        **options,
    }
    with LOCK:
        JOBS[jid] = job
        ORDER.append(jid)
    _ensure_dispatcher()
    prune_old_jobs()
    return job


def _job_public(job: dict, with_log: bool = True) -> dict:
    d = {
        "id": job["id"],
        "name": job["name"],
        "created": job["created"],
        "started": job["started"],
        "finished": job["finished"],
        "status": job["status"],
        "exit_code": job["exit_code"],
        "error": job["error"],
        "size_mb": job["size_mb"],
        "options": {k: job[k] for k in
                    ("language", "jobs", "output_type", "deskew", "clean",
                     "ocr")},
        "has_output": job["output_path"].exists(),
        "has_step1": job["step1_path"].exists(),
    }
    if with_log:
        d["log"] = list(job["log"])[-API_LOG_TAIL:]
    return d


def _jobs_public() -> list[dict]:
    with LOCK:
        return [_job_public(j, with_log=False)
                for jid in reversed(ORDER) if (j := JOBS.get(jid))]


def _get_job(job_id: str) -> dict | None:
    with LOCK:
        return JOBS.get(job_id)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template_string(PAGE_TEMPLATE, job_id=None,
                                  jobs=_jobs_public(),
                                  max_upload_mb=MAX_UPLOAD_MB)


@app.get("/job/<job_id>")
def job_page(job_id):
    job = _get_job(job_id)
    if job is None:
        abort(404)
    return render_template_string(PAGE_TEMPLATE, job_id=job_id,
                                  jobs=_jobs_public(),
                                  max_upload_mb=MAX_UPLOAD_MB)


@app.get("/api/jobs")
def api_jobs():
    return jsonify({"jobs": _jobs_public()})


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    job = _get_job(job_id)
    if job is None:
        abort(404)
    return jsonify(_job_public(job))


@app.get("/upload")
def upload_page():
    # Visiting /upload directly in a browser must never 405/500 - the form
    # lives at / (upload is POST /upload), so just go there.
    return redirect(url_for("index"))


@app.post("/upload")
def upload():
    try:
        file = request.files.get("pdf")
    except RequestEntityTooLarge:
        return render_template_string(
            PAGE_TEMPLATE, job_id=None, jobs=_jobs_public(),
            max_upload_mb=MAX_UPLOAD_MB,
            upload_error=f"File is larger than the {MAX_UPLOAD_MB} MB limit."
        ), 413
    if file is None or not file.filename:
        return render_template_string(
            PAGE_TEMPLATE, job_id=None, jobs=_jobs_public(),
            max_upload_mb=MAX_UPLOAD_MB,
            upload_error="No file selected."
        ), 400
    fname = secure_filename(file.filename) or "input.pdf"
    if not fname.lower().endswith(".pdf"):
        return render_template_string(
            PAGE_TEMPLATE, job_id=None, jobs=_jobs_public(),
            max_upload_mb=MAX_UPLOAD_MB,
            upload_error="Only .pdf files are accepted."
        ), 400

    options = {
        "language": request.form.get("language", "eng+hin"),
        "jobs": max(1, min(16, int(request.form.get("jobs", str(DEFAULT_JOBS)) or DEFAULT_JOBS))),
        "output_type": request.form.get("output_type", OUT_TYPE_DEFAULT),
        # NOTE: an UNCHECKED checkbox is simply absent from the form data, so
        # the default for absent fields must be "off" (checked boxes in the
        # template send "on").  clean/ocr are unchecked by default; deskew is
        # checked by default and therefore normally present.
        "deskew": request.form.get("deskew", "off") != "off",
        "clean": request.form.get("clean", "off") == "on",
        "ocr": request.form.get("ocr", "off") == "on",
        "samples": request.form.get("samples", "1,50,100,200,300"),
    }
    if options["language"] not in ("eng+hin", "eng"):
        options["language"] = "eng+hin"
    if options["output_type"] not in ("pdfa", "pdf"):
        options["output_type"] = "pdfa"

    job = _new_job(fname, options)
    # STREAM to disk in chunks (never file.read() into memory - a 100 MB
    # upload is 100 MB of RAM twice, which OOM-kills small Railway workers).
    file.save(job["input_path"])
    with open(job["input_path"], "rb") as fh:
        data = fh.read(5)
    if data != b"%PDF-":
        _clean_job_dir(Path(job["job_dir"]))
        with LOCK:
            JOBS.pop(job["id"], None)
            if job["id"] in ORDER:
                ORDER.remove(job["id"])
        return render_template_string(
            PAGE_TEMPLATE, job_id=None, jobs=_jobs_public(),
            max_upload_mb=MAX_UPLOAD_MB,
            upload_error="Uploaded file is not a valid PDF (missing %PDF header)."
        ), 400
    _append_log(job, f"uploaded {fname} ({job['input_path'].stat().st_size // 1024} KB)")
    return render_template_string(
        PAGE_TEMPLATE, job_id=job["id"], jobs=_jobs_public(),
        max_upload_mb=MAX_UPLOAD_MB, upload_error=None,
    )


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    job = _get_job(job_id)
    if job is None:
        abort(404)
    with LOCK:
        if job["status"] in ("running", "queued"):
            proc = PROCS.get(job_id)
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["finished"] = _now()
                _append_log(job, "CANCELLED: removed from queue")
            else:
                job["status"] = "cancelled"
                _append_log(job, "CANCELLED: terminating process ...")
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
    return jsonify({"ok": True, "status": job["status"]})


@app.get("/download/<job_id>/<kind>")
def download(job_id, kind):
    job = _get_job(job_id)
    if job is None:
        abort(404)
    if kind == "clean":
        path, label = job["output_path"], "clean"
    elif kind == "step1":
        path, label = job["step1_path"], "step1"
    else:
        abort(404)
    if not path.exists():
        abort(404)
    stem = Path(job["name"]).stem
    return send_file(
        path,
        as_attachment=True,
        download_name=f"{stem}_CLEAN.pdf" if kind == "clean" else f"{stem}_step1_no_watermark.pdf",
        mimetype="application/pdf",
    )


@app.get("/preview/<job_id>")
def preview(job_id):
    job = _get_job(job_id)
    if job is None or not job["output_path"].exists():
        abort(404)
    resp = send_file(
        job["output_path"],
        as_attachment=False,
        mimetype="application/pdf",
        download_name="preview.pdf",
    )
    resp.headers["Content-Disposition"] = "inline"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.get("/health")
def health():
    with LOCK:
        running = sum(1 for j in JOBS.values() if j["status"] == "running")
    return jsonify({"ok": True, "running_jobs": running,
                    "jobs_dir": str(JOBS_DIR)})


@app.errorhandler(413)
def too_large(e):
    """Upload over the limit: friendly page instead of the raw 413."""
    return render_template_string(
        PAGE_TEMPLATE, job_id=None, jobs=_jobs_public(),
        max_upload_mb=MAX_UPLOAD_MB,
        upload_error=f"File is larger than the {MAX_UPLOAD_MB} MB limit "
                     f"(reduce or re-save it, then upload again).",
    ), 413


@app.errorhandler(404)
def not_found(e):
    if str(request.path).startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template_string(
        PAGE_TEMPLATE, job_id=None, jobs=_jobs_public(),
        max_upload_mb=MAX_UPLOAD_MB,
        upload_error="Page not found.",
    ), 404


@app.errorhandler(500)
def internal_error(e):
    """Never leak a stack to the browser; show a friendly retry message."""
    try:
        exc = e.original_exception or e
        print(f"[500] {request.method} {request.path}: {exc!r}",
              file=sys.stderr)
    except Exception:
        pass
    return render_template_string(
        PAGE_TEMPLATE, job_id=None, jobs=_jobs_public(),
        max_upload_mb=MAX_UPLOAD_MB,
        upload_error="Server hit an internal error - please try again; if it "
                     "keeps happening, re-upload the same file.",
    ), 500


# --------------------------------------------------------------------------
# Template
# --------------------------------------------------------------------------

PAGE_TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF Fixer</title>
<style>
 :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --txt:#e2e8f0; --dim:#94a3b8;
         --acc:#38bdf8; --ok:#4ade80; --warn:#facc15; --err:#f87171; }
 * { box-sizing:border-box; }
 body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
        background:var(--bg); color:var(--txt); }
 .wrap { max-width:860px; margin:0 auto; padding:16px; }
 h1 { font-size:22px; margin:8px 0 4px; }
 .sub { color:var(--dim); font-size:13px; margin-bottom:18px; }
 .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
         padding:16px; margin-bottom:16px; }
 label { display:block; font-size:13px; color:var(--dim); margin:10px 0 4px; }
 input[type=file], select, .btn {
   width:100%; padding:10px; border-radius:8px; border:1px solid var(--line);
   background:#0b1220; color:var(--txt); font-size:15px; }
 .btn { display:inline-block; text-align:center; text-decoration:none;
        background:var(--acc); color:#06263a; font-weight:600; cursor:pointer;
        border:none; margin-top:12px; }
 .btn[disabled] { opacity:.5; cursor:not-allowed; }
 .row { display:flex; gap:12px; flex-wrap:wrap; }
 .row > div { flex:1 1 160px; }
 .checks { display:flex; gap:18px; margin-top:10px; flex-wrap:wrap; }
 .checks label { display:flex; align-items:center; gap:6px; margin:0; color:var(--txt);
                 font-size:14px; }
 table { width:100%; border-collapse:collapse; font-size:14px; }
 th, td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); }
 td.name { max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
 .badge { display:inline-block; padding:2px 10px; border-radius:99px; font-size:12px;
          font-weight:600; }
 .st-queued { background:#3b4a63; color:#cbd5e1; }
 .st-running{ background:#0e7490; color:#cffafe; }
 .st-done   { background:#14532d; color:#bbf7d0; }
 .st-error  { background:#7f1d1d; color:#fecaca; }
 .st-cancelled{ background:#525252; color:#e5e5e5; }
 .log { background:#0b1220; border:1px solid var(--line); border-radius:8px;
        padding:10px; font-family:ui-monospace,Menlo,Consolas,monospace;
        font-size:12px; line-height:1.5; max-height:420px; overflow:auto;
        white-space:pre-wrap; word-break:break-word; }
 .err { color:var(--err); background:#450a0a; border:1px solid #7f1d1d;
        border-radius:8px; padding:10px; margin-bottom:12px; font-size:14px; }
 a { color:var(--acc); }
 .muted { color:var(--dim); font-size:12px; }
 .dl { display:flex; gap:10px; margin-top:8px; flex-wrap:wrap; }
 .dl .btn { margin-top:0; }
 iframe { width:100%; height:70vh; border:1px solid var(--line); border-radius:8px;
          background:white; margin-top:10px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📄 PDF Fixer</h1>
  <div class="sub">Watermark removal (page content, images &amp; size stay
    like the upload). OCR text layer = optional extra — no JSON/QBank pipeline
    here.</div>

  {% if upload_error %}<div class="err">⚠ {{ upload_error }}</div>{% endif %}

  <div class="card">
    <form method="post" action="/upload" enctype="multipart/form-data">
      <label>PDF file (max {{ max_upload_mb }} MB)</label>
      <input type="file" name="pdf" accept="application/pdf,.pdf" required>
      <div class="checks">
        <label style="font-weight:600"><input type="checkbox" name="ocr"> Rebuild text layer with OCR
          <span class="muted">(optional — re-renders pages, so the text layer
          is replaced and the file gets much bigger; on low-DPI files the
          words can be wrong. Off by default.)</span></label>
      </div>
      <div class="row" id="ocrow">
        <div><label>OCR language</label>
          <select name="language">
            <option value="eng+hin" selected>English + Hindi</option>
            <option value="eng">English only</option>
          </select></div>
        <div><label>Parallel OCR jobs <span class="muted">(1 = safest)</span></label>
          <select name="jobs">
            {% for n in [1,2,3,4,6,8] %}<option value="{{ n }}" {% if n==DEFAULT_JOBS %}selected{% endif %}>{{ n }}</option>{% endfor %}
          </select></div>
        <div><label>Output type</label>
          <select name="output_type">
            <option value="pdfa" {% if OUT_TYPE_DEFAULT=='pdfa' %}selected{% endif %}>PDF/A (needs ghostscript)</option>
            <option value="pdf" {% if OUT_TYPE_DEFAULT=='pdf' %}selected{% endif %}>PDF</option>
          </select></div>
      </div>
      <div class="checks">
        <label><input type="checkbox" name="deskew" checked> Deskew pages</label>
        <label><input type="checkbox" name="clean"> Clean scan artifacts
          <span class="muted">(memory-heavy, off by default)</span></label>
      </div>
      <p class="muted">Tip: default keeps images, figures and file size like
        your upload. Only when <b>OCR is ON</b>, on small Railway plans use
        <b>Jobs = 1</b>, <b>Clean OFF</b> and <b>Output PDF</b> to avoid
        out-of-memory kills.</p>
      <button class="btn" type="submit">Upload &amp; Fix PDF</button>
    </form>
  </div>

  <div class="card">
    <h2 style="font-size:16px;margin:0 0 8px">Jobs</h2>
    {% if jobs %}
      <table>
        <tr><th>File</th><th>Status</th><th>Size</th><th></th></tr>
        {% for j in jobs %}
        <tr>
          <td class="name" title="{{ j.name }}">{{ j.name }}</td>
          <td><span class="badge st-{{ j.status }}">{{ j.status }}</span></td>
          <td>{{ j.size_mb if j.size_mb is not none else '—' }} MB</td>
          <td><a href="/job/{{ j.id }}">open</a></td>
        </tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="muted">No jobs yet. Upload a PDF above.</p>
    {% endif %}
  </div>

  {% if job_id %}
  <div class="card" id="jobcard">
    <h2 style="font-size:16px;margin:0">Job <code>{{ job_id }}</code></h2>
    <p id="jobstatus" class="muted">loading …</p>
    <div id="errorbox" class="err" style="display:none"></div>
    <div id="actions" style="display:none" class="dl">
      <a id="dlclean" class="btn" href="#">⬇ Download CLEAN PDF</a>
      <a id="dlstep1" class="btn" href="#">⬇ step1 (no watermark)</a>
      <a id="preview" class="btn" href="#" target="_blank">👁 Preview</a>
      <a id="cancel" class="btn" href="#" style="background:#ef4444;color:#fff;display:none">✕ Cancel</a>
    </div>
    <iframe id="pv" style="display:none"></iframe>
    <h3 style="font-size:13px;color:var(--dim);margin:14px 0 6px">Live log</h3>
    <div class="log" id="log"></div>
  </div>
  {% endif %}
</div>

{% if job_id %}
<script>
const jobId = {{ job_id | tojson }};
const logEl = document.getElementById('log');
let lastLine = 0;
async function poll() {
  try {
    const r = await fetch('/api/jobs/' + jobId, {cache:'no-store'});
    if (!r.ok) return;
    const j = await r.json();
    const st = document.getElementById('jobstatus');
    const stmap = {queued:'⏳ queued',
                   running:'🔄 fixing' + (j.options && j.options.ocr ? ' (OCR ON)…' : ' (watermark removal)…'),
                   done:'✅ done', error:'❌ error', cancelled:'✕ cancelled'};
    st.textContent = (stmap[j.status] || j.status) +
      (j.size_mb ? ` — clean PDF ${j.size_mb} MB` : '');
    document.getElementById('errorbox').style.display = j.error ? 'block' : 'none';
    if (j.error) document.getElementById('errorbox').textContent = '⚠ ' + j.error;
    const act = document.getElementById('actions');
    const isDone = j.status === 'done';
    act.style.display = (j.has_output || j.has_step1 || j.status === 'running' || j.status === 'queued') ? 'flex' : 'none';
    document.getElementById('dlclean').style.display = j.has_output ? 'inline-block' : 'none';
    document.getElementById('dlstep1').style.display = j.has_step1 ? 'inline-block' : 'none';
    document.getElementById('preview').style.display = j.has_output ? 'inline-block' : 'none';
    document.getElementById('pv').style.display = j.has_output ? 'block' : 'none';
    document.getElementById('cancel').style.display =
      (j.status === 'running' || j.status === 'queued') ? 'inline-block' : 'none';
    document.getElementById('dlclean').href = '/download/' + jobId + '/clean';
    document.getElementById('dlstep1').href = '/download/' + jobId + '/step1';
    document.getElementById('preview').href = '/preview/' + jobId;
    document.getElementById('pv').src = '/preview/' + jobId + '#view=FitH';
    const log = (j.log || []).slice(lastLine);
    lastLine = (j.log || []).length;
    for (const line of log) logEl.textContent += line + '\n';
    logEl.scrollTop = logEl.scrollHeight;
    if (j.status === 'done' || j.status === 'error' || j.status === 'cancelled') return;
  } catch (e) { /* ignore transient */ }
  setTimeout(poll, 2000);
}
document.getElementById('cancel').addEventListener('click', function(ev){
  ev.preventDefault();
  fetch('/api/jobs/' + jobId + '/cancel', {method:'POST'});
});
poll();
</script>
{% endif %}
</body>
</html>
"""


if __name__ == "__main__":
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
