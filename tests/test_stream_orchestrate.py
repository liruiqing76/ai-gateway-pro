# ============================================================
# 流式编排端到端测试
# relay/review 最后一级流式输出
# ============================================================
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from backend.core.orchestrate import run_relay_stream, run_review_stream


def fake_resp(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {}}


def make_stream_gen(chunks: list[str]):
    """构造一个假的流式生成器,产出 SSE 字节"""
    async def gen():
        for c in chunks:
            yield json.dumps({"choices": [{"delta": {"content": c}}]}, ensure_ascii=False).encode()
    return gen()


async def collect(agen) -> str:
    """收集异步生成器的全部字节并解码"""
    data = b""
    async for chunk in agen:
        data += chunk
    return data.decode("utf-8", errors="ignore")


class TestRelayStream:
    """流式接力: 前序级非流式, 末级流式"""

    @pytest.mark.asyncio
    async def test_streams_last_stage_only(self):
        """只有最后一级调用流式 forward"""
        ns_calls = []
        stream_calls = []

        async def ns_forward(stage, body):
            ns_calls.append(stage)
            return fake_resp(f"[{stage}输出]")

        async def stream_forward(stage, body):
            stream_calls.append(stage)
            return make_stream_gen(["你好", "世界"]), None

        body = {"messages": [{"role": "user", "content": "写文案"}]}
        gen, err = await run_relay_stream(
            ["组A", "组B", "组C"], body, ns_forward, stream_forward
        )

        # 最后一级流式被调用, 且只有它
        assert err is None
        assert gen is not None
        assert ns_calls == ["组A", "组B"]
        assert stream_calls == ["组C"]

        # 产出内容
        text = await collect(gen)
        assert "你好" in text
        assert "世界" in text

    @pytest.mark.asyncio
    async def test_stage_input_contains_previous_output(self):
        """前序级输出应注入下一级输入"""
        captured = {}

        async def ns_forward(stage, body):
            captured[stage] = body["messages"][-1]["content"]
            return fake_resp("[中间结果]")

        async def stream_forward(stage, body):
            captured["LAST"] = body["messages"][-1]["content"]
            return make_stream_gen(["final"]), None

        body = {"messages": [{"role": "user", "content": "原始问题"}]}
        await run_relay_stream(["A", "B"], body, ns_forward, stream_forward)

        assert "原始问题" in captured["A"]          # 首级用原始 prompt
        assert "[中间结果]" in captured["LAST"]      # 末级拿到上一级(A)输出

    @pytest.mark.asyncio
    async def test_skips_failed_mid_stage(self):
        """中间级失败应跳过并继续, 末级仍流式"""
        ns_calls = []

        async def ns_forward(stage, body):
            ns_calls.append(stage)
            if stage == "B":
                return None  # 中级失败
            return fake_resp(f"[{stage}输出]")

        async def stream_forward(stage, body):
            return make_stream_gen(["ok"]), None

        body = {"messages": [{"role": "user", "content": "你好"}]}
        gen, err = await run_relay_stream(["A", "B", "C"], body, ns_forward, stream_forward)
        assert gen is not None
        assert ns_calls == ["A", "B"]
        text = await collect(gen)
        assert "ok" in text

    @pytest.mark.asyncio
    async def test_stream_last_stage_failure(self):
        """末级流式失败时返回错误信息"""
        async def ns_forward(stage, body):
            return fake_resp("[输出]")

        async def stream_forward(stage, body):
            return None, "upstream 500"

        body = {"messages": [{"role": "user", "content": "你好"}]}
        gen, err = await run_relay_stream(["A", "B"], body, ns_forward, stream_forward)
        assert gen is None
        assert err is not None

    @pytest.mark.asyncio
    async def test_no_user_message(self):
        """没有 user 消息时拒绝执行"""
        async def ns_forward(stage, body):
            return fake_resp("x")

        async def stream_forward(stage, body):
            return make_stream_gen(["x"]), None

        body = {"messages": [{"role": "system", "content": "only system"}]}
        gen, err = await run_relay_stream(["A", "B"], body, ns_forward, stream_forward)
        assert gen is None
        assert "user" in (err or "").lower() or err is not None


class TestReviewStream:
    """流式审阅: 主模型非流式, 审阅模型流式"""

    @pytest.mark.asyncio
    async def test_draft_then_stream_review(self):
        calls = []

        async def ns_forward(stage, body):
            calls.append(("ns", stage))
            return fake_resp("[初稿]")

        async def stream_forward(stage, body):
            calls.append(("stream", stage))
            # 审阅 prompt 应包含初稿
            prompt = body["messages"][-1]["content"]
            assert "[初稿]" in prompt
            return make_stream_gen(["修订版"]), None

        body = {"messages": [{"role": "user", "content": "写文章"}]}
        gen, err = await run_review_stream(["主模型", "审阅"], body, ns_forward, stream_forward)

        assert err is None
        assert gen is not None
        assert calls == [("ns", "主模型"), ("stream", "审阅")]
        text = await collect(gen)
        assert "修订版" in text

    @pytest.mark.asyncio
    async def test_main_failure(self):
        async def ns_forward(stage, body):
            return None  # 主模型失败

        async def stream_forward(stage, body):
            return make_stream_gen(["x"]), None

        body = {"messages": [{"role": "user", "content": "内容"}]}
        gen, err = await run_review_stream(["主", "审"], body, ns_forward, stream_forward)
        assert gen is None
        assert err is not None

    @pytest.mark.asyncio
    async def test_wrong_stage_count(self):
        async def ns_forward(stage, body):
            return fake_resp("x")

        async def stream_forward(stage, body):
            return make_stream_gen(["x"]), None

        body = {"messages": [{"role": "user", "content": "内容"}]}
        gen, err = await run_review_stream(["A", "B", "C"], body, ns_forward, stream_forward)
        assert gen is None

    @pytest.mark.asyncio
    async def test_review_stream_failure(self):
        """审阅流式失败: 返回错误(不回退初稿, 因为无法在流中返回非流式响应)"""
        async def ns_forward(stage, body):
            return fake_resp("[初稿]")

        async def stream_forward(stage, body):
            return None, "connection error"

        body = {"messages": [{"role": "user", "content": "内容"}]}
        gen, err = await run_review_stream(["主", "审"], body, ns_forward, stream_forward)
        assert gen is None

    @pytest.mark.asyncio
    async def test_draft_empty(self):
        """主模型返回空内容时报错"""
        async def ns_forward(stage, body):
            return fake_resp("")  # 空内容

        async def stream_forward(stage, body):
            return make_stream_gen(["x"]), None

        body = {"messages": [{"role": "user", "content": "内容"}]}
        gen, err = await run_review_stream(["主", "审"], body, ns_forward, stream_forward)
        assert gen is None


class TestBuildStageBody:
    """阶段 body 构造单元测试"""

    def test_first_stage_uses_original_prompt(self):
        from backend.core.orchestrate import build_stage_body
        body = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "原始问题"},
            ]
        }
        result = build_stage_body(body, 0, 3, "原始问题")
        # 保留 system
        assert any(m["role"] == "system" for m in result["messages"])
        # 最后一条是 user 原始问题
        assert result["messages"][-1] == {"role": "user", "content": "原始问题"}

    def test_later_stage_injects_handoff(self):
        from backend.core.orchestrate import build_stage_body
        body = {"messages": [{"role": "user", "content": "原始"}]}
        result = build_stage_body(body, 1, 3, "原始", "[上一级]")
        last = result["messages"][-1]["content"]
        assert "原始" in last
        assert "[上一级]" in last
        # 包含接力 system 消息
        assert any("继续" in str(m.get("content", "")) for m in result["messages"])

    def test_does_not_duplicate_handoff(self):
        from backend.core.orchestrate import build_stage_body
        body = {"messages": [{"role": "user", "content": "原始"}]}
        result = build_stage_body(body, 1, 2, "原始", "out")
        handoff_count = sum(1 for m in result["messages"] if "继续之前的对话" in str(m.get("content", "")))
        assert handoff_count == 1

    @pytest.mark.asyncio
    async def test_review_draft_empty(self):
        """主模型返回空内容 → 流式审阅失败"""
        async def ns_forward(stage, body):
            return fake_resp(""), None   # 主模型空内容

        async def stream_forward(stage, body):
            return make_stream_gen([""]), None

        body = {"messages": [{"role": "user", "content": "你好"}]}
        gen, err = await run_review_stream(["A", "B"], body, ns_forward, stream_forward)
        assert gen is None
        assert err is not None

    @pytest.mark.asyncio
    async def test_relay_stream_ok_prevents_fallback(self):
        """末级流式成功时不回退非流式结果"""
        async def ns_forward(stage, body):
            return fake_resp("[非流式]"), None

        async def stream_forward(stage, body):
            return make_stream_gen(["streaming", "output"]), None

        body = {"messages": [{"role": "user", "content": "你好"}]}
        gen, err = await run_relay_stream(["A", "B"], body, ns_forward, stream_forward)
        assert err is None
        assert gen is not None
        collected = b"".join([c async for c in gen]).decode()
        assert "streaming" in collected
        assert "非流式" not in collected

    @pytest.mark.asyncio
    async def test_relay_empty_first_stage_outputs(self):
        """首级返回空文本 → 跳过该级继续"""
        async def ns_forward(stage, body):
            return fake_resp(""), None

        async def stream_forward(stage, body):
            return make_stream_gen(["final"]), None

        body = {"messages": [{"role": "user", "content": "你好"}]}
        gen, err = await run_relay_stream(["A", "B"], body, ns_forward, stream_forward)
        assert gen is not None
        collected = b"".join([c async for c in gen]).decode()
        assert "final" in collected

    def test_parse_extract_text_guards(self):
        """_extract_text 对异常输入返回空字符串"""
        from backend.core.orchestrate import _extract_text, _get_prompt
        assert _extract_text(None) == ""
        assert _extract_text({}) == ""
        assert _extract_text({"choices": []}) == ""
        assert _get_prompt({"messages": []}) == ""
        assert _get_prompt({"messages": [{"role": "system", "content": "sys"}]}) == ""

    def test_build_stage_body_single_stage(self):
        """单级时首级直接用原始 prompt 且不注入交接"""
        from backend.core.orchestrate import build_stage_body
        body = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "你好"}]}
        stage_body = build_stage_body(body, 0, 1, "你好")
        msgs = stage_body["messages"]
        assert msgs[0] == {"role": "system", "content": "S"}
        assert msgs[1] == {"role": "user", "content": "你好"}
        assert len(msgs) == 2  # 无交接注入

    def test_handoff_not_duplicated(self):
        """已有交接 system 消息时不重复注入"""
        from backend.core.orchestrate import _context_handoff
        msgs = [
            {"role": "system", "content": "S"},
            {"role": "system", "content": "你正在以接力(1/2)的方式继续之前的对话。上一步的输出仅供参考。请基于用户原始问题继续处理。"},
            {"role": "user", "content": "H"},
        ]
        out = _context_handoff(msgs, 1, 2, "prev")
        assert len(out) == len(msgs)  # 不重复添加