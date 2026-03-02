"""
Background Task Queue - Fire-and-forget pattern for non-blocking tool execution
"""

import asyncio
from typing import Coroutine, Any, Optional
from logger import logger


class BackgroundTaskQueue:
    """Fire-and-forget task queue for non-blocking operations"""

    def __init__(self):
        self._tasks: set[asyncio.Task] = set()

    def add(self, coro: Coroutine[Any, Any, Any], name: Optional[str] = None):
        """Add a coroutine to run in background without blocking"""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task):
        """Callback when task completes - log errors and remove from set"""
        self._tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"Background task '{task.get_name()}' failed: {exc}")
        except asyncio.CancelledError:
            pass

    async def wait_all(self, timeout: float = 10.0):
        """Wait for all pending tasks (call on shutdown)"""
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"Background tasks did not complete within {timeout}s timeout")

    @property
    def pending_count(self) -> int:
        """Number of pending background tasks"""
        return len(self._tasks)


# Global instance for background tasks
bg_tasks = BackgroundTaskQueue()
