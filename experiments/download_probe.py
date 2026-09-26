"""Download probe: what a network switch does to a large download, TCP vs QUIC.

The client downloads one large file (random bytes, so compression can't help) through the tunnel
while the scenario runs; in the Wi-Fi walk, Wi-Fi fades and dies part-way through. The path
manager moves the tunnel to 5G, either only after the link has failed or early on the predicted
fade. With TCP a move means a new connection, so the download fails and starts again from the
first byte (as it does for an app without resume support). With QUIC the connection, and the
download, carry on over the new network.

The link counts as alive while bytes arrive on the active interface. Pings alone would be unfair
to TCP: its ping replies queue behind the download on the one connection, so it would look dead
while working.

Reported per run: time to finish, how many times the download restarted, bytes received in
total (vs the file size), the longest stretch with nothing arriving, and every interface's
received bytes over time (for a chart).

Linux, root. `batch` builds the namespaces and runs every mode N times:

  sudo .venv-linux/bin/python -m experiments.download_probe batch --repeats 3
  python -m experiments.download_probe summary     # table from results/download/*.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

import yaml

from edgeproxy.client.path_manager import Interface, PathManager
from edgeproxy.client.signal_monitor import TraceSignal
from edgeproxy.common.eventlog import EventLog
from edgeproxy.common.protocol import Frame, MsgType, set_compression
from edgeproxy.common.settings import load_settings
from edgeproxy.server.proxy import ServerProxy
from edgeproxy.tunnel.certs import generate_self_signed
from emulation.shaper import apply, load_profiles, run_scenario
from experiments.harness import CERTDIR, INTERFACE_IPS, ROOT, SCENARIOS, SERVER_HOST

PORT = 4435  # not the proxy's port, so a stray proxy can't answer
OUT = ROOT / "results" / "download"
MODES = {  # name: (transport, switch early)
    "tcp_reactive": ("tcp", False),
    "tcp_early": ("tcp", True),
    "quic_reactive": ("quic", False),
    "quic_early": ("quic", True),
}
NO_SIGNAL = [{"t": 0, "dbm": -140}]
SAMPLE_S = 0.1
RETRY_DELAY_S = 0.2


def rx_bytes(iface: str) -> int:
    return int(Path(f"/sys/class/net/{iface}/statistics/rx_bytes").read_text())


async def serve(transport: str) -> None:
    set_compression(False)  # random bytes; don't spend seconds trying to deflate them
    CERTDIR.mkdir(parents=True, exist_ok=True)
    cert, key = CERTDIR / "cert.pem", CERTDIR / "key.pem"
    if not cert.exists():
        generate_self_signed(cert, key)
    proxy = ServerProxy()  # answers the path manager's pings
    files: dict[int, bytes] = {}

    async def handle(frame: Frame, session) -> Frame | None:
        if frame.type is MsgType.REQUEST:
            size = int(frame.headers["size"])
            body = files.setdefault(size, os.urandom(size))
            return Frame(MsgType.RESPONSE, {"status": 200}, body)
        return await proxy.handle(frame, session)

    if transport == "quic":
        from edgeproxy.tunnel.quic_tunnel import QuicTunnelServer, server_configuration

        server = QuicTunnelServer("0.0.0.0", PORT, server_configuration(cert, key), handle)
    else:
        from edgeproxy.tunnel.tcp_transport import TcpTunnelServer, server_ssl_context

        server = TcpTunnelServer("0.0.0.0", PORT, server_ssl_context(cert, key), handle)
    await server.start()
    print(f"download server ({transport}) on :{PORT}", flush=True)
    await asyncio.Event().wait()


def make_transport(kind: str, local_ip: str, settings):
    cafile = CERTDIR / "cert.pem"
    if kind == "quic":
        from edgeproxy.tunnel.quic_tunnel import QuicClientTransport, client_configuration

        cfg = client_configuration(cafile, idle_timeout=settings.tunnel.idle_timeout_s)
        return QuicClientTransport((SERVER_HOST, PORT), cfg, local_addr=(local_ip, 0))
    from edgeproxy.tunnel.tcp_transport import TcpClientTransport, client_ssl_context

    return TcpClientTransport(
        (SERVER_HOST, PORT),
        client_ssl_context(cafile),
        local_addr=(local_ip, 0),
        connect_timeout=settings.tunnel.tcp_connect_timeout_s,
    )


async def download(scenario_name: str, mode: str, size: int, start_s: float) -> dict:
    kind, early = MODES[mode]
    settings = load_settings()
    scenario = yaml.safe_load((SCENARIOS / f"{scenario_name}.yaml").read_text())
    duration = scenario["duration_s"]
    profiles = load_profiles()
    for iface, name in scenario["initial"].items():
        apply(iface, profiles[name])
    active = next(n for n in INTERFACE_IPS if scenario["initial"].get(n, "dead") != "dead")

    transport = make_transport(kind, INTERFACE_IPS[active], settings)
    await transport.connect()
    log = EventLog(None, "download")
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
        proactive=early,
        send_hints=False,
        dead_after_s=p.dead_after_s,
        ping_interval_s=p.ping_interval_s,
        tick_s=p.tick_s,
        return_after_s=p.return_after_s,
        log=log,
    )

    t0 = time.monotonic()
    series: list[list[float]] = []  # [t, rx bytes per interface since t0...]
    base = {n: rx_bytes(n) for n in INTERFACE_IPS}

    async def sample() -> None:
        last = dict(base)
        while True:
            now = time.monotonic()
            rx = {n: rx_bytes(n) for n in INTERFACE_IPS}
            series.append([round(now - t0, 2), *(rx[n] - base[n] for n in INTERFACE_IPS)])
            if rx[paths.active] > last[paths.active]:
                paths.last_pong = now  # bytes arriving: the active link is alive
            last = rx
            await asyncio.sleep(SAMPLE_S)

    tasks = [
        asyncio.ensure_future(run_scenario(scenario, profiles, log=lambda msg: None)),
        asyncio.ensure_future(paths.run(duration)),
        asyncio.ensure_future(sample()),
    ]
    await asyncio.sleep(start_s)
    started, attempts, done_at = time.monotonic(), 0, None
    rx_at_start = sum(rx_bytes(n) for n in INTERFACE_IPS)
    while time.monotonic() - t0 < duration:
        attempts += 1
        try:
            left = duration - (time.monotonic() - t0)
            reply = await asyncio.wait_for(
                transport.request(Frame(MsgType.REQUEST, {"size": size})), left
            )
        except (ConnectionError, OSError):
            await asyncio.sleep(RETRY_DELAY_S)  # the tunnel moved (TCP): try again from byte 0
            continue
        except TimeoutError:
            break
        if len(reply.body) == size:
            done_at = time.monotonic()
            break
    rx_total = sum(rx_bytes(n) for n in INTERFACE_IPS) - rx_at_start
    await asyncio.sleep(0.5)
    for task in tasks:
        task.cancel()
    await transport.close()

    t_start = started - t0
    t_end = (done_at or time.monotonic()) - t0
    during = [row for row in series if t_start <= row[0] <= t_end]
    progress = [row[0] for prev, row in pairwise(during) if sum(row[1:]) > sum(prev[1:])]
    stall = max((b - a for a, b in pairwise([t_start, *progress, t_end])), default=0.0)
    switches = [e for e in log.events if e["event"] == "path_switch"]
    return {
        "scenario": scenario_name,
        "mode": mode,
        "size_bytes": size,
        "start_s": round(t_start, 2),
        "finished": done_at is not None,
        "time_s": round(t_end - t_start, 2),
        "attempts": attempts,
        "received_bytes": rx_total,
        "longest_stall_s": round(stall, 2),
        "switches": [{k: e[k] for k in ("t", "frm", "to", "reason")} for e in switches],
        "interfaces": list(INTERFACE_IPS),
        "series": series,
    }


def _ns(ns: str, *cmd: str, **kw) -> subprocess.Popen:
    return subprocess.Popen(["ip", "netns", "exec", ns, *cmd], cwd=ROOT, **kw)


def batch(scenario: str, repeats: int, size_mb: float, start_s: float) -> None:
    if os.geteuid() != 0:
        sys.exit("needs root: sudo .venv-linux/bin/python -m experiments.download_probe batch ...")
    netns = str(ROOT / "emulation" / "netns_setup.sh")
    OUT.mkdir(parents=True, exist_ok=True)
    me = [sys.executable, "-m", "experiments.download_probe"]
    try:
        for i in range(repeats):
            for mode, (kind, _) in MODES.items():
                out = OUT / f"{scenario}-{mode}-{i}.json"
                if out.exists():
                    continue
                subprocess.run([netns, "up"], check=True, capture_output=True)
                server = _ns("ep-srv", *me, "server", "--transport", kind,
                             stdout=subprocess.PIPE, text=True)  # fmt: skip
                try:
                    server.stdout.readline()  # "download server ..."
                    client = _ns("ep-cli", *me, "client", "--scenario", scenario, "--mode", mode,
                                 "--size-mb", str(size_mb), "--start-s", str(start_s),
                                 stdout=subprocess.PIPE, text=True)  # fmt: skip
                    result, _ = client.communicate(timeout=600)
                    out.write_text(result)
                    r = json.loads(result)
                    print(f"{mode:14} #{i}: {r['time_s']} s, {r['attempts']} attempt(s), "
                          f"longest stall {r['longest_stall_s']} s", flush=True)  # fmt: skip
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
    print(f"{'scenario':16} {'mode':14} {'n':>2} {'finished':>8} {'time s':>7} {'attempts':>8} "
          f"{'received / size':>15} {'longest stall s':>15}")  # fmt: skip
    for (scenario, mode), rs in sorted(groups.items()):
        time_s, attempts, stall = (
            statistics.median(r[k] for r in rs) for k in ("time_s", "attempts", "longest_stall_s")
        )
        ratio = statistics.median(r["received_bytes"] / r["size_bytes"] for r in rs)
        done = sum(r["finished"] for r in rs)
        print(f"{scenario:16} {mode:14} {len(rs):2} {done:>5}/{len(rs):<2} {time_s:7.1f} "
              f"{attempts:8.0f} {ratio:15.2f} {stall:15.2f}")  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("server")
    s.add_argument("--transport", choices=["quic", "tcp"], required=True)
    c = sub.add_parser("client")
    c.add_argument("--scenario", required=True)
    c.add_argument("--mode", choices=list(MODES), required=True)
    for p in (c, b := sub.add_parser("batch")):
        p.add_argument("--size-mb", type=float, default=100)
        p.add_argument("--start-s", type=float, default=18, help="scenario time to start at")
    b.add_argument("--scenario", default="wifi_to_5g_walk")
    b.add_argument("--repeats", type=int, default=3)
    sub.add_parser("summary")
    args = ap.parse_args()
    if args.cmd == "server":
        asyncio.run(serve(args.transport))
    elif args.cmd == "client":
        size = int(args.size_mb * 1_000_000)
        print(json.dumps(asyncio.run(download(args.scenario, args.mode, size, args.start_s))))
    elif args.cmd == "batch":
        batch(args.scenario, args.repeats, args.size_mb, args.start_s)
    else:
        summary()


if __name__ == "__main__":
    main()
