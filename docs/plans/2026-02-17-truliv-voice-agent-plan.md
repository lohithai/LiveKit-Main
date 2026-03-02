# Truliv Voice AI Agent - Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy a self-hosted LiveKit voice AI agent on AWS that handles inbound/outbound calls for Truliv Coliving using Sarvam STT, Cartesia TTS, and Gemini 2.5 Flash LLM.

**Architecture:** Single EC2 (c5.xlarge, ap-south-1) running Docker Compose with 5 services: LiveKit Server, SIP Server, Redis, Caddy (TLS), and the Python agent. Custom SIP trunk routes calls through LiveKit SIP to the agent.

**Tech Stack:** LiveKit (self-hosted), Python 3.12, livekit-agents SDK, Sarvam saarika:v2.5 (STT), Cartesia sonic-3 (TTS), Google Gemini 2.5 Flash (LLM), MongoDB Atlas, Docker Compose, Caddy, Redis.

**Reference code:** `/Users/lohith/Desktop/Projects/livekitlatest/agent/`

---

## Task 1: Project Structure & Copy Unchanged Files

**Files:**
- Create: `agent/` directory structure
- Copy from reference: `agent_tools.py`, `database.py`, `lead_sync.py`, `logger.py`, `sheets_client.py`, `task_queue.py`, `helpers/`

**Step 1: Create directory structure**

```bash
mkdir -p /Users/lohith/Desktop/LiveKit/{agent/helpers,caddy,livekit,sip,scripts}
```

**Step 2: Copy unchanged files from reference project**

```bash
cp /Users/lohith/Desktop/Projects/livekitlatest/agent/agent_tools.py /Users/lohith/Desktop/LiveKit/agent/
cp /Users/lohith/Desktop/Projects/livekitlatest/agent/database.py /Users/lohith/Desktop/LiveKit/agent/
cp /Users/lohith/Desktop/Projects/livekitlatest/agent/lead_sync.py /Users/lohith/Desktop/LiveKit/agent/
cp /Users/lohith/Desktop/Projects/livekitlatest/agent/logger.py /Users/lohith/Desktop/LiveKit/agent/
cp /Users/lohith/Desktop/Projects/livekitlatest/agent/sheets_client.py /Users/lohith/Desktop/LiveKit/agent/
cp /Users/lohith/Desktop/Projects/livekitlatest/agent/task_queue.py /Users/lohith/Desktop/LiveKit/agent/
cp /Users/lohith/Desktop/Projects/livekitlatest/agent/helpers/__init__.py /Users/lohith/Desktop/LiveKit/agent/helpers/
cp /Users/lohith/Desktop/Projects/livekitlatest/agent/helpers/warden_corn_api.py /Users/lohith/Desktop/LiveKit/agent/helpers/
```

**Step 3: Initialize git repository**

```bash
cd /Users/lohith/Desktop/LiveKit
git init
git add -A
git commit -m "chore: initial project structure with unchanged reference files"
```

---

## Task 2: Create LiveKit Server Configuration

**Files:**
- Create: `livekit/livekit.yaml`

**Step 1: Create LiveKit server config**

Create `livekit/livekit.yaml` with the following content. The `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` will be generated in the next step.

```yaml
port: 7880
log_level: info
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true
redis:
  address: redis:6379
keys:
  # Replace with generated key/secret pair
  # APIxxxxxxx: YourSecretHere
turn:
  enabled: true
  domain: YOUR_DOMAIN_HERE
  tls_port: 5349
  udp_port: 443
```

Note: API keys will be placeholders. User fills in their domain and generated keys.

**Step 2: Commit**

```bash
git add livekit/livekit.yaml
git commit -m "feat: add LiveKit server configuration"
```

---

## Task 3: Create Caddy Reverse Proxy Configuration

**Files:**
- Create: `caddy/Caddyfile`

**Step 1: Create Caddyfile**

Caddy auto-provisions TLS certificates from Let's Encrypt. It reverse-proxies HTTPS traffic to LiveKit's HTTP port.

```
YOUR_DOMAIN_HERE {
    reverse_proxy livekit-server:7880
}
```

User replaces `YOUR_DOMAIN_HERE` with their actual domain (e.g., `livekit.truliv.com`).

**Step 2: Commit**

```bash
git add caddy/Caddyfile
git commit -m "feat: add Caddy reverse proxy with auto-TLS"
```

---

## Task 4: Create SIP Trunk Configurations

**Files:**
- Create: `sip/inbound-trunk.json`
- Create: `sip/outbound-trunk.json`
- Create: `sip/dispatch-rule.json`

**Step 1: Create inbound trunk config**

`sip/inbound-trunk.json`:
```json
{
  "trunk": {
    "name": "truliv-inbound",
    "numbers": ["+91XXXXXXXXXX"],
    "allowed_addresses": ["YOUR_SIP_PROVIDER_IP/32"]
  }
}
```

**Step 2: Create outbound trunk config**

`sip/outbound-trunk.json`:
```json
{
  "trunk": {
    "name": "truliv-outbound",
    "address": "YOUR_SIP_PROVIDER_HOST:5060",
    "numbers": ["+91XXXXXXXXXX"],
    "auth_username": "YOUR_SIP_USERNAME",
    "auth_password": "YOUR_SIP_PASSWORD"
  }
}
```

**Step 3: Create dispatch rule**

`sip/dispatch-rule.json`:
```json
{
  "rule": {
    "dispatchRuleIndividual": {
      "roomPrefix": "call-"
    }
  }
}
```

**Step 4: Commit**

```bash
git add sip/
git commit -m "feat: add SIP trunk configurations (inbound, outbound, dispatch)"
```

---

## Task 5: Create Docker Compose File

**Files:**
- Create: `docker-compose.yml`

**Step 1: Create docker-compose.yml**

This is the core deployment file that orchestrates all 5 services.

```yaml
version: "3.9"

services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data

  livekit-server:
    image: livekit/livekit-server:latest
    restart: unless-stopped
    ports:
      - "7880:7880"
      - "7881:7881"
      - "50000-60000:50000-60000/udp"
    volumes:
      - ./livekit/livekit.yaml:/etc/livekit.yaml
    command: ["--config", "/etc/livekit.yaml"]
    depends_on:
      - redis

  livekit-sip:
    image: livekit/sip:latest
    restart: unless-stopped
    network_mode: host
    environment:
      - SIP_PORT=5060
      - LIVEKIT_URL=ws://localhost:7880
      - LIVEKIT_API_KEY=${LIVEKIT_API_KEY}
      - LIVEKIT_API_SECRET=${LIVEKIT_API_SECRET}
    depends_on:
      - livekit-server

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile
      - caddy-data:/data
      - caddy-config:/config
    depends_on:
      - livekit-server

  truliv-agent:
    build:
      context: ./agent
      dockerfile: Dockerfile
    restart: unless-stopped
    env_file:
      - ./agent/.env.local
    environment:
      - LIVEKIT_URL=ws://livekit-server:7880
      - LIVEKIT_API_KEY=${LIVEKIT_API_KEY}
      - LIVEKIT_API_SECRET=${LIVEKIT_API_SECRET}
    depends_on:
      - livekit-server
      - redis

volumes:
  redis-data:
  caddy-data:
  caddy-config:
```

**Step 2: Create root .env file for shared LiveKit credentials**

`.env` (root level — shared by docker-compose):
```
LIVEKIT_API_KEY=APIxxxxxxxxx
LIVEKIT_API_SECRET=YourSecretHere
```

**Step 3: Commit**

```bash
git add docker-compose.yml .env
git commit -m "feat: add Docker Compose with LiveKit, SIP, Redis, Caddy, Agent"
```

---

## Task 6: Create Agent Dockerfile & pyproject.toml

**Files:**
- Create: `agent/Dockerfile`
- Create: `agent/pyproject.toml`

**Step 1: Create Dockerfile**

Based on reference but verified for our dependency set.

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for Docker layer caching
COPY pyproject.toml uv.lock* ./

# Install dependencies
RUN uv sync --frozen --no-dev || uv sync --no-dev

# Copy application code
COPY . .

# Run the agent
CMD ["uv", "run", "python", "main.py", "start"]
```

**Step 2: Create pyproject.toml**

Updated to include cartesia and sarvam plugins:

```toml
[project]
name = "truliv-agent"
version = "0.2.0"
description = "Truliv Voice AI Agent - Self-Hosted LiveKit"
requires-python = ">=3.10,<3.14"
dependencies = [
    # LiveKit core + plugins
    "livekit-agents[silero,turn-detector,google,cartesia,sarvam]~=1.3",
    # Config
    "python-dotenv",
    # HTTP clients
    "httpx",
    "aiohttp",
    "requests",
    # MongoDB
    "motor",
    "tenacity",
    # Google Sheets
    "gspread",
    "pandas",
    "google-auth",
    # LeadSquared / LLM for zero-deposit tool
    "langchain-core",
    "langchain-google-genai",
    # Logging
    "loguru",
]
```

Note: Removed `deepgram` from plugins (replaced by sarvam).

**Step 3: Commit**

```bash
git add agent/Dockerfile agent/pyproject.toml
git commit -m "feat: add agent Dockerfile and pyproject.toml with Cartesia + Sarvam"
```

---

## Task 7: Create .env.local Template

**Files:**
- Create: `agent/.env.local.example`

**Step 1: Create environment template**

```env
# === LiveKit ===
LIVEKIT_URL=ws://livekit-server:7880
LIVEKIT_API_KEY=APIxxxxxxxxx
LIVEKIT_API_SECRET=YourSecretHere

# === Agent ===
AGENT_NAME=truliv-telephony-agent
SIP_TRUNK_OUTBOUND_ID=your_outbound_trunk_id

# === STT: Sarvam AI ===
SARVAM_API_KEY=your_sarvam_api_key

# === TTS: Cartesia ===
CARTESIA_API_KEY=your_cartesia_api_key

# === LLM: Google Gemini ===
GOOGLE_API_KEY=your_google_api_key

# === MongoDB ===
MONGODB_CONNECTION_STRING=mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority

# === Google Sheets ===
GOOGLE_SERVICE_CRED={"type":"service_account","project_id":"..."}

# === LeadSquared CRM ===
LEADSQUARED_ACCESS_KEY=your_access_key
LEADSQUARED_SECRET_KEY=your_secret_key

# === Warden API ===
WARDEN_API_BASE_URL=https://truliv-cron-job.vercel.app/api
WARDEN_API_KEY=your_warden_api_key
```

**Step 2: Add .env.local to .gitignore**

Create `.gitignore`:
```
.env.local
agent/.env.local
.env
__pycache__/
*.pyc
.venv/
uv.lock
```

**Step 3: Commit**

```bash
git add agent/.env.local.example .gitignore
git commit -m "feat: add env template and gitignore"
```

---

## Task 8: Write Updated main.py

**Files:**
- Create: `agent/main.py`
- Reference: `/Users/lohith/Desktop/Projects/livekitlatest/agent/main.py`

**Step 1: Create updated main.py**

Key changes from reference:
1. STT: `deepgram.STT` -> `sarvam.STT(model="saarika:v2.5", language="hi-IN")`
2. TTS: `sarvam.TTS` -> `cartesia.TTS(model="sonic-3", voice="VOICE_ID")`
3. Remove deepgram import, add cartesia import
4. Keep all other logic identical (SIP handling, MongoDB context, cleanup, etc.)

The full file is written with these exact changes. All business logic (phone extraction, user ID normalization, greeting builder, MongoDB context loading, cleanup, call history) remains identical to the reference.

**Step 2: Commit**

```bash
git add agent/main.py
git commit -m "feat: update main.py with Sarvam STT + Cartesia TTS"
```

---

## Task 9: Write Updated assistant.py

**Files:**
- Create: `agent/assistant.py`
- Reference: `/Users/lohith/Desktop/Projects/livekitlatest/agent/assistant.py`

**Step 1: Create updated assistant.py**

Key changes from reference:
1. Language switching: Instead of `self.session.tts.update_options(target_language_code=tts_code)`, use Cartesia's language parameter: `self.session.tts.update_options(language=lang_code)`
2. Cartesia uses ISO language codes directly (e.g., "hi", "en", "ta") rather than BCP-47 codes like "hi-IN"
3. LANGUAGE_MAP values updated for Cartesia format
4. All tool methods remain identical

**Step 2: Commit**

```bash
git add agent/assistant.py
git commit -m "feat: update assistant.py with Cartesia language switching"
```

---

## Task 10: Write Optimized instruction.py

**Files:**
- Create: `agent/instruction.py`
- Reference: `/Users/lohith/Desktop/Projects/livekitlatest/agent/intruction.py`

**Step 1: Create optimized instruction.py**

Key changes:
1. Fix filename typo: `intruction.py` -> `instruction.py`
2. Reduce token count by making tool registry more concise
3. Remove duplicated rules and examples
4. Keep the state machine flow and all business logic identical
5. Update imports in assistant.py to reference `instruction` instead of `intruction`

**Step 2: Commit**

```bash
git add agent/instruction.py
git commit -m "feat: add optimized instruction.py (reduced token count)"
```

---

## Task 11: Write EC2 Setup Script

**Files:**
- Create: `scripts/setup-ec2.sh`

**Step 1: Create EC2 setup script**

This script runs on a fresh Ubuntu 22.04 EC2 instance to install all prerequisites.

```bash
#!/bin/bash
# Truliv LiveKit - EC2 Setup Script
# Run on fresh Ubuntu 22.04 LTS instance

set -euo pipefail

echo "=== Updating system ==="
sudo apt-get update && sudo apt-get upgrade -y

echo "=== Installing Docker ==="
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER

echo "=== Installing Docker Compose ==="
sudo apt-get install -y docker-compose-plugin

echo "=== Installing LiveKit CLI (lk) ==="
curl -sSL https://get.livekit.io/cli | bash

echo "=== Opening firewall ports ==="
# If using ufw:
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 7880/tcp
sudo ufw allow 7881/tcp
sudo ufw allow 5060/udp
sudo ufw allow 5060/tcp
sudo ufw allow 5349/tcp
sudo ufw allow 50000:60000/udp
sudo ufw --force enable

echo "=== Setup complete! ==="
echo "Log out and back in for Docker group to take effect."
echo "Then: cd /path/to/project && docker compose up -d"
```

**Step 2: Create deploy script**

`scripts/deploy.sh`:
```bash
#!/bin/bash
# Truliv LiveKit - Deploy/Restart Script
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Pulling latest images ==="
docker compose pull

echo "=== Building agent ==="
docker compose build truliv-agent

echo "=== Starting services ==="
docker compose up -d

echo "=== Checking service status ==="
docker compose ps

echo "=== Done! ==="
```

**Step 3: Make scripts executable and commit**

```bash
chmod +x scripts/setup-ec2.sh scripts/deploy.sh
git add scripts/
git commit -m "feat: add EC2 setup and deployment scripts"
```

---

## Task 12: Configure SIP Trunks via LiveKit CLI

**Files:** None (CLI commands only)

This task is run AFTER the server is deployed and running.

**Step 1: Generate LiveKit API key pair**

```bash
# On EC2 or locally with lk CLI installed:
lk generate-keys
# Output: API Key: APIxxxxx, Secret: xxxxxxx
# Save these in .env and livekit.yaml
```

**Step 2: Create inbound SIP trunk**

```bash
lk sip inbound create sip/inbound-trunk.json \
  --url https://YOUR_DOMAIN:7880 \
  --api-key YOUR_API_KEY \
  --api-secret YOUR_API_SECRET
```

**Step 3: Create outbound SIP trunk**

```bash
lk sip outbound create sip/outbound-trunk.json \
  --url https://YOUR_DOMAIN:7880 \
  --api-key YOUR_API_KEY \
  --api-secret YOUR_API_SECRET
```

Save the returned outbound trunk ID in `agent/.env.local` as `SIP_TRUNK_OUTBOUND_ID`.

**Step 4: Create dispatch rule**

```bash
lk sip dispatch create sip/dispatch-rule.json \
  --url https://YOUR_DOMAIN:7880 \
  --api-key YOUR_API_KEY \
  --api-secret YOUR_API_SECRET
```

---

## Task 13: AWS EC2 Launch & DNS Setup

This task is done in AWS Console (beginner-friendly steps).

**Step 1: Launch EC2 instance**
- Go to AWS Console -> EC2 -> Launch Instance
- Name: `truliv-livekit`
- AMI: Ubuntu 22.04 LTS
- Instance type: `c5.xlarge`
- Key pair: Create new or use existing
- Security group: Create with ports from design doc
- Storage: 30 GB gp3
- Region: ap-south-1 (Mumbai)

**Step 2: Allocate Elastic IP**
- EC2 -> Elastic IPs -> Allocate
- Associate with your instance

**Step 3: Point domain to Elastic IP**
- In your DNS provider, create an A record:
  - `livekit.yourdomain.com` -> `<Elastic IP>`

**Step 4: SSH into instance and run setup**
```bash
ssh -i your-key.pem ubuntu@<elastic-ip>
# Upload project files (git clone or scp)
# Run setup script
bash scripts/setup-ec2.sh
```

**Step 5: Deploy**
```bash
# Fill in .env and agent/.env.local with real credentials
# Fill in livekit.yaml with API keys and domain
# Fill in Caddyfile with domain
docker compose up -d
```

**Step 6: Configure SIP trunks (Task 12 commands)**

**Step 7: Test with a phone call**

---

## Execution Order Summary

| # | Task | Type |
|---|------|------|
| 1 | Project structure + copy files | Setup |
| 2 | LiveKit server config | Config |
| 3 | Caddy config | Config |
| 4 | SIP trunk configs | Config |
| 5 | Docker Compose | Config |
| 6 | Dockerfile + pyproject.toml | Config |
| 7 | .env template + .gitignore | Config |
| 8 | main.py (updated STT/TTS) | Code |
| 9 | assistant.py (updated lang switching) | Code |
| 10 | instruction.py (optimized prompt) | Code |
| 11 | EC2 setup + deploy scripts | Scripts |
| 12 | SIP trunk CLI config | Deploy |
| 13 | AWS EC2 launch + DNS | Deploy |

Tasks 1-11 can be done locally. Tasks 12-13 require AWS access.
