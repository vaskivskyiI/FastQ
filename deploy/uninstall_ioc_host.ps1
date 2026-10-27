<#
.SYNOPSIS
  Removes what deploy\install_ioc_host.ps1 set up: stops the headless IOC supervisor (its IOCs
  stop with it), removes the scheduled task / NSSM service and the "FastQ" firewall rules.
  The venv, the logs and EPICS_CA_ADDR_LIST stay unless asked (-RemoveVenv, -RemoveLogs, -RemoveCaAddrList).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\uninstall_ioc_host.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Name = 'FastQ IOC host',
    [string]$NssmPath = '',
    [string]$VenvDir = '',
    [string]$LogDir = '',                      # default <repo>\ioc_host\logs
    [switch]$KeepFirewall,
    [switch]$RemoveVenv,
    [switch]$RemoveLogs,
    [switch]$RemoveCaAddrList
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $VenvDir) { $VenvDir = Join-Path $Repo '.venv' }
if (-not $LogDir) { $LogDir = Join-Path $Repo 'ioc_host\logs' }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $WhatIfPreference) { throw 'Run this from an elevated PowerShell, or add -WhatIf.' }

$found = $false
if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    $found = $true
    # Stop = TerminateProcess; the supervisor's job object takes its IOCs along (headless.py)
    if ($PSCmdlet.ShouldProcess($Name, 'stop and unregister scheduled task')) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }
}
if (Get-Service -Name $Name -ErrorAction SilentlyContinue) {
    $found = $true
    $nssm = $NssmPath
    if (-not $nssm) { $c = Get-Command nssm.exe -ErrorAction SilentlyContinue; if ($c) { $nssm = $c.Source } }
    if ($PSCmdlet.ShouldProcess($Name, 'stop and remove Windows service')) {
        if ($nssm) { & $nssm stop $Name; & $nssm remove $Name confirm }
        else { Stop-Service -Name $Name; & sc.exe delete $Name | Out-Null }
    }
}
if (-not $found) { Write-Host "No scheduled task or service named '$Name'." }

if (-not $KeepFirewall) {
    $rules = @(Get-NetFirewallRule -Group 'FastQ' -ErrorAction SilentlyContinue)
    foreach ($r in $rules) {
        if ($PSCmdlet.ShouldProcess($r.DisplayName, 'remove firewall rule')) { $r | Remove-NetFirewallRule }
    }
}
if ($RemoveCaAddrList -and $PSCmdlet.ShouldProcess('EPICS_CA_ADDR_LIST (machine)', 'remove')) {
    [Environment]::SetEnvironmentVariable('EPICS_CA_ADDR_LIST', $null, 'Machine')
}
if ($RemoveVenv -and (Test-Path $VenvDir) -and $PSCmdlet.ShouldProcess($VenvDir, 'delete venv')) {
    Remove-Item -Recurse -Force $VenvDir
}
if ($RemoveLogs -and (Test-Path $LogDir) -and $PSCmdlet.ShouldProcess($LogDir, 'delete logs')) {
    Remove-Item -Recurse -Force $LogDir
}
Write-Host 'done.'
