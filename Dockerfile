# Dockerfile for pdf2img — Render / Fly.io / Hugging Face / any container host.
# Built image is ~150 MB (python:3.11-slim + poppler-utils + libcairo2 +
# Flask + Pillow + CairoSVG).

FROM python:3.11-slim

# System deps:
#   poppler-utils     — pdftoppm + pdfinfo for the /pdf2img endpoint
#   libcairo2         — runtime for CairoSVG (used by /img2png to render
#                       .svg files server-side, which works around the
#                       gradientTransform / clipPath limits of the ESP32's
#                       NanoSVG decoder)
# Combine apt update / install / clean in ONE layer so the image stays small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      poppler-utils \
      libcairo2 \
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
