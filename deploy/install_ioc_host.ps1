<#
.SYNOPSIS
  Installs a FastQ IOC host on Windows: venv from requirements-ioc-host.txt and the
  headless IOC supervisor as a background service.

.DESCRIPTION
  Steps (each one is skipped and only printed with -WhatIf):
    1. find Python (py launcher or -Python), check the version (3.10 - 3.14; 3.12 if the
       host has a Nanotec rotator - NanoLib has no newer build)
    2. create the venv (default <repo>\.venv, the one start_ioc_host.bat uses too)
       and pip install -r requirements-ioc-host.txt
    3. check the host file loads and list its enabled devices
    4. register the service:
         ScheduledTask (default, nothing to download): runs at startup, whether anyone is
           logged on or not, as SYSTEM (or -User), checked every 5 min and restarted if it died
         Nssm: a real Windows service through nssm.exe (on PATH or -NssmPath); its stop sends
           Ctrl-C, so the IOCs are stopped cleanly
    5. optional: firewall rules for Channel Access (-OpenFirewall), EPICS_CA_ADDR_LIST (-CaAddrList)
    6. optional: start it now (-Start)
  Run from an elevated PowerShell. See deploy\README.md.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\install_ioc_host.ps1 -HostFile ioc_host\supervisor_config.yaml -OpenFirewall -Start
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\install_ioc_host.ps1 -HostFile ioc_host\supervisor_config.yaml -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$HostFile,
    [string]$Python = '',                      # command or path; default: py -3.12 / -3.13 / -3 / python
    [string]$VenvDir = '',                     # default <repo>\.venv
    [string]$LogDir = '',                      # default <repo>\ioc_host\logs\<host file name>
    [ValidateSet('ScheduledTask', 'Nssm', 'None')][string]$ServiceMode = 'ScheduledTask',
    [string]$Name = 'FastQ IOC host',          # task / service name
    [string]$User = '',                        # run as this account (asks for its password); default SYSTEM
    [string]$NssmPath = '',
    [switch]$OpenFirewall,
    [string]$RemoteAddress = 'LocalSubnet',    # who may reach the IOCs, e.g. 'LocalSubnet,192.168.3.0/24'
    [string]$CaAddrList = '',                  # machine-wide EPICS_CA_ADDR_LIST, e.g. '192.168.1.255 192.168.3.12'
    [switch]$SkipPip,
    [switch]$Start
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$MinPy = [version]'3.10'; $MaxPy = [version]'3.14'

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $WhatIfPreference -and ($ServiceMode -ne 'None' -or $OpenFirewall -or $CaAddrList)) {
    throw 'Run this from an elevated PowerShell (Run as administrator), or add -WhatIf to see what it would do.'
}

# ---- host file / paths
$HostPath = $HostFile
if (-not [IO.Path]::IsPathRooted($HostPath)) { $HostPath = Join-Path $Repo $HostPath }
if (-not (Test-Path $HostPath -PathType Leaf)) { throw "Host file not found: $HostPath" }
$HostPath = (Resolve-Path $HostPath).Path
if (-not $VenvDir) { $VenvDir = Join-Path $Repo '.venv' }
if (-not $LogDir) { $LogDir = Join-Path $Repo ('ioc_host\logs\' + [IO.Path]::GetFileNameWithoutExtension($HostPath)) }
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
$Headless = Join-Path $Repo 'ioc_host\supervisor\headless.py'
$Requirements = Join-Path $Repo 'requirements-ioc-host.txt'

# ---- 1. Python
function Get-PyVersion([string[]]$cmd) {
    try {
        $exe = $cmd[0]; $pre = @($cmd | Select-Object -Skip 1)
        $out = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return [version]("$out".Trim()) }
    } catch { }
    return $null
}
$candidates = @()
if ($Python) { $candidates += , @($Python) }
else {
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3.12'); $candidates += , @('py', '-3.13'); $candidates += , @('py', '-3') }
    $candidates += , @('python')
}
$PyCmd = $null; $PyVer = $null
foreach ($c in $candidates) {
    $v = Get-PyVersion $c
    if ($v) { $PyCmd = $c; $PyVer = $v; break }
}
if (-not $PyCmd) { throw 'No Python found. Install Python 3.12 (python.org, "py launcher" ticked) or pass -Python <path to python.exe>.' }
Info "Python $PyVer ($($PyCmd -join ' '))"
if ($PyVer -lt $MinPy -or $PyVer -gt $MaxPy) { throw "Python $PyVer is not supported (need $MinPy - $MaxPy)." }
if ($PyVer -gt [version]'3.12') { Write-Warning "Python ${PyVer}: fine unless this host runs a Nanotec rotator (NanoLib needs <= 3.12)." }

# ---- 2. venv + requirements
if (Test-Path $VenvPy) {
    $v = Get-PyVersion @($VenvPy)
    if (-not $v) { throw "$VenvDir exists but its python does not run (copied from another PC?). Delete it or pass -VenvDir." }
    Info "venv $VenvDir (Python $v) exists"
} elseif ($PSCmdlet.ShouldProcess($VenvDir, "create venv with $($PyCmd -join ' ')")) {
    $exe = $PyCmd[0]; $pre = @($PyCmd | Select-Object -Skip 1)
    & $exe @pre -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "python -m venv $VenvDir failed" }
}
if (-not $SkipPip -and $PSCmdlet.ShouldProcess($VenvDir, "pip install -r $Requirements")) {
    & $VenvPy -m pip install --upgrade pip
    & $VenvPy -m pip install -r $Requirements
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed - read the error above (zhinst-core must match the LabOne version).' }
}

# ---- 3. host file check (read-only)
if (Test-Path $VenvPy) {
    $check = "import sys; sys.path.insert(0, r'$Repo\ioc_host\supervisor'); from config import load_config; " +
             "c = load_config(__import__('pathlib').Path(r'$HostPath')); " +
             "[print('   ', d, '|', x.ioc_script, '|', x.args) for d, x in sorted(c.devices.items()) if x.enabled]"
    Info "enabled devices in $HostPath"
    & $VenvPy -c $check
    if ($LASTEXITCODE -ne 0) { throw "Host file $HostPath does not load - fix it first." }
}

$PyArgs = "-u `"$Headless`" --config `"$HostPath`" --log-dir `"$LogDir`""
Info "service command: `"$VenvPy`" $PyArgs  (in $Repo)"

# ---- 4. service
$cred = $null
if ($User -and $ServiceMode -ne 'None' -and -not $WhatIfPreference) {
    $cred = Get-Credential -UserName $User -Message "Password of $User (the service runs as this account)"
}

if ($ServiceMode -eq 'ScheduledTask') {
    $action = New-ScheduledTaskAction -Execute $VenvPy -Argument $PyArgs -WorkingDirectory $Repo
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT30S'  # network and VISA services first
    # Task Scheduler's "restart on failure" does not fire when the program exits with an error code,
    # so the task is re-triggered every 5 min instead; IgnoreNew makes that a no-op while it runs
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd
    if ($PSCmdlet.ShouldProcess($Name, 'register scheduled task (at startup, run whether logged on or not)')) {
        if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        }
        if ($cred) {
            Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest `
                -User $cred.UserName -Password $cred.GetNetworkCredential().Password `
                -Description "FastQ headless IOC supervisor ($HostPath)" | Out-Null
        } else {
            $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
            Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings `
                -Principal $principal -Description "FastQ headless IOC supervisor ($HostPath)" | Out-Null
        }
    }
} elseif ($ServiceMode -eq 'Nssm') {
    $nssm = $NssmPath
    if (-not $nssm) { $c = Get-Command nssm.exe -ErrorAction SilentlyContinue; if ($c) { $nssm = $c.Source } }
    if (-not $nssm -and -not $WhatIfPreference) { throw 'nssm.exe not found: put it on PATH or pass -NssmPath (https://nssm.cc).' }
    if (-not $nssm) { $nssm = 'nssm.exe' }
    if ($PSCmdlet.ShouldProcess($Name, "install Windows service with $nssm")) {
        & $nssm stop $Name 2>$null | Out-Null
        & $nssm remove $Name confirm 2>$null | Out-Null
        & $nssm install $Name $VenvPy
        if ($LASTEXITCODE -ne 0) { throw 'nssm install failed' }
        # written to NSSM's registry key directly: Windows PowerShell mangles quotes inside native arguments
        Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$Name\Parameters" -Name AppParameters -Value $PyArgs
        New-Item -ItemType Directory -Force $LogDir | Out-Null
        & $nssm set $Name AppDirectory $Repo | Out-Null
        & $nssm set $Name Description "FastQ headless IOC supervisor ($HostPath)" | Out-Null
        & $nssm set $Name Start SERVICE_AUTO_START | Out-Null
        & $nssm set $Name AppStdout (Join-Path $LogDir 'service.out') | Out-Null
        & $nssm set $Name AppStderr (Join-Path $LogDir 'service.out') | Out-Null
        & $nssm set $Name AppRotateFiles 1 | Out-Null
        & $nssm set $Name AppRotateBytes 5000000 | Out-Null
        # Ctrl-C first and give the supervisor time to stop every IOC (5 s each at most)
        & $nssm set $Name AppStopMethodConsole 60000 | Out-Null
        & $nssm set $Name AppExit Default Restart | Out-Null
        & $nssm set $Name AppRestartDelay 10000 | Out-Null
        if ($cred) { & $nssm set $Name ObjectName $cred.UserName $cred.GetNetworkCredential().Password | Out-Null }
    }
}

# ---- 5. network
if ($OpenFirewall) {
    # caproto: UDP 5064 name search (shared by all IOCs), 5065 repeater/beacons; TCP 5064 for the
    # first IOC, a random port 49152-65535 for every other one (see ioc_host/common/ca_windows.py).
    # The TCP rule is limited to the interpreter that actually listens: a venv's python.exe is only
    # a launcher, the real one is "home" in pyvenv.cfg.
    $basePy = $null
    $cfg = Join-Path $VenvDir 'pyvenv.cfg'
    if (Test-Path $cfg) {
        $homeLine = Get-Content $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
        if ($homeLine) { $basePy = Join-Path ($homeLine -replace '^\s*home\s*=\s*', '').Trim() 'python.exe' }
    }
    $rules = @(
        @{ DisplayName = 'FastQ Channel Access UDP 5064-5065'; Protocol = 'UDP'; LocalPort = @('5064', '5065') },
        @{ DisplayName = 'FastQ Channel Access TCP (IOCs)'; Protocol = 'TCP'; LocalPort = @('5064', '49152-65535') }
    )
    foreach ($r in $rules) {
        $p = @{ Group = 'FastQ'; Direction = 'Inbound'; Action = 'Allow'; Profile = @('Domain', 'Private')
                RemoteAddress = @($RemoteAddress -split '\s*,\s*') } + $r
        if ($r.Protocol -eq 'TCP' -and $basePy) { $p.Program = $basePy }
        if ($PSCmdlet.ShouldProcess($r.DisplayName, "add inbound firewall rule from $RemoteAddress")) {
            Get-NetFirewallRule -DisplayName $r.DisplayName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
            New-NetFirewallRule @p | Out-Null
        }
    }
    Write-Warning 'Public network profile is not opened: set the lab network to Private or Domain.'
}
if ($CaAddrList -and $PSCmdlet.ShouldProcess('EPICS_CA_ADDR_LIST (machine)', "set to '$CaAddrList'")) {
    [Environment]::SetEnvironmentVariable('EPICS_CA_ADDR_LIST', $CaAddrList, 'Machine')
}

# ---- 6. start
if ($Start) {
    if ($ServiceMode -eq 'ScheduledTask' -and $PSCmdlet.ShouldProcess($Name, 'start scheduled task')) { Start-ScheduledTask -TaskName $Name }
    if ($ServiceMode -eq 'Nssm' -and $PSCmdlet.ShouldProcess($Name, 'start service')) { & $nssm start $Name }
}

Info "done. Logs: $LogDir (supervisor.log + one file per device)"
if ($ServiceMode -eq 'ScheduledTask') { Info "status: Get-ScheduledTask '$Name' | Get-ScheduledTaskInfo ; stop: Stop-ScheduledTask '$Name'" }
if ($ServiceMode -eq 'Nssm') { Info "status: nssm status '$Name' ; stop: nssm stop '$Name'" }
Write-Host 'Do not also run the supervisor GUI on this host file while the service runs (two supervisors = every IOC twice).'
