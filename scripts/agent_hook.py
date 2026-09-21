#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agent_picker import fallback_summary, state_dir  # noqa: E402


def tmux_set(pane: str, name: str, value: str | None) -> None:
    if value is None:
        command = ["tmux", "set-option", "-pu", "-t", pane, name]
    else:
        command = ["tmux", "set-option", "-p", "-t", pane, name, value]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def tmux_global(name: str) -> str:
    result = subprocess.run(
        ["tmux", "show-option", "-gqv", name], text=True, capture_output=True, check=False
    )
    return result.stdout.strip()


def enqueue_summary(pane: str, payload: dict, message: str) -> bool:
    model = tmux_global("@agent-picker-summary-model")
    if not model or not os.environ.get("OPENAI_API_KEY"):
        return False
    turn_id = str(payload.get("turn_id", "")) or hashlib.sha256(message.encode()).hexdigest()[:20]
    jobs = state_dir() / "summary-jobs"
    jobs.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_path = jobs / f"{hashlib.sha256((pane + turn_id).encode()).hexdigest()}.json"
    temporary = job_path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "pane": pane,
                "turn_id": turn_id,
                "session_id": str(payload.get("session_id", "")),
                "model": model,
                "message": message[:12000],
            },
            separators=(",", ":"),
        )
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, job_path)
    worker = Path(__file__).resolve().with_name("summarize.py")
    tmux_set(pane, "@agent_picker_summary_turn", turn_id)
    tmux_set(pane, "@agent_picker_summary_status", "pending")
    try:
        subprocess.Popen(
            [str(worker), "--job", str(job_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        tmux_set(pane, "@agent_picker_summary_status", "local")
        try:
            job_path.unlink()
        except OSError:
            pass
        return False
    return True


def event_state(event: str, payload: dict) -> str | None:
    if event in {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact", "PostCompact", "SubagentStart"}:
        return "working"
    if event == "PermissionRequest":
        return "waiting"
    if event == "Notification":
        notification = str(payload.get("notification_type", payload.get("type", ""))).lower()
        return "waiting" if any(word in notification for word in ("permission", "input", "question")) else None
    if event in {"Stop", "SubagentStop"}:
        return "done"
    if event in {"Interrupt", "SessionStart"}:
        return "idle"
    if event == "SessionEnd":
        return "clear"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True, choices=("codex", "claude"))
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        payload = {}
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return 0
    event = str(payload.get("hook_event_name", payload.get("event_name", "")))
    state = event_state(event, payload)
    if state == "clear":
        for option in ("@agent_picker_state", "@agent_picker_agent", "@agent_picker_session_id", "@agent_picker_updated"):
            tmux_set(pane, option, None)
        return 0
    if not state:
        return 0
    session_id = str(payload.get("session_id", payload.get("thread_id", "")))
    tmux_set(pane, "@agent_picker_state", state)
    tmux_set(pane, "@agent_picker_agent", args.agent)
    tmux_set(pane, "@agent_picker_session_id", session_id)
    tmux_set(pane, "@agent_picker_updated", str(int(time.time())))
    if event in {"Stop", "SubagentStop"}:
        message = str(payload.get("last_assistant_message") or "")
        immediate = fallback_summary(message)
        if immediate:
            tmux_set(pane, "@agent_picker_summary", immediate)
            tmux_set(pane, "@agent_picker_summary_status", "local")
        enqueue_summary(pane, payload, message) if message else None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
