#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agent_picker import (  # noqa: E402
    SEP,
    Pane,
    Window,
    decode_target,
    format_row,
    load_mru,
    parse_inventory,
    record_mru,
    scrollback_summary,
    sort_windows,
)
from remote_tmux import collect_remote_windows, remote_attach_command


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


def apply_bell_overlay(windows: list[Window]) -> None:
    """Apply immediate local/bridge bell events to a cached inventory."""
    path = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tmux-agent-picker" / "bells.json"
    try:
        bells = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return
    for window in windows:
        if bells.get(window.key):
            window.bell = True


def apply_remote_watch_overlay(windows: list[Window], history: dict[str, float], cache_path: Path | None) -> None:
    """Replace remote cache rows with snapshots from persistent SSH watchers."""
    if not cache_path:
        return
    cache_key = cache_path.stem.removeprefix("inventory-")
    for path in cache_path.parent.glob(f"watch-{cache_key}-*.json"):
        try:
            payload = json.loads(path.read_text())
            label, target, snapshot = payload["host"], payload["ssh"], payload["snapshot"]
            fresh = parse_inventory(snapshot, label, target)
        except (FileNotFoundError, OSError, ValueError, TypeError, KeyError):
            continue
        if not fresh:
            continue
        windows[:] = [window for window in windows if window.host != label]
        windows.extend(fresh)
        for window in fresh:
            if window.last_view:
                history[window.key] = window.last_view


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


def collect(
    hosts_value: str, timeout: float, batch_mode: str, remote_tmux: str, retries: int
) -> tuple[list[Window], dict[str, float]]:
    windows = local_inventory()
    remote_history: dict[str, float] = {}
    remote_windows, history = collect_remote_windows(
        hosts_value, timeout, batch_mode, remote_tmux, retries, TMUX_FORMAT
    )
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


def select(value: str, remote_tmux: str = "") -> int:
    target = decode_target(value)
    if not target.get("window_id"):
        return 1
    record_mru(target["key"])
    if target["host"] == "local":
        switched = run(["tmux", "switch-client", "-t", target["session"]])
        selected = run(["tmux", "select-window", "-t", target["window_id"]])
        return selected.returncode or switched.returncode

    bridge_name = target.get("bridge_name") or f"{target['host']}:{target['session']}"
    existing = run(
        [
            "tmux",
            "list-windows",
            "-a",
            "-F", SEP.join(("#{window_id}", "#{@agent_picker_remote_key}", "#{window_active}", "#{window_activity}")),
        ]
    )
    matches: list[tuple[bool, int, str]] = []
    for line in existing.stdout.splitlines():
        # tmux 3.5 may render the format separator as the octal spelling
        # instead of the control byte. Without this normalization bridge
        # reuse always misses and opens a new SSH attachment.
        fields = line.replace("\\037", SEP).split(SEP)
        if len(fields) != 4 or fields[1] != target["key"]:
            continue
        try:
            activity = int(fields[3] or 0)
        except ValueError:
            activity = 0
        matches.append((fields[2] == "1", activity, fields[0]))
    if matches:
        _, _, window_id = max(matches)
        run(["tmux", "rename-window", "-t", window_id, bridge_name])
        return run(["tmux", "select-window", "-t", window_id]).returncode

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
        run([str(Path(__file__).resolve().with_name("refresh_bells.py"))])
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
    apply_remote_watch_overlay(windows, remote_history, cache_path)
    apply_bell_overlay(windows)
    mru = load_mru()
    mru.update(remote_history)
    windows = sort_windows(windows, args.sort, mru)
    for window in windows:
        print(format_row(window, args.current_window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
