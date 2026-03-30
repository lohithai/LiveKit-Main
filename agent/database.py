import os
from motor.motor_asyncio import AsyncIOMotorClient
from logger import logger
from dotenv import load_dotenv
import asyncio


load_dotenv()

# Retrieve MongoDB connection string from environment
MONGODB_CONNECTION_STRING = os.environ.get("MONGODB_CONNECTION_STRING", "")

# Async MongoDB client with singleton pattern (per loop)
_clients = {}
_dbs = {}


async def get_async_client():
    """Get or create async MongoDB client with connection pooling for the current loop."""
    global _clients

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop not in _clients:
        # Create new client for this loop
        client = AsyncIOMotorClient(
            MONGODB_CONNECTION_STRING,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            retryWrites=True,
            w="majority",
            maxPoolSize=10,  # Connection pool size
            minPoolSize=2    # Minimum connections
        )
        # Verify connection
        try:
            await client.admin.command('ping')
            logger.info(f"Successfully connected to MongoDB Atlas! (Loop ID: {id(loop)})")
            _clients[loop] = client
        except Exception as e:
            logger.error("Failed to connect to MongoDB Atlas", exc_info=True)
            raise

    return _clients[loop]


async def get_async_db():
    """Get async database instance for the current loop."""
    global _dbs

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop not in _dbs:
        client = await get_async_client()
        _dbs[loop] = client.get_database("Truliv")

    return _dbs[loop]


async def get_async_collection(collection_name: str):
    """
    Returns a specific async collection from the MongoDB database.

    Args:
        collection_name (str): Name of the collection to retrieve.

    Returns:
        motor.motor_asyncio.AsyncIOMotorCollection: The async MongoDB collection object.
    """
    logger.debug(f"Retrieving async collection: {collection_name}")
    db = await get_async_db()
    return db[collection_name]


async def get_async_context_collection():
    """
    Returns the async 'user_contexts' collection.
    """
    logger.debug("Retrieving async 'user_contexts' collection.")
    return await get_async_collection("user_contexts")


async def get_async_call_logs_collection():
    """Get the call_logs collection for dashboard."""
    return await get_async_collection("call_logs")
