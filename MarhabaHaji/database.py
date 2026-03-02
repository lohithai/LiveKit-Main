"""
database.py — MongoDB Connection & Collections
Marhaba Haji Voice Agent (LiveKit)
Uses same MongoDB Atlas cluster as Truliv, separate DB: marhaba_haji
"""

import os
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from logger import logger

_async_clients = {}

MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "marhaba_haji")
CONTEXT_COLLECTION = os.getenv("MONGO_CONTEXT_COLLECTION", "marhaba_context")


async def get_async_client():
    """Get or create an async MongoDB client for the current event loop."""
    loop_id = id(asyncio.get_running_loop())

    if loop_id not in _async_clients:
        if not MONGO_URI:
            raise ValueError("MONGO_URI environment variable is not set")

        client = AsyncIOMotorClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            maxPoolSize=10,
            minPoolSize=1,
        )
        await client.admin.command("ping")
        _async_clients[loop_id] = client
        logger.info(f"Connected to MongoDB Atlas (Loop ID: {loop_id})")

    return _async_clients[loop_id]


async def get_async_context_collection():
    """Get the Marhaba Haji caller context collection."""
    client = await get_async_client()
    db = client[MONGO_DB_NAME]
    return db[CONTEXT_COLLECTION]


async def get_async_collection(collection_name: str):
    """Get any MongoDB collection by name."""
    client = await get_async_client()
    db = client[MONGO_DB_NAME]
    return db[collection_name]


async def get_async_call_logs_collection():
    """Get the call_logs collection."""
    return await get_async_collection("call_logs")


async def get_async_visits_collection():
    """Get the visits collection for callback scheduling."""
    return await get_async_collection("visits")
