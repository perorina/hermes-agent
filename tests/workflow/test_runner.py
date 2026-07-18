from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from workflow.models import Project, TaskState
from workflow.runner import WorkflowRunner
from workflow.store import WorkflowStore


def _queued_store(tmp_path: Path, count: int = 2) -> tuple[WorkflowStore, list[int]]:
    store = WorkflowStore(tmp_path / "workflow.db")
    store.save_project(Project("demo", "perorina/demo", "main", "pc"))
    task_ids = []
    for number in range(count):
        task = store.create_task(
            "demo", f"Task {number}", "Proposal", f"agent/task-{number}"
        )
        store.approve_task(task.id, actor="owner")
        task_ids.append(task.id)
    return store, task_ids


@pytest.mark.asyncio
async def test_runner_takes_fifo_task_and_only_one_global_slot(tmp_path: Path) -> None:
    store, task_ids = _queued_store(tmp_path)
    active = 0
    maximum_active = 0
    started = []

    async def execute(task):
        nonlocal active, maximum_active
        started.append(task.id)
        store.transition_task(task.id, TaskState.RUNNING, actor="executor")
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        store.transition_task(task.id, TaskState.FAILED, actor="executor")

    runner = WorkflowRunner(store, execute)
    results = await asyncio.gather(runner.run_next(), runner.run_next())

    assert results.count(True) == 1
    assert started == [task_ids[0]]
    assert maximum_active == 1
    assert store.get_task(task_ids[1]).state is TaskState.QUEUED


@pytest.mark.asyncio
async def test_waiting_task_retains_global_and_project_locks(tmp_path: Path) -> None:
    store, task_ids = _queued_store(tmp_path)

    async def execute(task):
        store.transition_task(task.id, TaskState.RUNNING, actor="executor")
        store.transition_task(task.id, TaskState.WAITING_FOR_USER, actor="executor")

    runner = WorkflowRunner(store, execute)
    assert await runner.run_next() is True

    waiting = store.get_task(task_ids[0])
    assert waiting.state is TaskState.WAITING_FOR_USER
    assert store.acquire_global_lock("claude", task_ids[1]) is False
    assert store.acquire_project_lock("demo", task_ids[1]) is False
