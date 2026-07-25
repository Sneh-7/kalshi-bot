# Deploying kalshi-bot to a 24/7 VPS

The bot must run somewhere always-on. This guide uses a cheap Linux VPS
(Hetzner CX22 ~€4/mo, or a DigitalOcean $6 droplet). Ubuntu 22.04/24.04.

## Key difference from your Mac

On the server, Claude decisions run through the **Anthropic API**, not the CLI
(the CLI needs an interactive subscription login). So the server `.env` must set:

```
ANALYST_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...      # the one paid key you need
```

Everything else stays keyless. Groq remains the free fallback.

## Recommended server .env for the PAPER week

```
TRADING_MODE=PAPER
# Point at PRODUCTION market data so you get the real market universe
# (political / "what will Trump say" markets). PAPER still only simulates fills,
# so this needs NO Kalshi key and risks NO money.
KALSHI_BASE_URL=https://external-api.kalshi.com/trade-api/v2

ANALYST_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...
GROQ_API_KEY=            # optional, free fallback
TELEGRAM_BOT_TOKEN=      # optional, phone alerts + control
TELEGRAM_CHAT_ID=
```

Leave `KALSHI_KEY_ID` / PEM blank until after the calibration gate.

---

## Creating the VM on Oracle Cloud (Always Free)

1. **Sign up:** cloud.oracle.com → "Start for free". Card is for identity
   verification only — Always Free resources are never charged. **Pick a home
   region near the US** (e.g. **US East (Ashburn)**) — it can't be changed later.
2. **Create the instance:** Console → hamburger menu → **Compute → Instances →
   Create instance**.
   - **Image:** Canonical **Ubuntu 24.04**.
   - **Shape:** click *Change shape* → **Ampere (ARM)** `VM.Standard.A1.Flex`
     with 1 OCPU / 6 GB (Always Free) — OR if you hit "out of capacity", use
     **`VM.Standard.E2.1.Micro`** (AMD x86, 1 OCPU / 1 GB, also Always Free).
     Either is plenty; the bot is tiny. (Our Docker image runs on both ARM and x86.)
   - **SSH keys:** choose *Generate a key pair for me* and **download the private
     key** (or paste your own public key).
   - Leave networking at defaults (it creates a VCN with a public IP and opens
     SSH/port 22). The bot only makes **outbound** calls, so no other ports needed.
   - **Create.** When it's running, copy the **Public IP address**.
3. **Connect** (Oracle's Ubuntu user is `ubuntu`):
   ```bash
   chmod 600 ~/Downloads/your-key.key
   ssh -i ~/Downloads/your-key.key ubuntu@YOUR_PUBLIC_IP
   ```

Then follow Option A below (Docker).

## Option A — Docker (recommended)

```bash
# 1. On the VPS, install Docker.
curl -fsSL https://get.docker.com | sh

# 2. Get the code (git clone your repo, or scp the folder up).
git clone <your-repo-url> kalshi && cd kalshi

# 3. Create .env from the template and edit it (see settings above).
cp example.env .env
nano .env

# 4. Health check (one-off), then start 24/7.
docker compose run --rm bot python main.py --check
docker compose up -d --build

# 5. Watch it.
docker compose logs -f
```

`restart: unless-stopped` in `docker-compose.yml` means it auto-restarts on crash
and on VPS reboot. The `./data` volume keeps your SQLite audit trail across
restarts, so the calibration sample is never lost.

## Option B — systemd (no Docker)

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo mkdir -p /opt/kalshi && sudo chown $USER /opt/kalshi
git clone <your-repo-url> /opt/kalshi && cd /opt/kalshi
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp example.env .env && nano .env
.venv/bin/python main.py --check          # verify

sudo cp deploy/kalshi-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-bot
journalctl -u kalshi-bot -f               # logs
```

`Restart=always` handles crashes and reboots.

---

## Monitoring

- **Telegram** (if configured): `/status`, `/pnl`, `/calibration`, `/positions`,
  `/pause`, `/resume`, `/kill`. You get a startup message and a watchdog alert if
  the loop stalls.
- **Logs**: `docker compose logs -f` or `journalctl -u kalshi-bot -f`.

## After the paper week (the gate)

Run `/calibration`. **Only if the model beats the market** do you go live:

1. Create Kalshi **production** API keys (kalshi.com → Profile → API Keys), put
   the PEM in `./secrets/kalshi_private_key.pem`, set `KALSHI_KEY_ID`.
2. Set `TRADING_MODE=RECOMMEND` (approval-gated via Telegram).
3. `docker compose up -d --build` (or `systemctl restart kalshi-bot`).

## Cost

VPS ~$5/mo + Claude API (a few $/day, only when fresh news matches a market).
Everything else free. Comfortably within budget.
