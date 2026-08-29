# ============================================================
# 免费模型帖子搜索 — 核心逻辑测试
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core import free_model_search as fms
from backend.models import Base, get_session, FreeModelPost, FreeModelReport, FreeModelEvaluation


@pytest.fixture
def patched_engine(tmp_path, monkeypatch):
    """隔离 DB: 用临时 sqlite 替换 backend.models 的引擎"""
    db = tmp_path / "fms.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)
    yield engine


def test_hash_key_deterministic():
    a = fms._hash_key("github", "http://x.com/a", "Title")
    b = fms._hash_key("github", "http://x.com/a", "Title")
    assert a == b
    assert len(a) == 32
    # 不同输入应不同
    assert fms._hash_key("github", "http://x.com/b") != a


def test_extract_text_strips_tags_and_scripts():
    html = "<html><script>var x=1;</script><style>.a{}</style><body><p>Hello <b>World</b></p></body></html>"
    t = fms._extract_text(html)
    assert "Hello" in t and "World" in t
    assert "<" not in t
    assert "var x=1" not in t


def test_similar_jaccard():
    assert fms._similar("", "") == 1.0
    assert fms._similar("abc", "") == 0.0
    assert fms._similar("免费模型注册", "免费模型注册") == 1.0
    assert fms._similar("免费模型注册方法", "免费模型注册技巧") > 0.5
    assert fms._similar("完全不相关的内容xyz", "另一个风马牛的事qwe") < 0.3


def test_parse_llm_json():
    assert fms._parse_llm_json('{"feasibility_score":4,"truthfulness_score":3}') == {
        "feasibility_score": 4, "truthfulness_score": 3
    }
    # 包裹在多余文本里也能提取
    assert fms._parse_llm_json('前缀文字 {"feasibility_score":5,"truthfulness_score":2} 后缀') == {
        "feasibility_score": 5, "truthfulness_score": 2
    }
    assert fms._parse_llm_json("没有任何 json") is None


def test_deduplicate_and_store(patched_engine):
    s = get_session()
    try:
        raw = [
            {"source": "github", "url": "http://a.com/1", "title": "Post A", "content_snippet": "x"},
            {"source": "github", "url": "http://a.com/1", "title": "Post A", "content_snippet": "x"},  # 重复
            {"source": "github", "url": "", "title": ""},  # 缺 url/title -> 跳过
            {"source": "baidu", "url": "http://b.com/2", "title": "Post B", "content_snippet": "y"},
        ]
        new, added, skipped = fms.deduplicate_and_store(raw, s)
        assert added == 2
        assert skipped == 2  # 重复 1 条 + 空 url/title 1 条
        assert len(s.query(FreeModelPost).all()) == 2
        # 二次入库同样内容应全部跳过(2 条已存在 + 1 条重复 + 1 条空字段)
        _, added2, skipped2 = fms.deduplicate_and_store(raw, s)
        assert added2 == 0
        assert skipped2 == 4
    finally:
        s.close()


def test_generate_report_content(patched_engine):
    posts = [{"id": 1, "source": "github", "url": "http://a.com",
              "title": "T1", "author": "u", "content_snippet": "s"}]
    evals = [{"post_id": 1, "feasibility_score": 4, "truthfulness_score": 3, "reason": "ok"}]
    content = fms.generate_report_content("2026-08-29", posts, evals)
    assert "2026-08-29" in content
    assert "T1" in content
    assert "4.00" in content  # 平均可行性
    assert "3.00" in content  # 平均真实性
    assert "推荐帖子" in content


def test_run_free_model_search_end_to_end(tmp_path, monkeypatch, patched_engine):
    """mock 掉网络搜索引擎与 LLM 评估, 验证 搜索->入库->评估->报告文件 全链路"""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(fms, "REPORTS_DIR", reports_dir)
    monkeypatch.setattr(fms, "SEARCH_ENGINES", {
        "github": lambda kw, ses, limit: [
            {"source": "github", "url": "http://a.com/1", "title": "P1", "content_snippet": "s"}
        ],
    })
    monkeypatch.setattr(fms, "evaluate_posts_with_llm",
                        lambda posts, session: [
                            {"post_id": p["id"], "feasibility_score": 4,
                             "truthfulness_score": 3, "reason": "ok"} for p in posts
                        ])

    result = fms.run_free_model_search(
        keywords=["k"], sources=["github"], limit_per_source=5, evaluate=True
    )
    assert result["ok"] is True
    assert result["added"] == 1
    assert result["evaluated"] == 1

    # 报告文件 + 数据库记录都应落盘
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d")
    assert (reports_dir / f"{date_str}.md").exists()
    s = get_session()
    try:
        assert s.query(FreeModelPost).count() == 1
        assert s.query(FreeModelReport).count() == 1
    finally:
        s.close()
