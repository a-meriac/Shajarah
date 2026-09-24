from edgeproxy.client.handover_predictor import HandoverPredictor, PredictorConfig

CFG = PredictorConfig(threshold_dbm=-110, window_s=5, horizon_s=8)


def feed(pred, samples):
    out = None
    for t, v in samples:
        out = pred.update(t, v)
    return out


def test_stable_signal_no_hint():
    p = HandoverPredictor(CFG)
    assert feed(p, [(t * 0.5, -90 + (0.5 if t % 2 else -0.5)) for t in range(20)]) is None


def test_steady_fade_predicts_crossing():
    p = HandoverPredictor(CFG)
    # -90 dBm falling 3 dB/s: crosses -110 at t≈6.7 s. Hint once within the 8 s horizon.
    hint = feed(p, [(t * 0.25, -90 - 3 * t * 0.25) for t in range(9)])  # up to t=2 s
    assert hint is not None
    assert abs(hint.eta_s - (20 - 6) / 3) < 0.2  # level at t=2 is -96 -> 14 dB to go at 3 dB/s
    assert hint.confidence > 0.99


def test_below_threshold_is_immediate():
    p = HandoverPredictor(CFG)
    hint = feed(p, [(t * 0.5, -115.0) for t in range(6)])
    assert hint is not None and hint.eta_s == 0.0


def test_hysteresis_clears_after_recovery():
    p = HandoverPredictor(CFG)
    feed(p, [(t * 0.5, -115.0) for t in range(6)])
    assert p.active is not None
    # Recovering to -108 (above threshold but inside margin) keeps the hint...
    feed(p, [(3 + t * 0.5, -108.0) for t in range(12)])
    assert p.active is not None
    # ...back to -100 and flat clears it.
    feed(p, [(9 + t * 0.5, -100.0) for t in range(12)])
    assert p.active is None
