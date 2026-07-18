from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from workflow.config import ProjectConfig, WorkflowConfig
from workflow.models import Project, TaskState
from workflow.planner import GitHubClient, RepositorySummary, TaskPlanner
from workflow.store import WorkflowStore


@dataclass
class _Conversation:
    stage: str
    repositories: tuple[RepositorySummary, ...] = ()
    project: Optional[ProjectConfig] = None
    task_id: Optional[int] = None
    offset: int = 0


class TelegramWorkflow:
    def __init__(
        self,
        config: WorkflowConfig,
        store: WorkflowStore,
        github: GitHubClient,
        planner: TaskPlanner,
        bot_getter: Callable[[], Any],
        button_factory: Callable[..., Any] = InlineKeyboardButton,
        markup_factory: Callable[..., Any] = InlineKeyboardMarkup,
        service: Any = None,
    ):
        self.config = config
        self.store = store
        self.github = github
        self.planner = planner
        self._bot_getter = bot_getter
        self._button = button_factory
        self._markup = markup_factory
        self.service = service
        self._conversations: dict[int, _Conversation] = {}
        for project in config.projects.values():
            self._save_project(project)

    @classmethod
    def from_hermes_config(cls, adapter: Any) -> Optional["TelegramWorkflow"]:
        from hermes_constants import get_config_path, get_hermes_home

        home = get_hermes_home()
        config = WorkflowConfig.load(get_config_path(), home)
        if not config.enabled:
            return None
        controller = cls(
            config=config,
            store=WorkflowStore(config.database_path),
            github=GitHubClient(),
            planner=TaskPlanner(),
            bot_getter=lambda: adapter._bot,
        )
        from workflow.service import WorkflowService

        controller.service = WorkflowService(
            config, controller.store, notify=controller.notify_task
        )
        return controller

    async def handle_command(self, update: Any) -> bool:
        message = _message(update)
        text = str(getattr(message, "text", "") or "")
        if not text.split(maxsplit=1)[0].split("@", 1)[0].lower() == "/workflow":
            return False
        user_id = _user_id(update)
        if not self._authorized(user_id):
            await self._send(_chat_id(update), "Workflow ini tidak diizinkan untuk akunmu.")
            return True
        await self._show_repositories(user_id, _chat_id(update), offset=0)
        return True

    async def handle_text(self, update: Any) -> bool:
        user_id = _user_id(update)
        conversation = self._conversations.get(user_id)
        if conversation is None or conversation.stage not in {
            "instruction",
            "editing",
            "answer",
        }:
            return False
        if not self._authorized(user_id):
            return True
        message = _message(update)
        instruction = str(getattr(message, "text", "") or "").strip()
        if not instruction:
            return True

        if conversation.stage == "answer" and conversation.task_id is not None:
            if self.service is None:
                await self._send(_chat_id(update), "Workflow service belum aktif.")
                return True
            task_id = conversation.task_id
            conversation.stage = "idle"
            await self.service.answer(task_id, instruction)
            return True

        if conversation.project is None:
            return True

        if conversation.stage == "editing" and conversation.task_id is not None:
            task_id = conversation.task_id
            proposal = self.planner.propose(conversation.project, task_id, instruction)
            task = self.store.revise_proposal(
                task_id,
                instruction,
                proposal.render_text(),
                proposal.branch,
                actor=f"telegram:{user_id}",
            )
        else:
            draft = self.store.create_draft(conversation.project.id, instruction)
            proposal = self.planner.propose(conversation.project, draft.id, instruction)
            task = self.store.submit_proposal(
                draft.id,
                instruction,
                proposal.render_text(),
                proposal.branch,
                actor=f"telegram:{user_id}",
            )

        conversation.stage = "approval"
        conversation.task_id = task.id
        await self._send(
            _chat_id(update),
            task.proposal,
            reply_markup=self._markup(
                [
                    [
                        self._button(
                            "Setujui", callback_data=f"wf:approve:{task.id}"
                        ),
                        self._button("Ubah", callback_data=f"wf:edit:{task.id}"),
                        self._button(
                            "Batalkan", callback_data=f"wf:cancel:{task.id}"
                        ),
                    ]
                ]
            ),
        )
        return True

    async def handle_callback(self, update: Any) -> bool:
        query = getattr(update, "callback_query", None)
        data = str(getattr(query, "data", "") or "")
        if not data.startswith("wf:"):
            return False
        user_id = _user_id(update)
        if not self._authorized(user_id):
            await query.answer(text="Tidak diizinkan.", show_alert=True)
            return True
        await query.answer()
        chat_id = int(getattr(getattr(query, "message", None), "chat_id", user_id))
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "more":
            offset = int(parts[2]) if len(parts) > 2 else 0
            await self._show_repositories(user_id, chat_id, offset)
            return True
        if action == "repo":
            conversation = self._conversations.get(user_id)
            index = int(parts[2]) if len(parts) > 2 else -1
            if conversation is None or not 0 <= index < len(conversation.repositories):
                await self._send(chat_id, "Pilihan repository sudah kedaluwarsa. Jalankan /workflow lagi.")
                return True
            repository = conversation.repositories[index]
            project = self._project_for(repository)
            conversation.project = project
            conversation.stage = "instruction"
            self._save_project(project)
            await self._send(
                chat_id,
                f"Repo dipilih: {repository.name_with_owner}\n\nTulis pekerjaan yang ingin Hermes delegasikan.",
            )
            return True

        task_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        conversation = self._conversations.get(user_id)
        if action == "approve" and task_id:
            task = self.store.approve_task(task_id, actor=f"telegram:{user_id}")
            if conversation:
                conversation.stage = "idle"
            await self._send(
                chat_id,
                f"Task #{task.id} masuk antrean posisi {task.queue_position}.",
            )
            if self.service is not None:
                self.service.start()
        elif action == "pr" and task_id:
            if self.service is None:
                await self._send(chat_id, "Workflow service belum aktif.")
            else:
                await self.service.create_pr(task_id)
        elif action == "answer" and task_id:
            if conversation is None:
                conversation = _Conversation(stage="answer")
                self._conversations[user_id] = conversation
            conversation.stage = "answer"
            conversation.task_id = task_id
            await self._send(chat_id, "Kirim jawaban untuk Claude Code.")
        elif action == "edit" and task_id:
            task = self.store.get_task(task_id)
            if task.state is not TaskState.AWAITING_APPROVAL:
                await self._send(chat_id, "Task ini tidak lagi bisa diubah.")
                return True
            if conversation is None:
                conversation = _Conversation(stage="editing")
                self._conversations[user_id] = conversation
            conversation.stage = "editing"
            conversation.task_id = task_id
            conversation.project = self._project_by_id(task.project_id)
            await self._send(chat_id, "Tulis ulang pekerjaan atau koreksi scope-nya.")
        elif action == "cancel" and task_id:
            self.store.transition_task(
                task_id, TaskState.CANCELLED, actor=f"telegram:{user_id}"
            )
            if conversation:
                conversation.stage = "idle"
            await self._send(chat_id, f"Task #{task_id} dibatalkan.")
        else:
            await self._send(chat_id, "Aksi workflow tidak dikenali.")
        return True

    async def notify_task(
        self,
        task_id: int,
        text: str,
        actions: tuple[tuple[str, str], ...] | list[tuple[str, str]],
    ) -> None:
        keyboard = [
            [self._button(label, callback_data=callback_data)]
            for label, callback_data in actions
        ]
        await self._send(
            int(self.config.owner_telegram_id or 0),
            f"Task #{task_id}\n\n{text}",
            reply_markup=self._markup(keyboard) if keyboard else None,
        )

    async def _show_repositories(self, user_id: int, chat_id: int, offset: int) -> None:
        repositories = tuple(
            self.github.list_recent_repositories(limit=10, offset=offset)
        )
        self._conversations[user_id] = _Conversation(
            stage="repository", repositories=repositories, offset=offset
        )
        if not repositories:
            await self._send(chat_id, "Tidak ada repository yang ditemukan.")
            return
        lines = [
            f"{index + 1}. `{repo.name_with_owner}`"
            for index, repo in enumerate(repositories)
        ]
        keyboard = [
            [self._button(str(index + 1), callback_data=f"wf:repo:{index}")]
            for index in range(len(repositories))
        ]
        if len(repositories) == 10:
            keyboard.append(
                [
                    self._button(
                        "Tampilkan lainnya", callback_data=f"wf:more:{offset + 10}"
                    )
                ]
            )
        await self._send(
            chat_id,
            "Pilih repository:\n\n" + "\n".join(lines),
            reply_markup=self._markup(keyboard),
        )

    def _project_for(self, repository: RepositorySummary) -> ProjectConfig:
        for project in self.config.projects.values():
            if project.name_with_owner == repository.name_with_owner:
                return project
        if not self.config.nodes:
            raise ValueError("workflow has no configured node")
        project_id = re.sub(r"[^a-z0-9]+", "-", repository.name_with_owner.lower()).strip("-")
        return ProjectConfig(
            id=project_id,
            name_with_owner=repository.name_with_owner,
            default_branch=repository.default_branch,
            preferred_node=next(iter(self.config.nodes)),
        )

    def _project_by_id(self, project_id: str) -> ProjectConfig:
        for project in self.config.projects.values():
            if project.id == project_id:
                return project
        raise KeyError(project_id)

    def _save_project(self, project: ProjectConfig) -> None:
        self.store.save_project(
            Project(
                id=project.id,
                name_with_owner=project.name_with_owner,
                default_branch=project.default_branch,
                preferred_node=project.preferred_node,
                fallback_node=project.fallback_node,
                production=project.production,
                retry_limit=project.retry_limit,
                minimum_free_disk_gb=project.minimum_free_disk_gb,
            )
        )

    def _authorized(self, user_id: int) -> bool:
        return user_id == self.config.owner_telegram_id

    async def _send(self, chat_id: int, text: str, **kwargs: Any) -> None:
        bot = self._bot_getter()
        if bot is None:
            raise RuntimeError("Telegram bot is not connected")
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)


def _message(update: Any) -> Any:
    return getattr(update, "effective_message", None) or getattr(update, "message", None)


def _user_id(update: Any) -> int:
    user = getattr(update, "effective_user", None)
    if user is None:
        user = getattr(getattr(update, "callback_query", None), "from_user", None)
    return int(getattr(user, "id", 0) or 0)


def _chat_id(update: Any) -> int:
    message = _message(update)
    return int(getattr(message, "chat_id", _user_id(update)))
