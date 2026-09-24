"""One run on the client side: network timeline, tunnel, client proxy, path manager, reader.

Runs inside the ep-cli namespace as root (the runner starts it). Everything shares one clock:
the scenario's netem events, the path manager's signal traces and the reader all start at t=0.
The reader replays one session: it opens a page, waits for it (however long that takes), reads
for the page's dwell time, then clicks the next one, until the scenario ends.

  ip netns exec ep-cli python -m experiments.client_run --config 5 \
      --scenario car_tunnel_45s --session 0 --log results/.../client.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import httpx
import yaml

from edgeproxy.client.cache import Cache
from edgeproxy.client.path_manager import Interface, PathManager
from edgeproxy.client.proxy import PREFETCH_HEADER, SOURCE_HEADER, ClientProxy
from edgeproxy.client.signal_monitor import TraceSignal
from edgeproxy.common.eventlog import EventLog
from edgeproxy.common.protocol import set_compression
from edgeproxy.common.settings import load_settings
from emulation.shaper import apply, load_profiles, run_scenario
from experiments.harness import (
    CERTDIR,
    CONFIGS,
    INTERFACE_IPS,
    SCENARIOS,
    SERVER_HOST,
    SERVER_PORT,
    load_sessions,
    page_url,
)

NO_SIGNAL = [{"t": 0, "dbm": -140}]
ORACLE_LEAD_S = 0.2  # Speculation Rules "moderate": prefetch on hover, ~200 ms before the click


def make_transport(config, local_ip: str, settings):
    cafile = CERTDIR / "cert.pem"
    if config.transport == "quic":
        from edgeproxy.tunnel.quic_tunnel import QuicClientTransport, client_configuration

        cfg = client_configuration(cafile, idle_timeout=settings.tunnel.idle_timeout_s)
        return QuicClientTransport((SERVER_HOST, SERVER_PORT), cfg, local_addr=(local_ip, 0))
    from edgeproxy.tunnel.tcp_transport import TcpClientTransport, client_ssl_context

    return TcpClientTransport(
        (SERVER_HOST, SERVER_PORT),
        client_ssl_context(cafile),
        local_addr=(local_ip, 0),
        connect_timeout=settings.tunnel.tcp_connect_timeout_s,
    )


async def read_session(proxy_port: int, pages: list[dict], oracle: bool, t0: float, until: float,
                       log: EventLog) -> None:  # fmt: skip
    proxy = f"http://127.0.0.1:{proxy_port}"
    timeout = httpx.Timeout(None)  # the reader waits as long as it takes
    async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=timeout) as browser:
        for i, page in enumerate(pages):
            if time.monotonic() - t0 >= until:
                return
            url = page_url(page["title"])
            start = time.monotonic()
            log.emit("click", t=start - t0, i=i, url=url)
            r = await browser.get(url, headers={"Accept": "text/html"})
            wait = time.monotonic() - start
            log.emit(
                "view",
                t=start - t0,
                i=i,
                url=url,
                status=r.status_code,
                source=r.headers.get(SOURCE_HEADER, ""),
                wait_s=wait,
                bytes=len(r.content),
            )
            dwell = page["dwell_s"]
            nxt = pages[i + 1] if i + 1 < len(pages) else None
            if oracle and nxt is not None and dwell > ORACLE_LEAD_S:
                await asyncio.sleep(dwell - ORACLE_LEAD_S)
                # Hovering: prefetch in the background, click 200 ms later regardless.
                hover = browser.get(
                    page_url(nxt["title"]), headers={PREFETCH_HEADER: "1", "Accept": "text/html"}
                )
                asyncio.ensure_future(hover)
                await asyncio.sleep(ORACLE_LEAD_S)
            else:
                await asyncio.sleep(dwell)


async def run(args) -> None:
    settings = load_settings(args.settings)
    set_compression(settings.tunnel.compress)
    config = CONFIGS[args.config]
    scenario = yaml.safe_load((SCENARIOS / f"{args.scenario}.yaml").read_text())
    duration = args.duration or scenario["duration_s"]
    session = load_sessions()[args.session]
    log = EventLog(args.log, "client", args.run_id)
    profiles = load_profiles()

    usable = [n for n in INTERFACE_IPS if scenario["initial"].get(n, "dead") != "dead"]
    active = usable[0]
    log.emit("run_start", config=config.name, scenario=args.scenario, session=args.session,
             active=active, duration_s=duration)  # fmt: skip

    # Initial link conditions before the tunnel comes up, then everything starts at t=0.
    for iface, name in scenario["initial"].items():
        apply(iface, profiles[name])
    transport = make_transport(config, INTERFACE_IPS[active], settings)
    await transport.connect()
    c = settings.client
    proxy = ClientProxy(
        transport,
        Cache(c.cache_mb * 1_000_000),
        log,
        request_timeout=c.request_timeout_s,
        revalidate_timeout=c.revalidate_timeout_s,
        retry_delay=c.retry_delay_s,
    )
    await proxy.start("127.0.0.1", 0)

    signals = scenario.get("signal", {})
    p = settings.paths
    interfaces = [
        Interface(
            name,
            ip,
            TraceSignal(signals.get(name, NO_SIGNAL)),
            p.wifi_threshold_dbm if name.startswith("wifi") else settings.handover.threshold_dbm,
        )
        for name, ip in INTERFACE_IPS.items()
    ]
    paths = PathManager(
        transport,
        interfaces,
        active,
        settings.handover,
        proactive=config.proactive,
        send_hints=config.send_hints,
        expected_outage_s=p.expected_outage_s,
        dead_after_s=p.dead_after_s,
        ping_interval_s=p.ping_interval_s,
        tick_s=p.tick_s,
        log=log,
    )

    def netem_log(msg: str) -> None:
        log.emit("netem", msg=msg)

    t0 = time.monotonic()
    tasks = [
        asyncio.ensure_future(run_scenario(scenario, profiles, log=netem_log)),
        asyncio.ensure_future(paths.run(duration)),
    ]
    reader = asyncio.ensure_future(
        read_session(proxy.port, session["pages"], config.hover_oracle, t0, duration, log)
    )
    try:
        await asyncio.wait_for(reader, timeout=duration)
    except TimeoutError:
        pass  # the scenario ended while the reader was waiting for a page
    for task in tasks:
        task.cancel()
    stats = proxy.cache.finalize()
    log.emit("run_end", t=time.monotonic() - t0, **vars(stats))
    await proxy.close()
    log.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", choices=list(CONFIGS), required=True)
    ap.add_argument("--scenario", required=True, help="file name in emulation/scenarios/")
    ap.add_argument("--session", type=int, required=True)
    ap.add_argument("--duration", type=float, default=None, help="default: the scenario's")
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
