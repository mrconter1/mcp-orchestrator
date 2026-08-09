"""Sampling the managed servers, so their stats describe more than this instant.

Two jobs. The first is bookkeeping: uptime and availability only mean anything
if somebody is looking regularly, and counting a crash the moment it happens
rather than whenever a tool is next called.

The second is that this is what makes the orchestrator honest when it runs
headless. Crash detection lives in ``supervise.status``, which only runs when
something asks. With the tray open, the tray asks. Without it, nothing would --
and a server could be dead for hours before anyone noticed.
"""

from __future__ import annotations

import datetime
import threading
import time
from typing import Any

from orchestrator import config, paths, supervise

SAMPLE_SECONDS = 10


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def sample() -> list[dict[str, Any]]:
    """Take one reading of every managed server and fold it into the stats."""
    specs = config.load()
    states = supervise.status_all(specs)  # this is also what detects a crash
    data = supervise.runtime_state()
    now = time.time()

    for state in states:
        entry = dict(data.get(state["name"], {}))
        up = state["state"] in ("running", "external")

        entry["samples"] = int(entry.get("samples", 0)) + 1
        entry["samples_up"] = int(entry.get("samples_up", 0)) + (1 if up else 0)
        entry.setdefault("watching_since", _now())

        # Accumulate runtime from the gap between samples rather than from the
        # current uptime, so the total survives restarts instead of resetting
        # with each new pid.
        last = entry.get("last_sample_epoch")
        if up and last is not None:
            elapsed = now - float(last)
            if 0 < elapsed < SAMPLE_SECONDS * 5:  # ignore gaps from a sleeping machine
                entry["total_runtime_seconds"] = round(
                    float(entry.get("total_runtime_seconds", 0)) + elapsed, 1
                )
        entry["last_sample_epoch"] = now
        if up:
            entry["last_seen_up"] = _now()

        data[state["name"]] = entry

    supervise.save_runtime_state(data)
    return states


def stats() -> dict[str, Any]:
    """Per-server statistics, plus the totals worth seeing at a glance."""
    specs = config.load()
    states = {s["name"]: s for s in supervise.status_all(specs)}
    data = paths.read_json(paths.running_file(), {})
    if not isinstance(data, dict):
        data = {}

    servers = []
    for spec in specs:
        state = states.get(spec.name, {})
        entry = data.get(spec.name, {})
        samples = int(entry.get("samples", 0))
        up = int(entry.get("samples_up", 0))
        servers.append(
            {
                "name": spec.name,
                "state": state.get("state"),
                "port": spec.port,
                "url": spec.url,
                "pid": state.get("pid"),
                "uptime_seconds": state.get("uptime_seconds"),
                "started": state.get("started"),
                "starts": entry.get("starts", 0),
                "crashes": entry.get("crashes", 0),
                "availability_percent": round(100 * up / samples, 1) if samples else None,
                "samples": samples,
                "watching_since": entry.get("watching_since"),
                "total_runtime_seconds": entry.get("total_runtime_seconds", 0),
                "last_seen_up": entry.get("last_seen_up"),
                "memory_mb": state.get("memory_mb"),
                "cpu_percent": state.get("cpu_percent"),
                "probe_ms": state.get("probe_ms"),
                "last_error": entry.get("last_error"),
                "last_exit": entry.get("last_exit"),
                "autostart": spec.autostart,
                "enabled": spec.enabled,
            }
        )

    return {
        "servers": servers,
        "managed": len(servers),
        "up": sum(1 for s in servers if s["state"] in ("running", "external")),
        "problems": [s["name"] for s in servers if s["state"] in ("crashed", "unhealthy")],
        "total_crashes": sum(int(s["crashes"] or 0) for s in servers),
        "sample_interval_seconds": SAMPLE_SECONDS,
    }


class Monitor:
    """The sampling thread."""

    def __init__(self, interval: float = SAMPLE_SECONDS) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sample()
            except Exception:  # noqa: BLE001 -- the watcher must outlive what it watches
                pass
            self._stop.wait(self.interval)

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
