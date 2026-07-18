from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from workflow.config import ProjectConfig


@dataclass(frozen=True)
class RepositorySummary:
    name_with_owner: str
    description: Optional[str]
    private: bool
    updated_at: str
    default_branch: str


@dataclass(frozen=True)
class RepositoryInspection:
    stack: tuple[str, ...]
    deployment_clues: tuple[str, ...]


@dataclass(frozen=True)
class TaskProposal:
    project: str
    instruction: str
    scope: str
    acceptance_criteria: tuple[str, ...]
    steps: tuple[str, ...]
    node: str
    fallback_node: Optional[str]
    branch: str
    checks: tuple[str, ...]

    def render_text(self) -> str:
        fallback = self.fallback_node or "tidak ada"
        checks = "\n".join(f"- `{command}`" for command in self.checks) or "- Deteksi dari repo"
        criteria = "\n".join(f"- {item}" for item in self.acceptance_criteria)
        steps = "\n".join(f"{index}. {item}" for index, item in enumerate(self.steps, 1))
        return (
            f"Repo: `{self.project}`\n"
            f"Scope: {self.scope}\n\n"
            f"Kriteria selesai:\n{criteria}\n\n"
            f"Rencana:\n{steps}\n\n"
            f"Node: `{self.node}` (fallback: `{fallback}`)\n"
            f"Branch: `{self.branch}`\n"
            f"Checks:\n{checks}"
        )


CommandRunner = Callable[[list[str]], str]


def _run_command(argv: list[str]) -> str:
    return subprocess.check_output(argv, text=True, encoding="utf-8")


class GitHubClient:
    def __init__(self, run: CommandRunner = _run_command):
        self._run = run

    def list_recent_repositories(
        self, limit: int = 10, offset: int = 0
    ) -> list[RepositorySummary]:
        count = min(max(limit + offset, 1), 100)
        output = self._run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                "user/repos",
                "-f",
                "sort=updated",
                "-f",
                "direction=desc",
                "-f",
                f"per_page={count}",
            ]
        )
        repositories = [
            RepositorySummary(
                name_with_owner=str(item["full_name"]),
                description=item.get("description"),
                private=bool(item.get("private", False)),
                updated_at=str(item.get("updated_at", "")),
                default_branch=str(item.get("default_branch", "main")),
            )
            for item in json.loads(output)
        ]
        return repositories[offset : offset + limit]


class TaskPlanner:
    def propose(
        self,
        project: ProjectConfig,
        task_id: int,
        instruction: str,
    ) -> TaskProposal:
        normalized = instruction.strip()
        if not normalized:
            raise ValueError("instruction cannot be empty")
        return TaskProposal(
            project=project.name_with_owner,
            instruction=normalized,
            scope=normalized,
            acceptance_criteria=(
                "Permintaan diterapkan pada scope repository yang dipilih.",
                "Checks proyek lulus tanpa perubahan di luar scope.",
                "Perubahan tersedia di branch agent terpisah untuk ditinjau.",
            ),
            steps=(
                "Periksa struktur repo dan konteks yang relevan.",
                "Terapkan perubahan paling kecil yang memenuhi permintaan.",
                "Jalankan checks, review diff, commit, lalu push branch agent.",
            ),
            node=project.preferred_node,
            fallback_node=project.fallback_node,
            branch=f"agent/hermes/{task_id}-{_slug(normalized)}",
            checks=project.checks,
        )


def inspect_repository(path: Path | str) -> RepositoryInspection:
    root = Path(path)
    stack_markers = (
        ("composer.json", "php"),
        ("package.json", "node"),
        ("pyproject.toml", "python"),
        ("go.mod", "go"),
        ("Cargo.toml", "rust"),
        ("Dockerfile", "docker"),
    )
    deployment_markers = (
        "Dockerfile",
        "docker-compose.yml",
        "compose.yaml",
        "azure.yaml",
        "wrangler.toml",
        "vercel.json",
    )
    return RepositoryInspection(
        stack=tuple(name for marker, name in stack_markers if (root / marker).is_file()),
        deployment_clues=tuple(
            marker for marker in deployment_markers if (root / marker).is_file()
        ),
    )


def _slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    return (slug[:48].rstrip("-") or "task")
