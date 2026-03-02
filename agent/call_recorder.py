"""
LiveKit Egress-based call recording.

Starts a RoomCompositeEgress (audio-only) when a call begins,
and returns the recording info (S3 URL) when the egress completes.
"""

import os
from livekit.api import LiveKitAPI
from livekit.protocol.egress import (
    RoomCompositeEgressRequest,
    StopEgressRequest,
    EncodedFileOutput,
    EncodedFileType,
    S3Upload,
)
from logger import logger


LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

# S3 config for recording storage
S3_BUCKET = os.getenv("RECORDING_S3_BUCKET", "")
S3_REGION = os.getenv("RECORDING_S3_REGION", "ap-south-1")
S3_ACCESS_KEY = os.getenv("RECORDING_S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("RECORDING_S3_SECRET_KEY", "")


async def start_recording(room_name: str, call_id: str) -> str | None:
    """Start an audio-only room composite egress for the given room.

    Args:
        room_name: The LiveKit room name to record.
        call_id: Unique identifier used for the recording filename.

    Returns:
        The egress_id if started successfully, None otherwise.
    """
    if not S3_BUCKET:
        logger.warning("RECORDING_S3_BUCKET not set — skipping recording")
        return None

    try:
        api = LiveKitAPI(
            url=LIVEKIT_URL.replace("ws://", "http://").replace("wss://", "https://"),
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )

        s3_upload = S3Upload(
            access_key=S3_ACCESS_KEY,
            secret=S3_SECRET_KEY,
            region=S3_REGION,
            bucket=S3_BUCKET,
        )

        file_output = EncodedFileOutput(
            file_type=EncodedFileType.MP3,
            filepath=f"recordings/{call_id}.mp3",
            s3=s3_upload,
            disable_manifest=True,
        )

        request = RoomCompositeEgressRequest(
            room_name=room_name,
            file_outputs=[file_output],
            audio_only=True,
        )

        egress_info = await api.egress.start_room_composite_egress(request)
        egress_id = egress_info.egress_id
        logger.info(f"Recording started: egress_id={egress_id} room={room_name}")
        await api.aclose()
        return egress_id

    except Exception as e:
        logger.error(f"Failed to start recording for room {room_name}: {e}")
        return None


def get_recording_url(call_id: str) -> str | None:
    """Build the public S3 URL for a completed recording."""
    if not S3_BUCKET:
        return None
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/recordings/{call_id}.mp3"


async def stop_recording(egress_id: str) -> dict | None:
    """Stop an active egress and return recording metadata.

    Returns:
        dict with url, format, size_bytes or None on failure.
    """
    if not egress_id:
        return None

    try:
        api = LiveKitAPI(
            url=LIVEKIT_URL.replace("ws://", "http://").replace("wss://", "https://"),
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )

        stop_request = StopEgressRequest(egress_id=egress_id)
        egress_info = await api.egress.stop_egress(stop_request)
        await api.aclose()

        # Extract file info from the egress result
        file_results = egress_info.file_results
        if file_results:
            fr = file_results[0]
            url = getattr(fr, "location", "") or ""
            size = getattr(fr, "size", 0) or 0
            duration_ns = getattr(fr, "duration", 0) or 0
            return {
                "url": url,
                "format": "mp3",
                "size_bytes": size,
                "duration_seconds": int(duration_ns / 1_000_000_000) if duration_ns else 0,
            }

        logger.warning(f"Egress {egress_id} stopped but no file results yet")
        return None

    except Exception as e:
        logger.error(f"Failed to stop recording egress {egress_id}: {e}")
        return None
