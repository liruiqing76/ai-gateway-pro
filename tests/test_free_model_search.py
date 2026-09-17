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
        new, added, skipped, upgraded = fms.deduplicate_and_store(raw, s)
        assert added == 2
        assert skipped == 2  # 重复 1 条 + 空 url/title 1 条
        assert upgraded == 0
        assert len(s.query(FreeModelPost).all()) == 2
        # 二次入库同样内容应全部跳过(2 条已存在 + 1 条重复 + 1 条空字段)
        _, added2, skipped2, _ = fms.deduplicate_and_store(raw, s)
        assert added2 == 0
        assert skipped2 == 4
    finally:
        s.close()


def test_deduplicate_same_title_different_url(patched_engine):
    """同一篇文章带不同 URL(token 变化)再次出现 → 按标题签名去重，不再重复入库。"""
    s = get_session()
    try:
        raw1 = [
            {"source": "wechat", "url": "https://weixin.sogou.com/link?url=AAA&k=old",
             "title": "GPT-4o 免费申请教程", "content_snippet": "x"},
        ]
        _, added1, _, _ = fms.deduplicate_and_store(raw1, s)
        assert added1 == 1
        # 第二天同标题、URL 换成带新 token 的链接 → 应视为重复跳过
        raw2 = [
            {"source": "wechat", "url": "https://weixin.sogou.com/link?url=AAA&k=new-token",
             "title": "GPT-4o 免费申请教程", "content_snippet": "x"},
        ]
        _, added2, skipped2, upgraded2 = fms.deduplicate_and_store(raw2, s)
        assert added2 == 0
        assert skipped2 == 1  # 新链接自己仍是中转 → 无升级价值, 按重复跳过
        assert upgraded2 == 0
    finally:
        s.close()


def test_deduplicate_upgrades_stale_link(patched_engine):
    """库里已存在的帖子是中转死链时, 同标题的新直链应升级旧行而不是跳过。

    修复: 早期版本把搜狗/百度中转链接直接入库, token 过期后点开就是
    "链接已失效", 而标题去重会挡住同文章的新直链 → 死链永远留在报告里。
    """
    s = get_session()
    try:
        raw1 = [
            {"source": "wechat", "url": "https://weixin.sogou.com/link?url=deadToken",
             "title": "GPT-4o 免费申请教程", "content_snippet": "x"},
        ]
        _, added1, _, _ = fms.deduplicate_and_store(raw1, s)
        assert added1 == 1

        # 再次搜到同一篇, 这次是解析后的微信文章直链
        new_url = "https://mp.weixin.qq.com/s?src=11&timestamp=9&signature=ok"
        raw2 = [
            {"source": "wechat", "url": new_url,
             "title": "GPT-4o 免费申请教程", "content_snippet": "x"},
        ]
        _, added2, skipped2, upgraded2 = fms.deduplicate_and_store(raw2, s)
        assert added2 == 0
        assert skipped2 == 0
        assert upgraded2 == 1
        # 旧行 URL 已被升级为直链
        row = s.query(FreeModelPost).first()
        assert row.url == new_url
        assert row.hash_key == fms._hash_key("wechat", new_url, "GPT-4o 免费申请教程")
    finally:
        s.close()


def test_ignore_by_title_signature(tmp_path, monkeypatch):
    """忽略某篇帖子后，同一篇文章第二天换 URL 回来也能被识别为忽略。"""
    from backend.core.free_model_search import IGNORED_FILE
    monkeypatch.setattr(fms, "IGNORED_FILE", tmp_path / ".ignored_posts.json")

    # 忽略带标题的帖子
    assert fms.mark_post_as_ignored("a" * 32, "豆包大模型免费领取 2025-06-01") is True
    # 同一篇内容、不同 URL token + 标题细微差别(空格/标点) → 标题签名命中
    assert fms.is_ignored("b" * 32, " 豆包大模型免费领取!2025-06-01 ") is True
    # 不同内容 → 不命中
    assert fms.is_ignored("c" * 32, "Claude 注册教程") is False


def test_unmark_post_as_ignored_removes_title(tmp_path, monkeypatch):
    """取消忽略同时移除标题签名。"""
    from backend.core.free_model_search import IGNORED_FILE
    monkeypatch.setattr(fms, "IGNORED_FILE", tmp_path / ".ignored_posts.json")

    h = "a" * 32
    t = "免费模型注册教程"
    fms.mark_post_as_ignored(h, t)
    assert fms.is_ignored(h, t) is True
    assert fms.unmark_post_as_ignored(h, t) is True
    assert fms.is_ignored(h, t) is False
    assert fms.is_ignored(h, "") is False


def test_title_key_normalization():
    assert fms._title_key("  Hello   World! ") == "helloworld"
    assert fms._title_key("免费模型：申请教程（2025）") == "免费模型申请教程2025"
    assert fms._title_key("") == ""


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


# ============================================================
# 时效性: 超过 7 天(MAX_POST_AGE_DAYS)的帖子/报告不再返回
# ============================================================

def _post(published=None, fetched=None):
    class P:
        pass
    p = P()
    p.published_at = published
    p.fetched_at = fetched
    return p


def test_is_post_fresh():
    from datetime import datetime, timedelta
    now = datetime.now()
    # 5 天前发表 → 仍在时效内
    assert fms.is_post_fresh(_post(published=now - timedelta(days=5), fetched=now)) is True
    # 8 天前发表 → 超期(7 天窗口)
    assert fms.is_post_fresh(_post(published=now - timedelta(days=8), fetched=now)) is False
    # 无发表时间 → 退化为抓取时间(8 天前抓取 → 超期)
    assert fms.is_post_fresh(_post(published=None, fetched=now - timedelta(days=8))) is False
    # 无发表时间且近期抓取 → 保留
    assert fms.is_post_fresh(_post(published=None, fetched=now)) is True
    # 时间戳全缺 → 保留(无法判断时效)
    assert fms.is_post_fresh(_post()) is True
    # 带时区的时间戳不应崩溃
    tz_aware = (now - timedelta(days=3)).astimezone()
    assert fms.is_post_fresh(_post(published=tz_aware, fetched=now)) is True


def test_deduplicate_and_store_skips_stale_published(patched_engine):
    """明确发表时间超过 7 天时效窗口的帖子不入库(免费 API 已失效)"""
    from datetime import datetime, timedelta
    s = get_session()
    try:
        raw = [
            {"source": "github", "url": "http://a.com/stale", "title": "Stale Offer",
             "published_at": (datetime.now() - timedelta(days=8)).isoformat()},
            {"source": "github", "url": "http://a.com/edge", "title": "Edge Offer",
             "published_at": (datetime.now() - timedelta(days=6)).isoformat()},
            {"source": "github", "url": "http://a.com/fresh", "title": "Fresh Offer",
             "published_at": datetime.now().isoformat()},
            {"source": "github", "url": "http://a.com/nopub", "title": "No Date",
             "content_snippet": "x"},  # 无发表时间 → 保留
        ]
        new, added, skipped, _ = fms.deduplicate_and_store(raw, s)
        assert added == 3
        assert skipped == 1  # 8 天前的失效帖被丢弃
        titles = [p.title for p in s.query(FreeModelPost).all()]
        assert "Stale Offer" not in titles
        assert "Edge Offer" in titles and "Fresh Offer" in titles and "No Date" in titles
    finally:
        s.close()


def test_ignore_hides_post_from_report_api(tmp_path, monkeypatch):
    """API 层: 忽略某帖后, 报告详情和 Markdown 文件都不再包含该帖。"""
    db_path = tmp_path / "fms_api.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))
    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User, FreeModelPost, FreeModelReport
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(fms, "REPORTS_DIR", reports_dir)
    monkeypatch.setattr(fms, "IGNORED_FILE", reports_dir / ".ignored_posts.json")

    from datetime import datetime
    s = get_session()
    try:
        u = User(username="fms2", email="fms2@x.com", password_hash="x", api_key="sk-fms2")
        s.add(u)
        s.commit()
        uid = u.id
        today = datetime.now().strftime("%Y-%m-%d")
        p = FreeModelPost(
            source="github", url="http://a.com/1", title="Post A",
            content_snippet="x", hash_key=fms._hash_key("github", "http://a.com/1", "Post A"),
        )
        s.add(p)
        s.commit()
        p_hash = p.hash_key  # commit 后属性过期, 需在会话内取用
        p_id = p.id
        s.add(FreeModelReport(run_date=today, post_ids=[p_id], status="completed"))
        s.commit()
    finally:
        s.close()

    from backend.auth.security import create_access_token
    token = create_access_token(uid)
    from fastapi.testclient import TestClient
    from backend.main import app
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})

    # 忽略前: 报告详情包含该帖
    r = c.get(f"/api/free-model-search/report/{today}")
    assert r.status_code == 200
    assert len(r.json()["posts"]) == 1

    # 忽略后: 详情与 Markdown 文件都不再包含
    resp = c.post(f"/api/free-model-search/post/{p_hash}/ignore")
    assert resp.status_code == 200
    assert resp.json()["ignored"] is True
    r = c.get(f"/api/free-model-search/report/{today}")
    assert r.status_code == 200
    assert r.json()["posts"] == []
    rmd = c.get(f"/api/free-model-search/report/{today}/file")
    assert rmd.status_code == 200
    assert "Post A" not in rmd.text


def test_list_reports_filters_old_dates(tmp_path, monkeypatch):
    """报告列表只返回最近 7 天内的报告"""
    db_path = tmp_path / "fms_api.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))
    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User, FreeModelReport
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)

    from datetime import datetime, timedelta
    s = get_session()
    try:
        u = User(username="fms", email="fms@x.com", password_hash="x", api_key="sk-fms")
        s.add(u)
        s.commit()
        uid = u.id
        today = datetime.now().strftime("%Y-%m-%d")
        old = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        s.add(FreeModelReport(run_date=today, post_ids=[]))
        s.add(FreeModelReport(run_date=old, post_ids=[]))
        s.commit()
    finally:
        s.close()

    from backend.auth.security import create_access_token
    token = create_access_token(uid)
    from fastapi.testclient import TestClient
    from backend.main import app
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    r = c.get("/api/free-model-search/reports")
    assert r.status_code == 200
    dates = [d["run_date"] for d in r.json()["data"]]
    assert today in dates
    assert old not in dates, "超过1个月的报告不应返回"


# ============================================================
# 中转链接解析: 搜狗微信/百度 /link → 最终直链(防"链接已失效")
# ============================================================

class _FakeResp:
    def __init__(self, text="", status_code=200, url=None):
        self.text = text
        self.status_code = status_code
        self.url = url


def test_resolve_search_url_passthrough_for_normal_url():
    # 非中转域名不发起请求, 原样返回
    assert fms._resolve_search_url("https://www.toutiao.com/a1", None) == "https://www.toutiao.com/a1"
    assert fms._resolve_search_url("https://gitee.com/a", None) == "https://gitee.com/a"
    assert fms._resolve_search_url("", None) is None
    assert fms._resolve_search_url("not-a-url", None) is None


def test_resolve_search_url_wechat_js_assembly():
    class FakeSession:
        def get(self, url, **kw):
            assert url.startswith("https://weixin.sogou.com/link")
            assert kw["headers"].get("Referer") == "https://weixin.sogou.com/"
            return _FakeResp(status_code=200, text=(
                "<script>var url='';url+='https://mp.';url+='weixin.qq.c';"
                "url+='om/s?src=11&amp;timestamp=1234&amp;ver=1&amp;signature=abc';</script>"
            ))
    got = fms._resolve_search_url("https://weixin.sogou.com/link?url=xxx", FakeSession())
    assert got == "https://mp.weixin.qq.com/s?src=11&timestamp=1234&ver=1&signature=abc"
    # &timestamp 不能被误转成 ×tamp
    assert "×tamp" not in got


def test_resolve_search_url_wechat_raw_ampersand():
    """搜狗 JS 片段里的 & 时常未转义, 直接替换 &amp; 即可, 不能走 html.unescape"""
    class FakeSession:
        def get(self, url, **kw):
            return _FakeResp(status_code=200, text=(
                "<script>var url='';url+='https://mp.weixin.qq.com/s?src=11';"
                "url+='&timestamp=1788366074';url+='&ver=6942';</script>"
            ))
    got = fms._resolve_search_url("https://weixin.sogou.com/link?url=xxx", FakeSession())
    assert got == "https://mp.weixin.qq.com/s?src=11&timestamp=1788366074&ver=6942"
    assert "×" not in got


def test_resolve_search_url_baidu_302():
    class FakeSession:
        def get(self, url, **kw):
            assert "baidu.com/link" in url
            return _FakeResp(status_code=200, url="https://www.jianshu.com/p/2ec66266fce7")
    assert fms._resolve_search_url("http://www.baidu.com/link?url=abc", FakeSession()) == \
        "https://www.jianshu.com/p/2ec66266fce7"


def test_resolve_search_url_failures_return_none():
    # 搜狗反爬页没有 url += 片段
    class BlockedSogou:
        def get(self, url, **kw):
            return _FakeResp(status_code=200, text="<html>antispider</html>")
    assert fms._resolve_search_url("https://weixin.sogou.com/link?url=x", BlockedSogou()) is None

    # 搜狗拼出的还是搜狗自身链接 → 拒绝
    class LoopSogou:
        def get(self, url, **kw):
            return _FakeResp(status_code=200, text="<script>url+='https://weixin.sogou.com/';</script>")
    assert fms._resolve_search_url("https://weixin.sogou.com/link?url=x", LoopSogou()) is None

    # 百度停在验证页
    class BaiduVerify:
        def get(self, url, **kw):
            return _FakeResp(status_code=200, url="https://www.baidu.com/s?wd=verify")
    assert fms._resolve_search_url("http://www.baidu.com/link?url=x", BaiduVerify()) is None

    # 请求异常
    class Boom:
        def get(self, url, **kw):
            raise RuntimeError("boom")
    assert fms._resolve_search_url("https://weixin.sogou.com/link?url=x", Boom()) is None


WECHAT_LIST_HTML = (
    '<div class="txt-box">'
    '<h3><a href="/link?url=abc&amp;token=xyz">免费模型申请教程</a></h3>'
    '<p class="txt-info">摘要内容</p>'
    '<span class="all-time-y2">某公众号</span>'
    '</div></div>'
)
WECHAT_LINK_JS = (
    "<script>var url='';url+='https://mp.';url+='weixin.qq.c';"
    "url+='om/s?src=11&amp;timestamp=9&amp;ver=1&amp;signature=ok';</script>"
)


def test_search_wechat_stores_resolved_url():
    class FakeSession:
        def get(self, url, **kw):
            if url.startswith("https://weixin.sogou.com/weixin"):
                return _FakeResp(status_code=200, text=WECHAT_LIST_HTML)
            return _FakeResp(status_code=200, text=WECHAT_LINK_JS)
    results = fms.search_wechat("模型", FakeSession(), limit=5)
    assert results
    assert all("weixin.sogou.com" not in r["url"] for r in results)
    assert results[0]["url"].startswith("https://mp.weixin.qq.com/")


def test_search_wechat_drops_unresolvable_links():
    class FakeSession:
        def get(self, url, **kw):
            if url.startswith("https://weixin.sogou.com/weixin"):
                return _FakeResp(status_code=200, text=WECHAT_LIST_HTML)
            return _FakeResp(status_code=200, text="<html>antispider</html>")
    assert fms.search_wechat("模型", FakeSession(), limit=5) == []


def test_refresh_dead_links_updates_stored_url(patched_engine, monkeypatch):
    from datetime import datetime
    dead = "https://weixin.sogou.com/link?url=dead"
    s = get_session()
    s.add(FreeModelPost(
        source="wechat", url=dead, title="旧帖子",
        fetched_at=datetime.now(), hash_key=fms._hash_key("wechat", dead, "旧帖子"),
    ))
    s.commit()
    s.close()

    monkeypatch.setattr(fms, "_resolve_search_url",
                        lambda url, ses: "https://mp.weixin.qq.com/s?new" if "sogou.com" in url else None)
    s = get_session()
    updated, failed = fms._refresh_dead_links(s, limit=10)
    assert updated == 1
    assert failed == 0
    row = s.query(FreeModelPost).first()
    assert row.url == "https://mp.weixin.qq.com/s?new"
    assert row.hash_key == fms._hash_key("wechat", "https://mp.weixin.qq.com/s?new", "旧帖子")
    s.close()


def test_refresh_dead_links_skips_unresolvable(patched_engine, monkeypatch):
    from datetime import datetime
    dead = "https://weixin.sogou.com/link?url=dead"
    s = get_session()
    s.add(FreeModelPost(
        source="wechat", url=dead, title="旧帖子",
        fetched_at=datetime.now(), hash_key=fms._hash_key("wechat", dead, "旧帖子"),
    ))
    s.commit()
    s.close()

    monkeypatch.setattr(fms, "_resolve_search_url", lambda url, ses: None)
    s = get_session()
    updated, failed = fms._refresh_dead_links(s, limit=10)
    assert updated == 0
    assert failed == 1
    assert s.query(FreeModelPost).first().url == dead  # 解析失败保留原值
    s.close()


def test_refresh_dead_links_skips_duplicate_destination(patched_engine, monkeypatch):
    """多条死链解析到同一落地页时不撞 (source,url) 唯一约束, 后到者跳过保留原值"""
    from datetime import datetime
    s = get_session()
    for i, u in enumerate(["https://weixin.sogou.com/link?url=dead1",
                           "https://weixin.sogou.com/link?url=dead2"]):
        s.add(FreeModelPost(
            source="wechat", url=u, title=f"旧帖子{i}",
            fetched_at=datetime.now(), hash_key=fms._hash_key("wechat", u, f"旧帖子{i}"),
        ))
    s.commit()
    s.close()

    monkeypatch.setattr(fms, "_resolve_search_url",
                        lambda url, ses: "https://post.smzdm.com/p/avge7nd9/")
    s = get_session()
    updated, failed = fms._refresh_dead_links(s, limit=10)
    assert updated == 1  # 先处理的那条升级成功
    assert failed == 1   # 后处理的那条目标被占用, 保留原值
    urls = [r.url for r in s.query(FreeModelPost).all()]
    assert urls.count("https://post.smzdm.com/p/avge7nd9/") == 1
    assert sum(1 for u in urls if u.startswith("https://weixin.sogou.com/")) == 1
    s.close()
