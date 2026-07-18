from __future__ import annotations

from workflow.models import TaskState, WorkflowTask
from workflow.prompts import build_checkpoint_prompt, build_task_prompt


def _task(instruction: str) -> WorkflowTask:
    return WorkflowTask(
        id=7,
        project_id="demo",
        instruction=instruction,
        proposal="Implement the requested change and run tests.",
        branch="agent/hermes/7-demo",
        state=TaskState.PREPARING,
        queue_position=1,
        created_at="2026-07-18T00:00:00+00:00",
        updated_at="2026-07-18T00:00:00+00:00",
    )


def test_task_prompt_contains_boundaries_and_redacts_secret_assignments() -> None:
    prompt = build_task_prompt(
        _task("Update docs. SESSION_SECRET=hunter2 and GH_TOKEN=ghp_example"),
        checks=("pytest -q",),
    )

    assert "SESSION_SECRET=[REDACTED]" in prompt
    assert "GH_TOKEN=[REDACTED]" in prompt
    assert "hunter2" not in prompt
    assert "ghp_example" not in prompt
    assert "Do not merge" in prompt
    assert "HERMES_QUESTION:" in prompt
    assert "pytest -q" in prompt


def test_checkpoint_prompt_requests_machine_readable_handoff() -> None:
    prompt = build_checkpoint_prompt()

    assert "HERMES_HANDOFF:" in prompt
    assert "current commit" in prompt
    assert "remaining work" in prompt
