from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import PurePath
from typing import Any, Optional

from workflow.config import ProjectConfig, WorkflowConfig
from workflow.executor import TaskExecutor
from workflow.github import WorkflowGitHub
from workflow.models import TaskState, WorkflowTask
from workflow.nodes import LinuxNode, WindowsNode
from workflow.runner import WorkflowRunner
from workflow.store import WorkflowStore

logger = logging.getLogger(__name__)

Action = tuple[str, str]
Notifier = Callable[[int, str, Sequence[Action]], Awaitable[None]]


class WorkflowService:
    def __init__(
        self,
        config: WorkflowConfig,
        store: WorkflowStore,
        notify: Notifier,
        nodes: Optional[Mapping[str, Any]] = None,
        executor_factory: Callable[..., Any] = TaskExecutor,
        github: Optional[WorkflowGitHub] = None,
    ):
        self.config = config
        self.store = store
        self.notify = notify
        self.nodes = dict(nodes or self._build_nodes())
        self._executor_factory = executor_factory
        self.github = github or WorkflowGitHub(store)
        self.runner = WorkflowRunner(store, self._execute)
        self.store.recover_interrupted_tasks()

    def start(self) -> None:
        self.runner.start()

    async def stop(self) -> None:
        await self.runner.stop()

    async def run_once(self) -> bool:
        return await self.runner.run_next()

    async def _execute(self, task: WorkflowTask) -> None:
        project = self._project(task.project_id)
        node = self.nodes[project.preferred_node]
        disk_root = self.config.worktree_roots.get(node.id)
        readiness = await node.readiness(
            project.minimum_free_disk_gb,
            str(disk_root) if disk_root is not None else None,
        )
        if not readiness.ready:
            reason = readiness.reason or "preferred node is not ready"
            self.store.transition_task(
                task.id, TaskState.BLOCKED, actor="runner", detail=reason
            )
            await self.notify(task.id, f"Task diblokir: {reason}", ())
            return
        executor = self._executor(project, node)
        try:
            workspace = (
                await executor.prepare_repair(task)
                if self.store.repair_attempts(task.id) > 0
                else await executor.prepare(task)
            )
            self.store.set_task_runtime(task.id, node.id, str(workspace))
            claude = await executor.run_claude(task, workspace)
            self.store.set_task_runtime(
                task.id, node.id, str(workspace), claude.session_id
            )
            if claude.question:
                await self.notify(
                    task.id,
                    claude.question,
                    (("Jawab", f"wf:answer:{task.id}"),),
                )
                return
            await self._finish_task(task, executor, workspace)
        except Exception as exc:
            await self.notify(task.id, f"Task gagal: {exc}", ())
            raise

    async def answer(self, task_id: int, answer: str) -> None:
        task = self.store.get_task(task_id)
        runtime = self.store.get_task_runtime(task_id)
        if not runtime.claude_session_id:
            raise ValueError("task has no resumable Claude session")
        project = self._project(task.project_id)
        node = self.nodes[runtime.node_id]
        executor = self._executor(project, node)
        workspace = self._remote_path(project, node.id, runtime.workspace)
        release_locks = True
        try:
            claude = await executor.answer_claude(
                task, workspace, runtime.claude_session_id, answer
            )
            self.store.set_task_runtime(
                task.id, node.id, str(workspace), claude.session_id
            )
            if claude.question:
                release_locks = False
                await self.notify(
                    task.id,
                    claude.question,
                    (("Jawab", f"wf:answer:{task.id}"),),
                )
                return
            await self._finish_task(task, executor, workspace)
        finally:
            if release_locks:
                self.store.release_project_lock(task.project_id, task.id)
                self.store.release_global_lock("claude", task.id)

    async def create_pr(self, task_id: int) -> None:
        task = self.store.get_task(task_id)
        project = self._project(task.project_id)
        pull_request = self.github.create_pr(
            task, project, approved_by_owner=True
        )
        runtime = self.store.get_task_runtime(task_id)
        node = self.nodes[runtime.node_id]
        executor = self._executor(project, node)
        workspace = self._remote_path(project, node.id, runtime.workspace)
        await executor.remove_worktree(task, workspace)
        await self.notify(
            task.id,
            f"PR #{pull_request.number} dibuat: {pull_request.url}",
            (),
        )

    async def _finish_task(
        self, task: WorkflowTask, executor: Any, workspace: PurePath
    ) -> None:
        checks = await executor.run_checks(task, workspace)
        pushed = await executor.commit_and_push(task, workspace)
        check_text = ", ".join(
            f"{command}: {'lulus' if passed else 'gagal'}"
            for command, passed, _ in checks
        )
        files = ", ".join(pushed.changed_files) or "tidak ada file"
        await self.notify(
            task.id,
            f"Branch berhasil dipush. Commit `{pushed.commit}`. Files: {files}. Checks: {check_text}.",
            (
                ("Buat PR", f"wf:pr:{task.id}"),
                ("Lanjutkan task", f"wf:continue:{task.id}"),
            ),
        )

    def _build_nodes(self) -> dict[str, Any]:
        result = {}
        for node_id, config in self.config.nodes.items():
            node_class = WindowsNode if config.kind == "windows" else LinuxNode
            result[node_id] = node_class(
                node_id,
                config.ssh_target,
                identity_file=config.identity_file,
                known_hosts_file=config.known_hosts_file,
            )
        return result

    def _project(self, project_id: str) -> ProjectConfig:
        try:
            return self.config.projects[project_id]
        except KeyError as exc:
            raise ValueError(f"project {project_id!r} is not configured") from exc

    def _executor(self, project: ProjectConfig, node: Any):
        try:
            root = self.config.worktree_roots[node.id]
        except KeyError as exc:
            raise ValueError(f"node {node.id!r} has no worktree root") from exc
        return self._executor_factory(
            store=self.store,
            node=node,
            project=project,
            worktree_root=root,
            logs_dir=self.config.logs_dir,
        )

    def _remote_path(
        self, project: ProjectConfig, node_id: str, value: str
    ) -> PurePath:
        root = self.config.worktree_roots[node_id]
        return type(root)(value)
