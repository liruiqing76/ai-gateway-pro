"""状态文件目录解析: 与真实数据库同级。

历史实现只看 `GW_DB_PATH`(桌面 exe 模式), 于是按 `GW_DATABASE_URL` 启动时
(源码/detached 运行)状态文件落到 `backend/data/`, 而数据库在 `dist/data/` ——
打包换库时状态"凭空丢失"。这里统一从实际 DB URL 推导, 让
`channel_checkin_state.json` / `lane_state.json` 始终跟着库走。

优先级:
  1. `GW_STATE_DIR`(显式覆盖, 测试与运维用)
  2. sqlite 文件库 → 该文件所在目录
  3. `GW_DB_PATH` → 其父目录(桌面模式)
  4. 兜底 `backend/data/`
"""
from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    explicit = os.environ.get("GW_STATE_DIR")
    if explicit:
        d = Path(explicit)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # sqlite 文件库: 状态跟着 .db 走(最常见, 也最不容易错)
    try:
        from backend.models import get_db_url
        url = get_db_url()
    except Exception:
        url = ""
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///"):]
        if raw and raw != ":memory:":
            d = Path(raw).resolve().parent
            d.mkdir(parents=True, exist_ok=True)
            return d

    gw_path = os.environ.get("GW_DB_PATH")
    if gw_path:
        d = Path(gw_path).resolve().parent
        d.mkdir(parents=True, exist_ok=True)
        return d

    d = Path(__file__).resolve().parent.parent / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d
