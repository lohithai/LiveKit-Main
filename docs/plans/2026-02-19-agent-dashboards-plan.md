# SuperCX Agent Dashboards — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a multi-tenant Next.js dashboard for Truliv and MavenTech voice AI agents with call metrics, call logs, transcripts, and AI-powered post-call QC scoring.

**Architecture:** Single Next.js 15 app using App Router, deployed on the same EC2 via Docker. Caddy reverse-proxies `truliv.supercx.co` and `maventech.supercx.co` to `localhost:3000`. Middleware detects subdomain → selects MongoDB database + branding. Agents write structured call logs + Gemini QC results to `call_logs` collection post-call.

**Tech Stack:** Next.js 15, TypeScript, Tailwind CSS, shadcn/ui, Recharts, MongoDB (native driver), bcrypt + JWT (jose), Gemini 2.5 Flash (QC analysis)

---

## Phase 1: Agent-Side Changes — Call Logging & QC

### Task 1: Add call logging to MavenTech agent

**Files:**
- Modify: `MavenTech/main.py`
- Modify: `MavenTech/database.py`

**Step 1: Add `get_async_call_logs_collection` to `MavenTech/database.py`**

Add at the end of `MavenTech/database.py`:

```python
async def get_async_call_logs_collection():
    """Get the call_logs collection for dashboard."""
    return await get_async_collection("call_logs")
```

**Step 2: Modify `MavenTech/main.py` to capture full transcript and write call_logs**

The changes are in the `maventech_agent` function. We need to:
1. Record `call_started_at` before `session.start()`
2. Rewrite `_cleanup()` to collect full transcript, compute duration, write to `call_logs`, then run QC

Replace the section from `# ── 9. Register post-call cleanup` through the end of `_cleanup()` with:

```python
    # ── 9. Record call start time ──────────────────────────────────
    call_started_at = datetime.now()

    # ── 10. Register post-call cleanup ──────────────────────────────
    async def _cleanup():
        logger.info(f"Session closing for {user_id}")
        call_ended_at = datetime.now()
        duration_seconds = int((call_ended_at - call_started_at).total_seconds())

        try:
            cached_ctx = get_cached_context(voice_user_id) or user_contexts

            # ── Collect full transcript ──
            transcript = []
            summary_parts = []
            try:
                history = session.history
                if history and hasattr(history, "items"):
                    for item in history.items:
                        text = getattr(item, "text_content", None) or ""
                        if text:
                            role = getattr(item, "role", "unknown")
                            transcript.append({
                                "role": str(role),
                                "text": text,
                                "timestamp": datetime.now().isoformat(),
                            })
                            summary_parts.append(f"{role}: {text}")
            except Exception as e:
                logger.error(f"Transcript collection failed: {e}")

            summary = " | ".join(summary_parts[-8:])[:500] if summary_parts else ""

            # ── Write to call_logs collection ──
            if transcript:
                call_log = {
                    "user_id": user_id,
                    "phone_number": phone_number or "",
                    "call_type": "outbound" if is_outbound else "inbound",
                    "started_at": call_started_at,
                    "ended_at": call_ended_at,
                    "duration_seconds": duration_seconds,
                    "status": "completed",
                    "transferred_to_human": False,
                    "transcript": transcript,
                    "summary": "",
                    "outcome": {
                        "booking_made": bool(cached_ctx.get("lastPNR")),
                        "pnr": cached_ctx.get("lastPNR", ""),
                    },
                    "qc": None,
                }
                try:
                    call_logs_coll = await get_async_call_logs_collection()
                    insert_result = await call_logs_coll.insert_one(call_log)
                    call_log_id = insert_result.inserted_id
                    logger.info(f"Saved call log {call_log_id} for {user_id}")

                    # ── Run QC analysis via Gemini ──
                    try:
                        await _run_qc_analysis(call_log_id, transcript, call_logs_coll)
                    except Exception as e:
                        logger.error(f"QC analysis failed: {e}")

                except Exception as e:
                    logger.error(f"Failed to save call log: {e}")

            # ── Legacy: still update context_data.callHistory ──
            if summary:
                now = datetime.now()
                call_entry = {
                    "date": now.strftime("%Y-%m-%d"),
                    "time": now.strftime("%I:%M %p"),
                    "summary": summary,
                    "bookingMade": bool(cached_ctx.get("lastPNR")),
                }
                try:
                    ctx_coll = await get_async_context_collection()
                    await ctx_coll.update_one(
                        {"_id": user_id},
                        {
                            "$push": {"context_data.callHistory": call_entry},
                            "$set": {"context_data.lastCallSummary": summary},
                        },
                    )
                    logger.info(f"Saved call summary for {user_id}")
                except Exception as e:
                    logger.error(f"Failed to save call history: {e}")

            await flush_cached_context(voice_user_id)

        except Exception as e:
            logger.error(f"Session cleanup error for {user_id}: {e}")
        finally:
            clear_cached_context(voice_user_id)
```

**Step 3: Add the QC analysis function to `MavenTech/main.py`**

Add this function before the `maventech_agent` function (after imports):

```python
async def _run_qc_analysis(call_log_id, transcript: list, call_logs_coll):
    """Run Gemini-based QC analysis on a completed call transcript."""
    import google.genai as genai

    transcript_text = "\n".join(
        f"{msg['role'].upper()}: {msg['text']}" for msg in transcript
    )

    qc_prompt = f"""Evaluate this voice AI call transcript. Score each category 0-100 with a brief note (max 15 words per note).

TRANSCRIPT:
{transcript_text}

Return ONLY valid JSON (no markdown, no code fences):
{{
  "overall_score": <int 0-100>,
  "summary": "<2-3 sentence call summary>",
  "greeting": {{"score": <int>, "notes": "<brief note>"}},
  "empathy": {{"score": <int>, "notes": "<brief note>"}},
  "script_adherence": {{"score": <int>, "notes": "<brief note>"}},
  "resolution": {{"score": <int>, "notes": "<brief note>"}},
  "call_handling": {{"score": <int>, "notes": "<brief note>"}},
  "language_quality": {{"score": <int>, "notes": "<brief note>"}}
}}"""

    try:
        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=qc_prompt,
        )
        raw = response.text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

        qc_result = json.loads(raw)

        # Extract summary from QC and update call log
        summary = qc_result.pop("summary", "")
        qc_result["analyzed_at"] = datetime.now()

        await call_logs_coll.update_one(
            {"_id": call_log_id},
            {"$set": {"qc": qc_result, "summary": summary}},
        )
        logger.info(f"QC analysis saved for call {call_log_id}: score={qc_result.get('overall_score')}")

    except Exception as e:
        logger.error(f"QC Gemini call failed: {e}")
```

**Step 4: Add missing import at top of `MavenTech/main.py`**

Add to imports section:

```python
from database import get_async_context_collection, get_async_call_logs_collection
```

(Replace the existing `from database import get_async_context_collection` line)

**Step 5: Move `call_started_at` and renumber sections**

In `maventech_agent()`, the new section numbering becomes:
- Sections 1-8 stay the same
- New section 9: Record call start time (add `call_started_at = datetime.now()` right before the existing `session = AgentSession(...)` block — actually right after the AgentSession creation, before `_cleanup`)
- Section 10: Register post-call cleanup (was 9)
- Section 11: on("close") handler (stays)
- Section 12: Start session (was 10)
- Section 13: Greeting (was 11)
- Section 14: Watchdog (was 12)

**Step 6: Verify locally**

```bash
cd /Users/lohith/Desktop/LiveKit/MavenTech
python -c "import main; print('OK')"
```

Expected: `OK` (no import errors)

---

### Task 2: Add call logging to Truliv agent

**Files:**
- Modify: `agent/main.py`
- Modify: `agent/database.py`

**Step 1: Add `get_async_call_logs_collection` to `agent/database.py`**

Add at the end of `agent/database.py` (before the `close_mongodb_connection` function):

```python
async def get_async_call_logs_collection():
    """Get the call_logs collection for dashboard."""
    return await get_async_collection("call_logs")
```

**Step 2: Add the QC analysis function to `agent/main.py`**

Add this import at the top (alongside existing imports):

```python
from database import get_async_context_collection, get_async_call_logs_collection
```

(Replace the existing `from database import get_async_context_collection` line)

Add the same `_run_qc_analysis` function as MavenTech (before `truliv_agent`):

```python
async def _run_qc_analysis(call_log_id, transcript: list, call_logs_coll):
    """Run Gemini-based QC analysis on a completed call transcript."""
    import google.genai as genai

    transcript_text = "\n".join(
        f"{msg['role'].upper()}: {msg['text']}" for msg in transcript
    )

    qc_prompt = f"""Evaluate this voice AI call transcript. Score each category 0-100 with a brief note (max 15 words per note).

TRANSCRIPT:
{transcript_text}

Return ONLY valid JSON (no markdown, no code fences):
{{
  "overall_score": <int 0-100>,
  "summary": "<2-3 sentence call summary>",
  "greeting": {{"score": <int>, "notes": "<brief note>"}},
  "empathy": {{"score": <int>, "notes": "<brief note>"}},
  "script_adherence": {{"score": <int>, "notes": "<brief note>"}},
  "resolution": {{"score": <int>, "notes": "<brief note>"}},
  "call_handling": {{"score": <int>, "notes": "<brief note>"}},
  "language_quality": {{"score": <int>, "notes": "<brief note>"}}
}}"""

    try:
        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=qc_prompt,
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

        qc_result = json.loads(raw)
        summary = qc_result.pop("summary", "")
        qc_result["analyzed_at"] = datetime.now()

        await call_logs_coll.update_one(
            {"_id": call_log_id},
            {"$set": {"qc": qc_result, "summary": summary}},
        )
        logger.info(f"QC analysis saved for call {call_log_id}: score={qc_result.get('overall_score')}")

    except Exception as e:
        logger.error(f"QC Gemini call failed: {e}")
```

**Step 3: Modify `truliv_agent()` in `agent/main.py`**

Add `call_started_at = datetime.now()` before the session creation (before `session = AgentSession(...)`).

Replace the `_cleanup` function (section 10) with:

```python
    # ── 10. Record call start time ──────────────────────────────────
    call_started_at = datetime.now()

    # ── 11. Register post-call cleanup ──────────────────────────────
    async def _cleanup():
        logger.info(f"Session closing for {user_id}")
        call_ended_at = datetime.now()
        duration_seconds = int((call_ended_at - call_started_at).total_seconds())

        try:
            cached_ctx = get_cached_context(voice_user_id) or user_contexts

            # ── Collect full transcript ──
            transcript = []
            summary_parts = []
            try:
                history = session.history
                if history and hasattr(history, "items"):
                    for item in history.items:
                        text = getattr(item, "text_content", None) or ""
                        if text:
                            role = getattr(item, "role", "unknown")
                            transcript.append({
                                "role": str(role),
                                "text": text,
                                "timestamp": datetime.now().isoformat(),
                            })
                            summary_parts.append(f"{role}: {text}")
            except Exception as e:
                logger.error(f"Transcript collection failed: {e}")

            summary = " | ".join(summary_parts[-8:])[:500] if summary_parts else ""

            # ── Write to call_logs collection ──
            if transcript:
                call_log = {
                    "user_id": user_id,
                    "phone_number": phone_number or "",
                    "call_type": "outbound" if is_outbound else "inbound",
                    "started_at": call_started_at,
                    "ended_at": call_ended_at,
                    "duration_seconds": duration_seconds,
                    "status": "completed",
                    "transferred_to_human": False,
                    "transcript": transcript,
                    "summary": "",
                    "outcome": {
                        "visit_scheduled": bool(cached_ctx.get("botSvDate")),
                    },
                    "qc": None,
                }
                try:
                    call_logs_coll = await get_async_call_logs_collection()
                    insert_result = await call_logs_coll.insert_one(call_log)
                    call_log_id = insert_result.inserted_id
                    logger.info(f"Saved call log {call_log_id} for {user_id}")

                    # ── Run QC analysis via Gemini ──
                    try:
                        await _run_qc_analysis(call_log_id, transcript, call_logs_coll)
                    except Exception as e:
                        logger.error(f"QC analysis failed: {e}")

                except Exception as e:
                    logger.error(f"Failed to save call log: {e}")

            # ── Legacy: still update context_data.callHistory ──
            if summary:
                now = datetime.now()
                call_entry = {
                    "date": now.strftime("%Y-%m-%d"),
                    "time": now.strftime("%I:%M %p"),
                    "summary": summary,
                    "visitScheduled": bool(cached_ctx.get("botSvDate")),
                }
                try:
                    ctx_coll = await get_async_context_collection()
                    await ctx_coll.update_one(
                        {"_id": user_id},
                        {
                            "$push": {"context_data.callHistory": call_entry},
                            "$set": {"context_data.lastCallSummary": summary},
                        },
                    )
                    logger.info(f"Saved call summary for {user_id}")
                except Exception as e:
                    logger.error(f"Failed to save call history: {e}")

            await flush_cached_context(voice_user_id)

            try:
                await sync_user_to_leadsquared(user_id, cached_ctx)
            except Exception as e:
                logger.error(f"LeadSquared sync error: {e}")

        except Exception as e:
            logger.error(f"Session cleanup error for {user_id}: {e}")
        finally:
            clear_cached_context(voice_user_id)
```

**Step 4: Verify locally**

```bash
cd /Users/lohith/Desktop/LiveKit/agent
python -c "import main; print('OK')"
```

Expected: `OK`

---

## Phase 2: Dashboard — Project Setup

### Task 3: Scaffold Next.js project

**Files:**
- Create: `dashboard/` directory with full Next.js project

**Step 1: Create the Next.js project**

```bash
cd /Users/lohith/Desktop/LiveKit
npx create-next-app@latest dashboard --typescript --tailwind --eslint --app --src-dir --import-alias "@/*" --use-npm
```

When prompted, accept defaults (yes to all).

**Step 2: Install dependencies**

```bash
cd /Users/lohith/Desktop/LiveKit/dashboard
npm install mongodb bcryptjs jose recharts lucide-react date-fns clsx tailwind-merge class-variance-authority
npm install -D @types/bcryptjs
```

**Step 3: Initialize shadcn/ui**

```bash
cd /Users/lohith/Desktop/LiveKit/dashboard
npx shadcn@latest init -d
```

**Step 4: Add shadcn components**

```bash
cd /Users/lohith/Desktop/LiveKit/dashboard
npx shadcn@latest add button card input label table badge separator sheet select avatar dropdown-menu progress tabs
```

**Step 5: Verify build**

```bash
cd /Users/lohith/Desktop/LiveKit/dashboard
npm run build
```

Expected: Build succeeds

---

### Task 4: Create tenant configuration

**Files:**
- Create: `dashboard/src/lib/tenants.ts`

**Step 1: Create the tenant config file**

```typescript
// dashboard/src/lib/tenants.ts

export interface TenantConfig {
  id: string;
  name: string;
  dbName: string;
  callLogsCollection: string;
  usersCollection: string;
  accentColor: string;       // tailwind color class
  logoText: string;           // display name in sidebar
  outcomeLabel: string;       // "Booking Made" vs "Visit Scheduled"
  outcomeField: string;       // field in outcome object
}

const tenants: Record<string, TenantConfig> = {
  truliv: {
    id: "truliv",
    name: "Truliv",
    dbName: "Truliv",
    callLogsCollection: "call_logs",
    usersCollection: "dashboard_users",
    accentColor: "emerald",
    logoText: "Truliv",
    outcomeLabel: "Visit Scheduled",
    outcomeField: "visit_scheduled",
  },
  maventech: {
    id: "maventech",
    name: "MavenTech",
    dbName: "maventech",
    callLogsCollection: "call_logs",
    usersCollection: "dashboard_users",
    accentColor: "blue",
    logoText: "MavenTech",
    outcomeLabel: "Booking Made",
    outcomeField: "booking_made",
  },
};

export function getTenantFromHost(host: string): TenantConfig | null {
  const subdomain = host.split(".")[0]?.toLowerCase();
  return tenants[subdomain] || null;
}

export function getTenantById(id: string): TenantConfig | null {
  return tenants[id] || null;
}

export default tenants;
```

---

### Task 5: Create MongoDB connection library

**Files:**
- Create: `dashboard/src/lib/mongodb.ts`

**Step 1: Create the MongoDB connection file**

```typescript
// dashboard/src/lib/mongodb.ts

import { MongoClient, Db, Collection, ObjectId } from "mongodb";

const MONGO_URI = process.env.MONGO_URI!;

if (!MONGO_URI) {
  throw new Error("MONGO_URI environment variable is not set");
}

let client: MongoClient;
let clientPromise: Promise<MongoClient>;

declare global {
  var _mongoClientPromise: Promise<MongoClient> | undefined;
}

if (process.env.NODE_ENV === "development") {
  if (!global._mongoClientPromise) {
    client = new MongoClient(MONGO_URI);
    global._mongoClientPromise = client.connect();
  }
  clientPromise = global._mongoClientPromise;
} else {
  client = new MongoClient(MONGO_URI);
  clientPromise = client.connect();
}

export async function getDb(dbName: string): Promise<Db> {
  const client = await clientPromise;
  return client.db(dbName);
}

export async function getCollection(
  dbName: string,
  collectionName: string
): Promise<Collection> {
  const db = await getDb(dbName);
  return db.collection(collectionName);
}

export { ObjectId };
export default clientPromise;
```

---

### Task 6: Create auth library (JWT + password hashing)

**Files:**
- Create: `dashboard/src/lib/auth.ts`

**Step 1: Create the auth file**

```typescript
// dashboard/src/lib/auth.ts

import { SignJWT, jwtVerify } from "jose";
import bcrypt from "bcryptjs";
import { cookies } from "next/headers";
import { getCollection } from "./mongodb";
import { TenantConfig } from "./tenants";

const JWT_SECRET = new TextEncoder().encode(
  process.env.JWT_SECRET || "change-me-in-production-32chars!"
);
const COOKIE_NAME = "supercx_token";

export interface UserPayload {
  email: string;
  name: string;
  tenantId: string;
}

export async function hashPassword(password: string): Promise<string> {
  return bcrypt.hash(password, 12);
}

export async function verifyPassword(
  password: string,
  hash: string
): Promise<boolean> {
  return bcrypt.compare(password, hash);
}

export async function createToken(payload: UserPayload): Promise<string> {
  return new SignJWT({ ...payload })
    .setProtectedHeader({ alg: "HS256" })
    .setExpirationTime("24h")
    .setIssuedAt()
    .sign(JWT_SECRET);
}

export async function verifyToken(
  token: string
): Promise<UserPayload | null> {
  try {
    const { payload } = await jwtVerify(token, JWT_SECRET);
    return payload as unknown as UserPayload;
  } catch {
    return null;
  }
}

export async function getCurrentUser(
  tenant: TenantConfig
): Promise<UserPayload | null> {
  const cookieStore = await cookies();
  const token = cookieStore.get(COOKIE_NAME)?.value;
  if (!token) return null;

  const user = await verifyToken(token);
  if (!user || user.tenantId !== tenant.id) return null;
  return user;
}

export async function authenticateUser(
  tenant: TenantConfig,
  email: string,
  password: string
): Promise<UserPayload | null> {
  const collection = await getCollection(
    tenant.dbName,
    tenant.usersCollection
  );
  const user = await collection.findOne({ email: email.toLowerCase() });
  if (!user) return null;

  const valid = await verifyPassword(password, user.password_hash);
  if (!valid) return null;

  return {
    email: user.email,
    name: user.name,
    tenantId: tenant.id,
  };
}

export { COOKIE_NAME };
```

---

### Task 7: Create middleware for subdomain detection + auth

**Files:**
- Create: `dashboard/src/middleware.ts`

**Step 1: Create the middleware**

```typescript
// dashboard/src/middleware.ts

import { NextRequest, NextResponse } from "next/server";
import { jwtVerify } from "jose";

const JWT_SECRET = new TextEncoder().encode(
  process.env.JWT_SECRET || "change-me-in-production-32chars!"
);
const COOKIE_NAME = "supercx_token";

const VALID_TENANTS = ["truliv", "maventech"];

export async function middleware(request: NextRequest) {
  const host = request.headers.get("host") || "";
  const subdomain = host.split(".")[0]?.toLowerCase();

  // Validate tenant
  if (!VALID_TENANTS.includes(subdomain)) {
    // In development, default to "truliv" for localhost
    const tenantId =
      process.env.NODE_ENV === "development" ? "truliv" : null;
    if (!tenantId) {
      return NextResponse.json({ error: "Unknown tenant" }, { status: 404 });
    }
    // Set tenant header for dev
    const response = NextResponse.next();
    response.headers.set("x-tenant-id", tenantId);
    return handleAuth(request, response, tenantId);
  }

  const response = NextResponse.next();
  response.headers.set("x-tenant-id", subdomain);
  return handleAuth(request, response, subdomain);
}

async function handleAuth(
  request: NextRequest,
  response: NextResponse,
  tenantId: string
): Promise<NextResponse> {
  const { pathname } = request.nextUrl;

  // Public routes — no auth needed
  if (
    pathname === "/login" ||
    pathname.startsWith("/api/auth")
  ) {
    return response;
  }

  // Check JWT cookie
  const token = request.cookies.get(COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  try {
    const { payload } = await jwtVerify(token, JWT_SECRET);
    if ((payload as any).tenantId !== tenantId) {
      return NextResponse.redirect(new URL("/login", request.url));
    }
  } catch {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
```

**Step 2: Create a server-side tenant helper**

Create: `dashboard/src/lib/get-tenant.ts`

```typescript
// dashboard/src/lib/get-tenant.ts

import { headers } from "next/headers";
import { getTenantFromHost, TenantConfig } from "./tenants";

export async function getTenant(): Promise<TenantConfig> {
  const headerList = await headers();

  // Try middleware-injected header first
  const tenantId = headerList.get("x-tenant-id");
  if (tenantId) {
    const { getTenantById } = await import("./tenants");
    const tenant = getTenantById(tenantId);
    if (tenant) return tenant;
  }

  // Fallback: parse host header
  const host = headerList.get("host") || "truliv.supercx.co";
  const tenant = getTenantFromHost(host);
  if (!tenant) {
    throw new Error(`Unknown tenant for host: ${host}`);
  }
  return tenant;
}
```

---

### Task 8: Create environment file and Dockerfile

**Files:**
- Create: `dashboard/.env.local`
- Create: `dashboard/Dockerfile`

**Step 1: Create `.env.local`**

```
MONGO_URI=mongodb+srv://gogizmo:root@cluster.akp9e.mongodb.net/?retryWrites=true&w=majority&appName=Cluster
JWT_SECRET=supercx-dashboard-secret-change-me
```

**Step 2: Create Dockerfile**

```dockerfile
FROM node:20-alpine AS builder

WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production

COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000
CMD ["node", "server.js"]
```

**Step 3: Update `dashboard/next.config.ts` for standalone output**

Replace the content of `dashboard/next.config.ts`:

```typescript
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
};

export default nextConfig;
```

**Step 4: Verify build**

```bash
cd /Users/lohith/Desktop/LiveKit/dashboard
npm run build
```

Expected: Build succeeds

---

## Phase 3: Dashboard Pages

### Task 9: Create the sidebar layout

**Files:**
- Create: `dashboard/src/components/sidebar.tsx`
- Modify: `dashboard/src/app/layout.tsx`

**Step 1: Create the sidebar component**

```typescript
// dashboard/src/components/sidebar.tsx

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutDashboard, Phone, LogOut } from "lucide-react";
import { cn } from "@/lib/utils";

interface SidebarProps {
  tenantName: string;
  accentColor: string;
  userName: string;
}

const navItems = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/calls", label: "Call Logs", icon: Phone },
];

export function Sidebar({ tenantName, accentColor, userName }: SidebarProps) {
  const pathname = usePathname();

  const isActive = (href: string) => {
    if (href === "/") return pathname === "/";
    return pathname.startsWith(href);
  };

  const accentClasses: Record<string, string> = {
    emerald: "bg-emerald-600 text-white",
    blue: "bg-blue-600 text-white",
  };

  const activeClasses: Record<string, string> = {
    emerald: "bg-emerald-50 text-emerald-700 border-r-2 border-emerald-600",
    blue: "bg-blue-50 text-blue-700 border-r-2 border-blue-600",
  };

  return (
    <aside className="flex h-screen w-64 flex-col border-r bg-white">
      {/* Logo */}
      <div className={cn("flex h-16 items-center px-6", accentClasses[accentColor])}>
        <span className="text-xl font-bold">{tenantName}</span>
        <span className="ml-2 text-sm opacity-80">SuperCX</span>
      </div>

      {/* Navigation */}
      <nav className="flex-1 space-y-1 px-3 py-4">
        {navItems.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={cn(
              "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
              isActive(item.href)
                ? activeClasses[accentColor]
                : "text-gray-600 hover:bg-gray-50 hover:text-gray-900"
            )}
          >
            <item.icon className="h-5 w-5" />
            {item.label}
          </Link>
        ))}
      </nav>

      {/* User section */}
      <div className="border-t px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-sm text-gray-600 truncate">{userName}</span>
          <form action="/api/auth/logout" method="POST">
            <button
              type="submit"
              className="text-gray-400 hover:text-gray-600"
              title="Logout"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </form>
        </div>
      </div>
    </aside>
  );
}
```

**Step 2: Create the root layout**

Replace `dashboard/src/app/layout.tsx`:

```typescript
// dashboard/src/app/layout.tsx

import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { getTenant } from "@/lib/get-tenant";
import { getCurrentUser } from "@/lib/auth";
import { Sidebar } from "@/components/sidebar";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "SuperCX Dashboard",
  description: "Voice AI Agent Analytics Dashboard",
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  let showSidebar = false;
  let tenantName = "SuperCX";
  let accentColor = "blue";
  let userName = "";

  try {
    const tenant = await getTenant();
    const user = await getCurrentUser(tenant);
    if (user) {
      showSidebar = true;
      tenantName = tenant.logoText;
      accentColor = tenant.accentColor;
      userName = user.name || user.email;
    }
  } catch {
    // Not authenticated or no tenant — show children only (login page)
  }

  return (
    <html lang="en">
      <body className={inter.className}>
        {showSidebar ? (
          <div className="flex h-screen overflow-hidden">
            <Sidebar
              tenantName={tenantName}
              accentColor={accentColor}
              userName={userName}
            />
            <main className="flex-1 overflow-y-auto bg-gray-50 p-6">
              {children}
            </main>
          </div>
        ) : (
          <main>{children}</main>
        )}
      </body>
    </html>
  );
}
```

---

### Task 10: Create auth API routes and login page

**Files:**
- Create: `dashboard/src/app/api/auth/login/route.ts`
- Create: `dashboard/src/app/api/auth/logout/route.ts`
- Create: `dashboard/src/app/login/page.tsx`

**Step 1: Login API route**

```typescript
// dashboard/src/app/api/auth/login/route.ts

import { NextRequest, NextResponse } from "next/server";
import { authenticateUser, createToken, COOKIE_NAME } from "@/lib/auth";
import { getTenantFromHost, getTenantById } from "@/lib/tenants";

export async function POST(request: NextRequest) {
  const host = request.headers.get("host") || "";
  const subdomain = host.split(".")[0]?.toLowerCase();

  const tenant =
    getTenantById(subdomain) ||
    (process.env.NODE_ENV === "development"
      ? getTenantById("truliv")
      : null);

  if (!tenant) {
    return NextResponse.json({ error: "Unknown tenant" }, { status: 400 });
  }

  const body = await request.json();
  const { email, password } = body;

  if (!email || !password) {
    return NextResponse.json(
      { error: "Email and password required" },
      { status: 400 }
    );
  }

  const user = await authenticateUser(tenant, email, password);
  if (!user) {
    return NextResponse.json(
      { error: "Invalid credentials" },
      { status: 401 }
    );
  }

  const token = await createToken(user);

  const response = NextResponse.json({ success: true, user });
  response.cookies.set(COOKIE_NAME, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    maxAge: 60 * 60 * 24, // 24 hours
    path: "/",
  });

  return response;
}
```

**Step 2: Logout API route**

```typescript
// dashboard/src/app/api/auth/logout/route.ts

import { NextResponse } from "next/server";
import { COOKIE_NAME } from "@/lib/auth";

export async function POST() {
  const response = NextResponse.redirect(new URL("/login", "http://localhost:3000"));
  response.cookies.set(COOKIE_NAME, "", {
    httpOnly: true,
    maxAge: 0,
    path: "/",
  });
  return response;
}
```

**Step 3: Login page**

```typescript
// dashboard/src/app/login/page.tsx

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const router = useRouter();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });

      const data = await res.json();

      if (!res.ok) {
        setError(data.error || "Login failed");
        return;
      }

      router.push("/");
      router.refresh();
    } catch {
      setError("Network error. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          <CardTitle className="text-2xl">SuperCX Dashboard</CardTitle>
          <p className="text-sm text-gray-500">
            Sign in to your account
          </p>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && (
              <div className="rounded-md bg-red-50 p-3 text-sm text-red-600">
                {error}
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </div>
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? "Signing in..." : "Sign in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
```

---

### Task 11: Create Dashboard home page with metrics and charts

**Files:**
- Create: `dashboard/src/app/page.tsx`
- Create: `dashboard/src/components/metric-card.tsx`
- Create: `dashboard/src/components/charts.tsx`
- Create: `dashboard/src/lib/queries.ts`

**Step 1: Create the data query library**

```typescript
// dashboard/src/lib/queries.ts

import { getCollection, ObjectId } from "./mongodb";
import { TenantConfig } from "./tenants";

export interface DashboardMetrics {
  totalCalls: number;
  aiHandled: number;
  transferredToHuman: number;
  avgDuration: number;
  avgQcScore: number;
  conversionRate: number;
}

export interface DailyCallData {
  date: string;
  calls: number;
  avgQcScore: number;
}

export interface CallOutcome {
  name: string;
  value: number;
}

export async function getDashboardMetrics(
  tenant: TenantConfig,
  startDate: Date,
  endDate: Date
): Promise<DashboardMetrics> {
  const coll = await getCollection(tenant.dbName, tenant.callLogsCollection);

  const pipeline = [
    {
      $match: {
        started_at: { $gte: startDate, $lte: endDate },
      },
    },
    {
      $group: {
        _id: null,
        totalCalls: { $sum: 1 },
        aiHandled: {
          $sum: {
            $cond: [{ $eq: ["$transferred_to_human", false] }, 1, 0],
          },
        },
        transferredToHuman: {
          $sum: {
            $cond: [{ $eq: ["$transferred_to_human", true] }, 1, 0],
          },
        },
        avgDuration: { $avg: "$duration_seconds" },
        avgQcScore: { $avg: "$qc.overall_score" },
        conversions: {
          $sum: {
            $cond: [
              { $eq: [`$outcome.${tenant.outcomeField}`, true] },
              1,
              0,
            ],
          },
        },
      },
    },
  ];

  const result = await coll.aggregate(pipeline).toArray();
  const data = result[0] || {};

  return {
    totalCalls: data.totalCalls || 0,
    aiHandled: data.aiHandled || 0,
    transferredToHuman: data.transferredToHuman || 0,
    avgDuration: Math.round(data.avgDuration || 0),
    avgQcScore: Math.round(data.avgQcScore || 0),
    conversionRate: data.totalCalls
      ? Math.round((data.conversions / data.totalCalls) * 100)
      : 0,
  };
}

export async function getDailyCallData(
  tenant: TenantConfig,
  days: number = 30
): Promise<DailyCallData[]> {
  const coll = await getCollection(tenant.dbName, tenant.callLogsCollection);
  const startDate = new Date();
  startDate.setDate(startDate.getDate() - days);

  const pipeline = [
    { $match: { started_at: { $gte: startDate } } },
    {
      $group: {
        _id: {
          $dateToString: { format: "%Y-%m-%d", date: "$started_at" },
        },
        calls: { $sum: 1 },
        avgQcScore: { $avg: "$qc.overall_score" },
      },
    },
    { $sort: { _id: 1 } },
  ];

  const result = await coll.aggregate(pipeline).toArray();
  return result.map((r: any) => ({
    date: r._id,
    calls: r.calls,
    avgQcScore: Math.round(r.avgQcScore || 0),
  }));
}

export async function getCallOutcomes(
  tenant: TenantConfig,
  startDate: Date,
  endDate: Date
): Promise<CallOutcome[]> {
  const coll = await getCollection(tenant.dbName, tenant.callLogsCollection);

  const pipeline = [
    { $match: { started_at: { $gte: startDate, $lte: endDate } } },
    {
      $group: {
        _id: "$status",
        value: { $sum: 1 },
      },
    },
  ];

  const result = await coll.aggregate(pipeline).toArray();
  return result.map((r: any) => ({
    name: r._id || "unknown",
    value: r.value,
  }));
}

export interface CallLogEntry {
  _id: string;
  phone_number: string;
  call_type: string;
  started_at: string;
  duration_seconds: number;
  status: string;
  transferred_to_human: boolean;
  qc_score: number | null;
  outcome: Record<string, any>;
  summary: string;
}

export async function getCallLogs(
  tenant: TenantConfig,
  page: number = 1,
  pageSize: number = 20,
  search?: string
): Promise<{ calls: CallLogEntry[]; total: number }> {
  const coll = await getCollection(tenant.dbName, tenant.callLogsCollection);

  const filter: any = {};
  if (search) {
    filter.phone_number = { $regex: search, $options: "i" };
  }

  const total = await coll.countDocuments(filter);
  const calls = await coll
    .find(filter)
    .sort({ started_at: -1 })
    .skip((page - 1) * pageSize)
    .limit(pageSize)
    .project({
      phone_number: 1,
      call_type: 1,
      started_at: 1,
      duration_seconds: 1,
      status: 1,
      transferred_to_human: 1,
      "qc.overall_score": 1,
      outcome: 1,
      summary: 1,
    })
    .toArray();

  return {
    calls: calls.map((c: any) => ({
      _id: c._id.toString(),
      phone_number: c.phone_number,
      call_type: c.call_type,
      started_at: c.started_at?.toISOString?.() || c.started_at,
      duration_seconds: c.duration_seconds,
      status: c.status,
      transferred_to_human: c.transferred_to_human,
      qc_score: c.qc?.overall_score ?? null,
      outcome: c.outcome || {},
      summary: c.summary || "",
    })),
    total,
  };
}

export interface CallDetail {
  _id: string;
  user_id: string;
  phone_number: string;
  call_type: string;
  started_at: string;
  ended_at: string;
  duration_seconds: number;
  status: string;
  transferred_to_human: boolean;
  transcript: { role: string; text: string; timestamp: string }[];
  summary: string;
  outcome: Record<string, any>;
  qc: {
    overall_score: number;
    greeting: { score: number; notes: string };
    empathy: { score: number; notes: string };
    script_adherence: { score: number; notes: string };
    resolution: { score: number; notes: string };
    call_handling: { score: number; notes: string };
    language_quality: { score: number; notes: string };
    analyzed_at: string;
  } | null;
}

export async function getCallDetail(
  tenant: TenantConfig,
  callId: string
): Promise<CallDetail | null> {
  const coll = await getCollection(tenant.dbName, tenant.callLogsCollection);

  let doc;
  try {
    doc = await coll.findOne({ _id: new ObjectId(callId) });
  } catch {
    return null;
  }

  if (!doc) return null;

  return {
    _id: doc._id.toString(),
    user_id: doc.user_id,
    phone_number: doc.phone_number,
    call_type: doc.call_type,
    started_at: doc.started_at?.toISOString?.() || doc.started_at,
    ended_at: doc.ended_at?.toISOString?.() || doc.ended_at,
    duration_seconds: doc.duration_seconds,
    status: doc.status,
    transferred_to_human: doc.transferred_to_human,
    transcript: doc.transcript || [],
    summary: doc.summary || "",
    outcome: doc.outcome || {},
    qc: doc.qc || null,
  };
}
```

**Step 2: Create metric card component**

```typescript
// dashboard/src/components/metric-card.tsx

import { Card, CardContent } from "@/components/ui/card";
import { LucideIcon } from "lucide-react";

interface MetricCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  icon: LucideIcon;
}

export function MetricCard({ title, value, subtitle, icon: Icon }: MetricCardProps) {
  return (
    <Card>
      <CardContent className="flex items-center gap-4 p-6">
        <div className="rounded-lg bg-gray-100 p-3">
          <Icon className="h-6 w-6 text-gray-600" />
        </div>
        <div>
          <p className="text-sm text-gray-500">{title}</p>
          <p className="text-2xl font-bold">{value}</p>
          {subtitle && (
            <p className="text-xs text-gray-400">{subtitle}</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
```

**Step 3: Create charts component**

```typescript
// dashboard/src/components/charts.tsx

"use client";

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Legend,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

const PIE_COLORS = ["#10b981", "#f59e0b", "#ef4444", "#6b7280"];

interface CallVolumeChartProps {
  data: { date: string; calls: number; avgQcScore: number }[];
}

export function CallVolumeChart({ data }: CallVolumeChartProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Call Volume & QC Trend</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 12 }}
                tickFormatter={(v) => v.slice(5)}
              />
              <YAxis yAxisId="left" tick={{ fontSize: 12 }} />
              <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 12 }} domain={[0, 100]} />
              <Tooltip />
              <Line
                yAxisId="left"
                type="monotone"
                dataKey="calls"
                stroke="#3b82f6"
                strokeWidth={2}
                name="Calls"
              />
              <Line
                yAxisId="right"
                type="monotone"
                dataKey="avgQcScore"
                stroke="#10b981"
                strokeWidth={2}
                name="Avg QC Score"
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}

interface OutcomePieChartProps {
  data: { name: string; value: number }[];
}

export function OutcomePieChart({ data }: OutcomePieChartProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Call Outcomes</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={data}
                cx="50%"
                cy="50%"
                outerRadius={100}
                dataKey="value"
                label={({ name, percent }) =>
                  `${name} ${(percent * 100).toFixed(0)}%`
                }
              >
                {data.map((_, i) => (
                  <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                ))}
              </Pie>
              <Tooltip />
              <Legend />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}
```

**Step 4: Create dashboard home page**

```typescript
// dashboard/src/app/page.tsx

import {
  Phone,
  Bot,
  UserRound,
  Clock,
  Star,
  TrendingUp,
} from "lucide-react";
import { getTenant } from "@/lib/get-tenant";
import {
  getDashboardMetrics,
  getDailyCallData,
  getCallOutcomes,
} from "@/lib/queries";
import { MetricCard } from "@/components/metric-card";
import { CallVolumeChart, OutcomePieChart } from "@/components/charts";

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

export default async function DashboardPage() {
  const tenant = await getTenant();

  const now = new Date();
  const startOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);

  const [metrics, dailyData, outcomes] = await Promise.all([
    getDashboardMetrics(tenant, startOfMonth, now),
    getDailyCallData(tenant, 30),
    getCallOutcomes(tenant, startOfMonth, now),
  ]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{tenant.name} Dashboard</h1>
        <p className="text-sm text-gray-500">This month&apos;s overview</p>
      </div>

      {/* Metric Cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <MetricCard title="Total Calls" value={metrics.totalCalls} icon={Phone} />
        <MetricCard title="AI Handled" value={metrics.aiHandled} icon={Bot} />
        <MetricCard
          title="Transferred"
          value={metrics.transferredToHuman}
          icon={UserRound}
        />
        <MetricCard
          title="Avg Duration"
          value={formatDuration(metrics.avgDuration)}
          icon={Clock}
        />
        <MetricCard
          title="Avg QC Score"
          value={`${metrics.avgQcScore}/100`}
          icon={Star}
        />
        <MetricCard
          title={tenant.outcomeLabel}
          value={`${metrics.conversionRate}%`}
          icon={TrendingUp}
        />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <CallVolumeChart data={dailyData} />
        <OutcomePieChart data={outcomes} />
      </div>
    </div>
  );
}
```

---

### Task 12: Create Call Logs page

**Files:**
- Create: `dashboard/src/app/calls/page.tsx`
- Create: `dashboard/src/components/calls-table.tsx`

**Step 1: Create calls table component**

```typescript
// dashboard/src/components/calls-table.tsx

"use client";

import { useRouter } from "next/navigation";
import { format } from "date-fns";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";

interface CallLogEntry {
  _id: string;
  phone_number: string;
  call_type: string;
  started_at: string;
  duration_seconds: number;
  status: string;
  transferred_to_human: boolean;
  qc_score: number | null;
  outcome: Record<string, any>;
  summary: string;
}

interface CallsTableProps {
  calls: CallLogEntry[];
  outcomeLabel: string;
  outcomeField: string;
}

function statusBadge(status: string) {
  switch (status) {
    case "completed":
      return <Badge variant="default" className="bg-green-100 text-green-700">Completed</Badge>;
    case "transferred":
      return <Badge variant="default" className="bg-yellow-100 text-yellow-700">Transferred</Badge>;
    case "missed":
      return <Badge variant="destructive">Missed</Badge>;
    default:
      return <Badge variant="secondary">{status}</Badge>;
  }
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function qcBadge(score: number | null) {
  if (score === null) return <span className="text-gray-400">-</span>;
  const color =
    score >= 80 ? "bg-green-100 text-green-700" :
    score >= 60 ? "bg-yellow-100 text-yellow-700" :
    "bg-red-100 text-red-700";
  return <Badge variant="default" className={color}>{score}</Badge>;
}

export function CallsTable({ calls, outcomeLabel, outcomeField }: CallsTableProps) {
  const router = useRouter();

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Time</TableHead>
          <TableHead>Phone</TableHead>
          <TableHead>Duration</TableHead>
          <TableHead>Type</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>QC Score</TableHead>
          <TableHead>{outcomeLabel}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {calls.length === 0 ? (
          <TableRow>
            <TableCell colSpan={7} className="text-center text-gray-500 py-8">
              No call logs found
            </TableCell>
          </TableRow>
        ) : (
          calls.map((call) => (
            <TableRow
              key={call._id}
              className="cursor-pointer hover:bg-gray-50"
              onClick={() => router.push(`/calls/${call._id}`)}
            >
              <TableCell className="text-sm">
                {format(new Date(call.started_at), "MMM d, h:mm a")}
              </TableCell>
              <TableCell className="font-mono text-sm">
                {call.phone_number}
              </TableCell>
              <TableCell>{formatDuration(call.duration_seconds)}</TableCell>
              <TableCell className="capitalize">{call.call_type}</TableCell>
              <TableCell>{statusBadge(call.status)}</TableCell>
              <TableCell>{qcBadge(call.qc_score)}</TableCell>
              <TableCell>
                {call.outcome?.[outcomeField] ? (
                  <Badge className="bg-green-100 text-green-700">Yes</Badge>
                ) : (
                  <span className="text-gray-400">No</span>
                )}
              </TableCell>
            </TableRow>
          ))
        )}
      </TableBody>
    </Table>
  );
}
```

**Step 2: Create calls page**

```typescript
// dashboard/src/app/calls/page.tsx

import { getTenant } from "@/lib/get-tenant";
import { getCallLogs } from "@/lib/queries";
import { CallsTable } from "@/components/calls-table";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface CallsPageProps {
  searchParams: Promise<{ page?: string; search?: string }>;
}

export default async function CallsPage({ searchParams }: CallsPageProps) {
  const params = await searchParams;
  const tenant = await getTenant();
  const page = parseInt(params.page || "1", 10);
  const search = params.search || "";

  const { calls, total } = await getCallLogs(tenant, page, 20, search);
  const totalPages = Math.ceil(total / 20);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Call Logs</h1>
        <p className="text-sm text-gray-500">{total} total calls</p>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">All Calls</CardTitle>
            <form className="flex gap-2">
              <input
                name="search"
                type="text"
                placeholder="Search by phone..."
                defaultValue={search}
                className="rounded-md border px-3 py-1.5 text-sm"
              />
              <button
                type="submit"
                className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white"
              >
                Search
              </button>
            </form>
          </div>
        </CardHeader>
        <CardContent>
          <CallsTable
            calls={calls}
            outcomeLabel={tenant.outcomeLabel}
            outcomeField={tenant.outcomeField}
          />

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="mt-4 flex items-center justify-center gap-2">
              {page > 1 && (
                <a
                  href={`/calls?page=${page - 1}${search ? `&search=${search}` : ""}`}
                  className="rounded-md border px-3 py-1 text-sm hover:bg-gray-50"
                >
                  Previous
                </a>
              )}
              <span className="text-sm text-gray-500">
                Page {page} of {totalPages}
              </span>
              {page < totalPages && (
                <a
                  href={`/calls?page=${page + 1}${search ? `&search=${search}` : ""}`}
                  className="rounded-md border px-3 py-1 text-sm hover:bg-gray-50"
                >
                  Next
                </a>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
```

---

### Task 13: Create Call Detail page with transcript and QC scorecard

**Files:**
- Create: `dashboard/src/app/calls/[id]/page.tsx`
- Create: `dashboard/src/components/transcript-view.tsx`
- Create: `dashboard/src/components/qc-scorecard.tsx`

**Step 1: Create transcript view component**

```typescript
// dashboard/src/components/transcript-view.tsx

"use client";

import { cn } from "@/lib/utils";

interface Message {
  role: string;
  text: string;
  timestamp: string;
}

interface TranscriptViewProps {
  messages: Message[];
}

export function TranscriptView({ messages }: TranscriptViewProps) {
  return (
    <div className="space-y-3 max-h-[600px] overflow-y-auto">
      {messages.map((msg, i) => {
        const isAgent = msg.role === "assistant" || msg.role === "agent";
        return (
          <div
            key={i}
            className={cn(
              "flex",
              isAgent ? "justify-start" : "justify-end"
            )}
          >
            <div
              className={cn(
                "max-w-[75%] rounded-lg px-4 py-2 text-sm",
                isAgent
                  ? "bg-gray-100 text-gray-800"
                  : "bg-blue-600 text-white"
              )}
            >
              <p className="mb-1 text-xs font-medium opacity-70">
                {isAgent ? "Agent" : "Caller"}
              </p>
              <p>{msg.text}</p>
            </div>
          </div>
        );
      })}
      {messages.length === 0 && (
        <p className="text-center text-gray-400 py-8">No transcript available</p>
      )}
    </div>
  );
}
```

**Step 2: Create QC scorecard component**

```typescript
// dashboard/src/components/qc-scorecard.tsx

import { Progress } from "@/components/ui/progress";

interface QcCategory {
  score: number;
  notes: string;
}

interface QcData {
  overall_score: number;
  greeting: QcCategory;
  empathy: QcCategory;
  script_adherence: QcCategory;
  resolution: QcCategory;
  call_handling: QcCategory;
  language_quality: QcCategory;
}

interface QcScorecardProps {
  qc: QcData;
}

const CATEGORIES: { key: keyof Omit<QcData, "overall_score" | "analyzed_at">; label: string }[] = [
  { key: "greeting", label: "Greeting" },
  { key: "empathy", label: "Empathy" },
  { key: "script_adherence", label: "Script Adherence" },
  { key: "resolution", label: "Resolution" },
  { key: "call_handling", label: "Call Handling" },
  { key: "language_quality", label: "Language Quality" },
];

function scoreColor(score: number): string {
  if (score >= 80) return "text-green-600";
  if (score >= 60) return "text-yellow-600";
  return "text-red-600";
}

function progressColor(score: number): string {
  if (score >= 80) return "[&>div]:bg-green-500";
  if (score >= 60) return "[&>div]:bg-yellow-500";
  return "[&>div]:bg-red-500";
}

export function QcScorecard({ qc }: QcScorecardProps) {
  return (
    <div className="space-y-6">
      {/* Overall Score */}
      <div className="text-center">
        <p className="text-sm text-gray-500">Overall QC Score</p>
        <p className={`text-5xl font-bold ${scoreColor(qc.overall_score)}`}>
          {qc.overall_score}
        </p>
        <p className="text-sm text-gray-400">out of 100</p>
      </div>

      {/* Category Breakdown */}
      <div className="space-y-4">
        {CATEGORIES.map(({ key, label }) => {
          const cat = qc[key] as QcCategory;
          if (!cat) return null;
          return (
            <div key={key}>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-sm font-medium">{label}</span>
                <span className={`text-sm font-bold ${scoreColor(cat.score)}`}>
                  {cat.score}
                </span>
              </div>
              <Progress value={cat.score} className={`h-2 ${progressColor(cat.score)}`} />
              <p className="mt-1 text-xs text-gray-400">{cat.notes}</p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
```

**Step 3: Create call detail page**

```typescript
// dashboard/src/app/calls/[id]/page.tsx

import Link from "next/link";
import { format } from "date-fns";
import { ArrowLeft, Phone, Clock, Calendar } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getTenant } from "@/lib/get-tenant";
import { getCallDetail } from "@/lib/queries";
import { TranscriptView } from "@/components/transcript-view";
import { QcScorecard } from "@/components/qc-scorecard";
import { notFound } from "next/navigation";

interface CallDetailPageProps {
  params: Promise<{ id: string }>;
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

export default async function CallDetailPage({ params }: CallDetailPageProps) {
  const { id } = await params;
  const tenant = await getTenant();
  const call = await getCallDetail(tenant, id);

  if (!call) notFound();

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <Link
          href="/calls"
          className="rounded-lg border p-2 hover:bg-gray-50"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <div>
          <h1 className="text-2xl font-bold">Call Detail</h1>
          <p className="text-sm text-gray-500">
            {call.phone_number} &middot; {call.call_type}
          </p>
        </div>
      </div>

      {/* Meta cards */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Calendar className="h-5 w-5 text-gray-400" />
            <div>
              <p className="text-xs text-gray-500">Date</p>
              <p className="text-sm font-medium">
                {format(new Date(call.started_at), "MMM d, yyyy")}
              </p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Clock className="h-5 w-5 text-gray-400" />
            <div>
              <p className="text-xs text-gray-500">Duration</p>
              <p className="text-sm font-medium">
                {formatDuration(call.duration_seconds)}
              </p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Phone className="h-5 w-5 text-gray-400" />
            <div>
              <p className="text-xs text-gray-500">Status</p>
              <Badge
                className={
                  call.status === "completed"
                    ? "bg-green-100 text-green-700"
                    : "bg-yellow-100 text-yellow-700"
                }
              >
                {call.status}
              </Badge>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <div>
              <p className="text-xs text-gray-500">{tenant.outcomeLabel}</p>
              <p className="text-sm font-medium">
                {call.outcome?.[tenant.outcomeField] ? "Yes" : "No"}
              </p>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Summary */}
      {call.summary && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">AI Summary</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-gray-700">{call.summary}</p>
          </CardContent>
        </Card>
      )}

      {/* Transcript + QC side by side */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Transcript</CardTitle>
          </CardHeader>
          <CardContent>
            <TranscriptView messages={call.transcript} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">QC Scorecard</CardTitle>
          </CardHeader>
          <CardContent>
            {call.qc ? (
              <QcScorecard qc={call.qc} />
            ) : (
              <p className="py-8 text-center text-gray-400">
                QC analysis not available
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
```

---

## Phase 4: Deployment

### Task 14: Update Caddy and Docker Compose

**Files:**
- Modify: `caddy/Caddyfile`
- Modify: `docker-compose.yml`

**Step 1: Update Caddyfile**

Replace `caddy/Caddyfile` with:

```
# Caddy reverse proxy for LiveKit + Dashboards

livekit.supercx.co {
    reverse_proxy 127.0.0.1:7880
}

truliv.supercx.co {
    reverse_proxy 127.0.0.1:3000
}

maventech.supercx.co {
    reverse_proxy 127.0.0.1:3000
}
```

**Step 2: Add dashboard service to `docker-compose.yml`**

Add before the `volumes:` section:

```yaml
  # ── SuperCX Dashboard (Next.js) ──────────────────────────────────
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

---

### Task 15: Create seed script for admin users

**Files:**
- Create: `dashboard/scripts/seed-users.ts`

**Step 1: Create the seed script**

```typescript
// dashboard/scripts/seed-users.ts
// Run: npx tsx scripts/seed-users.ts

import { MongoClient } from "mongodb";
import bcrypt from "bcryptjs";

const MONGO_URI = process.env.MONGO_URI || "mongodb+srv://gogizmo:root@cluster.akp9e.mongodb.net/?retryWrites=true&w=majority&appName=Cluster";

const USERS = [
  {
    db: "Truliv",
    collection: "dashboard_users",
    email: "admin@truliv.com",
    password: "admin123",
    name: "Truliv Admin",
  },
  {
    db: "maventech",
    collection: "dashboard_users",
    email: "admin@maventech.com",
    password: "admin123",
    name: "MavenTech Admin",
  },
];

async function seed() {
  const client = new MongoClient(MONGO_URI);
  await client.connect();
  console.log("Connected to MongoDB");

  for (const user of USERS) {
    const db = client.db(user.db);
    const coll = db.collection(user.collection);

    const existing = await coll.findOne({ email: user.email });
    if (existing) {
      console.log(`User ${user.email} already exists in ${user.db}, skipping`);
      continue;
    }

    const hash = await bcrypt.hash(user.password, 12);
    await coll.insertOne({
      email: user.email,
      password_hash: hash,
      name: user.name,
      created_at: new Date(),
    });
    console.log(`Created user ${user.email} in ${user.db}`);
  }

  await client.close();
  console.log("Done!");
}

seed().catch(console.error);
```

**Step 2: Add tsx as dev dependency**

```bash
cd /Users/lohith/Desktop/LiveKit/dashboard
npm install -D tsx
```

**Step 3: Run the seed script**

```bash
cd /Users/lohith/Desktop/LiveKit/dashboard
npx tsx scripts/seed-users.ts
```

Expected: `Created user admin@truliv.com in Truliv` and `Created user admin@maventech.com in maventech`

---

### Task 16: Deploy to server

**Step 1: Add DNS records**

In your DNS provider (e.g., Cloudflare, Route 53), add two A records:
- `truliv.supercx.co` → `13.232.158.181`
- `maventech.supercx.co` → `13.232.158.181`

**Step 2: Upload files to server**

```bash
# Upload dashboard
scp -i your-key.pem -r /Users/lohith/Desktop/LiveKit/dashboard/* ubuntu@13.232.158.181:~/LiveKit/dashboard/

# Upload updated agent files
scp -i your-key.pem /Users/lohith/Desktop/LiveKit/MavenTech/main.py ubuntu@13.232.158.181:~/LiveKit/MavenTech/main.py
scp -i your-key.pem /Users/lohith/Desktop/LiveKit/MavenTech/database.py ubuntu@13.232.158.181:~/LiveKit/MavenTech/database.py
scp -i your-key.pem /Users/lohith/Desktop/LiveKit/agent/main.py ubuntu@13.232.158.181:~/LiveKit/agent/main.py
scp -i your-key.pem /Users/lohith/Desktop/LiveKit/agent/database.py ubuntu@13.232.158.181:~/LiveKit/agent/database.py

# Upload updated configs
scp -i your-key.pem /Users/lohith/Desktop/LiveKit/caddy/Caddyfile ubuntu@13.232.158.181:~/LiveKit/caddy/Caddyfile
scp -i your-key.pem /Users/lohith/Desktop/LiveKit/docker-compose.yml ubuntu@13.232.158.181:~/LiveKit/docker-compose.yml
```

**Step 3: Build and deploy on server**

```bash
ssh -i your-key.pem ubuntu@13.232.158.181
cd ~/LiveKit

# Create dashboard dir if needed
mkdir -p dashboard

# Build and start everything
docker compose build supercx-dashboard maventech-agent truliv-agent
docker compose up -d

# Verify
docker compose logs -f supercx-dashboard --tail=20
```

**Step 4: Verify dashboards**

- Open `https://truliv.supercx.co` → should show login page
- Open `https://maventech.supercx.co` → should show login page
- Login with seeded credentials
- Make a test call to verify call logs appear
