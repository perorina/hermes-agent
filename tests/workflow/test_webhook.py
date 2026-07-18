from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from workflow.models import Project, TaskState
from workflow.store import WorkflowStore
from workflow.webhook import WebhookApp


SECRET = b"webhook-test-secret"


def _monitoring_store(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.db")
    store.save_project(
        Project(
            "demo",
            "perorina/demo",
            "main",
            "pc",
            retry_limit=3,
        )
    )
    task = store.create_task(
        "demo", "Update health check", "Proposal", "agent/hermes/1-health-check"
    )
    store.approve_task(task.id, actor="owner")
    for state in (
        TaskState.PREPARING,
        TaskState.RUNNING,
        TaskState.TESTING,
        TaskState.PUSHED,
        TaskState.AWAITING_PR,
        TaskState.MONITORING_PR,
    ):
        store.transition_task(task.id, state, actor="test")
    return store, store.get_task(task.id)


def _signed_headers(body: bytes, delivery: str = "delivery-1") -> dict[str, str]:
    signature = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": "check_suite",
    }


def _failure_body() -> bytes:
    return json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "perorina/demo"},
            "check_suite": {
                "head_branch": "agent/hermes/1-health-check",
                "conclusion": "failure",
            },
        }
    ).encode()


def test_rejects_invalid_signature_without_recording_delivery(tmp_path: Path) -> None:
    store, _ = _monitoring_store(tmp_path)
    body = _failure_body()
    headers = _signed_headers(body)
    headers["X-Hub-Signature-256"] = "sha256=wrong"
    app = WebhookApp(store, SECRET, enqueue_repair=lambda task_id, reason: None)

    response = app.handle(headers, body)

    assert response.status == 401
    assert store.has_webhook_delivery("delivery-1") is False


def test_deduplicates_delivery_and_queues_failed_check_once(tmp_path: Path) -> None:
    store, task = _monitoring_store(tmp_path)
    repairs = []
    body = _failure_body()
    headers = _signed_headers(body)
    app = WebhookApp(
        store, SECRET, enqueue_repair=lambda task_id, reason: repairs.append((task_id, reason))
    )

    first = app.handle(headers, body)
    duplicate = app.handle(headers, body)

    assert first.status == 202
    assert duplicate.status == 200
    assert repairs == [(task.id, "GitHub check suite failed")]
    assert store.get_task(task.id).state is TaskState.QUEUED
    assert store.get_task(task.id).queue_position is not None


def test_blocks_after_configured_repair_limit(tmp_path: Path) -> None:
    store, task = _monitoring_store(tmp_path)
    for _ in range(3):
        store.increment_repair_attempt(task.id)
    repairs = []
    body = _failure_body()
    app = WebhookApp(
        store, SECRET, enqueue_repair=lambda task_id, reason: repairs.append(task_id)
    )

    response = app.handle(_signed_headers(body, "delivery-limit"), body)

    assert response.status == 202
    assert repairs == []
    assert store.get_task(task.id).state is TaskState.BLOCKED
