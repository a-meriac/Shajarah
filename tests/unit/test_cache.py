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


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_unread_prefetched_pages_expire():
    clock = FakeClock()
    c = Cache(capacity_bytes=1000, push_ttl_s=900, clock=clock)
    c.put(Entry("unread", b"x" * 10, pushed=True))
    c.put(Entry("read", b"y" * 20, pushed=True))
    c.put(Entry("fetched", b"z" * 30))  # the reader's own page: never expires by time
    c.get("read")
    clock.now += 899
    assert "unread" in c and c.peek("unread") is not None
    clock.now += 2
    assert c.peek("unread") is None
    assert "read" in c and "fetched" in c
    assert c.used == 50
    assert c.stats.expired_bytes == 10 and c.stats.wasted_bytes == 10
    assert c.finalize().wasted_bytes == 10  # expired pages aren't counted twice


def test_no_expiry_when_ttl_is_zero():
    clock = FakeClock()
    c = Cache(capacity_bytes=1000, clock=clock)
    c.put(Entry("unread", b"x" * 10, pushed=True))
    clock.now += 10**6
    assert c.peek("unread") is not None


def test_repush_restarts_the_clock():
    clock = FakeClock()
    c = Cache(capacity_bytes=1000, push_ttl_s=900, clock=clock)
    c.put(Entry("page", b"x" * 10, pushed=True))
    clock.now += 800
    c.put(Entry("page", b"x" * 10, pushed=True))  # sent again before the next dropout
    clock.now += 800
    assert c.peek("page") is not None
