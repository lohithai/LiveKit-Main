"""
assistant.py — Marhaba Haji Voice AI Agent (LiveKit)
Maryam — Travel consultant for Hajj, Umrah & Halal holidays
"""

import asyncio
import json

from livekit import api
from livekit.agents import Agent, RunContext, function_tool, get_job_context

from instruction import build_system_prompt
from agent_tools import (
    update_cached_context,
    find_packages,
    schedule_callback,
)
from logger import logger


class MarhabaHajiAssistant(Agent):
    """Marhaba Haji Voice AI Agent for LiveKit."""

    LANGUAGE_MAP = {
        "en": {"lang_code": "en", "stt_code": "en-IN", "name": "English"},
        "hi": {"lang_code": "hi", "stt_code": "hi-IN", "name": "Hindi"},
        "ar": {"lang_code": "ar", "stt_code": "ar",    "name": "Arabic"},
        "ur": {"lang_code": "ur", "stt_code": "ur",    "name": "Urdu"},
        "ta": {"lang_code": "ta", "stt_code": "ta-IN", "name": "Tamil"},
        "te": {"lang_code": "te", "stt_code": "te-IN", "name": "Telugu"},
        "kn": {"lang_code": "kn", "stt_code": "kn-IN", "name": "Kannada"},
        "bn": {"lang_code": "bn", "stt_code": "bn-IN", "name": "Bengali"},
        "ml": {"lang_code": "ml", "stt_code": "ml-IN", "name": "Malayalam"},
        "mr": {"lang_code": "mr", "stt_code": "mr-IN", "name": "Marathi"},
    }

    def __init__(
        self,
        voice_user_id: str,
        user_id: str,
        user_contexts: dict,
    ) -> None:
        self.voice_user_id = voice_user_id
        self.user_id = user_id
        self.user_contexts = user_contexts
        self.current_language = "en"

        system_prompt = build_system_prompt(user_contexts)
        super().__init__(instructions=system_prompt)

    # ── Language Switching ────────────────────────────────────────────

    async def _switch_language(self, language_code: str) -> str:
        if language_code == self.current_language:
            return f"Already speaking {self.LANGUAGE_MAP[language_code]['name']}"

        lang_info = self.LANGUAGE_MAP[language_code]
        lang_code = lang_info["lang_code"]
        stt_code = lang_info["stt_code"]
        lang_name = lang_info["name"]

        if self.session and self.session.tts is not None:
            self.session.tts.update_options(language=lang_code)

        if self.session and self.session.stt is not None:
            try:
                self.session.stt.update_options(language=stt_code)
            except Exception as e:
                logger.warning(f"STT language switch failed: {e}")

        self.current_language = language_code
        update_cached_context(self.voice_user_id, {"context_data.language": lang_code})
        logger.info(f"Language switched to {lang_name} (TTS: {lang_code}, STT: {stt_code})")
        return f"Switched to {lang_name}"

    @function_tool()
    async def switch_language(
        self,
        ctx: RunContext,
        language: str,
    ) -> str:
        """Switch the conversation language ONLY when the caller clearly speaks in a non-English language.
        DO NOT call this on the first message. Default language is English.
        Only switch after hearing the caller speak in Hindi, Arabic, Urdu, or another supported language.

        Args:
            language: Language code — one of: en (English), hi (Hindi), ar (Arabic), ur (Urdu), ta (Tamil), te (Telugu), kn (Kannada), bn (Bengali), ml (Malayalam), mr (Marathi)
        """
        if language not in self.LANGUAGE_MAP:
            return f"Unsupported language: {language}. Supported: {', '.join(self.LANGUAGE_MAP.keys())}"
        return await self._switch_language(language)

    # ── Profile Update Tool ──────────────────────────────────────────

    @function_tool()
    async def voice_update_profile(
        self,
        ctx: RunContext,
        field_name: str,
        field_value: str,
    ) -> str:
        """Save caller information as you learn it during the conversation. Call this every time the caller shares new info.

        Args:
            field_name: The field to update — one of: name, serviceInterest, destination, travelMonth, numTravellers, packageType, departureCity, visaNeeded
            field_value: The value to save (e.g. "umrah", "Saudi Arabia", "March", "4", "economy", "Mumbai", "yes")
        """
        ALLOWED_FIELDS = {
            "name", "serviceInterest", "destination", "travelMonth",
            "numTravellers", "packageType", "departureCity", "visaNeeded",
        }
        if field_name not in ALLOWED_FIELDS:
            return f"Invalid field: {field_name}. Allowed: {', '.join(ALLOWED_FIELDS)}"

        update_cached_context(self.voice_user_id, {f"context_data.{field_name}": field_value})
        self.user_contexts[field_name] = field_value
        logger.info(f"[TOOL] update_profile | User: {self.user_id} | {field_name}={field_value}")
        return f"Saved {field_name}: {field_value}"

    # ── Package Search Tool ──────────────────────────────────────────

    @function_tool()
    async def voice_find_packages(
        self,
        ctx: RunContext,
        destination: str,
        service_interest: str = "",
        package_type: str = "",
    ) -> str:
        """Search for travel packages based on destination, service interest, and package type.

        Args:
            destination: Travel destination (e.g. "Saudi Arabia", "Dubai", "Turkey", "Malaysia", "Egypt", "Azerbaijan")
            service_interest: Type of service — umrah, hajj, halal_holiday, or empty for all
            package_type: Package tier — economy, standard, premium, vip, or empty for all
        """
        logger.info(f"[TOOL] find_packages | User: {self.user_id} | dest={destination} svc={service_interest} pkg={package_type}")
        result = await find_packages(destination, service_interest or None, package_type or None)
        return json.dumps(result)

    # ── Callback Scheduling Tool ─────────────────────────────────────

    @function_tool()
    async def voice_schedule_callback(
        self,
        ctx: RunContext,
        preferred_date: str,
        preferred_time: str,
    ) -> str:
        """Schedule a consultant callback for the caller. Call this when the caller agrees to a callback.

        Args:
            preferred_date: Callback date (e.g. "2026-02-25", "tomorrow", "next Monday")
            preferred_time: Preferred time (e.g. "10:00 AM", "morning", "evening")
        """
        name = self.user_contexts.get("name", "")
        service = self.user_contexts.get("serviceInterest", "consultation")
        phone = self.user_contexts.get("phoneNumber", "")

        logger.info(f"[TOOL] schedule_callback | User: {self.user_id} | {preferred_date} {preferred_time}")
        result = await schedule_callback(
            user_id=self.user_id,
            phone_number=phone,
            preferred_date=preferred_date,
            preferred_time=preferred_time,
            name=name,
            service=service,
        )

        update_cached_context(self.voice_user_id, {
            "context_data.callbackDate": preferred_date,
            "context_data.callbackTime": preferred_time,
        })

        return json.dumps(result)

    # ── Call Control ─────────────────────────────────────────────────

    @function_tool()
    async def end_call(
        self,
        ctx: RunContext,
    ) -> str:
        """Hang up the phone call. You MUST call this tool whenever you say goodbye or the conversation is ending.

        WHEN TO CALL:
        - After giving closing message with JazakAllah Khair
        - After the caller says "bye", "ok bye", "thanks", "that's all"
        - After the caller says they don't want to proceed
        - After any goodbye/closing message
        - If you don't call this, the caller will hear silence forever
        """
        logger.info(f"[END_CALL] end_call() invoked for user {self.user_id} — disconnecting in 6s")
        job_ctx = get_job_context()

        async def _delayed_shutdown():
            await asyncio.sleep(6)
            try:
                for participant in job_ctx.room.remote_participants.values():
                    logger.info(f"[END_CALL] Removing SIP participant: {participant.identity}")
                    await job_ctx.api.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=job_ctx.room.name,
                            identity=participant.identity,
                        )
                    )
            except Exception as e:
                logger.error(f"[END_CALL] Failed to remove SIP participant: {e}")
            logger.info(f"[END_CALL] Executing shutdown for user {self.user_id}")
            job_ctx.shutdown()

        asyncio.create_task(_delayed_shutdown())
        return "Call ending"
