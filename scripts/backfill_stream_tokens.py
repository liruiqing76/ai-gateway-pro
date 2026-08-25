# ============================================================
# 一次性回填: 修复历史流式请求 token 全为 0 的聊天记录与用量记录
#
# 背景: 2026-08-25 之前的流式路径没有 token 统计, 成功请求在
# chat_logs 里落了 total_tokens=0, 且完全没写 usage_logs。
# 这些行的原文(prompt_text/completion_text)仍在库中, 可按现行
# CJK 感知估算口径(estimate_tokens_from_text)回填近似值。
#
# 行为:
#   1. 找出 success=1 且 COALESCE(total_tokens,0)=0 的 chat_logs 行
#   2. 用存储原文估算 prompt/completion/total tokens 并更新
#   3. 为同一批调用补插 usage_logs 行(error_msg='estimated-backfill'
#      作为幂等标记, 重复执行不会二次插入)
#
# 用法: .venv\Scripts\python.exe scripts/backfill_stream_tokens.py [db_path]
#   db_path 缺省使用与后端一致的定位逻辑(dist/data 或 backend/data)
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402


def pick_db() -> str:
    """优先桌面版数据库, 其次开发库(与 models.get_db_url 优先级一致)"""
    candidates = [
        Path(__file__).resolve().parent.parent / "dist" / "data" / "gateway.db",
        Path(__file__).resolve().parent.parent / "backend" / "data" / "gateway.db",
    ]
    if len(sys.argv) > 1:
        return str(Path(sys.argv[1]))
    for p in candidates:
        if p.exists():
            return str(p)
    raise SystemExit("找不到 gateway.db, 请传入路径")


def main():
    from backend.core.chat_log import estimate_tokens_from_text

    db = pick_db()
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    print(f"目标库: {db}")

    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, user_id, provider_name, model_name, original_model, "
            "prompt_text, completion_text, created_at "
            "FROM chat_logs WHERE success=1 AND COALESCE(total_tokens,0)=0"
        )).fetchall()
        print(f"待回填聊天行: {len(rows)}")
        if not rows:
            return

        patched = 0
        for (cid, uid, prov, model, orig, pt_text, ct_text, created) in rows:
            pt = estimate_tokens_from_text(pt_text or "")
            ct = estimate_tokens_from_text(ct_text or "")
            conn.execute(text(
                "UPDATE chat_logs SET prompt_tokens=:pt, completion_tokens=:ct, "
                "total_tokens=:tt WHERE id=:id"
            ), {"pt": pt, "ct": ct, "tt": pt + ct, "id": cid})
            # 补插用量行: error_msg 作为幂等标记
            dup = conn.execute(text(
                "SELECT 1 FROM usage_logs WHERE error_msg='estimated-backfill' "
                "AND user_id=:uid AND model_name=:m AND created_at=:ts LIMIT 1"
            ), {"uid": uid, "m": orig, "ts": created}).first()
            if not dup:
                conn.execute(text(
                    "INSERT INTO usage_logs (user_id, provider_name, model_name, "
                    "prompt_tokens, completion_tokens, total_tokens, cost, latency_ms, "
                    "success, error_msg, created_at) VALUES (:uid, :prov, :model, "
                    ":pt, :ct, :tt, 0, NULL, 1, 'estimated-backfill', :ts)"
                ), {"uid": uid, "prov": prov or "-", "model": orig or model,
                    "pt": pt, "ct": ct, "tt": pt + ct, "ts": created})
            patched += 1
            if patched % 200 == 0:
                print(f"  已处理 {patched}...")
        print(f"回填完成: {patched} 行")

        check = conn.execute(text(
            "SELECT COUNT(*) FROM chat_logs WHERE success=1 AND COALESCE(total_tokens,0)=0"
        )).scalar()
        print(f"剩余零token成功行: {check}")


if __name__ == "__main__":
    main()
