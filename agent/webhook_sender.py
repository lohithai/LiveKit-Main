"""
External webhook sender for call completion events.

Sends call data (customer info, transcript, recording URL, etc.)
to a configured external webhook endpoint after each call.
"""

import os
import asyncio
from datetime import datetime
from typing import Any

import aiohttp
from logger import logger


WEBHOOK_URL = os.getenv("EXTERNAL_WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("EXTERNAL_WEBHOOK_SECRET", "")
WEBHOOK_TIMEOUT = int(os.getenv("EXTERNAL_WEBHOOK_TIMEOUT", "30"))
WEBHOOK_MAX_RETRIES = int(os.getenv("EXTERNAL_WEBHOOK_MAX_RETRIES", "3"))


def _format_duration(seconds: int) -> str:
    """Format seconds into human-readable duration like '5m 42s'."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    remaining = seconds % 60
    if remaining == 0:
        return f"{minutes}m"
    return f"{minutes}m {remaining}s"


def build_webhook_payload(
    call_log_id: str,
    phone_number: str,
    user_id: str,
    user_contexts: dict,
    call_started_at: datetime,
    call_ended_at: datetime,
    duration_seconds: int,
    status: str,
    transcript: list[dict],
    summary: str,
    outcome: dict,
    recording_info: dict | None = None,
    room_name: str = "",
    qc_scores: dict | None = None,
) -> dict[str, Any]:
    """Build the full webhook payload.

    Returns:
        dict ready to be JSON-serialized and sent to the webhook.
    """
    customer_name = user_contexts.get("name", "Unknown")
    raw_phone = phone_number.lstrip("+").strip()
    formatted_phone = f"+{raw_phone}" if raw_phone else ""

    payload = {
        "event": "call.completed",
        "call_id": str(call_log_id),
        "timestamp": call_ended_at.isoformat(),

        "customer": {
            "name": customer_name,
            "phone_number": formatted_phone,
            "user_id": user_id,
        },

        "metadata": {
            "agent_name": os.getenv("AGENT_NAME", "truliv-telephony-agent"),
            "call_type": "inbound",
            "tenant": "truliv",
            "room_name": room_name,
            "location_preference": user_contexts.get("botLocationPreference", ""),
            "move_in_preference": user_contexts.get("botMoveInPreference", ""),
            "room_sharing_preference": user_contexts.get("botRoomSharingPreference", ""),
            "budget": user_contexts.get("botBudget", ""),
            "profession": user_contexts.get("botProfession", ""),
        },

        "call_details": {
            "date": call_started_at.strftime("%Y-%m-%d"),
            "start_time": call_started_at.isoformat(),
            "end_time": call_ended_at.isoformat(),
            "duration_seconds": duration_seconds,
            "duration_formatted": _format_duration(duration_seconds),
            "status": status,
            "outcome": outcome,
        },

        "transcript": transcript,
        "summary": summary,

        "recording": recording_info or {
            "url": None,
            "format": "mp3",
            "size_bytes": 0,
            "duration_seconds": duration_seconds,
        },
    }

    if qc_scores:
        payload["qc_scores"] = qc_scores

    return payload


async def send_webhook(payload: dict) -> bool:
    """Send the webhook payload to the configured external endpoint.

    Retries up to WEBHOOK_MAX_RETRIES times with exponential backoff.

    Returns:
        True if the webhook was sent successfully, False otherwise.
    """
    if not WEBHOOK_URL:
        logger.info("EXTERNAL_WEBHOOK_URL not configured — skipping webhook")
        return False

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "TrulivAgent/1.0",
    }

    if WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = WEBHOOK_SECRET

    for attempt in range(1, WEBHOOK_MAX_RETRIES + 1):
        try:
            timeout = aiohttp.ClientTimeout(total=WEBHOOK_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    WEBHOOK_URL,
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status < 300:
                        logger.info(
                            f"Webhook sent successfully: call_id={payload.get('call_id')} "
                            f"status={response.status}"
                        )
                        return True
                    else:
                        body = await response.text()
                        logger.warning(
                            f"Webhook attempt {attempt}/{WEBHOOK_MAX_RETRIES} failed: "
                            f"status={response.status} body={body[:200]}"
                        )

        except asyncio.TimeoutError:
            logger.warning(
                f"Webhook attempt {attempt}/{WEBHOOK_MAX_RETRIES} timed out "
                f"after {WEBHOOK_TIMEOUT}s"
            )
        except Exception as e:
            logger.warning(
                f"Webhook attempt {attempt}/{WEBHOOK_MAX_RETRIES} error: {e}"
            )

        if attempt < WEBHOOK_MAX_RETRIES:
            wait = 2 ** attempt
            logger.info(f"Retrying webhook in {wait}s...")
            await asyncio.sleep(wait)

    logger.error(
        f"Webhook failed after {WEBHOOK_MAX_RETRIES} attempts: "
        f"call_id={payload.get('call_id')}"
    )
    return False
