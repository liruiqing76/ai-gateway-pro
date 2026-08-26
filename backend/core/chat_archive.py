# ============================================================
# 智能网关 — 聊天原文归档(DB 瘦身)
# chat_logs.prompt_text 曾存完整上下文原文(含系统提示/历史消息/工具调用/
# base64 图片), 几天就几百 MB。现在原文 gzip 压缩后按月追加写入
# {db目录}/chat_archive/YYYY-MM.jsonl.gz, 数据库只保留"头+尾预览"
# (经验提炼/每日总结用尾部采样, 不出库); 详情页按需从归档读回全文。
# 行本体(chat_logs)永远保留 —— 经验系统的 source_chat_id / 每日总结
# 依赖它, 且预览化后每行只有几百字节, 不再有膨胀问题。
# ============================================================

import gzip
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("gateway.chat_archive")

# 预览保留的头部/尾部字符数(头部给列表/日总结, 尾部是真实交互样本)
PREVIEW_HEAD = 1200
PREVIEW_TAIL = 1200
# 超过该长度的 prompt 才外置(短文本直接入库, 省一次文件 IO)
EXTERNAL_MIN = PREVIEW_HEAD + PREVIEW_TAIL

# 归档文件追加是共享写, 串行化避免并发请求把行写裂
_lock = threading.Lock()


def archive_dir() -> Path:
    """归档目录 = 数据库同目录下的 chat_archive/"""
    gw = os.getenv("GW_DB_PATH")
    if gw:
        return Path(gw).parent / "chat_archive"
    return Path(__file__).parent.parent / "data" / "chat_archive"


def month_file(created_at: datetime) -> Path:
    return archive_dir() / f"{created_at.strftime('%Y-%m')}.jsonl.gz"


def needs_external(text: str) -> bool:
    return len(text or "") > EXTERNAL_MIN


def make_preview(text: str) -> str:
    """存库预览: 头 PREVIEW_HEAD + 尾 PREVIEW_TAIL, 中段标注省略。
    短文本(< 阈值)原样返回(直接入库, 不走归档)。"""
    n = len(text or "")
    if n <= PREVIEW_HEAD + PREVIEW_TAIL:
        return text
    mid = n - PREVIEW_HEAD - PREVIEW_TAIL
    return (text[:PREVIEW_HEAD]
            + f"\n...[中间{mid}字符已归档省略]...\n"
            + text[-PREVIEW_TAIL:])


def tail_of(preview: str, limit: int = 2000) -> str:
    """从预览提取"尾部采样"(真实交互段, 经验提炼用)。
    预览形态取省略标记后的尾段; 短文本返回原样。"""
    marker = "...[中间"
    if marker in (preview or ""):
        return preview.split(marker, 1)[-1][:limit]
    return (preview or "")[-limit:]


def append_prompt(chat_id: int, created_at: datetime, text: str) -> bool:
    """把原文整段追加写入当月归档文件(每行一个 JSON, 带 chat id)。
    失败返回 False, 调用方回落"原文仍入数据库"保证不丢数据。"""
    try:
        archive_dir().mkdir(parents=True, exist_ok=True)
        line = json.dumps({"id": chat_id, "t": text},
                          ensure_ascii=False) + "\n"
        with _lock:
            with gzip.open(month_file(created_at), "ab", compresslevel=6) as f:
                f.write(line.encode("utf-8"))
        return True
    except Exception:
        logger.exception("chat_archive append failed id=%s", chat_id)
        return False


def read_prompt(chat_id: int, created_at: datetime) -> str | None:
    """从归档文件读回原文全文; 文件缺失/损坏/找不到该 id 返回 None。
    按月文件线性扫描: 一个月最多几千行, gzip 解压一次几十 ms, 可接受。"""
    try:
        with gzip.open(month_file(created_at), "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue  # 撕裂行跳过, 不影响其它记录
                if rec.get("id") == chat_id:
                    return rec.get("t") or ""
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("chat_archive read failed id=%s", chat_id, exc_info=True)
        return None
    return None


def ensure_archive_column() -> bool:
    """给已存在的旧库补 prompt_archive 列(SQLAlchemy create_all 不迁移列)。
    幂等: 列已存在直接返回。返回 True=确保可用, False=失败。"""
    from sqlalchemy import text
    try:
        from backend.models import get_session
        session = get_session()
        try:
            cols = [r[1] for r in session.execute(text("PRAGMA table_info(chat_logs)"))]
            if "prompt_archive" not in cols:
                session.execute(text(
                    "ALTER TABLE chat_logs ADD COLUMN prompt_archive VARCHAR(16)"))
                session.commit()
                logger.info("chat_logs 已补 prompt_archive 列")
            return True
        finally:
            session.close()
    except Exception:
        logger.exception("ensure prompt_archive column failed")
        return False