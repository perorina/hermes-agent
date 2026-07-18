from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TaskState(str, Enum):
    CREATED = "created"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    TESTING = "testing"
    PUSHED = "pushed"
    AWAITING_PR = "awaiting_pr"
    MONITORING_PR = "monitoring_pr"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Project:
    id: str
    name_with_owner: str
    default_branch: str
    preferred_node: str
    fallback_node: Optional[str] = None
    production: bool = False
    retry_limit: int = 3
    minimum_free_disk_gb: int = 10


@dataclass(frozen=True)
class WorkflowTask:
    id: int
    project_id: str
    instruction: str
    proposal: str
    branch: str
    state: TaskState
    queue_position: Optional[int]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskEvent:
    id: int
    task_id: int
    from_state: Optional[TaskState]
    to_state: TaskState
    actor: str
    detail: Optional[str]
    created_at: str


@dataclass(frozen=True)
class AuditRecord:
    id: int
    task_id: Optional[int]
    actor: str
    action: str
    detail: Optional[str]
    created_at: str


@dataclass(frozen=True)
class TaskRuntime:
    task_id: int
    node_id: str
    workspace: str
    claude_session_id: Optional[str]
    updated_at: str
