from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from workflow.models import TaskState
from workflow.store import WorkflowStore


@dataclass(frozen=True)
class WebhookResponse:
    status: int
    message: str


class WebhookApp:
    def __init__(
        self,
        store: WorkflowStore,
        secret: bytes,
        enqueue_repair: Callable[[int, str], None],
    ):
        self.store = store
        self.secret = secret
        self.enqueue_repair = enqueue_repair

    def handle(self, headers: Mapping[str, str], body: bytes) -> WebhookResponse:
        normalized = {key.lower(): value for key, value in headers.items()}
        signature = normalized.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return WebhookResponse(401, "invalid signature")
        delivery = normalized.get("x-github-delivery", "").strip()
        event_name = normalized.get("x-github-event", "").strip()
        if not delivery or not event_name:
            return WebhookResponse(400, "missing GitHub headers")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return WebhookResponse(400, "invalid JSON")
        if not self.store.record_webhook_delivery(delivery, event_name):
            return WebhookResponse(200, "duplicate ignored")

        if event_name == "check_suite":
            self._handle_check_suite(payload)
        elif event_name in {"pull_request_review", "pull_request_review_comment"}:
            self._handle_review(event_name, payload)
        elif event_name == "pull_request":
            self._handle_pull_request(payload)
        return WebhookResponse(202, "accepted")

    def _handle_check_suite(self, payload: Mapping[str, Any]) -> None:
        suite = _mapping(payload.get("check_suite"))
        if suite.get("conclusion") not in {"failure", "timed_out", "cancelled"}:
            return
        self._queue_repair(payload, str(suite.get("head_branch", "")), "GitHub check suite failed")

    def _handle_review(self, event_name: str, payload: Mapping[str, Any]) -> None:
        review = _mapping(payload.get("review"))
        action = str(payload.get("action", ""))
        actionable = review.get("state") == "changes_requested" or (
            event_name == "pull_request_review_comment" and action == "created"
        )
        if not actionable:
            return
        pull_request = _mapping(payload.get("pull_request"))
        head = _mapping(pull_request.get("head"))
        self._queue_repair(payload, str(head.get("ref", "")), "Actionable PR review feedback")

    def _handle_pull_request(self, payload: Mapping[str, Any]) -> None:
        if payload.get("action") != "closed":
            return
        pull_request = _mapping(payload.get("pull_request"))
        head = _mapping(pull_request.get("head"))
        task = self._find_task(payload, str(head.get("ref", "")))
        if task and task.state is TaskState.MONITORING_PR:
            self.store.transition_task(task.id, TaskState.COMPLETED, actor="github")

    def _queue_repair(
        self, payload: Mapping[str, Any], branch: str, reason: str
    ) -> None:
        task = self._find_task(payload, branch)
        if task is None or task.state is not TaskState.MONITORING_PR:
            return
        project = self.store.get_project(task.project_id)
        if self.store.repair_attempts(task.id) >= project.retry_limit:
            self.store.transition_task(
                task.id,
                TaskState.BLOCKED,
                actor="github",
                detail=f"repair limit exhausted: {reason}",
            )
            return
        self.store.increment_repair_attempt(task.id)
        self.store.requeue_task(task.id, actor="github", detail=reason)
        self.enqueue_repair(task.id, reason)

    def _find_task(self, payload: Mapping[str, Any], branch: str):
        repository = _mapping(payload.get("repository"))
        return self.store.find_task_by_repository_branch(
            str(repository.get("full_name", "")), branch
        )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
