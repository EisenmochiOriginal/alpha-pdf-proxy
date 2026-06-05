#!/usr/bin/env python3
# pdf2img.py — multi-page PDF→PNG proxy for the ALPHA ESP32 browser.
#
# What it does:
#   GET /pdf2img?url=<URL-encoded PDF URL>&page=N
#     -> 200 image/png         (the rendered page, header X-Pdf-Pages: M)
#     -> 404                   (PDF doesn't have a page N)
#     -> 415                   (URL didn't return a PDF)
#     -> 502                   (fetch from origin failed)
#
# Server-side cache lives in CACHE_DIR. Each PDF is downloaded ONCE
# (keyed by SHA1 of the URL), each requested page is rendered ONCE.
#
# Setup (Debian / Ubuntu / Raspberry Pi OS / WSL):
#   sudo apt install poppler-utils python3-flask python3-requests
#   python3 pdf2img.py
#
# Setup (macOS via Homebrew):
#   brew install poppler
#   pip3 install flask requests
#   python3 pdf2img.py
#
# Expose to the internet (free, no port forwarding):
#   See pdf2img-README.md for the Cloudflare Tunnel walkthrough.

import os
import hashlib
import subprocess
import requests
from flask import Flask, request, send_file, abort

app = Flask(__name__)

# Where rendered pages get cached on the server. Use a tmpfs (/tmp) if
# you don't want them to survive reboots; use a real path on disk to
# avoid re-downloading + re-rendering after every server restart.
CACHE_DIR = os.environ.get('PDF2IMG_CACHE', '/tmp/pdf2img-cache')
os.makedirs(CACHE_DIR, exist_ok=True)

# Maximum page index we'll honour. Stops a malicious request from
# pre-rendering 50 K pages.
MAX_PAGE = 200

# Render DPI. 100 -> ~825×1075 px for a US letter / A4 page. The ESP32
# screen is 320×480, so 100 DPI gives ~2× the screen resolution for
# crispness when LovyanGFX's drawPng downscales. Higher DPI = bigger
# PNG = longer ESP32 download. 100 is a good default.
RENDER_DPI = int(os.environ.get('PDF2IMG_DPI', '100'))

# Origin-fetch timeout (seconds).
FETCH_TIMEOUT = 30


def cache_key(url: str) -> str:
    return hashlib.sha1(url.encode('utf-8')).hexdigest()


def get_page_count(pdf_path: str) -> int:
    try:
        out = subprocess.check_output(['pdfinfo', pdf_path], timeout=10).decode('utf-8', 'replace')
        for line in out.splitlines():
            if line.startswith('Pages:'):
                return int(line.split(':', 1)[1].strip())
    except Exception:
        pass
    return 1


@app.route('/pdf2img')
def pdf2img():
    pdf_url = request.args.get('url')
    if not pdf_url:
        abort(400, 'missing ?url=')
    try:
        page = int(request.args.get('page', '1'))
    except ValueError:
        abort(400, '?page= must be an integer')
    if page < 1 or page > MAX_PAGE:
        abort(400, f'?page= must be in 1..{MAX_PAGE}')

    key      = cache_key(pdf_url)
    pdf_path = os.path.join(CACHE_DIR, key + '.pdf')
    meta_path = os.path.join(CACHE_DIR, key + '.pages')
    png_path = os.path.join(CACHE_DIR, f'{key}_p{page}.png')

    # Step 1: download the PDF if we haven't seen it before.
    if not os.path.exists(pdf_path):
        try:
            r = requests.get(
                pdf_url, timeout=FETCH_TIMEOUT,
                headers={'User-Agent': 'Mozilla/5.0 (ALPHA-pdf2img/1.0)'},
                allow_redirects=True,
            )
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            abort(502, f'origin fetch failed: {e}')
        if not r.content.startswith(b'%PDF-'):
            abort(415, 'origin did not return a PDF (no %PDF- magic)')
        with open(pdf_path, 'wb') as f:
            f.write(r.content)

    # Step 2: get page count (cached after first lookup).
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            total_pages = int(f.read().strip() or '1')
    else:
        total_pages = get_page_count(pdf_path)
        with open(meta_path, 'w') as f:
            f.write(str(total_pages))

    if page > total_pages:
        abort(404, f'pdf has {total_pages} pages; you asked for {page}')

    # Step 3: render the requested page if not yet rendered.
    if not os.path.exists(png_path):
        # pdftoppm with -singlefile -f N -l N writes <base>.png
        out_base = png_path[:-len('.png')]
        try:
            subprocess.run(
                [
                    'pdftoppm', '-png', '-singlefile',
                    '-r', str(RENDER_DPI),
                    '-f', str(page), '-l', str(page),
                    pdf_path, out_base,
                ],
                check=True, capture_output=True, timeout=30,
            )
        except subprocess.CalledProcessError as e:
            abort(500, f'pdftoppm failed: {e.stderr.decode("utf-8", "replace")[:200]}')
        except subprocess.TimeoutExpired:
            abort(500, 'pdftoppm timed out')
        if not os.path.exists(png_path):
            abort(500, 'pdftoppm exited 0 but PNG is missing')

    resp = send_file(png_path, mimetype='image/png')
    resp.headers['X-Pdf-Pages'] = str(total_pages)
    # Useful for ESP32-side debugging.
    resp.headers['X-Pdf-Page']  = str(page)
    return resp


@app.route('/health')
def health():
    return ('ok\n', 200, {'Content-Type': 'text/plain'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8080')))
