# -*- coding: utf-8 -*-
"""路由组分组隔离: 兜底候选不得跨渠道。"""
import pytest

from backend.core.proxy import pick_available_models, pick_fallback_models
from backend.models import Provider


def _p(name, models):
    p = Provider()
    p.id = abs(hash(name)) % 100000
    p.name = name
    p.models = list(models)
    p.disabled_models = []
    p.enabled = True
    return p


@pytest.fixture()
def providers():
    return [
        _p("qodercn", ["qfmodel", "qmodel_38max"]),
        _p("trae", ["glm-5.2", "kimi-k3"]),
        _p("agnes", ["agnes-2.5-pro"]),
    ]


@pytest.fixture()
def routers():
    return {
        "qodercn": ["qodercn-qfmodel", "qodercn-qmodel_38max"],
        "trae": ["trae-glm-5.2", "trae-kimi-k3"],
    }


class TestGroupIsolation:
    def test_group_candidates_are_pure(self, providers, routers):
        cands = pick_available_models(providers, "qodercn", routers)
        assert cands
        assert {p.name for p, _ in cands} == {"qodercn"}

    def test_fallback_stays_in_group_channel(self, providers, routers):
        """组名请求时兜底只能落在该组渠道, 不得跨到 agnes/trae。"""
        fb = pick_fallback_models(providers, "qodercn", routers)
        assert fb
        assert {p.name for p, _ in fb} == {"qodercn"}

    def test_fallback_for_trae_group(self, providers, routers):
        fb = pick_fallback_models(providers, "trae", routers)
        assert {p.name for p, _ in fb} == {"trae"}

    def test_fallback_unrestricted_for_plain_model(self, providers, routers):
        """非组名请求(具体模型/None)保持原有放宽行为。"""
        fb = pick_fallback_models(providers, "agnes-2.5-pro", routers)
        assert {p.name for p, _ in fb} >= {"agnes"}

    def test_fallback_all_channels_when_model_none(self, providers, routers):
        fb = pick_fallback_models(providers, None, routers)
        assert {p.name for p, _ in fb} == {"qodercn", "trae", "agnes"}

    def test_unknown_group_falls_back_to_all(self, providers, routers):
        fb = pick_fallback_models(providers, "no_such_group", routers)
        assert {p.name for p, _ in fb} == {"qodercn", "trae", "agnes"}

    def test_prefixed_model_stays_in_its_channel(self, providers, routers):
        """`渠道名-模型名` 显式请求: 兜底只能在该渠道内换模型, 不得跨厂商。

        实测 codearts-GLM-5.2 曾兜底到 OpenRouter 的免费模型 = 答非所问。
        """
        fb = pick_fallback_models(providers, "qodercn-qfmodel", routers)
        assert fb
        assert {p.name for p, _ in fb} == {"qodercn"}

    def test_prefixed_model_other_channel(self, providers, routers):
        fb = pick_fallback_models(providers, "trae-glm-5.2", routers)
        assert {p.name for p, _ in fb} == {"trae"}

    def test_unprefixed_model_still_relaxes(self, providers, routers):
        """裸模型名(不指定渠道)保持全渠道放宽, 这是原有行为。"""
        fb = pick_fallback_models(providers, "agnes-2.5-pro", routers)
        assert len({p.name for p, _ in fb}) >= 2
