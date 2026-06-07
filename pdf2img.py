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
import re
import hashlib
import subprocess
import requests
from urllib.parse import urlparse, urljoin, quote, parse_qs, urlencode, urlunparse
from flask import Flask, request, send_file, abort

# BeautifulSoup + lxml — used by the /page endpoint to fetch, filter and
# simplify arbitrary web pages down to the tiny tag subset the ESP32's
# on-device HTML parser renders, so the device "just renders what's given".
from bs4 import BeautifulSoup, Comment

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
            # Respect the SVG's OWN size (width/height, or the viewBox).
            # CairoSVG honours the intrinsic size when no output_width is
            # given — a viewBox="0 0 100 100" favicon renders ~100px, a
            # viewBox="0 0 140 22" wordmark renders 140x22, etc. We used to
            # FORCE output_width=SVG_RENDER_WIDTH (1200) unconditionally,
            # which blew tiny site icons up into ~half-megabyte 1200x1200
            # PNGs — slow to fetch and they often failed to load on the
            # device. Only fall back to an explicit width for SVGs with no
            # usable size at all (would otherwise render to a couple of px).
            png_bytes = cairosvg.svg2png(bytestring=raw)
            try:
                if max(Image.open(io.BytesIO(png_bytes)).size) < 16:
                    png_bytes = cairosvg.svg2png(
                        bytestring=raw, output_width=SVG_RENDER_WIDTH)
            except Exception:
                png_bytes = cairosvg.svg2png(
                    bytestring=raw, output_width=SVG_RENDER_WIDTH)
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


# ============================================================================
#  /page — fetch + filter + simplify a web page for the ESP32 browser
# ----------------------------------------------------------------------------
#  GET /page?url=<URL-encoded page URL>
#    -> 200 text/html   (simplified HTML the ESP32's parser renders directly)
#  The ESP32 routes every non-PDF/non-image page through here, so all the
#  heavy lifting (fetch, mature-content filter, strip scripts/styles/ads,
#  reduce to a tiny tag subset, rewrite images through /img2png) happens on
#  the server. See the ESP32-side PAGE_PROXY_URL in pdfviewer.h.
# ============================================================================

PAGE_UA        = 'Mozilla/5.0 (ALPHA-page/1.0)'
# Output cap, kept under the ESP32's 64 KB body cap with headroom for the
# device's own parse overhead.
PAGE_MAX_BYTES = int(os.environ.get('PAGE_MAX_BYTES', str(56 * 1024)))

# --- mature-content filter: domain blocklist + SafeSearch -------------------
# Curated exact-domain blocklist (matched on the host or any parent domain).
BLOCK_HOSTS = {
    'pornhub.com', 'xvideos.com', 'xnxx.com', 'redtube.com', 'youporn.com',
    'xhamster.com', 'spankbang.com', 'youjizz.com', 'tube8.com', 'onlyfans.com',
    'chaturbate.com', 'livejasmin.com', 'brazzers.com', 'nhentai.net',
    'rule34.xxx', 'e-hentai.org', 'hanime.tv', 'fapello.com',
}
# Substring heuristic on the hostname for the long tail of unlisted sites.
BLOCK_SUBSTR = re.compile(
    r'(porn|xxx|xvideo|xnxx|xhamster|redtube|youporn|hentai|nsfw|escort|'
    r'camgirl|sexcam|fuck|milf|brazzers|onlyfans|rule34)', re.I)

# Force SafeSearch on the major engines (host-substring -> (param, value)).
SEARCH_SAFE = {
    'google.':     ('safe', 'active'),
    'bing.':       ('adlt', 'strict'),
    'duckduckgo.': ('kp',   '1'),       # 1 = strict
    'yandex.':     ('family', 'yes'),
}


def is_blocked_host(host: str) -> bool:
    h = host.lower().split(':')[0]
    if h.startswith('www.'):
        h = h[4:]
    for b in BLOCK_HOSTS:
        if h == b or h.endswith('.' + b):
            return True
    return bool(BLOCK_SUBSTR.search(h))


def enforce_safesearch(url: str) -> str:
    """Rewrite search-engine URLs to force SafeSearch on."""
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        for key, (param, val) in SEARCH_SAFE.items():
            if key in host:
                q = {k: v[-1] for k, v in parse_qs(p.query).items()}
                q[param] = val
                return urlunparse(p._replace(query=urlencode(q)))
    except Exception:
        pass
    return url


# --- HTML simplification ----------------------------------------------------
# Whole subtrees to delete (tag + everything inside).
_DROP_TREES = [
    'script', 'style', 'noscript', 'head', 'svg', 'iframe', 'form', 'input',
    'button', 'select', 'textarea', 'nav', 'aside', 'footer',
    'video', 'audio', 'canvas', 'object', 'embed',
    'link', 'meta', 'base', 'template', 'dialog',
]
# NOTE: <header>, <figure>, <figcaption> are deliberately NOT dropped — an
# article's <header> can hold its <h1>, and a <figure> wraps a content <img>.
# They're unwrapped (content kept, tag dropped) by the main pass instead.
# Tags the ESP32 parser renders specially — keep these (cleaned). Anything
# else that survives is UNWRAPPED (its text kept, the tag dropped) so the
# payload stays lean and the device parser isn't fed tags it ignores.
_KEEP_TAGS = {
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'div', 'br', 'hr', 'a', 'ul',
    'ol', 'li', 'blockquote', 'img', 'span', 'b', 'strong', 'i', 'em',
    'table', 'tr', 'td', 'th', 'body', 'html',
}
# Junk markers, matched per TOKEN SEGMENT (a class/id split on space then on
# '-'/'_'), NOT as a loose substring of the whole class string. Substring
# matching was catastrophic: e.g. <html class="...language-in-header-enabled">
# matched "header" and decomposed the ENTIRE document. Segment matching only
# fires on an exact delimited segment, so feature-flag classes are safe.
_JUNK_WORDS = frozenset((
    'ad', 'ads', 'adbox', 'advert', 'advertisement', 'adslot', 'adunit',
    'sponsor', 'sponsored', 'banner', 'popup', 'modal', 'overlay', 'lightbox',
    'cookie', 'consent', 'gdpr', 'newsletter', 'subscribe', 'signup',
    'paywall', 'social', 'share', 'sharing', 'sharedaddy', 'related',
    'recommended', 'comments', 'disqus', 'sidebar', 'breadcrumb', 'promo',
    'masthead',
))
# Never decompose these — they are (or wrap) the whole document / main content.
_NEVER_KILL = frozenset(('html', 'body', 'main', 'article', '[document]'))


def _is_junk(tag):
    if getattr(tag, 'attrs', None) is None:
        return False
    if tag.name in _NEVER_KILL:
        return False
    tokens = []
    cls = tag.get('class') or []
    if isinstance(cls, str):
        cls = cls.split()
    tokens.extend(cls)
    idv = tag.get('id')
    if idv:
        tokens.append(idv)
    for tok in tokens:
        for seg in re.split(r'[-_]+', tok.lower()):
            if seg in _JUNK_WORDS:
                return True
    return False


def simplify_html(html: str, base_url: str, img_proxy: str) -> str:
    soup = BeautifulSoup(html, 'lxml')

    title = ''
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    for t in soup.find_all(_DROP_TREES):
        t.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    # Drop ad / cookie / chrome blocks by class/id segment. COLLECT first,
    # then decompose: decomposing a tag sets its descendants' `attrs` to None,
    # so iterating find_all() and decomposing in the same pass would call
    # .get() on an already-decomposed descendant (AttributeError). Gather the
    # matches while everything is attached, then decompose those still in tree.
    junk = [t for t in soup.find_all(True) if _is_junk(t)]
    for t in junk:
        if t.parent is not None:        # not already removed via an ancestor
            t.decompose()

    body = soup.body or soup

    # Clean attributes, rewrite links/images, unwrap unknown tags.
    for t in list(body.find_all(True)):
        if getattr(t, 'attrs', None) is None:   # detached by an earlier pass
            continue
        name = (t.name or '').lower()
        if name == 'a':
            href = t.get('href')
            t.attrs = {}
            if href and not href.startswith(('javascript:', '#', 'mailto:')):
                t['href'] = urljoin(base_url, href)
        elif name == 'img':
            src = t.get('src') or t.get('data-src') or t.get('data-original')
            alt = (t.get('alt') or '').strip()
            t.attrs = {}
            if src and not src.startswith('data:'):
                absu = urljoin(base_url, src)
                # Route EVERY image through /img2png so the ESP32 always gets
                # a downsized PNG (incl. WebP/SVG -> PNG).
                t['src'] = img_proxy + quote(absu, safe='')
                # Only keep a REAL caption as alt. We used to default to
                # 'img', but that printed a literal "img" placeholder next to
                # every single image on the device (the on-device parser shows
                # the alt text as the image's label). No alt -> no label; the
                # parser still reserves a box for the picture itself.
                if alt:
                    t['alt'] = alt
            else:
                t.decompose()
        elif name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            t.attrs = {k: v for k, v in t.attrs.items() if k == 'id'}
        elif name == 'span':
            keep = {}
            if t.get('id'):
                keep['id'] = t['id']
            style = t.get('style', '')
            if 'color' in style.lower():
                keep['style'] = style
            t.attrs = keep
        elif name in _KEEP_TAGS:
            t.attrs = {}
        else:
            t.unwrap()        # unknown tag -> keep its text, drop the wrapper

    inner = body.decode_contents() if hasattr(body, 'decode_contents') else str(body)
    head = ('<h1>' + title + '</h1>') if title else ''
    out = '<html><body>' + head + inner + '</body></html>'

    if len(out) > PAGE_MAX_BYTES:
        out = out[:PAGE_MAX_BYTES]
        cut = max(out.rfind('</p>'), out.rfind('</li>'), out.rfind('</div>'))
        if cut > 0:
            out = out[:cut + 6]
        out += '<hr><p>[page truncated]</p></body></html>'
    return out


def _page_notice(title: str, msg: str):
    body = '<html><body><h1>' + title + '</h1><p>' + msg + '</p></body></html>'
    return (body, 200, {'Content-Type': 'text/html; charset=utf-8'})


@app.route('/page')
def page():
    url = request.args.get('url')
    if not url:
        abort(400, 'missing ?url=')
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url

    url = enforce_safesearch(url)
    host = urlparse(url).netloc
    if is_blocked_host(host):
        return _page_notice('Blocked',
                            'This site is blocked by the mature-content filter.')

    try:
        r = requests.get(url, timeout=FETCH_TIMEOUT,
                         headers={'User-Agent': PAGE_UA,
                                  'Accept-Language': 'en-US,en;q=0.9'},
                         allow_redirects=True)
    except requests.exceptions.RequestException as e:
        return _page_notice('Could not load', str(e)[:200])

    ctype = r.headers.get('Content-Type', '').lower()
    head = r.text[:256].lstrip().lower()
    looks_html = ('html' in ctype) or head.startswith(('<!doctype', '<html'))
    if not looks_html:
        return _page_notice('Not a web page',
                            'This link is %s, not HTML.' % (ctype or 'an unknown type'))

    img_proxy = request.host_url + 'img2png?url='   # e.g. https://host/img2png?url=
    try:
        simplified = simplify_html(r.text, r.url, img_proxy)
    except Exception as e:
        return _page_notice('Could not simplify', str(e)[:200])

    return (simplified, 200, {'Content-Type': 'text/html; charset=utf-8'})


@app.route('/health')
def health():
    return ('ok\n', 200, {'Content-Type': 'text/plain'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8080')))
