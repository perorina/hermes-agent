from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from workflow.config import NodeConfig, ProjectConfig, WorkflowConfig
from workflow.executor import ClaudeResult, PushResult
from workflow.models import Project, TaskState
from workflow.nodes import NodeReadiness
from workflow.planner import RepositorySummary, TaskPlanner
from workflow.service import WorkflowService
from workflow.store import WorkflowStore
from workflow.telegram import TelegramWorkflow


class Button:
    def __init__(self, text, callback_data):
        self.text = text
        self.callback_data = callback_data


class Markup:
    def __init__(self, rows):
        self.inline_keyboard = rows


class Node:
    id = "pc"

    async def readiness(self, minimum_free_disk_gb):
        return NodeReadiness(True, True, False, 80.0, ())


class Executor:
    def __init__(self, store, **kwargs):
        self.store = store

    async def prepare(self, task):
        return PurePosixPath("/worktrees/1-small-doc-change")

    async def prepare_repair(self, task):
        return await self.prepare(task)

    async def run_claude(self, task, workspace):
        self.store.transition_task(task.id, TaskState.RUNNING, actor="executor")
        return ClaudeResult("session-1", "Done", None, Path("task.log"))

    async def run_checks(self, task, workspace):
        self.store.transition_task(task.id, TaskState.TESTING, actor="executor")
        return (("pytest -q", True, "passed"),)

    async def commit_and_push(self, task, workspace):
        self.store.transition_task(task.id, TaskState.PUSHED, actor="executor")
        return PushResult("abc123", ("README.md",))


def message_update(text):
    user = SimpleNamespace(id=123, first_name="Owner")
    message = SimpleNamespace(
        text=text,
        chat_id=123,
        from_user=user,
        chat=SimpleNamespace(id=123, type="private"),
    )
    return SimpleNamespace(
        effective_user=user,
        effective_message=message,
        message=message,
        update_id=1,
    )


def callback_update(data):
    user = SimpleNamespace(id=123, first_name="Owner")
    query = SimpleNamespace(
        data=data,
        from_user=user,
        message=SimpleNamespace(chat_id=123, chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query, effective_user=user)


@pytest.mark.asyncio
async def test_telegram_approval_to_push_and_pr_action(tmp_path: Path) -> None:
    config = WorkflowConfig(
        enabled=True,
        owner_telegram_id=123,
        database_path=tmp_path / "workflow.db",
        logs_dir=tmp_path / "logs",
        worktree_roots={"pc": PurePosixPath("/worktrees")},
        nodes={"pc": NodeConfig("pc", "linux", "pc.tailnet")},
        projects={
            "demo": ProjectConfig(
                id="demo",
                name_with_owner="perorina/demo",
                default_branch="main",
                preferred_node="pc",
                primary_clones={"pc": PurePosixPath("/repos/demo")},
                checks=("pytest -q",),
            )
        },
    )
    store = WorkflowStore(config.database_path)
    bot = SimpleNamespace(send_message=AsyncMock())
    github = MagicMock()
    github.list_recent_repositories.return_value = [
        RepositorySummary("perorina/demo", None, True, "2026-07-18", "main")
    ]
    controller = TelegramWorkflow(
        config,
        store,
        github,
        TaskPlanner(),
        bot_getter=lambda: bot,
        button_factory=Button,
        markup_factory=Markup,
    )
    service = WorkflowService(
        config,
        store,
        notify=controller.notify_task,
        nodes={"pc": Node()},
        executor_factory=Executor,
    )
    controller.service = SimpleNamespace(start=MagicMock())

    await controller.handle_command(message_update("/workflow"))
    await controller.handle_callback(callback_update("wf:repo:0"))
    await controller.handle_text(message_update("Small doc change"))
    await controller.handle_callback(callback_update("wf:approve:1"))
    assert store.get_task(1).state is TaskState.QUEUED

    assert await service.run_once() is True

    assert store.get_task(1).state is TaskState.PUSHED
    final_message = bot.send_message.await_args.kwargs
    labels = [button.text for row in final_message["reply_markup"].inline_keyboard for button in row]
    assert labels == ["Buat PR", "Lanjutkan task"]
