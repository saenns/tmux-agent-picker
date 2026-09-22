#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
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
BRIDGE_VERSION = "3"


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
            window = Window(**item, panes=panes)
            # Cache files written before proxy-session filtering may still
            # contain grouped bridge copies. Do not let a stale duplicate
            # hide or outrank its real source-session window.
            if window.session_name.startswith("__tap_"):
                continue
            windows.append(window)
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
    current_session = run(["tmux", "display-message", "-p", "#{session_id}"]).stdout.strip()
    remote_session_key = f"{target['host']}|{target.get('session_id', '')}"
    # This name is stable for one local tmux session and one remote source
    # session. It is deliberately a separate remote session in the same tmux
    # group, so other remote clients cannot change the bridge's selection.
    proxy_digest = hashlib.sha256(f"{current_session}|{remote_session_key}".encode()).hexdigest()[:16]
    proxy_session = f"__tap_{proxy_digest}"
    existing = run(
        [
            "tmux",
            "list-windows",
            "-a",
            "-F", SEP.join((
                "#{session_id}", "#{window_id}", "#{@agent_picker_remote_key}",
                "#{@agent_picker_remote_session_key}", "#{@agent_picker_proxy_session}",
                "#{@agent_picker_bridge_version}", "#{window_active}", "#{window_activity}",
            )),
        ]
    )
    matches: list[tuple[bool, int, str]] = []
    replace: list[str] = []
    for line in existing.stdout.splitlines():
        # tmux 3.5 may render the format separator as the octal spelling
        # instead of the control byte. Without this normalization bridge
        # reuse always misses and opens a new SSH attachment.
        fields = line.replace("\\037", SEP).split(SEP)
        if len(fields) != 8 or fields[0] != current_session:
            continue
        _, window_id, remote_key, existing_session_key, existing_proxy, bridge_version, active, activity = fields
        if remote_key == target["key"] and existing_proxy == proxy_session and bridge_version == BRIDGE_VERSION:
            try:
                activity_value = int(activity or 0)
            except ValueError:
                activity_value = 0
            matches.append((active == "1", activity_value, window_id))
        elif existing_session_key == remote_session_key or remote_key.rsplit("|", 1)[0] == remote_session_key:
            replace.append(window_id)
    if matches:
        _, _, window_id = max(matches)
        # A local bridge name may have been deliberately customized. Keep it
        # intact on reuse; a new bridge receives the current remote title.
        return run(["tmux", "select-window", "-t", window_id]).returncode

    # tmux stores the selected window on a session, not a client. Leaving two
    # bridges attached to one remote session lets each bridge show whichever
    # target was selected most recently. Keep one bridge per remote session.
    for window_id in replace:
        run(["tmux", "kill-window", "-t", window_id])

    command = remote_attach_command(target, remote_tmux, proxy_session)
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
        run(["tmux", "set-option", "-w", "-t", window_id, "@agent_picker_remote_session_key", remote_session_key])
        run(["tmux", "set-option", "-w", "-t", window_id, "@agent_picker_proxy_session", proxy_session])
        run(["tmux", "set-option", "-w", "-t", window_id, "@agent_picker_bridge_version", BRIDGE_VERSION])
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
    # A selection made through this picker is recorded locally immediately.
    # Cached remote focus data can legitimately be older, so never let it
    # demote a just-selected window.
    for key, timestamp in remote_history.items():
        mru[key] = max(mru.get(key, 0), timestamp)
    windows = sort_windows(windows, args.sort, mru)
    for window in windows:
        print(format_row(window, args.current_window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
