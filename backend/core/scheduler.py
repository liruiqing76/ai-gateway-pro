# ============================================================
# 智能网关 — 质量评分调度
# 记录每个 (provider, model) 的健康数据,按质量分排序
# ============================================================

import time
from collections import deque, defaultdict
from datetime import datetime

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
        # 今日成功调用数 (key = provider||model): 加权分发按剩余免费额度平滑用。
        # 单进程内存计数, 首次访问时从 usage_logs 聚合当日已用, 每日凌晨清零。
        self.today_usage: dict[str, int] = {}
        self._today_seeded = False

    def seed_today_from_db(self) -> None:
        """首次使用前把今日已记录的成功调用数从 usage_logs 聚合进内存。"""
        try:
            from backend.models import get_session, UsageLog
            from sqlalchemy import func
            session = get_session()
            try:
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                rows = (session.query(UsageLog.provider_name, UsageLog.model_name, func.count())
                        .filter(UsageLog.success.is_(True), UsageLog.created_at >= today)
                        .group_by(UsageLog.provider_name, UsageLog.model_name)
                        .all())
                self.today_usage = {f"{p}||{m}": int(c) for p, m, c in rows}
            finally:
                session.close()
        except Exception:
            pass  # 聚合失败不影响路由, 平滑从 0 开始
        self._today_seeded = True

    def get_today_usage(self, key: str) -> int:
        if not self._today_seeded:
            self.seed_today_from_db()
        return self.today_usage.get(key, 0)

    def bump_today_usage(self, key: str) -> None:
        if not self._today_seeded:
            self.seed_today_from_db()
        self.today_usage[key] = self.today_usage.get(key, 0) + 1

    def reset_today_usage(self) -> None:
        """每日额度重置时清零(与 quality_tracker.reset_all 同周期)"""
        self.today_usage.clear()
        self._today_seeded = True

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
            self.bump_today_usage(key)
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
        # 延迟惩罚: 平均延迟过高(>10s)的慢渠道扣分, 避免"慢但能成功"的
        # 上游永远排在候选最前把整个路由组拖垮(如 NVIDIA 降速期 20-200s)
        if q.latencies:
            avg_lat = sum(q.latencies) / len(q.latencies)
            if avg_lat > 10000:
                raw -= 0.3
        # 置信度: 请求越多越可信,权重向上修正
        confidence = min(total / self.window, 1.0)
        return raw * confidence

    def avg_latency_for_provider(self, provider_name: str) -> float | None:
        """渠道级延迟均值: 无该模型记录时用同渠道已知模型延迟估计, 让慢渠道的
        新模型/无记录模型也能正确排后(按模型排序, 而不是按配置档位排序)。"""
        lats = []
        prefix = f"{provider_name}||"
        for k, q in self._data.items():
            if k.startswith(prefix) and q.latencies:
                lats.extend(q.latencies)
        if not lats:
            return None
        return sum(lats) / len(lats)

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

    def reset_all(self) -> None:
        """清空所有记录(测试用)"""
        self._data.clear()
        self.reset_today_usage()


# ============================================================
# 全局质量追踪器
# ============================================================
quality_tracker = QualityTracker()