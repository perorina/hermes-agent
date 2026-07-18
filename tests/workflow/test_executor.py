from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from workflow.config import ProjectConfig
from workflow.executor import (
    DestructiveConfirmationRequired,
    DirtyRepositoryError,
    TaskExecutor,
)
from workflow.models import Project, TaskState
from workflow.nodes import CommandResult
from workflow.store import WorkflowStore


class FakeNode:
    def __init__(self, node_id: str = "pc"):
        self.id = node_id
        self.calls: list[tuple[list[str], float]] = []
        self.results: list[CommandResult] = []

    async def run(self, argv, timeout=60.0):
        self.calls.append((list(argv), timeout))
        if not self.results:
            raise AssertionError(f"unexpected command: {argv}")
        return self.results.pop(0)


def _setup(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.db")
    store.save_project(Project("demo", "perorina/demo", "main", "pc", "vps"))
    task = store.create_task(
        "demo", "Update health check", "Proposal", "agent/hermes/1-health-check"
    )
    store.approve_task(task.id, actor="owner")
    task = store.transition_task(task.id, TaskState.PREPARING, actor="runner")
    project = ProjectConfig(
        id="demo",
        name_with_owner="perorina/demo",
        default_branch="main",
        preferred_node="pc",
        fallback_node="vps",
        primary_clones={"pc": PurePosixPath("/repos/demo")},
        checks=("pytest -q",),
    )
    node = FakeNode()
    executor = TaskExecutor(
        store=store,
        node=node,
        project=project,
        worktree_root=PurePosixPath("/worktrees"),
        logs_dir=tmp_path / "logs",
    )
    return store, task, node, executor


@pytest.mark.asyncio
async def test_prepare_refuses_dirty_primary_clone(tmp_path: Path) -> None:
    _, task, node, executor = _setup(tmp_path)
    node.results = [CommandResult(0, " M README.md\n", "")]

    with pytest.raises(DirtyRepositoryError, match="primary clone is dirty"):
        await executor.prepare(task)

    assert node.calls == [
        (["git", "-C", "/repos/demo", "status", "--porcelain"], 30.0)
    ]


@pytest.mark.asyncio
async def test_prepare_creates_branch_from_latest_remote_default(tmp_path: Path) -> None:
    _, task, node, executor = _setup(tmp_path)
    node.results = [
        CommandResult(0, "", ""),
        CommandResult(0, "", ""),
        CommandResult(0, "", ""),
    ]

    workspace = await executor.prepare(task)

    assert workspace == PurePosixPath("/worktrees/1-health-check")
    assert [call[0] for call in node.calls] == [
        ["git", "-C", "/repos/demo", "status", "--porcelain"],
        ["git", "-C", "/repos/demo", "fetch", "--prune", "origin", "main"],
        [
            "git",
            "-C",
            "/repos/demo",
            "worktree",
            "add",
            "-b",
            "agent/hermes/1-health-check",
            "/worktrees/1-health-check",
            "origin/main",
        ],
    ]


@pytest.mark.asyncio
async def test_prepare_repair_recreates_worktree_from_remote_agent_branch(
    tmp_path: Path,
) -> None:
    _, task, node, executor = _setup(tmp_path)
    node.results = [CommandResult(0, "", "") for _ in range(4)]

    workspace = await executor.prepare_repair(task)

    assert workspace == PurePosixPath("/worktrees/1-health-check")
    assert [call[0] for call in node.calls[1:]] == [
        [
            "git",
            "-C",
            "/repos/demo",
            "fetch",
            "origin",
            "agent/hermes/1-health-check",
        ],
        [
            "git",
            "-C",
            "/repos/demo",
            "branch",
            "--force",
            "agent/hermes/1-health-check",
            "origin/agent/hermes/1-health-check",
        ],
        [
            "git",
            "-C",
            "/repos/demo",
            "worktree",
            "add",
            "/worktrees/1-health-check",
            "agent/hermes/1-health-check",
        ],
    ]

@pytest.mark.asyncio
async def test_claude_question_is_logged_and_pauses_task(tmp_path: Path) -> None:
    store, task, node, executor = _setup(tmp_path)
    node.results = [
        CommandResult(
            0,
            '\n'.join(
                [
                    '{"type":"system","session_id":"session-abc"}',
                    '{"type":"result","result":"HERMES_QUESTION: Which endpoint?"}',
                ]
            ),
            "",
        )
    ]

    result = await executor.run_claude(task, PurePosixPath("/worktrees/1-health-check"))

    assert result.session_id == "session-abc"
    assert result.question == "Which endpoint?"
    assert store.get_task(task.id).state is TaskState.WAITING_FOR_USER
    assert result.log_path.read_text(encoding="utf-8").startswith('{"type":"system"')


@pytest.mark.asyncio
async def test_checks_commit_and_push_agent_branch(tmp_path: Path) -> None:
    store, task, node, executor = _setup(tmp_path)
    store.transition_task(task.id, TaskState.RUNNING, actor="executor")
    node.results = [
        CommandResult(0, "1 passed", ""),
        CommandResult(0, " M README.md\0?? new.py\0", ""),
        CommandResult(0, "", ""),
        CommandResult(0, "", ""),
        CommandResult(0, "abc123\n", ""),
        CommandResult(0, "", ""),
    ]
    workspace = PurePosixPath("/worktrees/1-health-check")

    checks = await executor.run_checks(task, workspace)
    result = await executor.commit_and_push(task, workspace)

    assert checks == (("pytest -q", True, "1 passed"),)
    assert result.commit == "abc123"
    assert result.changed_files == ("README.md", "new.py")
    assert store.get_task(task.id).state is TaskState.PUSHED
    assert node.calls[-1][0] == [
        "git",
        "-C",
        "/worktrees/1-health-check",
        "push",
        "-u",
        "origin",
        "agent/hermes/1-health-check",
    ]


@pytest.mark.asyncio
async def test_cancel_requires_confirmation_when_branch_has_commits(tmp_path: Path) -> None:
    _, task, node, executor = _setup(tmp_path)
    node.results = [CommandResult(0, "2\n", "")]

    with pytest.raises(DestructiveConfirmationRequired):
        await executor.cancel(task, PurePosixPath("/worktrees/1-health-check"))

    assert len(node.calls) == 1
