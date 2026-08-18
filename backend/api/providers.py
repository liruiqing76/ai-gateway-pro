# ============================================================
# AI 模型网关 Pro — 渠道管理路由
# 用户渠道 CRUD + 系统预设模板 + 一键添加
# ============================================================

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field

from backend.auth.middleware import get_current_user_from_jwt
from backend.auth.security import encrypt_secret, decrypt_secret, mask_secret
from backend.models import get_session, User, Provider, ProviderTemplate

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

        provider = Provider(
            user_id=user.id,
            name=data.name,
            base_url=data.base_url,
            api_key_encrypted=encrypt_secret(data.api_key),
            models=data.models or [],
            disabled_models=[],
            free_only=data.free_only,
            enabled=True,
        )
        session.add(provider)
        session.commit()
        session.refresh(provider)
        return {"id": provider.id, "name": provider.name, "msg": "渠道添加成功"}
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

    session = get_session()
    try:
        provider = Provider(
            user_id=user.id,
            name=tpl["name"],
            base_url=tpl["base_url"],
            api_key_encrypted=encrypt_secret(data.api_key),
            models=tpl.get("models", []),
            disabled_models=[],
            free_only=tpl.get("free_only", True),
            enabled=True,
        )
        session.add(provider)
        session.commit()
        session.refresh(provider)
        return {
            "id": provider.id,
            "name": provider.name,
            "models_count": len(tpl.get("models", [])),
            "msg": f"「{tpl['name']}」配置成功!已自动添加 {len(tpl.get('models', []))} 个模型",
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
        if data.models is not None:
            p.models = data.models
        if data.enabled is not None:
            p.enabled = data.enabled

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