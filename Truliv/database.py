import os
from motor.motor_asyncio import AsyncIOMotorClient
from logger import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
from dotenv import load_dotenv
import asyncio
import traceback
import requests
import base64
from datetime import datetime, timedelta
import hashlib


load_dotenv()

# Retrieve MongoDB connection string from environment or default to the Atlas URI
MONGODB_CONNECTION_STRING = os.environ.get(
    "MONGODB_CONNECTION_STRING",
    "mongodb+srv://gogizmo:root@cluster.akp9e.mongodb.net/?retryWrites=true&w=majority&appName=Cluster"
)

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


# Legacy sync functions for backward compatibility (deprecated)
def get_db():
    """Legacy sync function - use get_async_db instead."""
    logger.warning("get_db() is deprecated. Use get_async_db() instead.")
    return None

def get_collection(collection_name: str):
    """Legacy sync function - use get_async_collection instead."""
    logger.warning("get_collection() is deprecated. Use get_async_collection() instead.")
    return None

def get_context_collection():
    """Legacy sync function - use get_async_context_collection instead."""
    logger.warning("get_context_collection() is deprecated. Use get_async_context_collection() instead.")
    return None

# New async functions
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

async def get_async_context_collection_by_user_id(user_id: str):
    """
    Returns the 'user_contexts' collection by user_id asynchronously.
    """
    logger.debug(f"Retrieving async 'user_contexts' collection by user_id: {user_id}")
    collection = await get_async_context_collection()
    return await collection.find_one({"_id": user_id})

async def get_async_daily_user_count_collection():
    """
    Returns the async 'daily_user_count' collection.
    """
    logger.debug("Retrieving async 'daily_user_count' collection.")
    return await get_async_collection("daily_user_count")

async def get_async_unique_users_collection():
    """
    Returns the async 'unique_users' collection.
    """
    logger.debug("Retrieving async 'unique_users' collection.")
    return await get_async_collection("unique_users")

async def get_async_action_logs_collection():
    """
    Returns the async 'action_logs' collection.
    """
    logger.debug("Retrieving async 'action_logs' collection.")
    return await get_async_collection("action_logs")

async def get_async_sessions_collection():
    """
    Returns the async 'sessions' collection.
    """
    logger.debug("Retrieving async 'sessions' collection.")
    return await get_async_collection("sessions")

# Backward compatibility wrappers (sync versions that call async versions)
async def get_context_collection_async():
    """Backward compatibility wrapper."""
    return await get_async_context_collection()

# Async versions of CRUD operations
@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
    retry=retry_if_exception_type((ConnectionError, TimeoutError))
)
async def async_update_document(collection_name: str, filter_query: dict, update_values: dict, upsert: bool = False):
    """
    Updates a single document in the specified collection asynchronously, retrying on failure.

    Args:
        collection_name (str): The name of the collection.
        filter_query (dict): The filter to find the document.
        update_values (dict): The updates to apply (e.g., {"$set": {"field": value}}).
        upsert (bool): If True, insert the document if it does not exist.

    Returns:
        motor.motor_asyncio.AsyncIOMotorClientSession: The result from the update_one operation.
    """
    logger.debug(
        f"Attempting to async update a document in '{collection_name}' with filter: {filter_query} and update: {update_values}"
    )
    collection = await get_async_collection(collection_name)
    result = await collection.update_one(filter_query, update_values, upsert=upsert)
    logger.info(f"async_update_document in '{collection_name}' modified_count: {result.modified_count}")
    return result

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
    retry=retry_if_exception_type((ConnectionError, TimeoutError))
)
async def async_update_documents(collection_name: str, filter_query: dict, update_values: dict, upsert: bool = False):
    """
    Updates multiple documents in the specified collection asynchronously, retrying on failure.

    Args:
        collection_name (str): The name of the collection.
        filter_query (dict): The filter to find the documents.
        update_values (dict): The updates to apply.
        upsert (bool): If True, insert the document if it does not exist.

    Returns:
        motor.motor_asyncio.AsyncIOMotorClientSession: The result from the update_many operation.
    """
    logger.debug(
        f"Attempting to async update multiple documents in '{collection_name}' with filter: {filter_query} and update: {update_values}"
    )
    collection = await get_async_collection(collection_name)
    result = await collection.update_many(filter_query, update_values, upsert=upsert)
    logger.info(f"async_update_documents in '{collection_name}' modified_count: {result.modified_count}")
    return result

async def async_log_action_to_mongodb(ip_address: str, action: str):
    """Store action log in MongoDB action_logs collection asynchronously."""
    try:
        action_logs_collection = await get_async_action_logs_collection()
        log_entry = {
            "timestamp": datetime.now(),
            "ip_address": ip_address,
            "action": action
        }
        result = await action_logs_collection.insert_one(log_entry)
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"Error saving action log to MongoDB: {str(e)}", exc_info=True)
        raise

async def async_get_action_logs_from_mongodb(limit: int = 50, skip: int = 0):
    """Retrieve action logs from MongoDB asynchronously."""
    try:
        action_logs_collection = await get_async_action_logs_collection()
        cursor = action_logs_collection.find().sort("timestamp", -1).skip(skip).limit(limit)
        logs = await cursor.to_list(length=None)

        # Convert ObjectId to string and format timestamp
        for log in logs:
            log['_id'] = str(log['_id'])
            if isinstance(log.get('timestamp'), datetime):
                log['timestamp'] = log['timestamp'].strftime("%Y-%m-%d %H:%M:%S")

        return logs
    except Exception as e:
        logger.error(f"Error retrieving action logs: {str(e)}", exc_info=True)
        return []

# Additional async utility functions
async def async_find_one(collection_name: str, filter_query: dict):
    """Find one document asynchronously."""
    collection = await get_async_collection(collection_name)
    return await collection.find_one(filter_query)

async def async_find_many(collection_name: str, filter_query: dict = None, limit: int = None):
    """Find multiple documents asynchronously."""
    collection = await get_async_collection(collection_name)
    cursor = collection.find(filter_query or {})
    if limit:
        cursor = cursor.limit(limit)
    return await cursor.to_list(length=None)

async def async_insert_one(collection_name: str, document: dict):
    """Insert one document asynchronously."""
    collection = await get_async_collection(collection_name)
    result = await collection.insert_one(document)
    return str(result.inserted_id)

async def async_insert_many(collection_name: str, documents: list):
    """Insert multiple documents asynchronously."""
    collection = await get_async_collection(collection_name)
    result = await collection.insert_many(documents)
    return [str(oid) for oid in result.inserted_ids]

# Legacy sync functions (deprecated)
@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3), reraise=True)
def update_document(collection_name: str, filter_query: dict, update_values: dict, upsert: bool = False):
    """Legacy sync function - use async_update_document instead."""
    logger.warning("update_document() is deprecated. Use async_update_document() instead.")
    return None

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3), reraise=True)
def update_documents(collection_name: str, filter_query: dict, update_values: dict, upsert: bool = False):
    """Legacy sync function - use async_update_documents instead."""
    logger.warning("update_documents() is deprecated. Use async_update_documents() instead.")
    return None

def log_action_to_mongodb(ip_address: str, action: str):
    """Legacy sync function - use async_log_action_to_mongodb instead."""
    logger.warning("log_action_to_mongodb() is deprecated. Use async_log_action_to_mongodb() instead.")
    return None

def get_action_logs_from_mongodb(limit: int = 50, skip: int = 0):
    """Legacy sync function - use async_get_action_logs_from_mongodb instead."""
    logger.warning("get_action_logs_from_mongodb() is deprecated. Use async_get_action_logs_from_mongodb() instead.")
    return []

# Async context management
async def close_mongodb_connection():
    """Close MongoDB connection gracefully."""
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None
        logger.info("MongoDB connection closed.")





