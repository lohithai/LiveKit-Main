"""
Marhaba Haji Voice AI Agent — LiveKit Entry Point
Maryam — Hajj, Umrah & Halal Holiday Travel Consultant
Sarvam STT + Gemini 2.5 Flash / OpenAI GPT-4o LLM + Cartesia TTS + Silero VAD
"""

import json
import os
import asyncio
from datetime import datetime

from dotenv import load_dotenv

from livekit import agents, api, rtc
from livekit.agents import AgentServer, AgentSession, AutoSubscribe
from livekit.plugins import cartesia, google, openai, sarvam, silero
from google.genai import types as genai_types

from assistant import MarhabaHajiAssistant
from instruction import build_greeting_instruction
from agent_tools import (
    set_cached_context,
    flush_cached_context,
    clear_cached_context,
    get_cached_context,
)
from database import get_async_context_collection, get_async_call_logs_collection
from logger import logger

load_dotenv(".env.local")

AGENT_NAME = os.getenv("AGENT_NAME", "marhaba-haji")
SIP_TRUNK_OUTBOUND_ID = os.getenv("SIP_TRUNK_OUTBOUND_ID", "")
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "")

server = AgentServer(port=8084)


# ── LLM Health Check & Fallback ────────────────────────────────────


async def _check_gemini_health() -> bool:
    """Quick probe to check if Gemini API is responsive (not rate-limited)."""
    import google.genai as genai

    try:
        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents="Say OK",
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=5,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            ),
            timeout=5.0,
        )
        return bool(response and response.text)
    except Exception as e:
        logger.warning(f"Gemini health check failed: {e}")
        return False


def _create_llm(use_gemini: bool):
    """Create the appropriate LLM instance based on health check."""
    if use_gemini:
        logger.info("Using Gemini 2.5 Flash as LLM")
        return google.LLM(
            model="gemini-2.5-flash",
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        )
    else:
        logger.info("Using OpenAI GPT-4o as fallback LLM")
        return openai.LLM(model="gpt-4o")


# ── Helpers ─────────────────────────────────────────────────────────


def _extract_phone_from_participant(participant: rtc.RemoteParticipant) -> str:
    phone = participant.attributes.get("sip.phoneNumber", "")
    if not phone:
        phone = participant.identity or ""
    return phone.lstrip("+").strip()


def _normalize_user_id(phone: str) -> str:
    clean = phone.lstrip("+").strip()
    if clean.startswith("91") and len(clean) > 10:
        return clean
    if len(clean) == 10 and clean.isdigit():
        return f"91{clean}"
    return clean


async def _run_qc_analysis(call_log_id, transcript: list, call_logs_coll):
    """Run Gemini-based QC analysis on a completed call transcript."""
    import google.genai as genai

    transcript_text = "\n".join(
        f"{msg['role'].upper()}: {msg['text']}" for msg in transcript
    )

    qc_prompt = f"""Evaluate this voice AI call transcript for a Hajj/Umrah travel agency. Score each category 0-100 with a brief note (max 15 words per note).

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


# ── Main Agent Entry Point ──────────────────────────────────────────


@server.rtc_session(agent_name=AGENT_NAME)
async def marhaba_haji_agent(ctx: agents.JobContext):
    """Marhaba Haji voice agent — handles both inbound and outbound SIP calls."""

    phone_number = None
    is_outbound = False

    # ── 1. Determine call type (outbound vs inbound) ────────────────
    if ctx.job.metadata:
        try:
            metadata = json.loads(ctx.job.metadata)
            phone_number = metadata.get("phone_number")
            if phone_number:
                is_outbound = True
        except (json.JSONDecodeError, TypeError):
            pass

    # ── 2. Handle outbound SIP dial ─────────────────────────────────
    if is_outbound and phone_number:
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=SIP_TRUNK_OUTBOUND_ID,
                    sip_call_to=phone_number,
                    participant_identity=phone_number,
                    wait_until_answered=True,
                    play_dialtone=False,
                )
            )
            logger.info(f"Outbound call to {phone_number} answered")
        except api.TwirpError as e:
            logger.error(
                f"SIP dial error: {e.message}, "
                f"status: {e.metadata.get('sip_status_code')} "
                f"{e.metadata.get('sip_status')}"
            )
            ctx.shutdown()
            return

    # ── 3. For inbound/Greeter-loopback, extract caller phone ───────
    if not is_outbound:
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        participant = await ctx.wait_for_participant()
        phone_number = _extract_phone_from_participant(participant)

        # Detect if this is a Greeter loopback from an outbound call
        call_direction = participant.attributes.get("sip.callDirection", "")
        if call_direction == "outbound":
            is_outbound = True
            logger.info(f"Outbound call (via Greeter loopback) from: {phone_number}")
        else:
            logger.info(f"Inbound call from: {phone_number}")

    # ── 4. Derive user IDs ──────────────────────────────────────────
    user_id = _normalize_user_id(phone_number or "unknown")
    voice_user_id = user_id
    logger.info(f"Session started: user_id={user_id}")

    # ── 5. Load user context from MongoDB ───────────────────────────
    user_contexts = {}
    try:
        context_collection = await get_async_context_collection()
        user_doc = await context_collection.find_one({"_id": user_id})

        if user_doc:
            user_contexts = user_doc.get("context_data", {})
            logger.info(f"Loaded existing context for {user_id}")
        else:
            user_contexts = {
                "phoneNumber": phone_number or "",
                "name": "",
            }
            await context_collection.update_one(
                {"_id": user_id},
                {"$set": {"context_data": user_contexts}},
                upsert=True,
            )
            logger.info(f"Created new user context for {user_id}")
    except Exception as e:
        logger.error(f"MongoDB context load failed: {e}")
        user_contexts = {"phoneNumber": phone_number or "", "name": ""}

    set_cached_context(voice_user_id, user_contexts)

    # ── 6. Create the assistant ─────────────────────────────────────
    assistant = MarhabaHajiAssistant(
        voice_user_id=voice_user_id,
        user_id=user_id,
        user_contexts=user_contexts,
    )

    # ── 7. Build STT (Sarvam saaras:v3) ─────────────────────────────
    stt_prompt = "Marhaba Haji, Umrah, Hajj, Makkah, Madinah, Ziyarat, Mutawwif, Halal, JazakAllah, InshAllah, Assalamu Alaikum"

    stt = sarvam.STT(
        api_key=os.getenv("SARVAM_API_KEY") or os.getenv("SARVAMAI_API_KEY"),
        model="saaras:v3",
        language="en-IN",
        mode="transcribe",
        prompt=stt_prompt,
        flush_signal=True,
        high_vad_sensitivity=True,
        sample_rate=16000,
    )

    # ── 8. Check LLM health and create agent session ────────────────
    gemini_ok = await _check_gemini_health()
    llm = _create_llm(use_gemini=gemini_ok)

    session = AgentSession(
        stt=stt,
        llm=llm,
        tts=cartesia.TTS(
            model="sonic-3",
            voice=CARTESIA_VOICE_ID,
            language="en",
            speed=0.9,
            emotion=["Calm", "Affectionate"],
        ),
        vad=silero.VAD.load(
            min_speech_duration=0.25,
            min_silence_duration=0.4,
            prefix_padding_duration=0.3,
            activation_threshold=0.55,
            sample_rate=16000,
        ),
        min_endpointing_delay=0.5,
        max_endpointing_delay=0.8,
        preemptive_generation=True,
    )

    # ── 9. Register post-call cleanup ───────────────────────────────
    call_started_at = datetime.now()

    async def _cleanup():
        logger.info(f"Session closing for {user_id}")
        call_ended_at = datetime.now()
        duration_seconds = int((call_ended_at - call_started_at).total_seconds())

        try:
            cached_ctx = get_cached_context(voice_user_id) or user_contexts

            # Collect full transcript
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

            # Write to call_logs collection
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
                        "callback_scheduled": bool(cached_ctx.get("callbackDate")),
                        "callback_date": cached_ctx.get("callbackDate", ""),
                        "service_interest": cached_ctx.get("serviceInterest", ""),
                        "destination": cached_ctx.get("destination", ""),
                    },
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

            # Update context with call history
            if summary:
                now = datetime.now()
                call_entry = {
                    "date": now.strftime("%Y-%m-%d"),
                    "time": now.strftime("%I:%M %p"),
                    "summary": summary,
                    "callbackScheduled": bool(cached_ctx.get("callbackDate")),
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

    @session.on("close")
    def on_session_close():
        asyncio.create_task(_cleanup())

    # ── 10. Start the session ───────────────────────────────────────
    await session.start(
        room=ctx.room,
        agent=assistant,
    )

    # ── 11. Greeting ────────────────────────────────────────────────
    greeting = build_greeting_instruction(user_contexts)
    await session.generate_reply(instructions=greeting)

    # ── 12. Auto-disconnect on prolonged silence (safety net) ───────
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
