"""
agent_tools.py — In-memory context cache & package catalog
Marhaba Haji Voice Agent (LiveKit)
"""

from datetime import datetime
from logger import logger
from database import get_async_context_collection, get_async_visits_collection

# ── In-memory context cache ──────────────────────────────────────────

_user_context_cache = {}


def set_cached_context(user_id: str, context_data: dict):
    _user_context_cache[user_id] = {
        "context_data": context_data,
        "dirty": False,
        "pending_updates": {},
    }


def get_cached_context(user_id: str) -> dict | None:
    entry = _user_context_cache.get(user_id)
    return entry["context_data"] if entry else None


def update_cached_context(user_id: str, updates: dict):
    entry = _user_context_cache.get(user_id)
    if not entry:
        return
    for key, value in updates.items():
        parts = key.split(".")
        target = entry["context_data"]
        for p in parts[:-1]:
            target = target.setdefault(p, {})
        target[parts[-1]] = value
    entry["dirty"] = True
    entry["pending_updates"].update(updates)


async def flush_cached_context(user_id: str):
    entry = _user_context_cache.get(user_id)
    if not entry or not entry["dirty"]:
        return
    try:
        coll = await get_async_context_collection()
        await coll.update_one(
            {"_id": user_id},
            {"$set": {"context_data": entry["context_data"]}},
            upsert=True,
        )
        entry["dirty"] = False
        entry["pending_updates"] = {}
        logger.info(f"Flushed context for {user_id}")
    except Exception as e:
        logger.error(f"Failed to flush context for {user_id}: {e}")


def clear_cached_context(user_id: str):
    _user_context_cache.pop(user_id, None)


# ── Static Package Catalog (test data) ──────────────────────────────

PACKAGE_CATALOG = {
    "saudi": {
        "umrah_economy": "Umrah Economy — flights + 3-star hotel near Haram, from INR 45,000/person",
        "umrah_standard": "Umrah Standard — flights + 4-star hotel, Ziyarat included, from INR 75,000/person",
        "umrah_premium": "Umrah Premium — Business class + 5-star Haram-view hotel, from INR 1,20,000/person",
        "hajj_guided": "Hajj Guided Group — full support with licensed Mutawwif, pricing on consultation",
        "hajj_vip": "Hajj VIP — luxury accommodation + private guide, pricing on consultation",
    },
    "dubai": {
        "halal_economy": "Dubai Halal Holiday — 4N/5D, Muslim-friendly hotel + city tour, from INR 35,000/person",
        "halal_premium": "Dubai Premium — 5-star hotel + desert safari + Burj Khalifa, from INR 65,000/person",
    },
    "turkey": {
        "halal_economy": "Turkey Halal Holiday — 5N/6D Istanbul, mosque tours, from INR 55,000/person",
        "halal_premium": "Turkey Premium — Istanbul + Cappadocia + Ephesus, from INR 90,000/person",
    },
    "malaysia": {
        "halal_economy": "Malaysia Halal — 4N/5D Kuala Lumpur, from INR 40,000/person",
    },
    "egypt": {
        "halal_economy": "Egypt Halal — 5N/6D Cairo + Alexandria, Islamic heritage tour, from INR 60,000/person",
    },
    "azerbaijan": {
        "halal_economy": "Azerbaijan Halal — 4N/5D Baku, from INR 55,000/person",
    },
}


async def find_packages(
    destination: str,
    service_interest: str | None = None,
    package_type: str | None = None,
) -> dict:
    """Return matching packages from the static catalog."""
    dest_key = destination.lower().replace(" ", "_").replace("saudi arabia", "saudi")
    catalog = PACKAGE_CATALOG.get(dest_key, {})

    if not catalog:
        return {
            "status": "not_found",
            "message": f"We are expanding to {destination} soon — our consultant can advise the best options!",
        }

    matches = {}
    for key, desc in catalog.items():
        if service_interest and service_interest.lower() not in key:
            continue
        if package_type and package_type.lower() not in key:
            continue
        matches[key] = desc

    if not matches:
        matches = catalog

    return {
        "status": "found",
        "destination": destination,
        "packages": list(matches.values())[:3],
        "note": "Prices are approximate — consultant will confirm exact rates.",
    }


async def schedule_callback(
    user_id: str,
    phone_number: str,
    preferred_date: str,
    preferred_time: str,
    name: str | None = None,
    service: str | None = None,
) -> dict:
    """Schedule a consultant callback and persist to visits collection."""
    visit = {
        "user_id": user_id,
        "phone_number": phone_number,
        "visit_date": preferred_date,
        "visit_time": preferred_time,
        "name": name or "",
        "service": service or "consultation",
        "status": "scheduled",
        "created_at": datetime.now(),
    }
    try:
        visits_coll = await get_async_visits_collection()
        await visits_coll.insert_one(visit)
        logger.info(f"Callback scheduled for {phone_number} on {preferred_date} at {preferred_time}")

        # Also update caller context
        coll = await get_async_context_collection()
        updates = {
            "context_data.callbackDate": preferred_date,
            "context_data.callbackTime": preferred_time,
        }
        if name:
            updates["context_data.name"] = name
        await coll.update_one({"_id": user_id}, {"$set": updates}, upsert=True)

    except Exception as e:
        logger.error(f"Failed to save callback: {e}")

    return {
        "status": "scheduled",
        "date": preferred_date,
        "time": preferred_time,
        "message": (
            f"JazakAllah Khair! Our consultant will call you on {preferred_date} at {preferred_time}. "
            "They will share the best package options tailored for you, InshAllah!"
        ),
    }
