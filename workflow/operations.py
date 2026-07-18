from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Optional

from workflow.nodes import NodeReadiness
from workflow.store import WorkflowStore


class OperationApprovalRequired(RuntimeError):
    pass


Sleep = Callable[[float], Awaitable[None]]
FallbackStarter = Callable[[int, str, bool], Awaitable[None]]


class WorkflowOperations:
    def __init__(self, store: WorkflowStore):
        self.store = store

    async def wake_pc(
        self,
        task_id: int,
        stb_node: Any,
        command: Sequence[str],
        approved: bool,
    ) -> None:
        self._require_approval(approved, "wake PC")
        if not command:
            raise ValueError("wake command cannot be empty")
        result = await stb_node.run(command, timeout=30.0)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "STB wake command failed")
        self.store.record_audit(
            task_id,
            actor="telegram-owner",
            action="wake_pc",
            detail=f"node={stb_node.id}",
        )

    async def wait_for_pc(
        self,
        pc_node: Any,
        minimum_free_disk_gb: int,
        timeout_seconds: float = 180.0,
        interval_seconds: float = 5.0,
        sleep: Sleep = asyncio.sleep,
    ) -> NodeReadiness:
        attempts = max(1, int(timeout_seconds // interval_seconds) + 1)
        last = NodeReadiness(False, False, False, None, (), "PC did not become ready")
        for attempt in range(attempts):
            last = await pc_node.readiness(minimum_free_disk_gb)
            if last.ready:
                return last
            if attempt + 1 < attempts:
                await sleep(interval_seconds)
        return last

    async def power_pc(
        self,
        task_id: int,
        pc_node: Any,
        action: str,
        approved: bool,
    ) -> None:
        if action not in {"sleep", "shutdown"}:
            raise ValueError("power action must be sleep or shutdown")
        self._require_approval(approved, f"PC {action}")
        command = (
            ["shutdown.exe", "/s", "/t", "0"]
            if action == "shutdown"
            else ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
        )
        result = await pc_node.run(command, timeout=30.0)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"PC {action} failed")
        self.store.record_audit(
            task_id,
            actor="telegram-owner",
            action=f"power_{action}",
            detail=f"node={pc_node.id}",
        )

    async def move_to_fallback(
        self,
        task_id: int,
        approved: bool,
        start_fallback: FallbackStarter,
    ) -> None:
        self._require_approval(approved, "move task to fallback")
        handoff = self.store.latest_handoff(task_id)
        if not handoff:
            raise ValueError("a checkpoint handoff is required before fallback")
        await start_fallback(task_id, handoff, False)
        self.store.record_audit(
            task_id,
            actor="telegram-owner",
            action="move_to_fallback",
            detail="fresh Claude session from stored handoff",
        )

    def daily_backup(
        self, backup_dir: Path | str, on_date: Optional[date] = None
    ) -> Path:
        directory = Path(backup_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target_date = on_date or date.today()
        target = directory / f"workflow-{target_date.strftime('%Y%m%d')}.db"
        self.store.backup_to(target)
        backups = sorted(directory.glob("workflow-????????.db"), reverse=True)
        for expired in backups[7:]:
            expired.unlink()
        self.store.record_audit(
            None,
            actor="scheduler",
            action="workflow_backup",
            detail=str(target),
        )
        return target

    def cleanup_expired_logs(self) -> list[Path]:
        return self.store.prune_logs(retention_days=30)

    @staticmethod
    def _require_approval(approved: bool, action: str) -> None:
        if not approved:
            raise OperationApprovalRequired(f"{action} requires owner approval")
