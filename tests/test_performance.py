# ============================================================
# 性能基准测试
# 覆盖关键路径: 候选选择 / 熔断器 / 质量调度 / 转发封装 / SSE 打包
# 阈值取宽松基线, 防止 CI 抖动误报; 可自行收紧用于回归对比
# ============================================================
import sys, json, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from backend.core.scheduler import quality_tracker
from backend.core.circuit import circuit_breaker


class FakeProvider:
    def __init__(self, i):
        self.id = i
        self.name = f"provider-{i}"
        self.models = [f"model-{i}-{j}" for j in range(10)]
        self.disabled_models = []


def make_providers(n: int, models_per=10):
    return [FakeProvider(i) for i in range(n)]


@pytest.fixture
def clean_state():
    from backend.core.scheduler import quality_tracker
    from backend.core.circuit import circuit_breaker
    quality_tracker.reset_all()
    circuit_breaker._states.clear()
    yield
    quality_tracker.reset_all()
    circuit_breaker._states.clear()


class TestPickModelsPerformance:
    """候选模型选择性能"""

    def test_small_route_group(self, clean_state):
        """小规模路由组(5 渠道 × 6 模型)的选择延迟"""
        from backend.core.proxy import pick_available_models

        providers = []
        for i in range(5):
            p = FakeProvider(i)
            p.models = [f"m{i}-{j}" for j in range(6)]
            providers.append(p)
        routers = {"grp": [f"m{i}-{j}" for i in range(5) for j in range(6)]}

        # 预热
        pick_available_models(providers, "grp", routers, force=True)

        start = time.perf_counter()
        for _ in range(200):
            pick_available_models(providers, "grp", routers, force=True)
        elapsed_ms = (time.perf_counter() - start) * 1000 / 200

        # 单次选择应远低于 1ms 量级; 宽松阈值 500ms
        assert elapsed_ms < 500, f"pick_available_models avg {elapsed_ms:.2f}ms too slow"

    def test_large_pool_50_providers(self, clean_state):
        """50 渠道 × 10 模型的大池选择延迟"""
        from backend.core.proxy import pick_available_models

        providers = make_providers(50)
        routers = {"big": [f"model-{i}-{j}" for i in range(50) for j in range(10)]}

        start = time.perf_counter()
        result = pick_available_models(providers, "big", routers, force=True)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 2000, f"large pool select took {elapsed_ms:.2f}ms"
        assert len(result) == 500  # 全部模型

    def test_strategy_sort_cost(self, clean_state):
        """三种策略排序的开销"""
        from backend.core.proxy import pick_available_models

        providers = make_providers(20)
        routers = {"g": [f"model-{i}-{j}" for i in range(20) for j in range(10)]}

        for strategy in ("quality", "fastest", "stable"):
            start = time.perf_counter()
            for _ in range(50):
                pick_available_models(providers, "g", routers, force=True, strategy=strategy)
            avg_ms = (time.perf_counter() - start) * 1000 / 50
            assert avg_ms < 1000, f"strategy {strategy} avg {avg_ms:.2f}ms too slow"


class TestCircuitBreakerPerformance:
    """熔断器状态机性能"""

    def test_10k_operations(self, clean_state):
        cb = circuit_breaker
        start = time.perf_counter()
        for i in range(10000):
            key = f"p{i % 50}||m{i % 30}"
            cb.record_fail(key)
            if i % 10 == 0:
                cb.record_success(key)
            cb.is_open(key)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 2000, f"10k circuit ops took {elapsed_ms:.2f}ms"

    def test_rate_limit_backoff_bounded(self, clean_state):
        """429 指数退避封顶在 300s(含时钟容差)"""
        cb = circuit_breaker
        cb.record_rate_limit("p||m")  # 第 1 次
        st = cb._states["p||m"]
        first_backoff = st.open_until - time.time()
        assert first_backoff <= 60, f"first backoff too big: {first_backoff:.1f}s"

        # 连续 20 次后降温应封顶在 300s 附近
        for i in range(20):
            cb.record_rate_limit("p||m")
            st = cb._states["p||m"]
            remaining = st.open_until - time.time()
            assert remaining <= 310, f"backoff exceeded cap: {remaining:.1f}s"


class TestSchedulerPerformance:
    """质量评分调度器性能"""

    def test_10k_scores(self, clean_state):
        start = time.perf_counter()
        for i in range(10000):
            key = f"p{i % 20}||m{i % 40}"
            if i % 3 == 0:
                quality_tracker.record(key, "ok", 120.0)
            elif i % 3 == 1:
                quality_tracker.record(key, "fail", 120.0)
            else:
                quality_tracker.record(key, "error")
            quality_tracker.get_score(key)
            quality_tracker.get_avg_latency(key)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 3000, f"10k scheduler ops took {elapsed_ms:.2f}ms"

    def test_window_bounded(self, clean_state):
        """滑动窗口不应无限增长"""
        key = "p||m"
        for i in range(500):
            quality_tracker.record(key, "ok", 100.0)
        from backend.core.scheduler import quality_tracker as qt
        # 内部窗口(通过 get_score 间接验证)后 200 条仍稳定
        for i in range(200):
            quality_tracker.record(key, "fail", 100.0)
        score = quality_tracker.get_score(key)
        assert -2 <= score <= 2  # 有界


class TestForwardHelpersPerformance:
    """转发相关的纯函数性能"""

    def test_sse_chunk_encoding(self):
        """SSE chunk JSON 编码速度"""
        obj = {
            "id": "chatcmpl-xxx",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "你好, 这是一个测试内容"}, "finish_reason": None}],
        }
        start = time.perf_counter()
        for _ in range(10000):
            json.dumps(obj, ensure_ascii=False)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 3000, f"10k SSE encodings took {elapsed_ms:.2f}ms"

    def test_pipeline_parse(self):
        """编排语法解析性能"""
        from backend.core.orchestrate import parse_pipeline
        models = [
            "relay:quick/high_quality/vision",
            "review:high_quality/quick",
            "NVIDIA-deepseek-v3",
            "quick",
            "",
            "gpt-4",
        ]
        start = time.perf_counter()
        for _ in range(5000):
            for m in models:
                parse_pipeline(m)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 2000, f"5k parse loops took {elapsed_ms:.2f}ms"

    def test_router_group_classification(self):
        """模型分类(正则匹配)性能"""
        from backend.core.router_group import classify_model
        models = [
            "z-ai/glm-5.2", "Qwen/Qwen3-VL-235B-A22B-Instruct", "meta/llama-3.2-3b-instruct",
            "deepseek-ai/DeepSeek-V4-Flash-0731", "qwen-vl-plus", "nvidia/nemotron-3-ultra-550b-a55b",
        ]
        start = time.perf_counter()
        for _ in range(10000):
            for m in models:
                classify_model(m)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 5000, f"10k classifications took {elapsed_ms:.2f}ms"


class TestAPILatencyBaseline:
    """API 基线延迟(mock 转发, 不含真实网络)"""

    def test_health_endpoint(self, tmp_path, monkeypatch):
        """健康检查端点延迟"""
        from fastapi.testclient import TestClient
        from backend.main import app
        monkeypatch.setenv("GW_DB_PATH", str(tmp_path / "t.db"))

        with TestClient(app) as client:
            # 预热
            client.get("/api/health")
            start = time.perf_counter()
            for _ in range(50):
                client.get("/api/health")
            avg_ms = (time.perf_counter() - start) * 1000 / 50
            assert avg_ms < 100, f"/api/health avg {avg_ms:.2f}ms too slow"

    def test_proxy_reject_unauthenticated(self, tmp_path, monkeypatch):
        """未认证请求被拒绝的延迟"""
        from fastapi.testclient import TestClient
        from backend.main import app
        monkeypatch.setenv("GW_DB_PATH", str(tmp_path / "t.db"))

        with TestClient(app) as client:
            start = time.perf_counter()
            for _ in range(20):
                resp = client.post(
                    "/v1/chat/completions",
                    json={"model": "quick", "messages": [{"role": "user", "content": "hi"}]},
                )
            avg_ms = (time.perf_counter() - start) * 1000 / 20
            assert resp.status_code == 401
            assert avg_ms < 200, f"auth reject avg {avg_ms:.2f}ms too slow"