"""Local handover/outage predictor from a signal-strength time series.

Runs on the client with no network calls, because the network is least reliable exactly when
this is needed. A least-squares slope over a sliding window is extrapolated to the point where
the signal crosses the usable threshold. Hysteresis stops the hint from flapping.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class HandoverHint:
    eta_s: float  # predicted seconds until the signal becomes unusable (0 = already)
    level: float  # smoothed current signal (dBm)
    slope: float  # dBm per second
    confidence: float  # R^2 of the linear fit, 0..1


@dataclass
class PredictorConfig:
    threshold_dbm: float = -110.0  # usable limit: ~-110 RSRP (cellular); use ~-80 for Wi-Fi RSSI
    window_s: float = 5.0  # slope is fitted over this window
    level_window_s: float = 1.0  # current level = mean over this shorter window
    horizon_s: float = 8.0  # raise a hint if the crossing is predicted within this many seconds
    min_slope: float = -0.3  # dBm/s; flatter trends are noise, not a fade
    min_confidence: float = 0.5
    clear_margin_db: float = 3.0  # hysteresis: clear only once back above threshold + margin
    min_samples: int = 5


class HandoverPredictor:
    def __init__(self, config: PredictorConfig | None = None) -> None:
        self.config = config or PredictorConfig()
        self._samples: deque[tuple[float, float]] = deque()
        self.active: HandoverHint | None = None

    def update(self, t: float, dbm: float) -> HandoverHint | None:
        """Feed one sample; returns the current hint (or None if no handover is expected)."""
        c = self.config
        self._samples.append((t, dbm))
        while self._samples and t - self._samples[0][0] > c.window_s:
            self._samples.popleft()
        if len(self._samples) < c.min_samples:
            return self.active

        slope, _, r2 = _fit(self._samples)
        recent = [v for ts, v in self._samples if t - ts <= c.level_window_s]
        level = sum(recent) / len(recent)
        if slope < 0:
            # A moving average lags a fade by half its window; correct only on the way down, so
            # fades are caught early but a step recovery can't overshoot and clear the hint.
            level += slope * c.level_window_s / 2

        if self.active is not None and level > c.threshold_dbm + c.clear_margin_db and slope >= 0:
            self.active = None
        if level <= c.threshold_dbm:
            self.active = HandoverHint(0.0, level, slope, r2)
        elif slope <= c.min_slope and r2 >= c.min_confidence:
            eta = (c.threshold_dbm - level) / slope
            if eta <= c.horizon_s:
                self.active = HandoverHint(eta, level, slope, r2)
        elif self.active is not None:
            # Keep the hint but refresh its numbers until hysteresis clears it.
            self.active = HandoverHint(self.active.eta_s, level, slope, r2)
        return self.active


def _fit(samples) -> tuple[float, float, float]:
    n = len(samples)
    mt = sum(t for t, _ in samples) / n
    my = sum(y for _, y in samples) / n
    sxx = sum((t - mt) ** 2 for t, _ in samples)
    sxy = sum((t - mt) * (y - my) for t, y in samples)
    syy = sum((y - my) ** 2 for _, y in samples)
    if sxx == 0:
        return 0.0, my, 0.0
    slope = sxy / sxx
    intercept = my - slope * mt
    r2 = (sxy * sxy) / (sxx * syy) if syy else 1.0
    return slope, intercept, r2
