"""qoder 签名器行协议: 单行响应可以远超 asyncio 默认 64KB limit。

回归背景: 长上下文请求把签名结果的单行 JSON 撑到 17KB+, 旧代码用默认
stream limit 创建签名器子进程, readline 抛 LimitOverrunError/ValueError,
表现为"所有候选模型均失败: Separator is found, but chunk is longer than limit"。
"""

import asyncio
import sys

from backend.core.channel_auth import qoder

# 极简假签名器: 读一行请求, 回一行超长 JSON
_FAKE_SIGNER = """
import json, sys
line = sys.stdin.readline()
req = json.loads(line)
size = int(req.get("size") or 200000)
sys.stdout.write(json.dumps({"ok": True, "result": {"blob": "x" * size}}) + "\\n")
sys.stdout.flush()
"""


def test_signer_reads_line_longer_than_default_limit(tmp_path, monkeypatch):
    script = tmp_path / "fake_signer.py"
    script.write_text(_FAKE_SIGNER, encoding="utf-8")
    monkeypatch.setattr(qoder, "_SIGNER_SCRIPT", script)
    monkeypatch.setattr(qoder, "_find_node", lambda: sys.executable)

    seen = {}
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        seen.update(kwargs)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    async def main():
        proc = qoder._SignerProcess()
        try:
            return await proc.request({"size": 200_000})
        finally:
            await proc._kill()

    result = asyncio.run(main())
    # 200KB 单行远超 asyncio 默认 64KB: 不加 limit 这里会抛 LimitOverrunError
    assert len(result["blob"]) == 200_000
    assert seen.get("limit", 0) >= 1024 * 1024
