param(
    [string]$Project = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)

$ErrorActionPreference = "Stop"
$Python = Join-Path $Project ".venv\Scripts\python.exe"
$Watchdog = Join-Path $Project "scripts\watchdog.py"
$Startup = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $Startup "ChaoXingReserveSeatWatchdog.lnk"

if (-not (Test-Path -LiteralPath $Python)) { throw "找不到虚拟环境：$Python" }
if (-not (Test-Path -LiteralPath $Watchdog)) { throw "找不到守护脚本：$Watchdog" }

$shell = New-Object -ComObject "WScript.Shell"
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $Python
$shortcut.Arguments = "`"$Watchdog`""
$shortcut.WorkingDirectory = $Project
$shortcut.WindowStyle = 7  # minimized
$shortcut.Description = "Watchdog for the local ChaoXing reservation service"
$shortcut.Save()

Write-Host "Startup shortcut created: $ShortcutPath"
Write-Host "The minimized watchdog will start after this Windows user logs in."
