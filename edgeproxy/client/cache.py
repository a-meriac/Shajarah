"""Client-side cache: LRU bounded by bytes, with HTTP validators and prefetch accounting.

Tracks whether each pushed entry was ever used so the experiments can report hit rate and
wasted bytes (pushed but evicted or never read).
"""

from __future__ import annotations

from collections import OrderedDict
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

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    push_hits: int = 0  # first use of a pushed entry
    pushed_bytes: int = 0
    wasted_bytes: int = 0  # pushed entries evicted (or still unused at finalize) without a hit


class Cache:
    def __init__(self, capacity_bytes: int) -> None:
        self.capacity = capacity_bytes
        self._entries: OrderedDict[str, Entry] = OrderedDict()
        self.used = 0
        self.stats = CacheStats()

    def __contains__(self, url: str) -> bool:
        return url in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def peek(self, url: str) -> Entry | None:
        return self._entries.get(url)

    def get(self, url: str) -> Entry | None:
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
        if entry.size > self.capacity:
            return
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
