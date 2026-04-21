"""F6 — Background recall prefetch (the astrocyte's true workload).

Two pieces:

  * ``PrefetchCache`` — bounded TTL+LRU cache of ``query → results`` pairs.
    Lets a previously-prefetched recall return instantly on the next call.

  * ``PrefetchPool`` — bounded async task pool that throttles background
    prefetches so they never starve foreground recall (the system's hot path).

The engine schedules a prefetch with ``engine.prefetch(query, top_k)``;
the next time ``engine.query(query, top_k, use_cache=True)`` runs, the result
is served from cache. Cache hits are best-effort: if the prefetch was stale
(e.g. a remember happened in between) the cached result is returned anyway.
This matches the astrocyte metaphor — pre-loaded potential wells are good
guesses, not authoritative facts.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Awaitable, Callable, TypeVar

from gaottt.core.types import QueryResultItem

T = TypeVar("T")


class PrefetchCache:
    """LRU + TTL cache keyed by ``(query_text, top_k)``.

    Cache hits return the original ``list[QueryResultItem]`` reference; callers
    must treat the list as immutable (do not mutate the returned objects).
    """

    def __init__(self, max_size: int = 64, ttl_seconds: float = 90.0):
        if max_size < 1:
            raise ValueError("max_size must be ≥ 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._entries: OrderedDict[
            tuple[str, int], tuple[float, list[QueryResultItem]]
        ] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, query: str, top_k: int) -> list[QueryResultItem] | None:
        key = (query, top_k)
        entry = self._entries.get(key)
        if entry is None:
            self._misses += 1
            return None
        cached_at, results = entry
        if time.time() - cached_at > self._ttl:
            del self._entries[key]
            self._misses += 1
            return None
        self._entries.move_to_end(key)
        self._hits += 1
        return results

    def put(self, query: str, top_k: int, results: list[QueryResultItem]) -> None:
        key = (query, top_k)
        self._entries[key] = (time.time(), results)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)
            self._evictions += 1

    def invalidate(self) -> int:
        n = len(self._entries)
        self._entries.clear()
        return n

    def stats(self) -> dict:
        now = time.time()
        active = sum(
            1 for cached_at, _ in self._entries.values()
            if now - cached_at <= self._ttl
        )
        total = self._hits + self._misses
        return {
            "size": len(self._entries),
            "active": active,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate": (self._hits / total) if total else 0.0,
            "max_size": self._max_size,
            "ttl_seconds": self._ttl,
        }


class PrefetchPool:
    """Bounded async task pool for background prefetches.

    Concurrency is capped via an ``asyncio.Semaphore`` so prefetches can never
    starve foreground recall, regardless of how many are scheduled.
    """

    def __init__(self, max_concurrent: int = 4):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be ≥ 1")
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task] = set()
        self._scheduled = 0
        self._completed = 0
        self._failed = 0
        self._max_concurrent = max_concurrent

    def schedule(
        self, coro_factory: Callable[[], Awaitable[T]],
    ) -> asyncio.Task[T]:
        """Schedule a background coroutine. Returns the task handle.

        ``coro_factory`` is a zero-arg callable that returns the awaitable.
        The factory pattern lets the semaphore acquire BEFORE the work starts,
        so work doesn't race ahead of the concurrency cap.
        """
        task = asyncio.create_task(self._wrap(coro_factory))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._scheduled += 1
        return task

    async def _wrap(self, coro_factory: Callable[[], Awaitable[T]]) -> T | None:
        async with self._sem:
            try:
                result = await coro_factory()
                self._completed += 1
                return result
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover — instrumentation path
                self._failed += 1
                raise

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for all in-flight prefetches to finish (best-effort)."""
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            for task in self._tasks:
                task.cancel()

    def stats(self) -> dict:
        return {
            "scheduled": self._scheduled,
            "completed": self._completed,
            "failed": self._failed,
            "in_flight": len(self._tasks),
            "max_concurrent": self._max_concurrent,
        }
