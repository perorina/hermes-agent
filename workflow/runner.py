from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Optional

from workflow.models import TaskState, WorkflowTask
from workflow.store import InvalidTaskTransition, WorkflowStore

logger = logging.getLogger(__name__)

TaskExecutor = Callable[[WorkflowTask], Awaitable[None]]


class WorkflowRunner:
    def __init__(
        self,
        store: WorkflowStore,
        execute: TaskExecutor,
        poll_interval_seconds: float = 2.0,
    ):
        self.store = store
        self._execute = execute
        self._poll_interval = poll_interval_seconds
        self._run_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._background_task: Optional[asyncio.Task] = None

    async def run_next(self) -> bool:
        if self._run_lock.locked():
            return False
        async with self._run_lock:
            queue = self.store.list_queue()
            if not queue:
                return False
            task = queue[0]
            if not self.store.acquire_global_lock("claude", task.id):
                return False
            if not self.store.acquire_project_lock(task.project_id, task.id):
                self.store.release_global_lock("claude", task.id)
                return False
            release_locks = True
            try:
                task = self.store.transition_task(
                    task.id, TaskState.PREPARING, actor="runner"
                )
                await self._execute(task)
                release_locks = (
                    self.store.get_task(task.id).state is not TaskState.WAITING_FOR_USER
                )
            except Exception as exc:
                logger.exception("Workflow task %s failed", task.id)
                try:
                    current = self.store.get_task(task.id)
                    if current.state not in {
                        TaskState.FAILED,
                        TaskState.CANCELLED,
                        TaskState.COMPLETED,
                    }:
                        self.store.transition_task(
                            task.id,
                            TaskState.FAILED,
                            actor="runner",
                            detail=str(exc),
                        )
                except (KeyError, InvalidTaskTransition):
                    logger.exception("Could not persist failure for task %s", task.id)
            finally:
                if release_locks:
                    self.store.release_project_lock(task.project_id, task.id)
                    self.store.release_global_lock("claude", task.id)
            return True

    def start(self) -> None:
        if self._background_task is None or self._background_task.done():
            self._stop_event.clear()
            self._background_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._background_task is not None:
            await self._background_task
            self._background_task = None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            await self.run_next()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass
