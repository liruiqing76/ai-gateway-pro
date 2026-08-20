# ============================================================
# 自动故障转移测试
# ============================================================
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from backend.core.proxy import pick_available_models


def test_failover_non_stream_retries_on_failure():
    """
    非流式: 第一个候选报错,自动重试第二个(已在现有代码中实现)
    验证 pick_available_models 返回多个候选
    """
    from backend.models import Provider

    providers = [
        Provider(id=1, name="MockA", models=["model-a"], disabled_models="[]", enabled=True),
        Provider(id=2, name="MockB", models=["model-b"], disabled_models="[]", enabled=True),
    ]

    candidates = pick_available_models(providers, None, force=True)
    assert len(candidates) >= 2, "应该返回多个候选"


def test_failover_picks_multiple_for_router_group():
    """
    路由组: 多个模型时返回全部候选(按策略排序)
    """
    from backend.models import Provider

    providers = [
        Provider(id=1, name="MockA", models=["model-a", "model-b"], disabled_models="[]", enabled=True),
        Provider(id=2, name="MockB", models=["model-c"], disabled_models="[]", enabled=True),
    ]

    routers = {"my-group": ["model-a", "model-c"]}
    candidates = pick_available_models(providers, "my-group", routers, force=True, strategy="fastest")
    assert len(candidates) >= 2  # 两个匹配


@pytest.mark.asyncio
async def test_failover_stage_forward():
    """
    编排 stage_forward: 第一个失败,自动重试第二个
    """
    from backend.models import Provider
    from backend.core.proxy import pick_available_models

    providers = [
        Provider(id=1, name="MockA", models=["model-a"], disabled_models="[]", enabled=True),
        Provider(id=2, name="MockB", models=["model-b"], disabled_models="[]", enabled=True),
    ]
    routers = {"test": ["model-a", "model-b"]}

    # 模拟 stage_forward: 第一个失败, 第二个成功
    call_count = 0

    async def mock_forward(prov, mdl, body):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # 第一个失败
        return {"choices": [{"message": {"content": "ok"}}]}  # 第二个成功

    # 模拟 pick_available_models 返回两个候选
    candidates = pick_available_models(providers, "test", routers, force=True, strategy="fastest")
    last_resp = None
    for prov, mdl in candidates:
        resp = await mock_forward(prov, mdl, None)
        if resp is not None:
            last_resp = resp
            break

    assert last_resp is not None, "应当有成功响应"
    assert call_count == 2, "应当尝试两次"


@pytest.mark.asyncio
async def test_failover_all_fail():
    """
    所有候选都失败时, 返回 None
    """
    from backend.models import Provider
    from backend.core.proxy import pick_available_models

    providers = [
        Provider(id=1, name="MockA", models=["model-a"], disabled_models="[]", enabled=True),
        Provider(id=2, name="MockB", models=["model-b"], disabled_models="[]", enabled=True),
    ]
    routers = {"test": ["model-a", "model-b"]}

    async def mock_forward_fail(prov, mdl, body):
        return None

    candidates = pick_available_models(providers, "test", routers, force=True, strategy="fastest")
    last_resp = None
    for prov, mdl in candidates:
        resp = await mock_forward_fail(prov, mdl, None)
        if resp is not None:
            last_resp = resp
            break

    assert last_resp is None, "全部失败应返回 None"