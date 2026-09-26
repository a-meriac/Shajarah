"""Decides which interface the tunnel uses, and tells the server when a dropout is coming.

Every tick it reads each interface's signal and feeds that interface's handover predictor.

- Reactive switch (every config): pings through the tunnel; if none has been answered for
  `dead_after_s`, the link is treated as dead and the tunnel moves to the healthiest other
  interface. This is how a system without prediction notices an outage.
- Proactive switch (`proactive`, config 5): if the active interface's signal is predicted to
  fail soon and another interface is healthy, move before the link dies.
- Cheaper network back (every config, as phones do): if a cheaper interface (Wi-Fi before
  cellular before satellite) has been healthy for `return_after_s`, move the tunnel to it.
- Hints (`send_hints`, config 5): if a dropout is predicted and no other interface is healthy,
  send HANDOVER_HINT so the server prefetches more; clear it once the outlook is good again.
  Also tell the server which kind of network the tunnel is on (NETWORK), so it prefetches less
  on mobile data and satellite.

An interface is "healthy" if its signal is above its usable threshold plus a margin and its own
predictor isn't warning. Among healthy interfaces the cheapest kind wins, then the strongest.

For the replay viewer it logs every interface's signal (`signal`, every `signal_log_s`), each
predictor warning starting or ending (`warning`), and every ping's round-trip time (`ping`).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace

from edgeproxy.client.handover_predictor import HandoverHint, HandoverPredictor, PredictorConfig
from edgeproxy.common.eventlog import NULL_LOG, EventLog
from edgeproxy.common.protocol import Frame, MsgType
from edgeproxy.tunnel.transport import ClientTransport

KINDS = ("wifi", "cellular", "satellite")  # cheapest first


def kind_of(name: str) -> str:
    """Kind of network from the interface name (wifi0, cell0, sat0; wlan0, rmnet0, ...)."""
    if name.startswith(("wifi", "wlan", "wl")):
        return "wifi"
    if name.startswith("sat"):
        return "satellite"
    return "cellular"


@dataclass
class Interface:
    name: str
    local_ip: str
    signal: object  # anything with sample(t) -> dBm, e.g. signal_monitor.TraceSignal
    threshold_dbm: float  # below this the link is unusable
    kind: str = ""  # wifi | cellular | satellite; guessed from the name if left out

    def __post_init__(self) -> None:
        self.kind = self.kind or kind_of(self.name)

    @property
    def cost(self) -> int:
        return KINDS.index(self.kind) if self.kind in KINDS else len(KINDS)


class PathManager:
    def __init__(
        self,
        transport: ClientTransport,
        interfaces: list[Interface],
        active: str,
        predictor_config: PredictorConfig | None = None,
        proactive: bool = True,
        send_hints: bool = True,
        expected_outage_s: float = 30.0,
        dead_after_s: float = 1.0,
        ping_interval_s: float = 0.2,
        tick_s: float = 0.1,
        return_after_s: float = 3.0,
        log: EventLog = NULL_LOG,
        signal_log_s: float = 0.5,
    ) -> None:
        config = predictor_config or PredictorConfig()
        self.transport = transport
        self.interfaces = {i.name: i for i in interfaces}
        self.active = active
        self.proactive = proactive
        self.send_hints = send_hints
        self.expected_outage_s = expected_outage_s
        self.dead_after_s = dead_after_s
        self.ping_interval_s = ping_interval_s
        self.tick_s = tick_s
        self.return_after_s = return_after_s
        self.margin_db = config.clear_margin_db
        self.log = log
        self.predictors = {
            i.name: HandoverPredictor(replace(config, threshold_dbm=i.threshold_dbm))
            for i in interfaces
        }
        self.levels: dict[str, float] = {}
        self.hints: dict[str, HandoverHint | None] = {}
        self.hint_sent = False
        self.network_sent: str | None = None  # kind last reported to the server
        self.healthy_since: dict[str, float] = {}
        self.last_pong = time.monotonic()
        self.switches: list[tuple[float, str, str, str]] = []  # (t, from, to, reason)
        self.signal_log_s = signal_log_s
        self._next_signal_log = 0.0
        self._t0 = time.monotonic()  # scenario t=0; reset by run()

    # ------------------------------------------------------------------------------ decisions

    def _healthy(self, name: str) -> bool:
        iface = self.interfaces[name]
        return self.levels[name] > iface.threshold_dbm + self.margin_db and self.hints[name] is None

    def _best_alternative(self) -> str | None:
        options = [n for n in self.interfaces if n != self.active and self._healthy(n)]
        return min(
            options,
            key=lambda n: (
                self.interfaces[n].cost,
                self.interfaces[n].threshold_dbm - self.levels[n],
            ),
            default=None,
        )

    def _cheaper(self, t: float) -> str | None:
        """A cheaper interface than the active one that has been healthy for return_after_s."""
        best = self._best_alternative()
        if best is None or self.interfaces[best].cost >= self.interfaces[self.active].cost:
            return None
        return best if t - self.healthy_since[best] >= self.return_after_s else None

    async def step(self, t: float, link_alive: bool) -> None:
        """One decision at scenario time t. `link_alive`: has a ping been answered recently?"""
        for name, iface in self.interfaces.items():
            self.levels[name] = iface.signal.sample(t)
            was_warning = self.hints.get(name) is not None
            self.hints[name] = self.predictors[name].update(t, self.levels[name])
            if not self._healthy(name):
                self.healthy_since.pop(name, None)
            else:
                self.healthy_since.setdefault(name, t)
            if (self.hints[name] is not None) != was_warning:
                hint = self.hints[name]
                self.log.emit(
                    "warning", t=t, iface=name, on=not was_warning,
                    eta_s=hint.eta_s if hint is not None else None,
                )  # fmt: skip
        if t >= self._next_signal_log:
            self.log.emit(
                "signal", t=t, active=self.active,
                dbm={n: round(v, 1) for n, v in self.levels.items()},
            )  # fmt: skip
            self._next_signal_log = t + self.signal_log_s

        warned = self.hints[self.active] is not None
        best = self._best_alternative()
        if not link_alive and best is not None:
            await self._switch(t, best, "reactive")
        elif warned and self.proactive and best is not None:
            await self._switch(t, best, "proactive")
        elif link_alive and (cheaper := self._cheaper(t)) is not None:
            await self._switch(t, cheaper, "cheaper")

        warned = self.hints[self.active] is not None  # the active interface may have changed
        outage_coming = (warned or not link_alive) and self._best_alternative() is None
        if self.send_hints and outage_coming != self.hint_sent:
            await self._send_hint(t, outage_coming)
        if self.send_hints and self.network_sent != self.interfaces[self.active].kind:
            await self._send_network(t)

    async def _switch(self, t: float, to: str, reason: str) -> None:
        old = self.active
        self.log.emit("path_switch", t=t, frm=old, to=to, reason=reason)
        self.active = to
        self.switches.append((t, old, to, reason))
        self.last_pong = time.monotonic()  # give the new path a full dead_after_s to answer
        try:
            await asyncio.wait_for(
                self.transport.migrate((self.interfaces[to].local_ip, 0)), self.dead_after_s * 3
            )
        except (OSError, TimeoutError) as e:
            self.log.emit("path_switch_error", t=t, to=to, error=type(e).__name__)

    async def _send_hint(self, t: float, active: bool) -> None:
        hint = self.hints[self.active]
        headers = {
            "active": active,
            "eta_s": hint.eta_s if hint is not None else 0.0,
            "outage_s": self.expected_outage_s if active else 0.0,
        }
        try:
            await asyncio.wait_for(self.transport.send(Frame(MsgType.HANDOVER_HINT, headers)), 1.0)
        except (OSError, TimeoutError):
            return  # link already gone; try again next tick
        self.hint_sent = active
        self.log.emit("hint_sent", t=t, **headers)

    async def _send_network(self, t: float) -> None:
        kind = self.interfaces[self.active].kind
        try:
            await asyncio.wait_for(self.transport.send(Frame(MsgType.NETWORK, {"kind": kind})), 1.0)
        except (OSError, TimeoutError):
            return  # try again next tick
        self.network_sent = kind
        self.log.emit("network_sent", t=t, kind=kind)

    # ---------------------------------------------------------------------------------- loops

    async def _pinger(self) -> None:
        while True:
            iface, sent = self.active, time.monotonic()
            try:
                await asyncio.wait_for(
                    self.transport.request(Frame(MsgType.PING)), self.dead_after_s
                )
                self.last_pong = time.monotonic()
                rtt_ms = (self.last_pong - sent) * 1000
            except (OSError, TimeoutError):
                rtt_ms = None  # unanswered within dead_after_s
            self.log.emit("ping", t=sent - self._t0, iface=iface, rtt_ms=rtt_ms)
            await asyncio.sleep(self.ping_interval_s)

    async def run(self, duration_s: float) -> None:
        """Run for `duration_s` seconds of scenario time, starting now."""
        start = self._t0 = time.monotonic()
        pinger = asyncio.ensure_future(self._pinger())
        try:
            while (t := time.monotonic() - start) < duration_s:
                alive = time.monotonic() - self.last_pong < self.dead_after_s
                await self.step(t, alive)
                await asyncio.sleep(self.tick_s)
        finally:
            pinger.cancel()
