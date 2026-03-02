import sys
from loguru import logger

# Remove default handler
logger.remove()

# Add custom handler with timestamp format
logger.add(
    sys.stderr,
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    level="INFO",
    colorize=True,
)
