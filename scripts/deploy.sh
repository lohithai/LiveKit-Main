#!/bin/bash
# ============================================================
# Truliv LiveKit - Deploy / Restart Script
# Usage: bash scripts/deploy.sh
# ============================================================

set -euo pipefail

# Navigate to project root
cd "$(dirname "$0")/.."

echo "========================================"
echo "  Truliv LiveKit - Deploying"
echo "========================================"

# ── 1. Pull latest Docker images ────────────────────────────
echo ""
echo "=== Pulling latest images ==="
docker compose pull

# ── 2. Build agent container ────────────────────────────────
echo ""
echo "=== Building Truliv Agent ==="
docker compose build truliv-agent

# ── 3. Start all services ───────────────────────────────────
echo ""
echo "=== Starting all services ==="
docker compose up -d

# ── 4. Wait for services to start ───────────────────────────
echo ""
echo "=== Waiting for services to start (10s) ==="
sleep 10

# ── 5. Show service status ──────────────────────────────────
echo ""
echo "=== Service Status ==="
docker compose ps

# ── 6. Show recent logs ─────────────────────────────────────
echo ""
echo "=== Recent Agent Logs ==="
docker compose logs truliv-agent --tail 20

echo ""
echo "========================================"
echo "  Deployment Complete!"
echo "========================================"
echo ""
echo "Useful commands:"
echo "  docker compose ps              # Check service status"
echo "  docker compose logs -f         # Follow all logs"
echo "  docker compose logs -f truliv-agent  # Follow agent logs"
echo "  docker compose restart truliv-agent  # Restart agent only"
echo "  docker compose down            # Stop all services"
echo ""
