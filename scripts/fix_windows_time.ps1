$ErrorActionPreference = 'Stop'

$logDirectory = Join-Path $PSScriptRoot '..\data'
$logPath = Join-Path $logDirectory 'windows_time_fix.log'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

try {
    Set-Service -Name W32Time -StartupType Automatic
    Start-Service -Name W32Time

    & w32tm /config /manualpeerlist:"ntp.aliyun.com,0x8 time.windows.com,0x8" /syncfromflags:manual /update | Out-String | Add-Content -Path $logPath -Encoding UTF8
    & w32tm /resync /rediscover | Out-String | Add-Content -Path $logPath -Encoding UTF8

    Start-Sleep -Seconds 3
    Get-Service W32Time | Select-Object Name, Status, StartType | Format-List | Out-String | Add-Content -Path $logPath -Encoding UTF8
    & w32tm /query /status | Out-String | Add-Content -Path $logPath -Encoding UTF8
    exit 0
}
catch {
    $_ | Out-String | Add-Content -Path $logPath -Encoding UTF8
    exit 1
}
