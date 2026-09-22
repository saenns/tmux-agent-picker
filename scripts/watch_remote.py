#!/usr/bin/env python3
"""Keep one SSH stream per host and persist changed remote tmux snapshots."""
from __future__ import annotations

import argparse
import base64
import json
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from agent_picker import parse_hosts  # noqa: E402
from remote_tmux import remote_tmux_command  # noqa: E402


def remote_script(tmux_command: list[str], tmux_format: str, interval: float) -> str:
    hook = "set-option -w -t '#{window_id}' @agent_picker_last_view '#{t:%s}'"
    install_hook = shlex.join([*tmux_command, "set-hook", "-g", "after-select-window[999]", hook])
    listing = shlex.join([*tmux_command, "list-panes", "-a", "-F", tmux_format])
    return f'''previous=""
while :; do
  snapshot=$({install_hook} >/dev/null 2>&1; {listing})
  fingerprint=$(printf '%s' "$snapshot" | cksum 2>/dev/null)
  if [ "$fingerprint" != "$previous" ]; then
    printf 'AP1 '
    printf '%s' "$snapshot" | base64 | tr -d '\\n'
    printf '\\n'
    previous=$fingerprint
  fi
  sleep {interval}
done'''


def save(path: Path, label: str, target: str, snapshot: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(f".{time.time_ns()}.tmp")
    temporary.write_text(json.dumps({"host": label, "ssh": target, "updated": time.time(), "snapshot": snapshot}))
    temporary.replace(path)


def watch(label: str, target: str, remote_tmux: str, tmux_format: str, interval: float, path: Path) -> None:
    command = remote_script(remote_tmux_command(label, remote_tmux), tmux_format, interval)
    remote_command = shlex.join(["sh", "-lc", command])
    while True:
        process = subprocess.Popen(
            ["ssh", "-T", "-o", "ControlMaster=no", "-o", "ControlPath=none", target, remote_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            if not line.startswith("AP1 "):
                continue
            try:
                snapshot = base64.b64decode(line[4:].strip()).decode()
            except (ValueError, UnicodeDecodeError):
                continue
            save(path, label, target, snapshot)
        process.wait()
        time.sleep(2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hosts", required=True)
    parser.add_argument("--remote-tmux", default="")
    parser.add_argument("--format", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--cache-key", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    hosts = parse_hosts(args.hosts)
    if not hosts:
        return 0
    root = Path(args.cache_root)
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        for label, target in hosts:
            path = root / f"watch-{args.cache_key}-{label}.json"
            executor.submit(watch, label, target, args.remote_tmux, args.format, args.interval, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
