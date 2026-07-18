from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from workflow.config import WorkflowConfig


def test_workflow_is_disabled_by_default(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n  default: test\n", encoding="utf-8")

    config = WorkflowConfig.load(config_path, tmp_path)

    assert config.enabled is False
    assert config.database_path == tmp_path / "workflow.db"
    assert config.owner_telegram_id is None
    assert config.projects == {}


def test_enabled_workflow_requires_numeric_owner(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("workflow:\n  enabled: true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="owner_telegram_id"):
        WorkflowConfig.load(config_path, tmp_path)


def test_loads_nodes_and_project_with_conservative_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
workflow:
  enabled: true
  owner_telegram_id: 123456
  logs_dir: workflow-logs
  worktree_roots:
    windows-pc: F:\\worktrees
    vps: /opt/data/worktrees
  nodes:
    windows-pc:
      kind: windows
      ssh_target: karen@desktop-8haj6sl
      identity_file: /opt/data/ssh/workflow_pc_ed25519
      known_hosts_file: /opt/data/ssh/workflow_known_hosts
    vps:
      kind: linux
      ssh_target: host-vps
  projects:
    yamansari:
      repo: perorina/yamansari-deployment
      default_branch: main
      preferred_node: windows-pc
      fallback_node: vps
      primary_clones:
        windows-pc: F:\\src\\yamansari
        vps: /opt/data/repos/yamansari
      checks:
        - composer test
""".lstrip(),
        encoding="utf-8",
    )

    config = WorkflowConfig.load(config_path, tmp_path)
    project = config.projects["yamansari"]

    assert config.enabled is True
    assert config.owner_telegram_id == 123456
    assert config.logs_dir == tmp_path / "workflow-logs"
    assert config.nodes["windows-pc"].kind == "windows"
    assert config.nodes["windows-pc"].identity_file == "/opt/data/ssh/workflow_pc_ed25519"
    assert config.worktree_roots["windows-pc"] == PureWindowsPath("F:/worktrees")
    assert config.worktree_roots["vps"] == PurePosixPath("/opt/data/worktrees")
    assert project.primary_clones["windows-pc"] == PureWindowsPath(
        "F:/src/yamansari"
    )
    assert project.primary_clones["vps"] == PurePosixPath(
        "/opt/data/repos/yamansari"
    )
    assert project.name_with_owner == "perorina/yamansari-deployment"
    assert project.production is False
    assert project.retry_limit == 3
    assert project.minimum_free_disk_gb == 10
    assert project.checks == ("composer test",)


def test_rejects_project_that_references_unknown_node(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
workflow:
  enabled: true
  owner_telegram_id: 123
  projects:
    demo:
      repo: perorina/demo
      preferred_node: missing-node
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing-node"):
        WorkflowConfig.load(config_path, tmp_path)
