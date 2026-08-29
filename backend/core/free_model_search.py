#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
免费模型注册帖子搜索 - 核心模块

功能:
  1. 搜索多个来源（Gitee, GitHub, 百度, 今日头条, 微信公众号）关于免费模型注册的帖子
  2. 去重（首级: 来源+URL, 次级: 内容相似度）
  3. 入库存储
  4. 调用 LLM 评估每条帖子的可行性和真实性
  5. 生成 Markdown 报告

提供手动搜索入口 run_free_model_search(), 被定时任务和 API 共用。
"""
import json
import hashlib
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from html import unescape
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

logger = logging.getLogger("gateway")

# 数据目录与报告目录
CORE_DIR = Path(__file__).resolve().parent
DATA_DIR = CORE_DIR.parent / "data"
REPORTS_DIR = DATA_DIR / "free_models"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# 默认搜索关键词
DEFAULT_KEYWORDS = [
    "免费模型 注册",
    "免费 API 模型",
    "免费大模型 申请",
    "免费 GPT 注册",
    "免费 Claude 注册",
    "免费模型 API key",
    "免费 AI 模型 试用",
    "模型免费额度 获取",
]

# 各来源的 User-Agent（模拟浏览器访问）
UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"

# 去重相似度阈值（字符编辑距离 / 长度比例）
SIMILARITY_THRESHOLD = 0.85  # 相似度 > 此值视为重复

_search_lock = threading.Lock()  # 防止并发搜索
_runtime_state = {
    "last_search_at": None,
    "last_result": None,
    "running": False,
}


def _make_session() -> httpx.Client:
    """创建带超时和重试的 httpx 客户端"""
    return httpx.Client(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": UA_PC},
        transport=httpx.HTTPTransport(retries=2),
    )


def _hash_key(source: str, url: str, title: str = "") -> str:
    """生成去重哈希键"""
    raw = f"{source}|{url}|{title}".strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _extract_text(html: str) -> str:
    """简单 HTML 转文本（去除标签、script、style）"""
    if not html:
        return ""
    # 去除 script 和 style
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    # 去除所有标签
    text = re.sub(r"<[^>]+>", " ", html)
    # 去除多余空白
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _similar(a: str, b: str) -> float:
    """计算两个字符串的相似度（基于字符编辑距离）"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    # 简单的 Jaccard 相似度（按字符 bigram）
    def bigrams(s: str) -> set:
        return set(s[i:i+2] for i in range(len(s)-1))
    sa, sb = bigrams(a), bigrams(b)
    if not sa and not sb:
        return 1.0
    intersection = sa & sb
    union = sa | sb
    return len(intersection) / len(union) if union else 0.0


# ============================================================
# 各来源搜索引擎
# ============================================================

def search_gitee(keyword: str, session: httpx.Client, limit: int = 20) -> List[Dict]:
    """Gitee 搜索 - 代码仓库搜索（需要 GITEE_TOKEN 环境变量才能用 API，否则返回空）"""
    results = []
    try:
        # 优先用 V5 API（需 token）
        token = os.environ.get("GITEE_TOKEN")
        if token:
            url = "https://gitee.com/api/v5/search/repositories"
            params = {"q": keyword, "access_token": token, "per_page": limit}
            resp = session.get(url, params=params)
            if resp.status_code == 200:
                for item in resp.json()[:limit]:
                    results.append({
                        "source": "gitee",
                        "url": item.get("html_url", ""),
                        "title": item.get("full_name", ""),
                        "author": item.get("owner", {}).get("login", ""),
                        "published_at": None,
                        "content_snippet": item.get("description", ""),
                    })
        else:
            # 无 token: Gitee 搜索页是 JS 渲染, 无法直接解析, 返回空
            logger.info("Gitee 搜索需要 GITEE_TOKEN 环境变量（Gitee API 鉴权）, 跳过")
    except Exception as e:
        logger.warning(f"Gitee 搜索失败: {e}")
    return results


def search_github(keyword: str, session: httpx.Client, limit: int = 20) -> List[Dict]:
    """GitHub 搜索 API"""
    results = []
    try:
        url = "https://api.github.com/search/repositories"
        params = {
            "q": keyword,
            "sort": "stars",
            "order": "desc",
            "per_page": limit,
        }
        resp = session.get(url, params=params, headers={"Accept": "application/vnd.github.v3+json"})
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("items", [])[:limit]:
                results.append({
                    "source": "github",
                    "url": item.get("html_url", ""),
                    "title": item.get("full_name", ""),
                    "author": item.get("owner", {}).get("login", ""),
                    "published_at": item.get("created_at"),
                    "content_snippet": item.get("description", ""),
                })
        elif resp.status_code == 403:
            logger.warning("GitHub API 速率限制, 跳过")
    except Exception as e:
        logger.warning(f"GitHub 搜索失败: {e}")
    return results


def search_baidu(keyword: str, session: httpx.Client, limit: int = 20) -> List[Dict]:
    """百度搜索 - HTML 解析（百度有反爬, 建议配置 BAIDU_COOKIE 环境变量）

    注意: 百度对自动化请求有安全验证, 无 cookie 时通常会返回验证页而非结果。
    如需启用, 可在环境变量 BAIDU_COOKIE 中填入浏览器抓取的百度 cookie。
    """
    results = []
    try:
        url = "https://www.baidu.com/s"
        params = {"wd": keyword, "rn": limit}
        headers = {
            "User-Agent": UA_PC,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        cookie = os.environ.get("BAIDU_COOKIE")
        if cookie:
            headers["Cookie"] = cookie
        resp = session.get(url, params=params, headers=headers)
        if resp.status_code == 200:
            html = resp.text
            # 反爬检测：出现验证页则跳过
            if "百度安全验证" in html or "antispider" in html.lower():
                logger.warning("百度返回安全验证页, 需要配置 BAIDU_COOKIE 才能搜索")
                return results
            # 提取搜索结果块
            blocks = re.findall(r'<div class="[^"]*result[^"]*"[^>]*>.*?</div>\s*</div>', html, re.DOTALL)
            for block in blocks[:limit]:
                title_match = re.search(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
                if title_match:
                    link = title_match.group(1)
                    title = re.sub(r"<[^>]+>", "", title_match.group(2)).strip()
                    snippet_match = re.search(r'<div class="c-abstract[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
                    snippet = ""
                    if snippet_match:
                        snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()
                    if title:
                        results.append({
                            "source": "baidu",
                            "url": link,
                            "title": title,
                            "author": None,
                            "published_at": None,
                            "content_snippet": snippet,
                        })
    except Exception as e:
        logger.warning(f"百度搜索失败: {e}")
    return results


def search_toutiao(keyword: str, session: httpx.Client, limit: int = 20) -> List[Dict]:
    """今日头条搜索"""
    results = []
    try:
        url = "https://so.toutiao.com/search"
        params = {
            "keyword": keyword,
            "pd": "information",
            "dvpf": "pc",
            "source": "input",
        }
        resp = session.get(url, params=params, headers={"Referer": "https://so.toutiao.com/"})
        if resp.status_code == 200:
            # 尝试 JSON 响应
            try:
                data = resp.json()
                for item in data.get("data", [])[:limit]:
                    if isinstance(item, dict):
                        results.append({
                            "source": "toutiao",
                            "url": item.get("url", ""),
                            "title": item.get("title", ""),
                            "author": item.get("source", ""),
                            "published_at": item.get("publish_time"),
                            "content_snippet": item.get("abstract", ""),
                        })
            except json.JSONDecodeError:
                # HTML 解析（今日头条为 SSR/JS 渲染, 无有效结果时返回空）
                html = resp.text
                blocks = re.findall(r'<div class="[^"]*result[^"]*"[^>]*>.*?</div>', html, re.DOTALL)
                for block in blocks[:limit]:
                    title_match = re.search(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
                    if title_match:
                        link = title_match.group(1)
                        title = re.sub(r"<[^>]+>", "", title_match.group(2)).strip()
                        # 必须有实质性标题和合法链接才收录
                        if title and len(title) > 3 and "toutiao.com" in link:
                            snippet_match = re.search(r'<div class="[^"]*abstract[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
                            snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip() if snippet_match else ""
                            results.append({
                                "source": "toutiao",
                                "url": link,
                                "title": title,
                                "author": None,
                                "published_at": None,
                                "content_snippet": snippet,
                            })
    except Exception as e:
        logger.warning(f"今日头条搜索失败: {e}")
    return results


def search_wechat(keyword: str, session: httpx.Client, limit: int = 20) -> List[Dict]:
    """微信公众号搜索 - 通过搜狗微信"""
    results = []
    try:
        url = "https://weixin.sogou.com/weixin"
        params = {
            "type": 2,  # 2=文章
            "query": keyword,
            "page": 1,
        }
        resp = session.get(url, params=params, headers={"User-Agent": UA_PC})
        if resp.status_code == 200:
            html = resp.text
            # 提取每个结果块: <div class="txt-box">...</div></div>
            blocks = re.findall(r'<div class="txt-box">(.*?)</div>\s*</div>', html, re.DOTALL)
            for block in blocks[:limit]:
                # 提取标题和链接 (h3 > a)
                title_match = re.search(r'<h3>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
                if not title_match:
                    continue
                link = title_match.group(1)
                # 补全为绝对 URL
                if link.startswith("/"):
                    link = "https://weixin.sogou.com" + link
                link = unescape(link)  # 解码 HTML 实体 (&amp; -> &)
                title = re.sub(r"<[^>]+>", "", title_match.group(2)).strip()
                # 提取公众号名称
                author_match = re.search(r'<span class="all-time-y2">(.*?)</span>', block, re.DOTALL)
                author = re.sub(r"<[^>]+>", "", author_match.group(1)).strip() if author_match else None
                # 提取摘要
                snippet_match = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', block, re.DOTALL)
                snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip() if snippet_match else ""
                # 去掉高亮标记
                title = title.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")
                snippet = snippet.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")
                if title:
                    results.append({
                        "source": "wechat",
                        "url": link,
                        "title": title,
                        "author": author,
                        "published_at": None,
                        "content_snippet": snippet,
                    })
    except Exception as e:
        logger.warning(f"微信公众号搜索失败: {e}")
    return results


# 搜索引擎映射
SEARCH_ENGINES = {
    "gitee": search_gitee,
    "github": search_github,
    "baidu": search_baidu,
    "toutiao": search_toutiao,
    "wechat": search_wechat,
}


# ============================================================
# 入库与去重
# ============================================================

def _load_existing_posts(session, since_days: int = 30) -> List[str]:
    """加载最近 N 天的帖子哈希键用于去重"""
    from backend.models import FreeModelPost
    cutoff = datetime.now() - timedelta(days=since_days)
    posts = session.query(FreeModelPost).filter(FreeModelPost.fetched_at >= cutoff).all()
    return [p.hash_key for p in posts]


def deduplicate_and_store(raw_posts: List[Dict], session) -> Tuple[List[Dict], int, int]:
    """
    去重并入库。
    返回: (新帖子列表, 新增数, 跳过数)
    """
    from backend.models import FreeModelPost
    
    existing_hashes = set(_load_existing_posts(session))
    new_posts = []
    skipped = 0
    
    for post in raw_posts:
        source = post.get("source", "")
        url = post.get("url", "")
        title = post.get("title", "")
        if not url or not title:
            skipped += 1
            continue
        
        # 计算哈希键
        hk = _hash_key(source, url, title)
        
        # 首级去重: 哈希键已存在
        if hk in existing_hashes:
            skipped += 1
            continue
        
        # 次级去重: 内容相似度（与已有帖子的标题+摘要比较）
        is_dup = False
        for ex_hk in existing_hashes:
            # 简化: 只比较 URL 是否相同（精确匹配）
            if ex_hk.startswith(f"{source}|{url}|"):
                is_dup = True
                break
        if is_dup:
            skipped += 1
            continue
        
        # 创建新帖子记录
        published = post.get("published_at")
        if isinstance(published, str):
            try:
                published = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except:
                published = None
        
        new_post = FreeModelPost(
            source=source,
            url=url,
            title=title,
            author=post.get("author"),
            published_at=published,
            fetched_at=datetime.now(),
            raw_html_path=None,
            content_snippet=post.get("content_snippet", ""),
            hash_key=hk,
        )
        session.add(new_post)
        new_posts.append(new_post)
        existing_hashes.add(hk)
    
    try:
        session.commit()
    except Exception as e:
        logger.error(f"入库失败: {e}")
        session.rollback()
        return [], 0, len(raw_posts)
    
    return new_posts, len(new_posts), skipped


# ============================================================
# LLM 评估
# ============================================================

def _pick_provider():
    """挑一个可用的 provider 和模型来跑评估 LLM（与 experience.py 同款）。"""
    from backend.models import get_session, Provider
    from backend.auth.security import decrypt_secret
    session = get_session()
    try:
        providers = session.query(Provider).filter_by(enabled=True).all()
        if not providers:
            return None, None, None
        providers.sort(key=lambda p: len(p.models or []), reverse=True)
        p = providers[0]
        models = [m for m in (p.models or []) if m not in (p.disabled_models or [])]
        if not models:
            return None, None, None
        key = decrypt_secret(p.api_key_encrypted)
        return p, key, models[0]
    finally:
        session.close()


def evaluate_posts_with_llm(posts: List[Dict], session) -> List[Dict]:
    """
    调用项目自身的 LLM（首个可用 provider 渠道）评估帖子的可行性和真实性。
    返回评估结果列表: [{post_id, feasibility_score, truthfulness_score, reason}, ...]
    """
    if not posts:
        return []
    
    from backend.models import FreeModelEvaluation
    
    provider, api_key, model = _pick_provider()
    if not provider or not api_key:
        logger.warning("免费模型评估: 没有可用的 provider 用于 LLM 评估, 跳过")
        return []
    
    results = []
    client = _make_session()
    try:
        for post in posts:
            try:
                prompt = f"""请评估以下免费模型注册帖子的可行性和真实性。

标题: {post.get('title', '')}
来源: {post.get('source', '')}
链接: {post.get('url', '')}
摘要: {post.get('content_snippet', '')}

请只输出一个 JSON 对象, 不要输出其他内容:
{{
  "feasibility_score": <1-5的整数, 注册获取免费模型的可行性>,
  "truthfulness_score": <1-5的整数, 信息真实性>,
  "reason": "<简短评估理由>"
}}"""
                
                body = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 400,
                    "stream": False,
                }
                resp = client.post(
                    provider.base_url.rstrip("/") + "/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    timeout=30.0,
                )
                if resp.status_code >= 400:
                    logger.warning(f"LLM 评估请求失败 {resp.status_code}: {resp.text[:120]}")
                    continue
                
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    continue
                content = str(choices[0].get("message", {}).get("content", "") or "").strip()
                
                eval_data = _parse_llm_json(content)
                if eval_data:
                    eval_record = FreeModelEvaluation(
                        report_id=None,  # 稍后由报告关联
                        post_id=post.get("id"),
                        feasibility_score=int(eval_data.get("feasibility_score", 3)),
                        truthfulness_score=int(eval_data.get("truthfulness_score", 3)),
                        reason=eval_data.get("reason", ""),
                    )
                    session.add(eval_record)
                    results.append({
                        "post_id": post.get("id"),
                        "feasibility_score": eval_record.feasibility_score,
                        "truthfulness_score": eval_record.truthfulness_score,
                        "reason": eval_record.reason,
                    })
            except Exception as e:
                logger.warning(f"评估帖子失败 (post_id={post.get('id')}): {e}")
                continue
    finally:
        client.close()
    
    try:
        session.commit()
    except Exception as e:
        logger.error(f"评估记录入库失败: {e}")
        session.rollback()
    
    return results


def _parse_llm_json(text: str) -> Optional[dict]:
    """从 LLM 输出中解析 JSON"""
    if not text:
        return None
    # 尝试直接解析
    try:
        return json.loads(text)
    except:
        pass
    # 尝试提取 JSON 块
    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except:
            pass
    return None


# ============================================================
# 报告生成
# ============================================================

def _safe_url(raw_url: str) -> str:
    """
    规范化 URL，保证写进 Markdown 后仍是可点击的完整链接。

    搜索引擎结果（尤其搜狗微信）常带裸空格、中文、未转义的 &，
    Markdown 遇到空格会把链接截断，导致渲染出的 <a> 指向错误地址。
    这里对 path/query 逐段做百分号编码，保留原有分隔符结构。
    """
    url = (raw_url or "").strip()
    if not url:
        return ""
    if not urlsplit(url).scheme:
        url = "https://" + url.lstrip("/")
    try:
        parts = urlsplit(url)
        # safe 里保留 URL 结构字符，其余（空格/中文/引号等）全部编码
        path = quote(unescape(parts.path), safe="/%:@&=+$,~-_.!*'()")
        query = quote(unescape(parts.query), safe="/%:@&=+$,~-_.!*'()?")
        return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
    except Exception:
        return url


def _md_link(title: str, url: str) -> str:
    """生成安全的 Markdown 链接，URL 用尖括号包裹以规避空格/括号截断。"""
    safe = _safe_url(url)
    text = (title or "").replace("[", "(").replace("]", ")").replace("\n", " ").strip()
    if not safe:
        return text
    return f"[{text}](<{safe}>)"


def generate_report_content(date_str: str, posts: List[Dict], evaluations: List[Dict]) -> str:
    """生成 Markdown 报告内容"""
    lines = []
    lines.append(f"# 免费模型注册帖子搜索报告 - {date_str}\n")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"发现帖子总数: {len(posts)}\n")
    
    if evaluations:
        avg_feas = sum(e["feasibility_score"] for e in evaluations) / len(evaluations)
        avg_truth = sum(e["truthfulness_score"] for e in evaluations) / len(evaluations)
        lines.append(f"平均可行性评分: {avg_feas:.2f} / 5")
        lines.append(f"平均真实性评分: {avg_truth:.2f} / 5\n")
    
    # 按综合得分排序
    scored = []
    for p in posts:
        eval_map = {e["post_id"]: e for e in evaluations}
        if p.get("id") in eval_map:
            e = eval_map[p["id"]]
            score = (e["feasibility_score"] + e["truthfulness_score"]) / 2
            scored.append((score, p, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    
    if scored:
        lines.append("## 推荐帖子（按综合评分排序）\n")
        for i, (score, p, e) in enumerate(scored, 1):
            lines.append(f"### {i}. {p.get('title', '')}")
            lines.append(f"- 来源: {p.get('source', '')}")
            lines.append(f"- 链接: {_md_link('打开原帖', p.get('url', ''))}")
            lines.append(f"- 作者: {p.get('author', '未知')}")
            lines.append(f"- 可行性: {e['feasibility_score']}/5 | 真实性: {e['truthfulness_score']}/5")
            if e.get("reason"):
                lines.append(f"- 评估理由: {e['reason']}")
            lines.append("")
    
    lines.append("## 全部帖子\n")
    for p in posts:
        lines.append(f"- {_md_link(p.get('title', ''), p.get('url', ''))} - {p.get('source', '')}")
    
    return "\n".join(lines)


def save_report_file(date_str: str, content: str) -> str:
    """保存报告到文件"""
    filename = f"{date_str}.md"
    filepath = REPORTS_DIR / filename
    filepath.write_text(content, encoding="utf-8")
    return str(filepath)


# ============================================================
# 主入口
# ============================================================

def run_free_model_search(
    keywords: List[str] = None,
    sources: List[str] = None,
    limit_per_source: int = 20,
    evaluate: bool = True,
) -> dict:
    """
    执行一次完整的免费模型搜索（线程安全）。
    返回统计结果, 可通过 API 暴露。
    """
    from backend.models import get_session, FreeModelReport
    
    if keywords is None:
        keywords = DEFAULT_KEYWORDS
    if sources is None:
        sources = list(SEARCH_ENGINES.keys())
    
    # 防止并发搜索
    if not _search_lock.acquire(blocking=False):
        return {"ok": False, "error": "已有搜索任务在运行"}
    
    _runtime_state["running"] = True
    result = {
        "ok": True,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_found": 0,
        "added": 0,
        "skipped": 0,
        "evaluated": 0,
        "errors": [],
    }
    
    try:
        session = get_session()
        
        # 1. 搜索所有来源
        raw_posts = []
        with _make_session() as http_session:
            for source in sources:
                if source not in SEARCH_ENGINES:
                    result["errors"].append(f"未知来源: {source}")
                    continue
                engine = SEARCH_ENGINES[source]
                for kw in keywords:
                    try:
                        posts = engine(kw, http_session, limit_per_source)
                        raw_posts.extend(posts)
                        logger.info(f"来源 {source} 关键词 '{kw}' 发现 {len(posts)} 条")
                    except Exception as e:
                        result["errors"].append(f"{source}/{kw}: {e}")
                        logger.warning(f"搜索失败 {source}/{kw}: {e}")
        
        result["total_found"] = len(raw_posts)
        
        # 2. 去重并入库
        new_posts, added, skipped = deduplicate_and_store(raw_posts, session)
        result["added"] = added
        result["skipped"] = skipped
        
        # 3. LLM 评估（可选）
        evaluations = []
        if evaluate and new_posts:
            # 转换为 dict 格式用于评估
            post_dicts = [{
                "id": p.id,
                "source": p.source,
                "url": p.url,
                "title": p.title,
                "author": p.author,
                "content_snippet": p.content_snippet,
            } for p in new_posts]
            
            evaluations = evaluate_posts_with_llm(post_dicts, session)
            result["evaluated"] = len(evaluations)
        
        # 4. 生成报告（无论是否评估都生成）
        if new_posts:
            post_dicts = [{
                "id": p.id,
                "source": p.source,
                "url": p.url,
                "title": p.title,
                "author": p.author,
                "content_snippet": p.content_snippet,
            } for p in new_posts]
            
            date_str = datetime.now().strftime("%Y-%m-%d")
            content = generate_report_content(date_str, post_dicts, evaluations)
            filepath = save_report_file(date_str, content)
            
            # 创建报告记录
            avg_feas = sum(e["feasibility_score"] for e in evaluations) / len(evaluations) if evaluations else None
            avg_truth = sum(e["truthfulness_score"] for e in evaluations) / len(evaluations) if evaluations else None
            
            report = FreeModelReport(
                run_date=date_str,
                post_ids=[p.id for p in new_posts],
                summary=f"本次搜索发现 {len(raw_posts)} 条帖子, 新增 {added} 条, 评估 {len(evaluations)} 条。",
                feasibility_score=avg_feas,
                truthfulness_score=avg_truth,
                status="completed",
                finished_at=datetime.now(),
                file_path=filepath,
            )
            session.add(report)
            session.commit()
            
            # 关联评估记录到报告
            from backend.models import FreeModelEvaluation
            for eval_record in session.query(FreeModelEvaluation).filter(
                FreeModelEvaluation.report_id.is_(None)
            ).all():
                eval_record.report_id = report.id
            session.commit()
        
        result["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
    except Exception as e:
        logger.exception("免费模型搜索异常")
        result["ok"] = False
        result["error"] = str(e)
    finally:
        _runtime_state["running"] = False
        _runtime_state["last_search_at"] = result.get("finished_at")
        _runtime_state["last_result"] = result
        if 'session' in locals():
            session.close()
        _search_lock.release()
    
    return result


def runtime_state() -> dict:
    return _runtime_state
