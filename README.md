# pdf2img — PDF→PNG proxy for the ALPHA ESP32 browser

The ESP32 can't render PDFs itself (no native PDF library fits the chip).
This proxy receives a PDF URL, downloads it, renders the requested page to
PNG with `pdftoppm` (poppler-utils), and returns the PNG so the ESP32 can
display it via its existing PNG decoder. Server-side cache so the same
PDF isn't re-rendered on every request.

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
sudo apt install poppler-utils python3-flask python3-requests   # Debian / Pi OS
python3 pdf2img.py
# Listening on 0.0.0.0:8080
```

Then in `browser.cpp` set
`#define PDF_PROXY_URL "http://192.168.1.50:8080/pdf2img?url="`
(replace with your box's LAN IP).

Test it:

```bash
curl -o page1.png "http://localhost:8080/pdf2img?url=https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf&page=1"
file page1.png   # → PNG image data
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
- `PORT` — TCP port. Default 8080.

## Endpoints

- `GET /pdf2img?url=<URL>&page=N` — main endpoint. Returns image/png plus
  `X-Pdf-Pages: M` header with the total page count.
- `GET /health` — for the tunnel / load balancer to ping. Returns 200 ok.

## ESP32-side cache

The ESP32 caches downloaded PNGs to `/sdcard/pdfcache/<urlhash>_p<N>.png`.
After the first load, page navigation between cached pages is instant — no
proxy round trip. Server-side cache and ESP32-side cache are independent.
