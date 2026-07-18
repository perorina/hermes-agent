from pathlib import Path


def test_windows_installer_seeds_missing_sshd_config() -> None:
    script = (
        Path(__file__).parents[2] / "scripts" / "install-workflow-node.ps1"
    ).read_text(encoding="utf-8")

    seed = "Copy-Item -LiteralPath $sshdConfigDefault -Destination $sshdConfig"
    assert "$sshdConfigDefault" in script
    assert seed in script
    assert script.index(seed) < script.index("function Set-SshdDirective")


def test_windows_installer_generates_host_keys_before_validation() -> None:
    script = (
        Path(__file__).parents[2] / "scripts" / "install-workflow-node.ps1"
    ).read_text(encoding="utf-8")

    generate = "& $sshKeygenExe -A"
    validate = "& $sshdExe -t"
    assert generate in script
    assert script.index(generate) < script.index(validate)


def test_windows_installer_repairs_openssh_log_acl_before_start() -> None:
    script = (
        Path(__file__).parents[2] / "scripts" / "install-workflow-node.ps1"
    ).read_text(encoding="utf-8")

    repair = "'*S-1-5-11:(OI)(CI)(RX)'"
    assert "icacls.exe $logsDir /inheritance:r" in script
    assert repair in script
    assert script.index(repair) < script.index("Start-Service sshd")


def test_windows_installer_locks_down_generated_private_host_keys() -> None:
    script = (
        Path(__file__).parents[2] / "scripts" / "install-workflow-node.ps1"
    ).read_text(encoding="utf-8")

    lock_down = "Get-ChildItem -LiteralPath (Split-Path $sshdConfig) -Filter 'ssh_host_*_key'"
    assert lock_down in script
    assert "$hostKeyAcl.SetOwner($systemSid)" in script
    assert script.index(lock_down) < script.index("& $sshdExe -t")


def test_windows_installer_replaces_instead_of_extending_host_key_acl() -> None:
    script = (
        Path(__file__).parents[2] / "scripts" / "install-workflow-node.ps1"
    ).read_text(encoding="utf-8")

    assert "New-Object System.Security.AccessControl.FileSecurity" in script
    assert "$hostKeyAcl.SetAccessRuleProtection($true, $false)" in script
    assert "Set-Acl -LiteralPath $hostKey.FullName -AclObject $hostKeyAcl" in script
