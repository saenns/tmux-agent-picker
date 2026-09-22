#!/usr/bin/env python3
"""Update the picker bell overlay without running remote inventory."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def overlay_path() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tmux-agent-picker"
    return root / "bells.json"


def main() -> int:
    result = subprocess.run(
        ["tmux", "list-windows", "-a", "-F", "#{session_id}\t#{window_id}\t#{window_active}\t#{window_bell_flag}\t#{@agent_picker_remote_key}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return result.returncode
    path = overlay_path()
    try:
        values = {str(key): float(value) for key, value in json.loads(path.read_text()).items()}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        values = {}
    remote_bells: set[str] = set()
    remote_views: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 5:
            continue
        session_id, window_id, active, bell, remote_key = fields
        key = remote_key or f"local|{session_id}|{window_id}"
        if remote_key and active == "1":
            remote_views.add(remote_key)
            continue
        if remote_key:
            if bell == "1":
                remote_bells.add(remote_key)
            continue
        if bell == "1":
            values[key] = 1
        else:
            values.pop(key, None)
    # A remote target may have several local bridge windows. Viewing it in any
    # one bridge clears its alert even if an older bridge still has a stale
    # local bell flag.
    for key in remote_views:
        values.pop(key, None)
    for key in remote_bells - remote_views:
        values[key] = 1
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(values, separators=(",", ":")))
    temporary.replace(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
