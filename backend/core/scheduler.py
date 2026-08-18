# ============================================================
# AI 模型网关 Pro — 质量评分调度
# 记录每个 (provider, model) 的健康数据,按质量分排序
# ============================================================

import time
from collections import deque, defaultdict

from backend.config import settings


class ModelQuality:
    """单个模型的质量状态"""

    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.error = 0
        self.latencies: deque[float] = deque(maxlen=settings.QUALITY_WINDOW)


class QualityTracker:
    """
    质量评分调度器。

    评分规则(从原版重构):
    - ok 记 1 分, fail/error 记 0 分
    - 用滑动窗口最近 20 次请求
    - 质量分 = (ok - fail*0.5 - error*1.0) / total,再减去延迟惩罚
    """

    def __init__(self, window: int = None):
        self.window = window or settings.QUALITY_WINDOW
        self._data: dict[str, ModelQuality] = defaultdict(ModelQuality)

    def _get(self, key: str) -> ModelQuality:
        q = self._data.get(key)
        if q is None:
            q = ModelQuality()
            self._data[key] = q
        return q

    def record(self, key: str, status: str, latency_ms: float = None) -> None:
        """记录一次请求结果"""
        q = self._get(key)
        if status == "ok":
            q.ok += 1
        elif status == "fail":
            q.fail += 1
        else:
            q.error += 1
        if latency_ms is not None:
            q.latencies.append(latency_ms)

    def get_score(self, key: str) -> float:
        """
        质量分: (ok - fail - error*2) / total,范围 [-2, 1]
        不过请求数少的模型会有误差,用 "置信度" 平滑
        """
        q = self._get(key)
        total = q.ok + q.fail + q.error
        if total == 0:
            return 0.0
        raw = (q.ok - q.fail * 0.5 - q.error * 1.0) / total
        # 置信度: 请求越多越可信,权重向上修正
        confidence = min(total / self.window, 1.0)
        return raw * confidence

    def get_avg_latency(self, key: str) -> float | None:
        q = self._get(key)
        if not q.latencies:
            return None
        return sum(q.latencies) / len(q.latencies)

    def get_stats(self, key: str) -> dict:
        """返回完整统计,用于监控面板"""
        q = self._get(key)
        return {
            "ok": q.ok,
            "fail": q.fail,
            "error": q.error,
            "score": self.get_score(key),
            "avg_latency_ms": self.get_avg_latency(key),
            "total": q.ok + q.fail + q.error,
        }

    def reset(self, key: str) -> None:
        if key in self._data:
            del self._data[key]


# ============================================================
# 全局质量追踪器
# ============================================================
quality_tracker = QualityTracker()