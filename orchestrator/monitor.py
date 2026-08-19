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

# Backoff between restart attempts. Fixed ladder rather than a formula: five
# seconds is long enough for a port to clear, and by the fifth attempt a server
# that is still failing is not going to be fixed by trying harder.
BACKOFF_SECONDS = (5, 15, 45, 120, 300)
MAX_ATTEMPTS = len(BACKOFF_SECONDS)

# How long a server must stay up before its failures are forgiven. Without
# this, a server that crashes once a day would eventually exhaust its attempts
# and stay down, having been healthy for weeks in between.
STABLE_SECONDS = 120

# Set by the tray so crash-loop warnings can reach the user. Left as None when
# running headless, where the log is the only place to say it.
notifier: Any = None


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _notify(title: str, message: str) -> None:
    """Tell the user, by whatever channel exists."""
    try:
        from orchestrator import server as server_module

        server_module.log(f"{title}: {message}")
    except Exception:  # noqa: BLE001
        pass
    if notifier is not None:
        try:
            notifier(message, title)
        except Exception:  # noqa: BLE001 -- a failed toast must not stop supervision
            pass


def _recover(spec: Any, state: dict[str, Any], entry: dict[str, Any], now: float) -> dict[str, Any]:
    """Restart a server that died, or give up loudly.

    ``crashed`` and ``unhealthy`` are always recoverable. ``external`` is not
    ours to touch.

    ``stopped`` is the subtle one. It usually means somebody stopped the server
    on purpose, and restarting it would be the orchestrator arguing with the
    user. But it is also where a server lands when it is simply down and we hold
    no record of it -- and treating that as intent left three servers dead for
    hours with autostart and auto_restart both on. So intent is now recorded
    explicitly at the point it is expressed, by ``supervise.stop``, and only a
    server carrying that flag is left alone.
    """
    state_name = state["state"]
    deliberate = bool(entry.get("stopped_by_user"))
    recoverable = state_name in ("crashed", "unhealthy") or (
        state_name == "stopped" and not deliberate and spec.autostart
    )

    if not recoverable:
        # A sustained healthy run wipes the slate, including a previous give-up.
        if state_name == "running" and (state.get("uptime_seconds") or 0) >= STABLE_SECONDS:
            if entry.get("restart_attempts") or entry.get("gave_up"):
                entry["restart_attempts"] = 0
                entry["gave_up"] = False
                entry["next_retry_epoch"] = None
        return entry

    if not (spec.enabled and spec.auto_restart) or entry.get("gave_up"):
        return entry

    attempts = int(entry.get("restart_attempts") or 0)
    next_retry = entry.get("next_retry_epoch")
    if next_retry is not None and now < float(next_retry):
        return entry  # still serving the backoff

    if attempts >= MAX_ATTEMPTS:
        entry["gave_up"] = True
        entry["next_retry_epoch"] = None
        _notify(
            "MCP server keeps crashing",
            f"{spec.name} failed {attempts} restarts and will not be retried. "
            f"See mcp_logs('{spec.name}').",
        )
        return entry

    entry["restart_attempts"] = attempts + 1
    entry["next_retry_epoch"] = now + BACKOFF_SECONDS[min(attempts, MAX_ATTEMPTS - 1)]
    entry["last_auto_restart"] = _now()
    try:
        result = supervise.restart(spec)
        ok = result.get("state") == "running"
    except supervise.SuperviseError as exc:
        ok = False
        entry["last_error"] = str(exc)

    if ok:
        _notify(
            "MCP server restarted",
            f"{spec.name} was {state_name} and has been restarted automatically "
            f"(attempt {attempts + 1}).",
        )
    return entry


def sample() -> list[dict[str, Any]]:
    """Take one reading of every managed server and fold it into the stats."""
    specs = config.load()
    # Reclaim anything running that we have lost the record for, before reading
    # state -- otherwise it reads as `external` and is skipped by _recover, and
    # a server can end up unsupervised for as long as it happens to stay up.
    for name in supervise.adopt_orphans(specs):
        _notify("MCP server adopted", f"{name} was running unsupervised and is now tracked.")
    states = supervise.status_all(specs)  # this is also what detects a crash
    data = supervise.runtime_state()
    now = time.time()

    by_name = {spec.name: spec for spec in specs}
    for state in states:
        entry = dict(data.get(state["name"], {}))
        up = state["state"] in ("running", "external")

        entry["samples"] = int(entry.get("samples") or 0) + 1
        entry["samples_up"] = int(entry.get("samples_up") or 0) + (1 if up else 0)
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

        spec = by_name.get(state["name"])
        if spec is not None:
            entry = _recover(spec, state, entry, now)

        data[state["name"]] = entry

    # _recover restarts through supervise, which writes this same file. Merge
    # its updates rather than clobbering them with our older snapshot.
    fresh = supervise.runtime_state()
    for name, entry in data.items():
        current = fresh.get(name, {})
        merged = dict(current)
        merged.update(entry)
        # These keys belong to supervise, so its copy wins -- but only where it
        # actually has one. A bare .get() invents a null for every server
        # supervise has not started, and a null `starts` then makes the next
        # start die on int(None) while a null pid makes a dead server look
        # merely `stopped`, which is the one state the monitor will not fix.
        for key in (
            "pid", "create_time", "started", "started_epoch", "starts",
            "crashes", "crashed_at", "stopped_by_user", "adopted",
        ):
            if key in current:
                merged[key] = current[key]
        fresh[name] = merged
    supervise.save_runtime_state(fresh)
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
        samples = int(entry.get("samples") or 0)
        up = int(entry.get("samples_up") or 0)
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
                "processes": state.get("processes"),
                "probe_ms": state.get("probe_ms"),
                "last_error": entry.get("last_error"),
                "last_exit": entry.get("last_exit"),
                "autostart": spec.autostart,
                "enabled": spec.enabled,
                "auto_restart": spec.auto_restart,
                "restart_attempts": entry.get("restart_attempts", 0),
                "gave_up": bool(entry.get("gave_up")),
                "last_auto_restart": entry.get("last_auto_restart"),
            }
        )

    return {
        "servers": servers,
        "managed": len(servers),
        "up": sum(1 for s in servers if s["state"] in ("running", "external")),
        "problems": [s["name"] for s in servers if s["state"] in ("crashed", "unhealthy")],
        "total_crashes": sum(int(s["crashes"] or 0) for s in servers),
        "given_up": [s["name"] for s in servers if s["gave_up"]],
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
