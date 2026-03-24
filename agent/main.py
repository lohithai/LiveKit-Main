"""
Truliv Luna Bengaluru — Voice Agent Entry Point
Sarvam STT + Sarvam TTS + OpenRouter LLM (Gemini 2.5 Flash)
"""

import json
import os
import asyncio
from datetime import datetime

from dotenv import load_dotenv

from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, AutoSubscribe
from livekit.plugins import google, openai, sarvam, silero

import agent_tools
from agent_tools import (
    load_properties_once,
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
SARVAM_VOICE_ID = os.getenv("SARVAM_VOICE_ID", "shreya")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

server = AgentServer()


# ── Helpers ─────────────────────────────────────────────────────────


def _extract_phone_from_participant(participant: rtc.RemoteParticipant) -> str:
    """Extract phone number from a SIP participant's attributes or identity."""
    phone = participant.attributes.get("sip.phoneNumber", "")
    if not phone:
        phone = participant.identity or ""
    return phone.lstrip("+").strip()


def _normalize_user_id(phone: str) -> str:
    """Normalize phone number to user_id format (91XXXXXXXXXX)."""
    clean = phone.lstrip("+").strip()
    if clean.startswith("91") and len(clean) > 10:
        return clean
    if len(clean) == 10 and clean.isdigit():
        return f"91{clean}"
    return clean


def _build_greeting(user_contexts: dict):
    """Build greeting — returns (text, use_llm).
    For new callers: returns static text (skip LLM, go straight to TTS).
    For returning callers: returns LLM instruction (needs personalization).
    """
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
                        f"Hey {first_name}! So nice to hear from you again. "
                        f"How did your visit go? Did you like the place?",
                        False,
                    )
                else:
                    return (
                        f"Hey {first_name}! Great to hear from you. "
                        f"You have a visit coming up on {bot_sv_date}. "
                        f"Do you have any questions before your visit?",
                        False,
                    )
            except (ValueError, TypeError):
                pass

        return (
            f"Hey {first_name}! So nice to hear from you again. How can I help you today?",
            False,
        )

    # New caller — static greeting, skip LLM for zero latency
    return (
        "Hi there! I'm Priya from Truliv Coliving. "
        "Are you looking for a comfortable co living space in Bengaluru?",
        False,
    )


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
    """Run QC analysis on a call transcript using OpenRouter."""

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

    raw = None

    # Use direct Gemini for QC
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
        logger.info(f"QC analysis saved for call {call_log_id}: score={qc_result.get('overall_score')}")

    except Exception as e:
        logger.error(f"QC response parsing failed: {e}")


# ── Main Agent Entry Point (Inbound Only) ──────────────────────────


@server.rtc_session(agent_name=AGENT_NAME)
async def truliv_agent(ctx: agents.JobContext):
    """Truliv Luna Bengaluru voice agent — handles inbound SIP calls."""

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    participant = await ctx.wait_for_participant()

    phone_number = _extract_phone_from_participant(participant)
    logger.info(f"Inbound call from: {phone_number}")

    user_id = _normalize_user_id(phone_number or "unknown")
    voice_user_id = user_id

    # ── Run all setup tasks in parallel for fastest greeting ───────
    async def _load_user_context():
        try:
            context_collection = await get_async_context_collection()
            user_doc = await context_collection.find_one({"_id": user_id})
            if user_doc:
                logger.info(f"Loaded existing context for {user_id}")
                return user_doc.get("context_data", {})
            else:
                ctx_data = {"phoneNumber": phone_number or "", "name": "Voice User"}
                await context_collection.update_one(
                    {"_id": user_id}, {"$set": {"context_data": ctx_data}}, upsert=True,
                )
                logger.info(f"Created new user context for {user_id}")
                return ctx_data
        except Exception as e:
            logger.error(f"MongoDB context load failed: {e}")
            return {"phoneNumber": phone_number or "", "name": "Voice User"}

    async def _load_property_data():
        """Fetch Truliv Luna data from Warden API with 2s timeout."""
        try:
            await asyncio.wait_for(preload_all_data(), timeout=2.0)
            names = get_warden_property_names()
            logger.info(f"Pre-loaded Truliv Luna property data from Warden API")
            return names
        except asyncio.TimeoutError:
            logger.warning("Warden API timed out (2s) — using defaults")
            return ["Truliv Luna"]
        except Exception as e:
            logger.error(f"Failed to load Warden property data: {e}")
            return ["Truliv Luna"]

    # Run MongoDB context + Warden API data fetch in parallel
    user_contexts, properties_name = await asyncio.gather(
        _load_user_context(),
        _load_property_data(),
    )

    set_cached_context(voice_user_id, user_contexts)

    # ── Build assistant and session components ─────────────────────
    assistant = TrulivAssistant(
        voice_user_id=voice_user_id,
        user_id=user_id,
        user_contexts=user_contexts,
        properties_name=properties_name,
    )

    # ── STT: Sarvam saaras:v3 ─────────────────────────────────────
    stt_hints = ["Truliv", "Truliv Luna", "Truliv Coliving", "PG", "co living", "Bengaluru", "Bangalore"]
    stt_hints.extend([
        "Koramangala", "HSR Layout", "Electronic City", "Whitefield", "Indiranagar",
        "Marathahalli", "Bellandur", "Sarjapur", "JP Nagar", "Jayanagar",
        "BTM Layout", "Banashankari", "Rajajinagar", "Hebbal", "Yelahanka",
        "Silk Board", "Majestic", "MG Road", "Brigade Road", "Bannerghatta",
    ])
    stt_prompt = "Truliv Luna Bengaluru PG property. Keywords: " + ", ".join(stt_hints)

    stt = sarvam.STT(
        model="saaras:v3",
        language="en-IN",
        mode="transcribe",
        prompt=stt_prompt,
        flush_signal=True,
        high_vad_sensitivity=True,
        sample_rate=16000,
    )

    # ── TTS: Sarvam (ultra-low-latency config) ───────────────────
    tts = sarvam.TTS(
        speaker=SARVAM_VOICE_ID,
        target_language_code="en-IN",
        model="bulbul:v3",
        loudness=1.5,
        pace=1.1,
        speech_sample_rate=22050,
        enable_preprocessing=False,
        min_buffer_size=8,
        max_chunk_length=40,
    )

    # ── VAD: Silero (aggressive — snap to speech quickly) ─────────
    vad = silero.VAD.load(
        min_speech_duration=0.1,
        min_silence_duration=0.25,
        prefix_padding_duration=0.15,
        activation_threshold=0.35,
        sample_rate=16000,
    )

    # ── LLM: Gemini 2.0 Flash (fastest for voice) ────────────────
    llm = google.LLM(
        model="gemini-2.0-flash",
        temperature=0.6,
    )
    logger.info("Using Gemini 2.0 Flash (direct) as LLM")

    session = AgentSession(
        stt=stt, llm=llm, tts=tts, vad=vad,
        min_endpointing_delay=0.2,
        max_endpointing_delay=0.5,
        preemptive_generation=True,
    )

    call_started_at = datetime.now()
    room_name = ctx.room.name or ""
    logger.info(f"Session started: user_id={user_id} room={room_name}")

    # ── Start call recording via LiveKit Egress ────────────────────
    egress_id = None

    async def _start_call_recording():
        nonlocal egress_id
        try:
            egress_id = await start_recording(room_name, user_id)
        except Exception as e:
            logger.error(f"Recording start error: {e}")

    asyncio.create_task(_start_call_recording())

    # ── Post-call cleanup ──────────────────────────────────────────
    async def _cleanup():
        logger.info(f"Session closing for {user_id}")
        call_ended_at = datetime.now()
        duration_seconds = int((call_ended_at - call_started_at).total_seconds())

        try:
            cached_ctx = get_cached_context(voice_user_id) or user_contexts

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

            # ── Stop recording & get recording info ────────────────
            recording_info = None
            if egress_id:
                try:
                    recording_info = await stop_recording(egress_id)
                    if not recording_info:
                        recording_url = get_recording_url(user_id)
                        recording_info = {
                            "url": recording_url,
                            "format": "mp3",
                            "size_bytes": 0,
                            "duration_seconds": duration_seconds,
                        }
                    logger.info(f"Recording stopped for {user_id}: {recording_info}")
                except Exception as e:
                    logger.error(f"Recording stop error: {e}")

            call_status = "completed"
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
                    "status": call_status,
                    "transferred_to_human": False,
                    "transcript": transcript,
                    "summary": "",
                    "outcome": {
                        "visit_scheduled": call_outcome["visit_scheduled"],
                    },
                    "recording_url": recording_info["url"] if recording_info else None,
                    "qc": None,
                }
                try:
                    call_logs_coll = await get_async_call_logs_collection()
                    insert_result = await call_logs_coll.insert_one(call_log)
                    call_log_id = insert_result.inserted_id
                    logger.info(f"Saved call log {call_log_id} for {user_id}")

                    try:
                        await _run_qc_analysis(call_log_id, transcript, call_logs_coll)
                    except Exception as e:
                        logger.error(f"QC analysis failed: {e}")

                except Exception as e:
                    logger.error(f"Failed to save call log: {e}")

            # ── Send external webhook ──────────────────────────────
            if call_log_id and transcript:
                try:
                    qc_scores = None
                    try:
                        call_logs_coll = await get_async_call_logs_collection()
                        saved_log = await call_logs_coll.find_one({"_id": call_log_id})
                        if saved_log:
                            qc_scores = saved_log.get("qc")
                            summary = saved_log.get("summary") or summary
                    except Exception:
                        pass

                    webhook_payload = build_webhook_payload(
                        call_log_id=str(call_log_id),
                        phone_number=phone_number or "",
                        user_id=user_id,
                        user_contexts=cached_ctx,
                        call_started_at=call_started_at,
                        call_ended_at=call_ended_at,
                        duration_seconds=duration_seconds,
                        status=call_status,
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

    @session.on("close")
    def on_session_close():
        asyncio.create_task(_cleanup())

    # ── Start the session ──────────────────────────────────────────
    await session.start(
        room=ctx.room,
        agent=assistant,
    )

    # ── Greeting (direct TTS — no LLM round-trip for instant response) ──
    greeting_text, use_llm = _build_greeting(user_contexts)
    if use_llm:
        await session.generate_reply(instructions=greeting_text)
    else:
        await session.say(greeting_text)

    # ── Auto-disconnect on prolonged silence (safety net) ──────────
    async def _silence_watchdog():
        MAX_SILENCE_SECS = 30
        try:
            while str(ctx.room.connection_state) != "CONN_DISCONNECTED":
                await asyncio.sleep(MAX_SILENCE_SECS)
                if not ctx.room.remote_participants:
                    logger.info(f"Watchdog: No remote participants for {user_id}, shutting down")
                    ctx.shutdown()
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Watchdog error for {user_id}: {e}")

    asyncio.create_task(_silence_watchdog())


if __name__ == "__main__":
    agents.cli.run_app(server)
