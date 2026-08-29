# PDF Fixer

A small Railway-hosted dashboard that cleans garbled medical MCQ PDFs
(MARROW ED8 series) before any extraction runs.

**One job, one output: a CLEAN PDF.** The dashboard fixes two things:

1. **Removes the full-page "Sold by @itachibot" watermark** (image + text).
2. **Rebuilds the broken text layer with OCR** (`ocrmypdf --force-ocr`,
   English + Hindi) so the PDF is readable and searchable again.

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
- Pick OCR language, parallel jobs, output type (PDF/A or PDF), deskew/clean
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
| `PDF_FIX_MAX_CONCURRENT` | `2` | max parallel OCR jobs |
| `PDF_FIX_MAX_JOB_KEEP` | `20` | job folders kept on disk |
| `PDF_FIX_MAX_UPLOAD_MB` | `200` | upload size cap |

## CLI (outside the dashboard)

```bash
python fix_pdf.py /data/input_pdfs/OPH.pdf            # -> OPH_CLEAN.pdf
python fix_pdf.py OPH.pdf --language eng --jobs 4
python fix_pdf.py OPH.pdf --skip-ocr                  # watermark removal only
```

## Verification

After each upload the dashboard's log shows the step-3 summary:
pages, OCR engine/language, file size before/after, sample-page quality
(1/50/100/200/300), watermark-string count (must be 0) and a PASS/FAIL verdict.
