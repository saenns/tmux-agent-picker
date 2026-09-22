"""SSH transport and remote-tmux operations for the window picker.

This module deliberately has no knowledge of Codex, Claude, hooks, or agent
state.  It turns remote tmux output into generic ``Window`` objects and
provides the command used to attach to one.  Agent metadata is merely optional
tmux data carried by the shared window format.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agent_picker import Pane, Window, parse_hosts, parse_inventory


def run(command: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)


def remote_tmux_command(label: str, configured: str) -> list[str]:
    """Return the tmux executable configured for one remote host."""
    for entry in configured.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        entry_label, command = entry.split("=", 1)
        if entry_label.strip() == label and command.strip():
            return shlex.split(command)
    return ["tmux"]


def remote_mru(label: str, ssh_command: list[str], windows: list[Window], timeout: float) -> dict[str, float]:
    """Import optional legacy picker MRU data without requiring it."""
    result = run([*ssh_command, "cat ~/.local/state/tmux-agent-picker/mru.json"], timeout=timeout + 1)
    if result.returncode != 0:
        return {}
    try:
        source = json.loads(result.stdout)
    except (ValueError, TypeError):
        return {}
    windows_by_id = {window.window_id: window for window in windows}
    merged: dict[str, float] = {}
    for key, timestamp in source.items():
        parts = str(key).split("|", 2)
        if len(parts) != 3 or parts[0] != "local":
            continue
        window = windows_by_id.get(parts[2])
        if not window:
            continue
        try:
            remote_key = f"{label}|{window.session_id}|{window.window_id}"
            merged[remote_key] = max(merged.get(remote_key, 0), float(timestamp))
        except (TypeError, ValueError):
            continue
    return merged


def remote_inventory(
    label: str, target: str, timeout: float, batch_mode: str, remote_tmux: str, retries: int, tmux_format: str
) -> tuple[list[Window], dict[str, float]]:
    """Read windows from a remote tmux server over SSH.

    Remote agent metadata is optional; this transport does not install hooks
    or otherwise mutate the remote tmux server while collecting inventory.
    """
    tmux_command = remote_tmux_command(label, remote_tmux)
    # A hook already runs with the newly selected window as its context.
    # Explicitly targeting ``#{window_id}`` is not expanded by older tmux
    # versions and makes a successful select-window report an error instead.
    list_panes = shlex.join([*tmux_command, "list-panes", "-a", "-F", tmux_format])
    remote_command = list_panes
    ssh_command = ["ssh", "-o", f"ConnectTimeout={max(1, int(timeout))}"]
    # WSSH rejects an explicitly supplied BatchMode option, even when it is
    # set to "no". "auto" leaves the SSH client defaults untouched.
    if batch_mode != "auto":
        ssh_command.extend(["-o", f"BatchMode={batch_mode}"])
    ssh_command.append(target)
    result = None
    for attempt in range(max(0, retries) + 1):
        try:
            result = run([*ssh_command, remote_command], timeout=timeout + 1)
        except (subprocess.TimeoutExpired, OSError):
            result = None
        if result and result.returncode == 0:
            break
        if attempt >= retries:
            break
        error = result.stderr.lower() if result else ""
        if not (result is None or any(marker in error for marker in ("connection reset", "connection closed", "broken pipe", "connection timed out", "mux_client", "control socket"))):
            break
        time.sleep(0.2)
    if result and result.returncode == 0:
        windows = parse_inventory(result.stdout, label, target)
        history = remote_mru(label, ssh_command, windows, timeout)
        for window in windows:
            if window.last_view:
                history[window.key] = window.last_view
        return windows, history
    return [
        Window(label, target, "", "unavailable", "", "-", "remote host offline", False,
               [Pane("", "0", True, "", "", "0", "offline", "", "", "", "", "")])
    ], {}


def collect_remote_windows(
    hosts_value: str, timeout: float, batch_mode: str, remote_tmux: str, retries: int, tmux_format: str
) -> tuple[list[Window], dict[str, float]]:
    hosts = parse_hosts(hosts_value)
    if not hosts:
        return [], {}
    windows: list[Window] = []
    history: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as executor:
        futures = {
            executor.submit(remote_inventory, label, target, timeout, batch_mode, remote_tmux, retries, tmux_format): label
            for label, target in hosts
        }
        for future in as_completed(futures):
            remote_windows, remote_history = future.result()
            windows.extend(remote_windows)
            history.update(remote_history)
    return windows, history


def remote_attach_command(
    target: dict[str, str], remote_tmux: str = "", proxy_session: str = ""
) -> str:
    """Attach through a private grouped session when ``proxy_session`` is set.

    A normal ``attach-session`` shares the remote session's selected window
    with every other client.  That means a local bridge can visibly jump when
    somebody (or another bridge) selects a window on the remote host.  A tmux
    session group shares the *windows* while retaining a per-session selected
    window, which is exactly the isolation a bridge needs.
    """
    tmux_command = remote_tmux_command(target.get("host", ""), remote_tmux)
    if proxy_session:
        has_proxy = shlex.join([*tmux_command, "has-session", "-t", proxy_session])
        create_proxy = shlex.join([
            *tmux_command, "new-session", "-d", "-t", target["session"], "-s", proxy_session,
        ])
        select_window = shlex.join([
            # @window_id is global in tmux target syntax: `proxy:@42` still
            # selects @42 in the invoking client's session. An index is
            # session-scoped, so it reliably selects it in the proxy session.
            *tmux_command, "select-window", "-t", f"{proxy_session}:{target['window']}",
        ])
        select_pane = shlex.join([*tmux_command, "select-pane", "-t", target["pane"]])
        attach = shlex.join([*tmux_command, "attach-session", "-t", proxy_session])
        remote = f"({has_proxy} || {create_proxy}) && {select_window} && {select_pane} && {attach}"
    else:
        remote = shlex.join([
            *tmux_command, "select-window", "-t", target["window_id"], ";",
            "select-pane", "-t", target["pane"], ";", "attach-session", "-t", target["session"],
        ])
    bridge = Path(__file__).resolve().parents[1] / "scripts" / "bridge.sh"
    return shlex.join([str(bridge), target["ssh"], remote])


def remote_focus_command(
    target: dict[str, str], remote_tmux: str = "", proxy_session: str = ""
) -> str:
    """Return the non-interactive tmux focus command for an existing bridge."""
    tmux_command = remote_tmux_command(target.get("host", ""), remote_tmux)
    if proxy_session:
        window_target = f"{proxy_session}:{target['window']}"
        pane_target = f"{window_target}.{target.get('pane_index', '0')}"
        return shlex.join([
            *tmux_command, "select-window", "-t", window_target, ";",
            *tmux_command, "select-pane", "-t", pane_target,
        ])
    return shlex.join([
        *tmux_command, "select-window", "-t", target["window_id"], ";",
        "select-pane", "-t", target["pane"],
    ])
