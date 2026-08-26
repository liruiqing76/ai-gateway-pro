# ============================================================
# 存量聊天原文迁移: chat_logs.prompt_text(全文) → 归档文件 + 预览
# 把库里已存在的大 prompt 一次性搬进 chat_archive/YYYY-MM.jsonl.gz,
# prompt_text 换成"头+尾预览"并打上 prompt_archive 月份标记。
# 幂等: 只处理 prompt_archive IS NULL 且超过阈值的行, 可重复执行;
# 短文本(<=阈值)本就不该外置, 保持原样。
#
# 用法:
#   ./.venv/Scripts/python.exe scripts/migrate_chat_archive.py            # 迁移默认库
#   GW_DB_PATH=dist/data/gateway.db ... migrate_chat_archive.py           # 指定库
#   ... migrate_chat_archive.py --dry-run                                  # 只统计不迁移
#   ... migrate_chat_archive.py --batch 200                               # 每批行数
# ============================================================
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run(dry_run: bool = False, batch: int = 200) -> None:
    import os
    from sqlalchemy import text
    from backend.models import get_session
    from backend.core import chat_archive

    if os.getenv("GW_DB_PATH"):
        print(f"目标库: {os.getenv('GW_DB_PATH')}")
    else:
        print("目标库: 默认 (backend/data/gateway.db 或 GW_DB_PATH 指向的库)")

    session = get_session()
    try:
        # 确保列存在(旧库第一次跑需要)
        cols = [r[1] for r in session.execute(text("PRAGMA table_info(chat_logs)"))]
        if "prompt_archive" not in cols:
            print("chat_logs 缺 prompt_archive 列, 先补列...")
            session.execute(text(
                "ALTER TABLE chat_logs ADD COLUMN prompt_archive VARCHAR(16)"))
            session.commit()

        # 统计待迁移
        total_rows, total_bytes = session.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(prompt_text)),0) FROM chat_logs "
            "WHERE prompt_archive IS NULL AND LENGTH(prompt_text) > :min"
        ), {"min": chat_archive.EXTERNAL_MIN}).first()
        print(f"待迁移: {total_rows} 行, {total_bytes/1024/1024:.1f} MB")

        if not total_rows:
            print("无待迁移数据(已全部外置或均为短文本)")
            return
        if dry_run:
            print("--dry-run, 未执行")
            return

        start = time.time()
        done_rows = done_bytes = 0
        while True:
            rows = session.execute(text(
                "SELECT id, created_at, prompt_text FROM chat_logs "
                "WHERE prompt_archive IS NULL AND LENGTH(prompt_text) > :min "
                "LIMIT :batch"
            ), {"min": chat_archive.EXTERNAL_MIN, "batch": batch}).fetchall()
            if not rows:
                break
            for rid, created_at, text_value in rows:
                # raw SELECT 返回的 created_at 是字符串, 转 datetime 再归档
                from datetime import datetime
                ts = created_at if isinstance(created_at, datetime) \
                    else datetime.fromisoformat(str(created_at))
                # 用 ORM 精确更新 preview + 归档标记; 原文必须完整写给文件
                if not chat_archive.append_prompt(rid, ts, text_value):
                    print(f"  [warn] id={rid} 归档写入失败, 跳过(下次重跑可补)")
                    continue
                preview = chat_archive.make_preview(text_value)
                month = ts.strftime("%Y-%m")
                session.execute(text(
                    "UPDATE chat_logs SET prompt_text=:pv, prompt_archive=:m "
                    "WHERE id=:rid"
                ), {"pv": preview, "m": month, "rid": rid})
                done_rows += 1
                done_bytes += len(text_value)
            session.commit()
            print(f"  已迁移 {done_rows} 行 / {total_rows} "
                  f"({done_bytes/1024/1024:.1f} MB), "
                  f"剩余 {total_rows - done_rows}, 用时 {time.time()-start:.0f}s")
        print(f"迁移完成: {done_rows} 行, {done_bytes/1024/1024:.1f} MB, "
              f"共用 {time.time()-start:.0f}s")

        # 收缩数据库文件(SQLite 删除后空间不自动归还)
        print("VACUUM 收缩数据库...")
        session.execute(text("VACUUM"))
        print("完成")
    finally:
        session.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="聊天原文外置归档迁移")
    ap.add_argument("--dry-run", action="store_true", help="只统计不执行")
    ap.add_argument("--batch", type=int, default=200, help="每批行数")
    args = ap.parse_args()
    run(dry_run=args.dry_run, batch=args.batch)