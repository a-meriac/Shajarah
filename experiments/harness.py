"""What every system-experiment script agrees on: addresses, the configs, and the sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from edgeproxy.common.http import canonical_url

ROOT = Path(__file__).parents[1]
SESSIONS = ROOT / "data" / "sessions.json"
SCENARIOS = ROOT / "emulation" / "scenarios"
CERTDIR = Path("/tmp/ep-run")
# Addresses in emulation/netns_setup.sh.
ORIGIN_HOST, ORIGIN_PORT = "10.8.0.2", 8080
ORIGIN = f"http://{ORIGIN_HOST}:{ORIGIN_PORT}"
SERVER_HOST, SERVER_PORT = "10.9.0.2", 4433
INTERFACE_IPS = {"wifi0": "10.1.0.2", "cell0": "10.2.0.2", "sat0": "10.3.0.2"}


@dataclass(frozen=True)
class RunConfig:
    name: str
    transport: str  # quic | tcp
    predictor: str  # none | jev
    fixed_policy: bool  # prefetch ignores handover hints
    proactive: bool  # switch interface before the link dies
    send_hints: bool  # tell the server when a dropout is coming
    hover_oracle: bool = False  # the next click is known 200 ms early (Speculation Rules)
    about: str = ""


CONFIGS = {
    c.name: c
    for c in [
        RunConfig("1", "tcp", "none", False, False, False, about="TCP+TLS, reconnect"),
        RunConfig("2", "quic", "none", False, False, False, about="QUIC migration after failure"),
        RunConfig("3", "quic", "jev", True, False, False, about="always-on fixed prefetch"),
        RunConfig("4", "quic", "none", False, False, False, True, "hover oracle, 200 ms early"),
        RunConfig("5", "quic", "jev", False, True, True, about="full: switch early + hints"),
        RunConfig("5a", "quic", "none", False, True, False, about="switch early only"),
        RunConfig("5b", "quic", "jev", False, False, True, about="hint-driven prefetch only"),
    ]
}


def page_url(title: str) -> str:
    return canonical_url(f"{ORIGIN}/wiki/{title}")


def load_sessions() -> list[dict]:
    return json.loads(SESSIONS.read_text(encoding="utf-8"))["sessions"]
