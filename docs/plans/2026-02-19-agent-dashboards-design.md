# SuperCX Agent Dashboards — Design Document

## Goal

Build a multi-tenant dashboard (truliv.supercx.co & maventech.supercx.co) to monitor voice AI agent performance — call metrics, call logs with full transcripts, and automated post-call QC scoring.

## Architecture

Single Next.js 15 app deployed on the same EC2 server (13.232.158.181) as a Docker container. Caddy reverse proxies both subdomains to `localhost:3000`. Middleware detects the subdomain and maps it to the correct MongoDB database and branding.

**Tech Stack:** Next.js 15 (App Router) + Tailwind CSS + shadcn/ui + MongoDB (Motor/native driver) + JWT auth

## Tenant Configuration

| Subdomain | MongoDB DB | Agent Name | Call Log Collection |
|-----------|-----------|------------|-------------------|
| `truliv` | `Truliv` | Truliv | `call_logs` |
| `maventech` | `maventech` | MavenTech | `call_logs` |

## Data Model — `call_logs` Collection (per agent DB)

```json
{
  "_id": "ObjectId",
  "user_id": "919876543210",
  "phone_number": "+919876543210",
  "call_type": "inbound | outbound",
  "started_at": "ISODate",
  "ended_at": "ISODate",
  "duration_seconds": 45,
  "status": "completed | missed | transferred",
  "transferred_to_human": false,

  "transcript": [
    { "role": "agent", "text": "Hello, this is Neha...", "timestamp": "ISODate" },
    { "role": "user", "text": "I want to book a bus...", "timestamp": "ISODate" }
  ],

  "summary": "Customer booked a bus from Chennai to Bangalore...",

  "outcome": {
    "booking_made": true,
    "pnr": "ABC123",
    "visit_scheduled": true
  },

  "qc": {
    "overall_score": 85,
    "greeting": { "score": 90, "notes": "Warm greeting, used customer name" },
    "empathy": { "score": 80, "notes": "Acknowledged frustration appropriately" },
    "script_adherence": { "score": 85, "notes": "Followed 7 of 8 booking steps" },
    "resolution": { "score": 90, "notes": "Successfully completed booking" },
    "call_handling": { "score": 80, "notes": "Minor delay in seat selection" },
    "language_quality": { "score": 85, "notes": "Clear English, appropriate tone" },
    "analyzed_at": "ISODate"
  }
}
```

## Agent-Side Changes

Both agents' `main.py` files need modifications in the `_cleanup()` function:

1. **Track call start time** — capture `datetime.now()` before `session.start()`
2. **Collect full transcript** — store every message with role and timestamp (not just last 8 truncated)
3. **Write to `call_logs` collection** — structured document with transcript, duration, outcome
4. **Run QC analysis** — call Gemini post-call with transcript + QC rubric, store scored result

Existing `context_data.callHistory` writes remain unchanged for backward compatibility.

### QC Rubric (Gemini Prompt)

Score each category 0-100 with brief notes:
1. **Greeting** — Warm and professional?
2. **Empathy** — Acknowledged customer concerns?
3. **Script Adherence** — Followed correct flow?
4. **Resolution** — Customer need resolved?
5. **Call Handling** — Efficient, no unnecessary delays?
6. **Language Quality** — Clear and appropriate communication?

Returns JSON with `overall_score` and per-category `{score, notes}`.

## Dashboard Pages

### 1. Dashboard (Home) — `/`
- Metric cards: Total Calls, AI Handled, Transferred to Human, Avg Duration, Avg QC Score, Conversion Rate
- Date range filter (today / week / month)
- Charts: Call volume (line, 30 days), QC trend (line), Outcome breakdown (pie)

### 2. Call Logs — `/calls`
- Paginated table: Time, Phone, Duration, Type, Status, QC Score, Outcome
- Sortable columns, date range filter, phone search
- Status badges (green/yellow/red)
- Click row → call detail

### 3. Call Detail — `/calls/[id]`
- Header: phone, date/time, duration, call type
- Transcript: chat-style view with timestamps
- Summary: AI-generated
- QC Scorecard: overall score + 6 sub-scores as progress bars with notes
- Outcome: booking PNR / visit scheduled

### 4. Login — `/login`
- Email/password form
- JWT in httpOnly cookie, 24h expiry
- Credentials in `users` collection per tenant DB

## Authentication

Simple email/password. JWT tokens stored in httpOnly cookies. `users` collection per agent DB:

```json
{
  "_id": "ObjectId",
  "email": "admin@supercx.co",
  "password_hash": "bcrypt hash",
  "name": "Admin",
  "created_at": "ISODate"
}
```

## Deployment

### Docker Compose Addition
```yaml
supercx-dashboard:
  build:
    context: ./dashboard
    dockerfile: Dockerfile
  restart: unless-stopped
  network_mode: host
  env_file:
    - ./dashboard/.env.local
  depends_on:
    - livekit-server
```

### Caddy Routing
```
truliv.supercx.co {
    reverse_proxy localhost:3000
}

maventech.supercx.co {
    reverse_proxy localhost:3000
}
```

### DNS
- `truliv.supercx.co` → A record → `13.232.158.181`
- `maventech.supercx.co` → A record → `13.232.158.181`

## Project Structure

```
LiveKit/
├── agent/              # Truliv agent (minor _cleanup changes)
├── MavenTech/          # MavenTech agent (minor _cleanup changes)
├── dashboard/          # NEW — Next.js app
│   ├── Dockerfile
│   ├── package.json
│   ├── next.config.js
│   ├── .env.local
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── login/page.tsx
│   │   │   ├── page.tsx            # Dashboard home
│   │   │   ├── calls/page.tsx      # Call logs
│   │   │   └── calls/[id]/page.tsx # Call detail
│   │   ├── components/
│   │   │   ├── Sidebar.tsx
│   │   │   ├── MetricCard.tsx
│   │   │   ├── CallsTable.tsx
│   │   │   ├── QCScorecard.tsx
│   │   │   └── TranscriptView.tsx
│   │   ├── lib/
│   │   │   ├── mongodb.ts
│   │   │   ├── auth.ts
│   │   │   └── tenants.ts
│   │   └── middleware.ts
│   └── ...
├── caddy/Caddyfile     # Updated with new subdomains
└── docker-compose.yml  # Updated with dashboard service
```

## UI

- **Tailwind CSS** + **shadcn/ui** for components
- **Recharts** for charts (line, pie)
- Responsive sidebar layout
- Agent-specific branding (logo, accent color) loaded from tenant config
