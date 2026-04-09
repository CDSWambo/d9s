#!/usr/bin/env python3
"""d9s - A k9s-like TUI for Docker"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

try:
    import docker
    from docker.errors import DockerException, NotFound
except ImportError:
    print("Missing dependency: pip install docker textual")
    sys.exit(1)

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.coordinate import Coordinate
from textual.suggester import Suggester
from textual.widgets import DataTable, Input, Label, Log, Static


# ── helpers ───────────────────────────────────────────────────────────────────

D9S_LOGO = r"""[bold cyan] ___  ___
[bold cyan]|   \/ _ \ ___
[bold cyan]| |) \_, |(_-<
[bold cyan]|___/ /_/ /__/[/bold cyan]"""

D9S_LOGO_SMALL = "[bold cyan]d9s[/bold cyan]"


def _ago(ts: int | None) -> str:
    if ts is None:
        return "N/A"
    delta = int(datetime.now(timezone.utc).timestamp() - ts)
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _short(s: str, n: int = 12) -> str:
    return s[:n] if s else ""


def _bytes(n: int | float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"


def _state_markup(state: str) -> str:
    return {
        "running":    f"[green]{state}[/green]",
        "exited":     f"[red]{state}[/red]",
        "paused":     f"[yellow]{state}[/yellow]",
        "restarting": f"[cyan]{state}[/cyan]",
        "dead":       f"[dim red]{state}[/dim red]",
        "created":    f"[blue]{state}[/blue]",
    }.get(state, state)


def _parse_ts(raw: str) -> int | None:
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _cpu_pct(s: dict[str, Any]) -> float:
    try:
        cpu   = s["cpu_stats"]["cpu_usage"]["total_usage"]
        pre   = s["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_c = s["cpu_stats"]["system_cpu_usage"]
        sys_p = s["precpu_stats"]["system_cpu_usage"]
        n     = s["cpu_stats"].get("online_cpus") or len(
            s["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])
        )
        sd = sys_c - sys_p
        return ((cpu - pre) / sd) * n * 100.0 if sd > 0 else 0.0
    except (KeyError, ZeroDivisionError):
        return 0.0


def _docker_version_info() -> str:
    try:
        info = docker.from_env().version()
        return f"Docker {info.get('Version', '?')}"
    except Exception:
        return "Docker"


# ── Command suggester ─────────────────────────────────────────────────────────

COMMANDS = ["img", "images", "vol", "volumes", "net", "networks", "com", "compose", "q!", "up", "prune"]


class CommandSuggester(Suggester):
    """Autocomplete for command mode."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=False)

    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None
        # Path completion for ":up <path>"
        if value.startswith("up "):
            partial = os.path.expanduser(value[3:])
            if not partial:
                return None
            parent = os.path.dirname(partial) or "."
            prefix = os.path.basename(partial)
            try:
                for entry in sorted(os.listdir(parent)):
                    if entry.startswith(prefix) and entry != prefix:
                        full = os.path.join(parent, entry)
                        if os.path.isdir(full):
                            full += os.sep
                        return "up " + full
            except OSError:
                pass
            return None
        for cmd in COMMANDS:
            if cmd.startswith(value) and cmd != value:
                return cmd
        return None


class CommandInput(Input):
    """Input that uses Tab to accept suggestions and cycle path completions."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tab_matches: list[str] = []
        self._tab_index: int = 0
        self._tab_prefix: str = ""

    def _is_cycling(self, val: str) -> bool:
        """Check if current value is one of our cycle results."""
        return any(val == "up " + m for m in self._tab_matches)

    def on_key(self, event) -> None:
        if event.key != "tab":
            self._tab_matches = []
            return
        event.prevent_default()
        event.stop()
        val = self.value
        # Path cycling for ":up <path>"
        if val.startswith("up "):
            # If already cycling through matches, just advance
            if self._tab_matches and self._is_cycling(val):
                self.value = "up " + self._tab_matches[self._tab_index]
                self.cursor_position = len(self.value)
                self._tab_index = (self._tab_index + 1) % len(self._tab_matches)
                return
            # Build new match list from current input
            partial = os.path.expanduser(val[3:])
            if not partial:
                return
            parent = os.path.dirname(partial) or "."
            prefix = os.path.basename(partial)
            try:
                self._tab_matches = []
                for entry in sorted(os.listdir(parent)):
                    if entry.lower().startswith(prefix.lower()):
                        full = os.path.join(parent, entry)
                        if os.path.isdir(full):
                            full += os.sep
                        self._tab_matches.append(full)
            except OSError:
                self._tab_matches = []
            if self._tab_matches:
                self._tab_index = 0
                self.value = "up " + self._tab_matches[self._tab_index]
                self.cursor_position = len(self.value)
                self._tab_index = (self._tab_index + 1) % len(self._tab_matches)
        # Command name completion
        elif self._suggestion and self._suggestion != self.value:
            self.value = self._suggestion
            self.cursor_position = len(self.value)


# ── Fuzzy file finder (fzf-style) ────────────────────────────────────────────

class FileFinder(ModalScreen[str | None]):
    """fzf-like fuzzy file finder for compose files."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
    ]

    COMPOSE_NAMES = {"compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"}

    def __init__(self, root: str = ".") -> None:
        super().__init__()
        self._root = os.path.abspath(root)
        self._all_files: list[str] = []
        self._filtered: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="finder-box"):
            yield Input(placeholder="Type to filter compose files...", id="finder-input")
            yield DataTable(id="finder-tbl", cursor_type="row")

    def on_mount(self) -> None:
        tbl = self.query_one("#finder-tbl", DataTable)
        tbl.add_columns("File")
        self.query_one("#finder-input", Input).focus()
        self._scan()

    @work(thread=True)
    def _scan(self) -> None:
        """Walk directory tree and find compose files."""
        results = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            # Skip hidden dirs and common non-project dirs
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in (
                "node_modules", "__pycache__", ".git", "venv", ".venv",
            )]
            for f in filenames:
                if f in self.COMPOSE_NAMES:
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, self._root)
                    results.append(rel)
        self._all_files = sorted(results)
        self.app.call_from_thread(self._update_table, "")

    @on(Input.Changed, "#finder-input")
    def _on_filter(self, event: Input.Changed) -> None:
        self._update_table(event.value)

    def _update_table(self, query: str) -> None:
        tbl = self.query_one("#finder-tbl", DataTable)
        tbl.clear()
        q = query.lower()
        self._filtered = []
        for f in self._all_files:
            if not q or all(c in f.lower() for c in q):
                self._filtered.append(f)
                tbl.add_row(f, key=f)

    def on_key(self, event) -> None:
        # Forward arrow keys to table for navigation
        tbl = self.query_one("#finder-tbl", DataTable)
        if event.key in ("down", "up") and tbl.row_count > 0:
            if not tbl.has_focus:
                tbl.focus()

    def action_select(self) -> None:
        tbl = self.query_one("#finder-tbl", DataTable)
        if tbl.row_count == 0:
            return
        try:
            rk, _ = tbl.coordinate_to_cell_key(tbl.cursor_coordinate)
            full = os.path.join(self._root, rk.value)
            self.dismiss(full)
        except Exception:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Shared CSS ────────────────────────────────────────────────────────────────

K9S_CSS = """
Screen {
    background: #1a1a2e;
}

/* ── Logo / header area ── */
#header-bar {
    height: 5;
    background: #1a1a2e;
    padding: 0 1;
}
#logo {
    width: 18;
    height: 5;
    padding: 0 1;
}
#header-right {
    height: 5;
    padding: 0 0 0 1;
}
#header-info {
    height: 2;
    padding: 0 0 0 1;
}

/* ── Nav bar ── */
#nav-bar {
    height: 1;
    background: #0f3460;
    padding: 0 1;
    color: #e0e0e0;
}

/* ── Crumb bar ── */
#crumb-bar {
    height: 1;
    background: #0f3460;
    padding: 0 1;
    color: #e0e0e0;
}

/* ── Filter bar ── */
#filter-bar {
    height: 3;
    display: none;
    padding: 0 1;
    background: #16213e;
}
#filter-bar.visible {
    display: block;
}
#filter-input {
    background: #0f3460;
    color: #e0e0e0;
    border: none;
}

/* ── Command bar ── */
#cmd-bar {
    height: 4;
    display: none;
    padding: 0 1;
    background: #16213e;
}
#cmd-bar.visible {
    display: block;
}
#cmd-help {
    height: 1;
    color: #666666;
    padding: 0 0;
}
#cmd-input {
    background: #0f3460;
    color: #e0e0e0;
    border: none;
}

/* ── Fuzzy finder ── */
FileFinder {
    align: center middle;
}
#finder-box {
    width: 80%;
    height: 80%;
    background: #16213e;
    border: solid #0f3460;
    padding: 1 2;
}
#finder-input {
    background: #0f3460;
    color: #e0e0e0;
    border: none;
    margin-bottom: 1;
}
#finder-tbl {
    height: 1fr;
    background: #1a1a2e;
}

/* ── Table ── */
DataTable {
    height: 1fr;
    background: #1a1a2e;
}
DataTable > .datatable--header {
    background: #16213e;
    color: #00d4ff;
    text-style: bold;
}
DataTable > .datatable--cursor {
    background: #0f3460;
    color: #ffffff;
}
DataTable > .datatable--even-row {
    background: #1a1a2e;
}
DataTable > .datatable--odd-row {
    background: #1e1e3a;
}

/* ── Key hints bar (in header) ── */
#hints-bar {
    height: 3;
    padding: 0 0 0 1;
    color: #888888;
}

/* ── Sub-screen header ── */
#hdr {
    height: 1;
    background: #0f3460;
    padding: 0 1;
    color: #00d4ff;
}

/* ── Sub-screen body ── */
#body {
    padding: 1 2;
    height: 1fr;
    background: #1a1a2e;
}

/* ── Modal dialogs ── */
ConfirmScreen, ConfirmWithFlagsScreen {
    align: center middle;
}
#dialog {
    padding: 1 2;
    background: #16213e;
    border: solid #0f3460;
    width: 58;
    height: auto;
    max-height: 12;
}
#dialog Label {
    width: 100%;
    content-align: center middle;
    margin-bottom: 1;
    color: #e0e0e0;
}
#flags-display {
    width: 100%;
    content-align: center middle;
    height: 1;
    margin-bottom: 1;
    color: #e0e0e0;
}
#pwd-label {
    width: 100%;
    height: 1;
    color: #e0e0e0;
}
#pwd-display {
    width: 100%;
    height: 1;
    margin-bottom: 1;
    color: #e0e0e0;
}
#buttons {
    align: center middle;
    height: 3;
}
.btn {
    margin: 0 1;
}

HelpScreen {
    align: center middle;
}
#helpbox {
    padding: 1 3;
    background: #16213e;
    border: solid #0f3460;
    width: 66;
    height: auto;
    max-height: 90%;
    color: #e0e0e0;
}
#help-scroll {
    height: auto;
    max-height: 100%;
}
#help-filter {
    dock: bottom;
    height: 1;
    margin: 0;
    padding: 0 1;
    background: #0f3460;
    border: none;
    width: 100%;
}
#help-filter.hidden {
    display: none;
}

Log {
    border: none;
    background: #1a1a2e;
}
"""


# ── Confirm dialog ────────────────────────────────────────────────────────────

class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y,enter", "confirm", "Yes"), Binding("n,escape", "cancel", "No")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message)
            with Horizontal(id="buttons"):
                yield Static("\\[[bold green]y/⏎[/bold green]] Yes", classes="btn")
                yield Static("\\[[bold red]n[/bold red]] No",  classes="btn")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmWithFlagsScreen(ModalScreen[dict]):
    """Confirm dialog with toggleable flags (like k9s force delete with x)."""
    BINDINGS = [
        Binding("y,enter", "confirm", "Yes"),
        Binding("n,escape", "cancel", "No"),
    ]

    def __init__(self, message: str, flags: list[tuple[str, str, str]]) -> None:
        """flags: list of (key, name, description) e.g. [("x", "volumes", "Remove volumes")]"""
        super().__init__()
        self.message = message
        self._flags = {name: False for _, name, _ in flags}
        self._flag_keys = {key: name for key, name, _ in flags}
        self._flag_descs = {name: desc for _, name, desc in flags}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message)
            yield Static("", id="flags-display")
            with Horizontal(id="buttons"):
                yield Static("\\[[bold green]y/⏎[/bold green]] Yes", classes="btn")
                yield Static("\\[[bold red]n[/bold red]] No",  classes="btn")

    def on_mount(self) -> None:
        self._update_flags()

    def _update_flags(self) -> None:
        parts = []
        for key, name in self._flag_keys.items():
            on = self._flags[name]
            desc = self._flag_descs[name]
            if on:
                parts.append(f"\\[[bold yellow]{key}[/bold yellow]] {desc} [bold green]ON[/bold green]")
            else:
                parts.append(f"\\[[dim]{key}[/dim]] {desc} [dim]off[/dim]")
        self.query_one("#flags-display", Static).update("  ".join(parts))

    def on_key(self, event) -> None:
        if event.character in self._flag_keys:
            name = self._flag_keys[event.character]
            self._flags[name] = not self._flags[name]
            self._update_flags()
            event.prevent_default()

    def action_confirm(self) -> None:
        self.dismiss({"confirmed": True, **self._flags})

    def action_cancel(self) -> None:
        self.dismiss({"confirmed": False})


class SudoPruneScreen(ModalScreen[dict]):
    """Confirm dialog for sudo docker system prune --all with password input."""
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._force = False
        self._password: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[bold yellow]sudo docker system prune --all[/bold yellow]")
            yield Static("", id="flags-display")
            yield Static("[dim]sudo password:[/dim]", id="pwd-label")
            yield Static("", id="pwd-display")
            with Horizontal(id="buttons"):
                yield Static("\\[[bold green]⏎[/bold green]] Run", classes="btn")
                yield Static("\\[[bold red]esc[/bold red]] Cancel", classes="btn")

    def on_mount(self) -> None:
        self._update_flags()
        self._update_pwd()

    def _update_flags(self) -> None:
        if self._force:
            text = "\\[[bold yellow]x[/bold yellow]] --force [bold green]ON[/bold green]"
        else:
            text = "\\[[dim]x[/dim]] --force [dim]off[/dim]"
        self.query_one("#flags-display", Static).update(text)

    def _update_pwd(self) -> None:
        if self._password:
            self.query_one("#pwd-display", Static).update("[green]●[/green]")
        else:
            self.query_one("#pwd-display", Static).update("[dim]waiting for input...[/dim]")

    def on_key(self, event) -> None:
        event.prevent_default()
        event.stop()
        if event.key == "escape":
            self.action_cancel()
        elif event.key == "enter":
            self.dismiss({
                "confirmed": True,
                "password": "".join(self._password),
                "force": self._force,
            })
        elif event.key == "backspace":
            if self._password:
                self._password.pop()
                self._update_pwd()
        elif event.character == "x" and not self._password:
            self._force = not self._force
            self._update_flags()
        elif event.character and event.is_printable:
            self._password.append(event.character)
            self._update_pwd()

    def action_cancel(self) -> None:
        self.dismiss({"confirmed": False})


# ── Log screen ────────────────────────────────────────────────────────────────

class LogScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("c", "clear", "Clear"),
        Binding("end", "scroll_end", "Bottom"),
    ]

    def __init__(self, container_id: str, cname: str) -> None:
        super().__init__()
        self.container_id = container_id
        self.cname = cname
        self._stop = False

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold cyan]Logs[/bold cyan] [white]{self.cname}[/white]  "
            f"[dim]<esc>Back  <c>Clear  <end>Bottom[/dim]",
            id="hdr",
        )
        yield Log(id="out", highlight=True, auto_scroll=True)

    def on_mount(self) -> None:
        self._stream()

    @work(thread=True)
    def _stream(self) -> None:
        log = self.query_one("#out", Log)
        try:
            c = docker.from_env().containers.get(self.container_id)
            for line in c.logs(stream=True, follow=True, tail=300):
                if self._stop:
                    break
                self.app.call_from_thread(log.write_line, line.decode("utf-8", errors="replace").rstrip())
        except Exception as exc:
            self.app.call_from_thread(log.write_line, f"[red]{exc}[/red]")

    def action_clear(self) -> None:
        self.query_one("#out", Log).clear()

    def action_scroll_end(self) -> None:
        self.query_one("#out", Log).scroll_end()

    def on_unmount(self) -> None:
        self._stop = True


# ── Stats screen ──────────────────────────────────────────────────────────────

class StatsScreen(Screen):
    BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

    def __init__(self, container_id: str, cname: str) -> None:
        super().__init__()
        self.container_id = container_id
        self.cname = cname
        self._stop = False

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold cyan]Stats[/bold cyan] [white]{self.cname}[/white]  [dim]<esc>Back[/dim]",
            id="hdr",
        )
        with ScrollableContainer(id="body"):
            yield Static("Waiting for first sample...", id="content")

    def on_mount(self) -> None:
        self._poll()

    @work(thread=True)
    def _poll(self) -> None:
        content = self.query_one("#content", Static)
        try:
            c = docker.from_env().containers.get(self.container_id)
            for raw in c.stats(stream=True, decode=True):
                if self._stop:
                    break
                self.app.call_from_thread(self._render_stats, raw)
        except NotFound:
            self.app.call_from_thread(content.update, "[red]Container not found[/red]")
        except Exception as exc:
            self.app.call_from_thread(content.update, f"[red]{exc}[/red]")

    def _render_stats(self, s: dict[str, Any]) -> None:
        cpu = _cpu_pct(s)

        mem_u = s.get("memory_stats", {}).get("usage", 0)
        mem_l = s.get("memory_stats", {}).get("limit", 1) or 1
        mem_pct = mem_u / mem_l * 100

        net_in = net_out = 0
        for iface in (s.get("networks") or {}).values():
            net_in  += iface.get("rx_bytes", 0)
            net_out += iface.get("tx_bytes", 0)

        blk_r = blk_w = 0
        for entry in (s.get("blkio_stats", {}).get("io_service_bytes_recursive") or []):
            op = entry.get("op", "").lower()
            if op == "read":
                blk_r += entry.get("value", 0)
            elif op == "write":
                blk_w += entry.get("value", 0)

        pids = s.get("pids_stats", {}).get("current", "?")

        def bar(pct: float, w: int = 38) -> str:
            f = int(pct / 100 * w)
            col = "green" if pct < 60 else "yellow" if pct < 85 else "red"
            return f"[{col}]{'|' * f}[/{col}][dim]{'.' * (w - f)}[/dim] {pct:5.1f}%"

        self.query_one("#content", Static).update(
            f"[bold cyan]CPU[/bold cyan]\n  {bar(cpu)}\n\n"
            f"[bold cyan]Memory[/bold cyan]\n  {bar(mem_pct)}  ({_bytes(mem_u)} / {_bytes(mem_l)})\n\n"
            f"[bold cyan]Network I/O[/bold cyan]\n"
            f"  RX [green]{_bytes(net_in)}[/green]    TX [green]{_bytes(net_out)}[/green]\n\n"
            f"[bold cyan]Block I/O[/bold cyan]\n"
            f"  Read [green]{_bytes(blk_r)}[/green]    Write [green]{_bytes(blk_w)}[/green]\n\n"
            f"[bold cyan]PIDs[/bold cyan]  {pids}"
        )

    def on_unmount(self) -> None:
        self._stop = True


# ── Describe screen ───────────────────────────────────────────────────────────

class DescribeScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("y", "toggle_raw", "YAML/JSON"),
    ]

    def __init__(self, container_id: str, cname: str) -> None:
        super().__init__()
        self.container_id = container_id
        self.cname = cname
        self._raw = False
        self._attrs: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold cyan]Describe[/bold cyan] [white]{self.cname}[/white]  "
            f"[dim]<esc>Back  <y>Raw JSON[/dim]",
            id="hdr",
        )
        with ScrollableContainer(id="body"):
            yield Static("Loading...", id="content")

    def on_mount(self) -> None:
        self._fetch()

    @work(thread=True)
    def _fetch(self) -> None:
        try:
            self._attrs = docker.from_env().containers.get(self.container_id).attrs
            self.app.call_from_thread(self._render_describe)
        except Exception as exc:
            self.app.call_from_thread(self.query_one("#content", Static).update, f"[red]{exc}[/red]")

    def _render_describe(self) -> None:
        content = self.query_one("#content", Static)
        if self._raw:
            from textual.content import Content
            content.update(Content(json.dumps(self._attrs, indent=2, default=str)))
            return

        a   = self._attrs
        cfg = a.get("Config", {})
        hc  = a.get("HostConfig", {})
        net = a.get("NetworkSettings", {})
        st  = a.get("State", {})

        def _lines(items: list[str]) -> str:
            return "\n".join(f"    {x}" for x in items) if items else "    [dim]none[/dim]"

        mounts = _lines([
            f"[cyan]{m.get('Source','?')}[/cyan] -> {m.get('Destination','?')} "
            f"({'ro' if not m.get('RW', True) else 'rw'})"
            for m in a.get("Mounts", [])
        ])

        ports = _lines([
            f"{k} -> " + (
                ", ".join(f"{b['HostIp']}:{b['HostPort']}" for b in v)
                if v else "[dim]not published[/dim]"
            )
            for k, v in (net.get("Ports") or {}).items()
        ])

        networks = _lines([
            f"[cyan]{nm}[/cyan]  IP={info.get('IPAddress','?')}  GW={info.get('Gateway','?')}"
            for nm, info in (net.get("Networks") or {}).items()
        ])

        labels = _lines([f"{k}={v}" for k, v in (cfg.get("Labels") or {}).items()])
        env    = _lines(cfg.get("Env") or [])

        restart = hc.get("RestartPolicy", {})
        mem     = hc.get("Memory", 0)
        cpus    = hc.get("NanoCpus", 0)
        res     = (
            f"CPUs={cpus / 1e9 if cpus else 'unlimited'}  "
            f"Mem={_bytes(mem) if mem else 'unlimited'}"
        )

        self.query_one("#content", Static).update(
            f"[bold cyan]Name[/bold cyan]        {a.get('Name','').lstrip('/')}\n"
            f"[bold cyan]ID[/bold cyan]          {a.get('Id','')[:12]}\n"
            f"[bold cyan]Image[/bold cyan]       {cfg.get('Image','')}\n"
            f"[bold cyan]Created[/bold cyan]     {a.get('Created','')[:19]}\n"
            f"[bold cyan]Status[/bold cyan]      {_state_markup(st.get('Status',''))}\n"
            f"[bold cyan]Started[/bold cyan]     {st.get('StartedAt','')[:19]}\n"
            f"[bold cyan]Finished[/bold cyan]    {st.get('FinishedAt','')[:19]}\n"
            f"[bold cyan]PID[/bold cyan]         {st.get('Pid') or '[dim]n/a[/dim]'}\n"
            f"[bold cyan]Restart[/bold cyan]     {restart.get('Name','')} (max={restart.get('MaximumRetryCount',0)})\n"
            f"[bold cyan]Resources[/bold cyan]   {res}\n"
            f"[bold cyan]Cmd[/bold cyan]         {' '.join(cfg.get('Cmd') or []) or '[dim]none[/dim]'}\n"
            f"[bold cyan]Entrypoint[/bold cyan]  {' '.join(cfg.get('Entrypoint') or []) or '[dim]none[/dim]'}\n"
            f"[bold cyan]User[/bold cyan]        {cfg.get('User') or '[dim]root[/dim]'}\n"
            f"[bold cyan]WorkingDir[/bold cyan]  {cfg.get('WorkingDir') or '[dim]/[/dim]'}\n\n"
            f"[bold cyan]Ports[/bold cyan]\n{ports}\n\n"
            f"[bold cyan]Networks[/bold cyan]\n{networks}\n\n"
            f"[bold cyan]Mounts[/bold cyan]\n{mounts}\n\n"
            f"[bold cyan]Labels[/bold cyan]\n{labels}\n\n"
            f"[bold cyan]Env[/bold cyan]\n{env}"
        )

    def action_toggle_raw(self) -> None:
        self._raw = not self._raw
        self._render_describe()


# ── Generic inspect screen (images, volumes, networks) ───────────────────────

class InspectScreen(Screen):
    """Inspect any Docker resource — shows human-friendly view + raw JSON toggle."""

    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("y", "toggle_raw", "YAML/JSON"),
    ]

    def __init__(self, title: str, attrs: dict[str, Any]) -> None:
        super().__init__()
        self._title = title
        self._attrs = attrs
        self._raw = False

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold cyan]Inspect[/bold cyan] [white]{self._title}[/white]  "
            f"[dim]<esc>Back  <y>Raw JSON[/dim]",
            id="hdr",
        )
        with ScrollableContainer(id="body"):
            yield Static("", id="content")

    def on_mount(self) -> None:
        self._update_content()

    def _update_content(self) -> None:
        content = self.query_one("#content", Static)
        if self._raw:
            from textual.content import Content
            content.update(Content(json.dumps(self._attrs, indent=2, default=str)))
            return
        content.update(self._human_view())

    def _human_view(self) -> str:
        a = self._attrs
        lines: list[str] = []

        def kv(key: str, val: Any) -> None:
            lines.append(f"[bold cyan]{key}[/bold cyan]  {val}")

        def section(key: str, items: list[str]) -> None:
            lines.append(f"\n[bold cyan]{key}[/bold cyan]")
            if items:
                for i in items:
                    lines.append(f"    {i}")
            else:
                lines.append("    [dim]none[/dim]")

        # Common fields
        for field in ("Id", "Name", "Driver", "Scope", "Created", "CreatedAt"):
            if field in a:
                val = a[field]
                if field == "Id":
                    val = str(val)[:12]
                if field == "Name" and isinstance(val, str):
                    val = val.lstrip("/")
                kv(field, val)

        # Image-specific
        if "RepoTags" in a:
            section("Tags", a.get("RepoTags") or ["<none>"])
        if "RepoDigests" in a:
            section("Digests", [d[:72] for d in (a.get("RepoDigests") or [])])
        if "Size" in a:
            kv("Size", _bytes(a["Size"]))
        if "Architecture" in a:
            kv("Arch", f"{a.get('Os', '?')}/{a.get('Architecture', '?')}")
        if "RootFS" in a:
            layers = (a.get("RootFS") or {}).get("Layers") or []
            kv("Layers", str(len(layers)))

        # Volume-specific
        if "Mountpoint" in a:
            kv("Mountpoint", a["Mountpoint"])
        if "Options" in a and a["Options"]:
            section("Options", [f"{k}={v}" for k, v in (a["Options"] or {}).items()])

        # Network-specific
        ipam = a.get("IPAM", {})
        if ipam:
            configs = ipam.get("Config") or []
            section("IPAM", [
                f"Subnet={c.get('Subnet', '?')}  Gateway={c.get('Gateway', '?')}"
                for c in configs
            ] if configs else ["[dim]no config[/dim]"])
        containers = a.get("Containers")
        if containers:
            section("Containers", [
                f"[cyan]{cid[:12]}[/cyan]  {info.get('Name', '?')}  "
                f"IPv4={info.get('IPv4Address', '?')}"
                for cid, info in containers.items()
            ])

        # Compose-specific (docker compose config output)
        if "services" in a:
            kv("Name", a.get("name", "?"))
            services = a.get("services") or {}
            for svc_name, svc in services.items():
                lines.append(f"\n[bold yellow]Service: {svc_name}[/bold yellow]")
                img = svc.get("image", "")
                build = svc.get("build", "")
                if img:
                    lines.append(f"    [bold cyan]Image[/bold cyan]       {img}")
                if build:
                    ctx = build if isinstance(build, str) else build.get("context", "?")
                    lines.append(f"    [bold cyan]Build[/bold cyan]       {ctx}")
                if svc.get("ports"):
                    for p in svc["ports"]:
                        if isinstance(p, dict):
                            lines.append(f"    [bold cyan]Port[/bold cyan]        {p.get('published', '?')}:{p.get('target', '?')}/{p.get('protocol', 'tcp')}")
                        else:
                            lines.append(f"    [bold cyan]Port[/bold cyan]        {p}")
                if svc.get("volumes"):
                    for v in svc["volumes"]:
                        if isinstance(v, dict):
                            lines.append(f"    [bold cyan]Volume[/bold cyan]      {v.get('source', '?')} -> {v.get('target', '?')}")
                        else:
                            lines.append(f"    [bold cyan]Volume[/bold cyan]      {v}")
                if svc.get("environment"):
                    env = svc["environment"]
                    if isinstance(env, dict):
                        for k, v in env.items():
                            lines.append(f"    [bold cyan]Env[/bold cyan]         {k}={v}")
                    elif isinstance(env, list):
                        for e in env:
                            lines.append(f"    [bold cyan]Env[/bold cyan]         {e}")
                if svc.get("depends_on"):
                    deps = svc["depends_on"]
                    if isinstance(deps, dict):
                        deps = list(deps.keys())
                    lines.append(f"    [bold cyan]Depends on[/bold cyan]  {', '.join(deps)}")
                if svc.get("restart"):
                    lines.append(f"    [bold cyan]Restart[/bold cyan]     {svc['restart']}")
                if svc.get("networks"):
                    nets = svc["networks"]
                    if isinstance(nets, dict):
                        nets = list(nets.keys())
                    lines.append(f"    [bold cyan]Networks[/bold cyan]    {', '.join(nets)}")
            # Top-level volumes/networks
            if a.get("volumes"):
                section("Volumes", [f"{n}" for n in a["volumes"]])
            if a.get("networks"):
                section("Networks", [f"{n}" for n in a["networks"]])

        # raw_config fallback (no yaml module)
        if "raw_config" in a:
            lines.append(a["raw_config"])

        # Labels
        labels = a.get("Labels") or (a.get("Config") or {}).get("Labels") or {}
        if labels:
            section("Labels", [f"{k}={v}" for k, v in labels.items()])

        return "\n".join(lines)

    def action_toggle_raw(self) -> None:
        self._raw = not self._raw
        self._update_content()


# ── Shell exec ────────────────────────────────────────────────────────────────

# ── Base resource screen (k9s-style layout) ──────────────────────────────────

class K9sResourceScreen(Screen):
    """Base class for all resource screens with k9s-style layout."""

    resource_name: str = "Resources"
    hint_text: str = ""

    NAV_BINDINGS = [
        Binding("1", "nav_containers", "Containers", show=False),
        Binding("2", "nav_images",     "Images",     show=False),
        Binding("3", "nav_volumes",    "Volumes",    show=False),
        Binding("4", "nav_networks",   "Networks",   show=False),
        Binding("5", "nav_compose",    "Compose",    show=False),
    ]

    def _make_header_info(self) -> str:
        return (
            f"[bold white]Context:[/bold white] [cyan]docker-desktop[/cyan]\n"
            f"[bold white]Engine:[/bold white]  [cyan]{_docker_version_info()}[/cyan]"
        )

    def _compose_header(self) -> ComposeResult:
        with Horizontal(id="header-bar"):
            yield Static(D9S_LOGO, id="logo")
            with Vertical(id="header-right"):
                yield Static(self._make_header_info(), id="header-info")
                yield Static(self.hint_text, id="hints-bar")

    def _compose_nav_bar(self) -> ComposeResult:
        """Yield a navigation bar showing available views."""
        items = [
            ("1", "Containers", "ContainersScreen"),
            ("2", "Images",     "ImagesScreen"),
            ("3", "Volumes",    "VolumesScreen"),
            ("4", "Networks",   "NetworksScreen"),
            ("5", "Compose",    "ComposeScreen"),
        ]
        parts = []
        for key, label, cls_name in items:
            if type(self).__name__ == cls_name:
                parts.append(f"[bold reverse cyan] {key}:{label} [/bold reverse cyan]")
            else:
                parts.append(f"[dim] {key}:[/dim][cyan]{label}[/cyan]")
        yield Static(" ".join(parts), id="nav-bar")

    def _compose_crumb(self) -> ComposeResult:
        yield Static(
            f"[bold cyan]<{self.resource_name}>[/bold cyan]",
            id="crumb-bar",
        )

    def _nav_to(self, screen_cls: type) -> None:
        """Navigate to another resource screen, replacing this one."""
        if isinstance(self, screen_cls):
            return
        self.app.pop_screen()
        self.app.push_screen(screen_cls())

    def action_nav_containers(self) -> None:
        # Go back to containers (just pop, it's always at the bottom)
        if not isinstance(self, ContainersScreen):
            self.app.pop_screen()

    def action_nav_images(self) -> None:
        self._nav_to(ImagesScreen)

    def action_nav_volumes(self) -> None:
        self._nav_to(VolumesScreen)

    def action_nav_networks(self) -> None:
        self._nav_to(NetworksScreen)

    def action_nav_compose(self) -> None:
        self._nav_to(ComposeScreen)


# ── Images screen ─────────────────────────────────────────────────────────────

class ImagesScreen(K9sResourceScreen):
    resource_name = "Images"
    hint_text = (
        "[cyan]<enter/i>[/cyan]Inspect [cyan]<d>[/cyan]Delete [cyan]<p>[/cyan]Prune "
        "[cyan]<R>[/cyan]Refresh [cyan]<esc>[/cyan]Back"
    )

    BINDINGS = [
        *K9sResourceScreen.NAV_BINDINGS,
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("i",        "inspect",        "Inspect",       show=False),
        Binding("d",        "delete_image",   "Delete"),
        Binding("p",        "prune",          "Prune dangling"),
        Binding("R",        "refresh",        "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield from self._compose_header()
        yield from self._compose_nav_bar()
        yield from self._compose_crumb()
        yield DataTable(id="tbl", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one("#tbl", DataTable).add_columns("ID", "Repository", "Tag", "Size", "Created")
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        try:
            tbl = self.query_one("#tbl", DataTable)
            self.app.call_from_thread(tbl.clear)
            for img in docker.from_env().images.list():
                tags = img.tags
                repo, tag = (tags[0].rsplit(":", 1) if tags and ":" in tags[0] else (tags[0] if tags else "<none>", "latest" if tags else "<none>"))
                self.app.call_from_thread(
                    tbl.add_row,
                    _short(img.short_id.replace("sha256:", ""), 12),
                    repo[:42], tag,
                    _bytes(img.attrs.get("Size", 0)),
                    _ago(_parse_ts(img.attrs.get("Created", ""))),
                    key=img.id,
                )
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")

    def action_refresh(self) -> None:
        self.notify("Refreshing…", timeout=1)
        self._load()

    def action_inspect(self) -> None:
        tbl = self.query_one("#tbl", DataTable)
        if not tbl.row_count:
            return
        rk, _ = tbl.coordinate_to_cell_key(tbl.cursor_coordinate)
        self._do_inspect(rk.value)

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        self._do_inspect(event.row_key.value)

    def _do_inspect(self, image_id: str) -> None:
        try:
            img = docker.from_env().images.get(image_id)
            tags = ", ".join(img.tags) if img.tags else img.short_id
            self.app.push_screen(InspectScreen(tags, img.attrs))
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def action_delete_image(self) -> None:
        tbl = self.query_one("#tbl", DataTable)
        if not tbl.row_count:
            return
        rk, _ = tbl.coordinate_to_cell_key(tbl.cursor_coordinate)

        def _do(ok: bool) -> None:
            if ok:
                try:
                    docker.from_env().images.remove(rk.value, force=True)
                    self.notify("Image removed")
                    self._load()
                except Exception as exc:
                    self.notify(str(exc), severity="error")

        self.app.push_screen(ConfirmScreen("Remove this image?"), _do)

    def action_prune(self) -> None:
        def _do(ok: bool) -> None:
            if ok:
                try:
                    r = docker.from_env().images.prune()
                    self.notify(f"Pruned dangling images — freed {_bytes(r.get('SpaceReclaimed', 0))}")
                    self._load()
                except Exception as exc:
                    self.notify(str(exc), severity="error")

        self.app.push_screen(ConfirmScreen("Prune all [bold]dangling[/bold] images?"), _do)


# ── Volumes screen ────────────────────────────────────────────────────────────

class VolumesScreen(K9sResourceScreen):
    resource_name = "Volumes"
    hint_text = (
        "[cyan]<enter/i>[/cyan]Inspect [cyan]<d>[/cyan]Delete [cyan]<p>[/cyan]Prune "
        "[cyan]<R>[/cyan]Refresh [cyan]<esc>[/cyan]Back"
    )

    BINDINGS = [
        *K9sResourceScreen.NAV_BINDINGS,
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("i",        "inspect",        "Inspect",      show=False),
        Binding("d",        "delete_volume",  "Delete"),
        Binding("p",        "prune",          "Prune unused"),
        Binding("R",        "refresh",        "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield from self._compose_header()
        yield from self._compose_nav_bar()
        yield from self._compose_crumb()
        yield DataTable(id="tbl", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one("#tbl", DataTable).add_columns("Name", "Driver", "Mountpoint")
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        try:
            tbl = self.query_one("#tbl", DataTable)
            self.app.call_from_thread(tbl.clear)
            for v in docker.from_env().volumes.list():
                self.app.call_from_thread(
                    tbl.add_row,
                    v.name, v.attrs.get("Driver", ""), v.attrs.get("Mountpoint", ""),
                    key=v.name,
                )
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")

    def action_refresh(self) -> None:
        self.notify("Refreshing…", timeout=1)
        self._load()

    def action_inspect(self) -> None:
        tbl = self.query_one("#tbl", DataTable)
        if not tbl.row_count:
            return
        rk, _ = tbl.coordinate_to_cell_key(tbl.cursor_coordinate)
        self._do_inspect(rk.value)

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        self._do_inspect(event.row_key.value)

    def _do_inspect(self, name: str) -> None:
        try:
            vol = docker.from_env().volumes.get(name)
            self.app.push_screen(InspectScreen(vol.name, vol.attrs))
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def action_delete_volume(self) -> None:
        tbl = self.query_one("#tbl", DataTable)
        if not tbl.row_count:
            return
        rk, _ = tbl.coordinate_to_cell_key(tbl.cursor_coordinate)

        def _do(ok: bool) -> None:
            if ok:
                try:
                    docker.from_env().volumes.get(rk.value).remove(force=True)
                    self.notify("Volume removed")
                    self._load()
                except Exception as exc:
                    self.notify(str(exc), severity="error")

        self.app.push_screen(ConfirmScreen("Remove this volume?"), _do)

    def action_prune(self) -> None:
        def _do(ok: bool) -> None:
            if ok:
                try:
                    r = docker.from_env().volumes.prune()
                    self.notify(f"Pruned unused volumes — freed {_bytes(r.get('SpaceReclaimed', 0))}")
                    self._load()
                except Exception as exc:
                    self.notify(str(exc), severity="error")

        self.app.push_screen(ConfirmScreen("Prune all [bold]unused[/bold] volumes?"), _do)


# ── Networks screen ───────────────────────────────────────────────────────────

class NetworksScreen(K9sResourceScreen):
    resource_name = "Networks"
    hint_text = (
        "[cyan]<enter/i>[/cyan]Inspect [cyan]<d>[/cyan]Delete [cyan]<p>[/cyan]Prune "
        "[cyan]<R>[/cyan]Refresh [cyan]<esc>[/cyan]Back"
    )

    BINDINGS = [
        *K9sResourceScreen.NAV_BINDINGS,
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("i",        "inspect",        "Inspect",      show=False),
        Binding("d",        "delete_network", "Delete"),
        Binding("p",        "prune",          "Prune unused"),
        Binding("R",        "refresh",        "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield from self._compose_header()
        yield from self._compose_nav_bar()
        yield from self._compose_crumb()
        yield DataTable(id="tbl", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one("#tbl", DataTable).add_columns("ID", "Name", "Driver", "Scope", "Subnet", "Ctrs")
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        try:
            tbl = self.query_one("#tbl", DataTable)
            self.app.call_from_thread(tbl.clear)
            for n in docker.from_env().networks.list():
                configs = (n.attrs.get("IPAM", {}).get("Config") or [])
                subnet  = configs[0].get("Subnet", "") if configs else ""
                ctrs    = len(n.attrs.get("Containers") or {})
                self.app.call_from_thread(
                    tbl.add_row,
                    _short(n.id, 12), n.name,
                    n.attrs.get("Driver", ""), n.attrs.get("Scope", ""),
                    subnet, str(ctrs),
                    key=n.id,
                )
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")

    def action_refresh(self) -> None:
        self.notify("Refreshing…", timeout=1)
        self._load()

    def action_inspect(self) -> None:
        tbl = self.query_one("#tbl", DataTable)
        if not tbl.row_count:
            return
        rk, _ = tbl.coordinate_to_cell_key(tbl.cursor_coordinate)
        self._do_inspect(rk.value)

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        self._do_inspect(event.row_key.value)

    def _do_inspect(self, net_id: str) -> None:
        try:
            net = docker.from_env().networks.get(net_id)
            self.app.push_screen(InspectScreen(net.name, net.attrs))
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def action_delete_network(self) -> None:
        tbl = self.query_one("#tbl", DataTable)
        if not tbl.row_count:
            return
        rk, _ = tbl.coordinate_to_cell_key(tbl.cursor_coordinate)

        def _do(ok: bool) -> None:
            if ok:
                try:
                    docker.from_env().networks.get(rk.value).remove()
                    self.notify("Network removed")
                    self._load()
                except Exception as exc:
                    self.notify(str(exc), severity="error")

        self.app.push_screen(ConfirmScreen("Remove this network?"), _do)

    def action_prune(self) -> None:
        def _do(ok: bool) -> None:
            if ok:
                try:
                    docker.from_env().networks.prune()
                    self.notify("Pruned unused networks")
                    self._load()
                except Exception as exc:
                    self.notify(str(exc), severity="error")

        self.app.push_screen(ConfirmScreen("Prune all [bold]unused[/bold] networks?"), _do)


# ── Compose screen ────────────────────────────────────────────────────────────

class ComposeScreen(K9sResourceScreen):
    resource_name = "Compose"
    hint_text = (
        "[cyan]<enter/i>[/cyan]Inspect [cyan]<u>[/cyan]Up [cyan]<w>[/cyan]Down [cyan]<r>[/cyan]Restart "
        "[cyan]<R>[/cyan]Refresh [cyan]<l>[/cyan]Logs [cyan]<esc>[/cyan]Back"
    )

    BINDINGS = [
        *K9sResourceScreen.NAV_BINDINGS,
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("i",        "inspect",         "Inspect",  show=False),
        Binding("u",        "compose_up",      "Up"),
        Binding("w",        "compose_down",    "Down"),
        Binding("r",        "compose_restart", "Restart"),
        Binding("R",        "refresh",         "Refresh"),
        Binding("l",        "compose_logs",    "Logs"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._pending_compose_args: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        yield from self._compose_header()
        yield from self._compose_nav_bar()
        yield from self._compose_crumb()
        yield DataTable(id="tbl", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one("#tbl", DataTable).add_columns(
            "Project", "Dir", "Services", "Running", "Stopped"
        )
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        try:
            projects: dict[str, dict[str, Any]] = {}
            for c in docker.from_env().containers.list(all=True):
                labels = c.labels or {}
                proj = labels.get("com.docker.compose.project")
                if not proj:
                    continue
                if proj not in projects:
                    projects[proj] = {
                        "dir":      labels.get("com.docker.compose.project.working_dir", "?"),
                        "services": set(),
                        "running":  0,
                        "stopped":  0,
                    }
                projects[proj]["services"].add(labels.get("com.docker.compose.service", "?"))
                if c.status == "running":
                    projects[proj]["running"] += 1
                else:
                    projects[proj]["stopped"] += 1

            tbl = self.query_one("#tbl", DataTable)
            self.app.call_from_thread(tbl.clear)
            for name, info in sorted(projects.items()):
                self.app.call_from_thread(
                    tbl.add_row,
                    name, info["dir"][-50:],
                    str(len(info["services"])),
                    f"[green]{info['running']}[/green]",
                    f"[red]{info['stopped']}[/red]",
                    key=name,
                )
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")

    def _do_inspect(self) -> None:
        sel = self._selected()
        if not sel:
            return
        proj, wd = sel
        try:
            r = subprocess.run(
                ["docker", "compose", "--project-name", proj, "config"],
                capture_output=True, text=True, cwd=wd,
            )
            if r.returncode == 0:
                try:
                    import yaml
                    attrs = yaml.safe_load(r.stdout) or {}
                except ImportError:
                    attrs = {"raw_config": r.stdout}
                except Exception:
                    attrs = {"raw_config": r.stdout}
            else:
                attrs = {"project": proj, "dir": wd, "error": r.stderr[:500]}
            self.app.push_screen(InspectScreen(proj, attrs))
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def action_inspect(self) -> None:
        self._do_inspect()

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        self._do_inspect()

    def _selected(self) -> tuple[str, str] | None:
        tbl = self.query_one("#tbl", DataTable)
        if not tbl.row_count:
            return None
        try:
            rk, _ = tbl.coordinate_to_cell_key(tbl.cursor_coordinate)
            proj = rk.value
            for c in docker.from_env().containers.list(all=True):
                labels = c.labels or {}
                if labels.get("com.docker.compose.project") == proj:
                    return proj, labels.get("com.docker.compose.project.working_dir", ".")
            return proj, "."
        except Exception:
            return None

    @work(thread=True)
    def _run_compose_worker(self) -> None:
        sel = self._selected()
        if not sel:
            return
        proj, wd = sel
        args = self._pending_compose_args
        try:
            r = subprocess.run(
                ["docker", "compose", "--project-name", proj, *args],
                capture_output=True, text=True, cwd=wd,
            )
            msg = f"compose {' '.join(args)} OK" if r.returncode == 0 else (r.stderr[:200] or "error")
            sev = "information" if r.returncode == 0 else "error"
            self.app.call_from_thread(self.notify, msg, severity=sev)
            self.app.call_from_thread(self._load)
        except FileNotFoundError:
            self.app.call_from_thread(self.notify, "`docker compose` not found in PATH", severity="error")

    def _run_compose(self, *args: str) -> None:
        self._pending_compose_args = args
        self._run_compose_worker()

    def action_compose_up(self) -> None:
        self._run_compose("up", "-d")

    def action_compose_down(self) -> None:
        sel = self._selected()
        if not sel:
            return
        proj, _ = sel

        def _do(ok: bool) -> None:
            if ok:
                self._run_compose("down")

        self.app.push_screen(ConfirmScreen(f"Compose down [bold]{proj}[/bold]?"), _do)

    def action_compose_restart(self) -> None:
        self._run_compose("restart")

    def action_compose_logs(self) -> None:
        sel = self._selected()
        if not sel:
            return
        proj, _ = sel
        try:
            cids = [
                c.id for c in docker.from_env().containers.list(all=True)
                if (c.labels or {}).get("com.docker.compose.project") == proj and c.status == "running"
            ]
        except Exception:
            cids = []
        if cids:
            self.app.push_screen(LogScreen(cids[0], f"{proj} [first running service]"))
        else:
            self.notify("No running containers for this project", severity="warning")

    def action_refresh(self) -> None:
        self.notify("Refreshing…", timeout=1)
        self._load()


# ── Help ──────────────────────────────────────────────────────────────────────

HELP_TEXT = """\
[bold cyan]Containers[/bold cyan]
  [cyan]arrows[/cyan]         Navigate
  [cyan]enter / d[/cyan]      Describe (inspect)
  [cyan]l[/cyan]              Logs (streaming)
  [cyan]s[/cyan]              Shell exec (-it)
  [cyan]S[/cyan]              Stats (CPU/mem/net/block)
  [cyan]ctrl+d[/cyan]         Remove container (x = also remove volumes)
  [cyan]ctrl+k[/cyan]         Kill (SIGKILL)
  [cyan]w[/cyan]              Compose down (x = also remove volumes)
  [cyan]W[/cyan]              Compose up (restart after down)
  [cyan]t[/cyan]              Stop
  [cyan]u[/cyan]              Start (up)
  [cyan]r[/cyan]              Restart
  [cyan]R[/cyan]              Refresh
  [cyan]a[/cyan]              Cycle workspace (running/all/exited)
  [cyan]A[/cyan]              Toggle auto-refresh on/off
  [cyan]/[/cyan]              Filter by name
  [cyan]:[/cyan]              Command mode

[bold cyan]Command mode (:)[/bold cyan]
  [cyan]:img[/cyan]           Images
  [cyan]:vol[/cyan]           Volumes
  [cyan]:net[/cyan]           Networks
  [cyan]:compose[/cyan]       Compose projects
  [cyan]:up[/cyan]             Find compose files (fuzzy finder)
  [cyan]:up <path>[/cyan]     Compose up from path
  [cyan]:q![/cyan]            Quit

[bold cyan]Navigation[/bold cyan]
  [cyan]1[/cyan]              Containers
  [cyan]2[/cyan]              Images
  [cyan]3[/cyan]              Volumes
  [cyan]4[/cyan]              Networks
  [cyan]5[/cyan]              Compose projects
  [cyan]P[/cyan]              System prune (sudo docker system prune --all)
  [cyan]?[/cyan]              This help
  [cyan]ctrl+c[/cyan]         Quit

[bold cyan]Images / Volumes / Networks[/bold cyan]
  [cyan]enter[/cyan]          Inspect (detail view)
  [cyan]d[/cyan]              Delete selected
  [cyan]p[/cyan]              Prune unused / dangling
  [cyan]R[/cyan]              Refresh

[bold cyan]Compose[/bold cyan]
  [cyan]enter[/cyan]          Inspect (compose config)
  [cyan]u[/cyan]              docker compose up -d
  [cyan]w[/cyan]              docker compose down
  [cyan]r[/cyan]              docker compose restart
  [cyan]R[/cyan]              Refresh
  [cyan]l[/cyan]              Logs (first running service)

[bold cyan]Describe[/bold cyan]
  [cyan]y[/cyan]              Toggle raw JSON / human view
"""


class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "esc", "Close", show=False),
        Binding("q,question_mark", "dismiss", "Close"),
        Binding("slash", "open_search", "Search", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="helpbox"):
            with ScrollableContainer(id="help-scroll"):
                yield Static(HELP_TEXT, id="help-content")
            yield Input(placeholder="/", id="help-filter", classes="hidden")

    def action_open_search(self) -> None:
        inp = self.query_one("#help-filter", Input)
        inp.remove_class("hidden")
        inp.focus()

    def action_esc(self) -> None:
        inp = self.query_one("#help-filter", Input)
        if not inp.has_class("hidden"):
            inp.value = ""
            inp.add_class("hidden")
            self.query_one("#help-content", Static).update(HELP_TEXT)
            self.query_one("#help-scroll", ScrollableContainer).focus()
        else:
            self.dismiss()

    @on(Input.Submitted, "#help-filter")
    def _submit_search(self) -> None:
        """Enter confirms search and returns focus to the help content."""
        self.query_one("#help-filter", Input).add_class("hidden")
        self.query_one("#help-scroll", ScrollableContainer).focus()

    @on(Input.Changed, "#help-filter")
    def _filter_help(self, event: Input.Changed) -> None:
        q = event.value.strip().lower()
        if not q:
            self.query_one("#help-content", Static).update(HELP_TEXT)
            return
        lines = HELP_TEXT.splitlines()
        filtered = []
        current_section = ""
        for line in lines:
            if line.startswith("[bold cyan]"):
                current_section = line
                continue
            if q in line.lower():
                if current_section:
                    filtered.append(current_section)
                    current_section = ""
                filtered.append(line)
        self.query_one("#help-content", Static).update(
            "\n".join(filtered) if filtered else "[dim]No matches[/dim]"
        )


# ── Containers screen (main) ──────────────────────────────────────────────────

class ContainersScreen(K9sResourceScreen):
    resource_name = "Containers"
    hint_text = (
        "[cyan]<enter/d>[/cyan]Describe [cyan]<l>[/cyan]Logs [cyan]<s>[/cyan]Shell "
        "[cyan]<S>[/cyan]Stats [cyan]<ctrl+d>[/cyan]Delete "
        "[cyan]<w>[/cyan]Down [cyan]<W>[/cyan]Up "
        "[cyan]<t>[/cyan]Stop [cyan]<ctrl+k>[/cyan]Kill [cyan]<u>[/cyan]Start [cyan]<r>[/cyan]Restart "
        "[cyan]<R>[/cyan]Refresh [cyan]<P>[/cyan]Prune "
        "[cyan]<a>[/cyan]Cycle [cyan]<A>[/cyan]AutoRefresh [cyan]</>[/cyan]Filter "
        "[cyan]<:>[/cyan]Cmd [cyan]<?>[/cyan]Help"
    )

    WORKSPACE_MODES = ["running", "all", "exited"]

    workspace:    reactive[str]  = reactive("running")
    filter_text:  reactive[str]  = reactive("")
    auto_refresh: reactive[bool] = reactive(True)

    BINDINGS = [
        Binding("ctrl+c",      "force_quit",    "Quit",      show=False),
        Binding("R",           "refresh",       "Refresh",   show=False),
        Binding("l",           "view_logs",     "Logs",      show=False),
        Binding("s",           "exec_shell",    "Shell",     show=False),
        Binding("S",           "view_stats",    "Stats",     show=False),
        Binding("d",           "describe",      "Describe",  show=False),
        Binding("ctrl+d",      "delete",        "Delete",    show=False),
        Binding("ctrl+k",      "kill",          "Kill",      show=False),
        Binding("t",           "stop",          "Stop",      show=False),
        Binding("u",           "start",         "Start",     show=False),
        Binding("r",           "restart",       "Restart",   show=False),
        Binding("w",           "compose_down",  "Down",      show=False),
        Binding("W",           "compose_up",    "Up",        show=False),
        Binding("P",           "system_prune",  "Prune",     show=False),
        Binding("a",           "cycle_workspace", "Cycle workspace", show=False),
        Binding("A",           "toggle_auto_refresh", "Auto-refresh", show=False),
        Binding("slash",       "filter",        "Filter",    show=False),
        Binding("colon",       "command",       "Command",   show=False),
        Binding("escape",      "clear_filter",  "Clear",     show=False),
        Binding("1",           "goto_containers", "Containers", show=False),
        Binding("2",           "goto_images",     "Images",     show=False),
        Binding("3",           "goto_volumes",    "Volumes",    show=False),
        Binding("4",           "goto_networks",   "Networks",   show=False),
        Binding("5",           "goto_compose",    "Compose",    show=False),
        Binding("question_mark", "help",         "Help",      show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: dict[str, dict[str, Any]] = {}
        self._pending_action: tuple[str, str] = ("", "")
        self._refresh_timer = None
        self._state_counts: dict[str, int] = {}
        self._last_compose: tuple[str, str] | None = None  # (project, dir)

    def compose(self) -> ComposeResult:
        yield from self._compose_header()
        yield from self._compose_nav_bar()
        yield from self._compose_crumb()
        with Vertical(id="filter-bar"):
            yield Input(placeholder="/filter by name... (Enter=apply  Esc=clear)", id="filter-input")
        with Vertical(id="cmd-bar"):
            yield Static(
                "[dim]tab=autocomplete  enter=run  esc=cancel[/dim]",
                id="cmd-help",
            )
            yield CommandInput(
                placeholder="img, vol, net, compose, up (find compose), prune, q!",
                id="cmd-input",
                suggester=CommandSuggester(),
            )
        yield DataTable(id="tbl", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        tbl = self.query_one("#tbl", DataTable)
        tbl.add_columns("ID", "Name", "Image", "State", "Ports", "Age", "Compose")
        tbl.focus()
        self._refresh_timer = self.set_interval(5, self._auto_load)
        self._load()

    def _auto_load(self) -> None:
        """Called by timer — only refreshes if auto_refresh is on."""
        if self.auto_refresh:
            self._load()

    def _update_crumb(self) -> None:
        ws = self.workspace
        ws_colors = {
            "running": "[green]\\[running][/green]",
            "all":     "[yellow]\\[all][/yellow]",
            "stopped": "[red]\\[stopped][/red]",
            "exited":  "[red]\\[exited][/red]",
        }
        parts = [f"[bold cyan]<Containers>[/bold cyan]"]
        parts.append(ws_colors.get(ws, f"[dim][{ws}][/dim]"))
        count = len(self._rows)
        parts.append(f"[dim]({count})[/dim]")
        if self.filter_text:
            parts.append(f"[green]/{self.filter_text}[/green]")
        if not self.auto_refresh:
            parts.append("[dim yellow][pause][/dim yellow]")
        self.query_one("#crumb-bar", Static).update(" ".join(parts))

    def _build_row(self, c) -> tuple[str, list[str], dict[str, str]]:
        """Build row data from a container. Returns (id, cells, meta)."""
        name = c.name or ""
        ports_raw = c.ports or {}
        port_str = ", ".join(
            f"{v[0]['HostPort']}->{k}" if v else k
            for k, v in ports_raw.items()
        ) or "-"
        image_name = (
            (c.image.tags[0] if c.image and c.image.tags else None)
            or c.attrs.get("Config", {}).get("Image", "")
        )
        state = c.status or "unknown"
        age = _ago(_parse_ts(c.attrs.get("Created", "")))
        labels = c.labels or {}
        compose_dir = labels.get("com.docker.compose.project.working_dir", "-")
        cells = [
            _short(c.short_id, 12),
            name,
            image_name[:34],
            _state_markup(state),
            port_str[:28],
            age,
            compose_dir if compose_dir == "-" else compose_dir[-50:],
        ]
        return c.id, cells, {"name": name, "state": state}

    @work(thread=True, exclusive=True)
    def _load(self) -> None:
        try:
            ws = self.workspace
            all_containers = docker.from_env().containers.list(all=True)

            # Count states across ALL containers
            counts: dict[str, int] = {}
            for c in all_containers:
                st = c.status or "unknown"
                if st in ("running", "exited", "paused"):
                    counts[st] = counts.get(st, 0) + 1
                else:
                    counts["other"] = counts.get("other", 0) + 1
            self._state_counts = counts

            # Filter by workspace
            if ws == "running":
                containers = [c for c in all_containers if c.status == "running"]
            elif ws == "stopped":
                containers = [c for c in all_containers if c.status != "running"]
            elif ws == "exited":
                containers = [c for c in all_containers if c.status == "exited"]
            else:
                containers = all_containers

            filt = self.filter_text.lower()
            tbl = self.query_one("#tbl", DataTable)

            # Build new data
            new_rows: dict[str, list[str]] = {}
            new_meta: dict[str, dict[str, str]] = {}
            new_order: list[str] = []
            for c in containers:
                name = c.name or ""
                if filt and filt not in name.lower():
                    continue
                cid, cells, meta = self._build_row(c)
                new_rows[cid] = cells
                new_meta[cid] = meta
                new_order.append(cid)

            # Apply diff to table instead of clear+rebuild
            self.app.call_from_thread(self._apply_table_diff, tbl, new_rows, new_order)
            self._rows = new_meta
            self.app.call_from_thread(self._update_crumb)

        except DockerException as exc:
            self.app.call_from_thread(self.notify, f"Docker: {exc}", severity="error")
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")

    def _apply_table_diff(
        self, tbl: DataTable, new_rows: dict[str, list[str]], new_order: list[str]
    ) -> None:
        """Update table in-place: remove stale, add new, update changed cells."""
        # Get existing row keys
        existing_keys = set()
        for i in range(tbl.row_count):
            try:
                rk, _ = tbl.coordinate_to_cell_key(Coordinate(i, 0))
                existing_keys.add(rk.value)
            except Exception:
                break

        new_keys = set(new_order)

        # Remove rows no longer present
        for key in existing_keys - new_keys:
            try:
                tbl.remove_row(key)
            except Exception:
                pass

        # Add or update rows
        for key in new_order:
            cells = new_rows[key]
            if key in existing_keys:
                # Update cells in place
                for col_idx, val in enumerate(cells):
                    try:
                        tbl.update_cell(key, tbl.ordered_columns[col_idx].key, val)
                    except Exception:
                        pass
            else:
                tbl.add_row(*cells, key=key)

    def _selected(self) -> str | None:
        tbl = self.query_one("#tbl", DataTable)
        if not tbl.row_count:
            return None
        try:
            rk, _ = tbl.coordinate_to_cell_key(tbl.cursor_coordinate)
            return rk.value
        except Exception:
            return None

    def action_refresh(self) -> None:
        self.notify("Refreshing…", timeout=1)
        self._load()

    def action_cycle_workspace(self) -> None:
        modes = self.WORKSPACE_MODES
        idx = (modes.index(self.workspace) + 1) % len(modes)
        self.workspace = modes[idx]
        self._load()

    def action_force_quit(self) -> None:
        self.app.exit()

    def action_toggle_auto_refresh(self) -> None:
        self.auto_refresh = not self.auto_refresh
        state = "on" if self.auto_refresh else "off"
        self.notify(f"Auto-refresh {state}")
        self._update_crumb()

    # ── Filter ──
    def action_filter(self) -> None:
        self.query_one("#filter-bar").add_class("visible")
        self.query_one("#cmd-bar").remove_class("visible")
        fi = self.query_one("#filter-input", Input)
        fi.value = ""
        fi.focus()

    def action_clear_filter(self) -> None:
        self.query_one("#filter-bar").remove_class("visible")
        self.query_one("#cmd-bar").remove_class("visible")
        self.query_one("#filter-input", Input).value = ""
        self.filter_text = ""
        self.query_one("#tbl", DataTable).focus()
        self._load()

    @on(Input.Submitted, "#filter-input")
    def _apply_filter(self, event: Input.Submitted) -> None:
        self.filter_text = event.value
        self.query_one("#filter-bar").remove_class("visible")
        self._load()

    # ── Command mode (like k9s :command) ──
    def action_command(self) -> None:
        self.query_one("#cmd-bar").add_class("visible")
        self.query_one("#filter-bar").remove_class("visible")
        ci = self.query_one("#cmd-input", CommandInput)
        ci.value = ""
        ci.focus()

    @on(Input.Submitted, "#cmd-input")
    def _apply_command(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        cmd = raw.lower()
        self.query_one("#cmd-bar").remove_class("visible")
        self.query_one("#tbl", DataTable).focus()

        # Handle :up — open file finder or use path directly
        if cmd == "up":
            self._open_file_finder()
            return
        if cmd.startswith("up "):
            path = os.path.expanduser(raw[3:].strip())
            self._cmd_compose_up(path)
            return

        commands = {
            "img": self.action_goto_images,
            "images": self.action_goto_images,
            "vol": self.action_goto_volumes,
            "volumes": self.action_goto_volumes,
            "net": self.action_goto_networks,
            "networks": self.action_goto_networks,
            "com": self.action_goto_compose,
            "compose": self.action_goto_compose,
            "q!": self.action_force_quit,
            "prune": self.action_system_prune,
        }

        action = commands.get(cmd)
        if action:
            action()
        else:
            self.notify(f"Unknown command: {cmd}", severity="warning")

    def _open_file_finder(self) -> None:
        def _on_result(path: str | None) -> None:
            if path:
                self._cmd_compose_up(path)
        self.app.push_screen(FileFinder(os.getcwd()), _on_result)

    def _cmd_compose_up(self, path: str) -> None:
        """Start a compose project from a path (file or directory)."""
        if os.path.isfile(path):
            compose_file = path
            project_dir = os.path.dirname(os.path.abspath(path))
        elif os.path.isdir(path):
            project_dir = os.path.abspath(path)
            compose_file = None
            for name in ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"):
                candidate = os.path.join(project_dir, name)
                if os.path.isfile(candidate):
                    compose_file = candidate
                    break
            if not compose_file:
                self.notify(f"No compose file found in {project_dir}", severity="error")
                return
        else:
            self.notify(f"Path not found: {path}", severity="error")
            return
        self._run_compose_from_file(project_dir, compose_file)

    @work(thread=True)
    def _run_compose_from_file(self, project_dir: str, compose_file: str) -> None:
        try:
            cmd = ["docker", "compose", "-f", compose_file, "up", "-d"]
            r = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True)
            if r.returncode == 0:
                self.app.call_from_thread(self.notify, f"Compose up from {compose_file}")
                self.app.call_from_thread(self._load)
            else:
                self.app.call_from_thread(self.notify, r.stderr.strip()[:200] or "error", severity="error")
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")

    # ── Container actions (worker method for thread safety) ──
    @work(thread=True)
    def _run_on_worker(self) -> None:
        cid, action = self._pending_action
        try:
            getattr(docker.from_env().containers.get(cid), action)()
            self.app.call_from_thread(self.notify, f"{action} OK")
            self.app.call_from_thread(self._load)
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")

    def _run_on(self, cid: str, action: str) -> None:
        self._pending_action = (cid, action)
        self._run_on_worker()

    def action_view_logs(self) -> None:
        cid = self._selected()
        if cid:
            self.app.push_screen(LogScreen(cid, self._rows.get(cid, {}).get("name", cid[:12])))

    def action_exec_shell(self) -> None:
        cid = self._selected()
        if not cid:
            return
        if self._rows.get(cid, {}).get("state") != "running":
            self.notify("Container is not running", severity="warning")
            return
        import shlex
        with self.app.suspend():
            os.system(
                f"docker exec -it {shlex.quote(cid)} /bin/sh -c "
                f"'command -v bash >/dev/null 2>&1 && exec bash || exec sh'"
            )

    def action_view_stats(self) -> None:
        cid = self._selected()
        if not cid:
            return
        if self._rows.get(cid, {}).get("state") != "running":
            self.notify("Container is not running", severity="warning")
            return
        self.app.push_screen(StatsScreen(cid, self._rows.get(cid, {}).get("name", cid[:12])))

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_describe()

    def action_describe(self) -> None:
        cid = self._selected()
        if cid:
            self.app.push_screen(DescribeScreen(cid, self._rows.get(cid, {}).get("name", cid[:12])))

    def action_stop(self) -> None:
        cid = self._selected()
        if cid:
            self._run_on(cid, "stop")

    def action_start(self) -> None:
        cid = self._selected()
        if cid:
            self._run_on(cid, "start")

    def action_restart(self) -> None:
        cid = self._selected()
        if cid:
            self._run_on(cid, "restart")

    def action_kill(self) -> None:
        cid = self._selected()
        if not cid:
            return
        name = self._rows.get(cid, {}).get("name", cid[:12])

        def _do(ok: bool) -> None:
            if ok:
                self._run_on(cid, "kill")

        self.app.push_screen(ConfirmScreen(f"Kill [bold]{name}[/bold]?"), _do)

    def action_delete(self) -> None:
        cid = self._selected()
        if not cid:
            return
        name = self._rows.get(cid, {}).get("name", cid[:12])

        def _do(result: dict) -> None:
            if not result.get("confirmed"):
                return
            try:
                docker.from_env().containers.get(cid).remove(
                    force=True, v=result.get("volumes", False)
                )
                msg = "Container removed"
                if result.get("volumes"):
                    msg += " (with volumes)"
                self.notify(msg)
                self._load()
            except Exception as exc:
                self.notify(str(exc), severity="error")

        self.app.push_screen(
            ConfirmWithFlagsScreen(
                f"Remove [bold]{name}[/bold]?",
                [("x", "volumes", "-v volumes")],
            ),
            _do,
        )

    def action_compose_down(self) -> None:
        cid = self._selected()
        if not cid:
            return
        try:
            c = docker.from_env().containers.get(cid)
            project = (c.labels or {}).get("com.docker.compose.project")
            project_dir = (c.labels or {}).get("com.docker.compose.project.working_dir")
        except Exception:
            self.notify("Cannot inspect container", severity="error")
            return
        if not project or not project_dir:
            self.notify("Not a Compose container", severity="warning")
            return

        def _do(result: dict) -> None:
            if not result.get("confirmed"):
                return
            self._last_compose = (project, project_dir)
            self._run_compose_down(project, project_dir, result.get("volumes", False))

        self.app.push_screen(
            ConfirmWithFlagsScreen(
                f"Compose down [bold]{project}[/bold]?",
                [("x", "volumes", "-v volumes")],
            ),
            _do,
        )

    @work(thread=True)
    def _run_compose_down(self, project: str, project_dir: str, volumes: bool) -> None:
        try:
            cmd = ["docker", "compose", "-p", project, "down"]
            if volumes:
                cmd.append("-v")
            subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, check=True)
            msg = f"Compose down {project}"
            if volumes:
                msg += " (with volumes)"
            self.app.call_from_thread(self.notify, msg)
            self.app.call_from_thread(self._load)
        except subprocess.CalledProcessError as exc:
            self.app.call_from_thread(self.notify, exc.stderr.strip() or str(exc), severity="error")
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")

    def action_compose_up(self) -> None:
        # Prefer last downed project (W is typically used right after w)
        if self._last_compose:
            project, project_dir = self._last_compose
            self._run_compose_up(project, project_dir)
            return
        # Otherwise try from selected container
        cid = self._selected()
        if cid:
            try:
                c = docker.from_env().containers.get(cid)
                project = (c.labels or {}).get("com.docker.compose.project")
                project_dir = (c.labels or {}).get("com.docker.compose.project.working_dir")
                if project and project_dir:
                    self._run_compose_up(project, project_dir)
                    return
            except Exception:
                pass
        self.notify("No Compose project found (do a down first or select a compose container)", severity="warning")

    @work(thread=True)
    def _run_compose_up(self, project: str, project_dir: str) -> None:
        try:
            cmd = ["docker", "compose", "-p", project, "up", "-d"]
            subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, check=True)
            self._last_compose = None
            self.app.call_from_thread(self.notify, f"Compose up {project}")
            self.app.call_from_thread(self._load)
        except subprocess.CalledProcessError as exc:
            self.app.call_from_thread(self.notify, exc.stderr.strip() or str(exc), severity="error")
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")

    def action_system_prune(self) -> None:
        def _do(result: dict) -> None:
            if not result.get("confirmed"):
                return
            self._run_system_prune(result.get("password", ""), result.get("force", False))
        self.app.push_screen(SudoPruneScreen(), _do)

    @work(thread=True)
    def _run_system_prune(self, password: str, force: bool) -> None:
        try:
            cmd = ["sudo", "-S", "docker", "system", "prune", "--all"]
            if force:
                cmd.append("--force")
            proc = subprocess.run(
                cmd,
                input=password + "\n",
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                self.app.call_from_thread(self.notify, "System prune completed")
                self.app.call_from_thread(self._load)
            else:
                err = proc.stderr.strip()
                if "incorrect password" in err.lower() or "sorry" in err.lower():
                    self.app.call_from_thread(self.notify, "Wrong sudo password", severity="error")
                else:
                    self.app.call_from_thread(self.notify, err[:200] or "error", severity="error")
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")

    def action_goto_containers(self) -> None:
        pass  # already here

    def action_goto_images(self) -> None:
        self.app.push_screen(ImagesScreen())

    def action_goto_volumes(self) -> None:
        self.app.push_screen(VolumesScreen())

    def action_goto_networks(self) -> None:
        self.app.push_screen(NetworksScreen())

    def action_goto_compose(self) -> None:
        self.app.push_screen(ComposeScreen())

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())


# ── App ───────────────────────────────────────────────────────────────────────

class D9s(App):
    TITLE = "d9s"
    CSS = K9S_CSS

    def on_mount(self) -> None:
        self.push_screen(ContainersScreen())


def main() -> None:
    try:
        docker.from_env().ping()
    except DockerException as exc:
        print(f"Cannot connect to Docker daemon: {exc}", file=sys.stderr)
        sys.exit(1)
    D9s().run()


if __name__ == "__main__":
    main()
