# -*- coding: utf-8 -*-
"""一次性回填: 把 users.quota_used_today 修正为今天真实的成功调用数。

背景: 旧版只有非流式请求才计入 quota_used_today, 且用 ORM merge 写回并发丢数,
导致仪表盘"今日调用"严重偏低(实测今天 1000+ 次调用, 计数只有 9)。

依据: request_traces 里今天 request_type 为 real(含历史 NULL 旧记录)且 success=1
的链路条数 = 今日真实成功调用数(流式+非流式都记账)。

用法:
    python scripts/backfill_quota_today.py            # 只打印, 不修改
    python scripts/backfill_quota_today.py --write    # 真正写入

注意: 会直接改库。运行中的网关服务同时写库时, SQLite 单写锁会短暂串行,
建议在服务空闲或停机维护时执行; 执行前先备份 gateway.db。
"""
import os
import sys
import argparse
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 定位当前在用的数据库(优先级与 backend/models.get_db_url 一致)
os.environ.setdefault("DATABASE_URL", "")
from backend.config import settings  # noqa: E402

if os.getenv("GW_DB_PATH"):
    DB_PATH = Path(os.getenv("GW_DB_PATH"))
else:
    DB_PATH = Path(__file__).resolve().parent.parent / "dist" / "data" / "gateway.db"
    if not DB_PATH.exists():
        DB_PATH = ROOT / "backend" / "data" / "gateway.db"
print("DB:", DB_PATH, "| exists:", DB_PATH.exists())

import sqlite3  # noqa: E402

con = sqlite3.connect(str(DB_PATH))
con.row_factory = sqlite3.Row
cur = con.cursor()


def today_success_calls(uid):
    """今天该用户成功真实调用数: 流式/非流式都要算, 探测(speed_test)不算"""
    cur.execute(
        """
        SELECT COUNT(*) FROM request_traces
        WHERE user_id = :uid
          AND success = 1
          AND (request_type = 'real' OR request_type IS NULL)
          AND substr(created_at, 1, 10) = :today
        """,
        {"uid": uid, "today": datetime.now().strftime("%Y-%m-%d")},
    )
    return cur.fetchone()[0]


users = cur.execute("SELECT id, username, quota_used_today, quota_reset_at FROM users").fetchall()
if not users:
    print("没有用户, 无需回填")
    sys.exit(0)

dry = "--write" not in sys.argv
print("模式:", "DRY-RUN(仅预览)" if dry else "WRITE(写库)")

for u in users:
    real = today_success_calls(u["id"])
    print(
        f"user {u['id']} {u['username']}: quota_used_today={u['quota_used_today']} "
        f"-> 今日真实成功调用={real} | reset_at={u['quota_reset_at']}"
    )
    if not dry:
        cur.execute(
            "UPDATE users SET quota_used_today = :v WHERE id = :uid",
            {"v": real, "uid": u["id"]},
        )

if not dry:
    con.commit()
    print("已写入。")
else:
    print("未修改任何数据。加 --write 参数执行写入。")

con.close()