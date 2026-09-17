#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
免费模型注册帖子搜索 - 核心模块

功能:
  1. 搜索多个来源（Gitee, GitHub, 百度, 今日头条, 微信公众号）关于免费模型注册的帖子
  2. 去重（首级: 来源+URL+标题哈希; 次级: 标题签名, 抗 URL token 每日变化）
  3. 入库存储
  4. 调用 LLM 评估每条帖子的可行性和真实性
  5. 生成 Markdown 报告
  6. 用户"忽略"：精确 hash + 标题签名双记录，同一篇文章换 URL/token 也不会再出现

提供手动搜索入口 run_free_model_search(), 被定时任务和 API 共用。
"""
import json
import hashlib
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from html import unescape
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

logger = logging.getLogger("gateway")

# 数据目录与报告目录
# ★ 必须用运行时数据目录(runtime_data_dir, GW_DB_PATH 感知): 打包后 __file__
#   指向 onefile 的临时解压目录(_MEIxxx), 直接取路径会在进程退出时全部丢失,
#   表现为"忽略/报告/原始 HTML 重启即消失"。桌面模式落在 exe 旁 data/ 下。
from backend.core.free_sources import runtime_data_dir

REPORTS_DIR = runtime_data_dir() / "free_models"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# 用户忽略的帖子列表（手动标记为无关的内容）: 精确 hash + 标题签名双记录,
# 避免同一篇文章换成带新 token 的 URL 后绕开忽略重新出现
IGNORED_FILE = REPORTS_DIR / ".ignored_posts.json"

# 帖子时效窗口: 超过该天数的帖子/报告一律不再返回(免费 API 时效性很强,
# 7 天前的注册/额度帖子基本已失效, 无保留意义, 默认 7 天)
MAX_POST_AGE_DAYS = 7

# "文章/帖子"类来源: 搜的是有时效的文章, 必须拿到发表时间, 否则新旧不明、
# 几年前的旧文也会混进结果(这正是"旧文章链接发给用户"的根因)。
# github/gitee 搜的是仓库(created_at 语义不同), 不在其列。
ARTICLE_SOURCES = {"wechat", "baidu", "toutiao"}


def _coerce_published_at(value):
    """把来源各异的时间值统一成 datetime; 解析失败返回 None。

    兼容: datetime / ISO 字符串 / 秒级整数时间戳 / 毫秒级整数时间戳。
    今日头条的 publish_time 常是秒级整数, 微信是秒级时间戳, 百度可能缺失。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # 毫秒级
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():  # 纯数字时间戳
            ts = float(s)
            if ts > 1e12:
                ts = ts / 1000.0
            try:
                return datetime.fromtimestamp(ts)
            except (ValueError, OSError):
                return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def is_post_fresh(post, max_age_days: int = MAX_POST_AGE_DAYS) -> bool:
    """帖子是否在时效窗口内。

    优先用 published_at(内容发表时间, 语义上的"时效"), 缺失时:
    - 文章类来源(wechat/baidu/toutiao)视为新旧不明 → False, 直接不放行;
    - 仓库类来源(github/gitee)退化为 fetched_at(抓取时间)。
    做过时区归一化避免 aware/naive 比较崩溃。
    """
    ts = getattr(post, "published_at", None)
    source = getattr(post, "source", "") or ""
    if ts is None:
        if source in ARTICLE_SOURCES:
            return False
        ts = getattr(post, "fetched_at", None)
    if ts is None:
        return True
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return (datetime.now() - ts).total_seconds() <= max_age_days * 86400


def _title_key(title: str) -> str:
    """规范化标题 → 稳定的内容签名。

    很多来源（搜狗微信/今日头条/百度）返回的是带时效 token 的中转跳转 URL，
    同一篇文章每天拿到的 URL 都会变，导致按 URL 算的 hash_key 每次都不稳定、
    忽略和去重都失效。这里用"标题规范化后的指纹"来识别同一篇内容：
    统一小写、合并空白、去掉常见标点/emoji 干扰。
    """
    t = (title or "").strip().lower()
    # 合并空白、全半角空格
    t = re.sub(r"\s+", " ", t)
    # 去掉 URL、常见标点与装饰符号，保留汉字/字母/数字
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t, flags=re.UNICODE)
    return t or ""


# 标题签名参与"自动去重"的最短长度: 太短的签名(如"免费模型")过于泛化,
# 不同文章可能撞同一签名 → 自动去重要求足够长, 避免误并; 用户"手动忽略"不受此限。
MIN_TITLE_KEY_LEN = 6


def _ignored_file_data() -> dict:
    """读取忽略文件，兼容旧格式（旧文件只有 hashes）。"""
    if not IGNORED_FILE.exists():
        return {"hashes": [], "titles": []}
    try:
        data = json.loads(IGNORED_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"hashes": [], "titles": []}
        return {
            "hashes": data.get("hashes") or [],
            "titles": data.get("titles") or [],
        }
    except Exception as e:
        logger.warning(f"加载忽略列表失败：{e}")
        return {"hashes": [], "titles": []}


def load_ignored_hashes() -> set:
    """加载用户手动标记为忽略的帖子哈希（32 位 SHA256）"""
    return set(_ignored_file_data()["hashes"])


def load_ignored_title_keys(session=None) -> set:
    """加载忽略的标题签名（用于抗 URL token 变化，识别同一篇内容）。

    传 session 时按 hash 回填标题签名: 老版本忽略文件只有 hash(如 dist 部署
    里已积累的旧忽略), 同一篇文章换成带新 token 的 URL 后 hash 会变,
    回填标题签名才能继续拦住它。
    """
    keys = set(_ignored_file_data()["titles"])
    if session is None:
        return keys
    hashes = load_ignored_hashes()
    if not hashes:
        return keys
    try:
        from backend.models import FreeModelPost
        rows = session.query(FreeModelPost).filter(FreeModelPost.hash_key.in_(list(hashes))).all()
        for p in rows:
            t = _title_key(p.title)
            if t:
                keys.add(t)
    except Exception as e:
        logger.warning(f"回填忽略标题签名失败: {e}")
    return keys


def save_ignored(hashes: set, title_keys: set):
    """保存忽略的帖子哈希 + 标题签名到文件。"""
    try:
        IGNORED_FILE.write_text(
            json.dumps(
                {"hashes": sorted(hashes), "titles": sorted(title_keys)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"保存忽略列表失败：{e}")


def save_ignored_hashes(hashes: set):
    """兼容旧引用：只传 hashes 时保留已有 titles。"""
    title_keys = load_ignored_title_keys()
    save_ignored(hashes, title_keys)


def is_ignored(post_hash: str, title: str = "", session=None) -> bool:
    """帖子是否命中忽略列表（精确 hash 或标题签名任一命中即视为忽略）。

    session 用于回填老版本 hash-only 忽略记录的标题签名。
    """
    ignored_hashes = load_ignored_hashes()
    if post_hash and post_hash in ignored_hashes:
        return True
    tkey = _title_key(title)
    if tkey and tkey in load_ignored_title_keys(session):
        return True
    return False


def make_ignore_check(session=None):
    """一次性加载忽略集合，返回 O(1) 的忽略判断函数。

    批量场景（报告详情逐条过滤帖子）用这个，避免每条帖子都重新读忽略文件/查库。
    """
    ignored_hashes = load_ignored_hashes()
    ignored_title_keys = load_ignored_title_keys(session)

    def check(post_hash: str, title: str) -> bool:
        if post_hash and post_hash in ignored_hashes:
            return True
        tkey = _title_key(title)
        return bool(tkey and tkey in ignored_title_keys)

    return check


def mark_post_as_ignored(post_hash: str, title: str = "") -> bool:
    """
    用户手动标记某条帖子为忽略（不再出现在搜索结果中）。
    
    同时记录精确 hash 和标题签名，这样即使同一篇文章下次带着不同的
    搜索 URL/token 回来，也能被同样识别为忽略。

    Args:
        post_hash: 帖子的 hash_key（32 位 SHA256）
        title: 帖子标题（用于生成稳定的标题签名）
    
    Returns:
        True 如果成功添加到忽略列表，False 如果已在列表中
    """
    hashes = load_ignored_hashes()
    title_keys = load_ignored_title_keys()
    tkey = _title_key(title)
    added = False
    if post_hash and post_hash not in hashes:
        hashes.add(post_hash)
        added = True
    if tkey and tkey not in title_keys:
        title_keys.add(tkey)
        added = True
    if not added:
        return False
    save_ignored(hashes, title_keys)
    logger.info(f"已标记帖子 {post_hash[:16] if post_hash else ''} (标题签名 {tkey[:16]}) 为忽略")
    return True


def unmark_post_as_ignored(post_hash: str, title: str = "") -> bool:
    """
    取消忽略某条帖子。
    
    Args:
        post_hash: 帖子的 hash_key
        title: 帖子标题（生成的标题签名也会一并移除）
    
    Returns:
        True 如果成功移除，False 如果未在忽略列表中
    """
    hashes = load_ignored_hashes()
    title_keys = load_ignored_title_keys()
    tkey = _title_key(title)
    removed = False
    if post_hash and post_hash in hashes:
        hashes.discard(post_hash)
        removed = True
    if tkey and tkey in title_keys:
        title_keys.discard(tkey)
        removed = True
    if not removed:
        return False
    save_ignored(hashes, title_keys)
    logger.info(f"已取消帖子 {post_hash[:16] if post_hash else ''} 的忽略标记")
    return True

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


# 判定为"不可读/失效"落地页的特征: 出现任一即视为死链(公众号文章被删/需登录/
# 反爬/内容被折叠时常见), 避免把"打开即失效"的链接发给用户。
_DEAD_LINK_MARKERS = (
    "该内容已被发布者删除",
    "此内容因违规无法查看",
    "此内容被投诉且经审核涉嫌侵权",
    "该文章已删除",
    "此内容涉嫌违规",
    "接验证码",
    "验证码",
    "环境异常",
    "系统繁忙",
)


def verify_link_live(url: str, session: httpx.Client = None,
                     timeout: float = 8.0) -> bool:
    """真实请求目标链接, 确认可读且未失效。

    中转链接在入库前已由 _resolve_search_url 解析成直链, 这里再对直链做
    真实可达性验证:
      - HTTP 200 才算通;
      - 落地页 HTML 出现失效/验证特征(公众号被删、需验证码等)判定为死链;
      - 网络异常/超时判定为不可达。

    返回 False 的帖子不入库, 也用于定时回填时淘汰存量死链。
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    headers = {
        "User-Agent": UA_MOBILE,  # 公众号对移动 UA 更宽松, 验证页更少
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        resp = session.get(url, headers=headers, timeout=timeout) if session else _make_session().get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return False
        body = resp.text[:8000].lower()
        for marker in _DEAD_LINK_MARKERS:
            if marker in body:
                return False
        return True
    except Exception:
        return False


# 搜索结果里带时效 token 的中转跳转域名: 直接入库的话 token 过期后
# 用户点开就是"链接已失效"。抓取时解析成最终直链再入库。
REDIRECT_HOSTS = {"weixin.sogou.com", "www.baidu.com", "baidu.com"}


def _resolve_search_url(url: str, session: httpx.Client, timeout: float = 10.0) -> Optional[str]:
    """把搜索引擎的中转跳转链接解析为最终直链; 无法解析时返回 None(调用方丢弃该条)。

    - 搜狗微信(weixin.sogou.com/link): 无 cookie 时不会 302, 而是返回一段
      JS 拼 URL 的落地页(`url += '...'` 片段), 拼起来即 mp.weixin.qq.com 文章直链;
    - 百度(baidu.com/link): 直接 302 到落地页, 跟随重定向即可;
    - 其他域名原样返回, 不发起请求。
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        host = urlsplit(url).hostname or ""
    except Exception:
        return None
    if host not in REDIRECT_HOSTS:
        return url
    headers = {
        "User-Agent": UA_PC,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        if "weixin.sogou.com" in host:
            # 浏览器从搜索结果页点开中转链接时 Referer 是搜狗搜索页
            headers["Referer"] = "https://weixin.sogou.com/"
            resp = session.get(url, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                return None
            # 搜狗 /link 落地页靠 JS `url += '片段';` 拼出真链接;
            # 只做 &amp; 替换避免 html.unescape 把 &timestamp 误转成 ×tamp
            parts = re.findall(r"\burl\s*\+=\s*(?:'([^']*)'|\"([^\"]*)\")", resp.text)
            real = "".join((a or b).replace("&amp;", "&") for a, b in parts).strip()
            if not real.startswith(("http://", "https://")):
                return None
            if "weixin.sogou.com" in (urlsplit(real).hostname or ""):
                return None  # 停在搜狗自身页面(反爬/死链)
            return real
        # 百度: /link 302 到落地页
        headers["Referer"] = "https://www.baidu.com/"
        resp = session.get(url, headers=headers, timeout=timeout)
        final = str(resp.url if resp.url is not None else "")
        if not final.startswith(("http://", "https://")):
            return None
        if "baidu.com" in (urlsplit(final).hostname or ""):
            return None  # 停在百度页面(安全验证/死链)
        return final
    except Exception as e:
        logger.warning(f"解析中转链接失败: {url[:80]}... {e}")
        return None


# 从微信文章页提取"可长期使用"的永久直链的 HTML 特征。
# 搜狗解析出的 mp.weixin.qq.com/s?src=11&timestamp&signature 是带签名/时效的
# 中转密文 URL, 几小时后即"链接已失效"; 文章页里的 msg_link / og:url / canonical
# 才是微信文章自身的永久地址(带 __biz&mid&sn 或 /s/<hash>), 可长期访问。
_PERMANENT_URL_PATTERNS = (
    re.compile(r'''var\s+msg_link\s*=\s*['"]([^'"]+)['"]''', re.IGNORECASE),
    re.compile(r'''<meta[^>]*property=["']og:url["'][^>]*content=["']([^"']+)["']''', re.IGNORECASE),
    re.compile(r'''<meta[^>]*content=["']([^"']+)["'][^>]*property=["']og:url["']''', re.IGNORECASE),
    re.compile(r'''<link[^>]*rel=["']canonical["'][^>]*href=["']([^"']+)["']''', re.IGNORECASE),
)


def _extract_permanent_wechat_url(html: str) -> Optional[str]:
    """从微信文章 HTML 里提取永久直链(msglink 优先, og:url/canonical 兜底)。"""
    if not html:
        return None
    for pat in _PERMANENT_URL_PATTERNS:
        m = pat.search(html)
        if not m:
            continue
        u = unescape(m.group(1)).strip()
        if u.startswith("//"):
            u = "https:" + u
        try:
            host = urlsplit(u).hostname or ""
        except Exception:
            host = ""
        if "mp.weixin.qq.com" in host:
            return u
    return None


def _to_permanent_wechat_url(resolved: str, session: httpx.Client,
                             timeout: float = 10.0) -> Optional[str]:
    """把搜狗解析出的中转 mp 密文 URL 升级为文章永久直链。

    中转 URL(src=11&timestamp&signature)带签名、很快过期; 真实打开文章页后,
    从 msg_link/og:url/canonical 拿到永久地址才不会被"链接已失效"。取不到
    (反爬/删文/验证页)返回 None, 调用方保留原 URL 或按死链处理。
    """
    if not resolved:
        return None
    headers = {
        "User-Agent": UA_MOBILE,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        resp = session.get(resolved, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return None
        # 删文/违规/验证码等死链特征, 不再尝试拿永久链接
        body = resp.text[:8000].lower()
        if any(m in body for m in _DEAD_LINK_MARKERS):
            return None
        perma = _extract_permanent_wechat_url(resp.text)
        if not perma or perma == resolved:
            return None
        return perma
    except Exception as e:
        logger.warning(f"解析公众号永久直链失败: {resolved[:60]}... {e}")
        return None


def _is_transitional_mp_url(url: str) -> bool:
    """判断 url 是否是带时效 token 的公众号中转密文链接。

    形如 mp.weixin.qq.com/s?src=11&timestamp=...&signature=...(path=/s 且带 query),
    token 几小时后即过期, 用户点开会"链接已失效"。永久直链是 /s/<hash> 或带
    __biz&mid&sn 的地址, 不在此列。
    """
    if not url:
        return False
    try:
        u = urlsplit(url)
    except Exception:
        return False
    if "mp.weixin.qq.com" not in (u.hostname or ""):
        return False
    path = (u.path or "").rstrip("/")
    if path == "/s" and u.query:
        return True
    return False


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
                        resolved = _resolve_search_url(link, session)
                        if not resolved:
                            logger.warning(f"百度结果无法解析出直链, 丢弃: {title[:30]}...")
                            continue
                        results.append({
                            "source": "baidu",
                            "url": resolved,
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
                # 提取发布时间: 搜狗用 document.write(timeConvert('秒级时间戳'))输出
                published_at = None
                pub_match = re.search(r"timeConvert\('(\d+)'\)", block)
                if pub_match:
                    try:
                        published_at = datetime.fromtimestamp(int(pub_match.group(1)))
                    except (ValueError, OSError):
                        published_at = None
                # 提取摘要
                snippet_match = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', block, re.DOTALL)
                snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip() if snippet_match else ""
                # 去掉高亮标记
                title = title.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")
                snippet = snippet.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")
                if title:
                    resolved = _resolve_search_url(link, session)
                    if not resolved:
                        logger.warning(f"搜狗微信结果无法解析出直链, 丢弃: {title[:30]}...")
                        continue
                    # 公众号文章直链必须是 mp.weixin.qq.com, 其它落点视为中转/反爬
                    # 死链, 丢弃(否则用户点开即"链接已失效")
                    if (urlsplit(resolved).hostname or "") != "mp.weixin.qq.com":
                        logger.warning(f"搜狗微信结果非 mp 直链, 丢弃: {title[:30]}...")
                        continue
                    # 升级为可长期访问的文章永久直链(中转 src=11&signature URL 会过期)
                    perma = _to_permanent_wechat_url(resolved, session)
                    if perma:
                        resolved = perma
                    elif _is_transitional_mp_url(resolved):
                        # 拿不到永久直链、且仍是会过期的中转密文链接 → 丢弃。
                        # 留着的话 token 过期后用户点开就是"链接已失效", 宁可少一条。
                        logger.warning(f"搜狗微信中转密文链接无法解析永久直链, 丢弃: {title[:30]}...")
                        continue
                    results.append({
                        "source": "wechat",
                        "url": resolved,
                        "title": title,
                        "author": author,
                        "published_at": published_at,
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

def _load_existing_posts(session, since_days: int = 30) -> list:
    """加载最近 N 天的帖子对象（用于去重）"""
    from backend.models import FreeModelPost
    cutoff = datetime.now() - timedelta(days=since_days)
    return session.query(FreeModelPost).filter(FreeModelPost.fetched_at >= cutoff).all()


def deduplicate_and_store(raw_posts: List[Dict], session) -> Tuple[List[Dict], int, int, int]:
    """
    去重并入库。
    返回：(新帖子列表，新增数，跳过数，升级数)
    升级: 标题签名命中的旧行若是中转死链(weixin.sogou.com/baidu /link,
    token 过期即"链接已失效"), 用本次解析出的直链替换旧 URL。
    """
    from backend.models import FreeModelPost

    existing_posts = _load_existing_posts(session)
    existing_hashes = {p.hash_key for p in existing_posts}
    # 已有帖子的标题签名（用于次级内容去重：同一篇文章换 URL/标题微变也合并；
    # 太短的签名过于泛化，不参与自动去重，避免误并不同文章）
    existing_title_keys = {
        t for t in (_title_key(p.title) for p in existing_posts)
        if t and len(t) >= MIN_TITLE_KEY_LEN
    }
    # 标题签名 → 最早入库的帖子(升级死链时用, 同签名多条取最早那条)
    existing_title_map: dict = {}
    for p in existing_posts:
        k = _title_key(p.title)
        if k and len(k) >= MIN_TITLE_KEY_LEN:
            existing_title_map.setdefault(k, p)
    # 用户手动标记的忽略列表：精确 hash + 抗 URL 变化的标题签名
    # (传 session 让老版本 hash-only 忽略也能回填标题签名)
    ignored_hashes = load_ignored_hashes()
    ignored_title_keys = load_ignored_title_keys(session)
    new_posts = []
    skipped = 0
    upgraded = 0
    
    for post in raw_posts:
        source = post.get("source", "")
        url = post.get("url", "")
        title = post.get("title", "")
        if not url or not title:
            skipped += 1
            continue
        
        # 计算哈希键
        hk = _hash_key(source, url, title)
        tkey = _title_key(title)
        
        # 过滤：用户手动标记为忽略的帖子（精确 hash 或标题签名命中都跳过，
        # 这样带时效 token 的 URL 每天变化也不会绕开忽略）
        if (hk in ignored_hashes) or (tkey and tkey in ignored_title_keys):
            skipped += 1
            logger.debug(f"跳过已忽略的帖子：{title[:30]}...")
            continue
        
        # 首级去重：哈希键已存在
        if hk in existing_hashes:
            skipped += 1
            continue
        
        # 次级去重: 标题签名已存在（同一篇文章换了 URL/标题微变 → 也视为重复，
        # 避免每天重复入库同一批帖子，报告里全是旧内容）
        if tkey and len(tkey) >= MIN_TITLE_KEY_LEN and tkey in existing_title_keys:
            # 旧记录若是时效敏感的中转死链, 直接跳过会让失效链接永远留在报告里;
            # 用本次解析出的直链升级旧行
            if _upgrade_stale_link(existing_title_map, tkey, source, url, title):
                upgraded += 1
                logger.info(f"升级存量死链为直链: {title[:30]}...")
                continue
            skipped += 1
            logger.debug(f"跳过标题重复的帖子：{title[:30]}...")
            continue
        
        # 创建新帖子记录
        published = _coerce_published_at(post.get("published_at"))

        # 入库前真实可达性验证: 中转链接在入库前已由 _resolve_search_url 解析,
        # 解析后的直链仍是"链接已失效"高发区(公众号被删/需登录/token过期等),
        # 真实 GET 一次确认可读后再入库, 避免把死链发给用户。
        with _make_session() as _v_http:
            if not verify_link_live(url, _v_http):
                skipped += 1
                logger.debug(f"跳过不可达/已失效的链接: {title[:30]}...")
                continue

        # 时效过滤: 文章类来源必须有发表时间, 否则新旧不明直接丢弃
        if source in ARTICLE_SOURCES and not isinstance(published, datetime):
            skipped += 1
            logger.debug(f"跳过无发表时间的帖子: {title[:30]}...")
            continue
        # 时效过滤: 明确发表时间且已超过时效窗口的帖子直接丢弃(免费 API 已失效/过期)
        if isinstance(published, datetime) and not is_post_fresh(
                SimpleNamespace(published_at=published, fetched_at=datetime.now())):
            skipped += 1
            logger.debug(f"跳过超过{MAX_POST_AGE_DAYS}天的失效帖子: {title[:30]}...")
            continue

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
        if tkey and len(tkey) >= MIN_TITLE_KEY_LEN:
            existing_title_keys.add(tkey)
    
    try:
        session.commit()
    except Exception as e:
        logger.error(f"入库失败: {e}")
        session.rollback()
        return [], 0, len(raw_posts), 0
    
    return new_posts, len(new_posts), skipped, upgraded


def _upgrade_stale_link(existing_title_map: dict, tkey: str, source: str,
                        url: str, title: str) -> bool:
    """标题签名命中的旧行是中转死链时, 用解析后的直链替换旧 URL。

    旧版把搜索引擎的中转链接(带时效 token)直接入库, token 过期即"链接已失效";
    同一篇文章再次被抓到直链时, 用直链升级旧行。返回是否发生升级,
    否则调用方按常规去重跳过。
    """
    if any(h in url for h in REDIRECT_HOSTS):
        # 新链接自己仍是中转 → 没有升级价值, 按重复跳过
        return False
    p = existing_title_map.get(tkey)
    if p is None:
        return False
    old = p.url or ""
    if any(h in old for h in REDIRECT_HOSTS) and old != url:
        p.url = url
        p.hash_key = _hash_key(source, url, title)
        return True
    return False


# ============================================================
# 存量死链回填
# ============================================================

# 每轮回填上限: 早期入库的中转链接逐条解析较慢, 限制数量避免拖慢定时任务
# 从 150 提到 300: 加快清掉存量过期密文链接(每条仅一次网络请求, 数秒)
MAX_LINK_REFRESH = 300


def _refresh_dead_links(session, limit: int = MAX_LINK_REFRESH) -> Tuple[int, int, int]:
    """回填存量死链: 把已入库的中转链接(weixin.sogou.com / baidu.com/link)
    重新解析为直链并更新 url + hash_key。

    早期版本把搜索结果页的中转链接直接入库, token 过期后用户点开即"链接已失效"。
    解析不出的行(反爬/真死链/落地重复)直接删除——留着只会继续坑用户点开失效。
    返回 (成功更新数, 失败数, 删除数)。
    """
    from sqlalchemy import or_
    from backend.models import FreeModelPost

    posts = session.query(FreeModelPost).filter(
        or_(
            FreeModelPost.url.like("%weixin.sogou.com%"),
            FreeModelPost.url.like("%baidu.com/link%"),
            FreeModelPost.url.like("%mp.weixin.qq.com/s?%"),  # 带 src=&signature 的中转密文链接
        )
    ).order_by(FreeModelPost.fetched_at.desc()).limit(limit).all()
    if not posts:
        return 0, 0, 0

    updated = failed = removed = 0
    used: set = set()  # 本批已占用的 (source, url), 防多条死链解析到同一落地页撞唯一约束
    with _make_session() as http:
        for p in posts:
            # 中转 mp 密文链接(带 src=&timestamp&signature, 会过期) → 升级成永久直链
            is_mp_transitional = ("mp.weixin.qq.com/s?" in (p.url or "")) and ("src=" in (p.url or ""))
            if is_mp_transitional:
                resolved = _to_permanent_wechat_url(p.url, http)
                if not resolved or resolved == p.url:
                    # 中转密文链接 token 必过期, 拿不到永久直链 = 用户必点开失效。
                    # 删除, 不留必失效链接; 且搜索侧已不再入这类链接, 不会重新产生。
                    session.delete(p)
                    removed += 1
                    continue
            else:
                resolved = _resolve_search_url(p.url, http)
                if not resolved or resolved == p.url:
                    # 解析不出直链 = 真死链, 删除
                    session.delete(p)
                    removed += 1
                    continue
            key = (p.source, resolved)
            if key in used:
                session.delete(p)
                removed += 1
                continue
            # (source, url) 有唯一约束: 其他行已是该直链则删除重复行
            clash = session.query(FreeModelPost.id).filter(
                FreeModelPost.source == p.source,
                FreeModelPost.url == resolved,
                FreeModelPost.id != p.id,
            ).first()
            if clash:
                session.delete(p)
                removed += 1
                continue
            used.add(key)
            p.url = resolved
            p.hash_key = _hash_key(p.source, resolved, p.title)
            updated += 1
    try:
        session.commit()
    except Exception as e:
        # (source, url) 唯一约束冲突概率极低, 冲突时回滚并丢弃本轮更新
        logger.error(f"死链回填入库失败: {e}")
        session.rollback()
        return 0, 0, len(posts)
    return updated, failed, removed


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

        # 0. 回填存量死链(早期入库的中转链接, token 过期即"链接已失效")
        try:
            refreshed, refresh_failed, removed = _refresh_dead_links(session)
            result["links_refreshed"] = refreshed
            result["links_refresh_failed"] = refresh_failed
            result["links_removed"] = removed
            if refreshed or removed:
                logger.info(f"死链回填: 更新 {refreshed} 条, 删除 {removed} 条, 失败 {refresh_failed} 条")
        except Exception as e:
            result["links_refreshed"] = 0
            result["links_refresh_failed"] = 0
            result["links_removed"] = 0
            result["errors"].append(f"死链回填: {e}")
            logger.warning(f"死链回填异常: {e}")

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
        new_posts, added, skipped, upgraded = deduplicate_and_store(raw_posts, session)
        result["added"] = added
        result["skipped"] = skipped
        result["upgraded"] = upgraded
        
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
