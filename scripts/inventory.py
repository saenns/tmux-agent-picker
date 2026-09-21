#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
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


def remote_inventory(label: str, target: str, timeout: float) -> list[Window]:
    remote_command = "tmux list-panes -a -F " + shlex.quote(TMUX_FORMAT)
    try:
        result = run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={max(1, int(timeout))}",
                target,
                remote_command,
            ],
            timeout=timeout + 1,
        )
    except (subprocess.TimeoutExpired, OSError):
        result = None
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


def collect(hosts_value: str, timeout: float) -> list[Window]:
    windows = local_inventory()
    hosts = parse_hosts(hosts_value)
    if not hosts:
        return windows
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as executor:
        futures = {
            executor.submit(remote_inventory, label, target, timeout): label
            for label, target in hosts
        }
        for future in as_completed(futures):
            windows.extend(future.result())
    return windows


def remote_attach_command(target: dict[str, str]) -> str:
    remote = "tmux select-window -t {window} \\; select-pane -t {pane} \\; attach-session -t {session}".format(
        window=shlex.quote(target["window_id"]),
        pane=shlex.quote(target["pane"]),
        session=shlex.quote(target["session"]),
    )
    return shlex.join(["ssh", "-tt", target["ssh"], remote])


def select(value: str) -> int:
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

    command = remote_attach_command(target)
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
    parser.add_argument("--select")
    args = parser.parse_args()
    if args.select:
        return select(args.select)
    windows = sort_windows(collect(args.hosts, args.timeout), args.sort, load_mru())
    for window in windows:
        print(format_row(window, args.current_window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
