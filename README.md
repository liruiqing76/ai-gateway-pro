# ⚡ 智能网关（AI Gateway Pro）

多渠道 AI 模型统一网关 — 一个 OpenAI 兼容端点，聚合 NVIDIA / 魔搭 / SenseNova 等平台，内置路由、熔断、故障转移、多模型编排、流式输出、模型别名与全链路追踪。

> 📖 **完整文档在 [docs/](docs/00-概览.md)**：快速开始 / 功能指南 / API 参考 / 架构设计 / 运维性能
> 📖 **自动签到功能说明**：[desktop/checkin/README_CHECKIN.md](desktop/checkin/README_CHECKIN.md)

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| **渠道管理** | 上游提供商 CRUD，密钥 AES-256-GCM 加密存储，预设模板一键添加，模型自动拉取 |
| **路由组** | `quick` / `high_quality` / `vision` 三分类 + 自定义组，客户端 `model: 组名` 即命中（支持中文组名） |
| **路由策略** | `quality` 质量优先 / `fastest` 最快优先 / `stable` 最稳优先 |
| **熔断 + 故障转移** | 连续失败自动熔断（60s 半开恢复）；单次请求失败自动切换同组下一个候选，流式请求无缝切换 |
| **限流冷却** | 429 命中后指数退避（30s→60s→…→300s） |
| **多模型编排** | `relay:组A/组B` 接力（≤5 级）、`review:主/审` 审阅，支持流式最终输出 |
| **流式输出** | 单一请求与编排请求均支持 OpenAI SSE 流式 |
| **模型别名** | 用户自定义映射（`gpt-4` → 路由组），兼容现有 OpenAI 客户端 |
| **请求链路日志** | 每次请求完整记录：选路原因、实际渠道/模型、延迟、熔断/限流命中、编排阶段明细 |
| **用量统计 + 计费** | 请求数/Token 统计；free/pro/enterprise 套餐与订单管理 |
| **自动签到** | 定时签到 WorkBuddy / Trae 领取免费额度（详见 [desktop/checkin/README_CHECKIN.md](desktop/checkin/README_CHECKIN.md)） |
| **多用户** | 每用户独立渠道/路由/别名/额度，JWT + API Key 双鉴权 |

## 🚀 快速开始

```bash
# 后端
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt

# 前端
cd web-vue && npm install && npm run build && cd ..

# 启动（桌面模式推荐）
python desktop/desktop.py

# 或纯 Web 模式
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

打开 http://127.0.0.1:8765 → 注册 → 添加渠道（模板一键）→ 复制 API Key → 用任意 OpenAI 客户端调用：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="你的API密钥")

resp = client.chat.completions.create(
    model="high_quality",   # 路由组名 / 别名 / 编排格式 / 具体模型名
    messages=[{"role": "user", "content": "你好"}]
)
```

## 🧪 测试

```bash
pytest tests/ -v                 # 全部用例（124+，含性能基准）
pytest tests/test_performance.py # 性能基准
```

## 📁 文档

| 文档 | 内容 |
|------|------|
| [00-概览](docs/00-概览.md) | 项目介绍、能力矩阵、技术栈、结构 |
| [01-快速开始](docs/01-快速开始.md) | 安装 / 构建 / 运行 / 打包 / 配置 / FAQ |
| [02-功能指南](docs/02-功能指南.md) | 每个功能的使用方法 |
| [03-API参考](docs/03-API参考.md) | 全部 HTTP 端点与示例 |
| [04-架构设计](docs/04-架构设计.md) | 模块职责、请求决策链路、数据模型、扩展点 |
| [05-运维与性能](docs/05-运维与性能.md) | 日志、链路追踪、备份、性能基准、FAQ |
| [desktop/checkin/README_CHECKIN.md](desktop/checkin/README_CHECKIN.md) | WorkBuddy / TraeWork 自动签到使用说明（校准、签到、导出配置、定时任务、日志） |

## 📦 打包 exe

```bash
pip install pyinstaller
pyinstaller ai_gateway_pro.spec   # 输出 dist/智能网关.exe
```

> 桌面版白屏排障见 [01-快速开始](docs/01-快速开始.md#8-常见问题) 与 README 附录。

---

*技术栈：FastAPI · SQLAlchemy 2 · httpx · Vue 3 · Element Plus · pywebview · SQLite/MySQL*