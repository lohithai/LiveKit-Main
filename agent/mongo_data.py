"""
Warden API data loader for Truliv Luna Bengaluru.
Loads from disk cache first (instant). Falls back to API if cache missing.
Bed availability is always fetched fresh (changes frequently).
"""

import os
import json
import asyncio
import time
from typing import Dict, List, Optional
from helpers.warden_corn_api import WardenAPI
from logger import logger
from dotenv import load_dotenv

load_dotenv(".env.local")

# ── Cached data (populated from disk or API) ──────────────────────
luna_property: Optional[Dict] = None
luna_room_types: Optional[List[Dict]] = None
luna_bed_availability: Optional[Dict] = None
_load_lock = asyncio.Lock()

PROPERTY_NAME = "Truliv Luna"
CACHE_FILE = "/tmp/truliv_luna_cache.json"
# Bed availability refreshes every call; property + rooms cache for 24h
CACHE_MAX_AGE_SECONDS = 86400  # 24 hours


def _read_disk_cache() -> Optional[Dict]:
    """Read cached property + room data from disk."""
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
        cached_at = cache.get("cached_at", 0)
        if time.time() - cached_at > CACHE_MAX_AGE_SECONDS:
            logger.info("[WARDEN] Disk cache expired (>24h)")
            return None
        logger.info("[WARDEN] Loaded property + rooms from disk cache (instant)")
        return cache
    except Exception as e:
        logger.warning(f"[WARDEN] Failed to read disk cache: {e}")
        return None


def _write_disk_cache(prop: Dict, rooms: List[Dict]):
    """Write property + room data to disk cache."""
    try:
        cache = {
            "cached_at": time.time(),
            "property": prop,
            "room_types": rooms,
        }
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
        logger.info("[WARDEN] Saved property + rooms to disk cache")
    except Exception as e:
        logger.warning(f"[WARDEN] Failed to write disk cache: {e}")


async def preload_all_data():
    """
    Load Truliv Luna data. Property + rooms come from disk cache (instant).
    Bed availability is always fetched fresh from API.
    If no disk cache, fetches everything from API and caches to disk.
    """
    global luna_property, luna_room_types, luna_bed_availability

    async with _load_lock:
        if luna_property is not None:
            return  # Already loaded in this process

        api_key = os.getenv("WARDEN_API_KEY", "")
        base_url = os.getenv("WARDEN_API_BASE_URL", "")

        if not api_key or not base_url:
            logger.error("[WARDEN] Missing WARDEN_API_KEY or WARDEN_API_BASE_URL")
            luna_property = {}
            luna_room_types = []
            luna_bed_availability = {}
            return

        warden = WardenAPI(api_key=api_key, base_url=base_url)

        # Try disk cache for property + rooms
        disk_cache = _read_disk_cache()

        if disk_cache:
            luna_property = disk_cache["property"]
            luna_room_types = disk_cache["room_types"]
            property_id = luna_property.get("id")

            # Only fetch bed availability fresh
            try:
                raw_beds = await warden.get_bed_availability()
                all_beds = raw_beds.get("data", []) if isinstance(raw_beds, dict) and "data" in raw_beds else raw_beds

                luna_bed_availability = {}
                if isinstance(all_beds, list):
                    for entry in all_beds:
                        if entry.get("propertyId") == property_id:
                            luna_bed_availability = entry
                            break
                elif isinstance(all_beds, dict) and all_beds.get("propertyId") == property_id:
                    luna_bed_availability = all_beds

                logger.info(f"[WARDEN] Disk cache hit + fresh availability fetched")
            except Exception as e:
                logger.warning(f"[WARDEN] Bed availability fetch failed: {e}")
                luna_bed_availability = {}
            return

        # No disk cache — fetch everything from API
        try:
            props_task = warden.get_properties()
            rooms_task = warden.get_room_types()
            beds_task = warden.get_bed_availability()

            all_properties, all_room_types, all_bed_availability = (
                await asyncio.gather(props_task, rooms_task, beds_task)
            )

            if isinstance(all_properties, dict):
                all_properties = all_properties.get("data", [])
            if isinstance(all_room_types, dict):
                all_room_types = all_room_types.get("data", [])
            if isinstance(all_bed_availability, dict) and "data" in all_bed_availability:
                all_bed_availability = all_bed_availability.get("data", [])

            # Find Truliv Luna
            luna_property = None
            for prop in (all_properties or []):
                name = prop.get("name", "")
                if "luna" in name.lower():
                    luna_property = prop
                    break

            if not luna_property:
                logger.error(f"[WARDEN] Truliv Luna not found in {len(all_properties or [])} properties")
                luna_property = {}
                luna_room_types = []
                luna_bed_availability = {}
                return

            property_id = luna_property.get("id")
            logger.info(f"[WARDEN] Found Truliv Luna (ID: {property_id})")

            luna_room_types = [
                r for r in (all_room_types or [])
                if r.get("propertyId") == property_id
            ]

            luna_bed_availability = {}
            if isinstance(all_bed_availability, list):
                for entry in all_bed_availability:
                    if entry.get("propertyId") == property_id:
                        luna_bed_availability = entry
                        break
            elif isinstance(all_bed_availability, dict):
                if all_bed_availability.get("propertyId") == property_id:
                    luna_bed_availability = all_bed_availability

            # Save property + rooms to disk cache
            _write_disk_cache(luna_property, luna_room_types)

            logger.info(
                f"[WARDEN] Pre-loaded Truliv Luna: "
                f"{len(luna_room_types)} room types, "
                f"availability: {'yes' if luna_bed_availability else 'no'}"
            )

        except Exception as e:
            logger.error(f"[WARDEN] Failed to preload data: {e}")
            luna_property = luna_property or {}
            luna_room_types = luna_room_types or []
            luna_bed_availability = luna_bed_availability or {}


# ── Query helpers (all read from in-memory cache) ───────────────────


def get_property() -> Dict:
    """Return cached Truliv Luna property data."""
    return luna_property or {}


def get_property_names() -> List[str]:
    """Return list with single property name."""
    if luna_property and luna_property.get("name"):
        return [luna_property["name"]]
    return ["Truliv Luna"]


def get_room_types() -> List[Dict]:
    """Return cached room types for Truliv Luna."""
    return luna_room_types or []


def get_bed_availability() -> Optional[Dict]:
    """Return cached bed availability for Truliv Luna."""
    return luna_bed_availability or {}


def get_starting_price() -> int:
    """Get starting price for Truliv Luna."""
    if not luna_property:
        return 0
    try:
        price = (
            luna_property.get("startingPrice")
            or luna_property.get("minPrice")
            or 0
        )
        return int(price) if price else 0
    except (TypeError, ValueError):
        return 0
