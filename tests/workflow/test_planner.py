from __future__ import annotations

import json
from pathlib import Path

from workflow.config import ProjectConfig
from workflow.planner import GitHubClient, TaskPlanner, inspect_repository


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="yamansari",
        name_with_owner="perorina/yamansari-deployment",
        default_branch="main",
        preferred_node="windows-pc",
        fallback_node="vps",
        primary_clones={"windows-pc": Path("F:/src/yamansari")},
        checks=("composer test",),
    )


def test_lists_recent_github_repositories_with_pagination() -> None:
    commands: list[list[str]] = []
    payload = [
        {
            "full_name": f"perorina/repo-{number}",
            "description": None,
            "private": number % 2 == 0,
            "updated_at": f"2026-07-{20 - number:02d}T00:00:00Z",
            "default_branch": "main",
        }
        for number in range(1, 16)
    ]

    def run(argv: list[str]) -> str:
        commands.append(argv)
        return json.dumps(payload)

    repositories = GitHubClient(run=run).list_recent_repositories(limit=10, offset=3)

    assert len(repositories) == 10
    assert repositories[0].name_with_owner == "perorina/repo-4"
    assert repositories[-1].name_with_owner == "perorina/repo-13"
    assert commands == [
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
            "per_page=13",
        ]
    ]


def test_proposal_is_deterministic_and_contains_approval_information() -> None:
    proposal = TaskPlanner().propose(
        project=_project(),
        task_id=42,
        instruction="Tambah health check untuk deployment Yamansari",
    )

    assert proposal.branch == "agent/hermes/42-tambah-health-check-untuk-deployment-yamansari"
    assert proposal.node == "windows-pc"
    assert proposal.scope == "Tambah health check untuk deployment Yamansari"
    assert "composer test" in proposal.render_text()
    assert "vps" in proposal.render_text()
    assert proposal.render_text() == TaskPlanner().propose(
        project=_project(),
        task_id=42,
        instruction="Tambah health check untuk deployment Yamansari",
    ).render_text()


def test_inspection_detects_stack_without_writing_to_repository(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM php:8.3", encoding="utf-8")
    before = {
        path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()
    }

    result = inspect_repository(tmp_path)

    after = {
        path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()
    }
    assert result.stack == ("php", "node", "docker")
    assert result.deployment_clues == ("Dockerfile",)
    assert before == after
