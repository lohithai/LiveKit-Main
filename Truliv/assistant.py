import json
import asyncio
from datetime import date, datetime
from typing import Optional, Annotated

from livekit import agents, api, rtc
from livekit.agents import Agent, RunContext, function_tool, get_job_context

from instruction import generate_agent_system_prompt
from agent_tools import (
    load_properties_once,
    update_user_profile,
    find_nearest_property,
    explore_more_properties,
    schedule_site_visit,
    query_property_information,
    zero_deposit,
    get_room_types_for_property,
    get_room_availability,
    get_all_room_availability,
    properties_according_to_budget,
    set_cached_context,
    flush_cached_context,
    clear_cached_context,
)
from lead_sync import sync_user_to_leadsquared
from database import get_async_context_collection
from logger import logger

import calendar


class TrulivAssistant(Agent):
    """Truliv Voice AI Agent for LiveKit - handles PG property inquiries and visit scheduling."""

    # Cartesia uses ISO 639-1 language codes for multilingual voice
    LANGUAGE_MAP = {
        "en": {"lang_code": "en", "stt_code": "en-IN", "name": "English"},
        "hi": {"lang_code": "hi", "stt_code": "hi-IN", "name": "Hindi"},
        "ta": {"lang_code": "ta", "stt_code": "ta-IN", "name": "Tamil"},
        "te": {"lang_code": "te", "stt_code": "te-IN", "name": "Telugu"},
        "kn": {"lang_code": "kn", "stt_code": "kn-IN", "name": "Kannada"},
        "bn": {"lang_code": "bn", "stt_code": "bn-IN", "name": "Bengali"},
        "gu": {"lang_code": "gu", "stt_code": "gu-IN", "name": "Gujarati"},
        "ml": {"lang_code": "ml", "stt_code": "ml-IN", "name": "Malayalam"},
        "mr": {"lang_code": "mr", "stt_code": "mr-IN", "name": "Marathi"},
    }

    def __init__(
        self,
        voice_user_id: str,
        user_id: str,
        user_contexts: dict,
        properties_name: list = None,
    ) -> None:
        self.voice_user_id = voice_user_id
        self.user_id = user_id
        self.user_contexts = user_contexts
        self.properties_name = properties_name or []
        self.current_language = "en"

        instruction = self._compose_system_prompt()
        super().__init__(
            instructions=instruction,
        )

    def _compose_system_prompt(self) -> str:
        """Compose the system prompt with live context."""
        logger.info(f"[PROMPT GENERATION] User: {self.user_id}")

        phone_number = self.user_contexts.get("phoneNumber", self.user_id)
        if isinstance(phone_number, str) and phone_number.startswith("91"):
            phone_number = phone_number[2:]

        bot_profession = self.user_contexts.get("botProfession")
        bot_timeline = self.user_contexts.get("botMoveInPreference")
        bot_location = self.user_contexts.get("botLocationPreference")
        bot_room_type = self.user_contexts.get("botRoomSharingPreference")
        bot_property = self.user_contexts.get("botPropertyPreference")
        bot_scheduled_visit_date = self.user_contexts.get("botSvDate", "")
        bot_scheduled_visit_time = self.user_contexts.get("botSvTime")
        name = self.user_contexts.get("name")

        now = datetime.now()
        today = now.date()
        current_date = today.strftime('%Y-%m-%d')
        current_day = calendar.day_name[today.weekday()]
        current_formatted = today.strftime('%d %B %Y')
        current_time = now.strftime('%I:%M %p')

        is_returning = name and name not in ['Voice User', 'User', 'Unknown', '']

        call_history = self.user_contexts.get("callHistory", [])
        total_calls = len(call_history)
        last_call_summary = self.user_contexts.get("lastCallSummary", "")

        call_history_text = ""
        if call_history:
            recent_calls = call_history[-3:]
            history_lines = []
            for i, call in enumerate(reversed(recent_calls), 1):
                call_date = call.get("date", "Unknown date")
                call_time_str = call.get("time", "")
                call_summary = call.get("summary", "No summary")
                visit_scheduled = "Visit booked" if call.get("visitScheduled") else ""
                history_lines.append(f"  Call {i} ({call_date} {call_time_str}): {call_summary} {visit_scheduled}")
            call_history_text = "\n".join(history_lines)

        return generate_agent_system_prompt(
            properties_name=self.properties_name,
            agent_name="Priya",
            company_name="Truliv Coliving",
            phone_number="9043221620",
            user_id=self.user_id,
            current_date=current_date,
            current_time=current_time,
            current_day=current_day,
            current_formatted=current_formatted,
            is_returning=is_returning,
            total_calls=total_calls,
            name=name,
            bot_profession=bot_profession,
            bot_timeline=bot_timeline,
            bot_location=bot_location,
            bot_room_type=bot_room_type,
            bot_property=bot_property,
            bot_scheduled_visit_date=bot_scheduled_visit_date,
            bot_scheduled_visit_time=bot_scheduled_visit_time,
            last_call_summary=last_call_summary,
            call_history_text=call_history_text,
        )

    # -- Language Switching (updated for Cartesia TTS) ---------------------

    async def _switch_language(self, language_code: str) -> str:
        """Switch both TTS and STT to the target language."""
        if language_code == self.current_language:
            return f"Already speaking {self.LANGUAGE_MAP[language_code]['name']}"

        lang_info = self.LANGUAGE_MAP[language_code]
        lang_code = lang_info["lang_code"]
        stt_code = lang_info["stt_code"]
        lang_name = lang_info["name"]

        # Switch Cartesia TTS language
        if self.session and self.session.tts is not None:
            self.session.tts.update_options(language=lang_code)

        # Switch Sarvam STT language for faster processing
        if self.session and self.session.stt is not None:
            try:
                self.session.stt.update_options(language=stt_code)
            except Exception as e:
                logger.warning(f"STT language switch failed: {e}")

        self.current_language = language_code
        logger.info(f"Language switched to {lang_name} (TTS: {lang_code}, STT: {stt_code})")
        return f"Switched to {lang_name}"

    @function_tool()
    async def switch_language(
        self,
        ctx: RunContext,
        language: str,
    ) -> str:
        """Switch the conversation language when the caller speaks a different language than the current one. Only call this AFTER the caller has spoken — never on the greeting turn. Do NOT ask permission — just detect and switch.

        Args:
            language: Language code — one of: en (English), hi (Hindi), ta (Tamil), te (Telugu), kn (Kannada), bn (Bengali), gu (Gujarati), ml (Malayalam), mr (Marathi)
        """
        if language not in self.LANGUAGE_MAP:
            return f"Unsupported language: {language}. Supported: {', '.join(self.LANGUAGE_MAP.keys())}"
        return await self._switch_language(language)

    # -- Tool Methods ---------------------------------------------------------

    @function_tool()
    async def voice_update_user_profile(
        self,
        ctx: RunContext,
        profession: str = "",
        move_in: str = "",
        room_type: str = "",
        property_name: str = "",
        name: str = "",
        phone_number: str = "",
    ) -> str:
        """Update user profile with preferences. Call when user mentions their profession, move-in timeline, room preference, property interest, name, or phone number.

        Args:
            profession: User's profession (working/student)
            move_in: When user wants to move in
            room_type: Room type preference (private/shared)
            property_name: Specific property name user is interested in
            name: User's name
            phone_number: User's phone number
        """
        return await update_user_profile(
            user_id=self.voice_user_id,
            profession=profession or None,
            timeline=move_in or None,
            room_type=room_type or None,
            property_preference=property_name or None,
            name=name or None,
            phone_number=phone_number or None,
        )

    @function_tool()
    async def voice_find_nearest_property(
        self,
        ctx: RunContext,
        location_query: str,
    ) -> str:
        """Find properties near a location or area in Chennai. Use when user mentions an area name like OMR, Kodambakkam, T.Nagar, Velachery, etc. Pass AREA name only, never property name.

        Args:
            location_query: Area or location name like 'OMR', 'Kodambakkam', 'T.Nagar'
        """
        return await find_nearest_property(
            self.voice_user_id,
            location_query,
        )

    @function_tool()
    async def voice_properties_according_to_budget(
        self,
        ctx: RunContext,
        budget_query: str,
    ) -> str:
        """Find properties within user's budget. Use when user mentions a specific budget amount.

        Args:
            budget_query: User's budget query like 'under 10k', 'between 10k and 15k', '8000'
        """
        return await properties_according_to_budget(
            self.voice_user_id,
            budget_query,
        )

    @function_tool()
    async def voice_query_property_information(
        self,
        ctx: RunContext,
        query: str,
        property_name: str,
    ) -> str:
        """Get details about a specific property - price, address, amenities, room types, etc. Use when user asks about a specific property by name.

        Args:
            query: Question like 'pricing', 'address', 'amenities', 'details'
            property_name: Exact property name like 'Truliv Amara', 'Truliv Vesta'
        """
        return await query_property_information(
            self.voice_user_id,
            query,
            property_name,
        )

    @function_tool()
    async def voice_explore_more_properties(
        self,
        ctx: RunContext,
        exclude_properties: str = "",
    ) -> str:
        """Show more properties in the current area, excluding ones already shown. Use when user asks for more options or different properties.

        Args:
            exclude_properties: Comma-separated property names to exclude from results
        """
        return await explore_more_properties(
            self.voice_user_id,
            exclude_properties,
        )

    @function_tool()
    async def voice_schedule_site_visit(
        self,
        ctx: RunContext,
        visit_date: str,
        visit_time: str,
        name: str,
    ) -> str:
        """Schedule a site visit for the user. Only call when user has confirmed both date AND time AND you have their name.

        Args:
            visit_date: Date in YYYY-MM-DD format (convert from natural language)
            visit_time: Time in HH:MM format or natural like '2 PM', '10:30 AM'
            name: User's name for the booking
        """
        return await schedule_site_visit(
            self.voice_user_id,
            visit_date,
            visit_time,
            name,
        )

    @function_tool()
    async def voice_get_room_types(
        self,
        ctx: RunContext,
        property_name: str = "",
    ) -> str:
        """Get available room types and their amenities for a property. Use when user asks about room types, single vs double, or male vs female options.

        Args:
            property_name: Property name to check room types for
        """
        return await get_room_types_for_property(
            self.voice_user_id,
            property_name or None,
        )

    @function_tool()
    async def voice_get_availability(
        self,
        ctx: RunContext,
        property_name: str = "",
        move_in_date: str = "",
    ) -> str:
        """Check real-time bed availability for a specific property. Use when user asks about availability or when they can move in.

        Args:
            property_name: Property name to check availability for
            move_in_date: Move-in date in YYYY-MM-DD format if specified
        """
        return await get_room_availability(
            self.voice_user_id,
            property_name or None,
            move_in_date or None,
        )

    @function_tool()
    async def voice_get_all_room_availability(
        self,
        ctx: RunContext,
    ) -> str:
        """Get room availability across ALL Truliv properties. Use when user asks which properties have rooms available without specifying a particular property."""
        return await get_all_room_availability(
            self.voice_user_id,
        )

    @function_tool()
    async def voice_zero_deposit(
        self,
        ctx: RunContext,
        query: str,
    ) -> str:
        """Answer questions about Truliv's Zero-Deposit option powered by CirclePe. ONLY use when user specifically asks about zero deposit alternative, NOT for general deposit questions.

        Args:
            query: User's specific question about zero deposit option
        """
        return await zero_deposit(query)

    @function_tool()
    async def end_call(
        self,
        ctx: RunContext,
    ) -> str:
        """Hang up the phone call. You MUST call this tool whenever you say goodbye or the conversation is ending. If you don't call this, the caller will hear silence forever.

        WHEN TO CALL:
        - After saying any goodbye/closing message
        - After the caller says "bye", "ok bye", "thanks", "ok thank you", "that's all"
        - After confirming a visit and the caller has no more questions
        """
        import asyncio
        logger.info(f"[END_CALL] end_call() invoked for user {self.user_id} — disconnecting in 6s")
        job_ctx = get_job_context()

        async def _delayed_shutdown():
            await asyncio.sleep(6)  # Wait for TTS to finish speaking goodbye

            # Remove SIP participant to send SIP BYE and hang up the phone
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
