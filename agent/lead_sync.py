import aiohttp
import os
from typing import Any, Dict, List, Optional
from logger import logger

# Load environment variables if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # Safe fallback when dotenv is not available
    pass

# LeadSquared API Credentials
ACCESS_KEY = os.getenv("LEADSQUARED_ACCESS_KEY")
SECRET_KEY = os.getenv("LEADSQUARED_SECRET_KEY")
BASE_URL = "https://api-in21.leadsquared.com/v2/LeadManagement.svc"

# Attribute Field mappings for LeadSquared
FIELD_MAPPINGS: Dict[str, str] = {
    "botProfession": "mx_Bot_Profession",
    "botLocationPreference": "mx_Bot_Location_Preference",
    "botMoveInPreference": "mx_Bot_Move_In_Preference",
    "botRoomSharingPreference": "mx_Bot_Room_Sharing_Preference",
    "botBudget": "mx_Bot_Budget",
    "botPropertyPreference": "mx_Wing",
    "botSvDate": "mx_LOI_Signed_Date",
    "botSvTime": "mx_Unit_Number",
    "name": "FirstName"  # Added name mapping
}


async def create_or_update_lead(lead_data: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Create or update a lead in LeadSquared.

    Args:
        lead_data: List of attribute-value pairs.

    Returns:
        Response containing lead ID and affected rows, or None on failure.
    """
    if not ACCESS_KEY or not SECRET_KEY:
        logger.error("LeadSquared API credentials not configured")
        return None

    url = f"{BASE_URL}/Lead.CreateOrUpdate"
    params = {"accessKey": ACCESS_KEY, "secretKey": SECRET_KEY}
    headers = {"Content-Type": "application/json"}

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, params=params, headers=headers, json=lead_data) as response:
                response.raise_for_status()
                return await response.json()
    except Exception as exc:
        logger.error(f"LeadSquared create/update failed: {exc}")
        return None


async def get_lead_by_id(lead_id: str) -> Optional[List[Dict[str, Any]]]:
    """
    Retrieve lead details by ID.

    Args:
        lead_id: The lead ID (UUID).

    Returns:
        List of lead records, or None on failure.
    """
    if not ACCESS_KEY or not SECRET_KEY:
        logger.error("LeadSquared API credentials not configured")
        return None

    url = f"{BASE_URL}/Leads.GetById"
    params = {"accessKey": ACCESS_KEY, "secretKey": SECRET_KEY, "id": lead_id}

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                return await response.json()
    except Exception as exc:
        logger.error(f"LeadSquared get by id failed: {exc}")
        return None


async def sync_user_to_leadsquared(
    user_phone: str,
    context_data: Dict[str, Any],
    updated_fields: Optional[List[str]] = None
) -> bool:
    """
    Sync user profile data from MongoDB to LeadSquared.

    This function maps MongoDB context fields to LeadSquared custom fields
    and creates/updates the lead in LeadSquared.

    Args:
        user_phone: User's phone number (used as Phone attribute in LeadSquared)
        context_data: User's context_data dict from MongoDB
        updated_fields: Optional list of MongoDB field names that were updated.
                       If None, syncs all available fields.

    Returns:
        bool: True if sync succeeded, False otherwise
    """
    try:
        # Build lead data array for LeadSquared
        lead_data = []

        # Remove "91" prefix if present (user_id format is 91{{mobile_number}})
        formatted_phone = user_phone
        if formatted_phone.startswith("91"):
            formatted_phone = formatted_phone[2:]  # Remove first 2 characters

        # Always include phone number (without country code)
        # Use "Mobile" instead of "Phone" as it's often the primary key in LeadSquared
        lead_data.append({
            "Attribute": "Mobile",
            "Value": formatted_phone
        })
        # Also add SearchBy to ensure it finds the lead by phone
        lead_data.append({
            "Attribute": "SearchBy",
            "Value": "Mobile"
        })

        # Map MongoDB fields to LeadSquared fields
        # If updated_fields is specified, only sync those fields
        # Otherwise, sync all available fields

        fields_to_sync = {}

        if updated_fields:
            # Only sync specified fields
            for field in updated_fields:
                if field in FIELD_MAPPINGS and field in context_data:
                    fields_to_sync[field] = context_data[field]
        else:
            # Sync all available mapped fields
            for mongo_field in FIELD_MAPPINGS.keys():
                if mongo_field in context_data:
                    fields_to_sync[mongo_field] = context_data[mongo_field]

        # Convert to LeadSquared format
        for mongo_field, value in fields_to_sync.items():
            leadsquared_field = FIELD_MAPPINGS[mongo_field]

            # Skip None/empty values
            if value is None or value == "":
                continue

            lead_data.append({
                "Attribute": leadsquared_field,
                "Value": str(value)
            })

        # Only make API call if we have data to sync (beyond just phone and searchby)
        if len(lead_data) <= 2:
            logger.warning(f"No fields to sync for user {formatted_phone}")
            return True  # Not an error, just nothing to sync

        logger.info(f"Syncing {len(lead_data) - 1} fields to LeadSquared for {formatted_phone}")

        # Create/update lead in LeadSquared
        result = await create_or_update_lead(lead_data)

        if result and result.get("Status") == "Success":
            logger.info(f"Successfully synced user {formatted_phone} to LeadSquared")
            return True
        elif result and result.get("ExceptionMessage"):
             # Log specific exception from LeadSquared if available
             logger.error(f"LeadSquared sync failed for {formatted_phone}: {result.get('ExceptionMessage')}")
             return False
        else:
            logger.error(f"LeadSquared sync failed for {formatted_phone}: {result}")
            return False

    except Exception as exc:
        logger.error(f"Error syncing user {formatted_phone} to LeadSquared: {exc}", exc_info=True)
        return False


async def sync_lla_signed_to_leadsquared(user_phone: str) -> bool:
    """
    Sync mx_Asset_LLA_Signed field to LeadSquared when user responds with
    "Reply to this message" quick reply.

    Args:
        user_phone: User's phone number (will remove 91 prefix)

    Returns:
        bool: True if sync succeeded, False otherwise
    """
    try:
        # Remove "91" prefix if present
        formatted_phone = user_phone
        if formatted_phone.startswith("91"):
            formatted_phone = formatted_phone[2:]

        # Build lead data with Phone and mx_Asset_LLA_Signed field
        lead_data = [
            {
                "Attribute": "Phone",
                "Value": formatted_phone
            },
            {
                "Attribute": "mx_Asset_LLA_Signed",
                "Value": "Yes"
            }
        ]

        logger.info(f"Syncing LLA Signed status to LeadSquared for {formatted_phone}")

        # Create/update lead in LeadSquared
        result = await create_or_update_lead(lead_data)

        if result and result.get("Status") == "Success":
            logger.info(f"Successfully synced LLA Signed for {formatted_phone} to LeadSquared")
            return True
        else:
            logger.error(f"LeadSquared LLA Signed sync failed for {formatted_phone}: {result}")
            return False

    except Exception as exc:
        logger.error(f"Error syncing LLA Signed for {formatted_phone} to LeadSquared: {exc}", exc_info=True)
        return False

