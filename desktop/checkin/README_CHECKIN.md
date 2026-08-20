# WorkBuddy + Trae 自动签到（集成版）使用说明

## 功能概览
- 自动检测 WorkBuddy / TraeWork 是否在运行，若未运行则尝试启动
- 使用界面点击 (pyautogui) + RapidOCR 进行签到，不调用任何内部 API
- 提供导出 Hermes、Claude、OpenCode 配置的功能
- 支持状态检查、校准、日志记录（使用 loguru，保留最近 7 天）

## 文件说明
- `checkin_launcher.py`：主脚本，支持子命令
- `run_workbuddy_checkin.bat`：一键执行 WorkBuddy 签到
- `run_checkin.bat`：一键执行 TraeWork 签到
- `run_both.bat`：依次执行两个签到
- `schedule_workbuddy_task.bat`、`schedule_task.bat`、`schedule_1am.bat`：定时任务注册脚本
- `wb_checkin_config.json`、`checkin_config.json`：校准后生成的配置文件
- `wb_last_checkin.txt`、`last_checkin.txt`：防重复签到记录
- `checkin.log`：日志文件
- `shots/`：签到过程中的截图（用于调试）

## 首次使用
1. 确保已安装 Python 3.11（或更高）以及依赖：
   ```bat
   C:\Python311\python.exe -m pip install -r requirements.txt
   ```
2. 运行校准（只需做一次）：
   ```bat
   run_workbuddy_checkin.bat calibrate
   run_checkin.bat calibrate
   ```
   按照提示把鼠标移动到对应程序左下角的头像图标上，不要点击，直接回车记录坐标。

## 日常签到
```bat
run_workbuddy_checkin.bat   // 执行 WorkBuddy 签到
run_checkin.bat             // 执行 TraeWork 签到
run_both.bat                // 依次执行两个
```
脚本会先检测目标程序是否在运行；若未运行，会尝试根据常见安装路径或注册表自动启动，然后进行签到。

## 导出外部工具配置
```bat
python checkin_launcher.py export hermes   -> 生成可粘入 Hermes config.yaml 的片段
python checkin_launcher.py export claude   -> 生成 Claude .env 文件内容
python checkin_launcher.py export opencode -> 生成 OpenCode 配置命令
```
将输出内容粘入对应工具的配置文件或环境变量中，即可让 Hermes、Claude、OpenCode 直接使用本机聚合网关。

## 查看签到状态（不修改防重记录）
```bat
python checkin_launcher.py status workbuddy
python checkin_launcher.py status trae
```

## 定时任务
- `schedule_workbuddy_task.bat`：每天 09:05 执行 WorkBuddy 签到
- `schedule_task.bat`：每天 09:00 执行 TraeWork 签到
- `schedule_1am.bat`：每天 01:00 执行两次签到
如需修改时间，直接编辑对应的 .bat 文件中的 `SET STARTTIME=HH:MM` 并以管理员身份重新运行一次以重新注册任务。

## 日志
- 日志文件：`checkin.log`（位于脚本同目录）
- 日志使用 loguru，保留最近 7 天，每日一个文件，同时输出到控制台。
- 可通过 `checkin.log` 查看签到过程和任何错误信息。

## 常见问题
- **找不到可执行文件**：脚本会先在常见路径和注册表中查找 WorkBuddy.exe、TraeWorkCN.exe。若仍未找到，请手动确保程序已在运行，或在 `checkin_launcher.py` 顶部修改 `WB_EXE`、`TRAE_EXE` 为实际路径。
- **OCR 未识别到签到行**：请确保程序窗口可见且未被最化或遮挡。可检查 `shots/` 目录下的 `_diagnose_*.png` 截图进行调试。如界面发生变化，请重新运行校准。
- **今日已签过但实际未签**：防重记录 (`wb_last_checkin.txt` / `last_checkin.txt`) 可能被误写。可手动删除这些文件后重新运行签到。

## 注意
- 签到过程需要桌面交互，因此请确保电脑未锁屏，且 WorkBuddy / TraeWork 窗口可见。
- 如需以系统服务方式在后台运行，请考虑使用 Windows 服务包装器（非本脚本当前支持范围）。