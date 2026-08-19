# ============================================================
# 智能网关 — 渠道管理路由
# 用户渠道 CRUD + 系统预设模板 + 一键添加
# ============================================================

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field

from backend.auth.middleware import get_current_user_from_jwt
from backend.auth.security import encrypt_secret, decrypt_secret, mask_secret
from backend.models import get_session, User, Provider, ProviderTemplate, Router

logger = logging.getLogger("gateway.providers")

router = APIRouter(prefix="/api/providers", tags=["providers"])

# 预设模板文件
TEMPLATES_FILE = Path(__file__).parent.parent / "data" / "provider_templates.json"


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


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    models: list[str] | None = None
    enabled: bool | None = None


# ============================================================
# 读取预设模板
# ============================================================
def load_templates() -> list[dict]:
    if TEMPLATES_FILE.exists():
        return json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
    return []


# ============================================================
# 上游模型拉取 + 自动分组(核心: 加渠道后模型自动显示/分组)
# ============================================================
async def fetch_upstream_models(base_url: str, api_key: str) -> list[str]:
    """
    请求上游 GET {base_url}/models 拉取模型列表。
    兼容两种 base_url 形态: 带 /v1 结尾 和 不带(自动补 /v1/models)。
    返回排序后的模型 ID 列表, 失败抛异常。
    """
    import httpx

    base = base_url.rstrip("/")
    candidates = [f"{base}/models"]
    if not base.endswith("/v1"):
        candidates.append(f"{base}/v1/models")

    headers = {"Authorization": f"Bearer {api_key}"}
    last_err = ""
    async with httpx.AsyncClient(timeout=20) as client:
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
                ids = sorted({
                    (it.get("id") or "").strip()
                    for it in items
                    if isinstance(it, dict) and it.get("id")
                })
                if ids:
                    return ids
                last_err = "上游返回空模型列表"
            except Exception as e:
                last_err = str(e)[:150]
    raise HTTPException(status_code=502, detail=f"拉取模型失败: {last_err}")


def rebuild_user_routers(session, user_id: int) -> list[str]:
    """根据用户全部启用渠道重建默认路由组(快速/日常/识图), 返回更新的组名"""
    from backend.core.router_group import build_router_groups

    providers = session.query(Provider).filter_by(user_id=user_id, enabled=True).all()
    groups = build_router_groups(providers)
    updated = []
    for name, models in groups.items():
        if not models:
            continue
        existing = session.query(Router).filter_by(user_id=user_id, name=name).first()
        if existing:
            existing.models = models
        else:
            session.add(Router(user_id=user_id, name=name, models=models, strategy="quality"))
        updated.append(name)
    return updated


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
                "enabled": p.enabled,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            result.append(d)
        return result
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
                models = await fetch_upstream_models(data.base_url, data.api_key)
                fetched = True
            except HTTPException as e:
                # 拉取失败不阻塞渠道创建, 仅提示
                logger.warning("auto fetch models failed for %s: %s", data.name, e.detail)

        provider = Provider(
            user_id=user.id,
            name=data.name,
            base_url=data.base_url,
            api_key_encrypted=encrypt_secret(data.api_key),
            models=models,
            disabled_models=[],
            free_only=data.free_only,
            enabled=True,
        )
        session.add(provider)
        session.commit()
        session.refresh(provider)

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

    # 模板模型为空时, 自动从上游拉取
    models = tpl.get("models", [])
    if not models:
        try:
            models = await fetch_upstream_models(tpl["base_url"], data.api_key)
        except HTTPException as e:
            logger.warning("template fetch models failed for %s: %s", tpl["name"], e.detail)
            models = []

    session = get_session()
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


@router.post("/fetch-models")
async def fetch_models(
    data: FetchModelsIn,
    user: User = Depends(get_current_user_from_jwt),
):
    """用表单中的 URL+Key 实时拉取上游模型列表(不落库)"""
    models = await fetch_upstream_models(data.base_url, data.api_key)
    return {"ok": True, "models": models, "count": len(models)}


# ============================================================
# 刷新指定渠道的模型列表(渠道表格「刷新模型」按钮)
# ============================================================
@router.post("/{provider_id}/refresh-models")
async def refresh_provider_models(
    provider_id: int,
    user: User = Depends(get_current_user_from_jwt),
):
    """重新拉取上游模型并更新库中渠道, 同时重建路由组"""
    session = get_session()
    try:
        p = session.query(Provider).filter_by(id=provider_id, user_id=user.id).first()
        if not p:
            raise HTTPException(status_code=404, detail="渠道不存在")
        api_key = decrypt_secret(p.api_key_encrypted)
        models = await fetch_upstream_models(p.base_url, api_key)

        # 保留用户禁用的模型标记
        p.models = models
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