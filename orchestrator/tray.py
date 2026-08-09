"""The tray icon: proof that the orchestrator is running, and a way to steer it.

Without this, the orchestrator is a hidden process with no window, which is
indistinguishable from a crashed one until a tool call fails. The icon's colour
carries the whole health summary, so a glance is enough:

    green   every managed server is up
    amber   something is stopped or still starting
    red     something crashed, or is alive but not listening
    grey    nothing is managed yet

The menu is rebuilt each time it opens, so it always shows current state rather
than whatever was true when the icon was created.
"""

from __future__ import annotations

import os
import threading
import webbrowser
from typing import Any, Callable

import pystray
from PIL import Image, ImageDraw

from orchestrator import config, monitor, paths, restart, supervise

REFRESH_SECONDS = 5
SIZE = 64

COLOURS = {
    "ok": (34, 197, 94),
    "busy": (245, 158, 11),
    "bad": (239, 68, 68),
    "idle": (156, 163, 175),
}

# What each server state does to the icon. Anything not listed counts as bad,
# so a state added later shows up as a problem rather than being silently
# treated as healthy.
GOOD_STATES = {"running", "external"}
BUSY_STATES = {"starting", "stopped"}

MARKERS = {
    "running": "*",
    "external": "~",
    "starting": ".",
    "stopped": "-",
    "crashed": "!",
    "unhealthy": "!",
}


def _health(states: list[dict[str, Any]]) -> str:
    if not states:
        return "idle"
    if any(s["state"] not in GOOD_STATES | BUSY_STATES for s in states):
        return "bad"
    if any(s["state"] in BUSY_STATES for s in states):
        return "busy"
    return "ok"


def _image(health: str) -> Image.Image:
    """A filled circle with a light ring, so it reads on a dark or light taskbar."""
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, SIZE - 4, SIZE - 4), fill=COLOURS[health] + (255,),
                 outline=(255, 255, 255, 220), width=4)
    if health == "bad":
        # An exclamation cut out of the circle: colour alone is no good to
        # someone who cannot distinguish red from green.
        draw.rectangle((SIZE // 2 - 4, 16, SIZE // 2 + 4, 38), fill=(255, 255, 255, 255))
        draw.rectangle((SIZE // 2 - 4, 43, SIZE // 2 + 4, 51), fill=(255, 255, 255, 255))
    return image


def _tooltip(states: list[dict[str, Any]], port: int) -> str:
    """Windows truncates tooltips hard, so lead with the number that matters."""
    if not states:
        return f"MCP Orchestrator :{port} - no servers managed"
    # "external" counts as up: the port answers, we just did not start it.
    # Counting only our own would report a working server as down.
    up = sum(1 for s in states if s["state"] in GOOD_STATES)
    broken = [s["name"] for s in states if s["state"] in ("crashed", "unhealthy")]
    crashes = sum(int(s.get("crashes") or 0) for s in states)
    line = f"MCP Orchestrator :{port} - {up}/{len(states)} up"
    if crashes:
        line += f", {crashes} crash{'es' if crashes != 1 else ''}"
    if broken:
        line += f" - PROBLEM: {', '.join(broken[:3])}"
    return line[:127]


class Tray:
    """Owns the icon and the refresh loop."""

    def __init__(self, port: int, on_quit: Callable[[], None] | None = None) -> None:
        self.port = port
        self.on_quit = on_quit
        self._stop = threading.Event()
        self._health = "idle"
        self.icon = pystray.Icon(
            "mcp-orchestrator",
            icon=_image("idle"),
            title=f"MCP Orchestrator :{port}",
            menu=pystray.Menu(self._build_menu),
        )

    # --- actions ---------------------------------------------------------

    def _background(self, work: Callable[[], Any]) -> None:
        """Run a menu action off the UI thread.

        Starting a server waits for its port, which can take seconds. Doing that
        inline freezes the tray menu and looks like the orchestrator has hung --
        while it is in fact doing exactly what was asked.
        """

        def wrapped() -> None:
            try:
                work()
            except Exception:  # noqa: BLE001 -- a menu click must not kill the tray
                pass
            self.refresh()

        threading.Thread(target=wrapped, daemon=True).start()

    def _server_items(self, spec_name: str) -> pystray.Menu:
        def act(fn: Callable[[Any], Any]) -> Callable[[], None]:
            def handler(icon: Any = None, item: Any = None) -> None:
                spec = config.get(spec_name)
                if spec:
                    self._background(lambda: fn(spec))

            return handler

        return pystray.Menu(
            pystray.MenuItem("Start", act(supervise.start)),
            pystray.MenuItem("Stop", act(supervise.stop)),
            pystray.MenuItem("Restart", act(supervise.restart)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open log", lambda i, t: _open(paths.log_file(spec_name))),
        )

    def _build_menu(self):
        """Rebuilt on every open: pystray calls this each time the menu is shown."""
        specs = config.load()
        states = {s["name"]: s for s in supervise.status_all(specs)}

        yield pystray.MenuItem(f"Orchestrator on :{self.port}", None, enabled=False)
        yield pystray.Menu.SEPARATOR

        if not specs:
            yield pystray.MenuItem("No servers managed yet", None, enabled=False)
        for spec in specs:
            state = states.get(spec.name, {}).get("state", "?")
            marker = MARKERS.get(state, "?")
            label = f"{marker} {spec.name} ({state})"
            if spec.port:
                label += f" :{spec.port}"
            yield pystray.MenuItem(label, self._server_items(spec.name))

        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(
            "Start all (autostart set)",
            lambda i, t: self._background(lambda: supervise.autostart_all(config.load())),
        )
        yield pystray.MenuItem(
            "Stop all",
            lambda i, t: self._background(lambda: supervise.stop_all(config.load())),
        )
        yield pystray.MenuItem(
            "Restart Claude sessions",
            lambda i, t: restart.request("requested from the tray"),
        )
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Open logs folder", lambda i, t: _open(paths.logs_dir()))
        yield pystray.MenuItem("Open servers.json", lambda i, t: _open(paths.config_file()))
        yield pystray.MenuItem("Refresh", lambda i, t: self.refresh())
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Quit (servers keep running)", self._quit)

    def _quit(self, icon: Any = None, item: Any = None) -> None:
        self._stop.set()
        if self.on_quit:
            try:
                self.on_quit()
            except Exception:  # noqa: BLE001
                pass
        self.icon.stop()

    # --- refresh ---------------------------------------------------------

    def refresh(self) -> None:
        try:
            states = supervise.status_all(config.load())
        except Exception:  # noqa: BLE001 -- never let a bad read kill the icon
            return
        health = _health(states)
        title = _tooltip(states, self.port)
        if health != self._health:
            self._health = health
            self.icon.icon = _image(health)
        if self.icon.title != title:
            self.icon.title = title

    def _loop(self) -> None:
        while not self._stop.wait(REFRESH_SECONDS):
            self.refresh()

    def _setup(self, icon: Any) -> None:
        """Called by pystray once the icon exists; before this it is not shown."""
        icon.visible = True
        # Give the supervisor a way to reach the user. A server quietly
        # exhausting its restarts is worth interrupting for; the tray colour
        # alone only helps someone already looking at the tray.
        monitor.notifier = self._notify
        self.refresh()

    def _notify(self, message: str, title: str) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:  # noqa: BLE001 -- notifications are best effort
            pass

    def run(self) -> None:
        """Blocks. pystray's Windows backend must own the main thread."""
        threading.Thread(target=self._loop, daemon=True).start()
        self.icon.run(setup=self._setup)


def _open(path: Any) -> None:
    """Open a file or folder in Explorer, or fall back to the browser."""
    try:
        os.startfile(str(path))  # noqa: S606 -- opening the user's own log
    except (OSError, AttributeError):
        webbrowser.open(f"file://{path}")


def run(port: int = 8768, on_quit: Callable[[], None] | None = None) -> None:
    Tray(port, on_quit).run()
