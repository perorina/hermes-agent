from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from workflow.config import NodeConfig, ProjectConfig, WorkflowConfig
from workflow.executor import ClaudeResult, PushResult
from workflow.models import Project, TaskState
from workflow.nodes import NodeReadiness
from workflow.service import WorkflowService
from workflow.store import WorkflowStore


class ReadyNode:
    id = "pc"

    def __init__(self):
        self.readiness_calls = []

    async def readiness(self, minimum_free_disk_gb, disk_path=None):
        self.readiness_calls.append((minimum_free_disk_gb, disk_path))
        return NodeReadiness(True, True, False, 80.0, ())


class FakeExecutor:
    def __init__(self, store, **kwargs):
        self.store = store

    async def prepare(self, task):
        return PurePosixPath("/worktrees/1-task")

    async def prepare_repair(self, task):
        return PurePosixPath("/worktrees/1-task")

    async def run_claude(self, task, workspace):
        self.store.transition_task(task.id, TaskState.RUNNING, actor="executor")
        return ClaudeResult(
            session_id="session-1",
            summary="Implemented task",
            question=None,
            log_path=Path("task.log"),
        )

    async def run_checks(self, task, workspace):
        self.store.transition_task(task.id, TaskState.TESTING, actor="executor")
        return (("pytest -q", True, "1 passed"),)

    async def commit_and_push(self, task, workspace):
        self.store.transition_task(task.id, TaskState.PUSHED, actor="executor")
        return PushResult("abc123", ("README.md",))


def _config(tmp_path: Path) -> WorkflowConfig:
    return WorkflowConfig(
        enabled=True,
        owner_telegram_id=123,
        database_path=tmp_path / "workflow.db",
        logs_dir=tmp_path / "logs",
        worktree_roots={"pc": PurePosixPath("/worktrees")},
        nodes={"pc": NodeConfig("pc", "linux", "pc.tailnet")},
        projects={
            "demo": ProjectConfig(
                id="demo",
                name_with_owner="perorina/demo",
                default_branch="main",
                preferred_node="pc",
                primary_clones={"pc": PurePosixPath("/repos/demo")},
                checks=("pytest -q",),
            )
        },
    )


@pytest.mark.asyncio
async def test_service_executes_queued_task_and_notifies_pr_action(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = WorkflowStore(config.database_path)
    store.save_project(Project("demo", "perorina/demo", "main", "pc"))
    task = store.create_task("demo", "Task", "Proposal", "agent/hermes/1-task")
    store.approve_task(task.id, actor="owner")
    notifications = []

    async def notify(task_id, text, actions):
        notifications.append((task_id, text, actions))

    node = ReadyNode()
    service = WorkflowService(
        config,
        store,
        notify=notify,
        nodes={"pc": node},
        executor_factory=FakeExecutor,
    )

    assert await service.run_once() is True

    result = store.get_task(task.id)
    runtime = store.get_task_runtime(task.id)
    assert result.state is TaskState.PUSHED
    assert runtime.node_id == "pc"
    assert runtime.workspace == "/worktrees/1-task"
    assert runtime.claude_session_id == "session-1"
    assert node.readiness_calls == [(10, "/worktrees")]
    assert notifications[-1][2] == (
        ("Buat PR", "wf:pr:1"),
        ("Lanjutkan task", "wf:continue:1"),
    )
