FROM python:3.11-slim

# poppler-utils gives us pdftoppm, pdfimages, pdftotext
# tzdata: today_stamp() stamps the quota day in US/Pacific to match Google's
# RPD reset. Without the tz database zoneinfo raises and we fall back to a
# fixed UTC-8 offset (safe, but an hour off during US DST).
RUN apt-get update && apt-get install -y poppler-utils tesseract-ocr tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY qbank_pipeline.py qbank_validator.py fix_output.py app.py split_outputs.py master_review_export.py gemini_keys.py review_digest.py review_queue.py flag_verifier.py boundary_phased.py FORMAT.md ./
# PDFs get uploaded via the dashboard now, so no need to bake them into the image.
# (If you'd rather pre-load them at build time, uncomment the next line and
#  add a pdfs/ folder next to this Dockerfile.)
# COPY pdfs/ ./pdfs/

# V2: SEPARATE output root on the Railway Volume -- v1 data (/data/qbank_output)
# stays untouched while v2 is being proven.
ENV OUTPUT_DIR=/data/qbank_output_v2
# Flush every print() immediately, otherwise Docker block-buffers stdout and
# pipeline progress never shows up in Railway's Deploy Logs in real time.
ENV PYTHONUNBUFFERED=1

# Container clock stays UTC; the quota day is computed in US/Pacific by
# today_stamp(). Override only if Google moves the reset boundary.
ENV QUOTA_RESET_TZ=America/Los_Angeles

EXPOSE 8080
# Gunicorn avoids Flask's development-server warning and is safe for Railway.
# One worker is intentional: the dashboard keeps in-memory run state and must
# not allow separate workers to start concurrent writes to the same volume.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 0 app:app"]
