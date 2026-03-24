"""
Truliv Luna Bengaluru — Agent Tools
Single-property agent: all data comes from Warden API cached in mongo_data.py
"""

import os
import json
import asyncio
import re
import time
from typing import Dict, List, Optional
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime
import traceback

import aiohttp
import requests
from dotenv import load_dotenv

from logger import logger
from database import get_async_context_collection
from lead_sync import sync_user_to_leadsquared
from task_queue import bg_tasks
from mongo_data import (
    preload_all_data,
    get_property,
    get_property_names,
    get_room_types,
    get_bed_availability,
    get_starting_price,
)

# LangChain removed — direct async Gemini calls for lower latency

load_dotenv()

PROPERTY_NAME = "Truliv Luna"

# ==================== User Context Cache ====================

_user_context_cache = {}


def get_cached_context(user_id: str) -> Optional[dict]:
    """Get user context from cache if available."""
    if user_id in _user_context_cache:
        return _user_context_cache[user_id].get("context_data")
    return None


def set_cached_context(user_id: str, context_data: dict):
    """Set user context in cache."""
    _user_context_cache[user_id] = {
        "context_data": context_data.copy(),
        "dirty": False,
        "pending_updates": {}
    }
    logger.info(f"[CACHE] Context cached for user {user_id}")


def update_cached_context(user_id: str, updates: dict):
    """Update specific fields in cached context (marks as dirty for later DB write)."""
    if user_id not in _user_context_cache:
        _user_context_cache[user_id] = {"context_data": {}, "dirty": False, "pending_updates": {}}

    for key, value in updates.items():
        clean_key = key.replace("context_data.", "")
        _user_context_cache[user_id]["context_data"][clean_key] = value
        _user_context_cache[user_id]["pending_updates"][key] = value

    _user_context_cache[user_id]["dirty"] = True
    logger.info(f"[CACHE] Updated cache for {user_id}: {list(updates.keys())}")


async def flush_cached_context(user_id: str) -> bool:
    """Write all pending updates to MongoDB and clear cache."""
    if user_id not in _user_context_cache:
        return False

    cache_entry = _user_context_cache[user_id]

    if not cache_entry.get("dirty") or not cache_entry.get("pending_updates"):
        logger.info(f"[CACHE] No pending updates for {user_id}")
        clear_cached_context(user_id)
        return True

    try:
        context_collection = await get_async_context_collection()
        update_data = {"$set": cache_entry["pending_updates"]}

        result = await context_collection.update_one(
            {"_id": user_id},
            update_data,
            upsert=True
        )

        logger.info(f"[CACHE] Flushed {len(cache_entry['pending_updates'])} updates to DB for {user_id}")
        clear_cached_context(user_id)
        return True

    except Exception as e:
        logger.error(f"[CACHE] Failed to flush context for {user_id}: {e}")
        return False


def clear_cached_context(user_id: str):
    """Clear user context from cache."""
    if user_id in _user_context_cache:
        del _user_context_cache[user_id]
        logger.info(f"[CACHE] Cleared cache for {user_id}")


# ==================== Data Loader ====================


async def load_properties_once():
    """Load Truliv Luna data from Warden API once and cache globally."""
    await preload_all_data()


# ==================== Tools ====================


async def update_user_profile(
    user_id: str,
    profession: Optional[str] = None,
    timeline: Optional[str] = None,
    room_type: Optional[str] = None,
    property_preference: Optional[str] = None,
    budget: Optional[str] = None,
    name: Optional[str] = None,
    phone_number: Optional[str] = None
) -> str:
    """Update user profile fields in cache (flushed to MongoDB at end of call)."""
    logger.info(f"[TOOL-START] update_user_profile | User: {user_id} | profession={profession}, timeline={timeline}, room_type={room_type}, name={name}")

    try:
        update_fields = {}
        updated_items = []

        if phone_number is not None:
            clean_phone = "".join(filter(str.isdigit, phone_number))
            if len(clean_phone) >= 10:
                clean_phone = clean_phone[-10:]
                update_fields["context_data.phoneNumber"] = clean_phone
                updated_items.append(f"Phone: {clean_phone}")
            elif clean_phone:
                update_fields["context_data.phoneNumber"] = clean_phone
                updated_items.append(f"Phone: {clean_phone}")

        if profession is not None:
            prof_lower = profession.lower()
            if any(w in prof_lower for w in ["work", "job", "employ", "office", "professional", "engineer"]):
                update_fields["context_data.botProfession"] = "working"
                updated_items.append("Profession: working")
            elif any(w in prof_lower for w in ["stud", "college", "university"]):
                update_fields["context_data.botProfession"] = "studying"
                updated_items.append("Profession: studying")
            else:
                update_fields["context_data.botProfession"] = profession
                updated_items.append(f"Profession: {profession}")

        if timeline is not None:
            tl = timeline.lower()
            if any(w in tl for w in ["immediate", "this month", "asap", "now"]):
                update_fields["context_data.botMoveInPreference"] = "this_month"
                updated_items.append("Timeline: this month")
            elif any(w in tl for w in ["next month", "1-2", "one to two", "6 week"]):
                update_fields["context_data.botMoveInPreference"] = "one_to_two_months"
                updated_items.append("Timeline: 1-2 months")
            elif any(w in tl for w in ["later", "after 2", "more than", "3 month"]):
                update_fields["context_data.botMoveInPreference"] = "more_than_two_months"
                updated_items.append("Timeline: more than 2 months")
            else:
                update_fields["context_data.botMoveInPreference"] = timeline
                updated_items.append(f"Timeline: {timeline}")

        if room_type is not None:
            rl = room_type.lower()
            if any(w in rl for w in ["private", "single", "1"]):
                update_fields["context_data.botRoomSharingPreference"] = "private"
                updated_items.append("Room type: private")
            elif any(w in rl for w in ["shared", "double", "triple", "2", "3"]):
                update_fields["context_data.botRoomSharingPreference"] = "shared"
                updated_items.append("Room type: shared")
            else:
                update_fields["context_data.botRoomSharingPreference"] = room_type
                updated_items.append(f"Room type: {room_type}")

        if property_preference is not None:
            update_fields["context_data.botPropertyPreference"] = property_preference
            updated_items.append(f"Property: {property_preference}")

        if budget is not None:
            update_fields["context_data.botBudget"] = budget
            updated_items.append(f"Budget: {budget}")

        if name is not None:
            update_fields["context_data.name"] = name
            updated_items.append(f"Name: {name}")

        if not update_fields:
            return "No profile information was provided to update."

        update_cached_context(user_id, update_fields)
        logger.info(f"[TOOL-END] update_user_profile | User: {user_id} | Cached: {', '.join(updated_items)}")
        return "OK"

    except Exception as e:
        logger.error(f"[TOOL-ERROR] update_user_profile failed | User: {user_id} | Error: {str(e)}", exc_info=True)
        return "Error updating profile"


async def schedule_site_visit(
    user_phone: str,
    visit_date: str,
    visit_time: str,
    name: Optional[str] = None
) -> str:
    """Schedule a site visit at Truliv Luna Bengaluru."""
    logger.info(f"[TOOL-START] schedule_site_visit | User: {user_phone} | Date: {visit_date} | Time: {visit_time} | Name: {name}")

    try:
        now = datetime.now()

        try:
            parsed_date = datetime.strptime(visit_date, "%Y-%m-%d")
        except ValueError:
            return "Invalid date format received. Please use date format like 2026-01-15."

        if parsed_date.date() < now.date():
            return "That date has already passed. Could you give me a future date for the visit?"

        parsed_time = None
        time_formats = ["%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"]

        for fmt in time_formats:
            try:
                parsed_time = datetime.strptime(visit_time.strip().upper(), fmt)
                break
            except ValueError:
                continue

        if parsed_time is None:
            return "Invalid time format received. Please provide time like 10 AM or 2:30 PM."

        formatted_time = parsed_time.strftime("%H:%M")

        visit_hour = parsed_time.hour
        if visit_hour < 9 or visit_hour >= 20:
            return "Our visiting hours are nine A.M. to eight P.M., any day of the week. Could you pick a time within that window?"

        if parsed_date.date() == now.date():
            visit_datetime = parsed_date.replace(hour=parsed_time.hour, minute=parsed_time.minute)
            if visit_datetime < now:
                return "That time has already passed for today. Could you pick a later time, or schedule for tomorrow?"

        update_fields = {
            "context_data.botSvDate": visit_date,
            "context_data.botSvTime": formatted_time,
            "context_data.botPropertyPreference": PROPERTY_NAME,
        }

        if name:
            update_fields["context_data.name"] = name
        else:
            cached_context = get_cached_context(user_phone)
            existing_name = cached_context.get("name") if cached_context else None
            if not existing_name or existing_name in ["Voice User", "User", "Unknown", ""]:
                return "I need your name to schedule the visit. May I know your name please?"

        update_cached_context(user_phone, update_fields)

        display_date = parsed_date.strftime("%d %B")
        display_time = parsed_time.strftime("%I:%M %p").lstrip("0")

        logger.info(f"[TOOL-END] schedule_site_visit | User: {user_phone} | Visit: {visit_date} at {formatted_time}")
        return f"Your visit to Truliv Luna is confirmed for {display_date} at {display_time}. Our team will be there to welcome you and show you around the property."

    except Exception as e:
        logger.error(f"[TOOL-ERROR] schedule_site_visit failed | Error: {str(e)}", exc_info=True)
        return "Sorry, couldn't book that. Let me try again - what date and time works for you?"


async def query_luna_property_info(user_id: str, query: str) -> str:
    """Get details about Truliv Luna from cached Warden API data, with static fallback."""
    logger.info(f"[TOOL-START] query_luna_property_info | Query: {query}")

    try:
        prop = get_property()

        # Use Warden data if available, otherwise static fallback
        name = prop.get("name", PROPERTY_NAME) if prop else PROPERTY_NAME
        address = (prop.get("fullAddress", "") if prop else "") or "Bengaluru, Karnataka"
        starting_price = get_starting_price()
        amenity_names = [a.get("name") for a in (prop.get("amenities", []) if prop else [])] if prop else []

        update_cached_context(user_id, {"context_data.botPropertyPreference": PROPERTY_NAME})

        query_lower = query.lower()

        if "address" in query_lower or "location" in query_lower or "where" in query_lower:
            return f"{name} is located in {address}. It's really well connected and easy to reach."

        elif "price" in query_lower or "rent" in query_lower or "cost" in query_lower:
            if starting_price > 0:
                return f"{name} has rooms starting from {starting_price:,} per month. Would you like to know about the different room types?"
            return f"{name} has very competitive pricing. Private rooms and shared rooms are both available. Would you like to visit and see the rooms?"

        elif "amenities" in query_lower or "facilities" in query_lower:
            if amenity_names:
                amenities_list = ", ".join(amenity_names[:6])
                return f"{name} comes with {amenities_list}. It's really well maintained."
            return f"{name} comes fully furnished with Wifi, housekeeping, electricity, water, and A.C. Everything you need to feel right at home."

        else:
            response = f"{name} is a lovely co living property in Bengaluru. Fully furnished rooms, great amenities, and a wonderful community."
            if starting_price > 0:
                response += f" Rooms start from {starting_price:,} per month."
            return response

    except Exception as e:
        logger.error(f"[TOOL-ERROR] query_luna_property_info failed | Error: {str(e)}", exc_info=True)
        return f"{PROPERTY_NAME} is a wonderful co living space in Bengaluru with fully furnished rooms, Wifi, housekeeping, and all modern amenities. Would you like to visit?"


async def get_luna_room_types(user_id: str) -> str:
    """Get room types for Truliv Luna from cached Warden data."""
    logger.info(f"[TOOL-START] get_luna_room_types | User: {user_id}")

    try:
        room_types_data = get_room_types()

        if not room_types_data:
            return f"At Truliv Luna, we have private rooms and shared rooms, both fully furnished with A.C., Wifi, and housekeeping. Would you like to come and see them? A visit really helps you decide."

        formatted_rooms = []
        for room in room_types_data:
            room_name = room.get("name", "Room")
            shared_amenities = [a.get("name") for a in room.get("sharedAmenities", []) if a.get("name")]
            private_amenities = [a.get("name") for a in room.get("privateAmenities", []) if a.get("name")]
            all_amenities = list(set(shared_amenities + private_amenities))

            if all_amenities:
                amenities_str = ", ".join(all_amenities[:5])
                formatted_rooms.append(f"{room_name} with {amenities_str}")
            else:
                formatted_rooms.append(room_name)

        logger.info(f"[TOOL-END] get_luna_room_types | Found {len(formatted_rooms)} room types")

        if formatted_rooms:
            rooms_str = ". ".join(formatted_rooms[:4])
            return f"At Truliv Luna, we have: {rooms_str}. Would you like to come and see them? A visit really helps you decide."
        return "I couldn't find room configurations right now."

    except Exception as e:
        logger.error(f"[TOOL-ERROR] get_luna_room_types failed | Error: {str(e)}", exc_info=True)
        return "Sorry, I couldn't fetch room details right now."


async def get_luna_availability(user_id: str) -> str:
    """Check bed availability for Truliv Luna from cached Warden data."""
    logger.info(f"[TOOL-START] get_luna_availability | User: {user_id}")

    try:
        bed_entry = get_bed_availability()

        if not bed_entry:
            return "Truliv Luna currently has rooms available in both private and shared options. But beds fill up quickly, so I'd suggest visiting soon to secure your spot. Would you like to schedule a visit?"

        available_rooms = []
        total_available = 0

        for avail in bed_entry.get("availability", []):
            room_type = avail.get("roomTypeName", "Room")
            beds = avail.get("availableBeds", 0)
            female_beds = avail.get("availableFemaleBeds", 0)
            male_beds = avail.get("availableMaleBeds", 0)

            if beds > 0:
                total_available += beds
                gender_parts = []
                if female_beds > 0:
                    gender_parts.append(f"{female_beds} female")
                if male_beds > 0:
                    gender_parts.append(f"{male_beds} male")

                if gender_parts:
                    gender_info = ", ".join(gender_parts)
                    available_rooms.append(f"{room_type} with {beds} beds available ({gender_info})")
                else:
                    available_rooms.append(f"{room_type} with {beds} beds available")

        logger.info(f"[TOOL-END] get_luna_availability | {total_available} total beds available")

        if available_rooms:
            rooms_str = ", ".join(available_rooms[:3])
            return (
                f"Great news! Truliv Luna currently has {rooms_str}. "
                f"But beds do fill up quickly. Would you like to come visit and secure your spot?"
            )
        return (
            "Truliv Luna is currently fully booked. "
            "But new openings come up regularly. Would you like me to keep you updated?"
        )

    except Exception as e:
        logger.error(f"[TOOL-ERROR] get_luna_availability failed | Error: {str(e)}", exc_info=True)
        return "Sorry, I couldn't check availability right now."


_ZERO_DEPOSIT_INFO = """Truliv provides a Zero-Deposit Move-In option through CirclePe.
How It Works: CirclePe pays Truliv your entire selected term's rent on your behalf on Day 1. You repay monthly rent to CirclePe with a small 2.25% platform fee.
Eligibility: Salaried individuals can apply directly. Students can apply through their parents. Self-employed individuals are currently not eligible. Eligibility is based on credit score and monthly income. If income is 2x monthly rent, approximately 90% chance of approval.
Payment: Monthly rent is auto-deducted via e-mandate on the 5th of every month.
Lock-in: Tenant cannot move out during lock-in period. If they do, they must still pay full rent until lock-in ends."""


async def zero_deposit(query: str) -> str:
    """Answer questions about Truliv's Zero-Deposit option powered by CirclePe."""
    logger.info(f"[TOOL-START] zero_deposit | Query: {query}")

    try:
        # Direct async Gemini call — no LangChain overhead
        import google.genai as genai

        prompt = f"""Answer concisely about Truliv's Zero-Deposit option. Only use facts below.

{_ZERO_DEPOSIT_INFO}

Question: {query}
Answer (1-2 sentences):"""

        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        response = await client.aio.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        answer = response.text.strip()

        logger.info(f"[TOOL-END] zero_deposit | Generated answer")
        return answer if answer else "I couldn't get that information right now."

    except Exception as e:
        logger.error(f"[TOOL-ERROR] zero_deposit failed | Error: {str(e)}", exc_info=True)
        return "Sorry, I couldn't answer that right now."


# ==================== Location Check ====================

_geocode_cache: Dict[str, Optional[Dict]] = {}


async def _geocode_address(address: str) -> Optional[Dict]:
    """Geocode an address using Google Maps Geocoding API (async, cached)."""
    if address in _geocode_cache:
        return _geocode_cache[address]

    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            logger.error("Google API key not found")
            return None

        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {"address": address, "key": api_key, "region": "in"}

        async with aiohttp.ClientSession() as http:
            async with http.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()

        if data.get("status") == "OK" and data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            result = {"lat": loc["lat"], "lng": loc["lng"]}
            _geocode_cache[address] = result
            return result

        _geocode_cache[address] = None
        return None

    except Exception as e:
        logger.error(f"Geocoding error for '{address}': {e}")
        return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    R = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


async def check_location_proximity(user_id: str, location_query: str) -> str:
    """
    Check if the user's preferred area is within 10km of Truliv Luna.
    Uses Google Geocoding API to resolve the area and compare distance.
    """
    logger.info(f"[TOOL-START] check_location_proximity | Location: {location_query} | User: {user_id}")

    try:
        # Get Truliv Luna's coordinates from cached property data
        prop = get_property()
        if not prop:
            return "I couldn't check the location right now. But we have a lovely property called Truliv Luna in Bengaluru!"

        prop_location = prop.get("location", {})
        prop_lat = prop_location.get("latitude")
        prop_lng = prop_location.get("longitude")

        if prop_lat is None or prop_lng is None:
            # Fallback: use address to geocode property location
            prop_address = prop.get("fullAddress", "Truliv Luna, Bengaluru")
            prop_coords = await _geocode_address(prop_address)
            if prop_coords:
                prop_lat = prop_coords["lat"]
                prop_lng = prop_coords["lng"]
            else:
                return "We have a lovely property called Truliv Luna in Bengaluru. Would you like to know more about it?"

        prop_lat = float(prop_lat)
        prop_lng = float(prop_lng)

        # Geocode user's area (async — non-blocking)
        user_coords = await _geocode_address(f"{location_query}, Bengaluru, India")

        if user_coords is None:
            return f"I couldn't find {location_query} on the map. Could you tell me the area name again?"

        # Calculate distance
        distance_km = _haversine_km(user_coords["lat"], user_coords["lng"], prop_lat, prop_lng)
        logger.info(f"[LOCATION] {location_query} is {distance_km:.1f}km from Truliv Luna")

        # Update location preference
        update_cached_context(user_id, {
            "context_data.botLocationPreference": location_query,
        })

        MAX_DISTANCE_KM = 10

        if distance_km <= MAX_DISTANCE_KM:
            return (
                f"Oh that's great! {location_query} is really close to our property, Truliv Luna. "
                f"It's just about {distance_km:.0f} kilometers away. "
                f"I think it would be perfect for you! Would you like to know more about it?"
            )
        else:
            return (
                f"Hmm, so {location_query} is about {distance_km:.0f} kilometers from our property Truliv Luna. "
                f"We don't have a PG right in {location_query} unfortunately. "
                f"But Truliv Luna is a really wonderful property and well connected by transport. "
                f"Would you be open to considering it? A lot of our residents commute from nearby areas and they love it here."
            )

    except Exception as e:
        logger.error(f"[TOOL-ERROR] check_location_proximity failed | Error: {str(e)}", exc_info=True)
        return "I couldn't check the location right now. But we have a lovely property called Truliv Luna in Bengaluru. Would you like to know more?"
