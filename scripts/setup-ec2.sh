#!/bin/bash
# ============================================================
# Truliv LiveKit - EC2 Initial Setup Script
# Run on a fresh Ubuntu 22.04 LTS EC2 instance
# Usage: bash scripts/setup-ec2.sh
# ============================================================

set -euo pipefail

echo "========================================"
echo "  Truliv LiveKit - EC2 Setup"
echo "========================================"

# ── 1. System update ────────────────────────────────────────
echo ""
echo "=== Step 1: Updating system packages ==="
sudo apt-get update && sudo apt-get upgrade -y

# ── 2. Install Docker ───────────────────────────────────────
echo ""
echo "=== Step 2: Installing Docker ==="
if command -v docker &> /dev/null; then
    echo "Docker already installed: $(docker --version)"
else
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    echo "Docker installed successfully"
fi

# ── 3. Install Docker Compose plugin ────────────────────────
echo ""
echo "=== Step 3: Verifying Docker Compose ==="
if docker compose version &> /dev/null; then
    echo "Docker Compose already available: $(docker compose version)"
else
    sudo apt-get install -y docker-compose-plugin
    echo "Docker Compose installed"
fi

# ── 4. Install LiveKit CLI (lk) ─────────────────────────────
echo ""
echo "=== Step 4: Installing LiveKit CLI ==="
if command -v lk &> /dev/null; then
    echo "LiveKit CLI already installed: $(lk version)"
else
    curl -sSL https://get.livekit.io/cli | bash
    echo "LiveKit CLI installed"
fi

# ── 5. Configure firewall (ufw) ─────────────────────────────
echo ""
echo "=== Step 5: Configuring firewall ==="
sudo ufw allow 22/tcp        # SSH
sudo ufw allow 80/tcp        # HTTP (Caddy)
sudo ufw allow 443/tcp       # HTTPS (Caddy + TURN TLS)
sudo ufw allow 443/udp       # TURN UDP
sudo ufw allow 7880/tcp      # LiveKit API
sudo ufw allow 7881/tcp      # WebRTC TCP
sudo ufw allow 5060/udp      # SIP signaling (UDP)
sudo ufw allow 5060/tcp      # SIP signaling (TCP)
sudo ufw allow 5349/tcp      # TURN TLS
sudo ufw allow 50000:60000/udp  # WebRTC media (RTP)
sudo ufw --force enable
echo "Firewall configured with all required ports"

# ── 6. Summary ──────────────────────────────────────────────
echo ""
echo "========================================"
echo "  Setup Complete!"
echo "========================================"
echo ""
echo "IMPORTANT: Log out and log back in for Docker group to take effect."
echo ""
echo "Next steps:"
echo "  1. Clone or upload your project to this instance"
echo "  2. Generate LiveKit API keys: lk generate-keys"
echo "  3. Fill in configuration files:"
echo "     - .env (LiveKit API key/secret)"
echo "     - agent/.env.local (all API keys)"
echo "     - livekit/livekit.yaml (API keys + domain)"
echo "     - caddy/Caddyfile (domain)"
echo "  4. Start services: docker compose up -d"
echo "  5. Create SIP trunks: see sip/ directory"
echo ""
