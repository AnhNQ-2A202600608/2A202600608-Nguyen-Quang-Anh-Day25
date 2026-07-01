import pytest
import redis as redis_lib
from reliability_lab.cache import SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider
from reliability_lab.config import LabConfig


def _redis_available() -> bool:
    try:
        r = redis_lib.Redis.from_url("redis://localhost:6379/0")
        r.ping()
        r.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _redis_available(), reason="Redis not running")
def test_redis_circuit_state_sharing() -> None:
    """Verifies that state coordinates between multiple circuit breaker instances using Redis."""
    r = redis_lib.Redis.from_url("redis://localhost:6379/0")
    r.flushdb()

    cb1 = CircuitBreaker("shared-provider", failure_threshold=2, reset_timeout_seconds=5, redis_client=r)
    cb2 = CircuitBreaker("shared-provider", failure_threshold=2, reset_timeout_seconds=5, redis_client=r)

    # Initially both are CLOSED
    assert cb1.allow_request()
    assert cb2.allow_request()

    # Record failures on cb1 to trigger OPEN state
    cb1.record_failure()
    cb1.record_failure()

    assert cb1.state == CircuitState.OPEN
    assert not cb1.allow_request()

    # cb2 should now also be OPEN and allow_request() should deny requests
    assert not cb2.allow_request()
    assert cb2.state == CircuitState.OPEN

    r.close()


def test_redis_graceful_degradation() -> None:
    """Verifies that SharedRedisCache falls back to memory cache if Redis is down."""
    # Point to a completely invalid port to simulate Redis down
    cache = SharedRedisCache(
        redis_url="redis://localhost:9999/0",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:failed:",
    )

    # ping() should return False but not raise
    assert not cache.ping()

    # set() should fall back to memory
    cache.set("graceful degradation query", "fallback response value")

    # get() should resolve from memory
    cached, score = cache.get("graceful degradation query")
    assert cached == "fallback response value"
    assert score == 1.0


def test_cost_aware_routing() -> None:
    """Verifies that gateway routes requests according to remaining budget."""
    primary = FakeLLMProvider("expensive", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=100.0)
    backup = FakeLLMProvider("cheap", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=1.0)

    breakers = {
        "expensive": CircuitBreaker("expensive", failure_threshold=3, reset_timeout_seconds=1),
        "cheap": CircuitBreaker("cheap", failure_threshold=3, reset_timeout_seconds=1),
    }

    # Set budget to 0.05. One call to 'expensive' (which uses tokens and scales by cost_per_1k_tokens)
    # will exceed the 80% threshold (0.04) and 100% threshold (0.05).
    gateway = ReliabilityGateway(
        providers=[primary, backup],
        breakers=breakers,
        cost_budget=0.05,
    )

    # First call: budget is at 0, primary is used
    res1 = gateway.complete("hello world")
    assert res1.provider == "expensive"
    assert gateway.cumulative_cost > 0.0

    # Ensure cumulative cost is updated
    assert gateway.cumulative_cost == res1.estimated_cost

    # Let's say cost is high enough to trigger 80% skip or 100% limit
    # We can also manually adjust self.cumulative_cost to verify thresholds
    gateway.cumulative_cost = 0.041  # > 80% of budget (0.04)

    # Second call: 80% budget cap is met, expensive model should be skipped, backup is used
    res2 = gateway.complete("hello world")
    assert res2.provider == "cheap"

    # Now exceed 100% budget limit
    gateway.cumulative_cost = 0.055  # > 100% of budget (0.05)

    # Third call: budget exceeded, fallback to static response
    res3 = gateway.complete("hello world")
    assert res3.route == "static_fallback"
    assert "budget exceeded" in res3.error.lower()


def test_concurrency_simulation_runner() -> None:
    """Verifies that simulation runs correctly with concurrency enabled in the config."""
    config = LabConfig(
        providers=[
            {"name": "p1", "fail_rate": 0.0, "base_latency_ms": 1, "cost_per_1k_tokens": 0.001},
            {"name": "p2", "fail_rate": 0.0, "base_latency_ms": 1, "cost_per_1k_tokens": 0.001},
        ],
        circuit_breaker={"failure_threshold": 3, "reset_timeout_seconds": 1.0, "success_threshold": 1},
        cache={"enabled": True, "backend": "memory", "ttl_seconds": 60, "similarity_threshold": 0.5},
        load_test={"requests": 10, "concurrent": True, "max_workers": 4},
        scenarios=[],
    )

    from reliability_lab.chaos import run_scenario
    metrics = run_scenario(config, ["hello", "world"], scenario=None)
    assert metrics.total_requests == 10
    assert metrics.successful_requests == 10
