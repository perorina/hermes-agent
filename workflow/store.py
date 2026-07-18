from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from workflow.models import (
    AuditRecord,
    Project,
    TaskEvent,
    TaskRuntime,
    TaskState,
    WorkflowTask,
)


class InvalidTaskTransition(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.CREATED: {TaskState.AWAITING_APPROVAL, TaskState.CANCELLED},
    TaskState.AWAITING_APPROVAL: {TaskState.QUEUED, TaskState.CANCELLED},
    TaskState.QUEUED: {TaskState.PREPARING, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.PREPARING: {
        TaskState.RUNNING,
        TaskState.BLOCKED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.RUNNING: {
        TaskState.WAITING_FOR_USER,
        TaskState.TESTING,
        TaskState.PUSHED,
        TaskState.BLOCKED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.WAITING_FOR_USER: {
        TaskState.RUNNING,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.TESTING: {
        TaskState.RUNNING,
        TaskState.PUSHED,
        TaskState.BLOCKED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.PUSHED: {
        TaskState.AWAITING_PR,
        TaskState.COMPLETED,
        TaskState.FAILED,
    },
    TaskState.AWAITING_PR: {
        TaskState.MONITORING_PR,
        TaskState.RUNNING,
        TaskState.COMPLETED,
        TaskState.CANCELLED,
    },
    TaskState.MONITORING_PR: {
        TaskState.QUEUED,
        TaskState.COMPLETED,
        TaskState.BLOCKED,
        TaskState.FAILED,
    },
    TaskState.BLOCKED: {TaskState.QUEUED, TaskState.CANCELLED},
    TaskState.FAILED: {TaskState.QUEUED, TaskState.CANCELLED},
    TaskState.COMPLETED: set(),
    TaskState.CANCELLED: set(),
}

_INTERRUPTED_STATES = {
    TaskState.PREPARING,
    TaskState.RUNNING,
    TaskState.WAITING_FOR_USER,
    TaskState.TESTING,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name_with_owner TEXT NOT NULL UNIQUE,
                    default_branch TEXT NOT NULL,
                    preferred_node TEXT NOT NULL,
                    fallback_node TEXT,
                    production INTEGER NOT NULL DEFAULT 0,
                    retry_limit INTEGER NOT NULL DEFAULT 3,
                    minimum_free_disk_gb INTEGER NOT NULL DEFAULT 10
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    instruction TEXT NOT NULL,
                    proposal TEXT NOT NULL,
                    branch TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    queue_position INTEGER,
                    repair_attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_locks (
                    project_id TEXT PRIMARY KEY REFERENCES projects(id),
                    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
                    acquired_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS global_locks (
                    name TEXT PRIMARY KEY,
                    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
                    acquired_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id),
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS handoffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS raw_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id),
                    path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pull_requests (
                    task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
                    repository TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(repository, number)
                );

                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event_name TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER REFERENCES tasks(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_runtime (
                    task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
                    node_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    claude_session_id TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "repair_attempts" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN repair_attempts INTEGER NOT NULL DEFAULT 0"
                )

    def save_project(self, project: Project) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO projects (
                    id, name_with_owner, default_branch, preferred_node,
                    fallback_node, production, retry_limit, minimum_free_disk_gb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name_with_owner = excluded.name_with_owner,
                    default_branch = excluded.default_branch,
                    preferred_node = excluded.preferred_node,
                    fallback_node = excluded.fallback_node,
                    production = excluded.production,
                    retry_limit = excluded.retry_limit,
                    minimum_free_disk_gb = excluded.minimum_free_disk_gb
                """,
                (
                    project.id,
                    project.name_with_owner,
                    project.default_branch,
                    project.preferred_node,
                    project.fallback_node,
                    int(project.production),
                    project.retry_limit,
                    project.minimum_free_disk_gb,
                ),
            )

    def get_project(self, project_id: str) -> Project:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return Project(
            id=row["id"],
            name_with_owner=row["name_with_owner"],
            default_branch=row["default_branch"],
            preferred_node=row["preferred_node"],
            fallback_node=row["fallback_node"],
            production=bool(row["production"]),
            retry_limit=int(row["retry_limit"]),
            minimum_free_disk_gb=int(row["minimum_free_disk_gb"]),
        )

    def create_task(
        self,
        project_id: str,
        instruction: str,
        proposal: str,
        branch: str,
    ) -> WorkflowTask:
        now = _utc_now()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    project_id, instruction, proposal, branch, state,
                    queue_position, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    project_id,
                    instruction,
                    proposal,
                    branch,
                    TaskState.AWAITING_APPROVAL.value,
                    now,
                    now,
                ),
            )
            task_id = int(cursor.lastrowid)
            self._insert_event(
                conn,
                task_id,
                None,
                TaskState.AWAITING_APPROVAL,
                "workflow",
                None,
                now,
            )
        return self.get_task(task_id)

    def create_draft(self, project_id: str, instruction: str) -> WorkflowTask:
        now = _utc_now()
        temporary_branch = f"draft/{uuid.uuid4().hex}"
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    project_id, instruction, proposal, branch, state,
                    queue_position, created_at, updated_at
                ) VALUES (?, ?, '', ?, ?, NULL, ?, ?)
                """,
                (
                    project_id,
                    instruction,
                    temporary_branch,
                    TaskState.CREATED.value,
                    now,
                    now,
                ),
            )
            task_id = int(cursor.lastrowid)
            self._insert_event(
                conn, task_id, None, TaskState.CREATED, "workflow", None, now
            )
        return self.get_task(task_id)

    def submit_proposal(
        self,
        task_id: int,
        instruction: str,
        proposal: str,
        branch: str,
        actor: str,
    ) -> WorkflowTask:
        with self._transaction() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = TaskState(row["state"])
            self._validate_transition(current, TaskState.AWAITING_APPROVAL)
            now = _utc_now()
            conn.execute(
                """
                UPDATE tasks
                SET instruction = ?, proposal = ?, branch = ?, state = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    instruction,
                    proposal,
                    branch,
                    TaskState.AWAITING_APPROVAL.value,
                    now,
                    task_id,
                ),
            )
            self._insert_event(
                conn,
                task_id,
                current,
                TaskState.AWAITING_APPROVAL,
                actor,
                None,
                now,
            )
        return self.get_task(task_id)

    def revise_proposal(
        self,
        task_id: int,
        instruction: str,
        proposal: str,
        branch: str,
        actor: str,
    ) -> WorkflowTask:
        with self._transaction() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = TaskState(row["state"])
            if current is not TaskState.AWAITING_APPROVAL:
                raise InvalidTaskTransition(
                    f"cannot revise proposal while task is {current.value}"
                )
            now = _utc_now()
            conn.execute(
                """
                UPDATE tasks
                SET instruction = ?, proposal = ?, branch = ?, updated_at = ?
                WHERE id = ?
                """,
                (instruction, proposal, branch, now, task_id),
            )
            self._insert_event(
                conn,
                task_id,
                current,
                current,
                actor,
                "proposal revised",
                now,
            )
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> WorkflowTask:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_from_row(row)

    def approve_task(self, task_id: int, actor: str) -> WorkflowTask:
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = TaskState(row["state"])
            self._validate_transition(current, TaskState.QUEUED)
            queue_position = int(
                conn.execute(
                    "SELECT COALESCE(MAX(queue_position), 0) + 1 FROM tasks"
                ).fetchone()[0]
            )
            now = _utc_now()
            conn.execute(
                "UPDATE tasks SET state = ?, queue_position = ?, updated_at = ? WHERE id = ?",
                (TaskState.QUEUED.value, queue_position, now, task_id),
            )
            self._insert_event(
                conn, task_id, current, TaskState.QUEUED, actor, None, now
            )
        return self.get_task(task_id)

    def transition_task(
        self,
        task_id: int,
        to_state: TaskState,
        actor: str,
        detail: Optional[str] = None,
    ) -> WorkflowTask:
        with self._transaction() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = TaskState(row["state"])
            self._validate_transition(current, to_state)
            now = _utc_now()
            conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                (to_state.value, now, task_id),
            )
            self._insert_event(conn, task_id, current, to_state, actor, detail, now)
        return self.get_task(task_id)

    def list_queue(self) -> list[WorkflowTask]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE state = ?
                ORDER BY queue_position ASC, id ASC
                """,
                (TaskState.QUEUED.value,),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def requeue_task(self, task_id: int, actor: str, detail: str) -> WorkflowTask:
        with self._transaction() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = TaskState(row["state"])
            self._validate_transition(current, TaskState.QUEUED)
            queue_position = int(
                conn.execute(
                    "SELECT COALESCE(MAX(queue_position), 0) + 1 FROM tasks"
                ).fetchone()[0]
            )
            now = _utc_now()
            conn.execute(
                """
                UPDATE tasks SET state = ?, queue_position = ?, updated_at = ?
                WHERE id = ?
                """,
                (TaskState.QUEUED.value, queue_position, now, task_id),
            )
            self._insert_event(
                conn, task_id, current, TaskState.QUEUED, actor, detail, now
            )
        return self.get_task(task_id)

    def acquire_project_lock(self, project_id: str, task_id: int) -> bool:
        try:
            with self._transaction() as conn:
                conn.execute(
                    "INSERT INTO project_locks (project_id, task_id, acquired_at) VALUES (?, ?, ?)",
                    (project_id, task_id, _utc_now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def release_project_lock(self, project_id: str, task_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM project_locks WHERE project_id = ? AND task_id = ?",
                (project_id, task_id),
            )

    def acquire_global_lock(self, name: str, task_id: int) -> bool:
        try:
            with self._transaction() as conn:
                conn.execute(
                    "INSERT INTO global_locks (name, task_id, acquired_at) VALUES (?, ?, ?)",
                    (name, task_id, _utc_now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def release_global_lock(self, name: str, task_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM global_locks WHERE name = ? AND task_id = ?",
                (name, task_id),
            )

    def recover_interrupted_tasks(self) -> list[int]:
        state_values = tuple(state.value for state in _INTERRUPTED_STATES)
        placeholders = ",".join("?" for _ in state_values)
        with self._transaction() as conn:
            rows = conn.execute(
                f"SELECT id, state FROM tasks WHERE state IN ({placeholders}) ORDER BY id",
                state_values,
            ).fetchall()
            now = _utc_now()
            for row in rows:
                task_id = int(row["id"])
                current = TaskState(row["state"])
                conn.execute(
                    "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                    (TaskState.FAILED.value, now, task_id),
                )
                conn.execute("DELETE FROM project_locks WHERE task_id = ?", (task_id,))
                conn.execute("DELETE FROM global_locks WHERE task_id = ?", (task_id,))
                self._insert_event(
                    conn,
                    task_id,
                    current,
                    TaskState.FAILED,
                    "recovery",
                    "Hermes restarted while task was active; artifacts were preserved.",
                    now,
                )
        return [int(row["id"]) for row in rows]

    def list_events(self, task_id: int) -> list[TaskEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        return [
            TaskEvent(
                id=int(row["id"]),
                task_id=int(row["task_id"]),
                from_state=TaskState(row["from_state"]) if row["from_state"] else None,
                to_state=TaskState(row["to_state"]),
                actor=row["actor"],
                detail=row["detail"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def record_handoff(self, task_id: int, content: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO handoffs (task_id, content, created_at) VALUES (?, ?, ?)",
                (task_id, content, _utc_now()),
            )

    def record_audit(
        self,
        task_id: Optional[int],
        actor: str,
        action: str,
        detail: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (task_id, actor, action, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, actor, action, detail, _utc_now()),
            )

    def list_audit(self, task_id: Optional[int] = None) -> list[AuditRecord]:
        with self._connect() as conn:
            if task_id is None:
                rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE task_id = ? ORDER BY id",
                    (task_id,),
                ).fetchall()
        return [
            AuditRecord(
                id=int(row["id"]),
                task_id=int(row["task_id"]) if row["task_id"] is not None else None,
                actor=row["actor"],
                action=row["action"],
                detail=row["detail"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def latest_handoff(self, task_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM handoffs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return row["content"] if row else None

    def record_pull_request(
        self, task_id: int, repository: str, number: int, url: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pull_requests (task_id, repository, number, url, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    repository = excluded.repository,
                    number = excluded.number,
                    url = excluded.url
                """,
                (task_id, repository, number, url, _utc_now()),
            )

    def set_task_runtime(
        self,
        task_id: int,
        node_id: str,
        workspace: str,
        claude_session_id: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_runtime (
                    task_id, node_id, workspace, claude_session_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    node_id = excluded.node_id,
                    workspace = excluded.workspace,
                    claude_session_id = excluded.claude_session_id,
                    updated_at = excluded.updated_at
                """,
                (task_id, node_id, workspace, claude_session_id, _utc_now()),
            )

    def get_task_runtime(self, task_id: int) -> TaskRuntime:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_runtime WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TaskRuntime(
            task_id=int(row["task_id"]),
            node_id=row["node_id"],
            workspace=row["workspace"],
            claude_session_id=row["claude_session_id"],
            updated_at=row["updated_at"],
        )

    def record_webhook_delivery(self, delivery_id: str, event_name: str) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO webhook_deliveries (delivery_id, event_name, received_at)
                    VALUES (?, ?, ?)
                    """,
                    (delivery_id, event_name, _utc_now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def has_webhook_delivery(self, delivery_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return row is not None

    def find_task_by_repository_branch(
        self, repository: str, branch: str
    ) -> Optional[WorkflowTask]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT tasks.* FROM tasks
                JOIN projects ON projects.id = tasks.project_id
                WHERE projects.name_with_owner = ? AND tasks.branch = ?
                ORDER BY tasks.id DESC LIMIT 1
                """,
                (repository, branch),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def increment_repair_attempt(self, task_id: int) -> int:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT repair_attempts FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            attempts = int(row["repair_attempts"]) + 1
            conn.execute(
                "UPDATE tasks SET repair_attempts = ?, updated_at = ? WHERE id = ?",
                (attempts, _utc_now(), task_id),
            )
        return attempts

    def repair_attempts(self, task_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT repair_attempts FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return int(row["repair_attempts"])

    def record_raw_log(
        self,
        task_id: int,
        path: Path | str,
        created_at: Optional[datetime] = None,
    ) -> None:
        timestamp = created_at or datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO raw_logs (task_id, path, created_at) VALUES (?, ?, ?)",
                (task_id, str(Path(path)), timestamp.isoformat()),
            )

    def prune_logs(
        self,
        now: Optional[datetime] = None,
        retention_days: int = 30,
    ) -> list[Path]:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT id, path FROM raw_logs WHERE created_at < ? ORDER BY id",
                (cutoff.isoformat(),),
            ).fetchall()
            removed: list[Path] = []
            for row in rows:
                path = Path(row["path"])
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                removed.append(path)
                conn.execute("DELETE FROM raw_logs WHERE id = ?", (row["id"],))
        return removed

    def backup_to(self, destination: Path | str) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
            status = target.execute("PRAGMA integrity_check").fetchone()[0]
        if status != "ok":
            raise sqlite3.DatabaseError(f"workflow backup integrity check failed: {status}")
        return destination

    @staticmethod
    def _validate_transition(current: TaskState, target: TaskState) -> None:
        if target not in _ALLOWED_TRANSITIONS[current]:
            raise InvalidTaskTransition(f"cannot transition {current.value} -> {target.value}")

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection,
        task_id: int,
        from_state: Optional[TaskState],
        to_state: TaskState,
        actor: str,
        detail: Optional[str],
        created_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_events (
                task_id, from_state, to_state, actor, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                from_state.value if from_state else None,
                to_state.value,
                actor,
                detail,
                created_at,
            ),
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> WorkflowTask:
        return WorkflowTask(
            id=int(row["id"]),
            project_id=row["project_id"],
            instruction=row["instruction"],
            proposal=row["proposal"],
            branch=row["branch"],
            state=TaskState(row["state"]),
            queue_position=row["queue_position"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
