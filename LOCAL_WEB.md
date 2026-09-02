# 本地网页版使用说明

## 启动

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe main.py -m serve
```

如需临时使用守护进程，可直接运行 `scripts/watchdog.py`；日常使用建议安装下面的计划任务。

浏览器访问 <http://127.0.0.1:8787/>。首次启动会在 `data/app.db` 创建 SQLite 数据库。

也可以运行 `powershell -ExecutionPolicy Bypass -File scripts/install_task.ps1 -StartNow`，安装守护式开机自启任务并立即启动。守护进程会通过 `/health` 检查服务和调度器状态，异常退出或假死时自动重启。如果创建计划任务被系统拒绝，可改用当前用户启动目录：`powershell -ExecutionPolicy Bypass -File scripts/install_startup.ps1`。

## 使用顺序

1. 在“账号”中添加超星账号；密码使用当前 Windows 用户的 DPAPI 加密保存。
2. 创建预约计划，填写阅览室 ID、起止时间、执行时间和候选座位号（候选数不能超过总尝试次数）。
3. 点击“自动发现参数”或“探测”，确认目标日期参数就绪；探测不会提交预约。通常不需要粘贴选座链接，只有自动发现失败时才在高级兜底栏保存链接覆盖。
4. 通过“预约”按钮手动验证，确认无误后再启用计划任务。

默认使用北京时间、预约次日、整个计划最多 3 次尝试，两次尝试至少间隔 2 秒。不同账号可受控并行，同一账号会串行。当前页面显示“有人使用”只作为参考，不会再被当成未来日期已被抢；只有超星明确返回座位不可用才会换座。提交后超时、响应无法解析或结果不明确时会停止为“需要核实”，不会自动再次提交。滑块和点选验证码均会安全停止并提示人工处理。守护日志位于 `data/logs/watchdog.log`，服务输出位于 `data/logs/service.log`，日志会轮转。

“任务执行星期”表示哪一天运行；“预约第几天”表示从运行日向后预约几天。历史运行记录显示北京时间，并保存计划与账号快照、参数来源及是否实际提交；服务中断留下的任务会标为“需要确认”，不会盲目重试。首次升级会先在 `data/backups/` 创建数据库备份。

## 兼容旧脚本

原有命令仍可用：

```powershell
python main.py -m debug -u config.json
python main.py -m reserve -u config.json
python main.py -m room -u config.json
```

新网页版本使用独立 SQLite 配置，不会修改或删除原来的 `config.json`。

## 安全边界

- 服务只监听 `127.0.0.1`，不应通过端口转发暴露到局域网。
- 不要把 `data/`、账号密码、Cookie 或运行日志提交到 Git。
- 检测到点选验证码、非法预约、限流或安全验证超时会停止本次任务。
