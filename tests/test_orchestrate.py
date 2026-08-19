# ============================================================
# 多模型编排 — 解析器 + 执行测试
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from backend.core.orchestrate import parse_pipeline, run_relay, run_review


class TestParsePipeline:
    def test_relay_format(self):
        result = parse_pipeline("relay:大模型/小模型")
        assert result is not None
        mode, stages = result
        assert mode == "relay"
        assert stages == ["大模型", "小模型"]

    def test_review_format(self):
        result = parse_pipeline("review:大模型/小模型")
        assert result is not None
        mode, stages = result
        assert mode == "review"
        assert stages == ["大模型", "小模型"]

    def test_plain_model_returns_none(self):
        assert parse_pipeline("deepseek-v4-flash") is None
        assert parse_pipeline("quick") is None

    def test_no_slash_returns_none(self):
        assert parse_pipeline("relay:大模型") is None

    def test_more_than_two_stages_relay(self):
        result = parse_pipeline("relay:A/B/C")
        assert result is not None
        mode, stages = result
        assert mode == "relay"
        assert stages == ["A", "B", "C"]

    def test_review_requires_two(self):
        assert parse_pipeline("review:A/B/C") is None

    def test_empty_stage_returns_none(self):
        assert parse_pipeline("relay:A/") is None
        assert parse_pipeline("relay:/B") is None

    def test_unknown_mode_returns_none(self):
        assert parse_pipeline("debate:A/B") is None

    def test_case_insensitive_mode(self):
        result = parse_pipeline("RELAY:大模型/小模型")
        assert result is not None
        assert result[0] == "relay"


# ============================================================
# 执行测试 — 用假 forward 函数
# ============================================================

def fake_resp(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {}}


@pytest.mark.asyncio
class TestRunRelay:
    async def test_two_stages_sequential(self):
        calls = []

        async def fake_forward(stage, body):
            calls.append((stage, body))
            prompt = body["messages"][-1]["content"]
            return fake_resp(f"[{stage}回答]")

        body = {"messages": [{"role": "user", "content": "写个文案"}]}
        result = await run_relay(["大模型", "小模型"], body, fake_forward)

        assert result is not None
        # 两次调用, 顺序正确
        assert [c[0] for c in calls] == ["大模型", "小模型"]
        # 第一级用原始 prompt
        first_prompt = calls[0][1]["messages"][-1]["content"]
        assert "写个文案" in first_prompt
        # 第二级包含上一级输出
        second_prompt = calls[1][1]["messages"][-1]["content"]
        assert "[大模型回答]" in second_prompt

    async def test_first_stage_failure_skips_to_second(self):
        calls = []

        async def fake_forward(stage, body):
            calls.append(stage)
            if stage == "大模型":
                return None  # 失败
            return fake_resp("[小模型回答]")

        body = {"messages": [{"role": "user", "content": "你好"}]}
        result = await run_relay(["大模型", "小模型"], body, fake_forward)
        assert result is not None
        assert calls == ["大模型", "小模型"]
        assert result["choices"][0]["message"]["content"] == "[小模型回答]"

    async def test_all_fail_returns_none(self):
        async def fake_forward(stage, body):
            return None

        body = {"messages": [{"role": "user", "content": "你好"}]}
        result = await run_relay(["大模型", "小模型"], body, fake_forward)
        assert result is None

    async def test_preserves_system_message(self):
        calls = []

        async def fake_forward(stage, body):
            calls.append(body["messages"])
            return fake_resp("ok")

        body = {
            "messages": [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "你好"},
            ]
        }
        await run_relay(["A", "B"], body, fake_forward)
        # 所有阶段都保留 system
        for msgs in calls:
            roles = [m["role"] for m in msgs]
            assert "system" in roles
            assert roles[-1] == "user"


@pytest.mark.asyncio
class TestRunReview:
    async def test_main_then_review(self):
        calls = []

        async def fake_forward(stage, body):
            calls.append(stage)
            prompt = body["messages"][-1]["content"]
            if stage == "大模型":
                return fake_resp("[初稿]")
            return fake_resp("[修订版]")

        body = {"messages": [{"role": "user", "content": "写一段话"}]}
        result = await run_review(["大模型", "小模型"], body, fake_forward)

        assert result is not None
        assert calls == ["大模型", "小模型"]
        assert result["choices"][0]["message"]["content"] == "[修订版]"

    async def test_review_failure_returns_draft(self):
        async def fake_forward(stage, body):
            if stage == "大模型":
                return fake_resp("[初稿]")
            return None  # 审阅失败

        body = {"messages": [{"role": "user", "content": "写一段话"}]}
        result = await run_review(["大模型", "小模型"], body, fake_forward)
        assert result is not None
        assert result["choices"][0]["message"]["content"] == "[初稿]"

    async def test_main_failure_returns_none(self):
        async def fake_forward(stage, body):
            return None

        body = {"messages": [{"role": "user", "content": "写一段话"}]}
        result = await run_review(["大模型", "小模型"], body, fake_forward)
        assert result is None

    async def test_review_prompt_contains_draft(self):
        captured = {}

        async def fake_forward(stage, body):
            if stage == "大模型":
                return fake_resp("[初稿内容]")
            captured["prompt"] = body["messages"][-1]["content"]
            return fake_resp("[修订]")

        body = {"messages": [{"role": "user", "content": "写文章"}]}
        await run_review(["大模型", "小模型"], body, fake_forward)
        assert "[初稿内容]" in captured["prompt"]
        assert "写文章" in captured["prompt"]