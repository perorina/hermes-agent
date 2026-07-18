from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Optional, Sequence

from workflow.config import ProjectConfig
from workflow.models import TaskState, WorkflowTask
from workflow.nodes import CommandResult, WindowsNode
from workflow.prompts import build_checkpoint_prompt, build_task_prompt
from workflow.store import WorkflowStore


class DirtyRepositoryError(RuntimeError):
    pass


class RemoteCommandError(RuntimeError):
    pass


class DestructiveConfirmationRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaudeResult:
    session_id: Optional[str]
    summary: str
    question: Optional[str]
    log_path: Path


@dataclass(frozen=True)
class PushResult:
    commit: str
    changed_files: tuple[str, ...]


class TaskExecutor:
    def __init__(
        self,
        store: WorkflowStore,
        node: Any,
        project: ProjectConfig,
        worktree_root: PurePath,
        logs_dir: Path,
    ):
        self.store = store
        self.node = node
        self.project = project
        self.worktree_root = worktree_root
        self.logs_dir = Path(logs_dir)

    async def prepare(self, task: WorkflowTask) -> PurePath:
        primary = self._primary_clone()
        status = await self.node.run(
            ["git", "-C", str(primary), "status", "--porcelain"], timeout=30.0
        )
        self._require_ok(status, "inspect primary clone")
        if status.stdout.strip():
            raise DirtyRepositoryError("primary clone is dirty; task was not started")
        fetch = await self.node.run(
            [
                "git",
                "-C",
                str(primary),
                "fetch",
                "--prune",
                "origin",
                self.project.default_branch,
            ],
            timeout=120.0,
        )
        self._require_ok(fetch, "fetch default branch")
        workspace = self.worktree_root / task.branch.removeprefix("agent/hermes/")
        created = await self.node.run(
            [
                "git",
                "-C",
                str(primary),
                "worktree",
                "add",
                "-b",
                task.branch,
                str(workspace),
                f"origin/{self.project.default_branch}",
            ],
            timeout=120.0,
        )
        self._require_ok(created, "create task worktree")
        return workspace

    async def run_claude(
        self, task: WorkflowTask, workspace: PurePath
    ) -> ClaudeResult:
        current = self.store.get_task(task.id)
        if current.state is TaskState.PREPARING:
            self.store.transition_task(task.id, TaskState.RUNNING, actor="executor")
        prompt = build_task_prompt(task, self.project.checks)
        return await self._invoke_claude(task, workspace, prompt)

    async def answer_claude(
        self,
        task: WorkflowTask,
        workspace: PurePath,
        session_id: str,
        answer: str,
    ) -> ClaudeResult:
        self.store.transition_task(task.id, TaskState.RUNNING, actor="telegram-owner")
        return await self._invoke_claude(
            task,
            workspace,
            answer,
            resume_session_id=session_id,
        )

    async def checkpoint(
        self,
        task: WorkflowTask,
        workspace: PurePath,
        session_id: str,
    ) -> ClaudeResult:
        return await self._invoke_claude(
            task,
            workspace,
            build_checkpoint_prompt(),
            resume_session_id=session_id,
            timeout=float(self.project.checkpoint_timeout_seconds),
            update_state=False,
        )

    async def run_checks(
        self, task: WorkflowTask, workspace: PurePath
    ) -> tuple[tuple[str, bool, str], ...]:
        self.store.transition_task(task.id, TaskState.TESTING, actor="executor")
        outcomes = []
        for command in self.project.checks:
            shell_argv = (
                ["powershell.exe", "-NoProfile", "-Command", command]
                if isinstance(self.node, WindowsNode)
                or getattr(self.node, "kind", "linux") == "windows"
                else ["sh", "-lc", command]
            )
            result = await self._run_in(workspace, shell_argv, timeout=1800.0)
            summary = (result.stdout or result.stderr).strip()
            outcomes.append((command, result.returncode == 0, summary))
            if result.returncode != 0:
                raise RemoteCommandError(f"check failed: {command}: {summary}")
        return tuple(outcomes)

    async def commit_and_push(
        self, task: WorkflowTask, workspace: PurePath
    ) -> PushResult:
        status = await self.node.run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z"],
            timeout=30.0,
        )
        self._require_ok(status, "inspect task worktree")
        changed_files = _porcelain_paths(status.stdout)
        if status.stdout.strip():
            added = await self.node.run(
                ["git", "-C", str(workspace), "add", "--all"], timeout=30.0
            )
            self._require_ok(added, "stage changes")
            committed = await self.node.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "commit",
                    "-m",
                    f"chore: complete Hermes task {task.id}",
                ],
                timeout=120.0,
            )
            self._require_ok(committed, "commit task changes")
        revision = await self.node.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"], timeout=30.0
        )
        self._require_ok(revision, "read task commit")
        pushed = await self.node.run(
            [
                "git",
                "-C",
                str(workspace),
                "push",
                "-u",
                "origin",
                task.branch,
            ],
            timeout=300.0,
        )
        self._require_ok(pushed, "push agent branch")
        self.store.transition_task(task.id, TaskState.PUSHED, actor="executor")
        return PushResult(revision.stdout.strip(), changed_files)

    async def cancel(
        self,
        task: WorkflowTask,
        workspace: PurePath,
        confirmed: bool = False,
    ) -> None:
        commits = await self.node.run(
            [
                "git",
                "-C",
                str(workspace),
                "rev-list",
                "--count",
                f"origin/{self.project.default_branch}..HEAD",
            ],
            timeout=30.0,
        )
        self._require_ok(commits, "inspect task commits")
        has_commits = int(commits.stdout.strip() or "0") > 0
        if has_commits and not confirmed:
            raise DestructiveConfirmationRequired(
                "task branch contains commits; a second confirmation is required"
            )
        primary = self._primary_clone()
        removed = await self.node.run(
            ["git", "-C", str(primary), "worktree", "remove", str(workspace)],
            timeout=120.0,
        )
        self._require_ok(removed, "remove task worktree")
        deleted = await self.node.run(
            ["git", "-C", str(primary), "branch", "-D", task.branch], timeout=30.0
        )
        self._require_ok(deleted, "delete local task branch")
        if has_commits:
            await self.node.run(
                ["git", "-C", str(primary), "push", "origin", "--delete", task.branch],
                timeout=120.0,
            )

    async def _invoke_claude(
        self,
        task: WorkflowTask,
        workspace: PurePath,
        prompt: str,
        resume_session_id: Optional[str] = None,
        timeout: float = 21600.0,
        update_state: bool = True,
    ) -> ClaudeResult:
        argv = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if resume_session_id:
            argv.extend(["--resume", resume_session_id])
        argv.append(prompt)
        result = await self._run_in(workspace, argv, timeout=timeout)
        log_path = self._write_log(task.id, result)
        self.store.record_raw_log(task.id, log_path)
        if result.returncode != 0:
            raise RemoteCommandError(result.stderr.strip() or "Claude Code failed")
        session_id, summary = _parse_stream_json(result.stdout)
        question = _extract_marker(summary, "HERMES_QUESTION")
        if question and update_state:
            self.store.transition_task(
                task.id,
                TaskState.WAITING_FOR_USER,
                actor="executor",
                detail=question,
            )
        handoff = _extract_marker(summary, "HERMES_HANDOFF")
        if handoff:
            self.store.record_handoff(task.id, handoff)
        return ClaudeResult(session_id or resume_session_id, summary, question, log_path)

    async def _run_in(
        self, workspace: PurePath, argv: Sequence[str], timeout: float
    ) -> CommandResult:
        run_in = getattr(self.node, "run_in", None)
        if callable(run_in):
            return await run_in(str(workspace), argv, timeout=timeout)
        return await self.node.run(argv, timeout=timeout)

    def _write_log(self, task_id: int, result: CommandResult) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.logs_dir / f"task-{task_id}-{stamp}.jsonl"
        body = result.stdout
        if result.stderr:
            body += f"\n{{\"type\":\"stderr\",\"text\":{json.dumps(result.stderr)}}}\n"
        path.write_text(body, encoding="utf-8")
        return path

    def _primary_clone(self) -> PurePath:
        try:
            return self.project.primary_clones[self.node.id]
        except KeyError as exc:
            raise ValueError(
                f"project {self.project.id!r} has no primary clone for node {self.node.id!r}"
            ) from exc

    @staticmethod
    def _require_ok(result: CommandResult, action: str) -> None:
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RemoteCommandError(f"failed to {action}: {detail}")


def _parse_stream_json(output: str) -> tuple[Optional[str], str]:
    session_id = None
    summaries = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = event.get("session_id") or session_id
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            summaries.append(event["result"])
    return session_id, "\n".join(summaries).strip()


def _extract_marker(value: str, marker: str) -> Optional[str]:
    match = re.search(rf"(?m)^{re.escape(marker)}:\s*(.+)$", value)
    return match.group(1).strip() if match else None


def _porcelain_paths(output: str) -> tuple[str, ...]:
    records = output.split("\0") if "\0" in output else output.splitlines()
    paths = []
    skip_original_rename_path = False
    for record in records:
        if not record:
            continue
        if skip_original_rename_path:
            skip_original_rename_path = False
            continue
        if len(record) < 4:
            continue
        status = record[:2]
        paths.append(record[3:])
        if "R" in status or "C" in status:
            skip_original_rename_path = True
    return tuple(paths)
