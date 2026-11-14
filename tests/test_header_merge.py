# -*- coding: utf-8 -*-
"""头合并大小写不敏感测试。"""
from backend.core.proxy import _merge_headers_ci


def test_extra_overrides_case_insensitively():
    base = {"Content-Type": "application/json", "Authorization": "Bearer x"}
    extra = {"content-type": "application/json", "x-sdk-date": "20260101T000000Z"}
    merged = _merge_headers_ci(base, extra)
    # 关键: 不能同时存在两个不同大小写的 content-type
    lowered = [k.lower() for k in merged]
    assert lowered.count("content-type") == 1
    assert merged["Content-Type"] == "application/json"
    assert merged["x-sdk-date"] == "20260101T000000Z"
    assert merged["Authorization"] == "Bearer x"


def test_extra_adds_new_keys():
    base = {"Content-Type": "application/json"}
    extra = {"Chat-Id": "abc", "lang": "en"}
    merged = _merge_headers_ci(base, extra)
    assert merged["Chat-Id"] == "abc"
    assert merged["lang"] == "en"
    assert merged["Content-Type"] == "application/json"


def test_empty_extra_returns_base():
    base = {"Content-Type": "application/json"}
    assert _merge_headers_ci(base, {}) == base
    assert _merge_headers_ci(base, None) == base


def test_authorization_override():
    """渠道自带的 Authorization 必须覆盖默认 Bearer(不能出现两个)。"""
    base = {"Content-Type": "application/json", "Authorization": "Bearer pkg"}
    extra = {"Authorization": "Cloud-IDE-JWT tok"}
    merged = _merge_headers_ci(base, extra)
    lowered = [k.lower() for k in merged]
    assert lowered.count("authorization") == 1
    assert merged["Authorization"] == "Cloud-IDE-JWT tok"
