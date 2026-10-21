<#
.SYNOPSIS
    Start Garmin Weight Coach automatically when you log on to Windows.

.DESCRIPTION
    Registers a Task Scheduler task for the current user that runs
    `uv run gwc serve` from the project folder at log on, with no console
    window. The task runs as you, so it can use your Claude login and the
    files in data/. If the app exits with an error, Task Scheduler restarts
    it up to 3 times, one minute apart. Logs go to data/logs/gwc.log.

    No administrator rights are needed.

.PARAMETER Uninstall
    Stop the app and remove the task.

.PARAMETER StartNow
    Also start the task right away. Stop any copy of the app you started by
    hand first, or the new one cannot bind the port.

.PARAMETER Stop
    Stop the running app, whether the task or a terminal started it. Ending
    the task in Task Scheduler is not enough: that only ends the console
    wrapper, and uv and the app keep running.

.PARAMETER Restart
    Stop the running app, then start the task (e.g. after pulling new code).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Restart

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$StartNow,
    [switch]$Stop,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$TaskName = 'Garmin Weight Coach'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Stop-App {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    # The app runs as uv -> gwc.exe -> python. Find the processes running this
    # project's gwc.exe, climb to the uv that launched them, and end each tree.
    $gwcExe = Join-Path $ProjectRoot '.venv\Scripts\gwc.exe'
    $all = @(Get-CimInstance Win32_Process)
    $byId = @{}
    foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
    $roots = @{}
    foreach ($p in $all) {
        if (-not $p.CommandLine -or $p.CommandLine.IndexOf($gwcExe, [StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
        if ($p.CommandLine -notmatch '\sserve\b') { continue }
        $root = $p
        while ($true) {
            $parent = $byId[[int]$root.ParentProcessId]
            if (-not $parent) { break }
            $isLauncher = $parent.Name -eq 'uv.exe' -or
                ($parent.CommandLine -and $parent.CommandLine.IndexOf($gwcExe, [StringComparison]::OrdinalIgnoreCase) -ge 0)
            if (-not $isLauncher) { break }
            $root = $parent
        }
        $roots[[int]$root.ProcessId] = $true
    }
    foreach ($id in $roots.Keys) {
        & taskkill.exe /T /F /PID $id | Out-Null
    }
    if ($roots.Count) {
        Write-Host 'Stopped the app.'
    } else {
        Write-Host 'The app was not running.'
    }
}

if ($Stop -or $Restart) {
    Stop-App
    if ($Restart) {
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $TaskName
        Write-Host 'Started. The app is at http://127.0.0.1:8060 once it finishes starting.'
    }
    return
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-App
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the '$TaskName' task."
    } else {
        Write-Host "No '$TaskName' task is installed."
    }
    return
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    throw 'uv was not found on PATH. Install it, open a new terminal, and run this script again.'
}

# conhost --headless runs the console app with no window. Launching uv directly
# would open a console (or a Windows Terminal tab) that closing would kill.
$action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\conhost.exe" `
    -Argument "--headless `"$uv`" run gwc serve" `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
# Give the network and Tailscale a moment after log on.
$trigger.Delay = 'PT30S'

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Runs 'uv run gwc serve' from $ProjectRoot at log on." `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Installed the '$TaskName' task. It starts the app 30 seconds after you log on."
Write-Host "Manage it in Task Scheduler, or remove it with: scripts\install_autostart.ps1 -Uninstall"

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host 'Started. The app is at http://127.0.0.1:8060 once it finishes starting.'
}
