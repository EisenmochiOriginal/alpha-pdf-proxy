# pdf2img — PDF / WebP / SVG → PNG proxy for the ALPHA ESP32 browser

The ESP32 can't render PDFs itself (no native PDF library fits the chip), it
has no native WebP decoder, and its NanoSVG decoder can't handle
`gradientTransform` or `clipPath`. This proxy takes a URL pointing at any
of those formats, converts it server-side, and returns a PNG the ESP32 can
display via its existing decoder.

- **`/pdf2img`** — downloads a PDF, renders page N via `pdftoppm` (poppler-utils).
  Returns PNG + `X-Pdf-Pages: M`.
- **`/img2png`** — downloads a WebP / SVG / JPG / PNG and converts it to a PNG.
  Pillow handles WebP / JPG decode; CairoSVG renders SVG via libcairo2.
  Returns PNG + `X-Original-Format: <fmt>`.

Both endpoints **downsize the output** if it would bust the ESP32's 4 MB
PSRAM sprite cap: anything larger than 1200 px on the long edge or 2 MB
total is iteratively scaled (LANCZOS) until it fits. The shrunk version is
what gets cached, so subsequent requests for the same URL are instant.

Server-side cache: each PDF and each converted image is downloaded ONCE
(keyed by SHA1 of the URL); each rendered page / converted PNG is generated
ONCE.

## Where do I run this?

| You want…                                         | Use…                            | Setup time | Cost |
|---------------------------------------------------|----------------------------------|------------|------|
| Zero CLI, click-to-deploy, OK with cold starts    | **Render.com** — see [DEPLOY-render.md](DEPLOY-render.md) | ~10 min   | Free forever |
| Always-on, no cold starts, willing to do CLI work | **Oracle Cloud Always Free** — see [DEPLOY-oracle.md](DEPLOY-oracle.md) | ~1 hr     | Free forever |
| Already own a Raspberry Pi / home server          | Run locally + **Cloudflare Tunnel** (below) | ~30 min   | Free, your hardware |
| Just testing on the same LAN                      | Local Python (below)            | ~5 min    | Free, only works at home |

If you've never done this before, do **Render.com**. It's the simplest by
a wide margin.

## Quick start (local LAN, for testing)

On any Linux box, macOS, WSL, or Raspberry Pi:

```bash
# Debian / Ubuntu / Raspberry Pi OS:
sudo apt install poppler-utils libcairo2 python3-flask python3-requests \
                 python3-pil python3-cairosvg
python3 pdf2img.py
# Listening on 0.0.0.0:8080
```

Then in `browser.cpp` set
`#define PDF_PROXY_URL "http://192.168.1.50:8080/pdf2img?url="`
(replace with your box's LAN IP).

Test it:

```bash
# PDF endpoint
curl -o page1.png "http://localhost:8080/pdf2img?url=https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf&page=1"
file page1.png   # → PNG image data

# WebP endpoint
curl -o pic.png "http://localhost:8080/img2png?url=https://www.gstatic.com/webp/gallery/1.webp"
file pic.png     # → PNG image data
curl -sI "http://localhost:8080/img2png?url=https://www.gstatic.com/webp/gallery/1.webp" | grep X-Original
# → X-Original-Format: webp

# SVG endpoint
curl -o tux.png "http://localhost:8080/img2png?url=https://upload.wikimedia.org/wikipedia/commons/3/35/Tux.svg"
file tux.png     # → PNG image data
```

## Exposing it to the internet — Cloudflare Tunnel (free, no port forwarding)

This is the easiest way to make the proxy reachable from any WiFi the ESP32
joins, without opening ports on your router.

1. Sign up at https://dash.cloudflare.com — free.
2. Add a domain (any domain you own, or use a free one from
   https://www.duckdns.org).
3. Install cloudflared:
   ```bash
   curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o cloudflared.deb
   sudo dpkg -i cloudflared.deb
   ```
4. `cloudflared tunnel login` — opens a browser, pick your zone.
5. `cloudflared tunnel create alpha-pdf` — gives you a tunnel ID.
6. Create `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: alpha-pdf
   credentials-file: /home/USER/.cloudflared/<TUNNEL_ID>.json
   ingress:
     - hostname: pdf.yourdomain.com
       service: http://localhost:8080
     - service: http_status:404
   ```
7. `cloudflared tunnel route dns alpha-pdf pdf.yourdomain.com`
8. `cloudflared tunnel run alpha-pdf`

Then in `browser.cpp`:
`#define PDF_PROXY_URL "https://pdf.yourdomain.com/pdf2img?url="`

The ESP32 hits Cloudflare's edge over HTTPS; Cloudflare forwards through the
tunnel to your home box. No router config, no static IP needed.

Make it a service so it survives reboots:
```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

## Alternatives

- **VPS** (DigitalOcean / Hetzner / etc., ~$5/mo): just run `pdf2img.py`
  behind nginx with Let's Encrypt. More control, costs money.
- **ngrok** (free tier): `ngrok http 8080`. Quick, but the URL changes on
  every restart unless you pay.
- **Tailscale** (free): the ESP32 can't be a Tailscale node, so this only
  helps if you're also reverse-proxying through a node that exposes a
  public endpoint. Skip unless you already have it set up.

## Config knobs

Environment variables read by `pdf2img.py`:

- `PDF2IMG_CACHE` — server-side cache dir. Default: `/tmp/pdf2img-cache`.
  Set to something persistent (e.g. `/var/cache/pdf2img`) if you don't
  want the cache wiped on reboot.
- `PDF2IMG_DPI` — render DPI. Default 100. Higher = sharper PNG = bigger
  download. The ESP32 screen is 320×480 so anything past ~150 is wasted.
- `PDF2IMG_MAX_DIM` — long-edge cap (px) for ALL output PNGs (PDF + img).
  Default 1200. Bumping past ~1400 risks busting the ESP32's 4 MB PSRAM
  sprite cap.
- `PDF2IMG_MAX_BYTES` — file-size cap for output PNGs. Default 2 MB.
  Iterative LANCZOS shrink until both dim and bytes caps fit.
- `PDF2IMG_SVG_WIDTH` — render width passed to CairoSVG for size-less
  SVGs (those using viewBox only with no intrinsic width). Defaults to
  `PDF2IMG_MAX_DIM`.
- `PORT` — TCP port. Default 8080.

## Endpoints

- `GET /pdf2img?url=<URL>&page=N` — PDF page → PNG. Returns image/png plus
  `X-Pdf-Pages: M` header with the total page count.
- `GET /img2png?url=<URL>` — WebP / SVG / JPG / PNG → PNG. Returns image/png
  plus `X-Original-Format: <fmt>` so the ESP32 can log what got converted.
- `GET /page?url=<URL>` — fetch a web page, filter mature content (domain
  blocklist + forced SafeSearch on Google/Bing/DDG/Yandex), and **simplify**
  the HTML down to the tiny tag subset the ESP32's on-device parser renders
  (headings / p / a / lists / img / blockquote / colour spans). Strips
  scripts, styles, forms, nav/aside/footer, ads and cookie bars; resolves
  relative URLs; rewrites every `<img src>` through `/img2png` so the device
  gets a downsized PNG. Returns `text/html` capped to ~56 KB. The ESP32
  routes all non-PDF/non-image page loads through this so it "just renders
  what's given" (see `PAGE_PROXY_URL` in `pdfviewer.h`).
- `GET /health` — for the tunnel / load balancer to ping. Returns 200 ok.

## ESP32-side cache

The ESP32 caches downloaded PNGs to `/sdcard/pdfcache/<urlhash>_p<N>.png`.
After the first load, page navigation between cached pages is instant — no
proxy round trip. Server-side cache and ESP32-side cache are independent.
