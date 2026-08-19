"""Starting, stopping and observing the managed servers.

The hard requirement from the design is that a hidden server must never be
*silently* dead: with no console window, a crashed server looks exactly like a
missing tool. So nothing here reports success from a pid alone. A start waits
for the port to answer, a status combines pid liveness with a port probe, and
every stdout and stderr byte lands in a log file.

Runtime state lives in ``running.json`` so a restarted orchestrator re-adopts
the servers it started last time instead of orphaning them or starting seconds.
"""

from __future__ import annotations

import datetime
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from orchestrator import paths
from orchestrator.config import ServerSpec

# A genuinely headless child. DETACHED_PROCESS would give a console app no
# console at all, so it starts, hands back a pid and dies invisibly -- the exact
# failure this module exists to prevent. CREATE_NEW_PROCESS_GROUP keeps a Ctrl+C
# in the orchestrator's own console from taking every server down with it.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
HIDDEN = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

# Markers Claude Code exports inside a live session. The orchestrator is often
# started from one during development, and a child that inherits these treats
# itself as a Claude child session.
SESSION_ENV_MARKERS = (
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_PID",
    "CLAUDECODE",
)

START_TIMEOUT = 20.0  # seconds to wait for a freshly started port to answer
STOP_TIMEOUT = 8.0
PROBE_TIMEOUT = 0.35
LOG_MAX_BYTES = 5 * 1024 * 1024


class SuperviseError(Exception):
    """Raised when a server cannot be started or stopped as asked."""


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _clean_env(spec: ServerSpec) -> dict[str, str]:
    env = os.environ.copy()
    for key in SESSION_ENV_MARKERS:
        env.pop(key, None)
    # Redirected to a file, a Python child block-buffers its output, so the log
    # stays empty for kilobytes at a time -- and empty is precisely how a hidden
    # server looks when it has crashed. Flush as it goes. A server that wants
    # the buffering back can set it in its own env block.
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.update(spec.env)
    return env


# --- runtime state -------------------------------------------------------


def _load_running() -> dict[str, dict[str, Any]]:
    data = paths.read_json(paths.running_file(), {})
    return data if isinstance(data, dict) else {}


def _save_running(data: dict[str, dict[str, Any]]) -> None:
    paths.write_json(paths.running_file(), data)


def _entry(name: str) -> dict[str, Any]:
    return _load_running().get(name, {})


def runtime_state() -> dict[str, dict[str, Any]]:
    """The raw per-server runtime record, for the stats layer to fold into."""
    return _load_running()


def save_runtime_state(data: dict[str, dict[str, Any]]) -> None:
    _save_running(data)


def _update_entry(name: str, **changes: Any) -> dict[str, Any]:
    data = _load_running()
    entry = dict(data.get(name, {}))
    entry.update(changes)
    data[name] = entry
    _save_running(data)
    return entry


# --- liveness ------------------------------------------------------------


def port_open(port: int | None, timeout: float = PROBE_TIMEOUT) -> bool:
    """Is something accepting connections on this local port?"""
    if not port:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def probe(port: int | None, timeout: float = PROBE_TIMEOUT) -> tuple[bool, float | None]:
    """Port probe with the round trip in milliseconds, for the stats view."""
    if not port:
        return False, None
    started = time.perf_counter()
    open_ = port_open(port, timeout)
    if not open_:
        return False, None
    return True, round((time.perf_counter() - started) * 1000, 1)


def _live_process(entry: dict[str, Any]) -> psutil.Process | None:
    """The recorded process, if it is still the same process.

    Windows reuses pids freely, so a bare ``pid in psutil.pids()`` will
    eventually report some unrelated program as a healthy MCP server. The
    creation time recorded at spawn is what makes the identity check honest.
    """
    pid = entry.get("pid")
    if not pid:
        return None
    try:
        proc = psutil.Process(int(pid))
        create_time = proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
        return None
    recorded = entry.get("create_time")
    if recorded is not None and abs(create_time - float(recorded)) > 1.0:
        return None  # pid was reused by something else
    return proc


# psutil measures CPU as the change between two reads of the *same* Process
# object, so a fresh object always answers 0.0. Keeping them alive between polls
# is what turns cpu_percent into a real number instead of a permanent zero.
_proc_cache: dict[int, psutil.Process] = {}


def _cached(pid: int) -> psutil.Process:
    proc = _proc_cache.get(pid)
    if proc is None:
        proc = psutil.Process(pid)
        proc.cpu_percent(interval=None)  # prime it; this first read is the 0.0
        _proc_cache[pid] = proc
    return proc


def _tree_resources(proc: psutil.Process) -> tuple[float | None, float | None, int]:
    """Memory and CPU for a server, summed over its whole process tree.

    The command we launch is usually a thin launcher that re-executes the real
    interpreter, and it is the *child* that holds the listening socket and does
    the work. Measuring only the tracked pid reported every server as an
    identical 4 MB, which is the launcher, not the server.
    """
    try:
        members = [proc, *proc.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None, None, 0

    memory = 0.0
    cpu = 0.0
    counted = 0
    for member in members:
        try:
            tracked = _cached(member.pid)
            with tracked.oneshot():
                memory += tracked.memory_info().rss
                cpu += tracked.cpu_percent(interval=None)
            counted += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            _proc_cache.pop(member.pid, None)
            continue

    if not counted:
        return None, None, 0
    # Drop cache entries for processes that have since died, so a long-running
    # orchestrator does not accumulate one per restarted server forever.
    if len(_proc_cache) > 64:
        for pid in [p for p, c in _proc_cache.items() if not c.is_running()]:
            del _proc_cache[pid]
    return round(memory / (1024 * 1024), 1), round(cpu, 1), counted


def _note_crash(name: str) -> dict[str, Any]:
    """Record that a server we started died by itself.

    Idempotent: the pid is cleared so the count cannot climb on every poll, and
    ``crashed_at`` is what keeps the state readable as ``crashed`` afterwards
    instead of decaying into an innocent-looking ``stopped``. Both ``start`` and
    ``stop`` clear it, so the flag only ever describes the latest death.
    """
    entry = _entry(name)
    return _update_entry(
        name,
        pid=None,
        create_time=None,
        crashed_at=_now(),
        crashes=int(entry.get("crashes") or 0) + 1,
        last_exit={"code": "vanished", "at": _now()},
        last_error=entry.get("last_error") or "process exited on its own",
    )


def status(spec: ServerSpec) -> dict[str, Any]:
    """Combined view of one server: what we started, and what is actually up.

    The distinction between states matters operationally. ``unhealthy`` means
    the process is alive but not listening, which is a crash loop or a bad
    config. ``crashed`` means we started it and it died on its own, which must
    never be reported as the same thing as ``stopped`` -- a hidden server has no
    window to die in, so this state is the only evidence the user gets.
    ``external`` means the port answers but we did not start it, which is the
    case when the user runs a server by hand from a terminal.
    """
    entry = _entry(spec.name)
    proc = _live_process(entry)
    listening, latency = probe(spec.port)

    if entry.get("pid") and proc is None:
        # Recorded as ours, but the process is gone and nothing here stopped it.
        entry = _note_crash(spec.name)

    if proc and listening:
        state = "running"
    elif proc and not listening:
        started_at = entry.get("started_epoch")
        warming = started_at is not None and (time.time() - float(started_at)) < START_TIMEOUT
        state = "starting" if warming else "unhealthy"
    elif listening:
        state = "external"
    elif entry.get("crashed_at"):
        state = "crashed"
    else:
        state = "stopped"

    info: dict[str, Any] = {
        "name": spec.name,
        "state": state,
        "listening": listening,
        "probe_ms": latency,
        "pid": proc.pid if proc else None,
        "port": spec.port,
        "url": spec.url,
        "autostart": spec.autostart,
        "enabled": spec.enabled,
        "started": entry.get("started"),
        "crashed_at": entry.get("crashed_at"),
        "uptime_seconds": None,
        "starts": entry.get("starts") or 0,
        "crashes": entry.get("crashes") or 0,
        "last_error": entry.get("last_error"),
        "last_exit": entry.get("last_exit"),
        "log": str(paths.log_file(spec.name)),
    }
    if proc:
        try:
            info["uptime_seconds"] = int(time.time() - proc.create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        memory, cpu, count = _tree_resources(proc)
        info["memory_mb"] = memory
        info["cpu_percent"] = cpu
        info["processes"] = count
    return info


def status_all(specs: list[ServerSpec]) -> list[dict[str, Any]]:
    return [status(spec) for spec in specs]


# --- logs ----------------------------------------------------------------


def _open_log(name: str) -> Any:
    """Append-mode log handle, rotating once the file gets unwieldy."""
    path = paths.log_file(name)
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            previous = path.with_suffix(".log.1")
            previous.unlink(missing_ok=True)
            path.rename(previous)
    except OSError:
        pass  # rotation is a nicety; never block a start on it
    return open(path, "a", encoding="utf-8", errors="replace", buffering=1)


def read_log(name: str, tail: int = 80) -> dict[str, Any]:
    path = paths.log_file(name)
    if not path.exists():
        return {"path": str(path), "exists": False, "lines": []}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"path": str(path), "exists": True, "error": str(exc), "lines": []}
    lines = content.splitlines()
    return {
        "path": str(path),
        "exists": True,
        "total_lines": len(lines),
        "lines": lines[-max(1, tail):],
    }


# --- start / stop --------------------------------------------------------


def start(spec: ServerSpec, wait: float = START_TIMEOUT) -> dict[str, Any]:
    """Start a server hidden, then wait for its port before calling it started."""
    if not spec.enabled:
        raise SuperviseError(f"{spec.name} is disabled in servers.json")

    current = status(spec)
    if current["state"] in ("running", "starting"):
        return {**current, "already_running": True}
    if current["state"] == "external":
        raise SuperviseError(
            f"port {spec.port} is already in use by a process this orchestrator did not "
            f"start; stop it by hand or give {spec.name} another port"
        )

    cwd = spec.cwd or str(paths.repo_root())
    if not Path(cwd).is_dir():
        raise SuperviseError(f"{spec.name}: cwd does not exist: {cwd}")
    executable = spec.command[0]
    if Path(executable).is_absolute() and not Path(executable).exists():
        raise SuperviseError(f"{spec.name}: command not found: {executable}")

    log = _open_log(spec.name)
    log.write(f"\n=== {_now()} starting {spec.name}: {' '.join(spec.command)} ===\n")
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv comes from the user's own config
            spec.command,
            cwd=cwd,
            env=_clean_env(spec),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=HIDDEN,
            close_fds=True,
        )
    except OSError as exc:
        log.write(f"=== {_now()} failed to spawn: {exc} ===\n")
        log.close()
        _update_entry(spec.name, last_error=str(exc), last_error_at=_now())
        raise SuperviseError(f"{spec.name}: {exc}") from exc
    finally:
        # Popen inherited its own duplicate of the handle; ours is dead weight.
        try:
            log.close()
        except OSError:
            pass

    try:
        create_time = psutil.Process(proc.pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        create_time = time.time()

    previous = _entry(spec.name)
    _update_entry(
        spec.name,
        pid=proc.pid,
        create_time=create_time,
        started=_now(),
        started_epoch=time.time(),
        port=spec.port,
        command=list(spec.command),
        cwd=cwd,
        starts=int(previous.get("starts") or 0) + 1,
        crashes=int(previous.get("crashes") or 0),
        crashed_at=None,
        stopped_by_user=False,
        last_error=None,
        last_error_at=None,
        last_exit=None,
    )

    healthy = _await_port(spec, proc, wait)
    result = status(spec)
    if not healthy:
        # Truthful failure: say what the log says rather than reporting a pid.
        tail = read_log(spec.name, 15)["lines"]
        detail = tail[-1] if tail else "no output"
        exited = proc.poll()
        message = (
            f"exited with code {exited}" if exited is not None
            else f"did not open port {spec.port} within {wait:.0f}s"
        )
        _update_entry(
            spec.name,
            last_error=f"{message}; last log line: {detail}",
            last_error_at=_now(),
            crashes=int(previous.get("crashes") or 0) + (1 if exited is not None else 0),
            last_exit={"code": exited, "at": _now()} if exited is not None else None,
        )
        result = status(spec)
        result["error"] = message
        result["log_tail"] = tail
    return result


def _await_port(spec: ServerSpec, proc: subprocess.Popen, wait: float) -> bool:
    """Poll until the port answers, the process dies, or time runs out."""
    if not spec.port:
        # Nothing to probe: give it a moment and report on liveness alone.
        time.sleep(0.5)
        return proc.poll() is None
    deadline = time.time() + wait
    while time.time() < deadline:
        if port_open(spec.port):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.25)
    return port_open(spec.port)


def stop(spec: ServerSpec, force: bool = False) -> dict[str, Any]:
    """Stop a server and its children, hardest last."""
    entry = _entry(spec.name)
    proc = _live_process(entry)
    if proc is None:
        if status(spec)["state"] == "external":
            raise SuperviseError(
                f"{spec.name}: port {spec.port} is held by a process this orchestrator "
                f"did not start; refusing to kill it"
            )
        _update_entry(spec.name, pid=None, create_time=None, stopped_by_user=True)
        return {**status(spec), "was_running": False}

    victims = []
    try:
        victims = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    victims.append(proc)

    for victim in victims:
        try:
            victim.kill() if force else victim.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    gone, alive = psutil.wait_procs(victims, timeout=STOP_TIMEOUT)
    for straggler in alive:
        try:
            straggler.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.wait_procs(alive, timeout=2)

    _update_entry(
        spec.name,
        pid=None,
        create_time=None,
        crashed_at=None,  # a deliberate stop is not a crash, and clears the last one
        # The monitor restarts a server that is merely down, so it needs to know
        # this one is down because somebody said so. Cleared again by start().
        stopped_by_user=True,
        stopped=_now(),
        last_exit={"code": "terminated", "at": _now()},
    )
    return {**status(spec), "was_running": True, "killed": len(gone) + len(alive), "forced": force}


def restart(spec: ServerSpec) -> dict[str, Any]:
    try:
        stop(spec)
    except SuperviseError:
        pass  # a server that was not running is a fine starting point
    time.sleep(0.4)  # let the listening socket clear before rebinding
    return start(spec)


# --- adoption and autostart ---------------------------------------------


def _normalise(command: list[str]) -> list[str]:
    return [str(part).replace("\\", "/").casefold() for part in command]


def _command_index() -> dict[tuple[str, ...], list[tuple[int, int | None, psutil.Process]]]:
    """One sweep of every process, keyed by command line.

    Built once per adoption pass rather than once per server: enumerating a few
    hundred processes with their command lines is the expensive part, and doing
    it per spec turned a cheap check into a visible cost every ten seconds.
    """
    index: dict[tuple[str, ...], list[tuple[int, int | None, psutil.Process]]] = {}
    for proc in psutil.process_iter(["pid", "ppid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline")
            if not cmdline:
                continue
            index.setdefault(tuple(_normalise(cmdline)), []).append(
                (proc.info["pid"], proc.info.get("ppid"), proc)
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            continue
    return index


def find_running(spec: ServerSpec, index: dict | None = None) -> psutil.Process | None:
    """A process already running this server's exact command, if there is one.

    This is how a server survives losing its runtime record -- an orchestrator
    that was killed rather than shut down, or a ``running.json`` that got
    truncated. Without it such a server reads as ``external``, which the monitor
    deliberately will not touch, so it is unsupervised for as long as it lives
    and stays dead the moment it falls over.

    The venv launcher re-executes the real interpreter with an identical command
    line, so a match is normally two processes deep. We want the outermost one,
    because ``stop`` kills the tree downwards from the pid it recorded.
    """
    index = _command_index() if index is None else index
    found = index.get(tuple(_normalise(spec.command)))
    if not found:
        return None
    pids = {pid for pid, _, _ in found}
    outermost = [entry for entry in found if entry[1] not in pids]
    return min(outermost or found, key=lambda entry: entry[0])[2]


def adopt_orphans(specs: list[ServerSpec], data: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """Claim any configured server that is running without us holding its record.

    Runs at startup *and* on every monitor pass, because the record can go
    missing at any time -- a hand-edited ``running.json``, a torn write, an
    orchestrator killed rather than shut down. An unclaimed server reads as
    ``external``, which is never supervised and never restarted, so leaving this
    to startup alone means a lost record quietly disarms the supervisor until
    somebody reboots.
    """
    save = data is None
    data = _load_running() if data is None else data
    unclaimed = [spec for spec in specs if not _live_process(data.get(spec.name, {}))]
    if not unclaimed:
        return []  # the healthy case: no process sweep at all

    index = _command_index()
    claimed: list[str] = []
    for spec in unclaimed:
        entry = dict(data.get(spec.name, {}))
        proc = find_running(spec, index)
        if proc is None:
            continue
        try:
            entry["create_time"] = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        entry["pid"] = proc.pid
        entry["starts"] = int(entry.get("starts") or 0)
        entry["crashes"] = int(entry.get("crashes") or 0)
        entry["crashed_at"] = None
        entry["stopped_by_user"] = False
        entry["adopted"] = _now()
        data[spec.name] = entry
        claimed.append(spec.name)
    if save and claimed:
        _save_running(data)
    return claimed


def reconcile(specs: list[ServerSpec]) -> dict[str, Any]:
    """Drop runtime state for processes that are no longer ours.

    Called at orchestrator startup: servers still alive from the previous run
    are adopted (their entry is left intact), and anything dead is recorded as a
    crash so the count in the stats view means something.
    """
    data = _load_running()
    adopted, lost = [], []
    known = {spec.name for spec in specs}
    for name in list(data):
        if name not in known:
            del data[name]
            continue
        entry = data[name]
        if not entry.get("pid"):
            continue
        if _live_process(entry):
            adopted.append(name)
        else:
            entry["pid"] = None
            entry["create_time"] = None
            entry["crashes"] = int(entry.get("crashes") or 0) + 1
            entry["crashed_at"] = _now()
            entry["last_exit"] = {"code": "vanished", "at": _now()}
            entry["last_error"] = "process was gone when the orchestrator next looked"
            lost.append(name)

    # Anything still running from an earlier orchestrator, whose record we no
    # longer hold: claim it by command line rather than leaving it unsupervised.
    for name in adopt_orphans(specs, data):
        if name not in adopted:
            adopted.append(name)

    _save_running(data)
    return {"adopted": adopted, "lost": lost}


def autostart_all(specs: list[ServerSpec]) -> list[dict[str, Any]]:
    """Start every enabled server marked autostart, reporting each outcome."""
    results = []
    for spec in specs:
        if not (spec.enabled and spec.autostart):
            continue
        try:
            results.append(start(spec))
        except SuperviseError as exc:
            results.append({"name": spec.name, "state": "error", "error": str(exc)})
    return results


def stop_all(specs: list[ServerSpec]) -> list[dict[str, Any]]:
    results = []
    for spec in specs:
        try:
            results.append(stop(spec))
        except SuperviseError as exc:
            results.append({"name": spec.name, "state": "error", "error": str(exc)})
    return results


def free_port(preferred: int | None = None, taken: set[int] | None = None) -> int:
    """An unused local port from the orchestrator's range."""
    from orchestrator.config import PORT_RANGE

    taken = taken or set()
    candidates = ([preferred] if preferred else []) + list(range(PORT_RANGE[0], PORT_RANGE[1] + 1))
    for port in candidates:
        if port in taken:
            continue
        if not port_open(port, timeout=0.15):
            return port
    raise SuperviseError(f"no free port in range {PORT_RANGE[0]}-{PORT_RANGE[1]}")
