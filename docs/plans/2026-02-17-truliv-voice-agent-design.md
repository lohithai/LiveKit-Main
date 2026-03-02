# Truliv Voice AI Agent - Self-Hosted LiveKit on AWS

## Overview

Build a multilingual voice AI agent for Truliv Coliving & PG that handles inbound/outbound calls to help customers find properties, answer queries, and book site visits. Self-hosted on AWS using LiveKit with custom SIP trunk integration.

## Architecture

### Deployment: Single EC2 + Docker Compose

```
[Caller] -> [Custom SIP Trunk] -> [EC2 ap-south-1]
                                      |
                                      v
                              +------------------+
                              | Docker Compose   |
                              |                  |
                              | - LiveKit Server |
                              | - LiveKit SIP    |
                              | - Redis          |
                              | - Caddy (TLS)    |
                              | - Truliv Agent   |
                              +------------------+
                                   |     |     |
                          Sarvam STT  Gemini  Cartesia TTS
                          (saarika:v2.5) (2.5 Flash) (sonic-3)
                                   |
                              MongoDB Atlas
                              (user contexts)
```

### EC2 Instance
- **Type:** c5.xlarge (4 vCPU, 8 GB RAM)
- **OS:** Ubuntu 22.04 LTS
- **Storage:** 30 GB gp3
- **Region:** ap-south-1 (Mumbai)

### Security Group Ports
| Port | Protocol | Purpose |
|------|----------|---------|
| 22 | TCP | SSH |
| 80 | TCP | HTTP (Caddy redirect) |
| 443 | TCP | HTTPS + TURN/TLS |
| 7880 | TCP | LiveKit API |
| 7881 | TCP | WebRTC TCP |
| 5060 | UDP/TCP | SIP signaling |
| 5349 | TCP | TURN TLS |
| 50000-60000 | UDP | WebRTC media (RTP) |

## AI Stack

| Component | Technology | Model/Version |
|-----------|-----------|---------------|
| STT | Sarvam AI | saarika:v2.5 |
| LLM | Google Gemini | 2.5 Flash |
| TTS | Cartesia | sonic-3 (custom voice ID) |
| VAD | Silero | Default config |

## SIP Configuration

### Inbound Trunk
Receives calls from custom SIP provider. Configured with allowed source IPs for security.

### Outbound Trunk
Makes outbound calls via SIP provider. Authenticated with SIP credentials.

### Dispatch Rule
Routes incoming SIP calls to individual rooms with prefix `call-`, where the Truliv agent auto-joins.

## Agent Design

### Conversation State Machine
GREET -> QUALIFY -> PRESENT -> SCHEDULE -> CLOSE

### Tools (12 function tools)
1. switch_language - Multilingual switching (9 Indian languages)
2. voice_update_user_profile - Save user preferences
3. voice_find_nearest_property - Location-based property search
4. voice_properties_according_to_budget - Budget-based search
5. voice_query_property_information - Property details lookup
6. voice_explore_more_properties - Show more options
7. voice_schedule_site_visit - Book site visit
8. voice_get_room_types - Room type info
9. voice_get_availability - Bed availability check
10. voice_get_all_room_availability - All properties availability
11. voice_zero_deposit - Zero deposit info (CirclePe)

### Language Support
Default: Hindi (Hinglish). Auto-detects and switches to: English, Tamil, Telugu, Kannada, Bengali, Gujarati, Malayalam, Marathi.

### Key Changes from Reference Code
1. **STT:** Deepgram nova-2 -> Sarvam saarika:v2.5
2. **TTS:** Sarvam bulbul:v3 -> Cartesia sonic-3
3. **Language switching:** Updated for Cartesia API (language parameter instead of target_language_code)
4. **System prompt:** Optimized for fewer tokens and faster processing

## Project Structure

```
LiveKit/
├── docker-compose.yml
├── caddy/Caddyfile
├── livekit/livekit.yaml
├── sip/
│   ├── inbound-trunk.json
│   ├── outbound-trunk.json
│   └── dispatch-rule.json
├── agent/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── .env.local
│   ├── main.py
│   ├── assistant.py
│   ├── agent_tools.py
│   ├── instruction.py
│   ├── database.py
│   ├── lead_sync.py
│   ├── logger.py
│   ├── sheets_client.py
│   ├── task_queue.py
│   └── helpers/warden_corn_api.py
└── scripts/
    ├── setup-ec2.sh
    └── deploy.sh
```

## External Dependencies
- MongoDB Atlas (existing)
- Google Sheets API (existing)
- Warden API (existing)
- LeadSquared CRM (existing)
- Google Maps Geocoding API (existing)
- Sarvam AI API (new - STT)
- Cartesia API (new - TTS)
- Google Gemini API (existing)

## Implementation Order
1. Set up project structure and copy unchanged files
2. Create Docker Compose + LiveKit server config
3. Configure SIP trunks and dispatch rules
4. Update agent code (main.py, assistant.py, instruction.py)
5. Create Dockerfile and requirements.txt
6. Write EC2 setup and deployment scripts
7. Test locally with Docker Compose
8. Deploy to AWS EC2
