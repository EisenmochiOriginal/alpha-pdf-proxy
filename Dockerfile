# Dockerfile for pdf2img — Render / Fly.io / Hugging Face / any container host.
# Built image is ~120 MB (python:3.11-slim + poppler-utils + Flask).

FROM python:3.11-slim

# poppler-utils gives us pdftoppm + pdfinfo, the only system deps we need.
# Combine apt update / install / clean in ONE layer so the image stays small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps — pinned conservatively so a future package break doesn't
# silently take the proxy down.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY pdf2img.py .

# Render / Fly / HF Spaces all pass the listen port via $PORT. 8080 is the
# local-dev default if not set.
ENV PORT=8080

# Gunicorn for production (the Flask dev server is single-threaded and
# blocks on long PDF renders). 2 workers, 120 s request timeout because
# big PDFs can take a while to render the first page.
CMD gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 120 pdf2img:app
