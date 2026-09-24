from edgeproxy.client.cache import Cache, Entry


def test_lru_eviction_by_bytes():
    c = Cache(capacity_bytes=10)
    c.put(Entry("a", b"12345"))
    c.put(Entry("b", b"12345"))
    c.get("a")  # a becomes most recent
    c.put(Entry("c", b"12345"))
    assert "a" in c and "c" in c and "b" not in c
    assert c.used == 10


def test_push_accounting():
    c = Cache(capacity_bytes=100)
    c.put(Entry("used", b"x" * 10, pushed=True, prob=0.5))
    c.put(Entry("unused", b"y" * 20, pushed=True, prob=0.1))
    c.get("used")
    c.get("used")
    assert c.get("missing") is None
    stats = c.finalize()
    assert stats.push_hits == 1
    assert stats.pushed_bytes == 30
    assert stats.wasted_bytes == 20
    assert (stats.hits, stats.misses) == (2, 1)


def test_oversized_entry_ignored():
    c = Cache(capacity_bytes=4)
    c.put(Entry("big", b"12345"))
    assert "big" not in c and c.used == 0
