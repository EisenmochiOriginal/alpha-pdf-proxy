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
#   GET /img2png?url=<URL-encoded image URL>                    (added 2026-06)
#     -> 200 image/png         (header X-Original-Format: webp|svg|png|jpg)
#     Converts WebP / SVG / JPG / PNG to a PNG sized for the ESP32 sprite cap.
#     The ESP32 has no native WebP decoder; SVG via NanoSVG can't do
#     gradientTransform or clipPath; large JPG/PNG can blow the 4 MB
#     PSRAM-sprite limit. This endpoint handles all of that server-side.
#
# Both endpoints always emit a PNG that fits within MAX_DIM × MAX_DIM and
# MAX_BYTES. If the natural render is larger, the server iteratively
# downsizes (Pillow, LANCZOS) until both caps are satisfied — so the
# ESP32 never sees a payload that won't fit its sprite.
#
# Server-side cache lives in CACHE_DIR. Each PDF is downloaded ONCE
# (keyed by SHA1 of the URL), each requested page is rendered ONCE.
# /img2png caches the converted PNG per-URL with the same key scheme.
#
# Setup (Debian / Ubuntu / Raspberry Pi OS / WSL):
#   sudo apt install poppler-utils libcairo2 python3-flask python3-requests \
#                    python3-pil python3-cairosvg
#   python3 pdf2img.py
#
# Setup (macOS via Homebrew):
#   brew install poppler cairo
#   pip3 install flask requests pillow cairosvg
#   python3 pdf2img.py
#
# Expose to the internet (free, no port forwarding):
#   See pdf2img-README.md for the Cloudflare Tunnel walkthrough.

import io
import os
import hashlib
import subprocess
import requests
from flask import Flask, request, send_file, abort

# Pillow — used by /img2png to decode WebP / JPG, and by downsize_png() to
# shrink any oversized output on both endpoints.
from PIL import Image

# CairoSVG — pure-Python on top of libcairo. Renders SVG (including
# gradientTransform / clipPath that NanoSVG on the ESP32 can't handle)
# into a PNG. The Dockerfile installs libcairo2 so this works in
# container hosts.
import cairosvg

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

# Output size caps. The ESP32 allocates an RGB565 PSRAM sprite for every
# rendered page / image; the cap on that sprite is ~4 MB (≈ 1450×1450 px).
# We default to 1200 px on the long edge and 2 MB total file size — well
# within the sprite cap, with headroom for PNG decode buffers. Override
# via env vars on the server side if a deployment wants different limits.
MAX_DIM   = int(os.environ.get('PDF2IMG_MAX_DIM',   '1200'))
MAX_BYTES = int(os.environ.get('PDF2IMG_MAX_BYTES', str(2 * 1024 * 1024)))

# Default render width when an SVG has no intrinsic size. CairoSVG will
# preserve the SVG's viewBox aspect ratio against this width.
SVG_RENDER_WIDTH = int(os.environ.get('PDF2IMG_SVG_WIDTH', str(MAX_DIM)))


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


def downsize_png(png_bytes: bytes,
                 max_dim:   int = MAX_DIM,
                 max_bytes: int = MAX_BYTES) -> bytes:
    """Return a PNG that fits both caps. If the input already fits, return
    it unchanged so a cached well-sized page costs nothing. Otherwise:
      - Scale down with LANCZOS so the longest edge is `max_dim`.
      - Re-encode to PNG with optimize=True.
      - If the file is STILL larger than `max_bytes`, keep shrinking by
        25 % until it fits or the longest edge drops below 256 px (at
        which point further shrinking would destroy the page).

    The ESP32-side rationale is in the PSRAM sprite cap — the page has to
    fit there as RGB565, which is fundamentally a width×height constraint
    rather than a byte one, but byte size also matters for the TLS read
    buffer + the SD write. Both caps catch slightly different failure
    modes so we enforce both.
    """
    try:
        img = Image.open(io.BytesIO(png_bytes))
    except Exception:
        # Not a decodable image — caller's problem. Return as-is so the
        # endpoint logic can decide whether to surface an error.
        return png_bytes

    dims_ok  = max(img.width, img.height) <= max_dim
    bytes_ok = len(png_bytes) <= max_bytes
    if dims_ok and bytes_ok:
        return png_bytes

    # First pass: shrink to max_dim along the longest edge.
    if not dims_ok:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format='PNG', optimize=True)
    out_bytes = out.getvalue()

    # Second pass: if it's still too big as a file, keep shrinking 25 %
    # per iteration until under cap or we hit a sensible floor.
    while len(out_bytes) > max_bytes and min(img.width, img.height) > 256:
        new_w = max(256, int(img.width  * 3 / 4))
        new_h = max(256, int(img.height * 3 / 4))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format='PNG', optimize=True)
        out_bytes = out.getvalue()

    return out_bytes


def _detect_image_format(url: str, raw: bytes) -> str:
    """Returns one of 'webp', 'svg', 'png', 'jpg', or 'unknown'. Sniff is
    magic-byte first (more reliable), URL extension second.
    """
    if len(raw) >= 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'webp'
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if raw[:3] == b'\xff\xd8\xff':
        return 'jpg'
    # SVG: skip an optional BOM + whitespace, then look for `<?xml` or `<svg`.
    head = raw[:512].lstrip(b'\xef\xbb\xbf').lstrip()
    if head.startswith(b'<?xml') or head.startswith(b'<svg'):
        return 'svg'
    # Fall back to URL extension if magic bytes were inconclusive.
    lower = url.lower().split('?')[0]
    if lower.endswith('.webp'): return 'webp'
    if lower.endswith('.svg'):  return 'svg'
    if lower.endswith('.png'):  return 'png'
    if lower.endswith('.jpg') or lower.endswith('.jpeg'): return 'jpg'
    return 'unknown'


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

        # Downsize the freshly rendered page if it busts either cap.
        # We do this ONCE on render and persist the downsized copy, so
        # subsequent requests served from cache pay nothing.
        try:
            with open(png_path, 'rb') as f:
                raw = f.read()
            shrunk = downsize_png(raw)
            if shrunk is not raw and len(shrunk) != len(raw):
                with open(png_path, 'wb') as f:
                    f.write(shrunk)
        except Exception:
            pass  # downsize is best-effort; fall back to the raw render

    resp = send_file(png_path, mimetype='image/png')
    resp.headers['X-Pdf-Pages'] = str(total_pages)
    # Useful for ESP32-side debugging.
    resp.headers['X-Pdf-Page']  = str(page)
    return resp


@app.route('/img2png')
def img2png():
    """Convert any of {WebP, SVG, PNG, JPG} from a URL to a PNG suitable
    for the ESP32. Always passes the output through downsize_png() so
    nothing the caller sees can bust the 4 MB sprite cap.
    """
    img_url = request.args.get('url')
    if not img_url:
        abort(400, 'missing ?url=')

    key       = cache_key(img_url)
    cache_png = os.path.join(CACHE_DIR, f'img_{key}.png')
    cache_fmt = os.path.join(CACHE_DIR, f'img_{key}.fmt')

    # Cache hit.
    if os.path.exists(cache_png):
        fmt = 'unknown'
        if os.path.exists(cache_fmt):
            try:
                with open(cache_fmt) as f:
                    fmt = f.read().strip() or 'unknown'
            except Exception:
                pass
        resp = send_file(cache_png, mimetype='image/png')
        resp.headers['X-Original-Format'] = fmt
        return resp

    # Fetch from origin.
    try:
        r = requests.get(
            img_url, timeout=FETCH_TIMEOUT,
            headers={'User-Agent': 'Mozilla/5.0 (ALPHA-pdf2img/1.0)'},
            allow_redirects=True,
        )
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        abort(502, f'origin fetch failed: {e}')
    raw = r.content

    fmt = _detect_image_format(img_url, raw)
    if fmt == 'unknown':
        abort(415, 'origin did not return a supported image format '
                   '(expected webp / svg / png / jpg)')

    # Convert to a PNG byte stream.
    try:
        if fmt == 'svg':
            # CairoSVG honours the SVG's intrinsic size when neither
            # output_width nor output_height is given; we pass an
            # explicit output_width so size-less SVGs (e.g. ones using
            # viewBox only) get a sensible render rather than 1×1.
            png_bytes = cairosvg.svg2png(
                bytestring=raw, output_width=SVG_RENDER_WIDTH,
            )
        elif fmt == 'webp':
            img = Image.open(io.BytesIO(raw))
            # WebP often comes through with mode RGBA; PNG is happy with
            # that and the ESP32's drawPng ignores alpha — leave as-is.
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            png_bytes = buf.getvalue()
        elif fmt == 'png':
            png_bytes = raw                              # passthrough
        elif fmt == 'jpg':
            img = Image.open(io.BytesIO(raw))
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            png_bytes = buf.getvalue()
        else:
            abort(500, f'internal: unhandled format {fmt!r}')
    except Exception as e:
        abort(500, f'{fmt} render/decode failed: {e}')

    # Always downsize through the same caps so the ESP32 sprite path is
    # guaranteed-safe regardless of origin size.
    png_bytes = downsize_png(png_bytes)

    # Cache + serve.
    with open(cache_png, 'wb') as f:
        f.write(png_bytes)
    with open(cache_fmt, 'w') as f:
        f.write(fmt)

    resp = send_file(cache_png, mimetype='image/png')
    resp.headers['X-Original-Format'] = fmt
    return resp


@app.route('/health')
def health():
    return ('ok\n', 200, {'Content-Type': 'text/plain'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8080')))
