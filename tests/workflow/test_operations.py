from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from workflow.models import Project
from workflow.nodes import CommandResult, NodeReadiness
from workflow.operations import OperationApprovalRequired, WorkflowOperations
from workflow.store import WorkflowStore


class FakeNode:
    def __init__(self, node_id="node"):
        self.id = node_id
        self.calls = []
        self.readiness_results = []

    async def run(self, argv, timeout=60.0):
        self.calls.append((list(argv), timeout))
        return CommandResult(0, "", "")

    async def readiness(self, minimum_free_disk_gb):
        return self.readiness_results.pop(0)


def _store(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.db")
    store.save_project(Project("demo", "perorina/demo", "main", "pc", "vps"))
    task = store.create_task("demo", "Task", "Proposal", "agent/task")
    return store, task


@pytest.mark.asyncio
async def test_wake_requires_approval_before_stb_command(tmp_path: Path) -> None:
    store, task = _store(tmp_path)
    operations = WorkflowOperations(store)
    stb = FakeNode("stb")

    with pytest.raises(OperationApprovalRequired):
        await operations.wake_pc(
            task.id, stb, ["wakeonlan", "AA:BB:CC:DD:EE:FF"], approved=False
        )
    assert stb.calls == []

    await operations.wake_pc(
        task.id, stb, ["wakeonlan", "AA:BB:CC:DD:EE:FF"], approved=True
    )
    assert stb.calls == [
        (["wakeonlan", "AA:BB:CC:DD:EE:FF"], 30.0)
    ]
    assert store.list_audit(task.id)[-1].action == "wake_pc"


@pytest.mark.asyncio
async def test_wait_for_pc_returns_when_tailscale_ssh_is_ready(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    operations = WorkflowOperations(store)
    pc = FakeNode("pc")
    pc.readiness_results = [
        NodeReadiness(False, False, False, None, (), "offline"),
        NodeReadiness(True, True, False, 80.0, ()),
    ]

    async def no_wait(_seconds):
        return None

    status = await operations.wait_for_pc(
        pc,
        minimum_free_disk_gb=10,
        timeout_seconds=180,
        interval_seconds=5,
        sleep=no_wait,
    )

    assert status.ready is True


@pytest.mark.asyncio
async def test_power_action_is_approved_and_allowlisted(tmp_path: Path) -> None:
    store, task = _store(tmp_path)
    operations = WorkflowOperations(store)
    pc = FakeNode("pc")

    with pytest.raises(ValueError, match="sleep or shutdown"):
        await operations.power_pc(task.id, pc, "restart", approved=True)
    with pytest.raises(OperationApprovalRequired):
        await operations.power_pc(task.id, pc, "shutdown", approved=False)

    await operations.power_pc(task.id, pc, "shutdown", approved=True)
    assert pc.calls[0][0] == ["shutdown.exe", "/s", "/t", "0"]


@pytest.mark.asyncio
async def test_fallback_requires_handoff_and_starts_fresh_session(tmp_path: Path) -> None:
    store, task = _store(tmp_path)
    operations = WorkflowOperations(store)
    calls = []

    async def start_fallback(task_id, handoff, resume_session):
        calls.append((task_id, handoff, resume_session))

    with pytest.raises(ValueError, match="handoff"):
        await operations.move_to_fallback(
            task.id, approved=True, start_fallback=start_fallback
        )

    store.record_handoff(task.id, "Commit abc123; remaining: run integration tests.")
    await operations.move_to_fallback(
        task.id, approved=True, start_fallback=start_fallback
    )

    assert calls == [
        (task.id, "Commit abc123; remaining: run integration tests.", False)
    ]


def test_daily_backup_keeps_seven_verified_versions(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    operations = WorkflowOperations(store)
    backup_dir = tmp_path / "backups"
    start = date(2026, 7, 1)

    for offset in range(8):
        operations.daily_backup(backup_dir, on_date=start + timedelta(days=offset))

    backups = sorted(backup_dir.glob("workflow-*.db"))
    assert len(backups) == 7
    assert backups[0].name == "workflow-20260702.db"
    assert backups[-1].name == "workflow-20260708.db"
