# ============================================================
# 智能网关 — 渠道管理路由
# 用户渠道 CRUD + 系统预设模板 + 一键添加
# ============================================================

import asyncio
import logging

import httpx

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field

from backend.auth.middleware import get_current_user_from_jwt
from backend.auth.security import encrypt_secret, decrypt_secret, mask_secret
from backend.core.proxy import get_context_length
from backend.core import zen_sources, free_sources
from backend.core.channel_auth import is_channel_provider
from backend.models import get_session, User, Provider, ProviderTemplate, Router, ModelSpeedLog

logger = logging.getLogger("gateway.providers")

router = APIRouter(prefix="/api/providers", tags=["providers"])


# ============================================================
# Schema
# ============================================================
class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=5, max_length=256)
    api_key: str = Field(min_length=1, max_length=256)
    models: list[str] = []
    free_only: bool = True
    auto_fetch: bool = True  # 未手填模型时自动请求上游 /models 拉取
    # 流量整形覆盖(null = 用全局默认): 渠道并发上限 / 同一模型最小间隔(毫秒)
    max_concurrency: int | None = None
    min_interval_ms: int | None = None


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    models: list[str] | None = None
    enabled: bool | None = None
    max_concurrency: int | None = None
    min_interval_ms: int | None = None


# ============================================================
# 读取预设模板
# ============================================================
def load_templates() -> list[dict]:
    """读取预设模板池。委托 free_sources: 走持久化运行时路径(exe 旁 data/),
    缺失时从打包内置基线 seed, 保证桌面版模板一键添加能列出模板。"""
    return free_sources.load_templates()


# ============================================================
# 上游模型拉取 + 自动分组(核心: 加渠道后模型自动显示/分组)
# ============================================================
def _is_free_item(item: dict) -> bool:
    """免费判定: 上游带 pricing 字段时, 全 0 或 id 带 free → 免费。

    无 pricing 信息的平台(NVIDIA 等)无法判定, 保守保留(当作免费),
    避免误伤现有渠道。pricing 值为字符串数字, 需转 float 比较。
    """
    mid = (item.get("id") or "").strip().lower()
    tail = mid.split("/")[-1]
    if ":free" in mid or tail.endswith("-free") or "-free-" in mid \
            or tail.split("-")[-1] == "free":
        return True
    pricing = item.get("pricing")
    if isinstance(pricing, dict):
        vals = []
        for v in pricing.values():
            if isinstance(v, bool):
                vals.append(float(v))
            elif isinstance(v, (int, float)):
                vals.append(float(v))
            elif isinstance(v, str):
                try:
                    vals.append(float(v))
                except ValueError:
                    continue
        if vals:
            return all(v == 0 for v in vals)
    return True  # 无 pricing 信息 → 保守保留


def _is_zen_channel(base_url: str) -> bool:
    """opencode zen 免 key 渠道识别。

    上游 /v1/models 返回全量 63 个模型且无 pricing 字段, 通用免费过滤
    (_is_free_item 对无 pricing 保守全留)会全部保留, 必须走 models.dev
    免费档 + 真探测的策展路径(与定时自动同步同一套逻辑)。
    """
    return (base_url or "").rstrip("/") == zen_sources.ZEN_BASE_URL


async def fetch_upstream_models(base_url: str, api_key: str,
                                free_only: bool = False) -> list[str]:
    """
    请求上游 GET {base_url}/models 拉取模型列表。
    兼容两种 base_url 形态: 带 /v1 结尾 和 不带(自动补 /v1/models)。
    free_only=True 时只保留免费模型(带 pricing 的上游按定价过滤,
    Nous Portal 等平台 372 个模型会收敛到 7 个免费 SKU; 无 pricing
    的 NVIDIA 等全量保留)。失败抛异常。
    """
    import httpx

    base = base_url.rstrip("/")
    candidates = [f"{base}/models"]
    if not base.endswith("/v1"):
        candidates.append(f"{base}/v1/models")

    headers = {}
    # 免 key 渠道(空密钥, 如 opencode zen 匿名档)不带 Authorization,
    # 否则拼出非法头 b'Bearer ' 被 httpx 直接拒绝
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    last_err = ""
    # 模型清单不小的平台(Nous 660KB / NVIDIA 等), 20s 会读超时, 给足 60s
    async with httpx.AsyncClient(timeout=60) as client:
        for url in candidates:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
                    continue
                data = resp.json()
                items = data.get("data") if isinstance(data, dict) else data
                if not isinstance(items, list):
                    last_err = "响应格式异常"
                    continue
                items = [it for it in items
                         if isinstance(it, dict) and it.get("id")]
                if free_only:
                    items = [it for it in items if _is_free_item(it)]
                ids = sorted({(it.get("id") or "").strip() for it in items})
                if ids:
                    return ids
                last_err = "上游返回空模型列表"
            except Exception as e:
                last_err = str(e)[:150] or "网络中断或超时"
    raise HTTPException(status_code=502, detail=f"拉取模型失败: {last_err}")


def rebuild_user_routers(session, user_id: int) -> list[str]:
    """根据当前启用渠道补齐默认路由组(quick/high_quality/vision)。

    ★ 只创建缺失的组, 绝不覆盖已存在的组: 用户在界面手动调整过的路由组
      (增删模型)必须原样保留 —— 旧实现每次添加/编辑/刷新/测试渠道都会用
      渠道模型重新生成并覆盖同名组, 导致用户手动删掉的下架模型(404 的
      kimi-k3 / AMD GLM-5.2 等)被"自动复活"。想按当前渠道整体重建请用
      界面的"自动分类"(POST /api/routers/init-defaults), 那是明确的覆盖动作。
    """
    from backend.core.router_group import build_router_groups
    providers = session.query(Provider).filter_by(user_id=user_id, enabled=True).all()
    groups = build_router_groups(providers, user_id=user_id)
    created = []
    for name, models in groups.items():
        if not models:
            continue
        existing = session.query(Router).filter_by(user_id=user_id, name=name).first()
        if existing:
            continue  # 已有组绝不覆盖(用户手动调整优先)
        session.add(Router(user_id=user_id, name=name, models=models, strategy="quality"))
        created.append(name)
    return created


# ============================================================
# 获取当前用户的渠道列表
# ============================================================
@router.get("")
async def list_providers(user: User = Depends(get_current_user_from_jwt)):
    session = get_session()
    try:
        providers = session.query(Provider).filter_by(user_id=user.id).all()
        result = []
        for p in providers:
            d = {
                "id": p.id,
                "name": p.name,
                "base_url": p.base_url,
                "models": p.models or [],
                "disabled_models": p.disabled_models or [],
                "free_only": p.free_only,
                "max_concurrency": p.max_concurrency,
                "min_interval_ms": p.min_interval_ms,
                "enabled": p.enabled,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                # 每个渠道的 模型名 -> 上下文长度 映射, 后端统一来源
                "context_lengths": {
                    m: get_context_length(m) for m in (p.models or [])
                },
            }
            result.append(d)
        return result
    finally:
        session.close()


# ============================================================
# 读取最新测速数据 (放在 /{provider_id} 之前避免路由冲突)
# ============================================================
@router.get("/model-speeds")
async def get_model_speeds(
    user: User = Depends(get_current_user_from_jwt),
):
    """获取所有模型的最新测速数据"""
    from sqlalchemy import func
    session = get_session()
    try:
        # 子查询: 每个 prefixed_id 的最新一条记录
        subq = (
            session.query(
                ModelSpeedLog.prefixed_id,
                func.max(ModelSpeedLog.id).label("max_id"),
            )
            .filter_by(user_id=user.id)
            .group_by(ModelSpeedLog.prefixed_id)
            .subquery()
        )
        rows = (
            session.query(ModelSpeedLog)
            .join(subq, ModelSpeedLog.id == subq.c.max_id)
            .all()
        )
        result = {}
        last_check = None
        for r in rows:
            is_group = r.prefixed_id.startswith("router-group/")
            result[r.prefixed_id] = {
                "provider": r.provider_name,
                "model": r.model_name,
                "latency_ms": r.latency_ms,
                "available": r.available,
                "error": r.error_msg,
                "checked_at": r.created_at.isoformat() if r.created_at else None,
                "is_group": is_group,
            }
            if r.created_at and (last_check is None or r.created_at > last_check):
                last_check = r.created_at
        return {
            "ok": True,
            "speeds": result,
            "last_check": last_check.isoformat() if last_check else None,
        }
    finally:
        session.close()


# ============================================================
# 获取渠道详情(含解密 Key,仅限本人)
# ============================================================
@router.get("/{provider_id}")
async def get_provider(
    provider_id: int,
    user: User = Depends(get_current_user_from_jwt),
):
    session = get_session()
    try:
        p = session.query(Provider).filter_by(
            id=provider_id, user_id=user.id
        ).first()
        if not p:
            raise HTTPException(status_code=404, detail="渠道不存在")
        try:
            api_key = decrypt_secret(p.api_key_encrypted)
        except Exception:
            api_key = ""
        return {
            "id": p.id,
            "name": p.name,
            "base_url": p.base_url,
            "api_key": api_key,
            "models": p.models or [],
            "disabled_models": p.disabled_models or [],
            "free_only": p.free_only,
            "max_concurrency": p.max_concurrency,
            "min_interval_ms": p.min_interval_ms,
            "enabled": p.enabled,
        }
    finally:
        session.close()


# ============================================================
# 新建渠道(API Key 加密存储)
# ============================================================
@router.post("")
async def create_provider(
    data: ProviderCreate,
    user: User = Depends(get_current_user_from_jwt),
):
    session = get_session()
    try:
        # 同名检查
        exists = session.query(Provider).filter_by(
            user_id=user.id, name=data.name
        ).first()
        if exists:
            raise HTTPException(status_code=400, detail=f"渠道名「{data.name}」已存在")

        # 未手填模型时, 自动请求上游拉取模型列表
        models = [m for m in (data.models or []) if m.strip()]
        fetched = False
        if not models and data.auto_fetch:
            try:
                models = await fetch_upstream_models(data.base_url, data.api_key,
                                                      free_only=data.free_only)
                fetched = True
            except HTTPException as e:
                # 拉取失败不阻塞渠道创建, 仅提示
                logger.warning("auto fetch models failed for %s: %s", data.name, e.detail)

        try:
            provider = Provider(
                user_id=user.id,
                name=data.name,
                base_url=data.base_url,
                api_key_encrypted=encrypt_secret(data.api_key),
                models=models,
                disabled_models=[],
                free_only=data.free_only,
                max_concurrency=data.max_concurrency,
                min_interval_ms=data.min_interval_ms,
                enabled=True,
            )
            session.add(provider)
            session.commit()
            session.refresh(provider)
        except Exception:
            # 落库失败必须留痕(此前打包环境无控制台, 500 静默无迹)
            logger.exception("create provider failed for %s", data.name)
            raise HTTPException(status_code=500, detail="写入渠道失败, 详情见日志")

        # 自动重建路由组(快速/日常/识图)
        groups = rebuild_user_routers(session, user.id)
        session.commit()

        return {
            "id": provider.id,
            "name": provider.name,
            "models_count": len(models),
            "models_fetched": fetched,
            "routers_updated": groups,
            "msg": f"渠道添加成功, {'已自动获取 ' if fetched else ''}{len(models)} 个模型, 已更新分组: {', '.join(groups) if groups else '无'}",
        }
    finally:
        session.close()


# ============================================================
# 获取系统预设模板列表
# ============================================================
@router.get("/templates/all")
async def list_templates(user: User = Depends(get_current_user_from_jwt)):
    return load_templates()


# ============================================================
# 一键添加预设模板(填入 API Key 即可)
# ============================================================
class TemplateApply(BaseModel):
    template_name: str
    api_key: str = Field(min_length=1)


@router.post("/apply-template")
async def apply_template(
    data: TemplateApply,
    user: User = Depends(get_current_user_from_jwt),
):
    templates = load_templates()
    tpl = next((t for t in templates if t["name"] == data.template_name), None)
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")

    # 模板模型为空时, 自动从上游拉取(按模板 free_only 过滤)
    models = tpl.get("models", [])
    if not models:
        try:
            models = await fetch_upstream_models(
                tpl["base_url"], data.api_key,
                free_only=tpl.get("free_only", True),
            )
        except HTTPException as e:
            logger.warning("template fetch models failed for %s: %s", tpl["name"], e.detail)
            models = []

    session = get_session()
    try:
        # 死模型黑名单: 拉黑过的模型不落库(模板重装/刷新也不会复活)
        from backend.core.dead_models import banned_keys
        banned = banned_keys(user.id)
        models = [m for m in models if f"{tpl['name']}||{m}" not in banned]

        # 重名保护: 模板渠道已存在时给出明确提示(否则裸 500 前端只会显示"添加失败")
        exists = session.query(Provider).filter_by(
            user_id=user.id, name=tpl["name"]
        ).first()
        if exists:
            raise HTTPException(
                status_code=400,
                detail=f"渠道「{tpl['name']}」已存在, 请直接在列表中编辑它",
            )

        try:
            provider = Provider(
                user_id=user.id,
                name=tpl["name"],
                base_url=tpl["base_url"],
                api_key_encrypted=encrypt_secret(data.api_key),
                models=models,
                disabled_models=[],
                free_only=tpl.get("free_only", True),
                enabled=True,
            )
            session.add(provider)
            session.commit()
            session.refresh(provider)
        except Exception:
            logger.exception("apply template failed for %s", tpl["name"])
            raise HTTPException(status_code=500, detail="写入渠道失败, 详情见日志")

        # 自动重建路由组
        groups = rebuild_user_routers(session, user.id)
        session.commit()

        return {
            "id": provider.id,
            "name": provider.name,
            "models_count": len(models),
            "routers_updated": groups,
            "msg": f"「{tpl['name']}」配置成功!已添加 {len(models)} 个模型, 分组已更新",
        }
    finally:
        session.close()


# ============================================================
# 拉取上游模型列表(渠道弹窗「获取模型」按钮)
# ============================================================
class FetchModelsIn(BaseModel):
    base_url: str
    api_key: str
    # 默认只拉免费模型: 上游带 pricing 时按定价过滤(Nous Portal 372 个
    # 会收敛到 7 个免费 SKU), 无 pricing 的平台(NVIDIA 等)全量保留。
    free_only: bool = True


@router.post("/fetch-models")
async def fetch_models(
    data: FetchModelsIn,
    user: User = Depends(get_current_user_from_jwt),
):
    """用表单中的 URL+Key 实时拉取上游模型列表(不落库)"""
    if _is_zen_channel(data.base_url):
        # opencode zen: /v1/models 是 63 个全量(无 pricing), 通用拉取会
        # 把付费模型也列出来。改为只取目录里 -free 结尾的免费档并真探测,
        # 只显示实测可用。探测是同步网络调用, 放后台线程避免阻塞事件循环;
        # 短超时(10s)保证按钮体验, 残缺结果由定时同步/健康检测补全。
        def _curate_zen():
            free = zen_sources.fetch_zen_free_models()
            if not free:
                raise HTTPException(status_code=502,
                                    detail="未能获取 opencode 免费模型列表")
            results = zen_sources.probe_many(free, timeout=10.0)
            return [m for m, (ok, _) in results.items() if ok]
        models = await asyncio.to_thread(_curate_zen)
    else:
        models = await fetch_upstream_models(data.base_url, data.api_key,
                                             free_only=data.free_only)
    return {"ok": True, "models": models, "count": len(models)}


# ============================================================
# 刷新指定渠道的模型列表(渠道表格「刷新模型」按钮)
# ============================================================


# 非对话类模型黑名单: NVIDIA 等平台 /v1/models 返回全集(embed/翻译/安全/图像生成等),
# 直接落库会把渠道撑爆并污染路由兜底, 刷新拉取时过滤掉。
_NON_CHAT_KEYWORDS = (
    "embed", "retriever", "translate", "guard", "safety", "reward",
    "detector", "parse", "calibration", "clip", "deplot", "kosmos",
    "diffusion", "vila", "neva", "ising", "omni", "synthetic",
    "nemo-retriever", "nv-embed", "embedqa", "riva", "nemotron-parse",
    "3-super-free", "x-preview-f-free",
)


def filter_chat_models(models):
    """只保留对话/生成类模型 ID(排除黑名单关键词)。"""
    return [m for m in models if not any(k in m.lower() for k in _NON_CHAT_KEYWORDS)]


@router.post("/{provider_id}/refresh-models")
async def refresh_provider_models(
    provider_id: int,
    user: User = Depends(get_current_user_from_jwt),
):
    """重新拉取上游模型并更新库中渠道, 同时重建路由组

    opencode zen 渠道特殊处理: 上游 /v1/models 返回全量 63 个模型且无
    pricing 字段, 通用免费过滤会全部保留, 因此走 zen 策展路径(与定时自动
    同步同一套逻辑): 目录里 -free 结尾的免费档 → 真探测 → 只落实测可用。
    同步在后台线程执行, 不阻塞事件循环; 探测全灭时保留现有模型不清空。
    """
    session = get_session()
    try:
        p = session.query(Provider).filter_by(id=provider_id, user_id=user.id).first()
        if not p:
            raise HTTPException(status_code=404, detail="渠道不存在")
        if _is_zen_channel(p.base_url):
            # 同步/探测是阻塞网络调用: 放后台线程执行, 与定时同步任务共用
            # _lock 天然串行, 事件循环不再被探测时间卡死(网关全程可响应)
            result = await asyncio.to_thread(zen_sources.sync_zen_free_models, 10.0)
            if not result.get("ok"):
                raise HTTPException(status_code=502, detail=result.get(
                    "error", "opencode 免费模型同步失败"))
            session.expire_all()  # sync 在独立会话里提交, 重读渠道最新值
            if not (result.get("usable") or []):
                # 全灭保护: 探测全部失败, sync 未覆盖任何渠道, 保留现状
                return {"ok": True, "models_count": len(p.models or []),
                        "routers_updated": [],
                        "msg": "探测全部失败(疑似网络/上游故障), 保留现有模型不变, 请稍后再试"}
            models = sorted(set(p.models or []))  # sync 已把渠道更新为实测可用集
        elif is_channel_provider(p.name):
            # 渠道 Provider(WorkBuddy/buddy/LobsterAI/Trae/CodeArts 等):
            # 走渠道专属目录接口, 比通用 /v1/models 透传多拿上游计费倍率
            # (workbuddy credits / lobsterai costMultiplier)并自动入库
            # model_rates。拉取失败时保留现有模型不清空(与 zen 全灭保护同思路)。
            from backend.core.channel_import import fetch_channel_models, sync_model_rates
            try:
                package = decrypt_secret(p.api_key_encrypted)
            except Exception:
                package = ""
            async with httpx.AsyncClient() as client:
                entries = await fetch_channel_models(client, p.name, package)
            if not entries:
                return {"ok": True, "models_count": len(p.models or []),
                        "routers_updated": [],
                        "msg": "上游模型目录拉取失败, 保留现有模型不变, 请稍后再试"}
            sync_model_rates(user.id, p.name, entries)
            models = [e["id"] if isinstance(e, dict) else str(e) for e in entries]
            from backend.core.dead_models import banned_keys
            banned = banned_keys(user.id)
            models = [m for m in models if f"{p.name}||{m}" not in banned]
            p.models = models
        else:
            try:
                api_key = decrypt_secret(p.api_key_encrypted)
            except Exception:
                # 免 key 匿名渠道(如 opencode zen)存的是空串/未加密数据:
                # 按无密钥渠道拉取, 而不是让解密异常把整个接口打成 500
                api_key = ""
            models = await fetch_upstream_models(p.base_url, api_key,
                                                 free_only=p.free_only)
            models = filter_chat_models(models)  # 过滤非对话模型, 防止渠道被全量污染
            from backend.core.dead_models import banned_keys
            banned = banned_keys(user.id)
            models = [m for m in models if f"{p.name}||{m}" not in banned]
            p.models = models  # 过滤结果必须落库(此前漏赋值导致刷新按钮假成功)
        session.commit()

        groups = rebuild_user_routers(session, user.id)
        session.commit()
        return {
            "ok": True,
            "models_count": len(models),
            "routers_updated": groups,
            "msg": f"已刷新 {len(models)} 个模型, 分组已更新",
        }
    finally:
        session.close()


# ============================================================
# 更新渠道
# ============================================================
@router.put("/{provider_id}")
async def update_provider(
    provider_id: int,
    data: ProviderUpdate,
    user: User = Depends(get_current_user_from_jwt),
):
    session = get_session()
    try:
        p = session.query(Provider).filter_by(
            id=provider_id, user_id=user.id
        ).first()
        if not p:
            raise HTTPException(status_code=404, detail="渠道不存在")

        if data.name is not None:
            p.name = data.name
        if data.base_url is not None:
            p.base_url = data.base_url
        if data.api_key is not None:
            p.api_key_encrypted = encrypt_secret(data.api_key)
        models_changed = False
        if data.models is not None:
            p.models = data.models
            models_changed = True
        if data.enabled is not None:
            p.enabled = data.enabled
        if data.max_concurrency is not None:
            p.max_concurrency = data.max_concurrency
        if data.min_interval_ms is not None:
            p.min_interval_ms = data.min_interval_ms

        session.commit()
        # 模型/启用状态变化 → 重建路由组
        if models_changed or data.enabled is not None:
            rebuild_user_routers(session, user.id)
            session.commit()
        return {"ok": True, "msg": "渠道已更新"}
    finally:
        session.close()


# ============================================================
# 删除渠道
# ============================================================
@router.delete("/{provider_id}")
async def delete_provider(
    provider_id: int,
    user: User = Depends(get_current_user_from_jwt),
):
    session = get_session()
    try:
        p = session.query(Provider).filter_by(
            id=provider_id, user_id=user.id
        ).first()
        if not p:
            raise HTTPException(status_code=404, detail="渠道不存在")
        session.delete(p)
        session.commit()
        # 渠道删除后重建路由组(移除失效模型)
        rebuild_user_routers(session, user.id)
        session.commit()
        return {"ok": True, "msg": "渠道已删除"}
    finally:
        session.close()


# ============================================================
# 测试渠道连通性(真实请求上游)
# ============================================================
class TestProviderIn(BaseModel):
    base_url: str
    api_key: str
    model: str | None = None


@router.post("/test")
async def test_provider(
    data: TestProviderIn,
):
    """真实请求上游 API 验证 Key 是否有效"""
    import httpx
    model = data.model or "gpt-3.5-turbo"
    url = data.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {data.api_key}"},
            )
            if resp.status_code == 200:
                return {"ok": True, "msg": "连接成功 ✓", "model": model}
            else:
                return {"ok": False, "msg": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "msg": f"连接失败: {str(e)[:200]}"}


# ============================================================
# 路由组测速 — 通过聚合网关按策略转发 (真正验证聚合API可用性)
# ============================================================
@router.post("/check-all-models")
async def check_all_models(
    user: User = Depends(get_current_user_from_jwt),
):
    """按路由组测试聚合API: 传组名当model, 网关按策略自动选模型转发"""
    session = get_session()
    try:
        providers = session.query(Provider).filter_by(user_id=user.id, enabled=True).all()
        if not providers:
            raise HTTPException(status_code=404, detail="暂无渠道")

        import httpx
        import time

        gateway_api_key = user.api_key
        gateway_base = "http://127.0.0.1:8765"

        # 获取用户的路由组
        from backend.models import Router as RouterModel
        routers = session.query(RouterModel).filter_by(user_id=user.id).all()
        if not routers:
            raise HTTPException(status_code=404, detail="暂无路由组，请先配置路由组")

        results = {}
        async with httpx.AsyncClient(timeout=60) as client:
            for r in routers:
                group_name = r.name
                strategy = r.strategy or "quality"
                strategy_label = {"quality": "质量优先", "fastest": "最快优先", "stable": "最稳优先", "failover": "故障转移"}.get(strategy, strategy)

                headers = {
                    "Authorization": f"Bearer {gateway_api_key}",
                    "Content-Type": "application/json",
                    # 标记为速度探测: 链路日志归类 request_type=speed_test, 不写聊天记录
                    "X-GW-Source": "speed-test",
                }
                payload = {
                    "model": group_name,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 8,
                }
                start = time.time()
                available = False
                latency_ms = None
                error_msg = None
                selected_model = None
                try:
                    resp = await client.post(
                        f"{gateway_base}/v1/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    elapsed = round((time.time() - start) * 1000, 1)
                    latency_ms = int(elapsed)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("choices"):
                            available = True
                            # 从响应头或响应体推断实际使用的模型
                            selected_model = data.get("model", "")
                        else:
                            error_msg = "上游返回空 choices"
                    elif resp.status_code == 503:
                        error_msg = "组内所有模型均不可用"
                    else:
                        body = resp.text[:200]
                        error_msg = f"HTTP {resp.status_code}: {body}"
                except Exception as e:
                    elapsed = round((time.time() - start) * 1000, 1)
                    latency_ms = int(elapsed) if elapsed > 1 else None
                    error_msg = str(e)[:150]

                results[group_name] = {
                    "strategy": strategy,
                    "strategy_label": strategy_label,
                    "available": available,
                    "latency_ms": latency_ms,
                    "error": error_msg,
                    "selected_model": selected_model,
                }

        # 写入测速历史
        for group_name, info in results.items():
            log = ModelSpeedLog(
                user_id=user.id,
                provider_name=group_name,
                model_name=group_name,
                prefixed_id=f"router-group/{group_name}",
                latency_ms=info.get("latency_ms"),
                available=info["available"],
                error_msg=info.get("error"),
            )
            session.add(log)
        session.commit()

        return {"ok": True, "groups": results}
    finally:
        session.close()



# ============================================================
# 后台轮询测速 (被 lifespan 调用)
# ============================================================
async def _run_speed_check_for_all_users():
    """对所有有渠道的用户执行测速, 结果写入 model_speed_logs"""
    import asyncio
    session = get_session()
    try:
        from backend.models import User as UserModel
        users = session.query(UserModel).filter(UserModel.is_active == True).all()
        user_ids = [u.id for u in users]
    finally:
        session.close()

    for uid in user_ids:
        try:
            await _do_speed_check(uid)
        except Exception as e:
            logger.warning(f"测速失败 user={uid}: {e}")


def _speed_check_skip_groups() -> set:
    """后台测速要跳过的路由组(默认 opencode/kilo 等公共额度车道)。

    `SPEED_CHECK_SKIP_GROUPS` 逗号分隔; 置空 = 不跳过任何组。
    """
    try:
        from backend.config import settings
        raw = getattr(settings, "SPEED_CHECK_SKIP_GROUPS", "") or ""
    except Exception:
        raw = ""
    return {p.strip() for p in raw.split(",") if p.strip()}


async def _do_speed_check(user_id: int):
    """对单个用户按路由组测速(通过网关聚合端点), 写入 DB"""
    import httpx, time, json as _json

    session = get_session()
    try:
        providers = session.query(Provider).filter_by(user_id=user_id, enabled=True).all()
        user = session.query(User).filter_by(id=user_id).first()
        if not providers or not user:
            return

        gateway_api_key = user.api_key
        gateway_base = "http://127.0.0.1:8765"

        # 获取用户的路由组
        from backend.models import Router as RouterModel
        routers = session.query(RouterModel).filter_by(user_id=user_id).all()
        if not routers:
            logger.info(f"user={user_id} 无路由组, 跳过测速")
            return

        async with httpx.AsyncClient(timeout=60) as client:
            skipped = set()
            for r in routers:
                group_name = r.name
                if group_name in _speed_check_skip_groups():
                    # 免密车道的额度是公共池, 测速会经 failover 把整组模型都试一遍,
                    # 白白烧掉本该留给用户的免费额度 → 不测(见 config 注释)
                    skipped.add(group_name)
                    continue
                headers = {
                    "Authorization": f"Bearer {gateway_api_key}",
                    "Content-Type": "application/json",
                    # 标记为速度探测: 链路日志归类 request_type=speed_test, 不写聊天记录
                    "X-GW-Source": "speed-test",
                }
                payload = {
                    "model": group_name,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 8,
                }
                start = time.time()
                available = False
                latency_ms = None
                error_msg = None
                try:
                    resp = await client.post(
                        f"{gateway_base}/v1/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    elapsed = round((time.time() - start) * 1000, 1)
                    latency_ms = int(elapsed)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("choices"):
                            available = True
                        else:
                            error_msg = "上游返回空 choices"
                    elif resp.status_code == 503:
                        error_msg = "组内所有模型均不可用"
                    else:
                        error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                except Exception as e:
                    elapsed = round((time.time() - start) * 1000, 1)
                    latency_ms = int(elapsed) if elapsed > 1 else None
                    error_msg = str(e)[:150]

                log = ModelSpeedLog(
                    user_id=user_id,
                    provider_name=r.name,
                    model_name=r.name,
                    prefixed_id=f"router-group/{r.name}",
                    latency_ms=latency_ms,
                    available=available,
                    error_msg=error_msg,
                )
                session.add(log)
        session.commit()
        logger.info(f"路由组测速完成 user={user_id}" +
                    (f" (跳过公共额度组: {', '.join(sorted(skipped))})" if skipped else ""))
    except Exception as e:
        session.rollback()
        logger.error(f"路由组测速异常 user={user_id}: {e}")
    finally:
        session.close()
