#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agent_picker import (  # noqa: E402
    SEP,
    Pane,
    Window,
    decode_target,
    format_row,
    load_mru,
    parse_hosts,
    parse_inventory,
    record_mru,
    scrollback_summary,
    sort_windows,
)


FORMAT_FIELDS = (
    "#{session_id}",
    "#{session_name}",
    "#{window_id}",
    "#{window_index}",
    "#{window_name}",
    "#{window_active}",
    "#{window_bell_flag}",
    "#{@last_view}",
    "#{@agent_picker_last_view}",
    "#{pane_id}",
    "#{pane_index}",
    "#{pane_active}",
    "#{pane_current_command}",
    "#{pane_title}",
    "#{pane_current_path}",
    "#{pane_dead}",
    "#{@agent_picker_state}",
    "#{@agent_picker_agent}",
    "#{@agent_picker_session_id}",
    "#{@agent_picker_hidden}",
    "#{@agent_picker_bridge}",
    "#{@agent_picker_summary}",
    "#{@agent_picker_summary_status}",
    "#{@agent_picker_summary_turn}",
)
TMUX_FORMAT = SEP.join(FORMAT_FIELDS)


def run(command: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)


def local_inventory() -> list[Window]:
    result = run(["tmux", "list-panes", "-a", "-F", TMUX_FORMAT])
    windows = parse_inventory(result.stdout) if result.returncode == 0 else []
    for window in windows:
        for pane in window.panes:
            if pane.summary:
                continue
            captured = run(["tmux", "capture-pane", "-p", "-J", "-S", "-80", "-t", pane.pane_id])
            if captured.returncode == 0:
                pane.summary = scrollback_summary(captured.stdout, pane.command)
                pane.summary_status = "scrollback"
    return windows


def remote_tmux_command(label: str, configured: str) -> list[str]:
    """Return the tmux executable for a host from label=command mappings."""
    for entry in configured.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        entry_label, command = entry.split("=", 1)
        if entry_label.strip() == label and command.strip():
            return shlex.split(command)
    return ["tmux"]


def remote_mru(label: str, target: str, ssh_command: list[str], windows: list[Window], timeout: float) -> dict[str, float]:
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
            # Older picker hooks sometimes wrote the same window under more
            # than one session key.  Retain the latest timestamp, never the
            # last JSON entry.
            merged[remote_key] = max(merged.get(remote_key, 0), float(timestamp))
        except (TypeError, ValueError):
            continue
    return merged


def remote_inventory(
    label: str, target: str, timeout: float, batch_mode: str, remote_tmux: str, retries: int
) -> tuple[list[Window], dict[str, float]]:
    tmux_command = remote_tmux_command(label, remote_tmux)
    # This is a native tmux hook, not a remote plugin dependency.  It is
    # installed into the live server under our own stable hook index whenever
    # inventory runs, so it also comes back after a remote tmux restart.
    # Keep tmux-fzf's @last_view untouched; this distinct option is the
    # generic fallback for hosts with no picker installation.
    hook_command = "set-option -w -t '#{window_id}' @agent_picker_last_view '#{t:%s}'"
    install_hook = shlex.join([*tmux_command, "set-hook", "-g", "after-select-window[999]", hook_command])
    list_panes = shlex.join([*tmux_command, "list-panes", "-a", "-F", TMUX_FORMAT])
    remote_command = f"{install_hook} && {list_panes}"
    ssh_command = ["ssh", "-o", f"ConnectTimeout={max(1, int(timeout))}"]
    # WSSH rejects an explicitly supplied BatchMode option, even when it is
    # set to "no".  "auto" leaves the SSH client defaults untouched.
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
        connection_failure = result is None or any(
            marker in error
            for marker in (
                "connection reset",
                "connection closed",
                "broken pipe",
                "connection timed out",
                "mux_client",
                "control socket",
            )
        )
        if not connection_failure:
            break
        time.sleep(0.2)
    if result and result.returncode == 0:
        windows = parse_inventory(result.stdout, label, target)
        history = remote_mru(label, target, ssh_command, windows, timeout)
        # Some hosts use tmux-fzf's @last_view hook as the canonical focus
        # history. Prefer it over the generic picker history when available.
        for window in windows:
            if window.last_view:
                history[window.key] = window.last_view
        return windows, history
    return [
        Window(
            host=label,
            ssh_target=target,
            session_id="",
            session_name="unavailable",
            window_id="",
            window_index="-",
            window_name="remote host offline",
            active=False,
            panes=[Pane("", "0", True, "", "", "0", "offline", "", "", "", "", "")],
        )
    ], {}


def collect(
    hosts_value: str, timeout: float, batch_mode: str, remote_tmux: str, retries: int
) -> tuple[list[Window], dict[str, float]]:
    windows = local_inventory()
    remote_history: dict[str, float] = {}
    hosts = parse_hosts(hosts_value)
    if not hosts:
        return windows, remote_history
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as executor:
        futures = {
            executor.submit(remote_inventory, label, target, timeout, batch_mode, remote_tmux, retries): label
            for label, target in hosts
        }
        for future in as_completed(futures):
            remote_windows, history = future.result()
            windows.extend(remote_windows)
            remote_history.update(history)
    return windows, remote_history


def load_cache(path: Path) -> tuple[list[Window], dict[str, float]] | None:
    try:
        payload = json.loads(path.read_text())
        windows = []
        for item in payload["windows"]:
            panes = [Pane(**pane) for pane in item.pop("panes", [])]
            windows.append(Window(**item, panes=panes))
        return windows, {str(key): float(value) for key, value in payload.get("mru", {}).items()}
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError):
        return None


def save_cache(path: Path, windows: list[Window], remote_history: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "mru": remote_history,
        "windows": [
            {
                "host": window.host,
                "ssh_target": window.ssh_target,
                "session_id": window.session_id,
                "session_name": window.session_name,
                "window_id": window.window_id,
                "window_index": window.window_index,
                "window_name": window.window_name,
                "active": window.active,
                "bell": window.bell,
                "last_view": window.last_view,
                "panes": [pane.__dict__ for pane in window.panes],
            }
            for window in windows
        ]
    }
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")))
    temporary.replace(path)


def remote_attach_command(target: dict[str, str], remote_tmux: str = "") -> str:
    tmux_command = remote_tmux_command(target.get("host", ""), remote_tmux)
    remote = shlex.join(
        [
            *tmux_command,
            "select-window",
            "-t",
            target["window_id"],
            ";",
            "select-pane",
            "-t",
            target["pane"],
            ";",
            "attach-session",
            "-t",
            target["session"],
        ]
    )
    bridge = Path(__file__).resolve().with_name("bridge.sh")
    return shlex.join([str(bridge), target["ssh"], remote])


def select(value: str, remote_tmux: str = "") -> int:
    target = decode_target(value)
    if not target.get("window_id"):
        return 1
    record_mru(target["key"])
    if target["host"] == "local":
        switched = run(["tmux", "switch-client", "-t", target["session"]])
        selected = run(["tmux", "select-window", "-t", target["window_id"]])
        return selected.returncode or switched.returncode

    bridge_name = f"{target['host']}:{target['session']}"
    existing = run(
        [
            "tmux",
            "list-windows",
            "-a",
            "-F",
            "#{window_id}" + SEP + "#{@agent_picker_remote_key}",
        ]
    )
    for line in existing.stdout.splitlines():
        fields = line.split(SEP)
        if len(fields) == 2 and fields[1] == target["key"]:
            return run(["tmux", "select-window", "-t", fields[0]]).returncode

    command = remote_attach_command(target, remote_tmux)
    created = run(
        [
            "tmux",
            "new-window",
            "-P",
            "-F",
            "#{window_id}",
            "-n",
            bridge_name,
            command,
        ]
    )
    window_id = created.stdout.strip()
    if created.returncode == 0 and window_id:
        run(["tmux", "set-option", "-w", "-t", window_id, "@agent_picker_bridge", "1"])
        run(["tmux", "set-option", "-w", "-t", window_id, "@agent_picker_remote_key", target["key"]])
    return created.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sort", choices=("mru", "state"), default="mru")
    parser.add_argument("--hosts", default="")
    parser.add_argument("--current-window", default="")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--ssh-batch-mode", choices=("yes", "no", "auto"), default="yes")
    parser.add_argument("--remote-tmux", default="")
    parser.add_argument("--ssh-retries", type=int, default=0)
    parser.add_argument("--cache-file")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--select")
    args = parser.parse_args()
    if args.select:
        return select(args.select, args.remote_tmux)
    cache_path = Path(args.cache_file).expanduser() if args.cache_file else None
    cached = None if args.refresh_cache else (load_cache(cache_path) if cache_path else None)
    if cached is None:
        windows, remote_history = collect(
            args.hosts, args.timeout, args.ssh_batch_mode, args.remote_tmux, args.ssh_retries
        )
        if cache_path:
            save_cache(cache_path, windows, remote_history)
    else:
        windows, remote_history = cached
    mru = load_mru()
    mru.update(remote_history)
    windows = sort_windows(windows, args.sort, mru)
    for window in windows:
        print(format_row(window, args.current_window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
