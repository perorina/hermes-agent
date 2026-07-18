from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from workflow.config import NodeConfig, ProjectConfig, WorkflowConfig
from workflow.models import TaskState
from workflow.planner import RepositorySummary, TaskPlanner
from workflow.store import WorkflowStore
from workflow.telegram import TelegramWorkflow


OWNER_ID = 123456


class _Button:
    def __init__(self, text: str, callback_data: str):
        self.text = text
        self.callback_data = callback_data


class _Markup:
    def __init__(self, rows):
        self.inline_keyboard = rows


def _config(tmp_path: Path) -> WorkflowConfig:
    return WorkflowConfig(
        enabled=True,
        owner_telegram_id=OWNER_ID,
        database_path=tmp_path / "workflow.db",
        logs_dir=tmp_path / "workflow-logs",
        worktree_roots={"windows-pc": Path("F:/worktrees")},
        nodes={
            "windows-pc": NodeConfig("windows-pc", "windows", "desktop-8haj6sl"),
            "vps": NodeConfig("vps", "linux", "host-vps"),
        },
        projects={
            "yamansari": ProjectConfig(
                id="yamansari",
                name_with_owner="perorina/yamansari-deployment",
                default_branch="main",
                preferred_node="windows-pc",
                fallback_node="vps",
                checks=("composer test",),
            )
        },
    )


def _message_update(text: str, user_id: int = OWNER_ID):
    user = SimpleNamespace(id=user_id, first_name="Owner")
    message = SimpleNamespace(
        text=text,
        chat_id=user_id,
        from_user=user,
        chat=SimpleNamespace(id=user_id, type="private"),
    )
    return SimpleNamespace(
        effective_user=user,
        effective_message=message,
        message=message,
        update_id=1,
    )


def _callback_update(data: str, user_id: int = OWNER_ID):
    user = SimpleNamespace(id=user_id, first_name="Owner")
    query = SimpleNamespace(
        data=data,
        from_user=user,
        message=SimpleNamespace(chat_id=user_id, chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query, effective_user=user)


@pytest.fixture
def workflow(tmp_path: Path):
    bot = SimpleNamespace(send_message=AsyncMock())
    github = MagicMock()
    github.list_recent_repositories.return_value = [
        RepositorySummary(
            name_with_owner="perorina/yamansari-deployment",
            description="Private deployment",
            private=True,
            updated_at="2026-07-18T00:00:00Z",
            default_branch="main",
        )
    ]
    controller = TelegramWorkflow(
        config=_config(tmp_path),
        store=WorkflowStore(tmp_path / "workflow.db"),
        github=github,
        planner=TaskPlanner(),
        bot_getter=lambda: bot,
        button_factory=_Button,
        markup_factory=_Markup,
    )
    return controller, bot


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_start_workflow(workflow) -> None:
    controller, bot = workflow

    consumed = await controller.handle_command(_message_update("/workflow", 999))

    assert consumed is True
    assert "tidak diizinkan" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_owner_can_select_repository_and_submit_proposal(workflow) -> None:
    controller, bot = workflow
    assert await controller.handle_command(_message_update("/workflow")) is True
    first_message = bot.send_message.await_args.kwargs
    assert "1. `perorina/yamansari-deployment`" in first_message["text"]

    callback = _callback_update("wf:repo:0")
    assert await controller.handle_callback(callback) is True
    assert "tulis pekerjaan" in bot.send_message.await_args.kwargs["text"].lower()

    assert await controller.handle_text(
        _message_update("Tambah health check deployment")
    ) is True
    proposal_message = bot.send_message.await_args.kwargs
    assert "Kriteria selesai" in proposal_message["text"]
    labels = [
        button.text
        for row in proposal_message["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels == ["Setujui", "Ubah", "Batalkan"]

    task = controller.store.get_task(1)
    assert task.state is TaskState.AWAITING_APPROVAL
    assert task.branch == "agent/hermes/1-tambah-health-check-deployment"


@pytest.mark.asyncio
async def test_approval_queues_task_and_normal_text_falls_through(workflow) -> None:
    controller, _ = workflow
    await controller.handle_command(_message_update("/workflow"))
    await controller.handle_callback(_callback_update("wf:repo:0"))
    await controller.handle_text(_message_update("Perbarui README"))

    callback = _callback_update("wf:approve:1")
    assert await controller.handle_callback(callback) is True
    assert controller.store.get_task(1).state is TaskState.QUEUED
    assert await controller.handle_text(_message_update("halo hermes")) is False


@pytest.mark.asyncio
async def test_adapter_stops_normal_command_dispatch_when_workflow_consumes() -> None:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._workflow = SimpleNamespace(handle_command=AsyncMock(return_value=True))
    adapter._should_process_message = MagicMock(return_value=True)
    adapter._is_user_authorized_from_message = MagicMock(return_value=True)
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()

    await adapter._handle_command(_message_update("/workflow"), MagicMock())

    adapter._workflow.handle_command.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()
