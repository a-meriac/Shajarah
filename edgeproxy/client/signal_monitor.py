"""Signal-strength source for the handover predictor: replay of a scenario trace (emulation).

The trace is piecewise-linear between the scenario's (t, dbm) points. A live source reading
Wi-Fi RSSI (`iw dev <if> link`) / cellular RSRP (ModemManager `mmcli --signal-get`) can
implement the same `sample(t)` interface for the real-device demo.
"""

from __future__ import annotations

from bisect import bisect_right


class TraceSignal:
    def __init__(self, points: list[dict]) -> None:
        pts = sorted((float(p["t"]), float(p["dbm"])) for p in points)
        self._t = [t for t, _ in pts]
        self._v = [v for _, v in pts]

    def sample(self, t: float) -> float:
        i = bisect_right(self._t, t)
        if i == 0:
            return self._v[0]
        if i == len(self._t):
            return self._v[-1]
        t0, t1, v0, v1 = self._t[i - 1], self._t[i], self._v[i - 1], self._v[i]
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
