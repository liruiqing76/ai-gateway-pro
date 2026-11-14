# ============================================================
# 渠道协议变换器 — TRAE(SOLO) / Qoder(加密信封) / OpenCode(v2 指纹门)
#
# 这些渠道的推理面不是 OpenAI 兼容直转能覆盖的:
#   - 请求需整体重写(url/headers/body 由渠道协议决定; Qoder 还要 wasm 签名;
#     OpenCode 免费车道要过 v2 指纹门: /inference/v1 端点 + v2 UA +
#     会话四头 + read/shell 逐字工具定义, 见 opencode_free.py);
#   - 响应是自定义 SSE(TRAE 用 event: 命名事件; Qoder 每帧套 {"body":...} 信封;
#     OpenCode 是标准 OpenAI 形状只需还原被改名的工具),
#     必须先转回标准 OpenAI chunk 行, 网关全部现有设施
#     (_stream_with_fallback / StreamRecorder / Anthropic 渲染器)才能零改动复用。
#
# 契约:
#   build_infer_request(name, api_key, model, req_body)
#     -> (url, headers, body_bytes, rename)  rename=发出名→调用方原名(可 None)
#   make_line_converter(name, model) -> fn(line)->list[str]  (标准 OpenAI data 行)
#     转换中抛 TraeStreamError / RuntimeError = 流内错误(按断流处理→候选切换)
#   transformed_stream(name, api_key, model, req_body, client, timeout)
#     -> async gen[bytes]  OpenAI 形状 SSE 字节流(供 forward_stream 复用)
#   transformed_non_stream(...) -> dict  消费流聚合成 chat.completion
# ============================================================

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import qoder as qoder_mod
from . import trae
from . import trae_solo
from . import opencode_free
from . import minimax as minimax_mod
from . import minimax_wire
from .trae_solo import SoloToOpenAISse, TraeStreamError

logger = logging.getLogger("gateway.channel_transform")

UTC8 = timezone(timedelta(hours=8))

TRAE_CHANNELS_SET = {trae.CHANNEL_ID}
QODER_CHANNELS_SET = {qoder_mod.CHANNEL_ID_CN, qoder_mod.CHANNEL_ID_INTL}
OPENCODE_CHANNELS_SET = {opencode_free.CHANNEL_ID}
# MiniMax 的推理面是 Anthropic Messages 形状, 也要整体变换
MINIMAX_CHANNELS_SET = {minimax_mod.CHANNEL_ID}


def is_transformed_channel(provider_name: str) -> bool:
    return (provider_name in TRAE_CHANNELS_SET
            or provider_name in QODER_CHANNELS_SET
            or provider_name in OPENCODE_CHANNELS_SET
            or provider_name in MINIMAX_CHANNELS_SET)


# ============================================================
# 请求构造
# ============================================================

async def build_infer_request(provider_name: str, api_key: str, model: str,
                              req_body: dict) -> tuple[str, dict, bytes, dict | None]:
    """OpenAI req_body → 渠道真实请求 (url, headers, body_bytes, rename表)。

    rename 表(opencode 指纹门改名)非 None 时, 响应侧要把发出的工具名还原成
    调用方原名; 其余渠道为 None(透传即可)。
    """
    if provider_name in TRAE_CHANNELS_SET:
        creds = trae.parse_credentials_package(api_key) or {}
        if not creds.get("access_token"):
            raise RuntimeError("trae: 无可用凭据, 请先登录")
        body = dict(req_body)
        body["model"] = model
        solo = trae_solo.to_solo_body(body)
        url = trae.AGENT_HOST + trae.CHAT_PATH
        headers = trae.chat_headers(creds, stream=True)
        payload = json.dumps(solo, ensure_ascii=False).encode("utf-8")
        return url, headers, payload, None

    if provider_name in QODER_CHANNELS_SET:
        creds = qoder_mod.parse_credentials_package(api_key, provider_name)
        if not creds:
            raise RuntimeError("qoder: 无可用凭据, 请先登录")
        body = dict(req_body)
        body["model"] = model
        signed = await qoder_mod.sign_infer_request(provider_name, creds, body)
        url = signed["url"]
        headers = {k: str(v) for k, v in (signed.get("headers") or {}).items()}
        payload = (signed.get("body") or "").encode("utf-8")
        return url, headers, payload, None

    if provider_name in OPENCODE_CHANNELS_SET:
        body = dict(req_body)
        body["model"] = model
        # 本变换层只说 chat wire; responses/messages 形状模型直接判不可用,
        # 让候选切换绕开它(同步侧已按 -free 过滤, 这里是防御)
        if not opencode_free.is_chat_wire(model):
            raise RuntimeError(f"opencode: model {model} 非 chat wire, 本车道不支持")
        # 车道会话: 配额按会话计 → 渠道固定一个 canonical session;
        # api_key 空(免 key 行)时用渠道级信号铸造, 同渠道永远同会话。
        session = opencode_free.session_from_api_key(
            api_key or f"opencode::{model}")
        url, headers, payload, rename = opencode_free.build_lane_request(
            body, session)
        return url, headers, payload, rename

    if provider_name in MINIMAX_CHANNELS_SET:
        creds = minimax_mod.parse_credentials_package(api_key)
        if not creds:
            raise RuntimeError("minimax: 无可用凭据, 请先登录")
        body = dict(req_body)
        body["model"] = model
        anth = minimax_wire.to_anthropic_body(body, stream=True)
        url = minimax_mod.API_HOST + minimax_mod.INFER_PATH
        headers = minimax_mod.chat_headers(creds)
        payload = json.dumps(anth, ensure_ascii=False).encode("utf-8")
        return url, headers, payload, None

    raise RuntimeError(f"未知变换渠道: {provider_name}")


# ============================================================
# 响应逐行转换
# ============================================================

def make_line_converter(provider_name: str, model: str, rename: dict | None = None):
    """返回 fn(line:str) -> list[str] 标准 OpenAI data 行(或空表示吃掉)。"""
    if provider_name in TRAE_CHANNELS_SET:
        state = SoloToOpenAISse(model)

        def convert(line: str) -> list[str]:
            out = list(state.feed(line))
            if state.done is True:
                out.append("data: [DONE]")
                state.done = "flushed"   # 只补一次
            return out
        return convert

    if provider_name in QODER_CHANNELS_SET:
        def convert(line: str) -> list[str]:
            r = qoder_mod.unwrap_qoder_line(line)
            return [r] if r else []
        return convert

    if provider_name in OPENCODE_CHANNELS_SET:
        # OpenAI 形状透传; 指纹门改过名的工具在响应侧还原成调用方原名
        return opencode_free.make_lane_response_converter(rename or {})

    if provider_name in MINIMAX_CHANNELS_SET:
        state = minimax_wire.AnthropicToOpenAISse(model)

        def convert(line: str) -> list[str]:
            return state.feed(line)

        # 上游没给 message_stop 时补收尾(finish_reason + usage + [DONE])
        convert.flush = state.finish   # type: ignore[attr-defined]
        return convert

    raise RuntimeError(f"未知变换渠道: {provider_name}")


# ============================================================
# 流式: 渠道 SSE → OpenAI SSE 字节流
# ============================================================

async def transformed_stream(provider_name: str, api_key: str, model: str,
                             req_body: dict, client: httpx.AsyncClient,
                             timeout: float):
    """发起渠道请求并产出 OpenAI 形状 SSE 字节流(末帧 data: [DONE])。

    上游 HTTP 非 200 → 抛 TransformUpstreamError(status, text), 由调用方归类。
    """
    url, headers, payload, rename = await build_infer_request(
        provider_name, api_key, model, req_body)
    resp = await client.send(
        client.build_request("POST", url, content=payload, headers=headers,
                             timeout=httpx.Timeout(connect=15, read=timeout,
                                                  write=30, pool=10)),
        stream=True)
    if resp.status_code != 200:
        body = ""
        try:
            body = (await resp.aread()).decode("utf-8", "replace")[:400]
        except Exception:
            pass
        retry_after = _retry_after_seconds(resp.headers)
        await resp.aclose()
        raise TransformUpstreamError(resp.status_code, body, retry_after,
                                     channel=provider_name)

    convert = make_line_converter(provider_name, model, rename)
    done_emitted = {"v": False}

    async def gen():
        try:
            async for raw in resp.aiter_lines():
                # 空行是 SOLO / Anthropic 事件边界(转换器靠它 dispatch), 必须喂进去
                for line in (convert(raw) or []):
                    if line.strip() == "data: [DONE]":
                        done_emitted["v"] = True
                    yield (line + "\n\n").encode("utf-8")
            flush = getattr(convert, "flush", None)
            if flush is not None:
                # 上游缺收尾事件时的兜底(MiniMax 的 message_stop)
                for line in (flush() or []):
                    if line.strip() == "data: [DONE]":
                        done_emitted["v"] = True
                    yield (line + "\n\n").encode("utf-8")
            if not done_emitted["v"]:
                yield b"data: [DONE]\n\n"
                done_emitted["v"] = True
        finally:
            await resp.aclose()
    return gen()


def _retry_after_seconds(headers) -> int | None:
    """取上游 `retry-after`(秒)。opencode 免费额度 429 会带, 例如 26715。

    也可能是 HTTP 日期格式, 这里只认纯数字(其余一律 None, 不猜)。
    """
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        secs = int(str(raw).strip())
    except ValueError:
        return None
    return secs if 0 < secs <= 7 * 24 * 3600 else None


def _note_resume(channel: str, retry_after: int) -> None:
    """把"额度何时恢复"写进车道状态(界面显示倒计时)。失败静默, 绝不影响主流程。"""
    try:
        from backend.core import lane_state
        lane_state.note_quota_resume(channel, retry_after)
    except Exception:  # pragma: no cover - 纯展示信息
        pass


class TransformUpstreamError(Exception):
    def __init__(self, status: int, text: str, retry_after: int | None = None,
                 channel: str | None = None):
        super().__init__(f"transformed upstream {status}: {text[:200]}")
        self.status = status
        self.text = text
        # 上游给出的恢复时间(秒)。opencode 免费额度会返回, 用来告诉用户
        # "什么时候能再用", 也用来把冷却设成同一时长, 避免空转刷上游。
        self.retry_after = retry_after
        self.channel = channel
        if retry_after and channel:
            # 额度恢复是"渠道级事实": 在这里就落盘, 不依赖候选循环最后落在
            #哪一条错误上(9 个模型的候选列表里, 最后一条常常是 403 地区封锁)
            _note_resume(channel, retry_after)

    def resume_at_utc8(self, now: float | None = None) -> str | None:
        """额度恢复时刻(UTC+8, `MM-DD HH:MM`)。无 retry_after 时返回 None。"""
        if not self.retry_after:
            return None
        ts = (now if now is not None else time.time()) + self.retry_after
        return datetime.fromtimestamp(ts, UTC8).strftime("%m-%d %H:%M")


# ============================================================
# 非流式: 消费渠道流聚合成 OpenAI chat.completion
# ============================================================

async def transformed_non_stream(provider_name: str, api_key: str, model: str,
                                 req_body: dict, client: httpx.AsyncClient,
                                 timeout: float) -> dict:
    stream_gen = await transformed_stream(provider_name, api_key, model,
                                          req_body, client, timeout)
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    usage: dict = {}
    finish_reason: str | None = None
    async for chunk in stream_gen:
        for line in chunk.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if p == "[DONE]":
                continue
            try:
                obj = json.loads(p)
            except ValueError:
                continue
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                if isinstance(delta.get("content"), str):
                    text_parts.append(delta["content"])
                if isinstance(delta.get("reasoning_content"), str):
                    reasoning_parts.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    idx = int(tc.get("index") or 0)
                    slot = tool_calls.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if isinstance(fn.get("arguments"), str):
                        slot["function"]["arguments"] += fn["arguments"]
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]

    message = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    out: dict = {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or
                     ("tool_calls" if tool_calls else "stop")}],
    }
    if usage:
        out["usage"] = usage
    return out
