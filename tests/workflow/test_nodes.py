from __future__ import annotations

import base64
from pathlib import Path

import pytest

from workflow.nodes import CommandResult, LinuxNode, WindowsNode


@pytest.mark.asyncio
async def test_linux_ssh_quotes_each_remote_argument() -> None:
    calls = []

    async def transport(argv, timeout):
        calls.append((argv, timeout))
        return CommandResult(0, "ok", "")

    node = LinuxNode("vps", "vps.tailnet", transport=transport)
    result = await node.run(["printf", "%s", "value; rm -rf /"])

    assert result.stdout == "ok"
    assert calls == [
        (["ssh", "vps.tailnet", "--", "printf %s 'value; rm -rf /'"], 60.0)
    ]


@pytest.mark.asyncio
async def test_ssh_uses_explicit_identity_and_strict_known_hosts() -> None:
    calls = []

    async def transport(argv, timeout):
        calls.append(argv)
        return CommandResult(0, "", "")

    node = LinuxNode(
        "vps",
        "user@vps.tailnet",
        transport=transport,
        identity_file="/opt/data/ssh/workflow_ed25519",
        known_hosts_file="/opt/data/ssh/workflow_known_hosts",
    )
    await node.run(["true"])

    assert calls[0][:11] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=/opt/data/ssh/workflow_known_hosts",
        "-i",
        "/opt/data/ssh/workflow_ed25519",
        "user@vps.tailnet",
        "--",
    ]


@pytest.mark.asyncio
async def test_windows_ssh_uses_encoded_powershell_not_raw_input() -> None:
    calls = []

    async def transport(argv, timeout):
        calls.append(argv)
        return CommandResult(0, "ok", "")

    node = WindowsNode("pc", "desktop.tailnet", transport=transport)
    dangerous = "value'; Remove-Item C:\\important"
    await node.run(["git", "status", "--", dangerous])

    ssh_argv = calls[0]
    assert dangerous not in " ".join(ssh_argv)
    assert ssh_argv[:5] == [
        "ssh",
        "desktop.tailnet",
        "--",
        "powershell.exe",
        "-NoProfile",
    ]
    encoded = ssh_argv[-1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert "ConvertFrom-Json" in script
    assert dangerous not in script


@pytest.mark.asyncio
async def test_nodes_can_run_command_in_explicit_working_directory() -> None:
    linux_calls = []
    windows_calls = []

    async def linux_transport(argv, timeout):
        linux_calls.append(argv)
        return CommandResult(0, "", "")

    async def windows_transport(argv, timeout):
        windows_calls.append(argv)
        return CommandResult(0, "", "")

    await LinuxNode("vps", "vps.tailnet", transport=linux_transport).run_in(
        "/work trees/task", ["claude", "-p", "hello; world"]
    )
    await WindowsNode("pc", "pc.tailnet", transport=windows_transport).run_in(
        "F:\\work trees\\task", ["claude", "-p", "hello; world"]
    )

    assert linux_calls[0][-1] == (
        "sh -lc 'cd -- '\"'\"'/work trees/task'\"'\"' && exec claude -p '\"'\"'hello; world'\"'\"''"
    )
    windows_script = base64.b64decode(windows_calls[0][-1]).decode("utf-16-le")
    assert "Set-Location -LiteralPath" in windows_script
    assert "hello; world" not in windows_script


@pytest.mark.asyncio
async def test_windows_readiness_reports_manual_work_and_disk() -> None:
    calls = []

    async def transport(argv, timeout):
        calls.append(argv)
        return CommandResult(
            0,
            '{"free_disk_gb":84.5,"manual_processes":["claude","Code"]}',
            "",
        )

    node = WindowsNode(
        "pc",
        "desktop.tailnet",
        transport=transport,
        ide_processes=("Code", "Cursor"),
    )

    status = await node.readiness(
        minimum_free_disk_gb=10, disk_path="F:\\masx\\agent-worktrees"
    )

    assert status.online is True
    assert status.ready is False
    assert status.manual_busy is True
    assert status.free_disk_gb == 84.5
    assert status.manual_processes == ("claude", "Code")
    script = base64.b64decode(calls[0][-1]).decode("utf-16-le")
    assert "F:\\masx\\agent-worktrees" not in script
    assert "Split-Path -Qualifier $diskPath" in script


@pytest.mark.asyncio
async def test_linux_readiness_rejects_low_disk() -> None:
    async def transport(argv, timeout):
        return CommandResult(
            0,
            '{"free_disk_gb":4.0,"manual_processes":[]}',
            "",
        )

    node = LinuxNode("vps", "vps.tailnet", transport=transport)
    status = await node.readiness(
        minimum_free_disk_gb=10, disk_path="/data/worktrees"
    )

    assert status.online is True
    assert status.ready is False
    assert status.reason == "free disk below 10 GB"
