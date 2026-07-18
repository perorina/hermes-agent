from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Optional

import yaml


@dataclass(frozen=True)
class NodeConfig:
    id: str
    kind: str
    ssh_target: str
    identity_file: Optional[str] = None
    known_hosts_file: Optional[str] = None


@dataclass(frozen=True)
class ProjectConfig:
    id: str
    name_with_owner: str
    default_branch: str
    preferred_node: str
    fallback_node: Optional[str] = None
    primary_clones: Mapping[str, PurePath] = field(default_factory=dict)
    checks: tuple[str, ...] = ()
    production: bool = False
    retry_limit: int = 3
    checkpoint_timeout_seconds: int = 60
    minimum_free_disk_gb: int = 10


@dataclass(frozen=True)
class WorkflowConfig:
    enabled: bool
    owner_telegram_id: Optional[int]
    database_path: Path
    logs_dir: Path
    worktree_roots: Mapping[str, PurePath]
    nodes: Mapping[str, NodeConfig]
    projects: Mapping[str, ProjectConfig]

    @classmethod
    def load(cls, config_yaml: Path | str, hermes_home: Path | str) -> "WorkflowConfig":
        config_path = Path(config_yaml)
        home = Path(hermes_home)
        if config_path.exists():
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        else:
            raw = {}
        workflow = _mapping(raw.get("workflow"))
        enabled = bool(workflow.get("enabled", False))
        owner = workflow.get("owner_telegram_id")
        if owner is not None:
            try:
                owner = int(owner)
            except (TypeError, ValueError) as exc:
                raise ValueError("workflow.owner_telegram_id must be numeric") from exc
        if enabled and (owner is None or owner <= 0):
            raise ValueError("workflow.owner_telegram_id is required when enabled")

        nodes = {
            node_id: NodeConfig(
                id=node_id,
                kind=str(_mapping(value).get("kind", "linux")),
                ssh_target=str(_mapping(value).get("ssh_target", node_id)),
                identity_file=(
                    str(_mapping(value).get("identity_file"))
                    if _mapping(value).get("identity_file")
                    else None
                ),
                known_hosts_file=(
                    str(_mapping(value).get("known_hosts_file"))
                    if _mapping(value).get("known_hosts_file")
                    else None
                ),
            )
            for node_id, value in _mapping(workflow.get("nodes")).items()
        }
        roots = {
            node_id: _remote_path(path, nodes.get(node_id))
            for node_id, path in _mapping(workflow.get("worktree_roots")).items()
        }
        projects: dict[str, ProjectConfig] = {}
        for project_id, value in _mapping(workflow.get("projects")).items():
            item = _mapping(value)
            preferred = str(item.get("preferred_node", ""))
            fallback_value = item.get("fallback_node")
            fallback = str(fallback_value) if fallback_value else None
            for node_id in (preferred, fallback):
                if node_id and node_id not in nodes:
                    raise ValueError(
                        f"project {project_id!r} references unknown node {node_id!r}"
                    )
            projects[project_id] = ProjectConfig(
                id=project_id,
                name_with_owner=str(item.get("repo", project_id)),
                default_branch=str(item.get("default_branch", "main")),
                preferred_node=preferred,
                fallback_node=fallback,
                primary_clones={
                    node_id: _remote_path(path, nodes.get(node_id))
                    for node_id, path in _mapping(item.get("primary_clones")).items()
                },
                checks=tuple(str(command) for command in item.get("checks", []) or []),
                production=bool(item.get("production", False)),
                retry_limit=int(item.get("retry_limit", 3)),
                checkpoint_timeout_seconds=int(
                    item.get("checkpoint_timeout_seconds", 60)
                ),
                minimum_free_disk_gb=int(item.get("minimum_free_disk_gb", 10)),
            )

        return cls(
            enabled=enabled,
            owner_telegram_id=owner,
            database_path=_under_home(home, workflow.get("database", "workflow.db")),
            logs_dir=_under_home(home, workflow.get("logs_dir", "workflow-logs")),
            worktree_roots=roots,
            nodes=nodes,
            projects=projects,
        )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _under_home(home: Path, value: Any) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else home / path


def _remote_path(value: Any, node: Optional[NodeConfig]) -> PurePath:
    if node is not None and node.kind == "windows":
        return PureWindowsPath(str(value))
    return PurePosixPath(str(value))
