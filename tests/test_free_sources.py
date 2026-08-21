# ============================================================
# 免费源自动同步测试
# ============================================================
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from backend.core import free_sources

# ---- 构造临时模板池 / 源配置 ----
LOCAL = [
    {
        "name": "NVIDIA",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models": ["meta/llama-3.2-11b-vision-instruct", "z-ai/glm-5.2"],
        "free_only": True,
    }
]

EXTERNAL = [
    {
        "name": "NVIDIA",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models": ["meta/llama-3.2-90b-vision-instruct"],  # 补一个新模型
    },
    {
        "name": "新发现免费源X",
        "base_url": "https://free-x.example.com/v1",
        "models": ["x-org/model-1", "x-org/model-2"],
        "free_only": True,
    },
]


@pytest.fixture
def workdir(tmp_path):
    templates = tmp_path / "provider_templates.json"
    templates.write_text(json.dumps(LOCAL, ensure_ascii=False), encoding="utf-8")
    sources = tmp_path / "free_sources.json"
    sources.write_text(
        json.dumps({"sources": [
            {"name": "外部目录", "url": "https://fake.example.com/list.json", "enabled": True},
        ], "meta": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return templates, sources


def test_sync_adds_new_and_merges_models(workdir, monkeypatch):
    """外部源里的新厂商自动入池, 已有厂商补新模型; 本地条目不受影响。"""
    templates, sources = workdir
    monkeypatch.setattr(free_sources, "fetch_source_list", lambda url: EXTERNAL)

    r = free_sources.sync_free_sources(templates_path=templates, sources_path=sources)

    assert r["ok"] is True
    assert "新发现免费源X" in r["added"]
    assert "NVIDIA" in r["updated"]

    pool = json.loads(templates.read_text(encoding="utf-8"))
    names = {t["name"] for t in pool}
    assert names == {"NVIDIA", "新发现免费源X"}
    nvidia = next(t for t in pool if t["name"] == "NVIDIA")
    assert "meta/llama-3.2-90b-vision-instruct" in nvidia["models"]  # 已合并
    assert "z-ai/glm-5.2" in nvidia["models"]  # 本地模型保留
    x = next(t for t in pool if t["name"] == "新发现免费源X")
    assert x["base_url"] == "https://free-x.example.com/v1"
    assert set(x["models"]) == {"x-org/model-1", "x-org/model-2"}


def test_sync_idempotent(workdir, monkeypatch):
    """重复同步不重复追加同名字段/模型。"""
    templates, sources = workdir
    monkeypatch.setattr(free_sources, "fetch_source_list", lambda url: EXTERNAL)

    free_sources.sync_free_sources(templates_path=templates, sources_path=sources)
    r2 = free_sources.sync_free_sources(templates_path=templates, sources_path=sources)

    assert r2["added"] == []
    assert r2["updated"] == []
    pool = json.loads(templates.read_text(encoding="utf-8"))
    assert len(pool) == 2  # 没有重复添加


def test_failed_source_is_isolated(workdir, monkeypatch):
    """外部源拉取失败只记录, 本地模板池不受影响、不中断其它源。"""
    templates, sources = workdir
    calls = [0]

    def flaky(url):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("网络错误")
        return EXTERNAL

    monkeypatch.setattr(free_sources, "fetch_source_list", flaky)

    r = free_sources.sync_free_sources(templates_path=templates, sources_path=sources)
    assert r["failed_sources"] and "网络错误" in r["failed_sources"][0]

    r2 = free_sources.sync_free_sources(templates_path=templates, sources_path=sources)
    assert r2["failed_sources"] == []
    assert "新发现免费源X" in r2["added"]


def test_sources_meta_persisted(workdir, monkeypatch):
    """同步后源配置的 meta 被写入上次结果, 供 GET API 展示。"""
    templates, sources = workdir
    monkeypatch.setattr(free_sources, "fetch_source_list", lambda url: EXTERNAL)

    free_sources.sync_free_sources(templates_path=templates, sources_path=sources)
    cfg = json.loads(sources.read_text(encoding="utf-8"))
    assert cfg["meta"]["last_sync_at"]
    assert "新发现免费源X" in cfg["meta"]["last_result"]["added"]