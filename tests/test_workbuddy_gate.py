# -*- coding: utf-8 -*-
"""WorkBuddy 渠道校验(11128)契约测试。

实测根因(2026-10-24, 直连 www.workbuddy.ai):
  - 只发原 chat_headers  → 400 {"code":11128,"msg":"request illegal"}
  - 补 X-Conversation-ID / -Request-ID / -Message-ID 三件套 → 200 真回文
上游把「缺少会话 id」伪装成「不是官方客户端发出的」安全文案, 必须靠三件套过门。
另一道门是首条消息必须是 system(ensureSystemHead), 两者独立且都要满足。
"""
from __future__ import annotations

import json
import re

from backend.core.channel_auth import workbuddy as wb

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _cred():
    return {"access_token": "tok-abc", "domain": "www.workbuddy.ai"}


class TestConversationHeaders:
    def test_three_ids_present(self):
        h = wb.conversation_headers()
        assert set(h) == {"X-Conversation-ID", "X-Conversation-Request-ID",
                          "X-Conversation-Message-ID"}

    def test_ids_are_uuids(self):
        h = wb.conversation_headers()
        for v in h.values():
            assert UUID_RE.match(v), v

    def test_ids_are_distinct_per_call(self):
        a = wb.conversation_headers()
        b = wb.conversation_headers()
        assert a["X-Conversation-ID"] != b["X-Conversation-ID"]

    def test_ids_distinct_within_one_call(self):
        h = wb.conversation_headers()
        assert len(set(h.values())) == 3


class TestChatHeaders:
    def test_includes_conversation_triple(self):
        h = wb.chat_headers(_cred(), "workbuddy", "deepseek-v4.1-flash")
        for key in ("X-Conversation-ID", "X-Conversation-Request-ID",
                    "X-Conversation-Message-ID"):
            assert UUID_RE.match(h[key])

    def test_keeps_original_headers(self):
        h = wb.chat_headers(_cred(), "workbuddy", "deepseek-v4.1-flash")
        assert h["Authorization"] == "Bearer tok-abc"
        assert h["X-Domain"] == "www.workbuddy.ai"
        assert h["X-Product-Code"] == "workbuddy"
        assert h["Accept"] == "text/event-stream"

    def test_buddy_product_headers(self):
        h = wb.chat_headers({"access_token": "t", "domain": "copilot.tencent.com"},
                            "buddy", "deepseek-v4.1-flash")
        assert h["X-Product-Code"] == "codebuddy"
        assert h["X-IDE-Name"] == "CodeBuddy"
        assert "X-Conversation-ID" in h

    def test_ua_by_model_family(self):
        intl = wb.chat_headers(_cred(), "workbuddy", "gpt-5.5")
        cn = wb.chat_headers(_cred(), "workbuddy", "glm-5.2")
        assert intl["User-Agent"] != cn["User-Agent"]
        assert "WorkBuddy AI" in intl["User-Agent"]


class TestPrepareBuddyBody:
    def _prep(self, body):
        from backend.core.proxy import _prepare_buddy_body
        return _prepare_buddy_body(json.loads(json.dumps(body)))

    def test_inserts_system_head_when_missing(self):
        out = self._prep({"model": "glm-5.2",
                          "messages": [{"role": "user", "content": "hi"}]})
        assert out["messages"][0]["role"] == "system"
        assert out["messages"][1]["content"] == "hi"

    def test_keeps_existing_system_head(self):
        out = self._prep({"model": "glm-5.2",
                          "messages": [{"role": "system", "content": "S"},
                                       {"role": "user", "content": "hi"}]})
        assert len(out["messages"]) == 2
        assert out["messages"][0]["content"] == "S"

    def test_adds_prompt_cache_key(self):
        out = self._prep({"model": "glm-5.2",
                          "messages": [{"role": "user", "content": "hi"}]})
        assert re.match(r"^[0-9a-f]{32}$", out["prompt_cache_key"])

    def test_preserves_existing_cache_key(self):
        out = self._prep({"model": "glm-5.2", "prompt_cache_key": "keep",
                          "messages": [{"role": "user", "content": "hi"}]})
        assert out["prompt_cache_key"] == "keep"

    def test_deepseek_gets_thinking(self):
        out = self._prep({"model": "deepseek-v4.1-flash",
                          "messages": [{"role": "user", "content": "hi"}]})
        assert out["thinking"] == {"type": "enabled"}

    def test_non_deepseek_no_thinking(self):
        out = self._prep({"model": "glm-5.2",
                          "messages": [{"role": "user", "content": "hi"}]})
        assert "thinking" not in out

    def test_empty_messages_safe(self):
        out = self._prep({"model": "glm-5.2", "messages": []})
        assert out["messages"][0]["role"] == "system"
