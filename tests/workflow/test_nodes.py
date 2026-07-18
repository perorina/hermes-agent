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
async def test_windows_readiness_reports_manual_work_and_disk() -> None:
    async def transport(argv, timeout):
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

    status = await node.readiness(minimum_free_disk_gb=10)

    assert status.online is True
    assert status.ready is False
    assert status.manual_busy is True
    assert status.free_disk_gb == 84.5
    assert status.manual_processes == ("claude", "Code")


@pytest.mark.asyncio
async def test_linux_readiness_rejects_low_disk() -> None:
    async def transport(argv, timeout):
        return CommandResult(
            0,
            '{"free_disk_gb":4.0,"manual_processes":[]}',
            "",
        )

    node = LinuxNode("vps", "vps.tailnet", transport=transport)
    status = await node.readiness(minimum_free_disk_gb=10)

    assert status.online is True
    assert status.ready is False
    assert status.reason == "free disk below 10 GB"
