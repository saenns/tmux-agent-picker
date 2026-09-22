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
        ["tmux", "list-windows", "-a", "-F", "#{session_id}\t#{window_id}\t#{window_bell_flag}\t#{@agent_picker_remote_key}"],
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
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        session_id, window_id, bell, remote_key = fields
        key = remote_key or f"local|{session_id}|{window_id}"
        if bell == "1":
            values[key] = 1
        else:
            values.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(values, separators=(",", ":")))
    temporary.replace(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
