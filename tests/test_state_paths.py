"""状态目录必须跟着真实数据库走。

历史 bug: `lane_state` / `channel_checkin_state` 只看 `GW_DB_PATH`, 于是用
`GW_DATABASE_URL` 指向 dist/data 启动时, 状态文件被写到 backend/data ——
换个库/打包发布时状态"凭空丢失"(签到防重失效、车道徽标消失)。
"""
import os

import pytest

from backend.core import state_paths


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("GW_STATE_DIR", "GW_DB_PATH", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    yield monkeypatch


def test_state_dir_follows_sqlite_db_url(clean_env, tmp_path):
    db = tmp_path / "sub" / "gateway.db"
    clean_env.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    got = state_paths.state_dir()
    assert got == db.resolve().parent
    assert got.is_dir()


def test_explicit_state_dir_wins(clean_env, tmp_path):
    clean_env.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'a' / 'g.db').as_posix()}")
    override = tmp_path / "custom"
    clean_env.setenv("GW_STATE_DIR", str(override))
    assert state_paths.state_dir() == override
    assert override.is_dir()


def test_gw_db_path_used_when_no_url(clean_env, tmp_path):
    clean_env.setenv("GW_DB_PATH", str(tmp_path / "desk" / "gateway.db"))
    assert state_paths.state_dir() == (tmp_path / "desk").resolve()


def test_memory_db_falls_through(clean_env, tmp_path):
    """sqlite 内存库没有文件目录, 必须继续往下走而不是解析成 cwd。"""
    clean_env.setenv("DATABASE_URL", "sqlite:///:memory:")
    clean_env.setenv("GW_DB_PATH", str(tmp_path / "m" / "gateway.db"))
    assert state_paths.state_dir() == (tmp_path / "m").resolve()


def test_non_sqlite_url_falls_through(clean_env, tmp_path):
    clean_env.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    clean_env.setenv("GW_DB_PATH", str(tmp_path / "pg" / "gateway.db"))
    assert state_paths.state_dir() == (tmp_path / "pg").resolve()


def test_checkin_and_lane_share_dir(clean_env, tmp_path):
    db = tmp_path / "shared" / "gateway.db"
    clean_env.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    from backend.core import checkin, lane_state
    assert checkin._state_dir() == lane_state._state_dir() == db.resolve().parent
