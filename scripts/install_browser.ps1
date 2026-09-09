param([string]$Project = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))
$ErrorActionPreference = 'Stop'
$browserPython = Join-Path $Project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $browserPython)) { throw '请先创建项目 Python 虚拟环境' }
& $browserPython -m pip install -r (Join-Path $Project 'requirements-browser.txt')
if ($LASTEXITCODE -ne 0) { throw '浏览器组件安装失败' }
& $browserPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Chromium 下载失败，请检查网络后重试' }
Write-Host '安装完成。请在管理页面点击浏览器演练，在打开的官方页面登录。'
