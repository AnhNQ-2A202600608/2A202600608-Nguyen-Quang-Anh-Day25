from __future__ import annotations

from collections import Counter
import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache skeleton.

    Add a better semantic similarity function and false-hit guardrails.
    Use the module-level _is_uncacheable() and _looks_like_false_hit() helpers in your
    get() and set() methods.  For production, replace with SharedRedisCache.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity.

        Implement cache lookup with guardrails:
        1. Return (None, 0.0) if _is_uncacheable(query) — privacy check
        2. Evict expired entries (compare time.time() - created_at vs ttl_seconds)
        3. Find best matching entry using self.similarity(query, entry.key)
        4. If best_score >= similarity_threshold:
           a. Check _looks_like_false_hit(query, best_key) — if true, log to
              self.false_hit_log and return (None, best_score)
           b. Otherwise return (best_value, best_score)
        5. Return (None, best_score) if no match above threshold
        """
        with self._lock:
            if _is_uncacheable(query):
                return None, 0.0

            now = time.time()
            self._entries = [
                entry for entry in self._entries
                if now - entry.created_at <= self.ttl_seconds
            ]

            if not self._entries:
                return None, 0.0

            best_entry = None
            best_score = -1.0

            for entry in self._entries:
                score = self.similarity(query, entry.key)
                if score > best_score:
                    best_score = score
                    best_entry = entry

            if best_entry is None or best_score < self.similarity_threshold:
                return None, max(0.0, best_score)

            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append({
                    "query": query,
                    "cached_key": best_entry.key,
                    "reason": "date_or_number_mismatch",
                    "ts": now
                })
                return None, best_score

            return best_entry.value, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in cache.

        Implement with privacy guardrail:
        1. Return immediately if _is_uncacheable(query)
        2. Append a CacheEntry to self._entries
        """
        with self._lock:
            if _is_uncacheable(query):
                return

            entry = CacheEntry(
                key=query,
                value=value,
                created_at=time.time(),
                metadata=metadata or {}
            )
            self._entries.append(entry)

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute semantic similarity between two strings.

        Implement cosine similarity over character n-grams + word tokens.
        The naive token-overlap (Jaccard) approach loses too much information.
        """
        if a == b:
            return 1.0

        words_a = re.findall(r"\w+", a.lower())
        words_b = re.findall(r"\w+", b.lower())

        def get_char_ngrams(text: str, n: int = 3) -> list[str]:
            text_lower = text.lower()
            return [text_lower[i : i + n] for i in range(len(text_lower) - n + 1)]

        ngrams_a = get_char_ngrams(a)
        ngrams_b = get_char_ngrams(b)

        tokens_a = words_a + ngrams_a
        tokens_b = words_b + ngrams_b

        if not tokens_a or not tokens_b:
            return 0.0

        counter_a = Counter(tokens_a)
        counter_b = Counter(tokens_b)

        intersection = set(counter_a.keys()) & set(counter_b.keys())
        dot_product = sum(counter_a[token] * counter_b[token] for token in intersection)

        mag_a = math.sqrt(sum(val ** 2 for val in counter_a.values()))
        mag_b = math.sqrt(sum(val ** 2 for val in counter_b.values()))

        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0

        return dot_product / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    TODO(student): Implement the get() and set() methods using Redis commands
    so that cache state is shared across multiple gateway instances.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, Any]] = []
        try:
            self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        except Exception:
            self._redis = None
        self._fallback_cache = ResponseCache(ttl_seconds, similarity_threshold)
        self._lock = threading.RLock()

    def ping(self) -> bool:
        """Check Redis connectivity."""
        if self._redis is None:
            return False
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        Implement cache lookup.
        """
        with self._lock:
            if _is_uncacheable(query):
                return None, 0.0

            try:
                if self._redis is None:
                    raise RuntimeError("Redis connection not initialized")

                # Try exact match first
                exact_key = f"{self.prefix}{self._query_hash(query)}"
                cached_response = self._redis.hget(exact_key, "response")
                if cached_response is not None:
                    return _decode(cached_response), 1.0

                # Scan and find similar queries
                best_query = None
                best_response = None
                best_score = -1.0

                for key in self._redis.scan_iter(f"{self.prefix}*"):
                    data = self._redis.hgetall(key)
                    if not data:
                        continue

                    decoded_data = {
                        _decode(k): _decode(v)
                        for k, v in data.items()
                    }
                    cached_q = decoded_data.get("query")
                    cached_r = decoded_data.get("response")

                    if cached_q is not None and cached_r is not None:
                        score = ResponseCache.similarity(query, cached_q)
                        if score > best_score:
                            best_score = score
                            best_query = cached_q
                            best_response = cached_r

                if best_query is None or best_response is None or best_score < self.similarity_threshold:
                    return None, max(0.0, best_score)

                if _looks_like_false_hit(query, best_query):
                    self.false_hit_log.append({
                        "query": query,
                        "cached_key": best_query,
                        "reason": "date_or_number_mismatch",
                        "ts": time.time()
                    })
                    return None, best_score

                return best_response, best_score
            except Exception:
                # Fallback dynamically to local cache
                res = self._fallback_cache.get(query)
                if self._fallback_cache.false_hit_log:
                    self.false_hit_log.extend(self._fallback_cache.false_hit_log)
                    self._fallback_cache.false_hit_log.clear()
                return res

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL.

        Implement cache storage.
        """
        with self._lock:
            if _is_uncacheable(query):
                return

            try:
                if self._redis is None:
                    raise RuntimeError("Redis connection not initialized")
                key = f"{self.prefix}{self._query_hash(query)}"
                mapping = {"query": query, "response": value}
                if metadata:
                    for k, v in metadata.items():
                        mapping[f"meta:{k}"] = str(v)
                self._redis.hset(key, mapping=mapping)
                self._redis.expire(key, self.ttl_seconds)
            except Exception:
                self._fallback_cache.set(query, value, metadata)

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        try:
            if self._redis is not None:
                for key in self._redis.scan_iter(f"{self.prefix}*"):
                    self._redis.delete(key)
        except Exception:
            pass
        self._fallback_cache._entries.clear()

    def close(self) -> None:
        """Close Redis connection."""
        try:
            if self._redis is not None:
                self._redis.close()
        except Exception:
            pass

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]


def _decode(val: Any) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8")
    return str(val)
