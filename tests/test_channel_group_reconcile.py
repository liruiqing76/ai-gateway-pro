"""渠道路由组自愈: 组内条目随渠道模型清单对齐。

真实场景: WorkBuddy 登录后上游新增了 gpt-6-luna, 渠道 Provider 有 31 个模型,
但路由组只在登录那一刻建过一次 → 组里 30 个, 客户端按组名 `workbuddy` 调用
就永远摸不到那个新模型。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from backend import models as models_mod
from backend.api import channels as channels_mod
from backend.models import Provider, Router, User, get_session


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "gw.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}",
                           connect_args={"check_same_thread": False})
    models_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr(models_mod, "_engine", None)
    monkeypatch.setattr(models_mod, "_get_engine", lambda: engine)

    session = get_session()
    user = User(username="u1", email="u1@example.com",
                password_hash="x", api_key="sk-gw-test-group-reconcile")
    session.add(user)
    session.commit()
    uid = user.id
    session.close()
    return {"user_id": uid}


def _add_provider(user_id, name, models):
    session = get_session()
    p = Provider(user_id=user_id, name=name, base_url="https://x.test",
                 api_key_encrypted="", models=list(models), enabled=True)
    session.add(p)
    session.commit()
    session.close()


def _enabled(user_id):
    session = get_session()
    try:
        return session.query(Provider).filter_by(
            user_id=user_id, enabled=True).all()
    finally:
        session.close()


def _group(user_id, name):
    session = get_session()
    try:
        r = session.query(Router).filter_by(user_id=user_id, name=name).first()
        return list(r.models or []) if r else None
    finally:
        session.close()


class TestMergeGroupModels:
    def test_builds_prefixed_list(self):
        out = channels_mod._merge_group_models("workbuddy", None, ["a", "b"])
        assert out == ["workbuddy-a", "workbuddy-b"]

    def test_drops_stale_channel_entries(self):
        out = channels_mod._merge_group_models(
            "workbuddy", ["workbuddy-a", "workbuddy-gone"], ["a"])
        assert out == ["workbuddy-a"]

    def test_keeps_foreign_entries(self):
        out = channels_mod._merge_group_models(
            "trae", ["trae-a", "kilo-free-x"], ["a", "b"])
        assert out == ["trae-a", "trae-b", "kilo-free-x"]

    def test_no_duplicate_on_repeat(self):
        out = channels_mod._merge_group_models(
            "cline", ["cline-a", "cline-b", "cline-a"], ["a", "b"])
        assert out == ["cline-a", "cline-b"]


class TestReconcile:
    def test_creates_missing_group(self, env):
        _add_provider(env["user_id"], "workbuddy", ["m1", "m2"])
        session = get_session()
        try:
            changed = channels_mod._reconcile_channel_groups(
                session, env["user_id"], _enabled(env["user_id"]))
            session.commit()
        finally:
            session.close()
        assert changed == 1
        assert _group(env["user_id"], "workbuddy") == ["workbuddy-m1", "workbuddy-m2"]

    def test_adds_new_upstream_model(self, env):
        _add_provider(env["user_id"], "workbuddy", ["m1", "m2"])
        session = get_session()
        channels_mod._reconcile_channel_groups(
            session, env["user_id"], _enabled(env["user_id"]))
        session.commit()
        session.close()
        # 上游后来多了一个模型
        session = get_session()
        p = session.query(Provider).filter_by(name="workbuddy").first()
        p.models = ["m1", "m2", "gpt-6-luna"]
        session.commit()
        session.close()

        session = get_session()
        try:
            changed = channels_mod._reconcile_channel_groups(
                session, env["user_id"], _enabled(env["user_id"]))
            session.commit()
        finally:
            session.close()
        assert changed == 1
        assert _group(env["user_id"], "workbuddy") == [
            "workbuddy-m1", "workbuddy-m2", "workbuddy-gpt-6-luna"]

    def test_second_call_is_noop(self, env):
        _add_provider(env["user_id"], "trae", ["a", "b"])
        session = get_session()
        channels_mod._reconcile_channel_groups(
            session, env["user_id"], _enabled(env["user_id"]))
        session.commit()
        try:
            again = channels_mod._reconcile_channel_groups(
                session, env["user_id"], _enabled(env["user_id"]))
            assert again == 0  # 稳态不写库
        finally:
            session.close()

    def test_ignores_non_channel_provider(self, env):
        _add_provider(env["user_id"], "my-openai", ["gpt-4o"])
        session = get_session()
        try:
            changed = channels_mod._reconcile_channel_groups(
                session, env["user_id"], _enabled(env["user_id"]))
        finally:
            session.close()
        assert changed == 0
        assert _group(env["user_id"], "my-openai") is None

    def test_disabled_provider_untouched(self, env):
        _add_provider(env["user_id"], "trae", ["a"])
        session = get_session()
        p = session.query(Provider).filter_by(name="trae").first()
        p.enabled = False
        session.commit()
        session.close()
        session = get_session()
        try:
            changed = channels_mod._reconcile_channel_groups(
                session, env["user_id"], _enabled(env["user_id"]))
        finally:
            session.close()
        assert changed == 0
        assert _group(env["user_id"], "trae") is None
