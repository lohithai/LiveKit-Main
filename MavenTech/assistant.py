import asyncio
from typing import Annotated

from livekit import api
from livekit.agents import Agent, RunContext, function_tool, get_job_context

from instruction import INSTRUCTION
from agent_tools import (
    booking_search_routes,
    booking_get_available_seats,
    booking_get_pickup_dropoff,
    booking_check_availability,
    booking_create_booking,
    booking_get_all_cities,
    booking_get_city_pairs,
    update_cached_context,
)
from logger import logger


class MavenTechAssistant(Agent):
    """MavenTech Bus Booking Voice AI Agent for LiveKit."""

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
    ) -> None:
        self.voice_user_id = voice_user_id
        self.user_id = user_id
        self.user_contexts = user_contexts
        self.current_language = "en"

        super().__init__(instructions=INSTRUCTION)

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
        logger.info(f"Language switched to {lang_name} (TTS: {lang_code}, STT: {stt_code})")
        return f"Switched to {lang_name}"

    @function_tool()
    async def switch_language(
        self,
        ctx: RunContext,
        language: str,
    ) -> str:
        """Switch the conversation language when the caller speaks a different language. Detect and switch automatically — never ask permission.

        Args:
            language: Language code — one of: en (English), hi (Hindi), ta (Tamil), te (Telugu), kn (Kannada), bn (Bengali), gu (Gujarati), ml (Malayalam), mr (Marathi)
        """
        if language not in self.LANGUAGE_MAP:
            return f"Unsupported language: {language}. Supported: {', '.join(self.LANGUAGE_MAP.keys())}"
        return await self._switch_language(language)

    # ── CRS Booking Tools ─────────────────────────────────────────────

    @function_tool()
    async def voice_booking_search_routes(
        self,
        ctx: RunContext,
        from_city: str,
        to_city: str,
        jdate: str,
    ) -> str:
        """Search available bus routes between two cities on a given date.

        Args:
            from_city: Departure city name (e.g. "Bangalore", "Chennai", "Delhi")
            to_city: Destination city name (e.g. "Mumbai", "Hyderabad")
            jdate: Journey date in YYYY-MM-DD format
        """
        logger.info(f"[TOOL] search_routes | User: {self.user_id} | {from_city} -> {to_city} on {jdate}")
        update_cached_context(self.voice_user_id, {
            "context_data.fromCity": from_city,
            "context_data.toCity": to_city,
            "context_data.journeyDate": jdate,
        })
        return await booking_search_routes(from_city, to_city, jdate)

    @function_tool()
    async def voice_booking_get_available_seats(
        self,
        ctx: RunContext,
        trip_id: int,
        from_city_id: int,
        to_city_id: int,
        journey_date: str,
    ) -> str:
        """Get available seats and fare details for a specific trip/bus.

        Args:
            trip_id: TripID from search results
            from_city_id: From City ID from search results
            to_city_id: To City ID from search results
            journey_date: Journey date in YYYY-MM-DD format
        """
        logger.info(f"[TOOL] get_available_seats | User: {self.user_id} | TripID: {trip_id}")
        return await booking_get_available_seats(trip_id, from_city_id, to_city_id, journey_date)

    @function_tool()
    async def voice_booking_get_pickup_dropoff(
        self,
        ctx: RunContext,
        route_code: str,
    ) -> str:
        """Get pickup and dropoff locations for a route. Call after user selects a bus.

        Args:
            route_code: RouteCode from search results
        """
        logger.info(f"[TOOL] get_pickup_dropoff | User: {self.user_id} | RouteCode: {route_code}")
        return await booking_get_pickup_dropoff(route_code)

    @function_tool()
    async def voice_booking_check_availability(
        self,
        ctx: RunContext,
        trip_id: int,
        journey_date: str,
        from_city_id: int,
        to_city_id: int,
    ) -> str:
        """Check real-time seat availability for a specific trip. Use for seat selection step.

        Args:
            trip_id: TripID from search results
            journey_date: Journey date in YYYY-MM-DD format
            from_city_id: From City ID
            to_city_id: To City ID
        """
        logger.info(f"[TOOL] check_availability | User: {self.user_id} | TripID: {trip_id}")
        return await booking_check_availability(trip_id, journey_date, from_city_id, to_city_id)

    @function_tool()
    async def voice_booking_create_booking(
        self,
        ctx: RunContext,
        trip_id: int,
        from_city_id: int,
        to_city_id: int,
        journey_date: str,
        pickup_id: int,
        dropoff_id: int,
        total_fare: float,
        primary_passenger_name: str,
        primary_passenger_mobile: str,
        primary_passenger_email: str = "",
        passenger_details_json: str = "[]",
    ) -> str:
        """Create a bus ticket booking. Only call after final confirmation from customer.

        Args:
            trip_id: TripID of the selected bus
            from_city_id: From City ID
            to_city_id: To City ID
            journey_date: Journey date YYYY-MM-DD
            pickup_id: Pickup Point ID
            dropoff_id: Dropoff Point ID
            total_fare: Total fare amount
            primary_passenger_name: Passenger name
            primary_passenger_mobile: 10-digit mobile number
            primary_passenger_email: Email address
            passenger_details_json: JSON string of passenger list e.g. '[{"SeatID": "2", "SeatNo": "B2", "Name": "Ravi", "Gender": "M", "Age": 30, "Fare": 550}]'
        """
        logger.info(f"[TOOL] create_booking | User: {self.user_id} | TripID: {trip_id} | Passenger: {primary_passenger_name}")
        update_cached_context(self.voice_user_id, {
            "context_data.passengerName": primary_passenger_name,
            "context_data.passengerMobile": primary_passenger_mobile,
        })
        return await booking_create_booking(
            trip_id, from_city_id, to_city_id, journey_date,
            pickup_id, dropoff_id, total_fare,
            primary_passenger_name, primary_passenger_mobile,
            primary_passenger_email, passenger_details_json,
        )

    @function_tool()
    async def voice_booking_get_all_cities(
        self,
        ctx: RunContext,
    ) -> str:
        """Fetch all available cities. Use if customer asks what cities are available."""
        logger.info(f"[TOOL] get_all_cities | User: {self.user_id}")
        return await booking_get_all_cities()

    @function_tool()
    async def voice_booking_get_city_pairs(
        self,
        ctx: RunContext,
    ) -> str:
        """Fetch available city pairs/routes. Use if customer asks what routes are available."""
        logger.info(f"[TOOL] get_city_pairs | User: {self.user_id}")
        return await booking_get_city_pairs()

    # ── Call Control ──────────────────────────────────────────────────

    @function_tool()
    async def end_call(
        self,
        ctx: RunContext,
    ) -> str:
        """Hang up the phone call. You MUST call this tool whenever you say goodbye or the conversation is ending. If you don't call this, the caller will hear silence forever.

        WHEN TO CALL:
        - After giving booking confirmation with PNR and closing message
        - After the caller says "bye", "ok bye", "thanks", "ok thank you", "that's all"
        - After the caller says they don't want to book
        - After any goodbye/closing message
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
