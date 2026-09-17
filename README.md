# 智能网关 · AI Gateway Pro

[![CI](https://github.com/liruiqing76/ai-gateway-pro/actions/workflows/ci.yml/badge.svg)](https://github.com/liruiqing76/ai-gateway-pro/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Vue](https://img.shields.io/badge/Vue-3-42b883.svg)](https://vuejs.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()

> 一个 OpenAI 兼容端点，聚合所有模型的免费额度。
> One OpenAI-compatible endpoint, aggregating free quotas from every model provider.

> - README 中文：本文档
> - README English: [README.en.md](README.en.md)
> - 详细文档手册: [docs/](docs/00-概览.md)
- 已知问题与规划: [KNOWN_ISSUES.md](KNOWN_ISSUES.md)

`AI Gateway Pro` 把 NVIDIA、魔搭 ModelScope、SenseNova、AMD Token Factory、OpenRouter、OpenCode Zen、Agnes 等上游平台的**免费额度**统一收编，对外暴露**一个** OpenAI 兼容接口（`/v1`）。客户端只需填一个简单的模型名，剩下的一切——模型选择、路由决策、熔断降级、故障转移、额度控制——全部交给网关。

---

## 目录

- [核心特性](#-核心特性)
- [架构总览](#-架构总览)
- [一次请求如何工作](#-一次请求如何工作)
- [API 端点一览](#-api-端点一览)
- [界面截图](#-界面截图)
- [快速开始](#-快速开始)
- [接入示例](#-接入示例)
- [配置](#-配置)
- [测试](#-测试)
- [项目结构](#-项目结构)
- [文档索引](#-文档索引)
- [打包桌面版 exe](#-打包桌面版-exe)
- [授权说明](#-授权说明)
- [许可证](#-许可证)

---

## 🚀 核心特性

| 能力 | 说明 |
|------|------|
| **多渠道聚合** | 收敛 NVIDIA / 魔搭 / SenseNova / AMD / OpenRouter 等免费上游，密钥 **AES-256-GCM 加密存储**，预设模板一键添加、模型自动拉取 |
| **免费源自动同步** | 定时拉取全网免费源 JSON 与本地模板合并，新增免费服务商自动进入「添加渠道」模板池 |
| **OpenCode Zen 自动渠道** | 定时发现 Zen 免费模型，真实探测 + 持续健康保活，失效模型自动移出 |
| **b.ai 自动渠道** | 定时拉取 b.ai 模型目录并健康探测（可选，可关，保留现有渠道不误清） |
| **智能路由组** | `quick` / `high_quality` / `vision` 三分类 + 用户自定义组，客户端填**组名/中文组名**即命中 |
| **多种路由策略** | `quality` 质量优先 / `fastest` 最快优先 / `stable` 最稳优先 |
| **加权流量分发** | 轮询 + 「质量 × 今日用量平滑 × 渠道冷却」加权随机抽样，免费额度日日均摊，避免逮住单一模型打爆配额 |
| **熔断 + 故障转移** | 连续失败自动熔断、半开探测恢复；单次失败自动切换同组下一个候选，流式请求**无缝切换** |
| **限流冷却** | 命中 429 后指数退避（30s → 60s → 120s → 300s） |
| **多模型编排** | `relay:组A/组B` 接力（≤5 级）、`review:主/审` 审阅，最终一级支持流式输出 |
| **流式输出** | 单一请求与编排请求均支持 OpenAI SSE 流式 |
| **模型别名** | 自定义映射（`gpt-4` → 路由组），兼容现有 OpenAI 客户端 |
| **自动识图** | 请求内视觉识别自动选渠道，失败降级文本兜底 |
| **请求链路追踪** | 每次请求完整记录选路原因、实际渠道/模型、延迟、熔断/限流命中、编排阶段 |
| **用量统计 + 计费** | 请求数 / Token / 成功率统计；free / pro / enterprise 套餐与订单管理 |
| **自动签到** | 定时签到 WorkBuddy / 微信读书等平台领取免费额度 |
| **免费模型情报** | 每日定时爬取 Gitee / GitHub / 百度 / 头条 / 公众号免费模型帖子，入库 + 可行性评估 + 生成报告 |
| **死模型自动复检** | 每日对拉黑模型做真实探测，误杀的 200 自动平反 |
| **多用户 + 双鉴权** | 每用户独立渠道 / 路由 / 别名 / 额度；**JWT + API Key** 双鉴权 |
| **桌面客户端** | pywebview + WebView2 壳，内嵌后端一键启动，支持开机自启 |

---

## 🏗️ 架构总览

```
┌─────────────────────────────────────────────────────┐
│             客户端 (OpenAI SDK / curl / 桌面UI)        │
└───────────────────────┬─────────────────────────────┘
                        │ HTTP
┌───────────────────────▼─────────────────────────────┐
│                 FastAPI 后端 (uvicorn)               │
│                                                     │
│   ┌────────────┐  ┌─────────────┐  ┌─────────────┐  │
│   │ auth 鉴权层 │  │ proxy 代理层 │  │ manage API  │  │
│   │ JWT/APIKey │  │   /v1/*     │  │   /api/*    │  │
│   └─────┬──────┘  └─────┬───────┘  └──────┬──────┘  │
│         │               │                  │         │
│   ┌─────▼───────────────▼──────────────────▼──────┐  │
│   │             core 核心引擎                      │  │
│   │  router_group → pick_available_models        │  │
│   │  → circuit(熔断) + scheduler(质量分)          │  │
│   │  → orchestrate(编排) + trace(链路)            │  │
│   └─────┬─────────────────────────┬──────────────┘  │
│         │  decrypt_secret         │                 │
│   ┌─────▼─────────────────────────▼──────────────┐  │
│   │             httpx AsyncClient (连接池)        │  │
│   └─────┬─────────────────────────┬──────────────┘  │
│         │                         │                 │
│   ┌─────▼─────────┐         ┌─────▼──────────┐      │
│   │ SQLite / MySQL │         │  上游 AI 平台    │      │
│   │ users/渠道/路由 │         │ NVIDIA/魔搭/... │      │
│   │ 链路/用量/订单  │         │                │      │
│   └───────────────┘         └────────────────┘      │
└─────────────────────────────────────────────────────┘
```

### 分层说明

- **auth 鉴权层**：API Key / JWT 双通道；bcrypt 密码哈希、AES-256-GCM 渠道密钥加密。
- **proxy 代理层**：`/v1/chat/completions`、`/v1/models` 等 OpenAI 兼容端点。
- **manage API**：渠道、路由组、别名、用量、追踪等管理端点（`/api/*`）。
- **core 核心引擎**：模型候选选择、熔断状态机、质量评分调度、编排执行、链路追踪上下文。
- **存储**：默认 SQLite（本地文件），可选 MySQL 8.0。

---

## ⚙️ 一次请求如何工作

以 `POST /v1/chat/completions`、`model: "high_quality"`、非流式为例：

```
1. 鉴权       get_current_user_from_api_key 校验 API Key
2. 配额       check_quota 校验今日调用次数
3. 装载上下文   查询 enabled=true 的渠道 / 路由组 / 别名 / 路由策略 → 关闭 session
4. 解密密钥    decrypt_secret 解密每个渠道的 API Key
5. 解析模型    parse_pipeline → 无编排；别名表无命中；normalize_group_name("high_quality")
6. 选候选      pick_available_models:
                - 组内模型集合 { NVIDIA-xxx, 魔搭-yyy, ... }
                - 排除熔断中的 → 按 strategy(quality)：质量分降序、延迟升序
7. 兜底候选    全渠道模型列表（放宽匹配）
8. 逐个尝试    候选1: forward_non_stream → 上游4xx → record_fail + 质量分降 → 候选2
              候选2: forward_non_stream → 200 → record_success + 质量分升
9. 记录        UsageLog + quota_used_today+1 + trace 落库
10. 返回       JSONResponse（model 字段标记为 "NVIDIA · deepseek-v3"）
```

**流式**：连接后预读首个 SSE chunk 校验有效性，无效视为失败切换候选；成功 → 直接 `StreamingResponse`，`X-Provider` / `X-Model` 头标记实际渠道 / 模型。

**编排**：命中 `relay:` / `review:` 时每个阶段内部重复步骤 6-8（阶段级故障转移）；非流式全部阶段执行完返回，流式最后一级才流式输出（前序需要完整输出作为下一级输入）。

---

## 🔌 API 端点一览

| 前缀 | 模块 | 用途 |
|------|------|------|
| `POST /v1/chat/completions` | proxy | OpenAI 兼容对话（流式/非流式、编排、别名） |
| `GET  /v1/models` | proxy | 可用模型列表 |
| `/api/providers` | providers | 渠道 CRUD + 预设模板 |
| `/api/routers` | routers | 路由组 CRUD |
| `/api/routers/check-availability` | router_check | 路由组可用性检测 |
| `/api/aliases` | aliases | 模型别名 CRUD |
| `/api/traces` | traces | 请求链路查询 / 统计 / 清理 |
| `/api/usage` | usage | 用量统计 |
| `/api/free-sources` | free_sources | 免费源自动同步 |
| `/api/free-model-search` | free_model_search | 免费模型帖子搜索报告 |
| `/api/dead-models` | dead_model_api / downed_models | 失活模型管理 & 自动复检 |
| `/api/chats` | chats | 会话记录 |
| `/api/requests` | requests | 请求/计费记录 |
| `/api/experience` | experience | 经验积累（LLM 提炼，默认关） |
| `/api/license` | license | 设备绑定 / 授权状态 |
| `（auth）` | auth | 注册 / 登录 / 刷新 / API Key 轮换 |

> 完整字段与请求/响应示例见 [docs/03-API参考.md](docs/03-API参考.md)。

---

## 🖥️ 界面截图

| 界面 | 预览 |
|------|------|
| 仪表盘 Dashboard | ![dashboard](docs/images/dashboard.png) |
| 渠道管理 Providers | ![providers](docs/images/providers.png) |
| 路由组 Routers | ![routers](docs/images/routers.png) |
| 请求日志 Traces | ![traces](docs/images/traces.png) |
| 自动签到 Auto Checkin | ![checkin](docs/images/auto-checkin.png) |

*以上为实际运行界面截图。*

---

## 🚀 快速开始

### 环境要求

- Python 3.10+（推荐 3.11+）
- Node.js 18+（仅构建前端时需要）
- Windows / macOS / Linux

### 安装与启动

```bash
# 1. 创建虚拟环境并安装后端依赖
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
pip install -r requirements.txt

# 2. 构建前端（输出到 web/，由后端静态托管）
cd web-vue
npm install
npm run build
cd ..

# 3. 启动 —— 桌面客户端（推荐）
python desktop/desktop.py

# 或纯 Web（仅后端）模式
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765`，按下面三步即可开始使用：

1. **注册 / 登录**
2. **添加渠道** —— 「渠道管理 → 模板一键添加」（NVIDIA、魔搭、SenseNova、AMD、OpenRouter 等），填入平台 API Key
3. **复制 API Key** —— 「API 密钥」生成后，用任意 OpenAI 客户端指向网关 `http://127.0.0.1:8765/v1`

---

## 💻 接入示例

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="你的API密钥",          # 网关分配的 API Key
)

resp = client.chat.completions.create(
    model="high_quality",          # 路由组名 / 别名 / 编排格式 / 具体模型名
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### curl（非流式）

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer 你的API密钥" \
  -H "Content-Type: application/json" \
  -d '{"model":"quick","messages":[{"role":"user","content":"你好"}]}'
```

### curl（流式 SSE）

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer 你的API密钥" \
  -H "Content-Type: application/json" \
  -d '{"model":"quick","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

### 进阶：多模型编排与别名

- 接力编排：`model = "relay:组A/组B"`（A 的输出接给 B，最多 5 级）
- 审阅编排：`model = "review:主/审"`（主答 + 审阅修正，最终流式输出）
- 模型别名：`gpt-4` → 某个路由组，让现有 OpenAI 客户端无缝接入

> 详细用法见 [docs/02-功能指南.md](docs/02-功能指南.md)。

---

## ⚙️ 配置

所有配置项前缀 `GW_`，可用环境变量或修改 `backend/config.py`（pydantic-settings，同时读取 `.env`）。常用项：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GW_HOST` | `127.0.0.1` | 监听地址 |
| `GW_PORT` | `8000` | 监听端口 |
| `GW_DATABASE_URL` | 空（自动 SQLite） | 数据库连接串（支持 MySQL） |
| `GW_SECRET_KEY` | `CHANGE_ME...` | JWT / 加密密钥（生产必须修改） |
| `GW_DEBUG` | `false` | 调试模式 |
| `GW_PROXY_TIMEOUT_SECONDS` | `120` | 上游请求总超时 |
| `GW_PROXY_STREAM_READ_TIMEOUT` | `30` | 流式读超时（卡住自动换候选） |
| `GW_PROXY_NONSTREAM_READ_TIMEOUT` | `30` | 非流式读超时 |
| `GW_ROUND_ROBIN_ENABLED` | `true` | 轮询分发 |
| `GW_SPREAD_WEIGHTED_ENABLED` | `true` | 加权流量分发 |
| `GW_SHAPING_DEFAULT_MAX_CONCURRENCY` | `4` | 渠道并发上限 |
| `GW_CHECKIN_ENABLED` | `true` | 自动签到开关 |
| `GW_FREE_SOURCE_AUTO_SYNC` | `true` | 免费源自动同步 |
| `GW_ZEN_AUTO_SYNC` | `true` | OpenCode Zen 自动渠道 |
| `GW_LICENSE_ENABLED` | `true` | 本地设备硬件绑定开关 |

> 完整配置项及说明见 [backend/config.py](backend/config.py)。

---

## 🧪 测试

```bash
pytest tests/ -v                  # 全部用例（124+，含性能基准）
pytest tests/test_performance.py  # 性能基准
```

---

## 📂 项目结构

```
ai-gateway-pro/
├── backend/                 # FastAPI 后端
│   ├── main.py              # 应用入口，路由注册，后台定时任务
│   ├── config.py            # 配置（GW_* 环境变量 / .env）
│   ├── api/                 # API 路由层（proxy/providers/routers/usage/traces…）
│   ├── core/                # 核心引擎（路由/熔断/编排/质量调度/免费源/Zen同步）
│   ├── auth/                # 鉴权（JWT / API Key / AES 加密 / 授权绑定）
│   ├── billing/             # 计费模块
│   ├── models/              # SQLAlchemy ORM 模型
│   └── data/                # SQLite 数据库与模板
├── web-vue/                 # Vue 3 前端源码
├── web/                     # 前端构建产物（后端静态托管）
├── desktop/                 # pywebview 桌面壳 + 自动签到
├── docs/                    # 中文文档手册（00~06）
├── tests/                   # 测试套件（124+ 用例）
└── smart-gateway-design/    # 高保真设计稿原型
```

---

## 📖 文档索引

| 文档 | 内容 |
|------|------|
| [00-概览](docs/00-概览.md) | 项目介绍、能力矩阵、技术栈、架构 |
| [01-快速开始](docs/01-快速开始.md) | 安装 / 运行 / 打包 / 配置 / FAQ |
| [02-功能指南](docs/02-功能指南.md) | 每个功能的详细用法 |
| [03-API参考](docs/03-API参考.md) | 全部 HTTP 端点与请求 / 响应示例 |
| [04-架构设计](docs/04-架构设计.md) | 模块职责、请求决策链路、数据表、设计决策 |
| [05-运维与性能](docs/05-运维与性能.md) | 日志、链路追踪、性能基准、FAQ |
| [06-渠道注册与接入](docs/06-渠道注册与接入文档.md) | 各上游渠道的注册与接入参数 |

---

## 📦 打包桌面版 exe

```bash
pip install pyinstaller
pyinstaller ai_gateway_pro.spec   # 输出 dist/智能网关.exe
```

---

## 📄 授权说明

本仓库同时包含**开源核心**与**商业授权增强模块**（如设备绑定 / 远程授权锁定）。

- 商业授权模块代码保留在仓库中，但可通过 `GW_LICENSE_ENABLED=false` 关闭验证，不影响正常功能使用。
- 开源使用 / 个人自用默认不受锁约束。

---

## 🔗 许可证

本仓库采用 [MIT License](LICENSE)（开源核心部分另附商用条款，见上「授权说明」）。

---

## 🙏 致谢

基于开源生态构建：

- FastAPI · SQLAlchemy 2 · httpx
- Vue 3 · Element Plus
- APScheduler · pywebview · PyInstaller
- RapidOCR