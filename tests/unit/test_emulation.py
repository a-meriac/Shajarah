from pathlib import Path

import yaml

from edgeproxy.client.handover_predictor import HandoverPredictor
from edgeproxy.client.signal_monitor import TraceSignal
from emulation.shaper import Profile, load_profiles, tc_commands

SCEN = Path(__file__).parents[2] / "emulation" / "scenarios"


def test_tc_commands_shape_both_directions():
    cmds = tc_commands("cell0", Profile(12, 4, 0.2, 150))
    assert cmds[0][:4] == ["ip", "netns", "exec", "ep-cli"]
    assert "r-cell0" in cmds[1]
    netem = ["netem", "delay", "12ms", "4ms", "loss", "0.2%", "rate", "150mbit"]
    assert cmds[0][cmds[0].index("root") + 1 :] == netem
    assert cmds[1][cmds[1].index("root") + 1 :] == netem


def test_profiles_and_scenarios_are_consistent():
    profiles = load_profiles()
    for f in SCEN.glob("*.yaml"):
        s = yaml.safe_load(f.read_text())
        for name in list(s["initial"].values()) + [e["profile"] for e in s["events"]]:
            assert name in profiles, (f.name, name)


def test_car_tunnel_hint_precedes_outage():
    """The scenario is only useful if the fade is predictable before the link dies."""
    s = yaml.safe_load((SCEN / "car_tunnel_45s.yaml").read_text())
    outage_t = min(e["t"] for e in s["events"] if e["profile"] == "dead")
    sig = TraceSignal(s["signal"]["cell0"])
    pred = HandoverPredictor()
    first_hint = None
    for i in range(int(outage_t * 10) + 1):
        t = i / 10
        if pred.update(t, sig.sample(t)) and first_hint is None:
            first_hint = t
    assert first_hint is not None and outage_t - first_hint >= 2.0, first_hint
