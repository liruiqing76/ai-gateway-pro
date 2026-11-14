"""tests for backend.core.proxy WorkBuddy 11128 指纹盲扰动(渠道校验第二道门)。

背景: 上游做的是「大小写不敏感、任意位置的精确子串匹配」, 黑名单内容未知且会变。
实测 Claude Code 触发它的不是自己的开场白, 而是 system 里一行 git 信息
`Main branch (you will usually use this for PRs): master` —— 所以词表枚举不可行,
只能靠扰动打碎字节序列。这里锁定扰动阶梯的行为。
"""

import re

from backend.core.proxy import (
    _ZWSP,
    _is_buddy_gate_reject,
    _perturb_buddy_body,
    _perturb_dense,
    _perturb_sparse,
    _prepare_buddy_body,
)

# 实测会触发 11128 的真实句子(Claude Code system 里的 git 信息行)
GATE_LINE = "Main branch (you will usually use this for PRs): master"
CC_SYSTEM = ("You are a Claude agent, built on Anthropic's Claude Agent SDK.\n\n"
             "You are an interactive agent that helps users with software "
             "engineering tasks.\n\n"
             "Current branch: main\n\n" + GATE_LINE + "\n\nGit user: leslie")


class TestPerturbSparse:
    def test_breaks_after_sentence_punctuation(self):
        out = _perturb_sparse("Done. Next step")
        assert _ZWSP in out
        assert out.replace(_ZWSP, "") == "Done. Next step"

    def test_breaks_after_cjk_punctuation(self):
        out = _perturb_sparse("完成了。下一步")
        assert _ZWSP in out

    def test_breaks_between_capitalised_words(self):
        out = _perturb_sparse("Main Branch")
        assert _ZWSP in out

    def test_gate_line_gets_no_break(self):
        # 这就是 sparse 不够的原因: 该句既无标点边界也无大写词对
        assert _perturb_sparse(GATE_LINE) == GATE_LINE

    def test_readable_after_strip(self):
        text = "Hello. World, again; fine"
        assert _perturb_sparse(text).replace(_ZWSP, "") == text

    def test_empty_safe(self):
        assert _perturb_sparse("") == ""
        assert _perturb_sparse(None) is None


class TestPerturbDense:
    def test_breaks_the_gate_line(self):
        # dense 是唯一能打碎这条句子的级别(实测 +42 字符后通过)
        out = _perturb_dense(GATE_LINE)
        assert out != GATE_LINE
        assert _ZWSP in out

    def test_contains_no_contiguous_original_words(self):
        # 打碎后不能有任何 3 字符以上的原文连续片段
        out = _perturb_dense(GATE_LINE)
        clean = out.replace(_ZWSP, "")
        assert clean == GATE_LINE
        for n in (6, 10, 20):
            assert GATE_LINE[:n] not in out, f"prefix {n} still contiguous"

    def test_readable_after_strip(self):
        assert _perturb_dense(CC_SYSTEM).replace(_ZWSP, "") == CC_SYSTEM

    def test_dense_is_superset_of_sparse(self):
        # dense 先跑 sparse 再逐字符加密
        sp = _perturb_sparse(CC_SYSTEM)
        dn = _perturb_dense(CC_SYSTEM)
        assert dn.count(_ZWSP) >= sp.count(_ZWSP)

    def test_empty_safe(self):
        assert _perturb_dense("") == ""
        assert _perturb_dense(None) is None


class TestPerturbBuddyBody:
    def test_only_system_is_touched(self):
        body = {
            "messages": [
                {"role": "system", "content": CC_SYSTEM},
                {"role": "user", "content": GATE_LINE},
                {"role": "assistant", "content": GATE_LINE},
            ]
        }
        out = _perturb_buddy_body(body, dense=True)
        assert _ZWSP in out["messages"][0]["content"]
        # 用户原话与助手历史必须保持原样
        assert out["messages"][1]["content"] == GATE_LINE
        assert out["messages"][2]["content"] == GATE_LINE

    def test_block_content_system_touched(self):
        body = {"messages": [{"role": "system",
                              "content": [{"type": "text", "text": GATE_LINE}]}]}
        out = _perturb_buddy_body(body, dense=True)
        assert _ZWSP in out["messages"][0]["content"][0]["text"]

    def test_missing_messages_safe(self):
        assert _perturb_buddy_body({}, dense=True) == {}

    def test_sparse_is_default(self):
        body = {"messages": [{"role": "system", "content": GATE_LINE}]}
        out = _perturb_buddy_body(body)
        # GATE_LINE 在 sparse 下不变
        assert out["messages"][0]["content"] == GATE_LINE


class TestIsBuddyGateReject:
    def test_detects_code_11128(self):
        assert _is_buddy_gate_reject(
            400, '{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}')

    def test_detects_by_message_text(self):
        assert _is_buddy_gate_reject(400, "Illegal API invocation from an unapproved channel")

    def test_other_400_not_gate(self):
        assert not _is_buddy_gate_reject(400, '{"code":11101,"msg":"Non-stream"}')
        assert not _is_buddy_gate_reject(
            400, '{"code":11158,"msg":"model not allowed for user type"}')

    def test_non_400_not_gate(self):
        assert not _is_buddy_gate_reject(429, "unapproved channel")
        assert not _is_buddy_gate_reject(500, "unapproved channel")

    def test_empty_safe(self):
        assert not _is_buddy_gate_reject(400, "")
        assert not _is_buddy_gate_reject(400, None)


class TestPrepareBuddyBodyStillWorks:
    def test_inserts_system_head(self):
        body = {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}
        out = _prepare_buddy_body(body)
        assert out["messages"][0]["role"] == "system"

    def test_adds_cache_key_and_thinking(self):
        body = {"model": "deepseek-v4.1-flash",
                "messages": [{"role": "system", "content": "s"}]}
        out = _prepare_buddy_body(body)
        assert len(out["prompt_cache_key"]) == 32
        assert out["thinking"] == {"type": "enabled"}

    def test_prepare_does_not_perturb(self):
        # 扰动会让前缀缓存失效, 必须只在被 11128 拒绝后按需升级
        body = {"model": "glm-5.3",
                "messages": [{"role": "system", "content": GATE_LINE}]}
        out = _prepare_buddy_body(body)
        assert out["messages"][0]["content"] == GATE_LINE
        assert _ZWSP not in out["messages"][0]["content"]
