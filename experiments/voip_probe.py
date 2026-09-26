"""Voice-call probe: what a network switch does to a live stream, reacting vs switching early.

A call sends ~50 small packets a second. The client sends one QUIC datagram every 20 ms (sequence
number + send time) and the server echoes it. The path manager runs on the scenario's signal
traces exactly as in the system runs, either only reacting to failure (as configs 1-4) or also
switching on a predicted fade (as config 5/5a). Reported per run: packets lost, the longest
silence between echoes, round-trip times, and when the switch happened.

Linux, root. `batch` builds the namespaces and runs both modes N times on one scenario:

  sudo .venv-linux/bin/python -m experiments.voip_probe batch --scenario wifi_to_5g_walk --repeats 5
  python -m experiments.voip_probe summary          # table from results/voip/*.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import struct
import subprocess
import sys
import time
from itertools import pairwise

import yaml

from edgeproxy.client.path_manager import Interface, PathManager
from edgeproxy.client.signal_monitor import TraceSignal
from edgeproxy.common.eventlog import EventLog
from edgeproxy.common.settings import load_settings
from edgeproxy.server.proxy import ServerProxy
from edgeproxy.tunnel.certs import generate_self_signed
from edgeproxy.tunnel.quic_tunnel import (
    QuicClientTransport,
    QuicTunnelServer,
    client_configuration,
    server_configuration,
)
from emulation.shaper import apply, load_profiles, run_scenario
from experiments.harness import CERTDIR, INTERFACE_IPS, ROOT, SCENARIOS, SERVER_HOST

PORT = 4434  # not the proxy's port, so a stray proxy can't answer
INTERVAL_S = 0.02  # 50 packets per second
PACKET = struct.Struct("!Id")  # sequence number, send time (client clock)
PADDING = b"\0" * 148  # ~160-byte payload, like a 64 kbit/s voice frame
OUT = ROOT / "results" / "voip"
MODES = {"reactive": False, "switch_early": True}
NO_SIGNAL = [{"t": 0, "dbm": -140}]


async def serve() -> None:
    CERTDIR.mkdir(parents=True, exist_ok=True)
    cert, key = CERTDIR / "cert.pem", CERTDIR / "key.pem"
    if not cert.exists():
        generate_self_signed(cert, key)
    proxy = ServerProxy()  # answers the path manager's pings
    server: QuicTunnelServer

    def echo(data: bytes) -> None:
        for session in server.sessions:  # one client per run
            session.send_datagram(data)

    server = QuicTunnelServer(
        "0.0.0.0", PORT, server_configuration(cert, key), proxy.handle, on_datagram=echo
    )
    await server.start()
    print(f"voip echo server on :{PORT}", flush=True)
    await asyncio.Event().wait()


async def call(scenario_name: str, mode: str) -> dict:
    settings = load_settings()
    scenario = yaml.safe_load((SCENARIOS / f"{scenario_name}.yaml").read_text())
    duration = scenario["duration_s"]
    profiles = load_profiles()
    for iface, name in scenario["initial"].items():
        apply(iface, profiles[name])
    active = next(n for n in INTERFACE_IPS if scenario["initial"].get(n, "dead") != "dead")

    transport = QuicClientTransport(
        (SERVER_HOST, PORT),
        client_configuration(CERTDIR / "cert.pem", idle_timeout=settings.tunnel.idle_timeout_s),
        local_addr=(INTERFACE_IPS[active], 0),
    )
    t0 = 0.0
    echoes: list[tuple[int, float, float]] = []  # (seq, received at, rtt)

    def on_echo(data: bytes) -> None:
        seq, sent = PACKET.unpack_from(data)
        now = time.monotonic()
        echoes.append((seq, now - t0, now - sent))

    transport.on_datagram = on_echo
    await transport.connect()

    log = EventLog(None, "voip")
    signals = scenario.get("signal", {})
    p = settings.paths
    paths = PathManager(
        transport,
        [
            Interface(
                n,
                ip,
                TraceSignal(signals.get(n, NO_SIGNAL)),
                p.wifi_threshold_dbm if n.startswith("wifi") else settings.handover.threshold_dbm,
            )
            for n, ip in INTERFACE_IPS.items()
        ],
        active,
        settings.handover,
        proactive=MODES[mode],
        send_hints=False,
        dead_after_s=p.dead_after_s,
        ping_interval_s=p.ping_interval_s,
        tick_s=p.tick_s,
        return_after_s=p.return_after_s,
        log=log,
    )

    t0 = time.monotonic()
    tasks = [
        asyncio.ensure_future(run_scenario(scenario, profiles, log=lambda msg: None)),
        asyncio.ensure_future(paths.run(duration)),
    ]
    sent = 0
    while time.monotonic() - t0 < duration:
        try:
            transport.send_datagram(PACKET.pack(sent, time.monotonic()) + PADDING)
        except (OSError, ConnectionError):
            pass
        sent += 1
        await asyncio.sleep(max(0.0, t0 + sent * INTERVAL_S - time.monotonic()))
    await asyncio.sleep(1.0)  # let the last echoes arrive
    for task in tasks:
        task.cancel()
    await transport.close()

    received = sorted({seq for seq, _, _ in echoes})
    times = sorted(at for _, at, _ in echoes)
    gaps = [(b - a, a) for a, b in pairwise(times)]
    longest, gap_at = max(gaps, default=(duration, 0.0))
    switches = [e for e in log.events if e["event"] == "path_switch"]
    rtts = [rtt for _, _, rtt in echoes]
    return {
        "scenario": scenario_name,
        "mode": mode,
        "sent": sent,
        "received": len(received),
        "lost_pct": round(100 * (1 - len(received) / sent), 2),
        "longest_silence_s": round(longest, 3),
        "silence_starts_s": round(gap_at, 2),
        "rtt_ms_median": round(1000 * statistics.median(rtts), 1) if rtts else None,
        "rtt_ms_p99": round(1000 * sorted(rtts)[int(0.99 * (len(rtts) - 1))], 1) if rtts else None,
        "switches": [{k: e[k] for k in ("t", "frm", "to", "reason")} for e in switches],
    }


def _ns(ns: str, *cmd: str, **kw) -> subprocess.Popen:
    return subprocess.Popen(["ip", "netns", "exec", ns, *cmd], cwd=ROOT, **kw)


def batch(scenario: str, repeats: int) -> None:
    if os.geteuid() != 0:
        sys.exit("needs root: sudo .venv-linux/bin/python -m experiments.voip_probe batch ...")
    netns = str(ROOT / "emulation" / "netns_setup.sh")
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        for i in range(repeats):
            for mode in MODES:
                out = OUT / f"{scenario}-{mode}-{i}.json"
                if out.exists():
                    continue
                subprocess.run([netns, "up"], check=True, capture_output=True)
                server = _ns("ep-srv", sys.executable, "-m", "experiments.voip_probe", "server",
                             stdout=subprocess.PIPE, text=True)  # fmt: skip
                try:
                    server.stdout.readline()  # "voip echo server on ..."
                    client = _ns("ep-cli", sys.executable, "-m", "experiments.voip_probe", "call",
                                 "--scenario", scenario, "--mode", mode,
                                 stdout=subprocess.PIPE, text=True)  # fmt: skip
                    result, _ = client.communicate(timeout=300)
                    out.write_text(result)
                    r = json.loads(result)
                    print(f"{mode:13} #{i}: lost {r['lost_pct']}%, "
                          f"longest silence {r['longest_silence_s']} s", flush=True)  # fmt: skip
                finally:
                    server.terminate()
                    server.wait(5)
                    subprocess.run([netns, "down"], capture_output=True, check=False)
    finally:
        owner = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if all(owner):
            for path in [OUT, *OUT.iterdir()]:
                os.chown(path, int(owner[0]), int(owner[1]))


def summary() -> None:
    rows = [json.loads(p.read_text()) for p in sorted(OUT.glob("*.json"))]
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["scenario"], r["mode"]), []).append(r)
    print(f"{'scenario':16} {'mode':13} {'n':>2} {'lost %':>7} {'longest silence s':>18} "
          f"{'rtt ms (median)':>16}")  # fmt: skip
    for (scenario, mode), rs in sorted(groups.items()):
        lost, silence, rtt = (
            statistics.median(r[k] for r in rs)
            for k in ("lost_pct", "longest_silence_s", "rtt_ms_median")
        )
        print(f"{scenario:16} {mode:13} {len(rs):2} {lost:7.2f} {silence:18.3f} {rtt:16.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("server")
    c = sub.add_parser("call")
    c.add_argument("--scenario", required=True)
    c.add_argument("--mode", choices=list(MODES), required=True)
    b = sub.add_parser("batch")
    b.add_argument("--scenario", default="wifi_to_5g_walk")
    b.add_argument("--repeats", type=int, default=5)
    sub.add_parser("summary")
    args = ap.parse_args()
    if args.cmd == "server":
        asyncio.run(serve())
    elif args.cmd == "call":
        print(json.dumps(asyncio.run(call(args.scenario, args.mode))))
    elif args.cmd == "batch":
        batch(args.scenario, args.repeats)
    else:
        summary()


if __name__ == "__main__":
    main()
