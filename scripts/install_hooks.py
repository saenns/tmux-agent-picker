#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path


CODEX_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "Stop",
    "Interrupt",
    "SessionEnd",
)
CLAUDE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Notification",
    "Stop",
    "SessionEnd",
)


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def install(path: Path, agent: str, events: tuple[str, ...], command: str) -> None:
    data = load(path)
    hooks = data.setdefault("hooks", {})
    for event in events:
        groups = hooks.setdefault(event, [])
        already_installed = any(
            handler.get("command") == command
            for group in groups
            if isinstance(group, dict)
            for handler in group.get("hooks", [])
            if isinstance(handler, dict)
        )
        if not already_installed:
            groups.append(
                {
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": command, "async": True, "timeout": 5}],
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.bak.{int(time.time())}")
        shutil.copy2(path, backup)
    temporary = path.with_suffix(f".{agent}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)
    print(f"Installed {agent} hooks in {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install tmux-agent-picker lifecycle hooks")
    parser.add_argument("--codex", action="store_true")
    parser.add_argument("--claude", action="store_true")
    args = parser.parse_args()
    if not args.codex and not args.claude:
        args.codex = args.claude = True
    hook = Path(__file__).resolve().with_name("agent_hook.py")
    if args.codex:
        command = f"{hook} --agent codex"
        install(Path.home() / ".codex/hooks.json", "codex", CODEX_EVENTS, command)
    if args.claude:
        command = f"{hook} --agent claude"
        install(Path.home() / ".claude/settings.json", "claude", CLAUDE_EVENTS, command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
