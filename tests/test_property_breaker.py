from hypothesis import given, strategies as st
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState
import time


@given(st.lists(st.booleans(), min_size=1, max_size=100))
def test_circuit_breaker_invariants(operations: list[bool]) -> None:
    """Property-based tests verifying core circuit breaker state machine invariants."""
    cb = CircuitBreaker(
        name="property-test",
        failure_threshold=3,
        reset_timeout_seconds=0.01,
        success_threshold=2,
    )

    for op in operations:
        assert cb.failure_count >= 0
        assert cb.success_count >= 0

        state_before = cb.state

        if op:
            cb.record_success()
            assert cb.failure_count == 0
            if state_before == CircuitState.HALF_OPEN:
                assert cb.success_count >= 1
        else:
            cb.record_failure()
            assert cb.success_count == 0
            if state_before == CircuitState.HALF_OPEN:
                assert cb.state == CircuitState.OPEN

        assert cb.failure_count >= 0
        assert cb.success_count >= 0

        if cb.state == CircuitState.OPEN:
            assert cb.opened_at is not None
            # Since hypothesis calls can run quickly, check allow_request constraints
            if time.monotonic() - cb.opened_at < cb.reset_timeout_seconds:
                assert not cb.allow_request()
