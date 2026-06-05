# Deploy pdf2img to Oracle Cloud Always Free — no sleep, no cold start

Oracle's Always Free tier gives you a real Linux VM that runs 24/7 at no
cost. The tradeoff vs Render is: more setup (about an hour), but no cold
starts, no sleep, and no GitHub round-trip to deploy changes.

You only need to do this once. After it's running, you basically forget
about it.

---

## Step 1 — Create an Oracle Cloud account

1. Go to https://www.oracle.com/cloud/free/ → **Start for free**.
2. Sign up. They DO ask for a credit card for identity verification, but
   they won't charge it as long as you only use "Always Free"-eligible
   resources. (No surprise bills like AWS.)
3. The signup flow takes ~15 minutes and includes phone verification.

---

## Step 2 — Create an Always-Free VM

1. After signup, in the Oracle Cloud Console, click the hamburger menu →
   **Compute** → **Instances**.
2. Click **Create Instance**.
3. Settings:
   - **Name**: `alpha-pdf`
   - **Image**: leave the default (Oracle Linux 8 or Ubuntu 22.04).
     Ubuntu is easier if you've used apt before.
   - **Shape**: click **Change shape** → tick **Always Free Eligible** →
     pick `VM.Standard.E2.1.Micro` (1/8 OCPU, 1 GB RAM). If sold out in
     your region, try `VM.Standard.A1.Flex` with 1 OCPU / 6 GB RAM
     (ARM — works fine for our use).
   - **Networking**: pick the auto-created VCN. Make sure **Assign a
     public IPv4 address** is ticked.
   - **SSH keys**: tick **Generate a key pair**. **DOWNLOAD BOTH the
     private and public key files.** You can't re-download the private
     key later. Save them somewhere safe.
4. Click **Create**. The instance comes up in ~2 minutes. Note its
   **Public IPv4 address** — that's your server's address.

---

## Step 3 — Open port 8080 to the internet

By default Oracle's firewall blocks everything except SSH.

1. From the instance's page, click the **Subnet** link (under "Primary VNIC").
2. Click the **Default Security List**.
3. **Add Ingress Rules**:
   - Source CIDR: `0.0.0.0/0`
   - Destination port range: `8080`
   - Description: `pdf2img`
4. Save.

(Inside the VM, the OS firewall ALSO blocks this. We'll open it there in
step 5.)

---

## Step 4 — SSH into the VM

On Windows, easiest is PowerShell:

```powershell
ssh -i C:\path\to\ssh-key-2026-XX-XX.key opc@<your-public-ip>
# (or "ubuntu@" if you picked Ubuntu)
```

If you get a "permissions too open" error, run:

```powershell
icacls "C:\path\to\ssh-key-2026-XX-XX.key" /inheritance:r /grant:r "$($env:USERNAME):(R)"
```

You should land in a shell prompt on the VM.

---

## Step 5 — Install the dependencies and run pdf2img

Inside the VM (Ubuntu):

```bash
# System packages
sudo apt update
sudo apt install -y python3-pip python3-venv poppler-utils git

# Open the OS firewall
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8080 -j ACCEPT
sudo netfilter-persistent save     # if installed; else next reboot loses it

# Get the proxy code (either git clone from your GitHub OR scp from your PC)
git clone https://github.com/<your-username>/alpha-pdf-proxy.git
cd alpha-pdf-proxy

# Python venv + dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Quick test
python3 pdf2img.py
# Should say: "Running on http://0.0.0.0:8080"
```

Now from your normal browser (NOT inside the VM), open:

```
http://<your-public-ip>:8080/pdf2img?url=https%3A%2F%2Fwww.w3.org%2FWAI%2FER%2Ftests%2Fxhtml%2Ftestfiles%2Fresources%2Fpdf%2Fdummy.pdf&page=1
```

You should see a small "Dummy PDF file" PNG. If yes, the server is
reachable from the internet. Ctrl-C the python process in SSH.

---

## Step 6 — Run as a systemd service so it survives reboots

```bash
sudo tee /etc/systemd/system/pdf2img.service > /dev/null <<EOF
[Unit]
Description=ALPHA pdf2img proxy
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/alpha-pdf-proxy
ExecStart=/home/ubuntu/alpha-pdf-proxy/venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 2 --timeout 120 pdf2img:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pdf2img
sudo systemctl status pdf2img        # confirm "active (running)"
```

If you used a non-Ubuntu image, replace `ubuntu` in the file with your
login user (`opc` for Oracle Linux).

---

## Step 7 — Optional: HTTPS via Caddy

The ESP32 talks plain HTTP fine, but if you want HTTPS (and a friendly
domain like `pdf.yourdomain.com` instead of an IP):

```bash
# Install Caddy — auto-HTTPS via Let's Encrypt
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# Point your DNS A record (pdf.yourdomain.com) at the VM's public IP first.
sudo tee /etc/caddy/Caddyfile > /dev/null <<EOF
pdf.yourdomain.com {
    reverse_proxy localhost:8080
}
EOF

sudo systemctl restart caddy
```

Open port 443 in both the VCN security list AND the OS firewall same way
you opened 8080. Caddy automatically gets a Let's Encrypt cert.

Then in `pdfviewer.h`:

```cpp
#define PDF_PROXY_URL "https://pdf.yourdomain.com/pdf2img?url="
```

---

## "How do I update the code on the VM?"

```bash
ssh ubuntu@<your-public-ip>
cd alpha-pdf-proxy
git pull
sudo systemctl restart pdf2img
```

Done.
