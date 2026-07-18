from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from workflow.config import ProjectConfig
from workflow.models import TaskState, WorkflowTask
from workflow.prompts import redact_secrets
from workflow.store import WorkflowStore


class ApprovalRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str


CommandRunner = Callable[[list[str]], str]


def _run(argv: list[str]) -> str:
    return subprocess.check_output(argv, text=True, encoding="utf-8")


class WorkflowGitHub:
    def __init__(self, store: WorkflowStore, run: CommandRunner = _run):
        self.store = store
        self._run = run

    def create_pr(
        self,
        task: WorkflowTask,
        project: ProjectConfig,
        approved_by_owner: bool,
    ) -> PullRequest:
        if not approved_by_owner:
            raise ApprovalRequired("pull request creation requires owner approval")
        current = self.store.get_task(task.id)
        if current.state is not TaskState.PUSHED:
            raise ValueError(f"task is {current.state.value}, not pushed")
        self.store.transition_task(
            task.id, TaskState.AWAITING_PR, actor="telegram-owner"
        )
        title = redact_secrets(task.instruction).strip()[:120] or f"Hermes task {task.id}"
        output = self._run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                project.name_with_owner,
                "--base",
                project.default_branch,
                "--head",
                task.branch,
                "--title",
                title,
                "--body",
                f"Automated branch for approved Hermes task #{task.id}. Merge remains manual.",
            ]
        ).strip()
        match = re.search(r"/pull/(\d+)(?:\D|$)", output)
        if not match:
            raise RuntimeError(f"gh pr create did not return a pull request URL: {output}")
        pull_request = PullRequest(number=int(match.group(1)), url=output)
        self.store.record_pull_request(
            task.id,
            project.name_with_owner,
            pull_request.number,
            pull_request.url,
        )
        self.store.transition_task(
            task.id, TaskState.MONITORING_PR, actor="github"
        )
        return pull_request
