"""
Centralized logging configuration using loguru.
Import logger from this module in all other files.
"""
from loguru import logger
import sys
import logging

# Suppress noisy standard library logs
logging.getLogger("pymongo").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("livekit").setLevel(logging.WARNING)

# Remove default handler
logger.remove()

# Add console handler with INFO level (for normal logs)
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="INFO",
    filter=lambda record: "pyngrok" not in record["name"] and "pymongo" not in record["name"] and "livekit" not in record["name"] and record["level"].name != "ERROR",
    backtrace=True,
    diagnose=True
)

# Add separate console handler for ERROR level with enhanced formatting
logger.add(
    sys.stderr,
    format="<red>{time:YYYY-MM-DD HH:mm:ss}</red> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>\n<red>{exception}</red>",
    level="ERROR",
    filter=lambda record: "pyngrok" not in record["name"],
    backtrace=True,
    diagnose=True
)

# Voice logger - same as main logger (console only)
voice_logger = logger
