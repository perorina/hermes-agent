#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^ssh-ed25519\s+')]
    [string]$PublicKey
)

$ErrorActionPreference = 'Stop'

$capability = Get-WindowsCapability -Online |
    Where-Object Name -Like 'OpenSSH.Server*' |
    Select-Object -First 1
if (-not $capability -or $capability.State -ne 'Installed') {
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
}

$sshdConfig = Join-Path $env:ProgramData 'ssh\sshd_config'
$sshdConfigDefault = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd_config_default'
$authorizedKeys = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
New-Item -ItemType Directory -Force (Split-Path $authorizedKeys) | Out-Null
if (-not (Test-Path $sshdConfig)) {
    Copy-Item -LiteralPath $sshdConfigDefault -Destination $sshdConfig
}
if (-not (Test-Path $authorizedKeys)) {
    New-Item -ItemType File $authorizedKeys | Out-Null
}

$existingKeys = @(Get-Content $authorizedKeys -ErrorAction SilentlyContinue)
if ($existingKeys -notcontains $PublicKey.Trim()) {
    Add-Content -LiteralPath $authorizedKeys -Value $PublicKey.Trim() -Encoding ascii
}

& icacls.exe $authorizedKeys /inheritance:r | Out-Null
& icacls.exe $authorizedKeys /grant '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null

function Set-SshdDirective {
    param([string]$Name, [string]$Value)
    $content = Get-Content -LiteralPath $sshdConfig
    $pattern = "^\s*#?\s*$([regex]::Escape($Name))\s+.*$"
    if ($content -match $pattern) {
        $content = $content -replace $pattern, "$Name $Value"
    } else {
        $content += "$Name $Value"
    }
    Set-Content -LiteralPath $sshdConfig -Value $content -Encoding ascii
}

Set-SshdDirective -Name 'PubkeyAuthentication' -Value 'yes'
Set-SshdDirective -Name 'PasswordAuthentication' -Value 'no'

$sshdExe = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
$sshKeygenExe = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh-keygen.exe'
& $sshKeygenExe -A
if ($LASTEXITCODE -ne 0) {
    throw 'OpenSSH host key generation failed'
}
$privateHostKeys = Get-ChildItem -LiteralPath (Split-Path $sshdConfig) -Filter 'ssh_host_*_key'
$systemSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$administratorsSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
foreach ($hostKey in $privateHostKeys) {
    $hostKeyAcl = New-Object System.Security.AccessControl.FileSecurity
    $hostKeyAcl.SetOwner($systemSid)
    $hostKeyAcl.SetAccessRuleProtection($true, $false)
    foreach ($identity in @($systemSid, $administratorsSid)) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $identity,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $hostKeyAcl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $hostKey.FullName -AclObject $hostKeyAcl
}
$logsDir = Join-Path $env:ProgramData 'ssh\logs'
New-Item -ItemType Directory -Force $logsDir | Out-Null
& icacls.exe $logsDir /inheritance:r | Out-Null
& icacls.exe $logsDir /grant:r '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' '*S-1-5-11:(OI)(CI)(RX)' | Out-Null
& $sshdExe -t
if ($LASTEXITCODE -ne 0) {
    throw 'sshd_config validation failed'
}

Set-Service sshd -StartupType Automatic
Start-Service sshd
Restart-Service sshd

$ruleName = 'Hermes Workflow SSH over Tailscale'
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule
New-NetFirewallRule `
    -DisplayName $ruleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort 22 `
    -RemoteAddress '100.64.0.0/10' `
    -Profile Any | Out-Null

Write-Host 'OpenSSH Server ready for key-only access from the Tailscale address range.'
Write-Host "Verify from another tailnet node: ssh $env:USERNAME@$env:COMPUTERNAME"
