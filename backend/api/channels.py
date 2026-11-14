# ============================================================
# 智能网关 — 白嫖渠道适配 API（专门的渠道, 非免费模型自动池）
#
# 设计目标(用户原话): "专门的渠道适配, 这样我可以在其他工具里选择
# 真正的模型使用" —— 渠道登录后成为正式 Provider:
#   /v1/models 出现 codearts-glm-5.3-flash / workbuddy-kimi-k3 等真实模型名,
#   Claude Code 等工具直接按名调用, 路由组/别名/熔断全复用。
#
# 端点:
#   GET  /api/channels                 渠道目录(支持哪些/登录状态)
#   POST /api/channels/{channel}/login 发起登录(返回浏览器要开的 URL)
#   GET  /api/channels/login/{login_id} 轮询登录结果(完成时建 Provider+拉模型)
#   GET  /channel-callback/codearts/{login_id}  华为云 OAuth 回调(授权页重定向来)
#   POST /api/channels/checkin         一键签到(全部支持渠道)
#   POST /api/channels/checkin/{channel} 单渠道签到
#   GET  /api/channels/checkin/status  签到状态(今日是否已领)
#
# 登录编排:
#   - WorkBuddy: state 轮询制(无回调), 后端替前端轮询上游 token 接口
#   - CodeArts: OAuth code 流, 回调挂在网关自身端口(port 参数传给授权页,
#     华为云登录完重定向 http://127.0.0.1:{网关端口}/channel-callback/codearts/{login_id})
# ============================================================

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx
import uvicorn
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from backend.auth.middleware import get_current_user_from_jwt
from backend.auth.security import encrypt_secret
from backend.config import settings
from backend.models import get_session, User, Provider, Router
from backend.core.channel_auth import (
    checkin_channels, channel_checkin,
)
from backend.core.channel_auth import (
    cline, codearts, workbuddy,
    trae as trae_mod, qoder as qoder_mod,
    minimax as minimax_mod,
    lobsterai as lobsterai_mod,
)
from backend.core import upstream_http
from backend.core.channel_import import sync_model_rates

logger = logging.getLogger("gateway.channels")
router = APIRouter(prefix="/api/channels", tags=["channels"])
UTC8 = timezone(timedelta(hours=8))

# pending 登录会话: login_id → {user_id, channel, pkce?, keypair?, state?,
#   started_at, status: pending|done|failed, result?}
_PENDING_LOGINS: dict[str, dict] = {}

LOGIN_SESSION_TTL_SECONDS = 600
POLL_UPSTREAM_INTERVAL = 1.0


def _prune_pending() -> None:
    now = time.time()
    expired = [k for k, v in _PENDING_LOGINS.items()
               if (v.get("status") == "pending"
                   and now - v["started_at"] > LOGIN_SESSION_TTL_SECONDS)
               or (v.get("status") == "done"
                   and now > v.get("expire_at", v["started_at"] + 600))]
    for k in expired:
        if _PENDING_LOGINS[k].get("status") == "pending":
            _PENDING_LOGINS[k]["status"] = "failed"
            _PENDING_LOGINS[k]["error"] = "登录会话超时"
        del _PENDING_LOGINS[k]


# ============================================================
# 渠道目录
# ============================================================

_CHANNEL_CATALOG = {
    "codearts": {
        "display_name": "CodeArts (华为云)",
        "description": "华为云 CodeArts Snap 免费额度; 登录换 AK/SK, "
                       "推理走华为云 HMAC 签名; 含每日积分签到(HTTP)",
        "login_flow": "oauth",
        "models_note": "GLM-5.2 / openpangu-2.0 盘古系列 / glm-5.3-flash 等 "
                       "完整目录(benefit 免费档带 maas_type 调用)",
        "has_checkin": True,
    },
    "workbuddy": {
        "display_name": "WorkBuddy (国际版)",
        "description": "腾讯 WorkBuddy 国际版免费模型池; 浏览器登录一次, "
                       "token 可刷新; 标准推理协议; 含每日签到(HTTP billing)",
        "login_flow": "state-poll",
        "models_note": "kimi-k3 / glm-5.3 / deepseek-v4.1-flash / gpt-5.6 系等",
        "has_checkin": True,
    },
    "buddy": {
        "display_name": "CodeBuddy (腾讯国内版)",
        "description": "腾讯 CodeBuddy 国内版; 与 WorkBuddy 同协议; 含每日签到(HTTP billing)",
        "login_flow": "state-poll",
        "models_note": "deepseek-v4-flash / glm-5.3 / kimi-k3 等",
        "has_checkin": True,
    },
    "cline": {
        "display_name": "Cline",
        "description": "Cline 官方免费模型(workos 设备码登录)",
        "login_flow": "device-code",
        "models_note": "cline-free/ 前缀 3 个免费模型",
        "has_checkin": False,
    },
    "trae": {
        "display_name": "TRAE (字节)",
        "description": "字节 TRAE SOLO 免费模型池; 浏览器登录一次, "
                       "自定义 SOLO 协议(网关自动转换); 含每日积分签到(HTTP)",
        "login_flow": "oauth",
        "models_note": "glm-5.2 / kimi-k3 / deepseek-v4 / doubao 系等",
        "has_checkin": True,
    },
    "qodercn": {
        "display_name": "Qoder (中国版)",
        "description": "阿里 Qoder 免费模型; 设备码登录, 加密端点(本机 node wasm 签名); "
                       "含每日 100 Credits 领取(HTTP)",
        "login_flow": "device-code",
        "models_note": "qfmodel(Qwen3.8-Flash 免费) / glm-5.3 / kimi / deepseek 等",
        "has_checkin": True,
    },
    "qoder": {
        "display_name": "Qoder (国际版)",
        "description": "阿里 Qoder 国际版; 需可达 api2.qoder.sh; 同中国版协议",
        "login_flow": "device-code",
        "models_note": "qfmodel / ultimate / sonus / kimi 等",
        "has_checkin": True,
    },
    "minimax": {
        "display_name": "MiniMax Code",
        "description": "MiniMax 国内版免费额度; 设备码登录(PKCE), 推理走 Anthropic "
                       "Messages 协议(网关自动转换); 含每日签到(HTTP 积分)",
        "login_flow": "device-code",
        "models_note": "MiniMax-M3.1-Flash(订阅限免) / M3 / M2.7 系",
        "has_checkin": True,
    },
    "lobsterai": {
        "display_name": "LobsterAI (有道)",
        "description": "有道 LobsterAI 免费模型池; 浏览器登录一次(本地回调换 token), "
                       "标准推理协议; 含每日活动签到(HTTP 积分)",
        "login_flow": "oauth",
        "models_note": "deepseek-v4-flash / glm-5.2 / kimi-k2.7-code / qwen3.7-max 等",
        "has_checkin": True,
    },
    "opencode": {
        "display_name": "OpenCode Zen 免费车道",
        "description": "免密免费池(v2 指纹门: /inference/v1 + 官方头 + 逐字工具定义); "
                       "由自动同步渠道维护, 无需登录",
        "login_flow": "auto-sync",
        "models_note": "*-free 模型(2026-10 车道被上游收紧期间可能 403/429, 状态见渠道卡)",
        "has_checkin": False,
        "read_only": True,
    },
    "kilo": {
        "display_name": "Kilo 免费池",
        "description": "Kilo 公共网关免认证免费池; 由自动同步渠道维护, 无需登录",
        "login_flow": "auto-sync",
        "models_note": "kilo-auto/free / nemotron / deepseek 系 17 个免费模型",
        "has_checkin": False,
        "read_only": True,
    },
}


@router.get("")
async def list_channels(user: User = Depends(get_current_user_from_jwt)):
    """渠道目录 + 当前用户的登录状态(已登录 = 存在该渠道 Provider)。"""
    session = get_session()
    try:
        providers = session.query(Provider).filter_by(
            user_id=user.id, enabled=True).all()
        # 渠道路由组自愈: 上游模型清单变了(新增/下线), 组内条目跟着对齐,
        # 保证"客户端填组名 = 该渠道全部模型"始终成立。
        try:
            if _reconcile_channel_groups(session, user.id, providers):
                session.commit()
        except Exception:
            session.rollback()
            logger.warning("渠道路由组自愈失败", exc_info=True)
        by_name = {p.name: p for p in providers}
        # 免密车道状态(opencode/kilo): 同步循环记录的 lane_state
        lane_states = {}
        lane_resume = {}
        try:
            from backend.core.zen_sources import runtime_state as zen_state
            from backend.core.kilo_sources import runtime_state as kilo_state
            from backend.core import lane_state as lane_store
            zr = (zen_state() or {}).get("last_result") or {}
            kr = (kilo_state() or {}).get("last_result") or {}
            if zr.get("lane_state"):
                lane_states["opencode"] = zr["lane_state"]
            if kr.get("lane_state"):
                lane_states["kilo"] = kr["lane_state"]
            elif kr.get("usable") is not None:
                # 旧同步结果(无 lane_state 字段)兼容推导
                lane_states["kilo"] = "open" if kr.get("usable") else "closed"
            # 内存态缺失(刚重启, 同步循环还没跑) → 用上次落盘的结论
            for ch_id in ("opencode", "kilo"):
                if ch_id in lane_states:
                    continue
                saved = (lane_store.get(ch_id) or {}).get("state")
                if saved:
                    lane_states[ch_id] = saved
            # 额度恢复提示(上游 429 带 retry-after): {channel: "10-25 08:00"}
            for ch_id in ("opencode", "kilo"):
                entry = lane_store.get(ch_id) or {}
                if entry.get("resume_at"):
                    lane_resume[ch_id] = {
                        "at": entry.get("resume_at"),
                        "reason": entry.get("reason") or "",
                    }
        except Exception:
            pass
        out = []
        for channel_id, meta in _CHANNEL_CATALOG.items():
            p = by_name.get(channel_id)
            out.append({
                "id": channel_id,
                **meta,
                "connected": p is not None,
                "models": (p.models or []) if p else [],
                "model_count": len(p.models or []) if p else 0,
                **({"lane_state": lane_states.get(channel_id)}
                   if channel_id in lane_states else {}),
                **({"lane_resume": lane_resume[channel_id]}
                   if channel_id in lane_resume else {}),
            })
        return {"channels": out, "checkin_channels": checkin_channels()}
    finally:
        session.close()


# ============================================================
# 登录: WorkBuddy / CodeBuddy (state 轮询制)
# ============================================================

@router.post("/workbuddy/login")
async def workbuddy_login(user: User = Depends(get_current_user_from_jwt),
                          channel: str = "workbuddy"):
    """发起 WorkBuddy/CodeBuddy 登录: 返回 auth_url 给前端 window.open。"""
    _prune_pending()
    product = workbuddy.product_for(channel)
    async with upstream_http.make_async(timeout=60) as client:
        state = await workbuddy.start_auth_state(client, product)
    login_id = secrets.token_hex(16)
    _PENDING_LOGINS[login_id] = {
        "user_id": user.id, "channel": channel,
        "state": state["state"],
        "started_at": time.time(), "status": "pending",
    }
    return {"login_id": login_id, "auth_url": state["auth_url"]}


@router.post("/buddy/login")
async def buddy_login(user: User = Depends(get_current_user_from_jwt)):
    """CodeBuddy 国内版登录(同 WorkBuddy 协议)。"""
    return await workbuddy_login(user, channel="buddy")


async def _poll_workbuddy_token(login_id: str) -> None:
    """后台替前端轮询上游 token 接口, 拿到即建 Provider。"""
    session_info = _PENDING_LOGINS.get(login_id)
    if not session_info:
        return
    channel = session_info["channel"]
    product = workbuddy.product_for(channel)
    async with upstream_http.make_async(timeout=60) as client:
        token = await workbuddy.poll_token(client, session_info["state"], product)
    if token is None:
        session_info["status"] = "failed"
        session_info["error"] = "登录超时(5 分钟未完成授权)"
        return
    session_info["token"] = token
    # 拉账号信息(uid/企业 id 供签到头 X-User-Id 用) + 模型清单
    async with upstream_http.make_async(timeout=60) as client:
        session_info["account"] = await workbuddy.fetch_account(client, token, channel)
        models = await workbuddy.fetch_models(client, token, channel)
    session_info["models"] = models
    session_info["status"] = "done"


def _upsert_channel_provider(user_id: int, channel: str, base_url: str,
                             api_key_plain: str, models: list) -> int | None:
    """登录成功后写/更新 Provider 行(专门的渠道, 真实模型清单)。"""
    session = get_session()
    try:
        p = session.query(Provider).filter_by(
            user_id=user_id, name=channel).first()
        model_ids = [m["id"] if isinstance(m, dict) else str(m) for m in models]
        if p is None:
            p = Provider(user_id=user_id, name=channel,
                         base_url=base_url,
                         api_key_encrypted=encrypt_secret(api_key_plain),
                         models=model_ids,
                         disabled_models=[], free_only=True, enabled=True)
            session.add(p)
        else:
            p.base_url = base_url
            p.api_key_encrypted = encrypt_secret(api_key_plain)
            if model_ids:
                p.models = model_ids
            p.enabled = True
        session.commit()
        session.refresh(p)
        provider_id = p.id
    finally:
        session.close()
    # 每渠道独立路由组: 客户端填组名即命中该渠道全部模型(如 "trae"/"qodercn")
    _ensure_channel_router(user_id, channel, model_ids)
    # 上游目录带的计费倍率(workbuddy credits / lobsterai costMultiplier)自动入库
    sync_model_rates(user_id, channel, models)
    return provider_id


def _ensure_channel_router(user_id: int, channel: str, model_ids: list) -> None:
    """建/刷新 "<渠道>-模型名" 前缀的渠道专属路由组(failover 策略)。

    与 zen/kilo 自动组同构: 组名=渠道名, 内容随模型清单刷新; 用户也可
    自己在路由组页改名/合并。
    """
    prefixed = [f"{channel}-{m}" for m in model_ids]
    session = get_session()
    try:
        r = session.query(Router).filter_by(
            user_id=user_id, name=channel).first()
        if r is None:
            r = Router(user_id=user_id, name=channel, models=prefixed,
                       strategy="failover")
            session.add(r)
        else:
            r.models = prefixed
        session.commit()
    except Exception:
        session.rollback()
        logger.warning("渠道 %s 路由组写入失败", channel, exc_info=True)
    finally:
        session.close()


def _merge_group_models(channel: str, existing, model_ids) -> list:
    """把渠道当前模型清单合进已有的组成员列表。

    - 本渠道前缀的旧条目: 按当前清单重建(上游下线的模型自动剔除)
    - 其它条目(用户手动往组里加的东西): 原样保留, 不被覆盖
    """
    prefix = f"{channel}-"
    wanted = [f"{channel}-{m}" for m in model_ids]
    keep = [m for m in (existing or []) if not str(m).startswith(prefix)]
    merged = list(wanted)
    for m in keep:
        if m not in merged:
            merged.append(m)
    return merged


def _reconcile_channel_groups(session, user_id: int, providers) -> int:
    """让每个渠道路由组与渠道 Provider 的模型清单保持一致(自愈)。

    路由组原本只在登录成功那一刻建一次; 之后上游新增/下线模型(例如
    WorkBuddy 后来多出 gpt-6-luna)组里就不同步了 —— 客户端按组名调用会
    漏掉模型。这里做增量对齐, 返回实际改动过的组数(便于只在需要时 commit)。
    """
    changed = 0
    rows = {r.name: r for r in session.query(Router).filter_by(
        user_id=user_id).all()}
    for p in providers:
        name = p.name
        if name not in _CHANNEL_CATALOG:
            continue
        model_ids = list(p.models or [])
        if not model_ids:
            continue
        r = rows.get(name)
        if r is None:
            session.add(Router(user_id=user_id, name=name,
                               models=[f"{name}-{m}" for m in model_ids],
                               strategy="failover"))
            changed += 1
            continue
        merged = _merge_group_models(name, r.models, model_ids)
        if merged != list(r.models or []):
            r.models = merged
            changed += 1
    return changed


_CHANNEL_BASE_URLS = {
    "codearts": codearts.CHAT_API_BASE,
    "workbuddy": workbuddy.WORKBUDDY["endpoint"] + "/v2",
    "buddy": workbuddy.CODEBUDDY["endpoint"] + "/v2",
    "cline": cline.CLINE_API_BASE + "/api/v1",
    "trae": "https://trae-api-cn.mchost.guru/api/agent/v3",
    "qoder": "https://api2.qoder.sh/algo",
    "qodercn": "https://gateway.qoder.com.cn/algo",
    "minimax": minimax_mod.API_HOST + "/mavis/api/v1/llm/v1",
    "lobsterai": lobsterai_mod.API_BASE + "/api/proxy/v1",
}


# ============================================================
# 登录: TRAE (浏览器授权 → 本地 :18080 回调 → ExchangeToken)
# ============================================================

@router.post("/trae/login")
async def trae_login(user: User = Depends(get_current_user_from_jwt)):
    """发起 TRAE 登录: 生成 machine/device id + 授权页 URL, 起临时回调服。"""
    _prune_pending()
    machine_id = trae_mod.generate_machine_id()
    device_id = trae_mod.generate_machine_id()   # 同款 32hex
    login_id = secrets.token_hex(16)
    port = trae_mod.CALLBACK_PORT
    callback_url = f"http://127.0.0.1:{port}/authorize"
    auth_url = trae_mod.build_login_url(machine_id, device_id, callback_url)
    _PENDING_LOGINS[login_id] = {
        "user_id": user.id, "channel": "trae",
        "machine_id": machine_id, "device_id": device_id,
        "started_at": time.time(), "status": "pending",
    }
    app, state = _build_trae_callback_app(login_id)
    asyncio.create_task(_serve_transient_login_server(
        "trae", login_id, app, state, port,
        timeout=trae_mod.LOGIN_TIMEOUT_SECONDS,
        fallback_port_0=True))
    return {"login_id": login_id, "auth_url": auth_url}


def _build_trae_callback_app(login_id: str):
    """TRAE 回调 ASGI app(挂 127.0.0.1:18080): /authorize 收 refreshToken。"""
    from starlette.responses import Response as StarResponse

    state = {"done": asyncio.Event(), "outcome": None}

    async def app(scope, receive, send):
        from starlette.requests import Request as StarRequest
        request = StarRequest(scope, receive)
        path = request.url.path
        if not path.startswith("/authorize"):
            await StarResponse("Not found", status_code=404)(scope, receive, send)
            return
        params = dict(request.query_params)
        session_info = _PENDING_LOGINS.get(login_id) or {}

        async def _run():
            try:
                info = trae_mod.parse_callback_params(params)
                if info is None:
                    raise RuntimeError("回调未携带 refreshToken / userJwt.Token / code")
                creds = await trae_mod.exchange_token(
                    upstream_http.make_async(timeout=60), info,
                    session_info.get("machine_id", ""),
                    session_info.get("device_id", ""))
                async with upstream_http.make_async(timeout=60) as client:
                    models = await trae_mod.fetch_models(client, creds)
                session_info["creds"] = creds
                session_info["models"] = models
                session_info["status"] = "done"
                state["outcome"] = "done"
            except Exception as e:
                session_info["status"] = "failed"
                session_info["error"] = str(e)
                state["outcome"] = f"failed: {e}"
                logger.warning("trae login failed: %s", e)
            finally:
                state["done"].set()

        # 先响应浏览器(200 文案), 再后台处理
        html = ("<html><body style='font-family:sans-serif;padding:40px'>"
                "<h3>TRAE 登录回跳已接收</h3><p>可以关闭此页面, 回到网关等待绑定完成。</p>"
                "</body></html>")
        # 处理完成后才回包(让页面停留反映真实结果)
        await _run()
        body = ("<html><body style='font-family:sans-serif;padding:40px'><h3>"
                + ("TRAE 登录完成, 请返回网关" if state["outcome"] == "done"
                   else f"TRAE 登录失败: {state['outcome']}")
                + "</h3></body></html>")
        await StarResponse(body, status_code=200,
                           media_type="text/html; charset=utf-8")(scope, receive, send)

    return app, state


async def _serve_transient_login_server(channel, login_id, app, state,
                                        preferred_port, timeout=600,
                                        fallback_port_0=False):
    """通用临时登录回调服(uvicorn, 端口占用可选回退随机)。"""
    session_info = _PENDING_LOGINS.get(login_id)
    if not session_info:
        return
    config = uvicorn.Config(app, host="127.0.0.1", port=preferred_port,
                            log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.wait_for(state["done"].wait(), timeout=timeout)
    except asyncio.TimeoutError:
        session_info["status"] = "failed"
        session_info["error"] = "登录超时, 未完成授权"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            task.cancel()


# ============================================================
# 登录: LobsterAI (有道) — 浏览器授权 → 本地回调 → /api/auth/exchange
# ============================================================

@router.post("/lobsterai/login")
async def lobsterai_login(user: User = Depends(get_current_user_from_jwt)):
    """发起 LobsterAI 登录: 授权页 URL + 临时回调服(127.0.0.1:18731/auth/callback)。"""
    _prune_pending()
    session = lobsterai_mod.new_login_session()
    login_id = secrets.token_hex(16)
    port = lobsterai_mod.CALLBACK_PORT
    auth_url = lobsterai_mod.build_login_url(port, session)
    _PENDING_LOGINS[login_id] = {
        "user_id": user.id, "channel": "lobsterai",
        "session": session,
        "started_at": time.time(), "status": "pending",
    }
    app, state = _build_lobsterai_callback_app(login_id)
    asyncio.create_task(_serve_transient_login_server(
        "lobsterai", login_id, app, state, port,
        timeout=lobsterai_mod.LOGIN_TIMEOUT_SECONDS,
        fallback_port_0=True))
    return {"login_id": login_id, "auth_url": auth_url}


def _build_lobsterai_callback_app(login_id: str):
    """LobsterAI 回调 ASGI app: /auth/callback 收 code → exchange 换 token。"""
    from starlette.responses import Response as StarResponse

    state = {"done": asyncio.Event(), "outcome": None}

    async def app(scope, receive, send):
        from starlette.requests import Request as StarRequest
        request = StarRequest(scope, receive)
        if not request.url.path.startswith(lobsterai_mod.CALLBACK_PATH):
            await StarResponse("Not found", status_code=404)(scope, receive, send)
            return
        params = dict(request.query_params)
        session_info = _PENDING_LOGINS.get(login_id) or {}
        try:
            info = lobsterai_mod.parse_callback_params(params)
            if info is None:
                raise RuntimeError("回调未携带授权码 code")
            expected = (session_info.get("session") or {}).get("state", "")
            if expected and info.get("state") and info["state"] != expected:
                raise RuntimeError("回调 state 与会话不匹配")
            async with upstream_http.make_async(timeout=60) as client:
                creds = await lobsterai_mod.exchange_code(
                    client, info["code"], session_info.get("session") or {})
                models = await lobsterai_mod.fetch_models(client, creds)
            session_info["creds"] = creds
            session_info["models"] = models
            session_info["status"] = "done"
            state["outcome"] = "done"
        except Exception as e:
            session_info["status"] = "failed"
            session_info["error"] = str(e)
            state["outcome"] = f"failed: {e}"
            logger.warning("lobsterai login failed: %s", e)
        finally:
            state["done"].set()
        body = ("<html><body style='font-family:sans-serif;padding:40px'><h3>"
                + ("LobsterAI 登录完成, 请返回网关" if state["outcome"] == "done"
                   else f"LobsterAI 登录失败: {state['outcome']}")
                + "</h3></body></html>")
        await StarResponse(body, status_code=200,
                           media_type="text/html; charset=utf-8")(scope, receive, send)

    return app, state


# ============================================================
# 登录: Cline (WorkOS 设备码: 浏览器确认 → 后端轮询 → register 兑换)
# ============================================================

@router.post("/cline/login")
async def cline_login(user: User = Depends(get_current_user_from_jwt)):
    """发起 Cline 登录: 设备码全程纯 HTTP, 无本地回调 server。"""
    _prune_pending()
    async with upstream_http.make_async(timeout=60) as client:
        dev = await cline.start_device_login(client)
    login_id = secrets.token_hex(16)
    _PENDING_LOGINS[login_id] = {
        "user_id": user.id, "channel": "cline",
        "device_code": dev.get("device_code", ""),
        "interval": int(dev.get("interval") or 5),
        "started_at": time.time(), "status": "pending",
    }
    asyncio.create_task(_poll_cline_token(login_id))
    return {"login_id": login_id,
            "auth_url": dev.get("verification_uri_complete")
            or dev.get("verification_uri", "")}


async def _poll_cline_token(login_id: str) -> None:
    info = _PENDING_LOGINS.get(login_id)
    if not info:
        return
    import asyncio as aio
    async with upstream_http.make_async(timeout=60) as client:
        token = await cline.poll_device_token(
            client, info["device_code"], interval=info.get("interval", 5))
        if token is None:
            info["status"] = "failed"
            info["error"] = "登录超时(10 分钟未完成授权)"
            return
        try:
            cline_token = await cline.register_cline_token(client, token)
        except httpx.HTTPError as e:
            info["status"] = "failed"
            info["error"] = f"Cline token 兑换失败: {e}"
            return
        creds_pkg = cline.credentials_package(cline_token)
        import json as _json
        creds = _json.loads(creds_pkg)
        info["creds"] = creds
        models = await cline.fetch_free_models(client, creds["access_token"])
    info["models"] = [{"id": m, "name": m} for m in models]
    info["status"] = "done"


# ============================================================
# 登录: Qoder (设备码: 浏览器授权页 → 后端轮询 deviceToken/poll)
# ============================================================

@router.post("/qodercn/login")
async def qodercn_login(user: User = Depends(get_current_user_from_jwt)):
    return await _qoder_login(user, "qodercn")


@router.post("/qoder/login")
async def qoder_login(user: User = Depends(get_current_user_from_jwt)):
    return await _qoder_login(user, "qoder")


async def _qoder_login(user, channel: str):
    _prune_pending()
    product = qoder_mod.product_for(channel)
    session = qoder_mod.create_device_session()
    login_id = secrets.token_hex(16)
    auth_url = qoder_mod.build_auth_url(session, product)
    _PENDING_LOGINS[login_id] = {
        "user_id": user.id, "channel": channel,
        "qoder_session": session, "product": product,
        "started_at": time.time(), "status": "pending",
    }
    asyncio.create_task(_poll_qoder_token(login_id))
    return {"login_id": login_id, "auth_url": auth_url}


async def _poll_qoder_token(login_id: str) -> None:
    info = _PENDING_LOGINS.get(login_id)
    if not info:
        return
    channel = info["channel"]
    product = info["product"]
    async with upstream_http.make_async(timeout=60) as client:
        token = await qoder_mod.poll_device_token(
            client, info["qoder_session"], product,
            timeout=qoder_mod.LOGIN_TIMEOUT_SECONDS)
    if token is None:
        info["status"] = "failed"
        info["error"] = "登录超时(5 分钟未完成授权)"
        return
    creds = qoder_mod.build_credential(
        token, info["qoder_session"]["machine_id"], channel=channel)
    if not creds.get("uid"):
        # poll 响应常不带 uid → /userinfo 补(插件同款 ensureUid)
        async with upstream_http.make_async(timeout=60) as client:
            ui = await qoder_mod.fetch_userinfo(client, creds["access_token"], product)
        uid = ui.get("user_id") or ui.get("userId") or ""
        if uid:
            creds["uid"] = str(uid)
        name = ui.get("nickname") or ui.get("user_name") or ""
        if name:
            creds["nickname"] = str(name)
    info["creds"] = creds
    # 全量模型(带积分倍率, 倍率自动入 model_rates): 之前只收 isFree 的 2 个,
    # 实际 qoder 账号全部模型都能调, 按倍率扣积分。
    info["models"] = qoder_mod.models(channel)
    info["status"] = "done"


# ============================================================
# 登录: MiniMax Code (设备码 + PKCE S256)
#
# 照抄 pack.js minimax.ts:
#   POST {account}/oauth2/device/code (form) → device_code + user_code + 验证 URL
#   用户浏览器开 verification_uri_complete 输 user_code
#   POST {account}/oauth2/token grant_type=device_code 轮询换 token
# ⚠️ refresh_token 一次性轮换, 登录/续期后都必须立刻回写 Provider。
# ============================================================

@router.post("/minimax/login")
async def minimax_login(user: User = Depends(get_current_user_from_jwt)):
    """发起 MiniMax 设备码登录: 返回 user_code + 验证 URL 给前端展示。"""
    _prune_pending()
    async with upstream_http.make_async(timeout=40) as client:
        auth = await minimax_mod.start_device_login(client)
    if auth is None:
        return JSONResponse(
            {"error": "MiniMax 设备码申请失败, 请稍后重试"}, status_code=502)
    login_id = secrets.token_hex(16)
    _PENDING_LOGINS[login_id] = {
        "user_id": user.id, "channel": "minimax",
        "minimax_auth": auth,
        "started_at": time.time(), "status": "pending",
    }
    asyncio.create_task(_poll_minimax_token(login_id))
    return {
        "login_id": login_id,
        "user_code": auth["user_code"],
        "auth_url": auth["verification_uri_complete"],
        "verification_uri": auth["verification_uri"],
        "expires_in": auth["expires_in"],
        "interval": auth["interval"],
    }


async def _poll_minimax_token(login_id: str) -> None:
    info = _PENDING_LOGINS.get(login_id)
    if not info:
        return
    auth = info["minimax_auth"]
    try:
        async with upstream_http.make_async(timeout=60) as client:
            grant = await minimax_mod.poll_device_token(
                client, auth, timeout=float(auth.get("expires_in") or 300))
    except Exception as e:
        info["status"] = "failed"
        info["error"] = f"轮询失败: {e}"[:200]
        return
    if grant is None:
        info["status"] = "failed"
        info["error"] = "登录超时或授权被拒绝"
        return
    info["creds"] = grant
    try:
        async with upstream_http.make_async(timeout=60) as client:
            info["models"] = await minimax_mod.fetch_models(client, grant)
    except Exception:
        info["models"] = list(minimax_mod.FALLBACK_MODELS)
    info["status"] = "done"


# ============================================================
# 登录: CodeArts (OAuth, 插件同款: 固定端口≥10000 的本地回调 server)
#
# 照抄 pack.js login.ts:
#   - startOAuthCallbackServer: 临时 server bind 127.0.0.1, 端口必须 ≥10000
#     (重听直到拿到合格端口), REDIRECT_PATH="/oauth/callback"
#   - 回调 "?code="  → PKCE+DPoP 换 STS token
#   - 回调 "?secret=" → ticket 轮询 /snap-manager/v1/login/ticket (老流)
#   - 两条路 307 → portal login result URL(login_succeed=true/false)
#   - 超时 180s
# ============================================================

_CODEARTS_SERVER_TIMEOUT_SECONDS = 180


async def _pick_callback_port() -> int:
    """挑一个 ≥10000 的可用回环端口(照抄 listenOnCallbackPort 的重听逻辑)。"""
    import socket
    for _ in range(50):
        port = secrets.randbelow(65536 - codearts.MIN_CALLBACK_PORT) + codearts.MIN_CALLBACK_PORT
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用的 ≥10000 回调端口")


async def _complete_codearts_login(session_info: dict, creds: dict) -> None:
    """拿到 AK/SK 凭据后的统一收尾: 拉模型 + 会话置 done。

    fetch_models 内部已带完整目录兜底(FALLBACK_MODELS, 含盘古/GLM-5.2),
    拉取失败/异常都不至于让渠道空清单。"""
    try:
        async with upstream_http.make_async(timeout=60) as client:
            models = await codearts.fetch_models(client, creds)
    except Exception as e:
        models = codearts._benefit_fallback_models()
        logger.warning("codearts 模型拉取失败, 用兜底表: %s", e)
    session_info["creds"] = creds
    session_info["models"] = models
    session_info["status"] = "done"


async def _handle_codearts_code(session_info: dict, code: str, port: int) -> None:
    """code 路: PKCE+DPoP → STS(照抄 exchangeAuthorizationCode 调用位)。"""
    async with upstream_http.make_async(timeout=60) as client:
        creds = await codearts.exchange_authorization_code(
            client, code, session_info["pkce"]["code_verifier"],
            port, session_info["keypair"])
    await _complete_codearts_login(session_info, creds)


async def _handle_codearts_secret(session_info: dict, secret: str) -> None:
    """secret 路: ticket 轮询领 AK/SK(照抄 pollForCredential + parseCredentialResponse)。

    授权页里 ticket_id 参数就是我们下发的 login_id, 轮询时原样带回。
    """
    async with upstream_http.make_async(timeout=60) as client:
        creds = await codearts.poll_for_credential(
            client, secret, ticket_id=session_info.get("ticket_id", ""))
    await _complete_codearts_login(session_info, creds)


def _build_oauth_callback_app(login_id: str) -> "tuple":
    """构造插件同款的临时回调 ASGI app。

    返回 (asgi_app, state): state dict 收 code/secret 并触发后台处理任务。
    """
    from starlette.responses import Response as StarResponse

    state = {"done": asyncio.Event(), "outcome": None}

    async def app(scope, receive, send):
        assert scope["type"] == "http"
        from starlette.requests import Request as StarRequest
        import urllib.parse
        request = StarRequest(scope, receive)
        path = request.url.path
        if path != codearts.REDIRECT_PATH and not path.startswith(codearts.REDIRECT_PATH):
            StarResponse("Not found", status_code=404).scope = scope
            await StarResponse("Not found", status_code=404)(scope, receive, send)
            return
        if request.method == "OPTIONS":
            await StarResponse("", status_code=204,
                               headers=codearts.CORS_HEADERS)(scope, receive, send)
            return
        params = request.query_params
        # 与插件一致: token/access_token/accessToken/authCode 任一
        direct = (params.get("token") or params.get("access_token")
                  or params.get("accessToken") or params.get("authCode") or "")
        code = params.get("code") or ""
        secret = params.get("secret") or ""
        fingerprint = params.get("fingerprint") or ""
        # fingerprint 老路: base64 里可能藏 token(照抄 pickToken+decode 分支)
        if not direct and fingerprint:
            try:
                import base64
                decoded = base64.b64decode(fingerprint).decode("utf-8", "replace")
                qs = urllib.parse.urlparse(decoded).query
                fp_params = dict(urllib.parse.parse_qsl(qs))
                direct = (fp_params.get("token") or fp_params.get("access_token")
                          or fp_params.get("accessToken") or fp_params.get("authCode") or "")
            except Exception:
                direct = ""

        session_info = _PENDING_LOGINS.get(login_id) or {}
        portal_ok = codearts.build_portal_login_result_url(True)
        portal_fail = codearts.build_portal_login_result_url(False)

        async def _fire(coro_factory):
            try:
                await coro_factory()
                state["outcome"] = "done"
            except Exception as e:
                state["outcome"] = f"failed: {e}"
                logger.warning("codearts callback failed: %s", e)
            finally:
                state["done"].set()

        if code:
            port = scope.get("server", (None, None))[1] or 0
            await _fire(lambda: _handle_codearts_code(session_info, code, port))
            location = portal_ok if state["outcome"] == "done" else portal_fail
        elif secret:
            await _fire(lambda: _handle_codearts_secret(session_info, secret))
            location = portal_ok if state["outcome"] == "done" else portal_fail
        elif direct:
            # 直发 token 的形态(插件直接接受, 记进会话)
            session_info["direct_token"] = direct
            state["outcome"] = "done"
            state["done"].set()
            location = portal_ok
        else:
            session_info["status"] = "failed"
            session_info["error"] = "回调缺少 code/secret"
            state["outcome"] = "failed"
            state["done"].set()
            location = portal_fail

        await StarResponse("", status_code=307,
                           headers={**codearts.CORS_HEADERS, "Location": location}
                           )(scope, receive, send)

    return app, state


@router.post("/codearts/login")
async def codearts_login(user: User = Depends(get_current_user_from_jwt)):
    """发起 CodeArts 登录: 起临时回调 server + 返回授权页 URL。

    授权页 URL 与插件 buildOAuthLoginUrl 逐字符对齐(port=临时端口,
    ticket_id=login_id, PKCE S256, plugin-name=snap_AIIDE)。
    """
    _prune_pending()
    port = await _pick_callback_port()
    pkce = codearts.generate_pkce_pair()
    keypair = codearts.generate_dpop_keypair()
    login_id = secrets.token_hex(16)
    authorize_url = codearts.build_oauth_login_url(port, pkce, login_id)
    _PENDING_LOGINS[login_id] = {
        "user_id": user.id, "channel": "codearts",
        "pkce": pkce, "keypair": keypair,
        "callback_port": port,
        "ticket_id": login_id,  # 授权页 ticket_id 参数(= login_id), secret 流轮询原样带回
        "started_at": time.time(), "status": "pending",
    }
    # 后台任务: uvicorn 起临时回调 server, 等结果/超时, 自动关服
    asyncio.create_task(_serve_codearts_callback(login_id, port))
    return {"login_id": login_id, "auth_url": authorize_url}


async def _serve_codearts_callback(login_id: str, port: int) -> None:
    session_info = _PENDING_LOGINS.get(login_id)
    if not session_info:
        return
    app, state = _build_oauth_callback_app(login_id)
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.wait_for(state["done"].wait(),
                               timeout=_CODEARTS_SERVER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        session_info["status"] = "failed"
        session_info["error"] = "登录超时(3 分钟未完成授权)"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            task.cancel()


# ============================================================
# 登录结果轮询(前端统一入口)
# ============================================================

@router.get("/login/{login_id}")
async def login_status(login_id: str, user: User = Depends(get_current_user_from_jwt)):
    """轮询登录结果; 完成时自动建 Provider(专门的渠道 + 真实模型清单)。

    WorkBuddy 类: 首次轮询触发后台轮询上游 token(前端每 2s 打一次本接口)。
    """
    _prune_pending()
    session_info = _PENDING_LOGINS.get(login_id)
    if not session_info:
        return JSONResponse({"error": "登录会话不存在"}, status_code=404)
    if session_info["user_id"] != user.id:
        return JSONResponse({"error": "无权查看该登录会话"}, status_code=403)

    channel = session_info["channel"]

    # WorkBuddy 类: pending 且未启动轮询 → 后台启动
    if (session_info["status"] == "pending"
            and channel in ("workbuddy", "buddy")
            and not session_info.get("polling")):
        session_info["polling"] = True
        asyncio.create_task(_poll_workbuddy_token(login_id))
        # 给上游第一拍 1s 间隔的时间
        await asyncio.sleep(1.2)

    if session_info["status"] != "done":
        return {
            "login_id": login_id,
            "channel": channel,
            "status": session_info["status"],
            **({"error": session_info["error"]}
               if session_info.get("error") else {}),
        }

    # done → 建 Provider + 独立路由组(幂等: 已建过就跳过)
    if not session_info.get("provider_created"):
        if channel == "codearts":
            api_key_plain = codearts.credentials_package(session_info["creds"])
            models = session_info["models"]
        elif channel in ("workbuddy", "buddy"):
            api_key_plain = workbuddy.credentials_package(
                session_info["token"], channel, session_info.get("account"))
            models = session_info["models"]
        elif channel == "cline":
            import json as _json
            api_key_plain = _json.dumps(session_info["creds"], ensure_ascii=False)
            models = session_info["models"]
        elif channel == "trae":
            api_key_plain = trae_mod.credentials_package(session_info["creds"])
            models = session_info["models"]
        elif channel in ("qoder", "qodercn"):
            api_key_plain = qoder_mod.credentials_package(session_info["creds"])
            models = session_info["models"]
        elif channel == "minimax":
            api_key_plain = minimax_mod.credentials_package(session_info["creds"])
            models = session_info["models"]
        elif channel == "lobsterai":
            api_key_plain = lobsterai_mod.credentials_package(session_info["creds"])
            models = session_info["models"]
        else:
            api_key_plain, models = "", []
        provider_id = _upsert_channel_provider(
            user.id, channel, _CHANNEL_BASE_URLS[channel], api_key_plain, models)
        session_info["provider_created"] = True
        session_info["provider_id"] = provider_id
        # 用完即弃(留 60s 供前端重复轮询确认, 之后清掉)
        session_info["expire_at"] = time.time() + 60

    return {
        "login_id": login_id,
        "channel": channel,
        "status": "done",
        "provider_id": session_info.get("provider_id"),
        "models": session_info.get("models", []),
        "model_count": len(session_info.get("models", [])),
        # 提示: 路由组里以 "渠道名-模型名" 直达, 如 codearts-glm-5.3-flash
        "model_prefix": f"{channel}-",
    }


# ============================================================
# 从 DSH 插件凭据库导入已登录账号(免重新授权)
# ============================================================

@router.get("/import/scan")
async def import_scan(user: User = Depends(get_current_user_from_jwt)):
    """扫描插件凭据库, 列出可导入的渠道账号(不落库, 只预览)。"""
    from backend.core.channel_import import collect_accounts, credentials_path
    try:
        found = await asyncio.to_thread(collect_accounts)
    except Exception as e:
        logger.warning("扫描插件凭据库失败: %s", e)
        return JSONResponse({"error": f"扫描失败: {e}"[:200]}, status_code=500)

    session = get_session()
    try:
        connected = {p.name for p in session.query(Provider)
                     .filter(Provider.user_id == user.id).all()}
    finally:
        session.close()

    out = []
    for item in found:
        ch = item["channel"]
        out.append({
            "channel": ch,
            "ref": item["ref"],
            "nickname": item.get("nickname", ""),
            "already_connected": ch in connected,
            "error": item.get("error"),
            "skipped": item.get("skipped", []),
        })
    return {"accounts": out, "credentials_path": str(credentials_path())}


@router.post("/import/apply")
async def import_apply(user: User = Depends(get_current_user_from_jwt)):
    """导入插件凭据库里的账号 → 建渠道 Provider + 独立路由组。"""
    from backend.core.channel_import import (
        CHANNEL_BASE_URLS, collect_accounts, fetch_channel_models,
    )
    try:
        found = await asyncio.to_thread(collect_accounts)
    except Exception as e:
        return JSONResponse({"error": f"扫描失败: {e}"[:200]}, status_code=500)

    results = []
    async with upstream_http.make_async(timeout=45) as client:
        for item in found:
            ch = item["channel"]
            if item.get("error") or not item.get("package"):
                results.append({"channel": ch, "ok": False,
                                "error": item.get("error") or "无凭据"})
                continue
            base_url = CHANNEL_BASE_URLS.get(ch)
            if not base_url:
                results.append({"channel": ch, "ok": False,
                                "error": "未知渠道"})
                continue
            models = await fetch_channel_models(client, ch, item["package"])
            model_ids = [m["id"] if isinstance(m, dict) else str(m)
                         for m in models]
            try:
                pid = _upsert_channel_provider(
                    user.id, ch, base_url, item["package"], models)
            except Exception as e:
                logger.warning("导入渠道 %s 落库失败: %s", ch, e, exc_info=True)
                results.append({"channel": ch, "ok": False,
                                "error": f"落库失败: {e}"[:200]})
                continue
            results.append({
                "channel": ch,
                "ok": True,
                "provider_id": pid,
                "nickname": item.get("nickname", ""),
                "model_count": len(model_ids),
                "models": model_ids[:20],
                "model_prefix": f"{ch}-",
                "skipped": item.get("skipped", []),
            })

    ok_count = sum(1 for r in results if r.get("ok"))
    return {"imported": ok_count, "total": len(results), "results": results}


# ============================================================
# 免密车道手动同步(opencode / kilo): 网络恢复后不必等 6h 调度
# ============================================================

@router.post("/{channel}/sync")
async def sync_lane(channel: str, user: User = Depends(get_current_user_from_jwt)):
    """手动触发免密免费池同步(仅 opencode / kilo)。"""
    if channel == "kilo":
        from backend.core.kilo_sources import sync_kilo_free_models
        fn = sync_kilo_free_models
    elif channel == "opencode":
        from backend.core.zen_sources import sync_zen_free_models
        # 手动同步=用户明确要求, 全量重探(会消耗免费额度); 定时同步只探新模型
        fn = lambda: sync_zen_free_models(probe_known=True)  # noqa: E731
    else:
        return JSONResponse({"error": f"{channel} 不是免密同步车道"}, status_code=404)
    try:
        r = await asyncio.to_thread(fn)
        return {"channel": channel, "ok": bool(r.get("ok")),
                "total_free": r.get("total_free", 0),
                "usable": r.get("usable", []),
                "unusable": r.get("unusable", {}),
                "users_updated": r.get("users_updated", 0),
                **({"lane_state": r["lane_state"]} if r.get("lane_state") else {}),
                **({"skipped": r["skipped"]} if r.get("skipped") else {}),
                **({"error": r.get("error")} if r.get("error") else {})}
    except Exception as e:
        logger.warning("车道 %s 手动同步失败: %s", channel, e)
        return JSONResponse({"channel": channel, "ok": False,
                             "error": str(e)[:200]}, status_code=502)


# ============================================================
# 一键签到
# ============================================================

async def _do_channel_checkin(user_id: int, channel: str) -> dict:
    """单渠道签到: 走 core/checkin(凭据刷新 + UTC+8 防重), 结果落盘。"""
    from backend.core.checkin import checkin_provider, record_state
    session = get_session()
    try:
        p = session.query(Provider).filter_by(
            user_id=user_id, name=channel).first()
        if p is None:
            return {"channel": channel, "kind": "inactive",
                    "message": "渠道未连接"}
    finally:
        session.close()
    async with upstream_http.make_async(timeout=60) as client:
        result = await checkin_provider(client, user_id, p)
    try:
        record_state(user_id, channel, result)
    except Exception as e:  # 状态落盘失败不影响签到结果
        logger.warning("签到状态落盘失败(%s): %s", channel, e)
    return result


@router.post("/checkin")
async def checkin_all(user: User = Depends(get_current_user_from_jwt)):
    """一键签到: 对所有支持 HTTP 签到且已连接的渠道执行(含凭据刷新)。"""
    from backend.core.checkin import run_checkin_for_all_users
    run = await run_checkin_for_all_users()
    mine = [r for r in run["results"] if r.get("user_id") == user.id]
    return {
        "date": run["date"],
        "results": mine,
        "claimed": sum(1 for r in mine if r.get("kind") == "claimed"),
        "already": sum(1 for r in mine if r.get("kind") in ("already-claimed", "skipped")),
        "failed": sum(1 for r in mine if r.get("kind") == "failed"),
        "total_credit": sum(r.get("credit") or 0 for r in mine),
    }


@router.post("/checkin/{channel}")
async def checkin_one(channel: str, user: User = Depends(get_current_user_from_jwt)):
    """单渠道签到。"""
    if channel not in _CHANNEL_CATALOG:
        return JSONResponse({"error": f"未知渠道 {channel}"}, status_code=404)
    try:
        result = await _do_channel_checkin(user.id, channel)
        return result
    except Exception as e:
        return JSONResponse({"channel": channel, "kind": "failed",
                             "message": str(e)})


def _checkin_next_run_at() -> str | None:
    """下一次自动签到的 UTC+8 时间(ISO, 无时区后缀以外的东西)。

    只做展示: 真正执行还是 main.py 的 `_channel_checkin_loop`。时间取
    settings.CHANNEL_CHECKIN_TIME(HH:MM), 今天已过就顺延到明天。
    """
    if not getattr(settings, "CHANNEL_CHECKIN_ENABLED", False):
        return None
    raw = str(getattr(settings, "CHANNEL_CHECKIN_TIME", "09:30") or "09:30")
    try:
        hh, mm = (int(x) for x in raw.split(":")[:2])
    except Exception:
        hh, mm = 9, 30
    now = datetime.now(UTC8)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.strftime("%Y-%m-%dT%H:%M:%S")


@router.get("/checkin/status")
async def checkin_status(user: User = Depends(get_current_user_from_jwt)):
    """签到支持情况(哪些渠道连了/支持签到) + 今日概览 + 自动签到日程。"""
    from backend.core.checkin import checkin_state_summary
    session = get_session()
    try:
        providers = session.query(Provider).filter_by(
            user_id=user.id, enabled=True).all()
        connected = {p.name for p in providers}
        supporting = checkin_channels()
        return {
            "supporting": supporting,
            "connected": [c for c in supporting if c in connected],
            "today": checkin_state_summary(),
            "schedule": {
                "enabled": bool(getattr(settings, "CHANNEL_CHECKIN_ENABLED", False)),
                "time": str(getattr(settings, "CHANNEL_CHECKIN_TIME", "09:30")),
                "timezone": "UTC+8",
                "next_run_at": _checkin_next_run_at(),
            },
            # 渠道展示名/说明, 前端不必再抄一份目录
            "catalog": [
                {
                    "id": c,
                    "display_name": _CHANNEL_CATALOG[c]["display_name"],
                    "has_checkin": bool(_CHANNEL_CATALOG[c].get("has_checkin")),
                    "models_note": _CHANNEL_CATALOG[c].get("models_note", ""),
                }
                for c in supporting if c in _CHANNEL_CATALOG
            ],
        }
    finally:
        session.close()
