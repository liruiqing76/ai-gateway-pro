"""失败结论必须来自用户点名的那个模型, 不是兜底候选。

真机案例: 点名 `opencode-glm-5.3-flash-free`(当日额度耗尽 → 429),
兜底会再试同渠道的 `beam-free`(地区封锁 → 403)。以前 `last_err` 被最后一条
错误覆盖, 用户看到"该模型在当前出口地区不可用, 请换模型" —— 而实际处置是
"等额度重置", 完全不同的动作。
"""
import pytest

from backend.api import anthropic as an
from backend.api import proxy as px


class TestPrimaryErrorPreferred:
    """两个面都必须优先回主候选原因。"""

    @pytest.mark.parametrize("module", [px, an], ids=["openai", "anthropic"])
    def test_module_has_primary_err_tracking(self, module):
        src = __import__("inspect").getsource(module)
        assert "primary_err" in src
        # 结论行必须用 primary_err 兜底, 不能直接用 last_err
        assert "final_err = primary_err or last_err" in src

    @pytest.mark.parametrize("module", [px, an], ids=["openai", "anthropic"])
    def test_primary_keys_snapshot_before_fallback(self, module):
        """primary_keys 必须在合并兜底候选之前取快照, 否则兜底也算主候选。"""
        src = __import__("inspect").getsource(module)
        i_keys = src.index("primary_keys = set(seen)")
        i_fb = src.index("for fb in fallback_candidates")
        assert i_keys < i_fb, "primary_keys 必须先于兜底合并"

    @pytest.mark.parametrize("module", [px, an], ids=["openai", "anthropic"])
    def test_primary_err_only_set_once(self, module):
        """每处赋值都必须带守卫, 保证第一条(主候选)不被后续兜底覆盖。

        守卫可能写在同一行, 也可能在上一行的 if 条件里 —— 两种都接受。
        (openai 面是 `primary_err, primary_label = err, ...` 元组赋值, 一并覆盖。)
        """
        src = __import__("inspect").getsource(module)
        lines = src.splitlines()
        checked = 0
        for i, line in enumerate(lines):
            if "primary_err = err" not in line and "primary_err, primary_label = err" not in line:
                continue
            checked += 1
            window = " ".join(lines[max(0, i - 1):i + 1])
            assert "primary_err is None" in window, f"缺少守卫: {line.strip()}"
        assert checked >= 1, "至少要有一处带守卫的赋值"


class TestFinalErrSelection:
    def test_primary_wins_over_later_error(self):
        primary_err = "该渠道今日免费额度已用尽, 请换模型或明天再试"
        last_err = "该模型在当前出口地区不可用, 请换模型"
        assert (primary_err or last_err) == primary_err

    def test_falls_back_when_primary_missing(self):
        primary_err = None
        last_err = "upstream timeout"
        assert (primary_err or last_err) == last_err
