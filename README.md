# 智能网关

智能网关桌面客户端 — 管理多渠道模型，统一 OpenAI 兼容接口，支持路由组、熔断、Fallback、自动签到。

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

输出在 `dist/智能网关.exe`，双击运行。

## 常见问题：桌面版窗口白屏 / 无界面

**症状**：双击打包后的 exe，窗口能弹出，但内容是空白（白色或背景色），无法显示登录/管理界面。

**根因**（三层，按出现顺序）：

1. **WebView2 初始化失败（RPC_E_CHANGED_MODE）**：桌面壳依赖 `pythonnet`(clr) 启动 WebView2 控件。`pythonnet` 在导入阶段会把主线程 COM 设为 MTA（多线程套间），而 WebView2 要求主线程是 STA（单线程套间），两者冲突导致窗口创建/渲染失败，表现为整个界面不渲染。这对本机开发（`python desktop/desktop.py`）和打包 exe 都可能出现。
2. **后端尚未就绪就导航**：桌面端通过 `http://127.0.0.1:8765/` 加载界面，而单文件 exe 启动时解包 + FastAPI 冷启动需要数秒到数十秒。若在 WebView2 就绪前立即导航，请求被拒（后端未监听）会落在一个失败页，且不会自动重试 → 空白。
3. **渲染引擎被误选为 IE 内核**：旧版本未显式指定引擎时，WebView2 不可用会回退到 IE 或干脆失败。

**修复措施**（已内置到 [desktop/desktop.py](desktop/desktop.py)）：

- 在任何 `import webview` / `import pythonnet` **之前**，先把主线程 COM 显式初始化为 STA（`pythoncom.CoInitializeEx(COINIT_APARTMENTTHREADED)`），避免 pythonnet 抢先设为 MTA。
- `gui='edgechromium'` 强制使用 WebView2 引擎，不选 IE。
- 创建窗口前轮询 `/api/health`，后端就绪后再加载页面；仍未就绪则等待后重试，不再一次性跳到失败页。
- 前端 `bootstrap()` 登录失败时自动重试，降低后端冷启动造成的偶发白屏。

**如果仍然白屏，按下面排查**：

1. `netstat -ano | findstr :8765` 确认后端是否在监听；后端未起则大概率是打包缺依赖或启动异常。
2. 确认目标机器已安装 **Microsoft Edge WebView2 Runtime**（Windows 10/11 通常自带；内网或精简系统可能缺失）。
3. 长期未发挥作用的旧 exe 建议**用当前源码重新打包一次**再验证（`pyinstaller ai_gateway_pro.spec`）。

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