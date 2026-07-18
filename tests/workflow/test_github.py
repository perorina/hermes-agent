from __future__ import annotations

from pathlib import Path

import pytest

from workflow.config import ProjectConfig
from workflow.github import ApprovalRequired, WorkflowGitHub
from workflow.models import Project, TaskState
from workflow.store import WorkflowStore


def _pushed_task(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.db")
    store.save_project(Project("demo", "perorina/demo", "main", "pc"))
    task = store.create_task(
        "demo", "Update health check", "Proposal", "agent/hermes/1-health-check"
    )
    store.approve_task(task.id, actor="owner")
    for state in (
        TaskState.PREPARING,
        TaskState.RUNNING,
        TaskState.TESTING,
        TaskState.PUSHED,
    ):
        store.transition_task(task.id, state, actor="test")
    project = ProjectConfig(
        id="demo",
        name_with_owner="perorina/demo",
        default_branch="main",
        preferred_node="pc",
    )
    return store, store.get_task(task.id), project


def test_pr_creation_requires_explicit_owner_approval(tmp_path: Path) -> None:
    store, task, project = _pushed_task(tmp_path)
    calls = []
    github = WorkflowGitHub(store, run=lambda argv: calls.append(argv) or "")

    with pytest.raises(ApprovalRequired):
        github.create_pr(task, project, approved_by_owner=False)

    assert calls == []
    assert store.get_task(task.id).state is TaskState.PUSHED


def test_approved_pr_uses_gh_create_and_enters_monitoring(tmp_path: Path) -> None:
    store, task, project = _pushed_task(tmp_path)
    calls = []

    def run(argv):
        calls.append(argv)
        return "https://github.com/perorina/demo/pull/17\n"

    pull_request = WorkflowGitHub(store, run=run).create_pr(
        task, project, approved_by_owner=True
    )

    assert pull_request.number == 17
    assert store.get_task(task.id).state is TaskState.MONITORING_PR
    assert calls[0][:3] == ["gh", "pr", "create"]
    assert "--head" in calls[0]
    assert task.branch in calls[0]
    assert "merge" not in calls[0]
    assert not hasattr(WorkflowGitHub, "merge")
