#!/usr/bin/env bash
# scripts/deploy.sh
# ─────────────────────────────────────────────────────────────────────────────
# One-command production deployment for Ubuntu VPS.
# Idempotent — safe to run multiple times.
#
# Usage:
#   chmod +x scripts/deploy.sh
#   ./scripts/deploy.sh
#
# What this does:
#   1. Validates .env exists and has required variables
#   2. Creates required directories
#   3. Sets correct file permissions
#   4. Builds Docker image
#   5. Starts all services
#   6. Waits for health check
#   7. Prints deployment summary
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'; BOLD='\033[1m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
fail() { echo -e "${RED}✗ FATAL: $*${NC}"; exit 1; }
info() { echo -e "${BLUE}→${NC} $*"; }
head() { echo -e "\n${BOLD}$*${NC}"; }

# ─── Prerequisites ────────────────────────────────────────────────────────────
head "Checking prerequisites..."

command -v docker        >/dev/null 2>&1 || fail "Docker not installed. Run: curl -fsSL https://get.docker.com | sh"
command -v docker compose >/dev/null 2>&1 || fail "Docker Compose not found. Update Docker to a version with Compose v2."

ok "Docker $(docker --version | grep -oP '(?<=version )[0-9.]+')"
ok "Compose $(docker compose version --short)"

# ─── Validate .env ────────────────────────────────────────────────────────────
head "Validating configuration..."

[[ -f ".env" ]] || fail ".env file not found. Copy .env.example and fill in your credentials."

source .env 2>/dev/null || true

# Required variables
REQUIRED_VARS=(
    "HYPERLIQUID_PRIVATE_KEY"
    "HYPERLIQUID_WALLET_ADDRESS"
    "API_SECRET"
    "OPENAI_API_KEY"
)

MISSING=()
for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        MISSING+=("$var")
    fi
done
[[ ${#MISSING[@]} -eq 0 ]] || fail "Missing required .env variables: ${MISSING[*]}"

# Security checks
if [[ "${API_HOST:-}" == "0.0.0.0" ]]; then
    warn "API_HOST=0.0.0.0 exposes the API to the internet. Strongly recommend 127.0.0.1."
fi

if [[ "${DRY_RUN:-false}" == "true" ]]; then
    warn "DRY_RUN=true — paper trading mode. No real orders will be placed."
fi

KEY="${HYPERLIQUID_PRIVATE_KEY:-}"
if [[ ${#KEY} -lt 60 ]]; then
    fail "HYPERLIQUID_PRIVATE_KEY appears invalid (too short). Check your .env."
fi

ok "All required environment variables present."

# Check Telegram config
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] || [[ -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    warn "Telegram NOT configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID for push alerts."
    warn "Strongly recommended for unattended 24/7 operation."
fi

# ─── Directories & Permissions ────────────────────────────────────────────────
head "Setting up directories..."

mkdir -p data logs backups
chmod 700 data     # state files contain trade metadata
chmod 755 logs backups

# .env must not be world-readable
chmod 600 .env
ok "Permissions set."

# ─── Check algo.py ───────────────────────────────────────────────────────────
if [[ ! -f "algo.py" ]]; then
    warn "algo.py not found — bot will use built-in algo rules."
fi

# ─── Build Docker image ───────────────────────────────────────────────────────
head "Building Docker image..."
docker compose build --no-cache bot
ok "Image built."

# ─── Start services ───────────────────────────────────────────────────────────
head "Starting services..."

# Graceful stop if already running
docker compose down --remove-orphans 2>/dev/null || true

docker compose up -d
ok "Services started."

# ─── Wait for health check ────────────────────────────────────────────────────
head "Waiting for bot health check..."

API_PORT="${API_PORT:-3000}"
MAX_WAIT=120
WAITED=0

while [[ $WAITED -lt $MAX_WAIT ]]; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' trading-bot 2>/dev/null || echo "starting")
    if [[ "$STATUS" == "healthy" ]]; then
        ok "Bot is healthy!"
        break
    fi
    echo -ne "\r  Waiting... ${WAITED}s (status: ${STATUS})   "
    sleep 5
    WAITED=$((WAITED + 5))
done
echo ""

if [[ "$STATUS" != "healthy" ]]; then
    warn "Bot did not become healthy within ${MAX_WAIT}s."
    warn "Check logs: docker compose logs -f bot"
fi

# ─── Deployment summary ───────────────────────────────────────────────────────
head "Deployment Summary"

echo ""
echo "  Services:"
docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || docker compose ps

echo ""
echo "  Useful commands:"
echo "    docker compose logs -f bot          # live bot logs"
echo "    docker compose logs -f backup       # backup logs"
echo "    docker compose ps                   # service status"
echo "    ./scripts/health_check.sh           # full health report"
echo "    ./scripts/backup.sh                 # manual backup"
echo ""
echo "  API Endpoints (requires API_SECRET):"
echo "    curl 'http://localhost:${API_PORT}/health?key=YOUR_SECRET'"
echo "    curl 'http://localhost:${API_PORT}/status?key=YOUR_SECRET'"
echo "    curl 'http://localhost:${API_PORT}/alarms?key=YOUR_SECRET'"
echo ""

if [[ "${DRY_RUN:-false}" == "true" ]]; then
    echo -e "  ${YELLOW}⚠ PAPER TRADING MODE — No real trades${NC}"
else
    echo -e "  ${RED}⚠ LIVE TRADING MODE — Real money at risk${NC}"
fi
echo ""

ok "Deployment complete."
