# AI 模型网关 Pro

AI 模型网关桌面客户端 — 管理多渠道模型，统一 OpenAI 兼容接口，支持路由组、熔断、Fallback、自动签到。

## 功能

| 功能 | 说明 |
|------|------|
| **渠道管理** | 添加/管理 NVIDIA、魔搭、SenseNova 等上游提供商 |
| **路由组** | 手动分组 + 自动分类（简单/高质量/图片识别），客户端 `model: 组名` 即可命中 |
| **熔断+Fallback** | 自动检测上游失败，按质量分排序切换候选模型 |
| **定时签到** | 自动签到 WorkBuddy / Trae 办公应用 |
| **用量统计** | 请求数、Token 消耗统计 |

## 快速开始

### 1. 安装依赖

```bash
cd ai-gateway-pro
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

### 2. 构建前端

```bash
cd web-vue
npm install
npm run build
```

### 3. 运行

```bash
# 桌面客户端（推荐）
python desktop/desktop.py

# 或纯 Web 模式
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

打开 http://127.0.0.1:8765，注册账号 → 添加渠道 → 配置路由组 → 开始使用。

### 4. 打包为 exe

```bash
pip install pyinstaller
pyinstaller ai_gateway_pro.spec
```

输出在 `dist/AI模型网关Pro.exe`，双击运行。

## 架构

```
┌─────────────────────────────────────────────────┐
│                桌面客户端 (pywebview)              │
│  ┌───────────────────┐  ┌────────────────────┐   │
│  │   Vue 3 + Element  │  │   PyInstaller 打包  │   │
│  │   前端页面          │  │   桌面壳 + 后端      │   │
│  └────────┬──────────┘  └────────┬───────────┘   │
│           │ HTTP/JSON             │ inline        │
│  ┌────────▼──────────────────────▼───────────┐   │
│  │         FastAPI 后端 (uvicorn)              │   │
│  │  ┌─────────┐ ┌─────────┐ ┌──────────┐    │   │
│  │  │ 渠道管理  │ │ 路由组   │ │ 熔断/限流 │    │   │
│  │  └─────────┘ └─────────┘ └──────────┘    │   │
│  │  ┌─────────┐ ┌─────────┐ ┌──────────┐    │   │
│  │  │ 定时签到  │ │ 用量统计 │ │ SQLiteDB │    │   │
│  │  └─────────┘ └─────────┘ └──────────┘    │   │
│  └─────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

## 路由组使用

客户端通过 OpenAI 兼容 SDK 调用：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="你的API密钥")

# 用路由组名代替具体模型名
resp = client.chat.completions.create(
    model="quick",  # 命中 quick 组，组内自动 fallback
    messages=[{"role": "user", "content": "你好"}]
)
```

内置路由组：`quick`(简单) / `high_quality`(高质量) / `vision`(图片识别)。

## 测试

```bash
pytest tests/
```

当前 71 个测试，覆盖：渠道 CRUD、路由组管理、路由分类、代理转发、调度器、鉴权。