# ============================================================
# 路由组同名去重迁移 — 修复"剔除的模型编辑后又被勾选"
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from backend.models import Base, _migrate_columns


def _old_schema(engine):
    """按旧版表结构建 routers 表(无唯一约束, 允许同名重复行)"""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE routers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name VARCHAR(64) NOT NULL,
                models TEXT,
                strategy VARCHAR(16),
                created_at DATETIME
            )
        """))


def _insert(engine, user_id, name, models, strategy="quality"):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO routers (user_id, name, models, strategy) "
            "VALUES (:u, :n, :m, :s)"
        ), {"u": user_id, "n": name, "m": models, "s": strategy})


def test_dedupe_keeps_max_id_row(tmp_path):
    """同名多行去重后只保留 id 最大的一行(即读路径一直展示的行)"""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    _old_schema(engine)
    _insert(engine, 4, "high_quality", '["A-m1"]')
    _insert(engine, 4, "high_quality", '["A-m2", "B-m3"]')
    _insert(engine, 4, "high_quality", '["C-m4"]')
    _insert(engine, 4, "quick", '["D-m1"]', "fastest")

    _migrate_columns(engine)

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, name, models FROM routers ORDER BY id")).fetchall()
    assert len(rows) == 2  # high_quality 只剩 1 行 + quick 1 行
    by_name = {r[1]: r for r in rows}
    assert by_name["high_quality"][0] == 3          # 保留 max(id)=3
    assert by_name["high_quality"][2] == '["C-m4"]'  # 恰好是用户看到的最新内容
    assert by_name["quick"][2] == '["D-m1"]'


def test_dedupe_per_user_name(tmp_path):
    """去重按 (user_id, name) 分组, 不同用户同名互不影响"""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    _old_schema(engine)
    _insert(engine, 1, "g", '["u1-old"]')
    _insert(engine, 1, "g", '["u1-new"]')
    _insert(engine, 2, "g", '["u2-only"]')

    _migrate_columns(engine)

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT user_id, id, models FROM routers ORDER BY user_id, id"
        )).fetchall()
    assert rows == [(1, 2, '["u1-new"]'), (2, 3, '["u2-only"]')]


def test_migration_adds_unique_index(tmp_path):
    """迁移后唯一索引生效: 再次插入同名行被拒绝"""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    _old_schema(engine)
    _insert(engine, 4, "high_quality", '["A-m1"]')

    _migrate_columns(engine)

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO routers (user_id, name, models) "
                "VALUES (4, 'high_quality', '[\"X-m9\"]')"
            ))


def test_migration_idempotent(tmp_path):
    """重复执行迁移不报错、不丢数据"""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    _old_schema(engine)
    _insert(engine, 4, "g", '["A-m1"]')
    _insert(engine, 4, "g", '["A-m2"]')

    _migrate_columns(engine)
    _migrate_columns(engine)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM routers")).fetchall()
    assert rows == [(2,)]


def test_fresh_create_all_has_unique_constraint(tmp_path):
    """全新库建表即带唯一约束(新装直接从根上杜绝重复行)"""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    _insert(engine, 4, "quick", '["A-m1"]')
    with pytest.raises(IntegrityError):
        _insert(engine, 4, "quick", '["A-m1"]')