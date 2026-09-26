"""Tunable settings, read from settings.yaml at the repo root.

The dataclass defaults below apply to anything the file leaves out. A misspelled key or a value
of the wrong type is an error, so a typo can't silently fall back to a default.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from edgeproxy.client.handover_predictor import PredictorConfig
from edgeproxy.predictors.jev import (
    DEFAULT_INCLUDE_CONTEXT,
    DEFAULT_LINK_ORDER,
    DEFAULT_MAX_OPTIONS,
    MODEL,
)
from edgeproxy.server.prefetch_policy import PolicyConfig
from edgeproxy.tunnel.quic_tunnel import DEFAULT_IDLE_TIMEOUT

SETTINGS_FILE = Path(__file__).parents[2] / "settings.yaml"


@dataclass
class JevSettings:
    model: str = MODEL
    max_options: int = DEFAULT_MAX_OPTIONS
    link_order: str = DEFAULT_LINK_ORDER
    include_context: bool = DEFAULT_INCLUDE_CONTEXT
    timeout_s: float = 30.0


@dataclass
class LinkSettings:
    max_candidates: int = 2000
    same_origin_only: bool = True


@dataclass
class ClientSettings:
    cache_mb: int = 200
    push_ttl_s: float = 900.0  # unread prefetched pages expire after this (0 = never)
    request_timeout_s: float = 60.0
    revalidate_timeout_s: float = 2.0
    retry_delay_s: float = 0.2


@dataclass
class ServerSettings:
    origin_timeout_s: float = 15.0


@dataclass
class TunnelSettings:
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT
    tcp_connect_timeout_s: float = 5.0
    compress: bool = True


@dataclass
class PathSettings:
    wifi_threshold_dbm: float = -80.0  # cellular/satellite use handover.threshold_dbm
    expected_outage_s: float = 30.0
    dead_after_s: float = 1.0
    ping_interval_s: float = 0.2
    tick_s: float = 0.1
    return_after_s: float = 3.0  # a cheaper network must stay healthy this long before moving back


@dataclass
class SnapshotSettings:
    key_month: str = "2026-07"
    session_month: str = "2026-08"
    history_month: str = "2026-06"
    buckets: dict = field(
        default_factory=lambda: {
            "head": [0, 1_000],
            "torso": [1_000, 50_000],
            "tail": [50_000, 300_000],
        }
    )
    per_bucket: int = 334
    targets_per_seed: int = 1
    seed: int = 0
    concurrency: int = 4


@dataclass
class SessionSettings:
    count: int = 30
    pages: int = 12  # longest session; runs stop at the scenario's end anyway
    dwell_median_s: float = 20.0
    dwell_sigma: float = 0.8
    dwell_min_s: float = 3.0
    dwell_max_s: float = 120.0
    seed: int = 1


SECTIONS = {
    "jev": JevSettings,
    "links": LinkSettings,
    "prefetch": PolicyConfig,
    "handover": PredictorConfig,
    "client": ClientSettings,
    "server": ServerSettings,
    "tunnel": TunnelSettings,
    "paths": PathSettings,
    "snapshot": SnapshotSettings,
    "sessions": SessionSettings,
}


@dataclass
class Settings:
    jev: JevSettings = field(default_factory=JevSettings)
    links: LinkSettings = field(default_factory=LinkSettings)
    prefetch: PolicyConfig = field(default_factory=PolicyConfig)
    handover: PredictorConfig = field(default_factory=PredictorConfig)
    client: ClientSettings = field(default_factory=ClientSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    tunnel: TunnelSettings = field(default_factory=TunnelSettings)
    paths: PathSettings = field(default_factory=PathSettings)
    snapshot: SnapshotSettings = field(default_factory=SnapshotSettings)
    sessions: SessionSettings = field(default_factory=SessionSettings)


def _check(where: str, value, default):
    expected = type(default)
    if isinstance(value, bool) != isinstance(default, bool):
        raise TypeError(f"{where}: expected {expected.__name__}, got {value!r}")
    if expected is float and isinstance(value, int):
        return float(value)
    if not isinstance(value, expected):
        raise TypeError(f"{where}: expected {expected.__name__}, got {value!r}")
    return value


def load_settings(path: Path | None = None) -> Settings:
    path = path or SETTINGS_FILE
    raw = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    unknown = set(raw) - set(SECTIONS)
    if unknown:
        raise ValueError(f"{path.name}: unknown section(s) {sorted(unknown)}")
    sections = {}
    for name, cls in SECTIONS.items():
        values = raw.get(name) or {}
        defaults = cls()
        known = {f.name for f in fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"{path.name}: unknown key(s) in {name}: {sorted(unknown)}")
        checked = {
            k: _check(f"{path.name}: {name}.{k}", v, getattr(defaults, k))
            for k, v in values.items()
        }
        sections[name] = cls(**checked)
    return Settings(**sections)
