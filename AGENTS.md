# AGENTS.md — 智能网关 (AI Gateway Pro)

## 项目概述

把各上游平台（NVIDIA / 魔搭 / SenseNova / OpenRouter / CodeArts / TRAE / Qoder / WorkBuddy / LobsterAI 等）的免费额度收编为**一个** OpenAI 兼容端点（`/v1`），路由、熔断、故障转移、额度分摊全由网关接管。

- 后端：FastAPI + SQLAlchemy 2 + httpx + APScheduler，默认 SQLite（可选 MySQL 8.0）
- 前端：Vue 3 + Element Plus + vue-i18n（源码 `web-vue/`，构建产物 `web/` 由后端静态托管）
- 桌面：pywebview + WebView2 壳（`desktop/`），PyInstaller 打包 `dist/智能网关.exe`

## 常用命令

```bash
# 后端测试（CI 同款；解释器一律用项目 .venv）
pytest tests/ -q
pytest tests/test_circuit.py -v          # 单文件

# 前端（vite outDir=../web，emptyOutDir 会清空 web/）
cd web-vue && npm install && npm run build

# 启动后端（本机开发/生产均按 8765 端口跑）
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765

# 桌面客户端
python desktop/desktop.py

# 打包 exe（必须用项目 .venv 的 Python）
pyinstaller ai_gateway_pro.spec          # 产物 dist/智能网关.exe

# 部署到生产并重启
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy_gw.ps1
```

无 lint/typecheck 配置（没有 ruff/mypy/pyproject.toml）；CI（.github/workflows/ci.yml）只跑 pytest + vite build。

## 架构边界

- **三协议面共用同一套核心引擎**：`/v1/chat/completions`（api/proxy.py）、`/v1/messages`（api/anthropic.py，Anthropic 原生）、`/v1/responses`（api/responses.py，OpenAI Responses 原生）。改 core 的路由/熔断/故障转移会同时影响三个面；协议变换在 `core/anthropic_codec.py`、`core/responses_codec.py`。
- **分层**：`backend/api/` 路由层 → `backend/core/` 引擎（router_group 选候选、circuit 熔断、scheduler 质量分、orchestrate 编排、proxy 转发）→ httpx 上游；`backend/auth/` JWT + API Key 双鉴权，渠道密钥 AES-256-GCM 加密存储。
- **所有 ORM 模型集中在 `backend/models/__init__.py` 单文件**，新增表/字段改这里（create_tables 自动建表）。
- 配置：pydantic-settings，环境变量前缀 `GW_`，见 `backend/config.py`（同时读 `.env`）。
- 后台定时任务（签到、免费源同步、死模型复检等）注册在 `backend/main.py` 的 lifespan。

## 约定与坑

- **新功能必须带 pytest 用例**；`tests/conftest.py` 默认 `GW_LICENSE_ENABLED=0`。不要新增依赖真实上游/网络凭据的用例（白嫖池是公共额度，CI 沙箱也跑不了）。
- **生产跑在 `D:\智能网关\智能网关.exe`，不是仓库 dist/**；构建后必须跑 `scripts/deploy_gw.ps1` 部署，部署前确认 `web/assets` 已随包存在。
- `.ps1` 脚本含中文必须保存为 **UTF-8 with BOM**（否则 PowerShell 乱码）。
- Cython 混淆构建（`scripts/setup_cython.py` + `scripts/build_exe.py`）只在 staging 目录进行、不动源码树；`main.py` 与 `config.py` 不可编译。
- 根目录大量 `*.log` / `*.mp4` / `build_*` / `temp/` 等是本地工作残留（已 gitignore），勿清理、勿提交。
- 改前端只动 `web-vue/`，`web/` 是构建产物；i18n 文案在 `web-vue/src/i18n/locales/`（zh-CN 与 en-US 要同步改）。
- 注释、日志、文档均使用中文；运行日志在 `logs/gateway.log`，数据库默认 `backend/data/`（生产 `D:\智能网关\data\gateway.db`）。

## 文档（改动敏感区域前先读）

- `docs/04-架构设计.md` — 模块职责、请求决策链路、数据表、设计决策
- `docs/03-API参考.md` — 全部 HTTP 端点（改 API 须同步）
- `docs/07-白嫖渠道收编路线图.md` — 白嫖渠道接入的协议/签到细节
- `KNOWN_ISSUES.md` — 已知问题与规划（设备绑定误锁、授权开关未完全生效等，改授权/机器码相关代码前必读）
