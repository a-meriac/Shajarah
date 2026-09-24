"""Structured JSONL event log. Every metric in the paper is computed from these files."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import IO


class EventLog:
    def __init__(self, path: Path | None, component: str, run_id: str = "") -> None:
        self.component = component
        self.run_id = run_id
        self._fh: IO[str] | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", buffering=1)  # line-buffered: survives a killed run
        self.events: list[dict] = []  # kept in memory too, for tests

    def emit(self, event: str, **fields) -> None:
        record = {
            "ts": time.time(),
            "mono": time.monotonic(),
            "run": self.run_id,
            "comp": self.component,
            "event": event,
            **fields,
        }
        self.events.append(record)
        if self._fh is not None:
            self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


NULL_LOG = EventLog(None, "null")
