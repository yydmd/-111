param(
    [string]$Project = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
$Python = Join-Path $Project ".venv\Scripts\python.exe"
$Watchdog = Join-Path $Project "scripts\watchdog.py"

if (-not (Test-Path -LiteralPath $Python)) { throw "找不到虚拟环境：$Python" }
if (-not (Test-Path -LiteralPath $Watchdog)) { throw "找不到守护脚本：$Watchdog" }

$taskName = "ChaoXingLocalReserveWatchdog"
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Watchdog`"" -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$settings.ExecutionTimeLimit = "PT0S"

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Watch and restart the local ChaoXing reservation web app" `
    -Force | Out-Null

# Remove the older direct-service task if this project previously installed it.
$legacyTask = Get-ScheduledTask -TaskName "ChaoXingLocalReserve" -ErrorAction SilentlyContinue
if ($legacyTask) {
    Stop-ScheduledTask -TaskName "ChaoXingLocalReserve" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "ChaoXingLocalReserve" -Confirm:$false
}

if ($StartNow) {
    Start-ScheduledTask -TaskName $taskName
}

Write-Host "Watchdog task installed: $taskName"
Write-Host "Service URL: http://127.0.0.1:8787/"
if (-not $StartNow) {
    Write-Host "Run this script with -StartNow to start it immediately."
}
