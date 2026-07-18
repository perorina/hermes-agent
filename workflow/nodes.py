from __future__ import annotations

import asyncio
import base64
import json
import shlex
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Sequence


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class NodeReadiness:
    online: bool
    ready: bool
    manual_busy: bool
    free_disk_gb: Optional[float]
    manual_processes: tuple[str, ...]
    reason: Optional[str] = None


Transport = Callable[[list[str], float], Awaitable[CommandResult]]


async def _subprocess_transport(argv: list[str], timeout: float) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise
    return CommandResult(
        returncode=int(process.returncode or 0),
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


class SSHNode:
    def __init__(
        self,
        node_id: str,
        ssh_target: str,
        transport: Transport = _subprocess_transport,
        ide_processes: Sequence[str] = ("Code", "Cursor"),
        identity_file: Optional[str] = None,
        known_hosts_file: Optional[str] = None,
    ):
        self.id = node_id
        self.ssh_target = ssh_target
        self._transport = transport
        self.ide_processes = tuple(ide_processes)
        self.identity_file = identity_file
        self.known_hosts_file = known_hosts_file

    async def run(
        self, argv: Sequence[str], timeout: float = 60.0
    ) -> CommandResult:
        raise NotImplementedError

    def _ssh_argv(self) -> list[str]:
        argv = ["ssh"]
        if self.identity_file or self.known_hosts_file:
            argv.extend(["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"])
            if self.known_hosts_file:
                argv.extend(
                    ["-o", f"UserKnownHostsFile={self.known_hosts_file}"]
                )
            if self.identity_file:
                argv.extend(["-i", self.identity_file])
        argv.extend([self.ssh_target, "--"])
        return argv

    @staticmethod
    def _readiness_from_result(
        result: CommandResult, minimum_free_disk_gb: int
    ) -> NodeReadiness:
        if result.returncode != 0:
            return NodeReadiness(
                online=False,
                ready=False,
                manual_busy=False,
                free_disk_gb=None,
                manual_processes=(),
                reason=result.stderr.strip() or "SSH readiness command failed",
            )
        try:
            payload = json.loads(result.stdout.strip())
            free_disk = float(payload["free_disk_gb"])
            processes = tuple(str(item) for item in payload.get("manual_processes", []))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return NodeReadiness(
                online=True,
                ready=False,
                manual_busy=False,
                free_disk_gb=None,
                manual_processes=(),
                reason="invalid readiness response",
            )
        if processes:
            return NodeReadiness(
                online=True,
                ready=False,
                manual_busy=True,
                free_disk_gb=free_disk,
                manual_processes=processes,
                reason="manual Claude or IDE process is active",
            )
        if free_disk < minimum_free_disk_gb:
            return NodeReadiness(
                online=True,
                ready=False,
                manual_busy=False,
                free_disk_gb=free_disk,
                manual_processes=(),
                reason=f"free disk below {minimum_free_disk_gb} GB",
            )
        return NodeReadiness(
            online=True,
            ready=True,
            manual_busy=False,
            free_disk_gb=free_disk,
            manual_processes=(),
        )


class LinuxNode(SSHNode):
    kind = "linux"

    async def run(
        self, argv: Sequence[str], timeout: float = 60.0
    ) -> CommandResult:
        if not argv:
            raise ValueError("remote argv cannot be empty")
        remote_command = shlex.join([str(item) for item in argv])
        return await self._transport([*self._ssh_argv(), remote_command], timeout)

    async def run_in(
        self, cwd: str, argv: Sequence[str], timeout: float = 60.0
    ) -> CommandResult:
        script = f"cd -- {shlex.quote(str(cwd))} && exec {shlex.join([str(item) for item in argv])}"
        return await self.run(["sh", "-lc", script], timeout=timeout)

    async def readiness(
        self, minimum_free_disk_gb: int, disk_path: Optional[str] = None
    ) -> NodeReadiness:
        process_names = ("claude", *self.ide_processes)
        encoded_names = base64.b64encode(
            json.dumps(process_names).encode("utf-8")
        ).decode("ascii")
        encoded_path = base64.b64encode(
            json.dumps(disk_path or "~").encode("utf-8")
        ).decode("ascii")
        script = (
            "import base64,json,os,shutil,subprocess;"
            f"names=json.loads(base64.b64decode('{encoded_names}'));"
            f"target=os.path.expanduser(json.loads(base64.b64decode('{encoded_path}')));"
            "running=[];"
            "[(running.append(name) if subprocess.run(['pgrep','-x',name],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0 else None) "
            "for name in names];"
            "free=shutil.disk_usage(target).free/(1024**3);"
            "print(json.dumps({'free_disk_gb':round(free,2),'manual_processes':running}))"
        )
        result = await self.run(["python3", "-c", script], timeout=15.0)
        return self._readiness_from_result(result, minimum_free_disk_gb)


class WindowsNode(SSHNode):
    kind = "windows"

    async def run(
        self, argv: Sequence[str], timeout: float = 60.0
    ) -> CommandResult:
        if not argv:
            raise ValueError("remote argv cannot be empty")
        payload = base64.b64encode(
            json.dumps([str(item) for item in argv]).encode("utf-8")
        ).decode("ascii")
        script = (
            f"$json=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'));"
            "$argv=ConvertFrom-Json $json;"
            "if($argv.Count -eq 1){& $argv[0]}else{& $argv[0] @($argv[1..($argv.Count-1)])};"
            "exit $LASTEXITCODE"
        )
        return await self._run_powershell(script, timeout)

    async def run_in(
        self, cwd: str, argv: Sequence[str], timeout: float = 60.0
    ) -> CommandResult:
        if not argv:
            raise ValueError("remote argv cannot be empty")
        cwd_payload = base64.b64encode(str(cwd).encode("utf-8")).decode("ascii")
        argv_payload = base64.b64encode(
            json.dumps([str(item) for item in argv]).encode("utf-8")
        ).decode("ascii")
        script = (
            f"$cwd=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{cwd_payload}'));"
            f"$json=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{argv_payload}'));"
            "$argv=ConvertFrom-Json $json;Set-Location -LiteralPath $cwd;"
            "if($argv.Count -eq 1){& $argv[0]}else{& $argv[0] @($argv[1..($argv.Count-1)])};"
            "exit $LASTEXITCODE"
        )
        return await self._run_powershell(script, timeout)

    async def _run_powershell(
        self, script: str, timeout: float
    ) -> CommandResult:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return await self._transport(
            [
                *self._ssh_argv(),
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            timeout,
        )

    async def readiness(
        self, minimum_free_disk_gb: int, disk_path: Optional[str] = None
    ) -> NodeReadiness:
        names = ("claude", *self.ide_processes)
        quoted_names = ",".join(f"'{name.replace(chr(39), '')}'" for name in names)
        if disk_path:
            encoded_path = base64.b64encode(disk_path.encode("utf-8")).decode("ascii")
            disk_script = (
                "$diskPath=[Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{encoded_path}'));"
            )
        else:
            disk_script = "$diskPath=$env:SystemDrive;"
        script = (
            disk_script + f"$names=@({quoted_names});"
            "$running=@(Get-Process -ErrorAction SilentlyContinue | "
            "Where-Object {$names -contains $_.ProcessName} | "
            "Select-Object -ExpandProperty ProcessName -Unique);"
            "$qualifier=Split-Path -Qualifier $diskPath;"
            "if(-not $qualifier){$qualifier=$env:SystemDrive};"
            "$drive=Get-PSDrive -Name $qualifier.TrimEnd(':').TrimEnd('\\');"
            "@{free_disk_gb=[math]::Round($drive.Free/1GB,2);manual_processes=$running}"
            "|ConvertTo-Json -Compress"
        )
        result = await self._run_powershell(script, timeout=15.0)
        return self._readiness_from_result(result, minimum_free_disk_gb)
