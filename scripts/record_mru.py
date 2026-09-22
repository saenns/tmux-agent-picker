#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agent_picker import record_mru  # noqa: E402


def main() -> int:
    window_id = sys.argv[1] if len(sys.argv) > 1 else ""
    remote_key = sys.argv[2] if len(sys.argv) > 2 else ""
    session_id = sys.argv[3] if len(sys.argv) > 3 else ""
    # tmux session IDs begin with "$". When this script is invoked by a
    # run-shell hook, an unescaped value such as "$0" is expanded by the
    # shell, producing "sh" instead of the actual session ID. Derive it from
    # the focused window so MRU records always match inventory keys.
    if window_id and not remote_key:
        session = subprocess.run(
            ["tmux", "display-message", "-p", "-t", window_id, "#{session_id}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if session.returncode == 0 and session.stdout.strip():
            session_id = session.stdout.strip()
    key = remote_key or f"local|{session_id}|{window_id}"
    record_mru(key)
    if window_id and not remote_key:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", window_id, "-F", "#{pane_id}\t#{@agent_picker_state}"],
            text=True,
            capture_output=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            pane, _, state = line.partition("\t")
            if state == "done":
                subprocess.run(
                    ["tmux", "set-option", "-p", "-t", pane, "@agent_picker_state", "idle"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
