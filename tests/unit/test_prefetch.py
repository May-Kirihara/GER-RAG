"""Unit tests for PrefetchCache and PrefetchPool (F6)."""
from __future__ import annotations

import asyncio
import time

import pytest

from gaottt.core.prefetch import PrefetchCache, PrefetchPool


# --- PrefetchCache ---

def test_get_returns_none_on_miss():
    cache = PrefetchCache()
    assert cache.get("foo", 5) is None
    stats = cache.stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 1


def test_put_then_get_hits():
    cache = PrefetchCache()
    cache.put("foo", 5, ["a", "b"])
    assert cache.get("foo", 5) == ["a", "b"]
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 0


def test_different_top_k_is_separate_entry():
    cache = PrefetchCache()
    cache.put("foo", 5, ["five"])
    cache.put("foo", 10, ["ten"])
    assert cache.get("foo", 5) == ["five"]
    assert cache.get("foo", 10) == ["ten"]


def test_ttl_expiry_returns_miss():
    cache = PrefetchCache(ttl_seconds=0.05)
    cache.put("foo", 5, ["x"])
    time.sleep(0.1)
    assert cache.get("foo", 5) is None


def test_lru_evicts_oldest():
    cache = PrefetchCache(max_size=2)
    cache.put("a", 1, ["A"])
    cache.put("b", 1, ["B"])
    cache.put("c", 1, ["C"])
    assert cache.get("a", 1) is None  # evicted
    assert cache.get("b", 1) == ["B"]
    assert cache.get("c", 1) == ["C"]
    assert cache.stats()["evictions"] == 1


def test_get_promotes_to_most_recent():
    cache = PrefetchCache(max_size=2)
    cache.put("a", 1, ["A"])
    cache.put("b", 1, ["B"])
    cache.get("a", 1)        # bumps "a" to most recent
    cache.put("c", 1, ["C"]) # evicts "b" (oldest)
    assert cache.get("a", 1) == ["A"]
    assert cache.get("b", 1) is None


def test_invalidate_clears_all():
    cache = PrefetchCache()
    cache.put("a", 1, ["A"])
    cache.put("b", 1, ["B"])
    n = cache.invalidate()
    assert n == 2
    assert cache.get("a", 1) is None


def test_invalid_constructor_args():
    with pytest.raises(ValueError):
        PrefetchCache(max_size=0)
    with pytest.raises(ValueError):
        PrefetchCache(ttl_seconds=0)


def test_stats_hit_rate():
    cache = PrefetchCache()
    cache.put("a", 1, ["A"])
    cache.get("a", 1)  # hit
    cache.get("a", 1)  # hit
    cache.get("b", 1)  # miss
    s = cache.stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert s["hit_rate"] == pytest.approx(2 / 3)


# --- PrefetchPool ---

async def test_pool_runs_scheduled_coroutine():
    pool = PrefetchPool(max_concurrent=2)
    out: list[int] = []

    async def work():
        out.append(1)
        return "done"

    task = pool.schedule(work)
    result = await task
    assert result == "done"
    assert out == [1]
    assert pool.stats()["completed"] == 1


async def test_pool_caps_concurrency():
    pool = PrefetchPool(max_concurrent=2)
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def slow():
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1

    tasks = [pool.schedule(slow) for _ in range(5)]
    await asyncio.gather(*tasks)
    assert peak <= 2


async def test_pool_drain_waits_for_completion():
    pool = PrefetchPool(max_concurrent=2)
    completed = 0

    async def work():
        nonlocal completed
        await asyncio.sleep(0.02)
        completed += 1

    for _ in range(3):
        pool.schedule(work)
    await pool.drain()
    assert completed == 3


async def test_pool_failed_increments_counter():
    pool = PrefetchPool(max_concurrent=1)

    async def boom():
        raise RuntimeError("nope")

    task = pool.schedule(boom)
    with pytest.raises(RuntimeError):
        await task
    assert pool.stats()["failed"] == 1


def test_pool_invalid_concurrent():
    with pytest.raises(ValueError):
        PrefetchPool(max_concurrent=0)
