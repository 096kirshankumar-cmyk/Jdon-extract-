# PDF Fixer

A small Railway-hosted dashboard that cleans garbled medical MCQ PDFs
(MARROW ED8 series) before any extraction runs.

**One job, one output: a CLEAN PDF.** The dashboard fixes one thing by
default, and one thing optionally:

1. **Removes the watermark** — generic, works for **any PDF / any repeated
   watermark**, not just the itachibot series. Pages are kept exactly as
   uploaded (no re-render, no re-encode), so the output is visually identical
   and stays near the original file size. Detection covers:
   - **full-page image families** — the same watermark is often stored as
     several pixel variants (e.g. 82% + 18% of pages); all full-page variants
     whose union covers ≥90% of the file are one family and all are removed;
   - **unique-per-page raster overlays** — some tools bake the watermark
     into a NEW raster per page (different pixels each page, e.g. xrefs
     3/5/7/9/… each covering 100% of one page). When full-page images cover
     ≥98% of pages AND those pages also carry visible vector content AND the
     images are rotated or light-gray (stamp-like), every full-page image is
     removed as one watermark family;
   - **same-position images** repeated on ≥90% of pages covering ≥25% of the
     page area (diagonal grey banners);
   - **full-page Form XObjects** repeated on ≥90% of pages (vector-text /
     painted-band watermarks);
   - **inline images** (`BI … EI`) repeated across pages;
   - **watermark text** — known strings ("Sold by", "itachibot",
     "you purchased", "not for distribution", …) plus any text span repeated
     on ≥90% of pages that is rotated, large, or known: removed from the
     content streams and redacted visually. A same-**position** text family
     (same diagonal region on ≥90% of pages even when the wording differs,
     e.g. "… - page 123") is redacted at its exact bbox on every page.
   - **raster-baked watermarks** — image-only books (one full-page bitmap
     per page, no vector content) where the watermark is burned INTO the
     pixels. The app builds a per-pixel consensus mask (same normalized
     pixel is a light-gray overlay on ≥75% of sampled pages), carves out
     solid gray figures/columns and anything next to dark content, then
     fills those watermark pixels with the page's paper tone and re-encodes
     the page image as JPEG (no geometry change, no re-render).
   Legitimate content is always protected: small corner logos, one-off
   figures, page numbers and header/footer furniture stay untouched, and an
   image-only scan with **no provable watermark pattern** is refused with a
   clear message instead of being damaged.
2. **OPTIONAL — rebuilds the broken text layer with OCR**
   (`ocrmypdf --force-ocr`, English + Hindi). **Off by default**: OCR
   re-renders every page at high DPI, so the file is typically many times
   larger than the upload, and on low-DPI scans (like 55 DPI iLovePDF
   outputs) the recognized words are often wrong. Only tick "Rebuild text
   layer with OCR" if you truly need a searchable layer and accept the size
   and accuracy trade-off.

There is **no JSON/QBank/Gemini pipeline** in this app. `fix_pdf.py` does all
the work; `app.py` is only the upload → progress → download UI.

## Run

```bash
apt-get update && apt-get install -y \
  poppler-utils tesseract-ocr tesseract-ocr-hin ghostscript unpaper

pip install -r requirements.txt
gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 0 app:app
```

or just build the Dockerfile (apt + pip are baked in).

## Dashboard

- Upload a PDF (max 200 MB)
- Watermark removal runs automatically. **OCR is a checkbox, unchecked by
  default** — leave it off to get an upload-like file. If you tick it, you
  can also pick OCR language, parallel jobs, output type (PDF/A or PDF),
  deskew/clean
- Watch the live log; when done, **Download CLEAN PDF** (or preview it,
  or grab the `step1_no_watermark.pdf` intermediate)
- Cancel a running job with one click

Each job is its own `fix_pdf.py` subprocess, so OCR hangs and crashes never
take the web worker down. Job output is stored in `PDF_FIX_JOBS_DIR`
(default `/data/pdf_fix_jobs` on the Railway volume).

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | listen port (set by Railway) |
| `PDF_FIX_JOBS_DIR` | `/data/pdf_fix_jobs` | job workspace root |
| `PDF_FIX_MAX_CONCURRENT` | `1` | max parallel OCR jobs (keep 1 on small plans) |
| `PDF_FIX_MAX_JOB_KEEP` | `20` | job folders kept on disk |
| `PDF_FIX_MAX_UPLOAD_MB` | `200` | upload size cap |
| `PDF_FIX_OCR_JOBS` | `1` | default tesseract workers per job (clamped to CPU count) |
| `PDF_FIX_OUTPUT_TYPE` | `pdfa` | default output type (`pdfa` or `pdf`) |

> **Memory (OCR only):** OCR is RAM-hungry. On 512 MB Railway plans keep
> `PDF_FIX_MAX_CONCURRENT=1`, `PDF_FIX_OCR_JOBS=1`, leave "Clean" unchecked
> and use `PDF_FIX_OUTPUT_TYPE=pdf` if you see `exit code -9` (the kernel's
> out-of-memory kill). The app also sets `OMP_THREAD_LIMIT=1` so every
> tesseract process uses one thread. The default no-OCR path uses PyMuPDF
> only and is light on memory.

## CLI (outside the dashboard)

```bash
python fix_pdf.py /data/input_pdfs/OPH.pdf            # watermark removal only
python fix_pdf.py OPH.pdf --ocr                       # add OCR text layer
python fix_pdf.py OPH.pdf --ocr --language eng --jobs 4
```

## Verification

After each upload the dashboard's log shows the step-3 summary:
pages, OCR engine/language, file size before/after, sample-page quality
(1/50/100/200/300), watermark-string count (must be 0) and a PASS/FAIL verdict.
