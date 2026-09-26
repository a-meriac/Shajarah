from edgeproxy.server.prefetch_policy import LinkOutlook, PolicyConfig, PrefetchPolicy

PROBS = {"a": 0.5, "b": 0.2, "c": 0.06, "d": 0.01}
CFG = PolicyConfig(
    threshold=0.25,
    budget_bytes=1000,
    handover_threshold=0.05,
    handover_budget_bytes=2500,
    handover_budget_per_outage_s=100,
    default_size=500,
)


def test_normal_is_conservative():
    d = PrefetchPolicy(CFG).decide(PROBS, LinkOutlook())
    assert [u for u, _ in d.urls] == ["a"]


def test_handover_lowers_threshold_and_raises_budget():
    d = PrefetchPolicy(CFG).decide(PROBS, LinkOutlook(handover_imminent=True))
    assert [u for u, _ in d.urls] == ["a", "b", "c"]
    assert d.budget_bytes == 2500


def test_budget_scales_with_outage_and_is_capped():
    p = PrefetchPolicy(
        PolicyConfig(
            max_budget_bytes=10_000, handover_budget_bytes=1000, handover_budget_per_outage_s=1000
        )
    )
    assert p.limits(LinkOutlook(True, predicted_outage_s=5))[1] == 6000
    assert p.limits(LinkOutlook(True, predicted_outage_s=60))[1] == 10_000


def test_non_adaptive_ignores_handover():
    d = PrefetchPolicy(CFG, adaptive=False).decide(PROBS, LinkOutlook(handover_imminent=True))
    assert [u for u, _ in d.urls] == ["a"]


def test_skips_cached_and_packs_smaller_items():
    sizes = {"a": 400, "b": 2000, "c": 100}
    d = PrefetchPolicy(CFG).decide(
        PROBS, LinkOutlook(handover_imminent=True), sizes=sizes, already_cached={"a"}
    )
    assert [u for u, _ in d.urls] == ["b", "c"]


def test_network_scales_the_normal_budget_but_not_the_outage_budget():
    p = PrefetchPolicy(CFG)
    assert p.limits(LinkOutlook(network="wifi"))[1] == 1000
    assert p.limits(LinkOutlook(network="cellular"))[1] == 250
    assert p.limits(LinkOutlook(network="satellite"))[1] == 50
    assert p.limits(LinkOutlook(network=""))[1] == 1000  # not reported yet
    assert p.limits(LinkOutlook(True, network="satellite"))[1] == 2500


def test_depth2_only_for_long_outages_with_adaptive_policy():
    p = PrefetchPolicy(PolicyConfig(depth2_top_k=3, depth2_min_outage_s=20))
    assert p.depth2_k(LinkOutlook()) == 0
    assert p.depth2_k(LinkOutlook(handover_imminent=True, predicted_outage_s=10)) == 0
    assert p.depth2_k(LinkOutlook(handover_imminent=True, predicted_outage_s=30)) == 3
    fixed = PrefetchPolicy(PolicyConfig(depth2_top_k=3), adaptive=False)
    assert fixed.depth2_k(LinkOutlook(handover_imminent=True, predicted_outage_s=30)) == 0
