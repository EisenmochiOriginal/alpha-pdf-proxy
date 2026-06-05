# Deploy pdf2img to Render.com — zero CLI, free forever

Render's free tier gives you a public HTTPS URL, auto-deploy from GitHub,
and 750 hours/month. The service sleeps after 15 min of inactivity and
the first request after sleep takes ~30-60 seconds to wake the container
back up. For occasional PDF viewing on your ESP32 that's fine.

**You'll end up with a URL like `https://alpha-pdf.onrender.com/pdf2img?url=...`
— paste it into `pdfviewer.h`'s `PDF_PROXY_URL` and you're done.**

---

## Step 1 — Push the `pdf2img/` folder to GitHub

If you've never used GitHub:

1. Create a free account at https://github.com.
2. Click **New repository** (the `+` icon top-right).
3. Name it `alpha-pdf-proxy`. Make it **public** (private also works on
   Render's free tier, but public is one less thing to configure).
4. **Don't** initialise with a README — we'll push our own files.
5. GitHub shows you a quick-start. The relevant command set is:

   ```bash
   cd "C:\Users\Iosua\OneDrive - kag-westerburg.de\Documents\Arduino\ModularPHONE\ALPHA\pdf2img"
   git init
   git add Dockerfile requirements.txt pdf2img.py README.md DEPLOY-render.md
   git commit -m "initial pdf2img proxy"
   git branch -M main
   git remote add origin https://github.com/<your-username>/alpha-pdf-proxy.git
   git push -u origin main
   ```

   If `git` isn't installed: download from https://git-scm.com/download/win
   and re-open the terminal. Default settings are fine.

   First push will ask you to log in to GitHub — paste a Personal Access
   Token instead of your password (GitHub no longer accepts passwords for
   CLI auth). Create one at
   https://github.com/settings/tokens → **Generate new token (classic)** →
   tick the `repo` scope → copy the token.

---

## Step 2 — Connect Render to GitHub

1. Sign up at https://render.com — free, no credit card.
2. After login, click **+ New** → **Web Service**.
3. Render asks to connect a GitHub account. Authorise it, give it access
   to your `alpha-pdf-proxy` repo (or all repos — whatever you prefer).
4. Pick the `alpha-pdf-proxy` repo from the list.

---

## Step 3 — Configure the service

Render auto-detects the `Dockerfile` and pre-fills most fields. You only
need to confirm / change these:

| Field | Value |
|---|---|
| **Name** | `alpha-pdf` (becomes part of the URL) |
| **Region** | Closest to you (e.g. `Frankfurt` for Germany) |
| **Branch** | `main` |
| **Runtime** | `Docker` (auto-detected) |
| **Instance Type** | **Free** |

That's it. Click **Create Web Service**.

Render builds the Docker image (takes ~2-3 minutes — installing
poppler-utils is the slow step) and starts it. The build log shows
progress in real time.

When the status turns green / "Live", you'll see your URL near the top:

```
https://alpha-pdf.onrender.com
```

---

## Step 4 — Test it from your browser

Open this URL in a normal browser (substitute your service name):

```
https://alpha-pdf.onrender.com/pdf2img?url=https%3A%2F%2Fwww.w3.org%2FWAI%2FER%2Ftests%2Fxhtml%2Ftestfiles%2Fresources%2Fpdf%2Fdummy.pdf&page=1
```

You should see a small PNG of "Dummy PDF file" — that's page 1 of a test
PDF rendered by your proxy.

If you see an error page instead, click **Logs** in the Render dashboard
to see what failed.

---

## Step 5 — Paste the URL into `pdfviewer.h`

Open `ALPHA/pdfviewer.h` and change the `PDF_PROXY_URL` line:

```cpp
#define PDF_PROXY_URL "https://alpha-pdf.onrender.com/pdf2img?url="
```

Reflash the ESP32. Open any `.pdf` link on the device — first request
will be slow (Render is waking the container) but every page after that
is fast. Subsequent visits to the SAME pdf load instantly because the
ESP32 caches them on the SD card.

---

## "I made a code change — how do I update?"

```bash
cd path/to/alpha-pdf-proxy
git add -A
git commit -m "describe your change"
git push
```

Render watches the GitHub branch — push → it rebuilds and redeploys
automatically. ~3 minutes per update.

---

## "The cold start sucks. Can I keep it warm?"

Three options:

1. **Cron-ping it.** Some free services (cron-job.org, uptimerobot.com)
   will ping `https://alpha-pdf.onrender.com/health` every 10 minutes.
   Renderwon't see it as idle so it never sleeps. Easiest fix.

2. **Pay $7/mo for the Starter plan.** No sleep, real CPU.

3. **Move to Oracle Cloud Always Free** — see `DEPLOY-oracle.md`. More
   setup but truly always-on at no cost.
