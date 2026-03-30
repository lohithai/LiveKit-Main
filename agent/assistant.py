"""
Truliv Luna Bengaluru — Voice AI Assistant
Single property agent with Sarvam TTS + STT
"""

import asyncio
from datetime import date, datetime

from livekit import api
from livekit.agents import Agent, RunContext, function_tool, get_job_context

from instruction import generate_agent_system_prompt
from agent_tools import (
    update_user_profile,
    schedule_site_visit,
    query_luna_property_info,
    get_luna_room_types,
    get_luna_availability,
    zero_deposit,
    check_location_proximity,
    update_cached_context,
)
from logger import logger

import calendar


class TrulivAssistant(Agent):
    """Truliv Luna Bengaluru Voice AI Agent — single property, Sarvam TTS+STT."""

    # Sarvam BCP-47 codes — same speaker (ritu) for all languages
    LANGUAGE_MAP = {
        "en": {"tts_code": "en-IN", "stt_code": "en-IN", "name": "English"},
        "hi": {"tts_code": "hi-IN", "stt_code": "hi-IN", "name": "Hindi"},
        "ta": {"tts_code": "ta-IN", "stt_code": "ta-IN", "name": "Tamil"},
        "te": {"tts_code": "te-IN", "stt_code": "te-IN", "name": "Telugu"},
        "kn": {"tts_code": "kn-IN", "stt_code": "kn-IN", "name": "Kannada"},
        "bn": {"tts_code": "bn-IN", "stt_code": "bn-IN", "name": "Bengali"},
        "gu": {"tts_code": "gu-IN", "stt_code": "gu-IN", "name": "Gujarati"},
        "ml": {"tts_code": "ml-IN", "stt_code": "ml-IN", "name": "Malayalam"},
        "mr": {"tts_code": "mr-IN", "stt_code": "mr-IN", "name": "Marathi"},
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

    # -- STT prompts per language (helps Sarvam accuracy) -----------
    STT_PROMPTS = {
        "en-IN": "Truliv Luna Bengaluru PG coliving property. Koramangala, HSR Layout, Electronic City, Whitefield, Indiranagar, Marathahalli, Bellandur, Sarjapur",
        "hi-IN": "Truliv Luna Bengaluru PG coliving. कोरमंगला, HSR लेआउट, इलेक्ट्रॉनिक सिटी, व्हाइटफील्ड",
        "kn-IN": "Truliv Luna ಬೆಂಗಳೂರು PG coliving. ಕೋರಮಂಗಲ, HSR ಲೇಔಟ್, ಎಲೆಕ್ಟ್ರಾನಿಕ್ ಸಿಟಿ, ವೈಟ್‌ಫೀಲ್ಡ್",
        "ta-IN": "Truliv Luna பெங்களூர் PG coliving. கோரமங்கலா, HSR லேஅவுட், எலக்ட்ரானிக் சிட்டி",
        "te-IN": "Truliv Luna బెంగళూరు PG coliving. కోరమంగల, HSR లేఔట్, ఎలక్ట్రానిక్ సిటీ",
        "ml-IN": "Truliv Luna ബെംഗളൂരു PG coliving",
        "mr-IN": "Truliv Luna बेंगळूरू PG coliving",
        "bn-IN": "Truliv Luna বেঙ্গালুরু PG coliving",
        "gu-IN": "Truliv Luna બેંગલુરુ PG coliving",
    }

    # -- Language Switching (Sarvam TTS + STT) ---------------------

    async def _switch_language(self, language_code: str) -> str:
        """Full language switch: STT + TTS (base opts + live streams) + instructions."""
        if language_code not in self.LANGUAGE_MAP:
            return f"Unsupported language: {language_code}"

        if language_code == self.current_language:
            return f"Already speaking {self.LANGUAGE_MAP[language_code]['name']}"

        lang_info = self.LANGUAGE_MAP[language_code]
        tts_code = lang_info["tts_code"]
        stt_code = lang_info["stt_code"]
        lang_name = lang_info["name"]

        # STEP 1: Update Sarvam STT — base opts + live streams
        if self.session and self.session.stt is not None:
            try:
                stt_prompt = self.STT_PROMPTS.get(stt_code, "")
                # Update base options for new streams
                self.session.stt.update_options(language=stt_code)
                # Update live streams if accessible
                if hasattr(self.session.stt, '_streams'):
                    for stream in self.session.stt._streams:
                        try:
                            stream.update_options(
                                language=stt_code,
                                mode="transcribe",
                                prompt=stt_prompt,
                            )
                        except Exception:
                            pass
                logger.info(f"[LANG] STT switched to {stt_code}")
            except Exception as e:
                logger.warning(f"STT language switch failed: {e}")

        # STEP 2: Update Sarvam TTS — language only, keep same speaker
        if self.session and self.session.tts is not None:
            try:
                self.session.tts.update_options(language=tts_code)
                # Update live streams if accessible
                if hasattr(self.session.tts, '_streams'):
                    for stream in self.session.tts._streams:
                        try:
                            stream._opts.target_language_code = tts_code
                        except Exception:
                            pass
                logger.info(f"[LANG] TTS switched to {tts_code}")
            except Exception as e:
                logger.warning(f"TTS language switch failed: {e}")

        # STEP 3: Update agent state
        self.current_language = language_code

        logger.info(f"[LANG] Full switch to {lang_name} (TTS: {tts_code}, STT: {stt_code})")

        # STEP 4: Return handoff message
        return f"Language switched to {lang_name}. Continue the conversation in {lang_name} from now on."

    @function_tool()
    async def switch_language(
        self,
        ctx: RunContext,
        language: str,
    ) -> str:
        """Switch the conversation language when the caller consistently speaks in another language.

        WHEN TO CALL:
        - Caller has spoken full sentences in another language for 2 CONSECUTIVE turns.
        - Caller explicitly asks: "Hindi mein baat karo", "Tamil la pesu", etc.

        NEVER call this for:
        - Filler words (haan, accha, theek hai, seri, anna, ji, yaar, etc.)
        - Code-mixing (English sentences with a few regional words mixed in)
        - Just 1 turn in another language (could be STT error)
        - On the greeting turn

        Args:
            language: Language code — one of: en (English), hi (Hindi), ta (Tamil), te (Telugu), kn (Kannada), bn (Bengali), gu (Gujarati), ml (Malayalam), mr (Marathi)
        """
        if language not in self.LANGUAGE_MAP:
            return f"Unsupported language: {language}. Supported: {', '.join(self.LANGUAGE_MAP.keys())}"
        logger.info(f"[LANG SWITCH] Caller requested: {language}")
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
        """Update user profile with preferences. Call when user mentions their profession, move-in timeline, room preference, name, or phone number.

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
    async def voice_check_location(
        self,
        ctx: RunContext,
        location_query: str,
    ) -> str:
        """Check if a Bengaluru area is near Truliv Luna. Call when user mentions a specific area or location in Bengaluru like Koramangala, Electronic City, Whitefield, HSR Layout, etc.

        Args:
            location_query: Area name in Bengaluru like 'Koramangala', 'Electronic City', 'Whitefield'
        """
        return await check_location_proximity(
            self.voice_user_id,
            location_query,
        )

    @function_tool()
    async def voice_query_property_info(
        self,
        ctx: RunContext,
        query: str,
    ) -> str:
        """Get details about Truliv Luna — pricing, address, amenities, etc. Use when user asks about the property.

        Args:
            query: Question like 'pricing', 'address', 'amenities', 'details'
        """
        return await query_luna_property_info(
            self.voice_user_id,
            query,
        )

    @function_tool()
    async def voice_get_room_types(
        self,
        ctx: RunContext,
    ) -> str:
        """Get available room types at Truliv Luna. Use when user asks about room types, single vs double, or male vs female options."""
        return await get_luna_room_types(self.voice_user_id)

    @function_tool()
    async def voice_get_availability(
        self,
        ctx: RunContext,
    ) -> str:
        """Check real-time bed availability at Truliv Luna. Use when user asks about availability or when they can move in."""
        return await get_luna_availability(self.voice_user_id)

    @function_tool()
    async def voice_schedule_site_visit(
        self,
        ctx: RunContext,
        visit_date: str,
        visit_time: str,
        name: str,
    ) -> str:
        """Schedule a site visit at Truliv Luna. ONLY call this after the caller has EXPLICITLY told you both a specific date AND a specific time. NEVER guess. You must also have their name.

        Args:
            visit_date: Date in YYYY-MM-DD format (convert from natural language like 'tomorrow', 'Monday')
            visit_time: Time in HH:MM format or natural like '2 PM', '10:30 AM' — must be explicitly stated by caller
            name: User's name for the booking
        """
        return await schedule_site_visit(
            self.voice_user_id,
            visit_date,
            visit_time,
            name,
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
        """Hang up the phone call. You MUST call this tool whenever you say goodbye or the conversation is ending.

        WHEN TO CALL:
        - After saying any goodbye/closing message
        - After the caller says "bye", "ok bye", "thanks", "ok thank you", "that's all"
        - After confirming a visit and the caller has no more questions
        """
        import asyncio
        logger.info(f"[END_CALL] end_call() invoked for user {self.user_id} — disconnecting in 15s")
        job_ctx = get_job_context()

        async def _delayed_shutdown():
            await asyncio.sleep(15)

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
