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


def remote_inventory(
    label: str, target: str, timeout: float, batch_mode: str, remote_tmux: str, retries: int
) -> list[Window]:
    tmux_command = remote_tmux_command(label, remote_tmux)
    remote_command = shlex.join([*tmux_command, "list-panes", "-a", "-F", TMUX_FORMAT])
    ssh_command = ["ssh", "-o", f"ConnectTimeout={max(1, int(timeout))}"]
    # WSSH rejects an explicitly supplied BatchMode option, even when it is
    # set to "no".  "auto" leaves the SSH client defaults untouched.
    if batch_mode != "auto":
        ssh_command.extend(["-o", f"BatchMode={batch_mode}"])
    ssh_command.extend([target, remote_command])
    result = None
    for attempt in range(max(0, retries) + 1):
        try:
            result = run(ssh_command, timeout=timeout + 1)
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
        return parse_inventory(result.stdout, label, target)
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
    ]


def collect(hosts_value: str, timeout: float, batch_mode: str, remote_tmux: str, retries: int) -> list[Window]:
    windows = local_inventory()
    hosts = parse_hosts(hosts_value)
    if not hosts:
        return windows
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as executor:
        futures = {
            executor.submit(remote_inventory, label, target, timeout, batch_mode, remote_tmux, retries): label
            for label, target in hosts
        }
        for future in as_completed(futures):
            windows.extend(future.result())
    return windows


def load_cache(path: Path) -> list[Window] | None:
    try:
        payload = json.loads(path.read_text())
        windows = []
        for item in payload["windows"]:
            panes = [Pane(**pane) for pane in item.pop("panes", [])]
            windows.append(Window(**item, panes=panes))
        return windows
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError):
        return None


def save_cache(path: Path, windows: list[Window]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
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
    return shlex.join(["ssh", "-tt", target["ssh"], remote])


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
    windows = None if args.refresh_cache else (load_cache(cache_path) if cache_path else None)
    if windows is None:
        windows = collect(args.hosts, args.timeout, args.ssh_batch_mode, args.remote_tmux, args.ssh_retries)
        if cache_path:
            save_cache(cache_path, windows)
    windows = sort_windows(windows, args.sort, load_mru())
    for window in windows:
        print(format_row(window, args.current_window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
