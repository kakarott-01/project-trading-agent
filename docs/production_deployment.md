# Production Deployment Guide
**Hyperliquid AI Trading Bot — Single-User VPS Deployment**

---

## Quick Reference

| Step | Command |
|------|---------|
| First-time deploy | `./scripts/deploy.sh` |
| Check health | `./scripts/health_check.sh` |
| View logs | `docker compose logs -f bot` |
| Manual backup | `./scripts/backup.sh` |
| Update bot | `./scripts/update.sh` |
| Emergency stop | `docker compose stop bot` |

---

## Recommended VPS Specifications

### Minimum (Paper Trading / Low-Asset Count)
- **CPU**: 1 vCPU (any modern processor)
- **RAM**: 1 GB
- **Storage**: 20 GB SSD
- **Bandwidth**: 100 Mbps
- **Cost**: ~$5–6/month (Hetzner CX11, DigitalOcean Droplet, Vultr)

### Recommended (Live Trading, 3–7 Assets)
- **CPU**: 2 vCPUs
- **RAM**: 2 GB
- **Storage**: 40 GB SSD
- **Bandwidth**: 200 Mbps
- **Cost**: ~$10–15/month

### Provider Recommendations
- **Hetzner** (Germany/Finland): Best price-to-performance. CX21 (2 vCPU / 4GB RAM) = €5.52/mo
- **DigitalOcean**: Easy interface, reliable. Basic Droplet 2GB = $12/mo
- **Vultr**: Multiple regions, fast provisioning. 2GB = $12/mo
- **Contabo**: Cheapest. VPS S (4GB RAM) = €5.99/mo (acceptable latency)

**Avoid**: AWS/GCP/Azure (expensive, complex). Free-tier VMs (unreliable, throttled).

**Location**: Choose a datacenter near Hyperliquid's servers. US/EU both fine. Singapore for Asia-Pacific users.

---

## Initial VPS Setup (Ubuntu 22.04 LTS)

### Step 1: Secure the VPS

```bash
# Connect as root
ssh root@YOUR_VPS_IP

# Create a non-root user
adduser tradingbot
usermod -aG sudo tradingbot

# Set up SSH key authentication
mkdir -p /home/tradingbot/.ssh
cp ~/.ssh/authorized_keys /home/tradingbot/.ssh/
chown -R tradingbot:tradingbot /home/tradingbot/.ssh
chmod 700 /home/tradingbot/.ssh
chmod 600 /home/tradingbot/.ssh/authorized_keys

# Disable password auth and root login
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh

# Switch to tradingbot user for all further operations
su - tradingbot
```

### Step 2: Firewall

```bash
# Allow SSH only from your IP (replace YOUR_IP)
ufw allow from YOUR_IP to any port 22 proto tcp

# Block everything else — API runs on localhost only
ufw default deny incoming
ufw default allow outgoing
ufw enable
ufw status
```

**Critical**: The trading bot API runs on `127.0.0.1:3000` (localhost only by default). Never expose it to the internet directly. If you need remote access to the dashboard, use SSH tunnel:

```bash
# From your local machine:
ssh -L 3000:127.0.0.1:3000 tradingbot@YOUR_VPS_IP
# Then open http://localhost:3000 in your browser
```

### Step 3: Install Docker

```bash
# Install Docker (one command)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker tradingbot

# Log out and back in for group membership
exit
ssh tradingbot@YOUR_VPS_IP

# Verify
docker --version
docker compose version
```

### Step 4: Upload Bot Files

From your local machine:

```bash
# Create directory on VPS
ssh tradingbot@YOUR_VPS_IP "mkdir -p ~/trading-bot"

# Upload the bot package (replace path to your zip)
scp -r /path/to/trading-bot/* tradingbot@YOUR_VPS_IP:~/trading-bot/

# Or if using scp of a zip:
scp trading-bot.zip tradingbot@YOUR_VPS_IP:~
ssh tradingbot@YOUR_VPS_IP "cd ~ && unzip trading-bot.zip -d trading-bot"
```

---

## Configuration

### Step 5: Create .env File

```bash
cd ~/trading-bot
cp .env.example .env
nano .env  # or vim .env
```

**Required variables:**

```dotenv
# ═══════════════════════════════════════════════
# EXCHANGE CREDENTIALS
# ═══════════════════════════════════════════════
HYPERLIQUID_PRIVATE_KEY=0x_your_agent_private_key_here
HYPERLIQUID_WALLET_ADDRESS=0x_your_wallet_address_here

# Use testnet for first run
HYPERLIQUID_TESTNET=false

# ═══════════════════════════════════════════════
# AI PROVIDER (choose one)
# ═══════════════════════════════════════════════
OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
AI_PROVIDER=openai

# ═══════════════════════════════════════════════
# API SECURITY (generate with: openssl rand -hex 32)
# ═══════════════════════════════════════════════
API_SECRET=your_32_char_hex_secret_here
API_HOST=127.0.0.1
API_PORT=3000

# ═══════════════════════════════════════════════
# TELEGRAM ALERTS (strongly recommended for 24/7)
# See docs/telegram_alerts.md for setup
# ═══════════════════════════════════════════════
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ═══════════════════════════════════════════════
# TRADING CONFIGURATION
# ═══════════════════════════════════════════════
ASSETS=BTC,ETH
INTERVAL=5m
DRY_RUN=true   # SET TO FALSE ONLY AFTER PAPER TESTING

# ═══════════════════════════════════════════════
# RISK SETTINGS (start conservative)
# ═══════════════════════════════════════════════
SAFE_RETAIL_MODE=true
SAFE_RETAIL_PRESET=conservative
# conservative: max 3x leverage, 5% per position, 8% daily circuit breaker
```

**Security reminder:** `chmod 600 .env` — the deploy script does this automatically.

---

## Deployment

### Step 6: First Deploy

```bash
cd ~/trading-bot
chmod +x scripts/*.sh
./scripts/deploy.sh
```

The deploy script will:
- Validate your `.env` configuration
- Check for common misconfigurations
- Build the Docker image
- Start all services (bot + backup + logrotate)
- Wait for health check
- Print a deployment summary

### Step 7: Verify Deployment

```bash
# Full health report
./scripts/health_check.sh

# Watch live logs (Ctrl+C to exit)
docker compose logs -f bot

# Check specific service
docker compose ps
```

---

## Filesystem Layout

```
~/trading-bot/
├── docker-compose.yml       # Service definitions
├── Dockerfile               # Bot container
├── .env                     # Your credentials (chmod 600)
├── algo.py                  # Custom strategy (optional)
│
├── data/                    # Persistent state (Docker volume)
│   ├── active_trades.json   # Open positions — CRITICAL
│   ├── risk_state.json      # Circuit breaker state — CRITICAL
│   ├── diary.jsonl          # Trade diary
│   ├── alarms.jsonl         # Critical alarms
│   └── decisions.jsonl      # AI decisions log
│
├── logs/                    # Log files
│   ├── trading.log          # Main application log
│   ├── llm_requests.log     # AI API request log (private)
│   └── prompts.log          # Prompt log (private)
│
├── backups/                 # Automatic state backups
│   ├── 20250516_140000/     # Timestamped backup dirs
│   └── ...
│
└── scripts/
    ├── deploy.sh            # Initial deployment
    ├── backup.sh            # Manual backup
    ├── update.sh            # Safe update
    └── health_check.sh      # Health report
```

---

## Auto-Restart Behavior

The bot uses Docker's `restart: always` policy:

| Event | Behavior |
|-------|----------|
| Bot crash | Restarts automatically within seconds |
| VPS reboot | Docker starts automatically, bot starts with it |
| Docker daemon restart | Bot resumes on daemon start |
| Manual stop (`docker compose stop`) | Does NOT auto-restart until `docker compose start` |

**On restart**: The bot runs startup reconciliation — it reads `active_trades.json`, then verifies against the live exchange. Any discrepancies are resolved in favour of exchange truth. Your positions are safe across restarts.

---

## SSH Tunnel for Dashboard Access

Since the API runs on localhost only, use SSH tunneling to access it remotely:

```bash
# On your LOCAL machine:
ssh -L 3000:127.0.0.1:3000 -N tradingbot@YOUR_VPS_IP

# Keep this running in a terminal, then open:
# http://localhost:3000/status?key=YOUR_API_SECRET
```

Or for the monitoring dashboard:
```bash
ssh -L 3000:127.0.0.1:3000 -N tradingbot@YOUR_VPS_IP &
open http://localhost:3000/dashboard?key=YOUR_API_SECRET
```

---

## Monitoring Schedule (Recommended for Live Trading)

| Frequency | Action |
|-----------|--------|
| Daily | Check `/alarms` endpoint or Telegram for any CRITICAL alerts |
| Daily | Review `diary.jsonl` for trades opened/closed |
| Weekly | Run `./scripts/health_check.sh` |
| Weekly | Review disk space and log sizes |
| Monthly | Review performance metrics at `/status` |
| Before going live | Run paper trading for minimum 2 weeks |

---

## Emergency Procedures

### Emergency Stop (Stop All Trading)
```bash
docker compose stop bot
```
This stops the bot. **Existing exchange positions remain open** — they are on the exchange, not in the bot. You must close them manually via Hyperliquid's interface.

### Complete Shutdown (Stop All Services)
```bash
docker compose down
```
Stops all services. Data volumes are preserved.

### Reset to Clean State (DANGEROUS)
```bash
docker compose down
rm -f data/active_trades.json data/risk_state.json
# WARNING: This loses local state. Bot will reconcile from exchange on next start.
docker compose up -d
```

### View Recent Alarms
```bash
# Via API
curl "http://localhost:3000/alarms?key=$API_SECRET" | python3 -m json.tool

# Via file
tail -50 data/alarms.jsonl | python3 -c "import sys,json; [print(json.dumps(json.loads(l), indent=2)) for l in sys.stdin]"
```
