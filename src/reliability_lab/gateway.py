from __future__ import annotations

from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError


import threading

@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        cost_budget: float | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cost_budget = cost_budget
        self.cumulative_cost = 0.0
        self._cost_lock = threading.Lock()

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        Implement the full request routing pipeline with cost budget tracking.
        """
        # Cost budget checking (Bonus 4)
        with self._cost_lock:
            current_cost = self.cumulative_cost
            budget = self.cost_budget

        if budget is not None and current_cost >= budget:
            # 100% of budget exceeded: cache only or static fallback
            if self.cache is not None:
                cached_text, score = self.cache.get(prompt)
                if cached_text is not None:
                    return GatewayResponse(
                        text=cached_text,
                        route=f"cache_hit:{score:.2f}",
                        provider=None,
                        cache_hit=True,
                        latency_ms=0.0,
                        estimated_cost=0.0,
                    )
            return GatewayResponse(
                text="The service is temporarily degraded. Please try again soon.",
                route="static_fallback",
                provider=None,
                cache_hit=False,
                latency_ms=0.0,
                estimated_cost=0.0,
                error="Cost budget exceeded",
            )

        # 80% budget check: check if we should skip expensive models
        skip_expensive = False
        if budget is not None and current_cost >= 0.8 * budget:
            cheapest_cost = min(p.cost_per_1k_tokens for p in self.providers) if self.providers else 0.0
            skip_expensive = True

        # 1. CACHE CHECK
        if self.cache is not None:
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0,
                )

        # 2. PROVIDER FALLBACK CHAIN
        last_error = None
        for i, provider in enumerate(self.providers):
            # Skip expensive providers if cumulative cost is over 80% and a cheaper provider exists
            if skip_expensive and provider.cost_per_1k_tokens > cheapest_cost:
                continue

            breaker = self.breakers[provider.name]
            try:
                response = breaker.call(provider.complete, prompt)
                
                with self._cost_lock:
                    self.cumulative_cost += response.estimated_cost

                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})

                route = "primary" if i == 0 else "fallback"
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=response.latency_ms,
                    estimated_cost=response.estimated_cost,
                )
            except (ProviderError, CircuitOpenError) as e:
                last_error = str(e)
            except Exception as e:
                last_error = str(e)

        # 3. STATIC FALLBACK
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=last_error,
        )
