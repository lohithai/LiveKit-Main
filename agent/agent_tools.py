import os
import json
import asyncio
import time
from typing import Dict, List, Optional
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime
import traceback

import aiohttp
import requests
from dotenv import load_dotenv

from logger import logger
from sheets_client import get_sheet_as_dataframe_async
from database import get_async_context_collection
from lead_sync import sync_user_to_leadsquared
from task_queue import bg_tasks
from helpers.warden_corn_api import WardenAPI

from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()

# ==================== Warden API Configuration ====================

# Load Warden API credentials
cred_file = os.getenv("GOOGLE_SERVICE_CRED")
credentials = json.loads(cred_file) if cred_file else {}

# Warden API Configuration
WARDEN_API_BASE_URL = os.getenv("WARDEN_API_BASE_URL", "https://truliv-cron-job.vercel.app/api")
WARDEN_API_KEY = os.getenv("WARDEN_API_KEY")

# Initialize Warden API Helper (lazy — may be None if env vars missing)
WardenHelper = None
if WARDEN_API_KEY and WARDEN_API_BASE_URL:
    try:
        WardenHelper = WardenAPI(
            api_key=WARDEN_API_KEY,
            base_url=WARDEN_API_BASE_URL,
        )
    except ValueError as e:
        logger.warning(f"WardenAPI not initialized: {e}")

# Global properties cache
properties_data_cache: Optional[List[Dict]] = None
sheet_properties_cache: Optional[List[Dict]] = None
_properties_lock = asyncio.Lock()

# ==================== User Context Cache ====================
# In-memory cache to reduce DB calls during a call session
# Structure: {user_id: {"context_data": {...}, "dirty": False, "pending_updates": {}}}

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

    # Update the cache
    for key, value in updates.items():
        # Handle nested keys like "context_data.botProfession"
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

# ==================== CACHE SHEET DATA ======================

async def get_properties_data_from_sheet() -> Optional[List[Dict]]:
    """Get properties data from cache or load from Google Sheets if not cached."""
    global sheet_properties_cache

    if sheet_properties_cache is not None:
        return sheet_properties_cache

    try:
        logger.info("Loading properties data from Google Sheets...")
        SHEET_ID = "1WkibURDCu8cXJ6msmEvtwhsatCA8YWTQSNBDWuFrd-k"
        SHEET_NAME = "Sheet1"

        properties_df = await get_sheet_as_dataframe_async(SHEET_NAME, SHEET_ID)

        if properties_df is None or properties_df.empty:
            logger.error("Failed to load property data from Google Sheets")
            return None

        sheet_properties_cache = properties_df
        logger.info(f"Loaded {len(properties_df)} properties from Google Sheets successfully.")

        return sheet_properties_cache

    except Exception as e:
        logger.error(f"Error loading properties data: {str(e)}")
        traceback.print_exc()
        return None

# async def main():
#     print("\nFirst call (loads from sheet):")
#     data = await get_properties_data_from_sheet()
#     print(data)

# # ---------------------------
# # Entry point
# # ---------------------------
# if __name__ == "__main__":
#     asyncio.run(main())

# ==================== Warden API Helper Functions ====================

async def load_properties_once():
    """
    Load properties from Warden API once and cache globally.
    This should be called at startup or before first use.
    """
    global properties_data_cache

    async with _properties_lock:
        if properties_data_cache is not None:
            return

        try:
            logger.info("Fetching properties from Warden API (one-time async load)...")

            response = await WardenHelper.get_properties()
            data = response.get("data", [])

            properties_data_cache = data
            logger.info(f"Loaded {len(data)} properties successfully.")

            return properties_data_cache

        except Exception as e:
            logger.error(f"Error fetching properties: {str(e)}")
            traceback.print_exc()


def get_properties_id_from_name(property_name: str) -> Optional[int]:
    """
    Map property name to Warden property ID using cached data.
    Uses fuzzy matching to handle slight variations in property names.

    Args:
        property_name: Property name (e.g., "Truliv Amara")

    Returns:
        int: Property ID or None if not found
    """
    if not properties_data_cache:
        logger.warning("Properties cache not loaded.")
        return None

    search_name = property_name.strip().lower()

    # Exact match
    for property_data in properties_data_cache:
        name = property_data.get("name", "")
        if name.lower() == search_name:
            property_id = property_data.get("id")
            logger.info(f"[WARDEN-API] Found exact match: {search_name} -> ID {property_id}")
            return property_id

    # Partial match
    for property_data in properties_data_cache:
        name = property_data.get("name", "")
        if search_name in name.lower() or name.lower() in search_name:
            property_id = property_data.get("id")
            logger.info(f"[WARDEN-API] Found partial match: {search_name} -> {name} (ID {property_id})")
            return property_id

    logger.warning(f"[WARDEN-API] No property found matching: {search_name}")
    return None


async def get_room_types_by_property_name(property_name: str) -> Optional[List[Dict]]:
    """
    Fetch room types for a specific property from Warden API by property name.

    Args:
        property_name: Property name (e.g., "Truliv Amara")

    Returns:
        List of room type dictionaries or None if error
    """
    try:
        if properties_data_cache is None:
            await load_properties_once()

        property_id = get_properties_id_from_name(property_name)
        if not property_id:
            logger.warning(f"Property ID not found for name: {property_name}")
            return None

        logger.info(f"Fetching room types for property ID: {property_id}")

        response = await WardenHelper.get_room_types(property_id=property_id)
        room_types = response.get("data", [])

        logger.info(f"Fetched {len(room_types)} room types.")
        return room_types

    except Exception as e:
        logger.error(f"Error fetching room types: {str(e)}")
        traceback.print_exc()
        return None


async def get_bed_availability_by_property_name(property_name: str) -> Optional[List[Dict]]:
    """
    Fetch bed availability for a specific property from Warden API by property name.

    Args:
        property_name: Property name (e.g., "Truliv Amara")

    Returns:
        List of availability dictionaries or None if error
    """
    try:
        if properties_data_cache is None:
            await load_properties_once()

        property_id = get_properties_id_from_name(property_name)
        if not property_id:
            logger.warning(f"Property ID not found for name: {property_name}")
            return None

        logger.info(f"Fetching bed availability for property ID: {property_id}")

        response = await WardenHelper.get_bed_availability(property_id=property_id)
        beds = response.get("data", [])

        logger.info(f"Fetched {len(beds)} bed availability records.")
        return beds

    except Exception as e:
        logger.error(f"Error fetching bed availability: {str(e)}")
        traceback.print_exc()
        return None

# ==================== Helper Functions ====================

def geocode_address_google(address: str, api_key: str = None) -> Optional[Dict]:
    """
    Geocode an address using Google Maps Geocoding API.

    Args:
        address: Address, locality, or pincode to geocode
        api_key: Google API key (uses env var GOOGLE_API_KEY if not provided)

    Returns:
        dict: {'lat': float, 'lng': float} or None if geocoding fails
    """
    try:
        if not api_key:
            api_key = os.getenv("GOOGLE_API_KEY")

        if not api_key:
            logger.error("Google API key not found")
            return None

        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            "address": address,
            "key": api_key,
            "region": "in"  # Bias results to India
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        data = response.json()

        if data.get("status") == "OK" and data.get("results"):
            location = data["results"][0]["geometry"]["location"]
            return {"lat": location["lat"], "lng": location["lng"]}
        else:
            logger.warning(f"Geocoding failed for '{address}': {data.get('status')} - {data.get('error_message', '')}")
            return None

    except Exception as e:
        logger.error(f"Error geocoding address '{address}': {e}", exc_info=True)
        return None


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two points on Earth (in km).

    Args:
        lat1, lon1: Latitude and longitude of first point
        lat2, lon2: Latitude and longitude of second point

    Returns:
        float: Distance in kilometers
    """
    R = 6371.0  # Earth radius in kilometers
    φ1, φ2 = radians(lat1), radians(lat2)
    Δφ = radians(lat2 - lat1)
    Δλ = radians(lon2 - lon1)
    a = sin(Δφ / 2)**2 + cos(φ1) * cos(φ2) * sin(Δλ / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

# ==================== Tools ====================

async def find_nearest_property(user_id: str, location_query: str) -> str:
    """
    LOCATION SEARCH - Find properties near a location/area in Chennai.

    WHEN TO CALL:
    - User mentions ANY location: "OMR", "Kodambakkam", "T.Nagar", "Guindy"
    - User mentions a pincode: "600001"
    - User asks "properties near my office in Velachery"

    HOW IT WORKS:
    1. Uses Google Maps API to find the exact coordinates of the location
    2. Calculates distance to ALL Truliv properties
    3. Returns properties in the nearest cluster (area)
    4. Even if location is not in database, finds nearest available properties

    PREREQUISITES (asks one by one if missing):
    - Profession (working/student)
    - Move-in timeline (this month/later)
    - Room type (private/shared)

    Args:
        user_id (str): User's phone number (from context)
        location_query (str): Location/area name like "OMR", "Kodambakkam", "T.Nagar"
                             NOT property names like "Truliv Vesta"

    Returns:
        str: List of nearby properties OR follow-up question if info missing
    """
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"[TOOL-START] find_nearest_property called | Query: {location_query} | User: {user_id}")
    logger.info(f"[{timestamp_str}] [AGENT-LOG] Searching properties near: {location_query}...")

    try:
        # First, check if required prerequisites are collected (use cache first)
        context_data = get_cached_context(user_id)

        if not context_data:
            # Fallback to DB if not cached
            context_collection = await get_async_context_collection()
            user_doc = await context_collection.find_one({"_id": user_id})
            if not user_doc:
                logger.error(f"User document not found for {user_id}")
                return "Sorry, something went wrong. Can you tell me which area you are looking at?"
            context_data = user_doc.get("context_data", {})

        # Check prerequisites in order: timeline -> room_type
        # Profession is NOT required — collected opportunistically
        # Return ONLY the first missing question to ensure "one by one" flow

        if not context_data.get("botMoveInPreference"):
            logger.warning(f"[TOOL-WARNING] find_nearest_property missing timeline")
            return "Okay, and when are you planning to move in? This month or later?"

        if not context_data.get("botRoomSharingPreference"):
            logger.warning(f"[TOOL-WARNING] find_nearest_property missing room_type")
            return "Very good. And do you prefer a private room or shared room?"

        # Continue with location processing
        # Load property data from Google Sheets
        # SHEET_ID = "188J7Mmf4ZS080jJzzxtf3C2AgWvGx012DTLY_aOBcA8"
        SHEET_ID = "1WkibURDCu8cXJ6msmEvtwhsatCA8YWTQSNBDWuFrd-k"
        SHEET_NAME = "Sheet1"

        if sheet_properties_cache is not None:
            properties_df = sheet_properties_cache
            logger.info("Loaded properties data from cache.")
        else:
            properties_df = await get_properties_data_from_sheet()

        if properties_df is None or properties_df.empty:
            logger.error("Failed to load property data from Google Sheets")
            return "Sorry, I'm having trouble loading property data right now. Can you try again in a moment?"

        # Geocode the user's location
        location = geocode_address_google(location_query + ", Chennai, India")

        if location is None:
            return "I couldn't find that location. Can you tell me the area name or pincode again?"

        # ── Validate: location must be within Chennai (50km from city center) ──
        CHENNAI_CENTER_LAT = 13.0827
        CHENNAI_CENTER_LNG = 80.2707
        MAX_DISTANCE_KM = 50  # Chennai metro area radius

        user_lat = location['lat']
        user_lng = location['lng']

        distance_from_chennai = haversine_distance(
            CHENNAI_CENTER_LAT, CHENNAI_CENTER_LNG, user_lat, user_lng
        )

        if distance_from_chennai > MAX_DISTANCE_KM:
            logger.warning(
                f"[TOOL-WARNING] Location outside Chennai: {location_query} "
                f"({user_lat}, {user_lng}) is {distance_from_chennai:.1f}km from Chennai center"
            )
            return (
                f"Sorry, {location_query} is not in our serviceable area. "
                f"Currently, Truliv properties are available only in Chennai. "
                f"Could you tell me an area within Chennai?"
            )

        logger.info(f"Location validated: {location_query} is {distance_from_chennai:.1f}km from Chennai center")

        properties_df['distance_km'] = properties_df.apply(
            lambda row: haversine_distance(
                user_lat,
                user_lng,
                float(row['Lat']),
                float(row['Long'])
            ),
            axis=1
        )

        # Step 1: Find the nearest property to get the cluster
        nearest_property = properties_df.loc[properties_df['distance_km'].idxmin()]
        cluster = nearest_property['Cluster']

        logger.info(f"Nearest property cluster: {cluster}")
        logger.info(f"[{timestamp_str}] [AGENT-LOG] Location mapped to cluster: {cluster}")

        # Helper function to extract unique properties from a dataframe
        def extract_unique_properties(df):
            """Extract unique properties with their details from a dataframe"""
            unique_props = []
            for prop_name in df['Property Name'].unique():
                prop_rows = df[df['Property Name'] == prop_name]
                first_row = prop_rows.iloc[0]

                # Calculate min and max price across all configs
                prices = prop_rows['Price'].astype(str).str.replace(',', '').astype(float)
                min_price = prices.min()
                max_price = prices.max()

                # Extract drive folder ID from Image link
                image_link = first_row.get('Image link', '')
                drive_folder_id = ''
                if 'drive.google.com/drive/folders/' in image_link:
                    try:
                        drive_folder_id = image_link.split('drive.google.com/drive/folders/')[1]
                    except:
                        drive_folder_id = image_link

                unique_props.append({
                    'property_name': prop_name,
                    'location': first_row['Location'],
                    'distance_km': first_row['distance_km'],
                    'cluster': first_row['Cluster'],
                    'min_price': f"{int(min_price):,}",
                    'max_price': f"{int(max_price):,}",
                    'template_image_link': first_row.get('Template_Image_Link', 'https://gallabox.com/gallabox-card.png').strip(),
                    'drive_folder_id': drive_folder_id
                })
            return unique_props

        # Step 2: Get properties from the nearest cluster
        cluster_properties = properties_df[properties_df['Cluster'] == cluster].copy()
        cluster_unique = extract_unique_properties(cluster_properties)
        cluster_unique.sort(key=lambda x: x['distance_km'])

        # Step 3: If cluster has fewer than 7 properties, fill with nearest from other clusters
        if len(cluster_unique) < 7:
            logger.info(f"Cluster {cluster} has only {len(cluster_unique)} properties. Filling with nearby properties from other clusters.")

            # Get all properties from other clusters
            other_clusters_df = properties_df[properties_df['Cluster'] != cluster].copy()
            other_unique = extract_unique_properties(other_clusters_df)
            other_unique.sort(key=lambda x: x['distance_km'])

            # Calculate how many more we need
            needed = 7 - len(cluster_unique)

            # Combine: cluster properties first (in their proximity order),
            # then nearest from other clusters (in their proximity order)
            # DO NOT re-shuffle - maintain cluster priority
            top_7_properties = cluster_unique + other_unique[:needed]

            logger.info(f"Combined {len(cluster_unique)} from cluster '{cluster}' (sorted by proximity) + {len(other_unique[:needed])} from nearby clusters (sorted by proximity) = {len(top_7_properties)} total")
        else:
            # Cluster has 7 or more properties, just take top 7 from cluster
            top_7_properties = cluster_unique[:7]
            logger.info(f"Found {len(top_7_properties)} properties in cluster '{cluster}' (sorted by proximity)")

        # LOGGING FOR DEBUGGING
        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # logger.info(f"[{timestamp_str}] [AGENT-LOG] Found {len(top_7_properties)} properties near {location_query}")
        for p in top_7_properties:
            logger.info(f"   - {p['property_name']} ({p['location']}): {p['min_price']} - {p['max_price']}")

        # Update cache with discovered location info (will be flushed to DB at end of call)
        update_cached_context(user_id, {
            "context_data.botLocationPreference": location_query,
            "context_data.cluster": cluster
        })
        logger.info(f"[CACHE] Updated location for {user_id}: cluster={cluster}, location={location_query}")

        # Build property list for voice response - Indian style
        property_names = [prop['property_name'] for prop in top_7_properties[:5]]  # Limit to 5 for voice

        if len(property_names) == 1:
            return f"I found {property_names[0]} near {location_query}. Would you like to know more about it?"
        elif len(property_names) <= 3:
            names_str = ", ".join(property_names[:-1]) + " and " + property_names[-1]
            return f"Actually, I found {names_str} near {location_query}. Which one interests you?"
        else:
            return f"Very good! I have {len(top_7_properties)} options near {location_query}. Some good ones are {property_names[0]}, {property_names[1]}, and {property_names[2]}. Which one would you like to know about?"

    except Exception as e:
        traceback.print_exc()
        logger.error(f"[TOOL-ERROR] find_nearest_property failed | Query: {location_query} | Error: {str(e)}", exc_info=True)
        return "Sorry, I couldn't search that area. Can you tell me the location again?"

async def properties_according_to_budget(user_id: str, budget_query: str) -> str:
    """
    BUDGET SEARCH - Find properties within user's budget.

    WHEN TO CALL:
    - User mentions specific budget:
        "My budget is 8000"
        "Looking around 10k"
        "Budget between 7000 to 9000"
        "I can spend max 12000"

    HOW IT WORKS:
    1. Extracts budget amount from user query
    2. Filters properties within budget
    3. If cluster already selected -> prioritize that cluster
    4. Returns best matching properties

    PREREQUISITES (asks one by one if missing):
    - Profession (working/student)
    - Move-in timeline (this month/later)
    - Room type (private/shared)

    Args:
        user_id (str): User phone number
        budget_query (str): Budget sentence from user

    Returns:
        str: Matching properties OR follow-up question
    """

    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"[TOOL-START] properties_according_to_budget | Query: {budget_query} | User: {user_id}")

    try:
        # -----------------------------
        # STEP 1: Load Context (Cache First)
        # -----------------------------
        context_data = get_cached_context(user_id)

        if not context_data:
            context_collection = await get_async_context_collection()
            user_doc = await context_collection.find_one({"_id": user_id})
            if not user_doc:
                return "Sorry, something went wrong. Can you tell me your budget again?"
            context_data = user_doc.get("context_data", {})

        # -----------------------------
        # STEP 2: Check Prerequisites (One by One)
        # -----------------------------
        # Profession is NOT required — collected opportunistically
        if not context_data.get("botMoveInPreference"):
            return "When are you planning to move in? This month or later?"

        if not context_data.get("botRoomSharingPreference"):
            return "Do you prefer a private room or shared room?"

        # -----------------------------
        # STEP 3: Extract Budget
        # -----------------------------
        import re

        numbers = re.findall(r"\d+", budget_query.replace(",", ""))
        if not numbers:
            return "Can you please tell me your budget in numbers? For example, 8000 or 10000."

        budget = int(numbers[0])
        logger.info(f"Extracted budget: {budget}")

        # -----------------------------
        # STEP 4: Load Properties
        # -----------------------------
        if sheet_properties_cache is not None:
            properties_df = sheet_properties_cache
        else:
            properties_df = await get_properties_data_from_sheet()

        if properties_df is None or properties_df.empty:
            return "Sorry, I'm unable to fetch property data right now. Please try again shortly."

        # Clean price column
        properties_df['Price'] = (
            properties_df['Price']
            .astype(str)
            .str.replace(',', '')
            .astype(float)
        )

        # -----------------------------
        # STEP 5: Filter by Budget
        # -----------------------------
        filtered_df = properties_df[properties_df['Price'] <= budget].copy()

        if filtered_df.empty:
            return f"I couldn't find any properties under {budget:,}. Would you like to slightly increase your budget?"

        # -----------------------------
        # STEP 6: Prioritize Cluster (if exists)
        # -----------------------------
        cluster = context_data.get("cluster")

        if cluster:
            cluster_df = filtered_df[filtered_df['Cluster'] == cluster]
            if not cluster_df.empty:
                filtered_df = cluster_df
                logger.info(f"Prioritizing cluster: {cluster}")

        # -----------------------------
        # STEP 7: Extract Unique Properties
        # -----------------------------
        unique_props = []

        for prop_name in filtered_df['Property Name'].unique():
            prop_rows = filtered_df[filtered_df['Property Name'] == prop_name]
            first_row = prop_rows.iloc[0]

            prices = prop_rows['Price']
            min_price = prices.min()
            max_price = prices.max()

            unique_props.append({
                "property_name": prop_name,
                "min_price": int(min_price),
                "max_price": int(max_price),
                "location": first_row['Location']
            })

        # Sort by price ascending
        unique_props.sort(key=lambda x: x['min_price'])

        top_properties = unique_props[:5]

        logger.info(f"Found {len(top_properties)} properties within budget {budget}")

        # -----------------------------
        # STEP 8: Voice-Friendly Response
        # -----------------------------
        names = [p['property_name'] for p in top_properties]

        if len(names) == 1:
            return f"Good news! I found {names[0]} within your budget of {budget:,}. Would you like more details about it?"

        elif len(names) <= 3:
            names_str = ", ".join(names[:-1]) + " and " + names[-1]
            return f"I found {names_str} within your budget of {budget:,}. Which one would you like to explore?"

        else:
            return (
                f"Very good! I found {len(unique_props)} properties within {budget:,}. "
                f"Some good options are {names[0]}, {names[1]}, and {names[2]}. "
                f"Which one interests you?"
            )

    except Exception as e:
        traceback.print_exc()
        logger.error(f"[TOOL-ERROR] properties_according_to_budget failed | Error: {str(e)}")
        return "Sorry, I couldn't search based on your budget. Can you tell me the budget again?"





async def schedule_site_visit(
    user_phone: str,
    visit_date: str,
    visit_time: str,
    name: Optional[str] = None
) -> str:
    """
    Schedule a site visit for the user at a specific date and time.

    CRITICAL - WHEN TO CALL THIS TOOL:
    - ONLY call when user has confirmed BOTH specific date AND specific time
    - If user response is vague ("now", "soon", "whenever") or incomplete (only date OR only time), get confirmation before calling tool
    - Don't assume times for vague phrases like "morning" or "evening" - ask for specific time
    - MANDATORY: You MUST have the user's name before scheduling. If you don't know it, ASK for it first.

    DATE & TIME CONVERSION:
    You have access to current_date, current_time, and current_day context. Convert natural language to proper format:
    - Dates: Convert "tomorrow", "Monday", "15th December" etc. to YYYY-MM-DD format
    - Times: Convert "2 PM", "10:30 AM" etc. to HH:MM 24-hour format
    - NEVER ask user for YYYY-MM-DD or HH:MM format - YOU handle the conversion

    This tool saves the visit details to MongoDB fields:
    - visit_date -> botSvDate (format: YYYY-MM-DD after conversion)
    - visit_time -> botSvTime (format: HH:MM in 24-hour format after conversion)
    - name -> name (if provided)

    Args:
        user_phone (str): User's phone number/ID (available in context - do not ask user)
        visit_date (str): Date in YYYY-MM-DD format (YOU convert from natural language)
        visit_time (str): Time in HH:MM 24-hour format (YOU convert from 12-hour/natural language)
        name (str, optional): User's name. REQUIRED if not already in context.

    Returns:
        str: Confirmation message with visit details
    """
    logger.info(f"[TOOL-START] schedule_site_visit called | User: {user_phone} | Date: {visit_date} | Time: {visit_time} | Name: {name}")

    try:
        # Parse and validate date format
        from datetime import datetime
        now = datetime.now()

        try:
            parsed_date = datetime.strptime(visit_date, "%Y-%m-%d")
            formatted_date = visit_date
        except ValueError:
            logger.warning(f"[TOOL-WARNING] Invalid date format received: {visit_date}")
            return f"Invalid date format received. Please use date format like 2026-01-15."

        # ── Validate: reject past dates immediately (before even checking time) ──
        if parsed_date.date() < now.date():
            logger.warning(f"[TOOL-WARNING] Past date rejected: {visit_date}")
            return "That date has already passed. Could you give me a future date for the visit?"

        # Parse and validate time format (accept both 12-hour and 24-hour formats)
        parsed_time = None
        time_formats = [
            "%H:%M",       # 14:00
            "%I:%M %p",    # 10:00 AM
            "%I:%M%p",     # 10:00AM
            "%I %p",       # 10 AM
            "%I%p",        # 10AM
        ]

        for fmt in time_formats:
            try:
                parsed_time = datetime.strptime(visit_time.strip().upper(), fmt)
                break
            except ValueError:
                continue

        if parsed_time is None:
            logger.warning(f"[TOOL-WARNING] Invalid time format received: {visit_time}")
            return f"Invalid time format received. Please provide time like 10 AM or 2:30 PM."

        # Convert to 24-hour format for storage
        formatted_time = parsed_time.strftime("%H:%M")

        # ── Validate: visiting hours 9 AM – 8 PM ──
        visit_hour = parsed_time.hour
        if visit_hour < 9 or visit_hour >= 20:
            logger.warning(f"[TOOL-WARNING] Outside visiting hours: {visit_time}")
            return "Our visiting hours are nine A.M. to eight P.M., any day of the week. Could you pick a time within that window?"

        # ── Validate: if today, reject past times ──
        if parsed_date.date() == now.date():
            visit_datetime = parsed_date.replace(
                hour=parsed_time.hour, minute=parsed_time.minute
            )
            if visit_datetime < now:
                logger.warning(f"[TOOL-WARNING] Past time rejected for today: {visit_time}")
                return "That time has already passed for today. Could you pick a later time, or schedule for tomorrow?"

        # Build update fields for visit details
        update_fields = {
            "context_data.botSvDate": formatted_date,
            "context_data.botSvTime": formatted_time
        }

        if name:
            update_fields["context_data.name"] = name
        else:
            # Check if name exists in cache first
            cached_context = get_cached_context(user_phone)
            existing_name = cached_context.get("name") if cached_context else None

            if not existing_name or existing_name in ["Voice User", "User", "Unknown", ""]:
                return "I need your name to schedule the visit. May I know your name please?"

        # Update cache (will be flushed to DB at end of call)
        update_cached_context(user_phone, update_fields)

        # Format date nicely for voice - Indian style
        display_date = parsed_date.strftime("%d %B")  # e.g., "15 January"
        display_time = parsed_time.strftime("%I:%M %p").lstrip("0")  # e.g., "2:30 PM"

        logger.info(f"[TOOL-END] schedule_site_visit | User: {user_phone} | Visit scheduled: {formatted_date} at {formatted_time}")

        return f"Perfect! Your visit is confirmed for {display_date} at {display_time}. Our team will be there to show you around. Looking forward to seeing you!"

    except Exception as e:
        logger.error(f"[TOOL-ERROR] schedule_site_visit failed | User: {user_phone} | Error: {str(e)}", exc_info=True)
        return "Sorry, couldn't book that. Let me try again - what date and time works for you?"


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
    """
    Update user profile fields in MongoDB based on extracted information from conversation.
    Can update multiple fields at once. Only updates fields that are provided (non-None).

    WHEN TO CALL THIS TOOL:

    CRITICAL: Call this tool IMMEDIATELY when user mentions profile information - DO NOT just say "let me update that" without actually calling the tool!

    What to extract and save:
    1. Profession - User mentions if they're working or studying -> profession="working" or "studying"
    2. Move-in timeline - User mentions when they plan to move -> timeline="this_month" or "one_to_two_months" or "more_than_two_months"
    3. Room preference - User mentions room type preference -> room_type="private" or "shared"
    4. Property interest - User shows explicit interest in specific property -> property_preference="Property Name"
    5. Budget - User mentions their budget range -> budget="budget_range" (save as mentioned by user)
    6. Name - User mentions their name -> name="User Name"
    7. Phone Number - User mentions their phone number -> phone_number="9876543210"

    Important behavior:
    - Call this tool FIRST before other tools when user provides profile info
    - Update silently in background - NEVER tell user you're saving their data
    - Can update multiple fields in one call
    - If user changes their mind, call again to update
    - If user is interested change their preference suddenly, save the property name as-is
    - Extract intent from what user says, normalize to the allowed values above
    - CRITICAL: Return a very short confirmation string. DO NOT repeat the updated fields in the conversation unless asked.

    Args:
        user_id (str): User's ID (available in context - do not ask user)
        profession (str, optional): User's profession - "working" or "studying"
        timeline (str, optional): Move-in timeline - "this_month", "one_to_two_months", or "more_than_two_months"
        room_type (str, optional): Room preference - "private" or "shared"
        property_preference (str, optional): Property name when user shows interest (e.g., "Truliv Olympus")
        budget (str, optional): User's budget range as mentioned (e.g., "10000-15000", "under 20000", "around 12000")
        name (str, optional): User's name if mentioned.
        phone_number (str, optional): User's phone number if mentioned.

    Returns:
        str: Confirmation of what was updated
    """
    logger.info(f"[TOOL-START] update_user_profile called | User: {user_id} | profession={profession}, timeline={timeline}, room_type={room_type}, property={property_preference}, name={name}, phone={phone_number}")

    try:
        # Build update dictionary only for provided fields
        update_fields = {}
        updated_items = []

        if phone_number is not None:
            # Basic cleaning of phone number
            clean_phone = "".join(filter(str.isdigit, phone_number))
            if len(clean_phone) >= 10:
                # Take last 10 digits
                clean_phone = clean_phone[-10:]
                update_fields["context_data.phoneNumber"] = clean_phone
                updated_items.append(f"Phone: {clean_phone}")
            else:
                # If it looks invalid, still save it but maybe log a warning?
                # For now, just save what we got if it's not empty
                if clean_phone:
                    update_fields["context_data.phoneNumber"] = clean_phone
                    updated_items.append(f"Phone: {clean_phone}")

        if profession is not None:
            # Normalize profession
            prof_lower = profession.lower()
            if "work" in prof_lower or "job" in prof_lower or "employ" in prof_lower or "office" in prof_lower or "professional" in prof_lower or "engineer" in prof_lower:
                update_fields["context_data.botProfession"] = "working"
                updated_items.append("Profession: working")
            elif "stud" in prof_lower or "college" in prof_lower or "university" in prof_lower:
                update_fields["context_data.botProfession"] = "studying"
                updated_items.append("Profession: studying")
            else:
                update_fields["context_data.botProfession"] = profession
                updated_items.append(f"Profession: {profession}")

        if timeline is not None:
            # Normalize timeline
            timeline_lower = timeline.lower()
            if "immediate" in timeline_lower or "this month" in timeline_lower or "asap" in timeline_lower or "now" in timeline_lower:
                update_fields["context_data.botMoveInPreference"] = "this_month"
                updated_items.append("Timeline: this month")
            elif "next month" in timeline_lower or "1-2" in timeline_lower or "one to two" in timeline_lower or "6 week" in timeline_lower:
                update_fields["context_data.botMoveInPreference"] = "one_to_two_months"
                updated_items.append("Timeline: 1-2 months")
            elif "later" in timeline_lower or "after 2" in timeline_lower or "more than" in timeline_lower or "3 month" in timeline_lower:
                update_fields["context_data.botMoveInPreference"] = "more_than_two_months"
                updated_items.append("Timeline: more than 2 months")
            else:
                update_fields["context_data.botMoveInPreference"] = timeline
                updated_items.append(f"Timeline: {timeline}")

        if room_type is not None:
            # Normalize room type
            room_lower = room_type.lower()
            if "private" in room_lower or "single" in room_lower or "1" in room_lower:
                update_fields["context_data.botRoomSharingPreference"] = "private"
                updated_items.append("Room type: private")
            elif "shared" in room_lower or "double" in room_lower or "triple" in room_lower or "2" in room_lower or "3" in room_lower:
                update_fields["context_data.botRoomSharingPreference"] = "shared"
                updated_items.append("Room type: shared")
            else:
                update_fields["context_data.botRoomSharingPreference"] = room_type
                updated_items.append(f"Room type: {room_type}")

        if property_preference is not None:
            # Save property preference as-is (property name)
            update_fields["context_data.botPropertyPreference"] = property_preference
            updated_items.append(f"Property: {property_preference}")

        if budget is not None:
            # Save budget as mentioned by user
            update_fields["context_data.botBudget"] = budget
            updated_items.append(f"Budget: {budget}")

        if name is not None:
            update_fields["context_data.name"] = name
            updated_items.append(f"Name: {name}")

        # Only update if we have fields to update
        if not update_fields:
            logger.warning(f"[TOOL-WARNING] update_user_profile called but no fields provided to update | User: {user_id}")
            return "No profile information was provided to update."

        # Update in-memory cache (will be flushed to DB at end of call)
        update_cached_context(user_id, update_fields)

        logger.info(f"[TOOL-END] update_user_profile | User: {user_id} | Cached: {', '.join(updated_items)}")

        # Return minimal response - model should continue conversation naturally
        return "OK"

    except Exception as e:
        logger.error(f"[TOOL-ERROR] update_user_profile failed | User: {user_id} | Error: {str(e)}", exc_info=True)
        return "Error updating profile"




async def explore_more_properties(
    user_id: str,
    exclude_properties: Optional[List[str]] = None
) -> str:
    """
    Show MORE properties in the same area, excluding ones already mentioned,
    and only if rooms/beds are AVAILABLE (live check).
    """

    logger.info(f"[TOOL-START] explore_more_properties | User: {user_id} | Exclude: {exclude_properties}")

    try:
        # -------------------- Get user context --------------------
        context_data = get_cached_context(user_id)

        if not context_data:
            context_collection = await get_async_context_collection()
            user_doc = await context_collection.find_one({"_id": user_id})
            if not user_doc:
                return "I need to know your preferred area first. Which location in Chennai are you looking at?"
            context_data = user_doc.get("context_data", {})

        cluster = context_data.get("cluster")
        room_preference = context_data.get("botRoomSharingPreference")
        location_preference = context_data.get("botLocationPreference")

        if not cluster:
            return "I need to know your preferred area first. Which location in Chennai are you looking at?"

        # -------------------- Load Warden properties (once) --------------------
        await load_properties_once()

        # -------------------- Load Google Sheet --------------------
        SHEET_ID = "1WkibURDCu8cXJ6msmEvtwhsatCA8YWTQSNBDWuFrd-k"
        SHEET_NAME = "Sheet1"

        properties_df = await get_sheet_as_dataframe_async(SHEET_NAME, SHEET_ID)
        if properties_df is None or properties_df.empty:
            return "I'm having trouble loading property data. Can you try again in a moment?"

        # -------------------- Filter by cluster --------------------
        cluster_properties = properties_df[
            properties_df['Cluster'].str.upper() == cluster.upper()
        ].copy()

        if cluster_properties.empty:
            return "I couldn't find properties in that area. Would you like to try a different location?"

        unique_properties = cluster_properties['Property Name'].unique().tolist()

        if exclude_properties:
            exclude_normalized = [p.strip().lower() for p in exclude_properties]
            unique_properties = [
                p for p in unique_properties if p.lower() not in exclude_normalized
            ]

        if not unique_properties:
            area_name = location_preference or "this area"
            return f"You've seen all the properties in {area_name}. Would you like to explore other areas?"

        # -------------------- Build results (WITH AVAILABILITY CHECK) --------------------
        results = []

        for prop_name in unique_properties:

            # Get property ID from Warden cache
            property_id = get_properties_id_from_name(prop_name)
            if not property_id:
                logger.info(f"Skipping {prop_name} - no Warden property ID")
                continue

            # LIVE availability check - FIXED LOGIC
            try:
                bed_resp = await get_bed_availability_by_property_name(property_name=prop_name)

                logger.info(f"""
                ++++++++++++++++++++++++++++++++++
                Property: {prop_name}
                ===============================================
                get_bed_availability_by_property_name -> {bed_resp}
                =================================================
""")

                # The API returns a LIST directly: [{"propertyId": X, "availability": [...]}]
                if not bed_resp or not isinstance(bed_resp, list):
                    logger.info(f"Skipping {prop_name} - no availability data returned")
                    continue

                # Get the first property's availability array
                property_availability = bed_resp[0].get("availability", []) if bed_resp else []

                # Check if ANY room type has available beds and collect availability info
                has_available_beds = False
                availability_summary = []

                for room_type_avail in property_availability:
                    available_beds = room_type_avail.get("availableBeds", 0)
                    room_type_name = room_type_avail.get("roomTypeName", "Unknown")

                    if available_beds > 0:
                        has_available_beds = True
                        availability_summary.append({
                            "room_type": room_type_name,
                            "available_beds": available_beds
                        })
                        logger.info(f"{prop_name} - {room_type_name} has {available_beds} beds available")

                if not has_available_beds:
                    logger.info(f"Skipping {prop_name} - no available beds in any room type")
                    continue

            except Exception as api_err:
                logger.error(f"Availability check failed for {prop_name}: {api_err}")
                continue

            # Simply collect the property with its availability
            results.append({
                "name": prop_name,
                "availability": availability_summary,
            })

        # -------------------- Final response --------------------
        if not results:
            area_name = location_preference or "this area"
            if room_preference:
                return f"Those are all the {room_preference} rooms currently available in {area_name}. Want me to check other areas?"
            return f"Those are all the available options in {area_name}. Would you like to explore other locations?"

        area_name = location_preference or "this area"
        logger.info(f"[TOOL-END] explore_more_properties | Found {len(results)} available properties")

        # Build simple response with property names and availability
        response_parts = [f"Here are more available properties in {area_name}:\n"]

        for idx, prop in enumerate(results, 1):
            response_parts.append(f"\n{idx}. {prop['name']}")

            # Add availability information
            if prop.get('availability'):
                for avail in prop['availability']:
                    response_parts.append(f"   - {avail['room_type']}: {avail['available_beds']} beds available")

        response_parts.append(f"\n\nWhich property interests you?")

        return "\n".join(response_parts)

    except Exception as e:
        logger.error(f"[TOOL-ERROR] explore_more_properties failed | {e}", exc_info=True)
        return "Sorry, I couldn't load more options right now. Which area were you looking at?"



async def zero_deposit(query: str) -> str:
    """
    Answer questions ONLY about Truliv's Zero-Deposit option powered by CirclePe.

    CRITICAL - WHEN TO CALL THIS TOOL:
    Call this tool ONLY when user asks if there is a zero deposit ALTERNATIVE/OPTION:
    - "Is there any zero deposit option?"
    - "Do you have zero deposit?"
    - "What about zero deposit?"
    - "Any zero deposit alternative?"
    - User explicitly mentions "zero deposit" or "CirclePe" in their question

    DO NOT CALL THIS TOOL when user asks general deposit questions:
    - "What is deposit?" -> Answer with Regular Deposit Policy (NOT this tool)
    - "How much is deposit?" -> Answer with Regular Deposit Policy (NOT this tool)
    - "What is advance?" -> Answer with Regular Deposit Policy (NOT this tool)
    - "Do I need to pay deposit?" -> Answer with Regular Deposit Policy (NOT this tool)

    This tool should ONLY be invoked when user is specifically asking about the zero deposit option,
    not when asking about deposit in general.

    Args:
        query (str): User's specific question about zero deposit option

    Returns:
        str: Factual answer from CirclePe terms and conditions
    """
    logger.info(f"[TOOL-START] zero_deposit called | Query: {query}")

    try:
        # Zero-deposit information context
        zero_deposit_info = """
Truliv now provides a Zero-Deposit Move-In option through our partner CirclePe, designed to remove the upfront financial burden of renting
CirclePe Zero-Deposit Move-In Program

**How It Works:**
CirclePe pays Truliv your entire selected term's rent (3, 4, 5...11 months) on your behalf on Day 1. This removes the deposit requirement for eligible tenants. You don't need to pay any deposit or large upfront amount at the time of move-in. You simply repay your monthly rent to CirclePe along with a small 2.25% platform fee on that rent.

**Important:** This feature is only available to eligible tenants. If interested, we can share the eligibility link.

**Benefits for Tenants:**
Zero Deposit Move-In - Move in without any upfront financial burden.
Quick & Paperless Process - Instant approvals, no waiting or paperwork.
Stress-Free Relocation - Focus on settling in, not finances.
Safe & Transparent - Clear process, no hidden charges.

**CirclePe Terms & Conditions:**

1. Eligibility Criteria:
   - The tenant must be a salaried individual to apply for CirclePe's Zero Security Deposit move-in.
   - Students can apply only if the verification and banking checks are completed through their parents.
   - Self-employed individuals are currently not eligible for this service.
   - Eligibility is determined based on two factors:
     (a) The tenant's credit score, and
     (b) The tenant's monthly income.
   - If the tenant's income is 2x the monthly rent, there is a high (around 90%) chance of approval.

2. Rent Payment & Deduction:
   - Monthly rent will be auto-deducted via e-mandate only.
   - Rent is deducted on the 5th of every month.
   - The monthly rent amount will be converted into EMIs for easy payment.

3. Lock-in & Move-out Conditions:
   - The tenant is not allowed to move out during the lock-in period because rent paid by CirclePe to the owner on behalf of the tenant is non-refundable.
   - If the tenant vacates the property during this period, they will still be liable to pay the full rent until the lock-in ends.
"""

        # Create prompt for LLM - purely informational
        prompt_template = """
You are providing factual information about Truliv's Zero-Deposit Move-In option powered by CirclePe.

**Instructions:**
- Provide ONLY the information requested in the user's question
- Use ONLY information from the Context below
- Be factual and direct - no conversational filler
- If asked about eligibility, provide the criteria clearly
- If asked about terms, provide the relevant terms
- Keep responses concise and to the point
- Do not add information not asked for

Context:
{context}

User Question: {query}

Factual Answer:
""".strip()

        # Use Gemini to answer the query
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
            temperature=0.2,  # Lower temperature for more factual responses
        )

        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["context", "query"]
        )

        # Format prompt
        formatted_prompt = prompt.format(
            context=zero_deposit_info,
            query=query
        )

        # Get LLM response
        response = await asyncio.to_thread(llm.invoke, formatted_prompt)
        answer = response.content.strip()

        logger.info(f"[TOOL-END] zero_deposit | Generated answer for: {query}")

        return answer if answer else "Unable to generate answer. Please rephrase your question about the zero-deposit option."

    except Exception as e:
        logger.error(f"[TOOL-ERROR] zero_deposit failed | Query: {query} | Error: {str(e)}", exc_info=True)
        return f"Error answering zero-deposit question: {str(e)}"


async def get_property_details_from_sheet(property_name: str) -> Optional[str]:
    """
    Directly fetch property details from Google Sheet by name.
    Used as a primary source or fallback when RAG/Warden fails.
    """
    try:
        SHEET_ID = "1WkibURDCu8cXJ6msmEvtwhsatCA8YWTQSNBDWuFrd-k"
        SHEET_NAME = "Sheet1"

        df = await get_sheet_as_dataframe_async(SHEET_NAME, SHEET_ID)
        if df is None or df.empty:
            return None

        # Normalize property name for search
        search_name = property_name.lower().strip()

        # Filter dataframe
        # We look for partial match in 'Property Name' column
        # Create a mask for matching properties
        mask = df['Property Name'].str.lower().str.contains(search_name, na=False)
        matched_df = df[mask]

        if matched_df.empty:
            return None

        # Get the first matching property name to group by
        # (In case search term matches multiple, we take the first one's full name)
        full_property_name = matched_df.iloc[0]['Property Name']

        # Get all rows for this specific property
        prop_rows = df[df['Property Name'] == full_property_name]

        if prop_rows.empty:
            return None

        # Extract details from first row
        first_row = prop_rows.iloc[0]
        address = first_row.get('Address', 'N/A')
        gmap_link = first_row.get('Gmap Link', 'N/A')
        cluster = first_row.get('Cluster', 'N/A')

        # Build pricing/room info for voice
        room_details = []
        for _, row in prop_rows.iterrows():
            occupancy = row.get('Config') or row.get('Occupancy Type') or 'N/A'
            price = row.get('Price', 'N/A')

            if occupancy != 'N/A' and price != 'N/A':
                # Make it voice-friendly
                room_type = occupancy.replace("Single", "Private room").replace("Double", "Double sharing").replace("Triple", "Triple sharing")
                room_details.append(f"{room_type} at {price} rupees per month")

        # Format voice-friendly output - Indian style with full address
        if room_details:
            pricing_str = ". ".join(room_details[:3])  # Limit to 3 for voice
            if address and address != 'N/A':
                output = f"So {full_property_name} is located at {address}. {pricing_str}. Would you like to visit?"
            else:
                output = f"So {full_property_name} is in {cluster} area. {pricing_str}. Would you like to visit?"
        else:
            if address and address != 'N/A':
                output = f"{full_property_name} is located at {address}. I can give you exact pricing when you visit!"
            else:
                output = f"{full_property_name} is in {cluster} area. I can give you exact pricing when you visit!"

        return output

    except Exception as e:
        logger.error(f"Error in get_property_details_from_sheet: {e}")
        return None

def get_starting_price_from_sheet(property_name: str) -> int:
    """
    Lookup property price from pandas DataFrame cache safely.
    """

    global sheet_properties_cache

    if sheet_properties_cache is None or sheet_properties_cache.empty:
        return 0

    search_name = property_name.strip().lower()

    try:

        # Adjust column names based on your dataframe
        matches = sheet_properties_cache[
            sheet_properties_cache["Property Name"]
            .str.lower()
            .str.contains(search_name, na=False)
        ]

        if matches.empty:
            return 0

        prices = (
            matches["Price"]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("₹", "", regex=False)
            .astype(float)
            .astype(int)
        )

        valid_prices = prices[prices > 0]

        return int(valid_prices.min()) if not valid_prices.empty else 0

    except Exception as e:

        logger.error(f"[SHEET-PRICE-ERROR] {e}")

        return 0



async def query_property_information(
    user_id: str,
    query: str,
    property_name: str
) -> str:
    """
    PROPERTY DETAILS - Get info about a SPECIFIC property by name.
    Uses properties_data_cache instead of Google Sheets.
    """

    start_time = time.time()
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info(
        f"[TOOL-START] query_property_information | "
        f"Query: {query} | Property: {property_name}"
    )

    logger.info(
        f"[{timestamp_str}] [AGENT-LOG] "
        f"Checking property details for {property_name}..."
    )

    try:
        # Step 1: Ensure cache is loaded
        global properties_data_cache

        if properties_data_cache is None:
            logger.info("[PROPERTY-QUERY] Cache empty, loading properties...")
            await load_properties_once()

        if sheet_properties_cache is None:
            logger.info("[PROPERTY-QUERY] Sheet cache empty, loading from Google Sheets...")
            await get_properties_data_from_sheet()

        if not properties_data_cache:
            return "Sorry, property data is not available right now."

        # Step 2: Normalize search
        search_name = property_name.strip().lower()

        # Step 3: Find property in cache
        matched_property = None

        for prop in properties_data_cache:

            prop_name = prop.get("name", "").strip().lower()

            if (
                search_name == prop_name
                or search_name in prop_name
                or prop_name in search_name
            ):
                matched_property = prop
                break

        logger.info(f"Matched property: {matched_property.get('name', 'unknown')} (id={matched_property.get('_id', 'N/A')})")

        # Step 4: If not found
        if not matched_property:
            logger.warning(
                f"[PROPERTY-QUERY] Property not found in cache: {property_name}"
            )

            return (
                f"Sorry, I couldn't find {property_name}. "
                f"Can you please confirm the property name?"
            )

        # Step 5: Extract necessary details from the cached data structure
        name = matched_property.get("name", property_name)

        # Get address from fullAddress field
        address = matched_property.get("fullAddress", "")

        # If fullAddress not available, build from components
        if not address:
            address_parts = [
                matched_property.get("addressStreet", ""),
                matched_property.get("addressCity", ""),
                matched_property.get("addressState", ""),
                matched_property.get("addressPincode", "")
            ]
            address = ", ".join(
                part for part in address_parts if part
            )

        # Get property details
        gender_type = matched_property.get("genders", "Any")
        property_type = matched_property.get("type", "Coliving")
        resident_type = matched_property.get("residentType", "")

        # Get description (strip HTML tags if present)
        description = matched_property.get("description", "")

        # Get location details if available
        location = matched_property.get("location", {})
        area_name = location.get("parentLocationName", matched_property.get("addressCity", ""))
        map_url = location.get("mapUrl", "")

        # Get amenities info
        amenities = matched_property.get("amenities", [])
        amenity_names = [amenity.get("name") for amenity in amenities if amenity.get("name")]

        # Get starting price
        # Get starting price safely (API first, then Sheet fallback)

        starting_price_raw = matched_property.get("startingPrice")

        # Normalize API price
        try:
            starting_price = int(starting_price_raw) if starting_price_raw else 0
        except (TypeError, ValueError):
            starting_price = 0


        # FALLBACK -> lookup in Google Sheet cache
        if starting_price <= 0:

            sheet_price = get_starting_price_from_sheet(name)

            if sheet_price > 0:
                starting_price = sheet_price

                logger.info(
                    f"[PRICE-FALLBACK] Using sheet price for {name}: {starting_price}"
                )


        # Get property status
        property_status = matched_property.get("status", "Live")

        # Step 6: Update user context
        update_cached_context(
            user_id,
            {"context_data.botPropertyPreference": name}
        )

        # Step 7: Build response based on query intent
        query_lower = query.lower()

        if "address" in query_lower or "location" in query_lower:
            response = f"{name} is located at {address}."
            if map_url:
                response += f" You can view the location here: {map_url}"

        elif "price" in query_lower or "rent" in query_lower or "cost" in query_lower:
            if starting_price > 0:
                response = (
                    f"{name} has rooms starting from {starting_price:,}. "
                    f"Would you like to know about single, double, or triple sharing options?"
                )
            else:
                response = (
                    f"I'll check the latest pricing for {name}. "
                    f"Would you like single, double, or triple sharing?"
                )

        elif "amenities" in query_lower or "amenity" in query_lower or "facilities" in query_lower:
            if amenity_names:
                amenities_list = ", ".join(amenity_names[:5])  # Show first 5
                response = (
                    f"{name} is a {gender_type} {property_type} property "
                    f"located in {area_name}. "
                    f"Key amenities include: {amenities_list}"
                )
                if len(amenity_names) > 5:
                    response += f" and {len(amenity_names) - 5} more."
            else:
                response = (
                    f"{name} is a {gender_type} {property_type} property "
                    f"located in {area_name} with modern amenities and fully managed services."
                )

        elif "type" in query_lower or "kind" in query_lower:
            response = (
                f"{name} is a {property_type} property suitable for {resident_type}. "
                f"It's a {gender_type} accommodation."
            )

        elif "description" in query_lower or "about" in query_lower or "details" in query_lower:
            # Strip HTML tags from description for cleaner output
            import re
            clean_description = re.sub('<[^<]+?>', '', description) if description else ""
            clean_description = clean_description.strip()

            if clean_description:
                # Limit description length
                if len(clean_description) > 300:
                    clean_description = clean_description[:300] + "..."
                response = f"{name}: {clean_description}"
            else:
                response = (
                    f"{name} is a {gender_type} {property_type} property "
                    f"located at {address}. "
                    f"It caters to {resident_type}."
                )

        else:
            # General information response
            response = (
                f"{name} is located at {address}. "
                f"It is a {gender_type} {property_type} property suitable for {resident_type}."
            )
            if starting_price > 0:
                response += f" Starting from {starting_price:,}."

        elapsed = time.time() - start_time

        logger.info(
            f"[TOOL-END] query_property_information | "
            f"Found: {name} | Time: {elapsed:.2f}s"
        )

        return response

    except Exception as e:

        safe_error = str(e).replace("{", "{{").replace("}", "}}")

        logger.error(
            f"[TOOL-ERROR] query_property_information failed | "
            f"Error: {safe_error}",
            exc_info=True
        )

        return (
            "Sorry, I couldn't get that property information right now."
        )

async def get_room_types_for_property(
    user_id: str,
    property_name: Optional[str] = None
) -> str:
    """
    Fetch room types and their amenities for a specific property.
    Returns only necessary information: Room type name + amenities.
    """

    logger.info(
        f"[TOOL-START] get_room_types_for_property | "
        f"User: {user_id} | Property: {property_name}"
    )

    try:
        # Ensure properties cache is loaded
        if properties_data_cache is None:
            await load_properties_once()

        if not property_name:
            return "Please tell me which property you'd like to check room types for."

        logger.info(f"[TOOL] Fetching room types for: {property_name}")

        room_types_data = await get_room_types_by_property_name(property_name)

        if not room_types_data:
            return f"I couldn't find room details for {property_name} right now. Would you like to schedule a visit instead?"

        formatted_rooms = []

        for room in room_types_data:
            room_name = room.get("name", "Room")

            # Extract shared amenities
            shared_amenities = [
                amenity.get("name")
                for amenity in room.get("sharedAmenities", [])
                if amenity.get("name")
            ]

            # Extract private amenities
            private_amenities = [
                amenity.get("name")
                for amenity in room.get("privateAmenities", [])
                if amenity.get("name")
            ]

            # Combine & remove duplicates
            all_amenities = list(set(shared_amenities + private_amenities))

            if all_amenities:
                amenities_str = ", ".join(all_amenities[:6])  # limit for voice
                formatted_rooms.append(
                    f"{room_name} includes {amenities_str}"
                )
            else:
                formatted_rooms.append(f"{room_name}")

        logger.info(
            f"[TOOL-END] get_room_types_for_property | "
            f"Found {len(formatted_rooms)} room types for {property_name}"
        )

        if formatted_rooms:
            rooms_str = ". ".join(formatted_rooms[:3])  # limit to 3 room types
            return (
                f"At {property_name}, available room types are: {rooms_str}. "
                f"Would you like pricing details or to schedule a visit?"
            )
        else:
            return f"I couldn't find room configurations for {property_name} right now."

    except Exception as e:
        logger.error(
            f"[TOOL-ERROR] get_room_types_for_property failed | "
            f"Error: {str(e)}",
            exc_info=True
        )
        return "Sorry, I couldn't fetch room details right now. Would you like me to try again?"

# @tool
# async def get_room_availability(
#     user_id: str,
#     property_name: Optional[str] = None,
#     move_in_date: Optional[str] = None,
#     duration_months: int = 6
# ) -> str:
#     """
#     Check real-time bed availability for room types at a specific property.

#     **WHEN TO USE THIS TOOL:**
#     - User asks: "Is Twin Sharing available?", "Do you have beds available?"
#     - User asks: "Any availability in January?", "When can I move in?"
#     - User wants to know if specific room type has vacant beds
#     - Follow-up after viewing room types to check availability

#     **DATE HANDLING:**
#     - If move_in_date not provided, uses user's move-in preference from context (botMoveInPreference)
#     - Converts natural language dates to ISO format automatically
#     - Default duration: 6 months from move-in date
#     - Date format expected: YYYY-MM-DD (tool handles timezone conversion)

#     **CONTEXT USAGE:**
#     - Property name: Uses botPropertyPreference if not provided
#     - Move-in date: Uses botMoveInPreference if not provided
#     - Shows availability for all room types at the property

#     Args:
#         user_id (str): User's phone number (available in context)
#         property_name (str, optional): Property name. Uses context if not provided.
#         move_in_date (str, optional): Move-in date in YYYY-MM-DD format. Uses context if not provided.
#         duration_months (int): Duration in months (default: 6 months)

#     Returns:
#         str: Availability status for each room type OR error message

#     Examples:
#         User: "Is there availability at Truliv Amara in January?"
#         -> get_room_availability("919876543210", property_name="Truliv Amara", move_in_date="2026-01-01")

#         User: "Any beds available?" (after discussing property and timeline)
#         -> get_room_availability("919876543210")  # Uses context
#     """
#     logger.info(f"[AGENT-LOG] Checking bed availability for {property_name}...")
#     logger.info(f"[TOOL-START] get_room_availability | User: {user_id} | Property: {property_name} | Date: {move_in_date}")

#     try:
#         # Ensure properties cache is loaded
#         if properties_data_cache is None:
#             await load_properties_once()

#         context_collection = await get_async_context_collection()
#         user_doc = await context_collection.find_one({"_id": user_id})

#         if not user_doc:
#             logger.error(f"User document not found for {user_id}")
#             return "I need more information. Which property are you interested in and when do you want to move in?"

#         context_data = user_doc.get("context_data", {})

#         # Get property name from context if not provided
#         if not property_name:
#             property_name = context_data.get("botPropertyPreference")
#             if not property_name:
#                 return "Which property would you like to check availability for?"

#         # Get move-in date from context if not provided
#         if not move_in_date:
#             from datetime import datetime, timedelta
#             move_in_pref = context_data.get("botMoveInPreference")
#             if move_in_pref:
#                 # Try to parse move-in preference to date
#                 # Common formats: "Immediate", "Within 1 month", "January", "Next month", etc.

#                 now = datetime.now()

#                 if "immediate" in move_in_pref.lower() or "now" in move_in_pref.lower():
#                     move_in_date = now.strftime("%Y-%m-%d")
#                 elif "1 month" in move_in_pref.lower() or "within a month" in move_in_pref.lower():
#                     move_in_date = (now + timedelta(days=30)).strftime("%Y-%m-%d")
#                 elif "2 month" in move_in_pref.lower():
#                     move_in_date = (now + timedelta(days=60)).strftime("%Y-%m-%d")
#                 elif "3 month" in move_in_pref.lower():
#                     move_in_date = (now + timedelta(days=90)).strftime("%Y-%m-%d")
#                 else:
#                     # Default to today if can't parse
#                     move_in_date = now.strftime("%Y-%m-%d")
#             else:
#                 # Use today as default
#                 move_in_date = datetime.now().strftime("%Y-%m-%d")

#         logger.info(f"[TOOL] Checking availability for: {property_name} from {move_in_date}")

#         # Fetch availability using new API
#         availability_data = await get_bed_availability_by_property_name(property_name)

#         logger.info(f" {"="*10} [TOOL] Received availability data for {property_name}: {availability_data}")

#         if not availability_data:
#             return f"I couldn't check availability for {property_name} right now. Would you like to schedule a visit and check in person?"

#         # Build voice-friendly availability response
#         available_rooms = []
#         booked_rooms = []

#         for avail in availability_data:
#             available_beds = avail.get("availableBeds", 0)
#             room_type_name = avail.get("roomTypeName", "Room")

#             if available_beds > 0:
#                 available_rooms.append(f"{room_type_name} with {available_beds} beds")
#             else:
#                 booked_rooms.append(room_type_name)

#         logger.info(f"[AGENT-LOG] Availability check complete for {property_name}.")
#         logger.info(f"[TOOL-END] get_room_availability | Checked availability for {property_name}")

#         if available_rooms:
#             rooms_str = ", ".join(available_rooms[:3])  # Limit for voice
#             return f"Good news! {property_name} has {rooms_str} available. Would you like to schedule a visit?"
#         else:
#             return f"Actually, {property_name} is fully booked right now. But I can show you similar options nearby. Interested?"

#     except Exception as e:
#         logger.error(f"[TOOL-ERROR] get_room_availability failed | Error: {str(e)}", exc_info=True)
#         return "Sorry, couldn't check availability right now. Would you like to visit and check in person?"

async def get_room_availability(
    user_id: str,
    property_name: Optional[str] = None,
    move_in_date: Optional[str] = None,
    duration_months: int = 6
) -> str:
    """
    Check real-time bed availability for room types at a specific property.
    Returns gender-specific availability when applicable.
    """

    logger.info(f"[AGENT-LOG] Checking bed availability for {property_name}...")
    logger.info(f"[TOOL-START] get_room_availability | User: {user_id} | Property: {property_name} | Date: {move_in_date}")

    try:
        # Ensure properties cache is loaded
        if properties_data_cache is None:
            await load_properties_once()

        if not property_name:
            return "Please tell me which property you'd like to check availability for."

        logger.info(f"[TOOL] Fetching availability from API for: {property_name}")

        # Fetch availability from API
        availability_data = await get_bed_availability_by_property_name(property_name)

        logger.info(f"[TOOL] Received availability data for {property_name}")

        if not availability_data:
            return f"I couldn't check availability for {property_name} right now. Would you like to schedule a visit instead?"

        available_rooms = []
        total_available_count = 0

        for property_data in availability_data:
            for avail in property_data.get("availability", []):

                room_type = avail.get("roomTypeName", "Room")

                total_available = avail.get("availableBeds", 0)
                female_available = avail.get("availableFemaleBeds", 0)
                male_available = avail.get("availableMaleBeds", 0)

                # Only show rooms where at least 1 bed is available
                if total_available > 0:
                    total_available_count += total_available

                    # Build gender-specific string
                    gender_parts = []
                    if female_available > 0:
                        gender_parts.append(f"{female_available} female")
                    if male_available > 0:
                        gender_parts.append(f"{male_available} male")

                    if gender_parts:
                        gender_info = ", ".join(gender_parts)
                        available_rooms.append(
                            f"{room_type} with {total_available} beds available ({gender_info})"
                        )
                    else:
                        available_rooms.append(
                            f"{room_type} with {total_available} beds available"
                        )

        logger.info(f"[AGENT-LOG] Availability check complete for {property_name}.")
        logger.info(f"[TOOL-END] get_room_availability | Success")

        # Limit response length for voice friendliness
        if available_rooms:
            rooms_str = ", ".join(available_rooms[:3])  # limit to first 3 room types
            return (
                f"Good news! {property_name} has {rooms_str}. "
                f"Would you like to schedule a visit?"
            )
        else:
            return (
                f"Currently, {property_name} is fully booked. "
                f"Would you like me to check similar properties nearby?"
            )

    except Exception as e:
        logger.error(
            f"[TOOL-ERROR] get_room_availability failed | Error: {str(e)}",
            exc_info=True
        )
        return "Sorry, I couldn't check availability right now. Would you like me to try again or schedule a visit?"



async def get_all_room_availability(
    user_id: str,
    move_in_date: Optional[str] = None,
    duration_months: int = 6
) -> str:
    """
    Fetch availability for ALL properties, merge with cached property info,
    and return only properties that have available beds.

    Includes:
    - Property name
    - Address
    - Room types available
    - Gender-specific availability
    """

    logger.info(
        f"[TOOL-START] get_all_room_availability | User: {user_id}"
    )

    try:
        # Step 1: Ensure property cache exists
        global properties_data_cache

        if properties_data_cache is None:
            logger.info("[TOOL] properties_data_cache empty, loading...")
            await load_properties_once()

        if not properties_data_cache:
            return "Sorry, property data is not available right now."

        # Step 2: Create propertyId -> property info map
        property_map = {}

        for prop in properties_data_cache:
            property_id = prop.get("propertyId") or prop.get("id")

            if property_id:
                property_map[property_id] = {
                    "name": prop.get("name", "Property"),
                    "address": prop.get("address", ""),
                    "city": prop.get("city", ""),
                    "location": prop.get("location", "")
                }

        logger.info(f"[TOOL] Property cache mapped: {len(property_map)} properties")

        # Step 3: Fetch ALL availability from Warden API
        logger.info("[TOOL] Fetching global availability from WardenAPI")

        api_response = await WardenHelper.get_bed_availability(property_id=None)

        if not api_response or not api_response.get("success"):
            return "Sorry, I couldn't fetch availability right now."

        availability_data = api_response.get("data", [])

        logger.info(
            f"[TOOL] Received availability for {len(availability_data)} properties"
        )

        # Step 4: Merge availability with property info
        available_properties = []

        for property_availability in availability_data:

            property_id = property_availability.get("propertyId")

            if property_id not in property_map:
                continue

            property_info = property_map[property_id]

            room_summaries = []
            property_total_available = 0

            for room in property_availability.get("availability", []):

                total_available = room.get("availableBeds", 0)
                female_available = room.get("availableFemaleBeds", 0)
                male_available = room.get("availableMaleBeds", 0)

                if total_available <= 0:
                    continue

                property_total_available += total_available

                room_type = room.get("roomTypeName", "Room")

                gender_parts = []

                if female_available > 0:
                    gender_parts.append(f"{female_available} female")

                if male_available > 0:
                    gender_parts.append(f"{male_available} male")

                if gender_parts:
                    gender_str = ", ".join(gender_parts)
                    room_summaries.append(
                        f"{room_type} ({total_available} beds: {gender_str})"
                    )
                else:
                    room_summaries.append(
                        f"{room_type} ({total_available} beds)"
                    )

            # Only include properties with availability
            if property_total_available > 0:
                available_properties.append({
                    "propertyId": property_id,
                    "name": property_info["name"],
                    "address": property_info["address"],
                    "city": property_info["city"],
                    "availableBeds": property_total_available,
                    "rooms": room_summaries
                })

        logger.info(
            f"[TOOL] Found {len(available_properties)} properties with availability"
        )

        # Step 5: If none available
        if not available_properties:
            return (
                "Currently, all properties are fully booked. "
                "Would you like me to notify you when beds become available?"
            )

        # Step 6: Sort by most availability
        available_properties.sort(
            key=lambda x: x["availableBeds"],
            reverse=True
        )

        # Step 7: Build voice-friendly response
        response_parts = []

        for prop in available_properties[:3]:  # limit for voice
            room_str = ", ".join(prop["rooms"][:2])

            response_parts.append(
                f"{prop['name']} in {prop['city']} has {room_str}"
            )

        final_response = ". ".join(response_parts)

        logger.info("[TOOL-END] get_all_room_availability | Success")

        return (
            f"I found multiple properties with availability. "
            f"{final_response}. "
            f"Would you like me to suggest the best one for you?"
        )

    except Exception as e:

        logger.error(
            f"[TOOL-ERROR] get_all_room_availability failed | {str(e)}",
            exc_info=True
        )

        return (
            "Sorry, I couldn't fetch property availability right now. "
            "Please try again shortly."
        )
