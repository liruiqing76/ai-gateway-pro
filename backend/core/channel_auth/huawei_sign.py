# ============================================================
# 华为云 SDK-HMAC-SHA256 请求签名（CodeArts 渠道专用）
#
# 提取自 dsh-our-free-model 渠道包 vendor/channel-pack/src/sign.ts，
# 算法与华为云官方 APIG 签名规范一致（非独创，纯标准件）：
#
#   1) payload SHA-256 hex → x-sdk-content-sha256
#   2) 规范化请求 = method\n uri\n query\n 排序后的 header 行\n \n
#      signedHeaders 列表\n payloadHash
#   3) stringToSign = "SDK-HMAC-SHA256\n" + 时间戳 + "\n" + 规范化请求的 SHA-256 hex
#   4) signature = HMAC-SHA256(sk, stringToSign) hex
#   5) Authorization: SDK-HMAC-SHA256 Access={ak},SignedHeaders=...,Signature=...
#
# 与 pack.js 的实现细节对齐点（照抄，勿"优化"）：
#   - uri 不以 / 结尾时补一个尾 /（华为云 APIG 规范如此）
#   - dateStamp 取 ISO8601 的紧凑形式（20261024T001200Z）
#   - 签名头集合：host / x-sdk-date / x-sdk-content-sha256 / x-security-token
#     (+ extraHeaders)；非 GET 加 content-type: application/json
#   - httpx 会自动发送 host 与 content-type，因此返回时把 host 剔除、
#     content-type 交给调用方控制（pack.js 同款处理）
# ============================================================

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256_hex(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def _datestamp(now: datetime | None = None) -> str:
    """ISO8601 紧凑时间戳：20261024T001200Z（pack.js: toISOString 去掉 -: 与毫秒）"""
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def build_canonical_request(
    method: str,
    uri: str,
    query: str,
    headers: dict[str, str],
    payload_hash: str,
) -> tuple[str, str]:
    """构造规范化请求与其 signedHeaders 列表（header 按名排序）。"""
    signed_headers = sorted(headers.keys())
    header_lines = "\n".join(f"{k}:{headers[k]}" for k in signed_headers)
    canonical = "\n".join([
        method.upper(),
        uri,
        query,
        header_lines,
        "",
        ";".join(signed_headers),
        payload_hash,
    ])
    return canonical, ";".join(signed_headers)


def sign_request_huawei(
    ak: str,
    sk: str,
    security_token: str,
    method: str,
    url: str,
    body: bytes = b"",
    extra_headers: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """对一次请求做华为云 SDK-HMAC-SHA256 签名。

    返回**全部要发的头**（含 Authorization / x-sdk-date / x-sdk-content-sha256 /
    x-security-token / 额外头；不含 host —— httpx 自管，pack.js 也剔除）。
    非 GET 时含 content-type: application/json（签名进集合，pack.js 同款）。
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    uri = parts.path or "/"
    if not uri.endswith("/"):
        uri += "/"
    query = parts.query or ""

    payload_hash = _sha256_hex(body)
    datestamp = _datestamp(now)

    headers: dict[str, str] = {
        "host": parts.netloc,
        "x-sdk-date": datestamp,
        "x-sdk-content-sha256": payload_hash,
    }
    if security_token:
        headers["x-security-token"] = security_token
    if extra_headers:
        headers.update(extra_headers)
    if method.upper() != "GET":
        headers["content-type"] = "application/json"

    canonical, signed_headers = build_canonical_request(
        method, uri, query, headers, payload_hash)
    canonical_hash = _sha256_hex(canonical.encode("utf-8"))
    string_to_sign = f"SDK-HMAC-SHA256\n{datestamp}\n{canonical_hash}"
    signature = _hmac_sha256_hex(sk.encode("utf-8"), string_to_sign.encode("utf-8"))

    # 输出头集合：签名头 + Authorization（host 不发, httpx 自管）
    out = {k: v for k, v in headers.items() if k != "host"}
    out["Authorization"] = (
        f"SDK-HMAC-SHA256 Access={ak},"
        f"SignedHeaders={signed_headers},"
        f"Signature={signature}"
    )
    return out
