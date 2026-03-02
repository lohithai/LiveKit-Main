import json
import os
import asyncio
from datetime import datetime

from dotenv import load_dotenv

from livekit import agents, api, rtc
from livekit.agents import AgentServer, AgentSession, AutoSubscribe
from livekit.plugins import cartesia, google, sarvam, silero
from google.genai import types as genai_types

import agent_tools
from agent_tools import (
    load_properties_once,
    set_cached_context,
    flush_cached_context,
    clear_cached_context,
    get_cached_context,
)
from assistant import TrulivAssistant
from database import get_async_context_collection
from lead_sync import sync_user_to_leadsquared
from logger import logger

load_dotenv(".env.local")

AGENT_NAME = os.getenv("AGENT_NAME", "all don")
SIP_TRUNK_OUTBOUND_ID = os.getenv("SIP_TRUNK_OUTBOUND_ID", "")

# Cartesia voice ID — replace with your actual voice ID
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "REPLACE_WITH_YOUR_VOICE_ID")


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


def _build_greeting_instructions(user_contexts: dict) -> str:
    """Build dynamic greeting instructions based on returning customer context."""
    name = user_contexts.get("name", "")
    is_returning = name and name not in ["Voice User", "User", "Unknown", ""]
    bot_location = user_contexts.get("botLocationPreference", "")
    bot_sv_date = user_contexts.get("botSvDate", "")

    if is_returning and name:
        first_name = name.split()[0]
        if bot_sv_date:
            return (
                f"Greet the caller by name '{first_name}'. "
                f"They have a visit scheduled on {bot_sv_date}. "
                f"Ask if they completed the visit or need to reschedule."
            )
        if bot_location:
            return (
                f"Greet the caller by name '{first_name}'. "
                f"They were interested in properties near {bot_location}. "
                f"Ask if they visited or need more help."
            )
        return (
            f"Greet the returning caller by name '{first_name}'. "
            f"Ask how you can help them today."
        )

    return (
        "Greet the caller warmly in English. Introduce yourself as Priya from Truliv Coliving. "
        "Ask if they are looking for a PG in Chennai. Speak in English."
    )


# ── Main Agent Entry Point ──────────────────────────────────────────


@server.rtc_session(agent_name=AGENT_NAME)
async def truliv_agent(ctx: agents.JobContext):
    """Truliv voice agent — handles both inbound and outbound SIP calls."""

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

    # ── 3. For inbound, extract caller phone from SIP participant ───
    if not is_outbound:
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        participant = await ctx.wait_for_participant()
        phone_number = _extract_phone_from_participant(participant)
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
                "name": "Voice User",
            }
            await context_collection.update_one(
                {"_id": user_id},
                {"$set": {"context_data": user_contexts}},
                upsert=True,
            )
            logger.info(f"Created new user context for {user_id}")
    except Exception as e:
        logger.error(f"MongoDB context load failed: {e}")
        user_contexts = {"phoneNumber": phone_number or "", "name": "Voice User"}

    # Cache context in-memory for tool access during the call
    set_cached_context(voice_user_id, user_contexts)

    # ── 6. Load property data (cached globally after first call) ────
    properties_name = []
    try:
        await load_properties_once()
        if agent_tools.properties_data_cache:
            properties_name = [
                p.get("name", "")
                for p in agent_tools.properties_data_cache
                if p.get("name")
            ]
            logger.info(f"Loaded {len(properties_name)} property names")
    except Exception as e:
        logger.error(f"Failed to load properties: {e}")

    # ── 7. Create the assistant with user context ───────────────────
    assistant = TrulivAssistant(
        voice_user_id=voice_user_id,
        user_id=user_id,
        user_contexts=user_contexts,
        properties_name=properties_name,
    )

    # ── 8. Build STT (Sarvam saaras:v3 — streaming + interim) ───────
    stt = sarvam.STT(
        model="saaras:v3",
        language="en-IN",               # Start with English (switched on language change)
        mode="transcribe",              # Real-time transcription (no batch)
        flush_signal=True,              # Emit speech start/end events for turn-taking
        high_vad_sensitivity=True,      # Aggressive speech detection — catch speech faster
        sample_rate=16000,              # Match telephony audio
    )

    # ── 9. Create agent session (low-latency streaming pipeline) ──
    session = AgentSession(
        stt=stt,
        llm=google.LLM(
            model="gemini-2.5-flash",
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
        tts=cartesia.TTS(
            model="sonic-3",
            voice=CARTESIA_VOICE_ID,
            language="en",
        ),
        vad=silero.VAD.load(
            min_speech_duration=0.1,        # 100ms — catch short words without false triggers
            min_silence_duration=0.4,       # 400ms — natural speech pause detection
            prefix_padding_duration=0.3,    # 300ms — capture full word onset
            activation_threshold=0.35,      # Balanced sensitivity
            sample_rate=16000,
        ),
        min_endpointing_delay=0.5,          # 500ms — wait for natural pause before responding
        max_endpointing_delay=1.0,          # 1s cap — still responsive
        preemptive_generation=False,        # Wait for full transcript before generating
    )

    # ── 10. Register post-call cleanup ──────────────────────────────
    async def _cleanup():
        logger.info(f"Session closing for {user_id}")
        try:
            cached_ctx = get_cached_context(voice_user_id) or user_contexts

            summary = ""
            try:
                history = session.history
                if history and hasattr(history, "items"):
                    msgs = []
                    for item in history.items:
                        text = getattr(item, "text_content", None) or ""
                        if text:
                            role = getattr(item, "role", "unknown")
                            msgs.append(f"{role}: {text}")
                    if msgs:
                        summary = " | ".join(msgs[-8:])[:500]
            except Exception as e:
                logger.error(f"Summary generation failed: {e}")

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
            # Always clear cache even if cleanup fails
            clear_cached_context(voice_user_id)

    @session.on("close")
    def on_session_close():
        asyncio.create_task(_cleanup())
        # NOTE: Do NOT call clear_cached_context here — _cleanup() reads it first,
        # then flush_cached_context() clears it after writing to DB.

    # ── 11. Start the session ───────────────────────────────────────
    await session.start(
        room=ctx.room,
        agent=assistant,
    )

    # ── 12. Greeting (inbound only — outbound waits for recipient) ──
    if not is_outbound:
        greeting = _build_greeting_instructions(user_contexts)
        await session.generate_reply(instructions=greeting)

    # ── 13. Auto-disconnect on prolonged silence (safety net) ──────
    async def _silence_watchdog():
        """Disconnect call if no activity for 30 seconds (prevents zombie calls)."""
        MAX_SILENCE_SECS = 30
        try:
            while ctx.room.connection_state.name != "CONN_DISCONNECTED":
                await asyncio.sleep(MAX_SILENCE_SECS)
                # Check if room still has remote participants
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
