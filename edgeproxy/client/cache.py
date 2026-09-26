"""Client-side cache: LRU bounded by bytes, with HTTP validators and prefetch accounting.

Tracks whether each pushed entry was ever used so the experiments can report hit rate and
wasted bytes (pushed but evicted, expired or never read).

Prefetched pages the reader never opens expire after `push_ttl_s`: they were sent for one
predicted dropout and are dead weight once it has passed. Pages the reader has opened, and pages
it fetched itself, only leave when the byte budget needs the room.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Entry:
    url: str
    body: bytes
    headers: dict = field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None
    pushed: bool = False  # arrived via prefetch rather than a user request
    prob: float | None = None  # predictor probability at push time
    hits: int = 0
    stored_at: float = 0.0  # cache clock, set by Cache.put

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    push_hits: int = 0  # first use of a pushed entry
    pushed_bytes: int = 0
    wasted_bytes: int = 0  # pushed entries evicted, expired or still unused at finalize
    expired_bytes: int = 0  # the part of wasted_bytes that expired unread


class Cache:
    def __init__(
        self,
        capacity_bytes: int,
        push_ttl_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = capacity_bytes
        self.push_ttl_s = push_ttl_s  # 0 = unread prefetched pages never expire
        self._clock = clock
        self._entries: OrderedDict[str, Entry] = OrderedDict()
        self.used = 0
        self.stats = CacheStats()

    def __contains__(self, url: str) -> bool:
        return url in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def peek(self, url: str) -> Entry | None:
        self.expire()
        return self._entries.get(url)

    def get(self, url: str) -> Entry | None:
        self.expire()
        entry = self._entries.get(url)
        if entry is None:
            self.stats.misses += 1
            return None
        self._entries.move_to_end(url)
        if entry.pushed and entry.hits == 0:
            self.stats.push_hits += 1
        entry.hits += 1
        self.stats.hits += 1
        return entry

    def put(self, entry: Entry) -> None:
        self.expire()
        if entry.size > self.capacity:
            return
        entry.stored_at = self._clock()
        old = self._entries.pop(entry.url, None)
        if old is not None:
            self.used -= old.size
            # A refetch of an unused pushed entry doesn't count as waste; the use carries over.
            if old.hits and entry.pushed:
                entry.hits = old.hits
        if entry.pushed:
            self.stats.pushed_bytes += entry.size
        self._entries[entry.url] = entry
        self.used += entry.size
        while self.used > self.capacity:
            _, evicted = self._entries.popitem(last=False)
            self.used -= evicted.size
            self._account_waste(evicted)

    def expire(self) -> None:
        """Drop prefetched pages that have gone unread for longer than push_ttl_s."""
        if self.push_ttl_s <= 0:
            return
        cutoff = self._clock() - self.push_ttl_s
        stale = [
            e for e in self._entries.values() if e.pushed and e.hits == 0 and e.stored_at < cutoff
        ]
        for entry in stale:
            del self._entries[entry.url]
            self.used -= entry.size
            self.stats.expired_bytes += entry.size
            self._account_waste(entry)

    def touch(self, url: str) -> Entry | None:
        """Origin said 304 Not Modified: keep the copy, count as a hit."""
        return self.get(url)

    def finalize(self) -> CacheStats:
        """Count still-unused pushed entries as wasted. Call once at the end of a run."""
        for entry in self._entries.values():
            self._account_waste(entry)
        return self.stats

    def _account_waste(self, entry: Entry) -> None:
        if entry.pushed and entry.hits == 0:
            self.stats.wasted_bytes += entry.size
