"""Path manager on the scenario files, tick by tick, against a fake tunnel."""

from pathlib import Path

import yaml

from edgeproxy.client.path_manager import Interface, PathManager
from edgeproxy.client.signal_monitor import TraceSignal
from edgeproxy.common.eventlog import EventLog
from edgeproxy.common.protocol import MsgType

SCEN = Path(__file__).parents[2] / "emulation" / "scenarios"
THRESHOLDS = {"wifi0": -80.0, "cell0": -110.0, "sat0": -110.0}
IPS = {"wifi0": "10.1.0.2", "cell0": "10.2.0.2", "sat0": "10.3.0.2"}
DEAD_AFTER = 1.0


class FakeTransport:
    def __init__(self):
        self.migrations, self.sent = [], []

    async def migrate(self, local_addr):
        self.migrations.append(local_addr[0])

    async def send(self, frame):
        self.sent.append(frame)

    def hints(self):
        return [f for f in self.sent if f.type is MsgType.HANDOVER_HINT]

    def networks(self):
        return [f.headers["kind"] for f in self.sent if f.type is MsgType.NETWORK]


def dead_intervals(scenario):
    """iface -> list of (start, end) when netem makes it dead."""
    out = {}
    state = dict(scenario["initial"])
    since = dict.fromkeys(state, 0.0)
    for ev in sorted(scenario["events"], key=lambda e: e["t"]):
        if state[ev["iface"]] == "dead":
            out.setdefault(ev["iface"], []).append((since[ev["iface"]], ev["t"]))
        state[ev["iface"]], since[ev["iface"]] = ev["profile"], ev["t"]
    for iface, profile in state.items():
        if profile == "dead":
            out.setdefault(iface, []).append((since[iface], float("inf")))
    return out


async def replay(name, **kwargs):
    scenario = yaml.safe_load((SCEN / f"{name}.yaml").read_text())
    signals = scenario["signal"]
    ifaces = [
        Interface(n, IPS[n], TraceSignal(signals.get(n, [{"t": 0, "dbm": -140}])), THRESHOLDS[n])
        for n in THRESHOLDS
    ]
    start = next(n for n, p in scenario["initial"].items() if p != "dead")
    transport = FakeTransport()
    pm = PathManager(
        transport, ifaces, start, dead_after_s=DEAD_AFTER, log=EventLog(None, "test"), **kwargs
    )
    dead = dead_intervals(scenario)
    for i in range(int(scenario["duration_s"] * 10)):
        t = i / 10
        # The pings stop being answered DEAD_AFTER seconds after the active link dies.
        alive = not any(a + DEAD_AFTER <= t < b for a, b in dead.get(pm.active, []))
        await pm.step(t, alive)
    return pm, transport


async def test_walk_switches_before_wifi_dies_when_proactive():
    pm, transport = await replay("wifi_to_5g_walk", proactive=True, send_hints=True)
    ((t, frm, to, reason),) = pm.switches
    assert (frm, to, reason) == ("wifi0", "cell0", "proactive")
    assert t < 28.5 - 2  # seconds of warning before Wi-Fi goes dead at 28.5 s
    assert transport.migrations == ["10.2.0.2"]
    assert transport.hints() == []  # 5G was available, so no dropout for the server to prepare for
    assert transport.networks() == ["wifi", "cellular"]


async def test_walk_switches_only_after_failure_when_reactive():
    pm, _ = await replay("wifi_to_5g_walk", proactive=False, send_hints=False)
    ((t, _, to, reason),) = pm.switches
    assert (to, reason) == ("cell0", "reactive")
    assert 28.5 + DEAD_AFTER <= t < 28.5 + DEAD_AFTER + 0.2


async def test_outage_warns_server_before_the_drop_and_clears_after():
    pm, transport = await replay("car_tunnel_45s", proactive=True, send_hints=True)
    assert pm.switches == []  # nowhere to switch to: every network drops
    hints = [(f.headers["active"], f.headers["outage_s"]) for f in transport.hints()]
    assert [h[0] for h in hints] == [True, False]
    assert hints[0][1] > 0
    first = next(e for e in pm.log.events if e["event"] == "hint_sent")
    assert first["t"] <= 38 - 3  # the server gets 3+ s to prefetch before the drop at 38 s


async def test_no_hints_unless_enabled():
    _, transport = await replay("car_tunnel_45s", proactive=True, send_hints=False)
    assert transport.sent == []


async def test_logs_signal_and_warnings_for_the_replay_viewer():
    pm, _ = await replay("car_tunnel_45s", proactive=True, send_hints=True)
    signals = [e for e in pm.log.events if e["event"] == "signal"]
    assert 0.45 <= signals[1]["t"] - signals[0]["t"] <= 0.55  # every signal_log_s
    assert set(signals[0]["dbm"]) == {"wifi0", "cell0", "sat0"}
    assert signals[0]["active"] == "cell0"
    cell = [e for e in pm.log.events if e["event"] == "warning" and e["iface"] == "cell0"]
    assert cell[0]["on"] and cell[0]["t"] < 38  # warned before the drop
    assert any(not e["on"] and e["t"] > 84 for e in cell)  # and cleared after it


def trace(*points):
    return [{"t": t, "dbm": dbm} for t, dbm in points]


def _manager(traces, start, transport, **kwargs):
    ifaces = [Interface(n, IPS[n], TraceSignal(tr), THRESHOLDS[n]) for n, tr in traces.items()]
    return PathManager(transport, ifaces, start, log=EventLog(None, "test"), **kwargs)


async def test_moves_back_to_wifi_once_it_has_been_healthy_for_a_while():
    transport = FakeTransport()
    wifi = trace((0, -95), (10, -95), (11, -60), (30, -60))
    cell = trace((0, -90), (30, -90))
    pm = _manager({"wifi0": wifi, "cell0": cell}, "cell0", transport, return_after_s=3.0)
    for i in range(300):
        await pm.step(i / 10, True)
    ((t, frm, to, reason),) = pm.switches
    assert (frm, to, reason) == ("cell0", "wifi0", "cheaper")
    assert 13 <= t < 15  # healthy from ~11 s, plus return_after_s
    assert transport.networks() == ["cellular", "wifi"]


async def test_prefers_cheaper_network_over_stronger_one():
    transport = FakeTransport()
    wifi = trace((0, -55), (20, -60), (28, -82), (30, -95))
    cell = trace((0, -100), (30, -100))  # weaker margin than satellite
    sat = trace((0, -70), (30, -70))
    pm = _manager({"wifi0": wifi, "cell0": cell, "sat0": sat}, "wifi0", transport)
    for i in range(300):
        await pm.step(i / 10, True)
    assert [(to, reason) for _, _, to, reason in pm.switches] == [("cell0", "proactive")]


async def test_does_not_leave_a_working_link_for_a_pricier_one():
    transport = FakeTransport()
    cell = trace((0, -100), (30, -100))
    sat = trace((0, -60), (30, -60))
    pm = _manager({"cell0": cell, "sat0": sat}, "cell0", transport)
    for i in range(300):
        await pm.step(i / 10, True)
    assert pm.switches == []
