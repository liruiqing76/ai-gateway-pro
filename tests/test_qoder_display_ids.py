# ============================================================
# qoder 模型 ID 迁移 — 内部代号(qfmodel) → 对外真名(Qwen3.8-Flash)
#
# 背景: 上游签名只认内部代号, 但用户在路由分组界面看到 qfmodel/dmodel
# 完全不知道是什么模型。改造后对外 id 一律真名, 签名时 resolve_model_key
# 反查回代号; 老库里已存的代号要在启动迁移时改写过来。
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text

from backend.models import Base, _migrate_columns


def _json_list(conn, sql, params=None):
    """裸 SQL 读出的 JSON 列是字符串, 这里解出来比对"""
    import json
    raw = conn.execute(text(sql), params or {}).scalar()
    return json.loads(raw) if isinstance(raw, str) else raw


def _insert_provider(engine, name, models, disabled=None):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO providers (user_id, name, base_url, api_key_encrypted, "
            "models, disabled_models) VALUES (1, :n, 'http://x', 'k', :m, :d)"
        ), {"n": name, "m": models, "d": disabled or '[]'})


def _insert_router(engine, name, models):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO routers (user_id, name, models, strategy) "
            "VALUES (1, :n, :m, 'quality')"
        ), {"n": name, "m": models})


def _insert_rate(engine, provider_name, model, rate):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO model_rates (user_id, provider_name, model, rate, source) "
            "VALUES (1, :p, :m, :r, 'manual')"
        ), {"p": provider_name, "m": model, "r": rate})


def test_provider_and_router_codes_become_display_names(tmp_path):
    """providers/models、路由组成员、倍率表里的代号都改写真名"""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    _insert_provider(engine, "qodercn", '["qfmodel", "dmodel"]', '["mmodel"]')
    _insert_router(engine, "qodercn", '["qodercn-qfmodel", "qodercn-dmodel"]')
    _insert_rate(engine, "qodercn", "kmodel_latest", 1.4)

    _migrate_columns(engine)

    with engine.connect() as conn:
        p = conn.execute(text(
            "SELECT models, disabled_models FROM providers WHERE name='qodercn'"
        )).one()
        r = _json_list(conn, "SELECT models FROM routers WHERE name='qodercn'")
        rate = conn.execute(text(
            "SELECT model, rate FROM model_rates WHERE provider_name='qodercn'"
        )).one()
    import json
    assert json.loads(p[0]) == ["Qwen3.8-Flash", "DeepSeek-V4-Pro"]
    assert json.loads(p[1]) == ["MiniMax-M2.7"]
    assert r == ["qodercn-Qwen3.8-Flash", "qodercn-DeepSeek-V4-Pro"]
    # 真名改了, 用户手填的倍率必须原样保留
    assert tuple(rate) == ("Kimi-K3", 1.4)


def test_intl_channel_uses_intl_table(tmp_path):
    """国际版独立代号(Sonus/MiniMax-M3)用 INTEL 表映射, 不与国区串味"""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    _insert_provider(engine, "qoder", '["smodel", "mmodel"]')
    _insert_provider(engine, "qodercn", '["mmodel"]')

    _migrate_columns(engine)

    with engine.connect() as conn:
        intl = _json_list(conn, "SELECT models FROM providers WHERE name='qoder'")
        cn = _json_list(conn, "SELECT models FROM providers WHERE name='qodercn'")
    assert intl == ["Sonus", "MiniMax-M3"]
    assert cn == ["MiniMax-M2.7"]          # 国区同名代号是真名里的另一个


def test_migration_idempotent_and_leaves_other_channels(tmp_path):
    """重复执行不改变结果; 其它渠道的模型名一个字都不动"""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    _insert_provider(engine, "qodercn", '["qfmodel"]')
    _insert_provider(engine, "nvidia", '["qfmodel", "deepseek-v3"]')
    _insert_router(engine, "qodercn", '["qodercn-qfmodel"]')
    _insert_router(engine, "mixed", '["qodercn-qfmodel", "nvidia-qfmodel"]')

    _migrate_columns(engine)
    _migrate_columns(engine)

    with engine.connect() as conn:
        qcn = _json_list(conn, "SELECT models FROM providers WHERE name='qodercn'")
        nv = _json_list(conn, "SELECT models FROM providers WHERE name='nvidia'")
        mixed = _json_list(conn, "SELECT models FROM routers WHERE name='mixed'")
    assert qcn == ["Qwen3.8-Flash"]
    assert nv == ["qfmodel", "deepseek-v3"]      # 非 qoder 渠道不受影响
    # 混组里只有 qodercn 前缀那条被改写
    assert mixed == ["qodercn-Qwen3.8-Flash", "nvidia-qfmodel"]
