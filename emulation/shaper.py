"""Apply netem profiles to the emulated access links, following a scenario timeline.

Shaping is applied on both ends of each veth (client egress in ep-cli, downlink in ep-rtr), so
delay/loss hit both directions. `tc qdisc replace` is idempotent, so changing a profile mid-run
does not tear down sockets.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

HERE = Path(__file__).parent
CLIENT_NS, ROUTER_NS = "ep-cli", "ep-rtr"


@dataclass
class Profile:
    delay_ms: float
    jitter_ms: float
    loss_pct: float
    rate_mbit: float


def load_profiles(path: Path = HERE / "profiles.yaml") -> dict[str, Profile]:
    return {k: Profile(**v) for k, v in yaml.safe_load(path.read_text()).items()}


def netem_args(p: Profile) -> list[str]:
    args = ["netem"]
    if p.delay_ms:
        args += ["delay", f"{p.delay_ms}ms"]
        if p.jitter_ms:
            # distribution normal + no reordering would need a rate/slot model; keep the default
            # (jitter can reorder packets, which is realistic for radio links).
            args += [f"{p.jitter_ms}ms"]
    if p.loss_pct:
        args += ["loss", f"{p.loss_pct}%"]
    if p.rate_mbit:
        args += ["rate", f"{p.rate_mbit}mbit"]
    return args


def tc_commands(iface: str, p: Profile) -> list[list[str]]:
    """Commands to shape both directions of one access link."""
    return [
        [
            "ip",
            "netns",
            "exec",
            CLIENT_NS,
            "tc",
            "qdisc",
            "replace",
            "dev",
            iface,
            "root",
            *netem_args(p),
        ],
        [
            "ip",
            "netns",
            "exec",
            ROUTER_NS,
            "tc",
            "qdisc",
            "replace",
            "dev",
            f"r-{iface}",
            "root",
            *netem_args(p),
        ],
    ]


def apply(iface: str, p: Profile, dry_run: bool = False) -> None:
    for cmd in tc_commands(iface, p):
        if dry_run:
            print(" ".join(cmd))
        else:
            subprocess.run(cmd, check=True)


async def run_scenario(scenario: dict, profiles: dict[str, Profile], dry_run=False, log=print):
    for iface, name in scenario["initial"].items():
        apply(iface, profiles[name], dry_run)
    start = time.monotonic()
    for ev in sorted(scenario.get("events", []), key=lambda e: e["t"]):
        delay = start + ev["t"] - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        apply(ev["iface"], profiles[ev["profile"]], dry_run)
        log(f"t={ev['t']:.1f} {ev['iface']} -> {ev['profile']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    scenario = yaml.safe_load(args.scenario.read_text())
    asyncio.run(run_scenario(scenario, load_profiles(), args.dry_run))


if __name__ == "__main__":
    main()
