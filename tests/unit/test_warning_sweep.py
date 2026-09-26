from itertools import pairwise

from edgeproxy.client.handover_predictor import PredictorConfig
from experiments.warning_sweep import TICK_S, evaluate

CFG = PredictorConfig(threshold_dbm=-110)


def signal(points):
    """Piecewise-linear signal, one value per tick, from (t, dBm) points."""
    out = []
    for (t0, v0), (t1, v1) in pairwise(points):
        n = round((t1 - t0) / TICK_S)
        out += [v0 + (v1 - v0) * i / n for i in range(n)]
    return out


def test_steady_fade_is_warned_seconds_ahead():
    s = signal([(0, -90), (30, -90), (38, -111), (39, -140), (60, -140)])
    leads, false_alarms = evaluate(s, s, CFG)
    assert len(leads) == 1 and 3 <= leads[0] <= 8
    assert false_alarms == 0


def test_dip_that_recovers_is_a_false_alarm():
    s = signal([(0, -90), (30, -90), (36, -106), (42, -90), (80, -90)])
    leads, false_alarms = evaluate(s, s, CFG)
    assert leads == [] and false_alarms == 1


def test_sudden_drop_is_not_warned():
    s = signal([(0, -90), (30, -90), (30.1, -140), (60, -140)])
    leads, _ = evaluate(s, s, CFG)
    assert leads == [0.0]
