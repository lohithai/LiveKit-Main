"""
Truliv Luna Bengaluru — Voice Agent Entry Point
Sarvam STT + Sarvam TTS (Ritu) + Gemini 2.0 Flash LLM
"""

import json
import os
import asyncio
from datetime import datetime

from dotenv import load_dotenv

from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, AutoSubscribe, TurnHandlingOptions
from livekit.plugins import google, sarvam, silero

from agent_tools import (
    set_cached_context,
    flush_cached_context,
    clear_cached_context,
    get_cached_context,
)
from mongo_data import preload_all_data, get_property_names as get_warden_property_names
from assistant import TrulivAssistant
from call_recorder import start_recording, stop_recording, get_recording_url
from database import get_async_context_collection, get_async_call_logs_collection
from lead_sync import sync_user_to_leadsquared
from logger import logger
from webhook_sender import build_webhook_payload, send_webhook

load_dotenv(".env.local")

AGENT_NAME = os.getenv("AGENT_NAME", "truliv-telephony-agent")
SARVAM_VOICE_ID = os.getenv("SARVAM_VOICE_ID", "ritu")

server = AgentServer()


# ── Helpers ─────────────────────────────────────────────────────────


def _extract_phone(participant: rtc.RemoteParticipant) -> str:
    """Extract phone number from a SIP participant."""
    phone = participant.attributes.get("sip.phoneNumber", "") or participant.identity or ""
    return phone.lstrip("+").strip()


def _normalize_user_id(phone: str) -> str:
    """Normalize phone number to user_id format (91XXXXXXXXXX)."""
    clean = phone.lstrip("+").strip()
    if clean.startswith("91") and len(clean) > 10:
        return clean
    if len(clean) == 10 and clean.isdigit():
        return f"91{clean}"
    return clean


def _build_greeting(user_contexts: dict) -> tuple[str, bool]:
    """Build greeting text. Returns (text, use_llm)."""
    from datetime import date as date_type

    name = user_contexts.get("name", "")
    is_returning = name and name not in ["Voice User", "User", "Unknown", ""]
    bot_sv_date = user_contexts.get("botSvDate", "")

    if is_returning and name:
        first_name = name.split()[0]
        if bot_sv_date:
            try:
                visit_date = date_type.fromisoformat(bot_sv_date)
                today = date_type.today()
                if visit_date < today:
                    return (
                        f"Hey {first_name}! How did your visit go?",
                        False,
                    )
                else:
                    return (
                        f"Hey {first_name}! Your visit is on {bot_sv_date}. Any questions?",
                        False,
                    )
            except (ValueError, TypeError):
                pass
        return (
            f"Hey {first_name}! How can I help you today?",
            False,
        )

    return (
        "Hi! I'm Priya from Truliv. Are you looking for a PG in Bengaluru?",
        False,
    )


# ── QC Analysis ────────────────────────────────────────────────────


def _parse_qc_response(raw: str) -> dict:
    """Parse QC JSON response, stripping markdown fences if present."""
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()
    return json.loads(raw)


async def _run_qc_analysis(call_log_id, transcript: list, call_logs_coll):
    """Run QC analysis on a call transcript using Gemini."""
    transcript_text = "\n".join(
        f"{msg['role'].upper()}: {msg['text']}" for msg in transcript
    )

    qc_prompt = f"""Evaluate this voice AI call transcript for Truliv Luna Bengaluru. Score each category 0-100 with a brief note (max 15 words per note).

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
        import google.genai as genai
        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=qc_prompt,
        )
        raw = response.text.strip()
        logger.info(f"QC analysis via Gemini for call {call_log_id}")
    except Exception as e:
        logger.error(f"QC analysis failed: {e}")
        return

    try:
        qc_result = _parse_qc_response(raw)
        summary = qc_result.pop("summary", "")
        qc_result["analyzed_at"] = datetime.now()

        await call_logs_coll.update_one(
            {"_id": call_log_id},
            {"$set": {"qc": qc_result, "summary": summary}},
        )
        logger.info(f"QC saved for call {call_log_id}: score={qc_result.get('overall_score')}")
    except Exception as e:
        logger.error(f"QC response parsing failed: {e}")


# ── Post-call Cleanup ──────────────────────────────────────────────


async def _run_cleanup(
    session: AgentSession,
    user_id: str,
    voice_user_id: str,
    phone_number: str,
    user_contexts: dict,
    call_started_at: datetime,
    egress_id: str | None,
    room_name: str,
):
    """Post-call cleanup: save transcript, call log, QC, webhook, LeadSquared."""
    logger.info(f"Session closing for {user_id}")
    call_ended_at = datetime.now()
    duration_seconds = int((call_ended_at - call_started_at).total_seconds())

    try:
        cached_ctx = get_cached_context(voice_user_id) or user_contexts

        # 1. Collect transcript
        transcript = []
        summary_parts = []
        try:
            history = session.history
            if history and hasattr(history, "items"):
                for item in history.items:
                    text = getattr(item, "text_content", None) or ""
                    if text:
                        role = str(getattr(item, "role", "unknown"))
                        transcript.append({
                            "role": role,
                            "text": text,
                            "timestamp": datetime.now().isoformat(),
                        })
                        summary_parts.append(f"{role}: {text}")
        except Exception as e:
            logger.error(f"Transcript collection failed: {e}")

        summary = " | ".join(summary_parts[-8:])[:500] if summary_parts else ""

        # 2. Stop recording
        recording_info = None
        if egress_id:
            try:
                recording_info = await stop_recording(egress_id)
                if not recording_info:
                    recording_info = {
                        "url": get_recording_url(user_id),
                        "format": "mp3",
                        "size_bytes": 0,
                        "duration_seconds": duration_seconds,
                    }
                logger.info(f"Recording stopped for {user_id}")
            except Exception as e:
                logger.error(f"Recording stop error: {e}")

        # 3. Save call log
        call_outcome = {
            "visit_scheduled": bool(cached_ctx.get("botSvDate")),
            "transferred_to_human": False,
        }
        call_log_id = None

        if transcript:
            call_log = {
                "user_id": user_id,
                "phone_number": phone_number or "",
                "call_type": "inbound",
                "started_at": call_started_at,
                "ended_at": call_ended_at,
                "duration_seconds": duration_seconds,
                "status": "completed",
                "transferred_to_human": False,
                "transcript": transcript,
                "summary": "",
                "outcome": {"visit_scheduled": call_outcome["visit_scheduled"]},
                "recording_url": recording_info["url"] if recording_info else None,
                "qc": None,
            }
            try:
                call_logs_coll = await get_async_call_logs_collection()
                insert_result = await call_logs_coll.insert_one(call_log)
                call_log_id = insert_result.inserted_id
                logger.info(f"Saved call log {call_log_id} for {user_id}")

                # 4. QC analysis (non-blocking — fire and forget)
                asyncio.create_task(_run_qc_and_webhook(
                    call_log_id, transcript, call_logs_coll,
                    phone_number, user_id, cached_ctx,
                    call_started_at, call_ended_at, duration_seconds,
                    call_outcome, recording_info, room_name, summary,
                ))
            except Exception as e:
                logger.error(f"Failed to save call log: {e}")

        # 5. Save call summary to user context
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

        # 6. Flush context cache + LeadSquared sync
        await flush_cached_context(voice_user_id)

        try:
            await sync_user_to_leadsquared(user_id, cached_ctx)
        except Exception as e:
            logger.error(f"LeadSquared sync error: {e}")

    except Exception as e:
        logger.error(f"Session cleanup error for {user_id}: {e}")
    finally:
        clear_cached_context(voice_user_id)


async def _run_qc_and_webhook(
    call_log_id, transcript, call_logs_coll,
    phone_number, user_id, cached_ctx,
    call_started_at, call_ended_at, duration_seconds,
    call_outcome, recording_info, room_name, summary,
):
    """Run QC analysis then send webhook (background task)."""
    try:
        await _run_qc_analysis(call_log_id, transcript, call_logs_coll)
    except Exception as e:
        logger.error(f"QC analysis failed: {e}")

    # Send webhook with QC scores if available
    try:
        qc_scores = None
        saved_log = await call_logs_coll.find_one({"_id": call_log_id})
        if saved_log:
            qc_scores = saved_log.get("qc")
            summary = saved_log.get("summary") or summary

        webhook_payload = build_webhook_payload(
            call_log_id=str(call_log_id),
            phone_number=phone_number or "",
            user_id=user_id,
            user_contexts=cached_ctx,
            call_started_at=call_started_at,
            call_ended_at=call_ended_at,
            duration_seconds=duration_seconds,
            status="completed",
            transcript=transcript,
            summary=summary,
            outcome=call_outcome,
            recording_info=recording_info,
            room_name=room_name,
            qc_scores=qc_scores,
        )
        await send_webhook(webhook_payload)
    except Exception as e:
        logger.error(f"Webhook send error: {e}")


# ── Main Agent Entry Point ─────────────────────────────────────────


@server.rtc_session(agent_name=AGENT_NAME)
async def truliv_agent(ctx: agents.JobContext):
    """Truliv Luna Bengaluru voice agent — handles inbound SIP calls."""

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    participant = await ctx.wait_for_participant()

    phone_number = _extract_phone(participant)
    logger.info(f"Inbound call from: {phone_number}")

    user_id = _normalize_user_id(phone_number or "unknown")
    voice_user_id = user_id

    # ── Load user context + property data in parallel ─────────────
    async def _load_user_context():
        try:
            ctx_coll = await get_async_context_collection()
            user_doc = await ctx_coll.find_one({"_id": user_id})
            if user_doc:
                logger.info(f"Loaded context for {user_id}")
                return user_doc.get("context_data", {})
            else:
                ctx_data = {"phoneNumber": phone_number or "", "name": "Voice User"}
                await ctx_coll.update_one(
                    {"_id": user_id}, {"$set": {"context_data": ctx_data}}, upsert=True,
                )
                logger.info(f"Created context for {user_id}")
                return ctx_data
        except Exception as e:
            logger.error(f"MongoDB context load failed: {e}")
            return {"phoneNumber": phone_number or "", "name": "Voice User"}

    async def _load_property_data():
        try:
            await asyncio.wait_for(preload_all_data(), timeout=8.0)
            names = get_warden_property_names()
            logger.info("Loaded Truliv Luna property data")
            return names
        except asyncio.TimeoutError:
            logger.warning("Warden data load timed out — using defaults")
            return ["Truliv Luna"]
        except Exception as e:
            logger.error(f"Failed to load property data: {e}")
            return ["Truliv Luna"]

    user_contexts, properties_name = await asyncio.gather(
        _load_user_context(),
        _load_property_data(),
    )

    set_cached_context(voice_user_id, user_contexts)

    # ── Build assistant + session ─────────────────────────────────
    assistant = TrulivAssistant(
        voice_user_id=voice_user_id,
        user_id=user_id,
        user_contexts=user_contexts,
        properties_name=properties_name,
    )

    stt = sarvam.STT(
        model="saaras:v3",
        language="en-IN",
        mode="transcribe",
        prompt="Truliv Luna Bengaluru PG coliving property. Koramangala, HSR Layout, Electronic City, Whitefield, Indiranagar, Marathahalli, Bellandur, Sarjapur",
        flush_signal=True,
        high_vad_sensitivity=True,
        sample_rate=16000,
    )

    tts = sarvam.TTS(
        speaker=SARVAM_VOICE_ID,
        target_language_code="en-IN",
        model="bulbul:v3",
        loudness=1.5,
        pace=1.15,
        speech_sample_rate=22050,
        enable_preprocessing=False,
        min_buffer_size=30,
        max_chunk_length=50,
    )

    vad = silero.VAD.load(
        min_speech_duration=0.1,
        min_silence_duration=0.25,
        prefix_padding_duration=0.1,
        activation_threshold=0.4,
        sample_rate=16000,
    )

    llm = google.LLM(model="gemini-2.0-flash", temperature=0.7)

    turn_handling = TurnHandlingOptions(
        endpointing={
            "mode": "dynamic",
            "min_delay": 0.1,
            "max_delay": 0.35,
        },
        interruption={
            "enabled": True,
            "mode": "vad",
            "min_duration": 0.5,
            "min_words": 2,
        },
    )

    session = AgentSession(
        stt=stt, llm=llm, tts=tts, vad=vad,
        turn_handling=turn_handling,
        preemptive_generation=True,
    )

    call_started_at = datetime.now()
    room_name = ctx.room.name or ""
    logger.info(f"Session started: user_id={user_id} room={room_name}")

    # ── Recording (background) ────────────────────────────────────
    egress_id = None

    async def _start_recording():
        nonlocal egress_id
        try:
            egress_id = await start_recording(room_name, user_id)
        except Exception as e:
            logger.error(f"Recording start error: {e}")

    asyncio.create_task(_start_recording())

    # ── Post-call cleanup ─────────────────────────────────────────
    @session.on("close")
    def on_session_close():
        asyncio.create_task(_run_cleanup(
            session=session,
            user_id=user_id,
            voice_user_id=voice_user_id,
            phone_number=phone_number,
            user_contexts=user_contexts,
            call_started_at=call_started_at,
            egress_id=egress_id,
            room_name=room_name,
        ))

    # ── Start session + greeting ──────────────────────────────────
    await session.start(room=ctx.room, agent=assistant)

    # Delay so SIP audio channel is fully ready before first word
    await asyncio.sleep(0.5)
    greeting_text, use_llm = _build_greeting(user_contexts)
    if use_llm:
        await session.generate_reply(instructions=greeting_text)
    else:
        await session.say(greeting_text, allow_interruptions=False)

    # ── Silence watchdog ──────────────────────────────────────────
    async def _silence_watchdog():
        try:
            while ctx.room.remote_participants:
                await asyncio.sleep(30)
                if not ctx.room.remote_participants:
                    logger.info(f"Watchdog: No participants for {user_id}, shutting down")
                    ctx.shutdown()
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Watchdog error for {user_id}: {e}")

    asyncio.create_task(_silence_watchdog())


if __name__ == "__main__":
    agents.cli.run_app(server)
