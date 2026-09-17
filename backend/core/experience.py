# ============================================================
# 智能网关 — 经验提炼 / 每日总结 / 上下文注入
# 参考 OpenCovibe 经验引擎机制:
#   - 从 chat_logs 用 LLM 提炼成 lesson/fact/sop 经验
#   - 定时生成每日经验/教训总结
#   - 下次调用时把相关经验注入到模型上下文
# ============================================================

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from backend.config import settings

logger = logging.getLogger("gateway.experience")

# 注入块在 system 消息中的标记
INJECT_TAG = "[历史经验/教训]"  # 用于识别已注入, 避免重复
INJECT_LIMIT = 6   # 最多注入条数
INJECT_CHARS = 2500  # 注入内容最大字符


_TRIVIAL_STOP_RE = re.compile(
    r"^\s*(hi+|hello|你好|哈喽|嗨|在吗|测试|test|用.{0,8}回复|回复一下|试试)\s*[!。？?,.，、\s]*$",
    re.IGNORECASE)


def _is_trivial_chat(c) -> bool:
    """判断一条聊天是否是无可提炼价值的琐碎对话(问候/探针/极短请求)。"""
    from backend.core import chat_archive
    try:
        probe = chat_archive.tail_of(c.prompt_text or "", 2000).strip()
    except Exception:
        probe = ""
    if not probe:
        return True
    if len(probe) < 15:   # 问候/纯探针/极短请求, 提炼不出可复用经验
        return True
    if _TRIVIAL_STOP_RE.search(probe):
        return True
    return False


# 产出经验里的"空泛模型行为噪音"特征词: 命中的特征词越多, 越像从测试探针对话
# (hi / 用两个字回复 之类)提炼出的通用废话(模型响应不稳定/需重复测试/一致性),
# 缺乏具体场景、因果与可复用的落点, 无可参考价值, 直接丢弃。
_GENERIC_INFLATION_PHRASES = (
    "响应差异", "响应不稳定", "稳定性", "一致性", "重复测试", "重复请求",
    "非确定性", "碎片化", "重新校准", "采样参数", "无响应", "训练数据",
    "语义微调", "问候", "确认词", "同义词", "多轮测试", "输出差异",
    "指令遵循", "行为特征", "会话启动", "建立连接", "微调策略", "鲁棒性",
    "空白回复", "正常返回", "基准验证", "基线验证", "版本差异", "随机性",
)


def _is_garbage_experience(title: str, content: str) -> bool:
    """过滤"空泛模型行为"类经验: 命中两条及以上泛化噪音词即判定无价值。"""
    text = f"{title or ''} {content or ''}"
    hits = sum(1 for p in _GENERIC_INFLATION_PHRASES if p in text)
    return hits >= 2



# ─────────────────────────────────────────────
# LLM 调用(复用用户渠道)
# ─────────────────────────────────────────────

def _pick_provider():
    """挑一个可用的 provider 和模型来跑提炼 LLM。"""
    from backend.models import get_session, Provider
    session = get_session()
    try:
        providers = session.query(Provider).filter_by(enabled=True).all()
        if not providers:
            return None, None, None
        # 优先挑有较多模型的, 用第一个可用模型的原名
        providers.sort(key=lambda p: len(p.models or []), reverse=True)
        p = providers[0]
        models = [m for m in (p.models or [])
                  if m not in (p.disabled_models or [])]
        if not models:
            return None, None, None
        # 解密 key
        from backend.auth.security import decrypt_secret
        key = decrypt_secret(p.api_key_encrypted)
        return p, key, models[0]
    finally:
        session.close()


async def _call_llm(messages: list, max_tokens: int = 1200) -> str:
    """调用提炼 LLM, 返回文本。失败返回空串。"""
    try:
        from backend.core.proxy import forward_non_stream
        from backend.api.proxy import get_http_client
        provider, api_key, model = _pick_provider()
        if not provider or not api_key:
            logger.warning("experience: no usable provider for LLM distillation")
            return ""
        client = await get_http_client()
        body = {"model": model, "messages": messages,
                "max_tokens": max_tokens, "stream": False}
        resp, err = await forward_non_stream(client, provider, api_key, model, body)
        if resp is None:
            logger.warning("experience: LLM call failed: %s", err)
            return ""
        return _extract_msg(resp)
    except Exception as e:
        logger.exception("experience LLM error: %s", e)
        return ""


def _extract_msg(resp: dict) -> str:
    try:
        choices = resp.get("choices") or []
        if not choices:
            return ""
        return str(choices[0].get("message", {}).get("content", "") or "").strip()
    except Exception:
        return ""


# ─────────────────────────────────────────────
# 经验提炼(从一批聊天记录)
# ─────────────────────────────────────────────

async def distill_chats(user_id: int, chat_ids: list[int] = None,
                        limit: int = 40) -> int:
    """把最近(或指定的)聊天记录用 LLM 提炼成经验, 返回新建条数。

    参考 OpenCovibe experience_distill_run:
    把对话输入/输出去给 LLM, 让它输出 lesson/fact/sop。
    """
    from backend.models import get_session, ChatLog
    session = get_session()
    try:
        q = session.query(ChatLog).filter_by(user_id=user_id, success=True)
        if chat_ids:
            q = q.filter(ChatLog.id.in_(chat_ids))
        chats = q.order_by(ChatLog.created_at.desc()).limit(limit).all()
        if not chats:
            return 0
    finally:
        session.close()

    # 拆成小块让 LLM 提炼
    chunks = _chunk_chats(chats, 5)
    total = 0
    for ck in chunks:
        made = await _distill_one_chunk(user_id, ck)
        total += made
    return total


def _chunk_chats(chats, size: int):
    out = []
    for i in range(0, len(chats), size):
        out.append(chats[i:i + size])
    return out


async def _distill_one_chunk(user_id: int, chats) -> int:
    from backend.core import chat_archive
    transcript = []
    for c in chats:
        # 输入/输出都取尾部采样: agent/完整上下文场景下, 开头全是固定
        # system 提示词/应答模板(每条都一样), 提炼出来全是"模型基线验证"类
        # 通用废话。真实交互在消息末尾, 预览列的尾段正好是它。
        if _is_trivial_chat(c):
            continue  # 琐碎探针/问候/极短请求, 提炼不出可复用经验, 直接跳过
        transcript.append(
            f"### 请求(模型 {c.original_model or '?'})\n"
            f"输入: {chat_archive.tail_of(c.prompt_text or '', 2000)}\n"
            f"输出: {chat_archive.tail_of(c.completion_text or '', 2000)}")
    if not transcript:
        return 0  # 该 chunk 全为琐碎对话, 不必调用 LLM
    prompt = (
        "你是经验提炼助手。从下面的真实 AI 对话记录中提炼可复用的经验(lesson)/事实(fact)/标准流程(sop)。\n"
        "要求:\n"
        "1. 只提炼有信息量的内容: 每条必须说清「什么场景/遇到什么问题/如何解决或结论是什么」,\n"
        "   空泛的\"用户在使用模型\"\"模型正常返回\"这类对话直接跳过, 不要为凑数硬造经验。\n"
        "2. kind 只能取: lesson=踩坑/失败换来的教训, fact=验证过的事实或配置知识, sop=可复用的操作步骤。\n"
        "3. title 一句话点明主题, content 写 2-4 句可直接复用的具体经验(含关键参数/命令/因果)。\n"
        "4. 若整段没有可提炼的价值, 输出空数组 []。\n"
        "输出为严格 JSON 数组, 每个元素含 kind、title、content; 只输出 JSON, 不要其它文字。\n\n"
        "对话记录:\n" + "\n\n".join(transcript)
    )
    text = await _call_llm([{"role": "user", "content": prompt}], max_tokens=1500)
    return _save_parsed(user_id, text, source_chat_ids=[c.id for c in chats])


def _save_parsed(user_id: int, text: str, source_chat_ids=None):
    """解析 LLM 返回的 JSON 并入库, 返回成功条数"""
    from backend.models import get_session, ExperienceLog
    if not text:
        return 0
    items = _extract_json_array(text)
    if not items:
        logger.info("experience: no parseable items from LLM output")
        return 0
    source_chat_id = (source_chat_ids or [None])[0]
    saved = 0
    session = get_session()
    try:
        for it in items:
            kind = (str(it.get("kind", "lesson")) or "lesson").lower()
            if kind not in ("lesson", "fact", "sop"):
                kind = "lesson"
            title = (str(it.get("title", "") or "")).strip()[:256]
            content = (str(it.get("content", "") or "")).strip()
            if not title or not content:
                continue
            if _is_garbage_experience(title, content):
                continue  # 空泛的模型行为废话, 无可复用价值
            # 去重: 同用户同标题
            dup = session.query(ExperienceLog).filter_by(
                user_id=user_id, title=title).first()
            if dup:
                continue
            session.add(ExperienceLog(
                user_id=user_id, kind=kind, title=title, content=content,
                source_chat_id=source_chat_id,
            ))
            saved += 1
        session.commit()
    except Exception:
        logger.exception("experience save error")
    finally:
        session.close()
    return saved


def _extract_json_array(text: str) -> list:
    """从 LLM 输出中尽可能提取 JSON 数组。"""
    text = text.strip()
    # 去掉可能的 ```json 包裹
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "experiences" in data:
            return data["experiences"]
    except Exception:
        pass
    # 尝试找第一个 [ ... ] 片段
    try:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


# ─────────────────────────────────────────────
# 每日总结
# ─────────────────────────────────────────────

async def summarize_daily(user_id: int, day: str = None) -> Optional[dict]:
    """生成某天的经验/教训总结。day 格式 YYYY-MM-DD, 默认今天。"""
    from backend.models import get_session, ChatLog, DailySummary
    day = day or datetime.now().strftime("%Y-%m-%d")

    # 取当天聊天记录
    session = get_session()
    try:
        start = datetime.strptime(day, "%Y-%m-%d")
        end = start + timedelta(days=1)
        chats = session.query(ChatLog).filter(
            ChatLog.user_id == user_id,
            ChatLog.created_at >= start,
            ChatLog.created_at < end,
        ).order_by(ChatLog.created_at).all()

        # 当日统计
        total = len(chats)
        success = sum(1 for c in chats if c.success)
        fail = total - success
        prompt_tokens = sum(int(c.prompt_tokens or 0) for c in chats)
        completion_tokens = sum(int(c.completion_tokens or 0) for c in chats)
        prompt_chars = sum(int(c.prompt_chars or 0) for c in chats)
        completion_chars = sum(int(c.completion_chars or 0) for c in chats)

        stats = {
            "requests": total,
            "success": success,
            "fail": fail,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_chars": prompt_chars,
            "completion_chars": completion_chars,
            "total_chars": prompt_chars + completion_chars,
            "models": {},
        }
        for c in chats:
            m = c.model_name or c.original_model or "?"
            stats["models"][m] = stats["models"].get(m, 0) + 1
    finally:
        session.close()

    # 若当天无记录, 生成占位总结
    if not chats:
        content = f"{day} 没有产生调用记录。"
    else:
        content = await _llm_daily_summary(day, chats)
        if not content:
            content = _rule_based_summary(day, total, fail)

    # 保存
    session = get_session()
    try:
        existing = session.query(DailySummary).filter_by(
            user_id=user_id, summary_date=day).first()
        if existing:
            existing.content = content
            existing.daily_stats = stats
        else:
            session.add(DailySummary(
                user_id=user_id, summary_date=day,
                content=content, daily_stats=stats,
            ))
        session.commit()
        result = {"date": day, "content": content, "stats": stats}
    finally:
        session.close()
    return result


async def _llm_daily_summary(day: str, chats) -> str:
    from backend.core import chat_archive
    transcript = []
    for c in chats[:50]:
        status = "成功" if c.success else f"失败({c.error_msg or '?'})"
        transcript.append(
            f"- [{status}] {c.original_model}: "
            f"输入『{chat_archive.tail_of(c.prompt_text or '', 500)[:300]}』"
            f" → 输出『{chat_archive.tail_of(c.completion_text or '', 300)}』")
    prompt = (
        f"你是运营复盘助手。请基于下面 {day} 这一天的 AI 调用记录, 写一份有实际价值的复盘总结。\n"
        f"要求:\n"
        f"1) 今日概览: 一句话概括当天调用规律(模型/任务类型) 2) 成功经验: 指出具体做对了什么\n"
        f"3) 失败教训: 点出具体失败类型与可改进点(有失败就分析原因, 没有就写\"今日无失败\")\n"
        f"4) 明日建议: 给出 1-3 条可执行建议。\n"
        f"禁止空话套话(\"继续努力\"\"保持良好\"之类), 每条要有记录依据。200-400 字。\n\n"
        f"今日记录:\n" + "\n".join(transcript)
    )
    return await _call_llm([{"role": "user", "content": prompt}], max_tokens=800)


def _rule_based_summary(day: str, total: int, fail: int) -> str:
    return (f"{day} 共 {total} 次调用，成功 {total - fail} 次，失败 {fail} 次。"
            f"{'有较多失败，建议检查失败的模型/渠道。' if fail > 0 else '运行平稳。'}")


# ─────────────────────────────────────────────
# 注入(供下次调用)
# ─────────────────────────────────────────────

def build_injection_block(user_id: int, requested_model: str = "") -> str:
    """为下一次调用构建经验注入块(拼接成 system 前缀文本)。"""
    from backend.models import get_session, ExperienceLog
    session = get_session()
    try:
        q = session.query(ExperienceLog).filter_by(user_id=user_id)
        rows = q.order_by(ExperienceLog.created_at.desc()).limit(200).all()
        # pinned 列可能为 NULL/0/1(旧/外部写入), 在此归一化处理
        pinned = [r for r in rows if r.pinned]
        recent = [r for r in rows if not r.pinned][:30]
        pool = pinned + recent
        if not pool:
            return ""
        lines = []
        for e in pool[:INJECT_LIMIT]:
            tag = {"lesson": "教训", "fact": "事实", "sop": "流程"}.get(e.kind, e.kind)
            lines.append(f"- [{tag}] {e.title}: {e.content}")
        block = f"{INJECT_TAG} (以下为历史经验，供参考):\n" + "\n".join(lines)
        if len(block) > INJECT_CHARS:
            block = block[:INJECT_CHARS] + "\n...（已截断）"
        return block
    finally:
        session.close()


def has_injection(messages: list) -> bool:
    """判断 messages 是否已含经验注入(避免重复)。"""
    for m in messages or []:
        c = str(m.get("content", ""))
        if c and INJECT_TAG in c:
            return True
    return False
