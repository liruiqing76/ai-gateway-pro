# API 参考

本文档列出门户的全部 HTTP 端点。基础地址：`http://127.0.0.1:8765`。

## 鉴权

| 头 | 范围 | 说明 |
|----|------|------|
| `Authorization: Bearer <JWT>` | 管理 API | 登录后获取 |
| `Authorization: Bearer <API Key>` | `/v1/*` 代理 | 管理面板「API 密钥」页复制 |

---

## OpenAI 兼容代理

### `POST /v1/chat/completions`

兼容 OpenAI API 的代理端点，支持：

- 路由组名、具体模型名、模型别名、编排格式（`relay:` / `review:`）
- 流式（`stream: true`）与非流式
- 候选级故障转移

请求体：

```json
{
  "model": "high_quality",
  "messages": [{"role": "user", "content": "你好"}],
  "stream": false,
  "temperature": 0.7
}
```

`model` 可取值：

| 值 | 说明 |
|----|------|
| `quick` / `high_quality` / `vision` | 内置路由组（中文「快速/日常/识图」也可） |
| `my-group` | 用户自定义路由组 |
| `NVIDIA-deepseek-v3` | 具体模型（渠道前缀-模型名） |
| `gpt-4` | 用户自定义别名 |
| `relay:quick/high_quality` | 接力编排（≤5 级） |
| `review:high_quality/quick` | 审阅编排（恰 2 级） |

成功响应（非流式）：

```json
{
  "id": "...",
  "object": "chat.completion",
  "model": "NVIDIA · deepseek-v3",
  "choices": [{"message": {"role": "assistant", "content": "..."}}],
  "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
}
```

流式响应为 SSE 事件流，每 chunk 含 `choices[0].delta`，结束为 `data: [DONE]`。

错误响应：`503` 无可用渠道/模型，`502` 所有候选失败，`400` 参数错误，`429` 配额超限。

### `GET /v1/models`

返回当前用户可见的模型列表（路由组 + 渠道模型，均带前缀）。

```json
{
  "object": "list",
  "data": [
    {"id": "high_quality", "object": "model", "owned_by": "gateway"},
    {"id": "NVIDIA-deepseek-v3", "object": "model", "owned_by": "NVIDIA", "context_length": 131072}
  ]
}
```

---

## 管理 API

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册 `{username, email, password}` |
| POST | `/api/auth/login` | 登录，返回 JWT + API Key |
| POST | `/api/auth/refresh` | 刷新 JWT |
| POST | `/api/auth/rotate-key` | 重置 API Key |

### 渠道管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/providers` | 列出我的渠道 |
| POST | `/api/providers` | 创建渠道 `{name, base_url, api_key}` |
| PUT | `/api/providers/{id}` | 更新渠道 |
| DELETE | `/api/providers/{id}` | 删除渠道 |
| POST | `/api/providers/apply-template` | 模板一键添加 |
| GET | `/api/providers/templates` | 渠道模板列表 |
| POST | `/api/providers/{id}/fetch-models` | 重新拉取上游模型列表 |

创建渠道后自动根据模型生成内置路由组（`rebuild_user_routers`）。

### 路由组

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/routers` | 列出路由组 |
| POST | `/api/routers` | 创建/更新路由组 `{name, models[], strategy}` |
| DELETE | `/api/routers/{name}` | 删除路由组 |
| POST | `/api/routers/init-defaults` | 自动生成内置三分类 |
| POST | `/api/routers/check-availability` | 检测组内模型可用性 |

### 模型别名

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/aliases` | 列出别名（`?enabled_only=true`） |
| POST | `/api/aliases` | 创建 `{alias, target, description?}` |
| PUT | `/api/aliases/{id}` | 更新 `{target?, description?, enabled?}` |
| DELETE | `/api/aliases/{id}` | 删除 |
| POST | `/api/aliases/batch` | 批量创建 `[{alias, target, ...}]` |

### 请求链路

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/traces` | 查询链路日志（`?limit=&offset=&model=&success=&pipeline_only=`） |
| GET | `/api/traces/stats` | 统计概览（`?days=7`）：成功率、均延迟、熔断/限流命中、Top 模型 |
| DELETE | `/api/traces/cleanup` | 清理旧日志（`?days=30`，保留最近 N 天） |

### 用量统计

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/usage` | 用量统计（今日/历史/按模型汇总） |
| GET | `/api/usage/records` | 用量明细（分页） |

### 计费中心

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/billing/plans` | 套餐列表 |
| POST | `/api/billing/orders` | 创建订单 |
| POST | `/api/billing/orders/{id}/pay` | 支付/标记已付 |
| GET | `/api/billing/orders` | 我的订单 |
| POST | `/api/billing/admin/confirm` | 管理员确认订单（升级套餐） |

### 系统

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 `{status, version}` |
| GET | `/` | 前端页面（Web 模式） |

---

## 编排格式详细规则

### 优先级

请求进入后按以下顺序解析 `model`：

1. **编排语法**：`relay:...` / `review:...` → 多模型编排
2. **模型别名**：命中用户别名映射 → 替换为目标（路由组或具体模型）
3. **路由组**：`normalize_group_name` 后命中组名 → 组内路由
4. **具体模型**：精确匹配或带渠道前缀匹配

### relay 阶段构造

| 阶段 | messages 构造 |
|------|--------------|
| 第 0 级 | 保留 system，追加 `user: 原始prompt` |
| 第 i 级 | 保留 system，追加 `user: 原始prompt + [上一级输出] + 请继续处理`，并注入接力 system 消息 |

### review 阶段构造

| 阶段 | messages 构造 |
|------|--------------|
| 主模型 | 原始请求体透传 |
| 审阅模型 | 保留 system，追加 `user: 原始prompt + [初稿] + 请审阅并输出修订版`，注入审阅 system 消息 |

审阅失败回退返回初稿；relay 某级失败跳过该级继续下一级。