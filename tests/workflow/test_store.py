from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from workflow.models import Project, TaskState
from workflow.store import InvalidTaskTransition, WorkflowStore


@pytest.fixture
def store(tmp_path: Path) -> WorkflowStore:
    result = WorkflowStore(tmp_path / "workflow.db")
    result.save_project(
        Project(
            id="yamansari",
            name_with_owner="perorina/yamansari-deployment",
            default_branch="main",
            preferred_node="windows-pc",
            fallback_node="vps",
        )
    )
    return result


def test_initializes_workflow_database_without_touching_state_db(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    state_db.write_bytes(b"hermes-memory-must-not-change")

    result = WorkflowStore(tmp_path / "workflow.db")

    assert state_db.read_bytes() == b"hermes-memory-must-not-change"
    with sqlite3.connect(result.path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert {
        "projects",
        "tasks",
        "project_locks",
        "task_events",
        "handoffs",
        "raw_logs",
    }.issubset(tables)
    assert journal_mode == "wal"


def test_task_transitions_are_validated_and_audited(store: WorkflowStore) -> None:
    task = store.create_task(
        project_id="yamansari",
        instruction="Update the deployment documentation",
        proposal="Edit the runbook and verify links.",
        branch="agent/hermes/1-update-deployment-docs",
    )

    assert task.state is TaskState.AWAITING_APPROVAL
    queued = store.approve_task(task.id, actor="telegram:123")
    running = store.transition_task(queued.id, TaskState.PREPARING, actor="runner")

    assert queued.queue_position == 1
    assert running.state is TaskState.PREPARING
    assert [event.to_state for event in store.list_events(task.id)] == [
        TaskState.AWAITING_APPROVAL,
        TaskState.QUEUED,
        TaskState.PREPARING,
    ]

    with pytest.raises(InvalidTaskTransition):
        store.transition_task(task.id, TaskState.COMPLETED, actor="runner")


def test_draft_can_be_submitted_and_revised_before_approval(
    store: WorkflowStore,
) -> None:
    draft = store.create_draft("yamansari", "Initial request")

    assert draft.state is TaskState.CREATED
    submitted = store.submit_proposal(
        draft.id,
        instruction="Initial request",
        proposal="Initial plan",
        branch=f"agent/hermes/{draft.id}-initial-request",
        actor="telegram:123",
    )
    revised = store.revise_proposal(
        draft.id,
        instruction="Revised request",
        proposal="Revised plan",
        branch=f"agent/hermes/{draft.id}-revised-request",
        actor="telegram:123",
    )

    assert submitted.state is TaskState.AWAITING_APPROVAL
    assert revised.instruction == "Revised request"
    assert revised.proposal == "Revised plan"
    assert revised.branch.endswith("revised-request")


def test_approved_tasks_keep_fifo_order(store: WorkflowStore) -> None:
    first = store.create_task("yamansari", "First", "First proposal", "agent/first")
    second = store.create_task("yamansari", "Second", "Second proposal", "agent/second")

    store.approve_task(first.id, actor="owner")
    store.approve_task(second.id, actor="owner")

    queue = store.list_queue()
    assert [task.id for task in queue] == [first.id, second.id]
    assert [task.queue_position for task in queue] == [1, 2]


def test_project_lock_is_unique_and_releasable(store: WorkflowStore) -> None:
    first = store.create_task("yamansari", "First", "Proposal", "agent/first")
    second = store.create_task("yamansari", "Second", "Proposal", "agent/second")

    assert store.acquire_project_lock("yamansari", first.id) is True
    assert store.acquire_project_lock("yamansari", second.id) is False
    store.release_project_lock("yamansari", first.id)
    assert store.acquire_project_lock("yamansari", second.id) is True


def test_restart_marks_active_tasks_failed_and_releases_locks(
    store: WorkflowStore,
) -> None:
    task = store.create_task("yamansari", "Run", "Proposal", "agent/run")
    store.approve_task(task.id, actor="owner")
    store.transition_task(task.id, TaskState.PREPARING, actor="runner")
    assert store.acquire_project_lock("yamansari", task.id) is True

    recovered = store.recover_interrupted_tasks()

    assert recovered == [task.id]
    assert store.get_task(task.id).state is TaskState.FAILED
    replacement = store.create_task("yamansari", "Next", "Proposal", "agent/next")
    assert store.acquire_project_lock("yamansari", replacement.id) is True


def test_handoffs_survive_and_backup_is_consistent(
    store: WorkflowStore, tmp_path: Path
) -> None:
    task = store.create_task("yamansari", "Run", "Proposal", "agent/run")
    store.record_handoff(task.id, "Changed deployment docs; tests passed.")
    backup_path = tmp_path / "backups" / "workflow.db"

    store.backup_to(backup_path)

    restored = WorkflowStore(backup_path)
    assert restored.get_task(task.id).instruction == "Run"
    assert restored.latest_handoff(task.id) == "Changed deployment docs; tests passed."
    with sqlite3.connect(backup_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_prune_logs_removes_only_expired_files(
    store: WorkflowStore, tmp_path: Path
) -> None:
    task = store.create_task("yamansari", "Run", "Proposal", "agent/run")
    old_file = tmp_path / "old.log"
    current_file = tmp_path / "current.log"
    old_file.write_text("old", encoding="utf-8")
    current_file.write_text("current", encoding="utf-8")
    now = datetime.now(timezone.utc)
    store.record_raw_log(task.id, old_file, created_at=now - timedelta(days=31))
    store.record_raw_log(task.id, current_file, created_at=now)

    removed = store.prune_logs(now=now, retention_days=30)

    assert removed == [old_file]
    assert not old_file.exists()
    assert current_file.exists()
