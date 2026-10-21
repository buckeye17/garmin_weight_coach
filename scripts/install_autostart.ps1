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
    Remove the task instead of creating it.

.PARAMETER StartNow
    Also start the task right away. Stop any copy of the app you started by
    hand first, or the new one cannot bind the port.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$TaskName = 'Garmin Weight Coach'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
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
