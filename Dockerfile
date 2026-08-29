FROM python:3.11-slim

# poppler-utils gives us pdftoppm, pdfimages, pdftotext
# tzdata: today_stamp() stamps the quota day in US/Pacific to match Google's
# RPD reset. Without the tz database zoneinfo raises and we fall back to a
# fixed UTC-8 offset (safe, but an hour off during US DST).
# tesseract-ocr-hin: Hindi OCR pack for fix_pdf.py (--language eng+hin)
# ghostscript: PDF/A conversion + ocrmypdf postprocessing
# unpaper: optional, enables ocrmypdf --clean (scan artifact cleanup)
RUN apt-get update && apt-get install -y \
        poppler-utils tesseract-ocr tesseract-ocr-hin ghostscript unpaper tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# PDF Fixer only -- no QBank/JSON pipeline code.
COPY app.py fix_pdf.py ./
ENV PYTHONUNBUFFERED=1
# Job directories (uploaded PDF + clean output) live on the Railway Volume.
ENV PDF_FIX_JOBS_DIR=/data/pdf_fix_jobs
# Max OCR processes running at once (each job = one fix_pdf.py subprocess).
ENV PDF_FIX_MAX_CONCURRENT=2
# Keep this many finished job folders on disk.
ENV PDF_FIX_MAX_JOB_KEEP=20

EXPOSE 8080
# Gunicorn avoids Flask's development-server warning and is safe for Railway.
# One worker is intentional: the dashboard keeps in-memory run state and must
# not allow separate workers to start concurrent writes to the same volume.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 0 app:app"]
