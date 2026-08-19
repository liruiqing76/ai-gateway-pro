# ============================================================
# 智能网关 — 路由组可用性检查
# 对每个路由组中的每个模型发轻量请求，验证连通性+延迟
# ============================================================

import json
import time
import logging
import asyncio
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from backend.auth.middleware import get_current_user_from_jwt
from backend.auth.security import decrypt_secret
from backend.models import get_session, User, Provider, Router

logger = logging.getLogger("gateway.router_check")

router = APIRouter(prefix="/api/routers", tags=["routers"])


class CheckResult(BaseModel):
    model: str
    provider: str
    available: bool
    latency_ms: float | None = None
    error: str | None = None


class GroupCheckResult(BaseModel):
    group_name: str
    total: int
    available: int
    results: list[CheckResult]


async def test_single_model(base_url: str, api_key: str, model: str) -> CheckResult:
    """对一个模型发起轻量 chat 请求，返回可用性结果"""
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
        elapsed = round((time.time() - start) * 1000, 1)
        if resp.status_code == 200:
            return CheckResult(
                model=model,
                provider="",
                available=True,
                latency_ms=elapsed,
            )
        else:
            return CheckResult(
                model=model,
                provider="",
                available=False,
                error=f"HTTP {resp.status_code}: {resp.text[:100]}",
            )
    except Exception as e:
        elapsed = round((time.time() - start) * 1000, 1)
        return CheckResult(
            model=model,
            provider="",
            available=False,
            latency_ms=elapsed if elapsed > 1 else None,
            error=str(e)[:150],
        )


@router.post("/check-availability")
async def check_availability(
    user: User = Depends(get_current_user_from_jwt),
):
    """检查所有路由组的模型可用性"""
    session = get_session()
    try:
        # 1. 所有路由组
        routers = session.query(Router).filter_by(user_id=user.id).all()
        if not routers:
            raise HTTPException(status_code=404, detail="暂无路由组")

        # 2. 所有渠道（含解密 key）
        providers = session.query(Provider).filter_by(user_id=user.id, enabled=True).all()
        prov_map = {}
        for p in providers:
            try:
                key = decrypt_secret(p.api_key_encrypted)
            except Exception:
                key = ""
            prov_map[p.name] = {"base_url": p.base_url, "api_key": key}

        # 3. 对每个路由组检测
        results = {}
        for r in routers:
            models = r.models or []
            if isinstance(models, str):
                models = json.loads(models)

            checks = []
            for prefixed in models:
                # 解析渠道前缀: "NVIDIA-glm-5.2" -> name="NVIDIA", model="glm-5.2"
                if "-" in prefixed:
                    parts = prefixed.split("-", 1)
                    prov_name = parts[0]
                    model_name = parts[1]
                else:
                    prov_name = ""
                    model_name = prefixed

                prov = prov_map.get(prov_name)
                if not prov:
                    checks.append(CheckResult(
                        model=prefixed,
                        provider=prov_name,
                        available=False,
                        error="渠道不存在或已禁用",
                    ))
                    continue

                result = await test_single_model(prov["base_url"], prov["api_key"], model_name)
                result.model = prefixed
                result.provider = prov_name
                checks.append(result)

            available = sum(1 for c in checks if c.available)
            results[r.name] = GroupCheckResult(
                group_name=r.name,
                total=len(checks),
                available=available,
                results=checks,
            )

        return {"ok": True, "groups": results}

    finally:
        session.close()